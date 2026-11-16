"""Judgements the program makes come from configuration, and the guesses got wider.

An audit for hardcoding found the program deciding things from values written
into code: musl recognised on one architecture, fish looked for at one path,
fourteen public suffixes standing in for the public suffix list, a 4000
character window fixed in three places, credential words and failure wordings
as regular expressions, and a generated bridge claiming one protocol revision
and a version that was never the program's. Each is now a setting (or found on
the machine), with the code's fallback equal to the packaged value -- which
``test_config_fallbacks`` enforces for every read.
"""

from __future__ import annotations

import json
import subprocess
import sys
from types import SimpleNamespace

import pytest

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - 3.10
    import tomli as tomllib  # type: ignore[no-redef]

from initialize_claude_ai import __version__
from initialize_claude_ai.core import envfile, platform_detect
from initialize_claude_ai.core.envfile import EnvStore
from initialize_claude_ai.mcp_auto_discovery import grounding, runtime, secret_policy
from initialize_claude_ai.mcp_auto_discovery.bridge import BridgeBuilder
from initialize_claude_ai.mcp_auto_discovery.probes import McpProber

DATA = __import__("pathlib").Path(platform_detect.__file__).resolve().parent.parent / "data"
MCP = tomllib.loads((DATA / "40-mcp.toml").read_text(encoding="utf-8"))["mcp"]


# -- the platform -----------------------------------------------------------------------


@pytest.mark.parametrize("arch", ["x86_64", "aarch64", "powerpc64le", "s390x", "riscv64"])
def test_musl_is_recognised_on_any_architecture(monkeypatch, tmp_path, arch) -> None:
    loader = tmp_path / f"ld-musl-{arch}.so.1"
    loader.write_text("")
    monkeypatch.setattr(platform_detect.platform, "libc_ver", lambda: ("", ""))
    monkeypatch.setattr(platform_detect, "MUSL_LOADER_GLOB", str(tmp_path / "ld-musl-*.so.1"))
    assert platform_detect._detect_libc() == "musl"


def test_no_musl_loader_means_no_musl(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(platform_detect.platform, "libc_ver", lambda: ("", ""))
    monkeypatch.setattr(platform_detect, "MUSL_LOADER_GLOB", str(tmp_path / "ld-musl-*.so.1"))
    assert platform_detect._detect_libc() == ""


@pytest.mark.parametrize(
    ("shell", "on_path", "written"),
    [
        ("/bin/bash", "/home/linuxbrew/.linuxbrew/bin/fish", True),
        ("/usr/local/bin/fish", None, True),
        ("/bin/bash", None, False),
    ],
)
def test_fish_is_found_wherever_it_is_installed(monkeypatch, tmp_path, shell, on_path, written) -> None:
    monkeypatch.setenv("SHELL", shell)
    monkeypatch.setattr(envfile.shutil, "which", lambda name: on_path if name == "fish" else None)
    store = EnvStore(state_dir=tmp_path, dry_run=False)
    store.set("EXAMPLE", "1", apply=False)
    store.save(force=True)
    assert store.fish_path.exists() is written


# -- public suffixes ----------------------------------------------------------------------


PSL = """// ===BEGIN ICANN DOMAINS===
co.uk
kawasaki.jp
*.ck
!www.ck
// ===END ICANN DOMAINS===
// ===BEGIN PRIVATE DOMAINS===
github.io
// ===END PRIVATE DOMAINS===
"""


def test_the_packaged_suffixes_are_the_code_s() -> None:
    assert tuple(MCP["grounding"]["multi_part_suffixes"]) == grounding.MULTI_PART_SUFFIXES


@pytest.mark.parametrize(
    ("host", "organisation"),
    [
        ("api.example.co.uk", "example.co.uk"),
        ("www.example.com.au", "example.com.au"),
        ("api.example.co.kr", "example.co.kr"),
        ("build.opensuse.org", "opensuse.org"),
    ],
)
def test_the_packaged_list_alone_knows_the_common_registries(host, organisation) -> None:
    grounding.configure_suffixes(public_suffix_file="")
    assert grounding.registrable_domain(host) == organisation


def test_the_system_list_adds_what_the_packaged_one_lacks(tmp_path) -> None:
    listed = tmp_path / "public_suffix_list.dat"
    listed.write_text(PSL)
    assert grounding.registrable_domain("a.foo.kawasaki.jp") == "kawasaki.jp"
    grounding.configure_suffixes(public_suffix_file=str(listed))
    assert grounding.registrable_domain("a.foo.kawasaki.jp") == "foo.kawasaki.jp"
    assert grounding.registrable_domain("x.y.ck") == "x.y.ck"  # *.ck
    assert grounding.registrable_domain("a.www.ck") == "www.ck"  # !www.ck
    assert not grounding.same_organisation("example.co.uk", "other.co.uk")


def test_private_suffixes_count_only_when_asked(tmp_path) -> None:
    listed = tmp_path / "public_suffix_list.dat"
    listed.write_text(PSL)
    grounding.configure_suffixes(public_suffix_file=str(listed))
    assert grounding.same_organisation("alice.github.io", "bob.github.io")
    grounding.configure_suffixes(public_suffix_file=str(listed), sections=["icann", "private"])
    assert not grounding.same_organisation("alice.github.io", "bob.github.io")


def test_a_missing_list_leaves_the_packaged_suffixes(tmp_path) -> None:
    grounding.configure_suffixes(public_suffix_file=str(tmp_path / "absent.dat"))
    assert grounding.registrable_domain("api.example.co.uk") == "example.co.uk"


def test_phase_four_reads_the_suffix_settings(ctx, tmp_path) -> None:
    from initialize_claude_ai.phases.phase4_mcp_auto_discovery import McpPhase

    listed = tmp_path / "public_suffix_list.dat"
    listed.write_text(PSL)
    ctx.settings.set("mcp.grounding.public_suffix_file", str(listed))
    McpPhase(ctx)._declare_public_suffixes()
    assert grounding.registrable_domain("a.foo.kawasaki.jp") == "foo.kawasaki.jp"


# -- how much of a reply is read -----------------------------------------------------------


def test_a_marker_past_the_window_is_found_when_the_window_is_widened(ctx) -> None:
    ctx.settings.set("mcp.discovery.auth.credential_refusal_statuses", [400])
    ctx.settings.set("mcp.discovery.auth.credential_refusal_markers", ["no api key"])
    reply = SimpleNamespace(status=400, text="x" * 5000 + " no api key", headers={})
    assert McpProber(ctx).credential_refusal(reply) == ""
    ctx.settings.set("mcp.discovery.response_scan_chars", 0)
    assert McpProber(ctx).credential_refusal(reply) == "no api key"


# -- what counts as a credential ------------------------------------------------------------


def test_the_packaged_secret_policy_is_the_code_s() -> None:
    policy = MCP["secret_policy"]
    assert tuple(policy["sensitive_words"]) == secret_policy.SENSITIVE_WORDS
    assert tuple(policy["selector_words"]) == secret_policy.SELECTOR_WORDS
    assert tuple(policy["inline_credential_patterns"]) == secret_policy.INLINE_CREDENTIAL_PATTERNS
    assert tuple(policy["not_a_secret_words"]) == secret_policy.NOT_A_SECRET_WORDS
    assert tuple(policy["not_opaque_starts"]) == secret_policy.NOT_OPAQUE_STARTS


def test_configuring_the_packaged_policy_changes_nothing(settings) -> None:
    names = ["GITHUB_TOKEN", "BUGZILLA_AUTH_MODE", "NODE_TLS_REJECT_UNAUTHORIZED", "SESSION_TIMEOUT", "API_KEY"]
    before = [secret_policy.is_secret_name(n) for n in names]
    secret_policy.configure(settings)
    assert [secret_policy.is_secret_name(n) for n in names] == before
    assert secret_policy.holds_a_credential("X", "Bearer abcdefghijkl")
    assert not secret_policy.holds_a_credential("SESSION_TIMEOUT", "30")


def test_an_operator_s_word_reaches_both_pipelines(settings) -> None:
    assert not secret_policy.is_secret_name("SERVICE_PASSPHRASE")
    settings.set("mcp.secret_policy.sensitive_words", [*secret_policy.SENSITIVE_WORDS, "passphrase"])
    secret_policy.configure(settings)
    assert secret_policy.is_secret_name("SERVICE_PASSPHRASE")
    assert secret_policy.holds_a_credential("SERVICE_PASSPHRASE", "correct horse")


# -- a generated bridge's versions ------------------------------------------------------------


def _initialize(ctx, tmp_path, requested: str) -> dict:
    ctx.dry_run = False
    ctx.settings.set("mcp.bridge.output_dir", str(tmp_path / "bridges"))
    ctx.settings.set("mcp.bridge.generated.auth.probe_challenge", False)
    spec = {
        "openapi": "3.0.0",
        "info": {"title": "t", "version": "1"},
        "servers": [{"url": "http://127.0.0.1:9"}],
        "paths": {"/x": {"get": {"operationId": "get_x", "responses": {"200": {}}}}},
    }
    _candidate, artifacts, detail = BridgeBuilder(ctx).generate_from_openapi(
        "api.example.test", spec, base_url="http://127.0.0.1:9"
    )
    assert artifacts is not None, detail
    message = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {"protocolVersion": requested, "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}},
    }
    run = subprocess.run(
        [sys.executable, str(artifacts.script)],
        input=json.dumps(message) + "\n",
        capture_output=True,
        text=True,
        timeout=30,
        env={"MCP_BRIDGE_MANIFEST": str(artifacts.manifest), "PATH": "/usr/bin:/bin"},
    )
    return json.loads(run.stdout.splitlines()[0])["result"]


def test_the_bridge_answers_in_the_client_s_revision(ctx, tmp_path) -> None:
    versions = MCP["protocol"]["versions"]
    assert _initialize(ctx, tmp_path, versions[-1])["protocolVersion"] == versions[-1]


def test_the_bridge_offers_its_newest_revision_to_an_unknown_one(ctx, tmp_path) -> None:
    result = _initialize(ctx, tmp_path, "1999-01-01")
    assert result["protocolVersion"] == MCP["protocol"]["versions"][0]
    assert result["serverInfo"]["version"] == __version__


# -- agent failure classes --------------------------------------------------------------------


def test_the_packaged_failure_wordings_are_the_code_s() -> None:
    packaged = MCP["agent"]["failure_patterns"]
    assert {k: tuple(v) for k, v in packaged.items()} == runtime.FAILURE_PATTERNS


def test_a_configured_wording_classes_a_failure() -> None:
    text = "the gateway said: circuit open"
    assert runtime.failure_kind(text) == runtime.OTHER
    assert runtime.failure_kind(text, patterns={"transient": ["circuit open"]}) == runtime.TRANSIENT
    # A class left out keeps its built-in wordings; one given is replaced.
    assert runtime.failure_kind("rate limit hit", patterns={"refusal": ["nothing"]}) == runtime.TRANSIENT
    assert runtime.failure_kind("rate limit hit", patterns={"transient": ["circuit open"]}) == runtime.OTHER
