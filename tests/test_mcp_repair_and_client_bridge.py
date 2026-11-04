"""Repair until it works; a client bridge that never depends on the client's setup here.

build-example-net's login sat under a variable mcp-openapi-proxy never reads, and
api.example.net asks for SSH-signed requests that only osc performs: each check
found the problem and the run stopped there. Now every correctable failure is
repaired and proved again, and a service whose login only its client performs
gets a bridge that drives the client -- with a configuration of its own, built
from the entry's variables, so whether osc is set up on this machine changes
nothing. What only the operator can do is a checklist with a recheck. The same
user name and password may work on several servers, and is never reported.
"""

from __future__ import annotations

import json
import os
import sys

import pytest

from initialize_claude_ai.core.secrets import Secret
from initialize_claude_ai.mcp_auto_discovery import client_bridge, login_check, repair, secret_policy
from initialize_claude_ai.mcp_auto_discovery.types import Candidate, Handshake, Stage, TargetOutcome
from initialize_claude_ai.mcp_auto_discovery.verifier import CandidateVerifier

TOKEN = "".join(chr(c) for c in (115, 115, 111, 45, 112, 119, 45, 51))


@pytest.fixture
def live(ctx, tmp_path):
    ctx.dry_run = False
    ctx.runner.which = lambda name, **_: name  # type: ignore[assignment]
    ctx.settings.set("mcp.packages.trial_timeout_seconds", 20)
    ctx.settings.set("mcp.bridge.output_dir", str(tmp_path / "bridges"))
    return ctx


# -- the same login on several servers ----------------------------------------------------------


def registrations(sandbox, entries: dict) -> None:
    (sandbox / ".mcp.json").write_text(json.dumps({"mcpServers": entries}))


def test_tokens_belong_to_one_server_passwords_do_not(settings) -> None:
    assert secret_policy.instance_bound("GITLAB_TOKEN", settings) and secret_policy.instance_bound(
        "BUGZILLA_API_KEY", settings
    )
    assert not secret_policy.instance_bound("Example_PASSWORD", settings)
    assert not secret_policy.instance_bound("JIRA_USERNAME", settings)


def test_the_same_user_name_and_password_on_several_servers_is_never_reported(ctx, sandbox) -> None:
    from initialize_claude_ai.mcp_auto_discovery.existing import ExistingRegistrations
    from initialize_claude_ai.mcp_auto_discovery.writer import McpWriter

    ctx.settings.set("mcp.discovery.reuse.sources", ["project_file"])
    ctx.settings.set("mcp.mcp_json_path", str(sandbox / ".mcp.json"))
    registrations(
        sandbox,
        {
            "build-example-example": {
                "command": "x",
                "env": {"URL": "https://build.example.example", "OBS_PASSWORD": TOKEN},
            },
            "gitlab-example-example": {
                "command": "y",
                "env": {"URL": "https://gitlab.example.example", "GL_PASSWORD": TOKEN},
            },
            "build-opensuse-example": {
                "command": "z",
                "env": {"URL": "https://api.opensuse.example", "OBS_PASSWORD": TOKEN},
            },
        },
    )
    ExistingRegistrations.for_run(ctx).registrations()
    assert [f for f in McpWriter(ctx).shared_secret_findings() if f.get("same_value")] == []


def test_a_token_on_two_servers_is_reported(ctx, sandbox) -> None:
    from initialize_claude_ai.mcp_auto_discovery.existing import ExistingRegistrations
    from initialize_claude_ai.mcp_auto_discovery.writer import McpWriter

    ctx.settings.set("mcp.discovery.reuse.sources", ["project_file"])
    ctx.settings.set("mcp.mcp_json_path", str(sandbox / ".mcp.json"))
    registrations(
        sandbox,
        {
            "a": {"command": "x", "env": {"URL": "https://jira.example.example", "JIRA_TOKEN": TOKEN}},
            "b": {"command": "y", "env": {"URL": "https://sd.example.example", "JIRA_TOKEN": TOKEN}},
        },
    )
    ExistingRegistrations.for_run(ctx).registrations()
    assert [f for f in McpWriter(ctx).shared_secret_findings() if f.get("same_value")]


# -- the client bridge ----------------------------------------------------------------------------------


FAKE_OSC = """#!{python}
import os, sys
config_path = os.environ.get("OSC_CONFIG", "")
home = os.environ.get("HOME", "")
real_oscrc = os.path.join(os.environ["REAL_HOME"], ".config", "osc", "oscrc")
config = open(config_path).read() if config_path and os.path.exists(config_path) else ""
isolated = os.path.basename(home).startswith("mcp-client-") and os.environ.get("XDG_CONFIG_HOME") == home
logged_in = "user = operator" in config and ("sshkey = " + os.environ["EXPECTED_KEY"]) in config and "pass =" in config
if not isolated or not logged_in or os.path.exists(os.path.join(home, ".config", "osc", "oscrc")):
    sys.stderr.write("401 Unauthorized\\n")
    sys.exit(1)
args = sys.argv[1:]
if args[-1] == "whois":
    print("operator: Operator <operator@example.test>")
elif args[2] == "api":
    print("<collection matches='1'><package name='qemu' project='Virtualization'/></collection> " + args[3])
"""


@pytest.fixture
def osc(tmp_path, monkeypatch):
    binary = tmp_path / "bin" / "osc"
    binary.parent.mkdir()
    binary.write_text(FAKE_OSC.replace("{python}", sys.executable))
    binary.chmod(0o755)
    monkeypatch.setenv("PATH", f"{binary.parent}:{os.environ['PATH']}")
    real_home = tmp_path / "operator-home"
    (real_home / ".config" / "osc").mkdir(parents=True)
    (real_home / ".config" / "osc" / "oscrc").write_text("[general]\napiurl = https://somewhere.else\n")
    monkeypatch.setenv("REAL_HOME", str(real_home))
    key = tmp_path / "id_ed25519.pub"
    key.write_text("not a real key")
    monkeypatch.setenv("EXPECTED_KEY", str(key))
    return key


#: A service whose only login is one a client performs (SSH-key signed requests).
PLAN = {
    "chosen": {"method": "client", "client": "osc"},
    "alternatives": [],
    "client_only": True,
    "offered": [{"host": "api.example.test", "scheme": "Signature", "kind": "client", "method": "client"}],
}


def bridge(live) -> Candidate:
    candidate, artifacts, detail = client_bridge.generate(
        live, "https://build.example.test", "build.example.test", PLAN
    )
    assert candidate is not None, detail
    for name in ("REAL_HOME", "EXPECTED_KEY"):
        candidate.env[name] = os.environ[name]
    return candidate


def test_the_client_bridge_reads_its_login_from_named_variables(live, osc) -> None:
    candidate = bridge(live)
    assert set(candidate.secret_env) == {"BUILD_EXAMPLE_TEST_USERNAME"}, "Signature: the user is a credential"
    assert candidate.env["BUILD_EXAMPLE_TEST_SSH_KEY_FILE"] == "${BUILD_EXAMPLE_TEST_SSH_KEY_FILE:-}", (
        "the key file is optional"
    )
    manifest = json.loads(open(candidate.env["MCP_CLIENT_MANIFEST"]).read())
    assert manifest["key_part"] == "BUILD_EXAMPLE_TEST_SSH_KEY_FILE" and manifest["key_files"]
    assert manifest["read"] == ["osc", "-A", "https://api.example.test", "api", "{path}"]
    assert manifest["config_variable"] == "OSC_CONFIG" and "HOME" in manifest["isolate"]
    assert candidate.env["MCP_CLIENT_SERVICE"] == "https://build.example.test"


def test_without_its_variables_it_waits_for_them_like_any_entry(live, osc) -> None:
    candidate = CandidateVerifier(live).verify(bridge(live))
    assert candidate.needs_credential and not candidate.verified
    assert set(candidate.handshake.credential_pending) == {"BUILD_EXAMPLE_TEST_USERNAME"}, "never waits for a key file"


def test_with_its_variables_it_works_and_never_reads_this_machine_s_osc_setup(live, osc, monkeypatch) -> None:
    values = {"env:BUILD_EXAMPLE_TEST_USERNAME": "operator"}
    live.secrets.resolve = lambda reference, **_: Secret(values[reference]) if reference in values else None  # type: ignore[assignment]
    monkeypatch.setenv("BUILD_EXAMPLE_TEST_SSH_KEY_FILE", str(osc))
    live.settings.set(
        "mcp.contract.targets",
        {
            "build.example.test": {
                "checks": [
                    {
                        "name": "search packages named qemu",
                        "tools": ["^search_packages$"],
                        "arguments": {"^name$": "qemu"},
                        "expect": ["qemu"],
                    }
                ]
            }
        },
    )
    candidate = CandidateVerifier(live).verify(bridge(live))
    assert candidate.verified, candidate.handshake.error
    assert {r["task"] for r in candidate.handshake.contract if r["passed"]} >= {"search packages named qemu"}


def test_a_password_comes_before_an_ssh_key(settings) -> None:
    """The old ranking, kept reachable: with methods_equal off, a password still comes before a key."""
    settings.set("mcp.discovery.auth.methods_equal", False)
    client = settings.dict_("mcp.discovery.clients")["osc"]
    plan = {
        "offered": [
            {"host": "api.example.example", "scheme": "Signature", "kind": "client", "method": "client"},
            {"host": "build.example.example", "scheme": "basic", "kind": "header", "method": "basic"},
        ]
    }
    assert client_bridge.route(client, plan, "build.example.example", settings) == (
        "basic",
        ["USERNAME", "PASSWORD"],
        "build.example.example",
    )


def test_with_methods_equal_the_api_hosts_login_comes_first_whatever_its_kind(settings) -> None:
    """build.example.net: api.example.net offers only a signature; no kind of login is preferred, the API host decides."""
    client = settings.dict_("mcp.discovery.clients")["osc"]
    plan = {
        "offered": [
            {"host": "build.example.example", "scheme": "basic", "kind": "header", "method": "basic"},
            {"host": "api.example.example", "scheme": "Signature", "kind": "client", "method": "client"},
        ]
    }
    assert client_bridge.route(client, plan, "build.example.example", settings)[0] == "signature"
    assert [r[0] for r in client_bridge.routes(client, plan, "build.example.example", settings)] == [
        "signature",
        "basic",
    ]


def test_the_api_host_that_offers_the_login_is_used(settings) -> None:
    """build.opensuse.org's own login does not know an openSUSE account; api.opensuse.org does."""
    client = settings.dict_("mcp.discovery.clients")["osc"]
    plan = {
        "offered": [
            {"host": "build.opensuse.example", "scheme": "basic", "kind": "header", "method": "basic"},
            {"host": "api.opensuse.example", "scheme": "Basic", "kind": "header", "method": "basic"},
        ]
    }
    assert client_bridge.route(client, plan, "build.opensuse.example", settings)[2] == "api.opensuse.example"


def test_an_ssh_key_only_when_nothing_else_is_offered(settings) -> None:
    client = settings.dict_("mcp.discovery.clients")["osc"]
    assert client_bridge.route(client, PLAN, "build.example.test", settings) == (
        "signature",
        ["USERNAME", "SSH_KEY_FILE"],
        "api.example.test",
    )


def test_a_key_path_variable_holding_anything_else_is_named_plainly(live, osc, monkeypatch) -> None:
    values = {"env:BUILD_EXAMPLE_TEST_USERNAME": "operator"}
    live.secrets.resolve = lambda reference, **_: Secret(values[reference]) if reference in values else None  # type: ignore[assignment]
    monkeypatch.setenv("BUILD_EXAMPLE_TEST_SSH_KEY_FILE", "mN17notakeypathAAAAAAAAA")
    candidate = CandidateVerifier(live).verify(bridge(live))
    rows = [r for r in candidate.handshake.functional]
    assert any("must be the path of an existing key file" in str(r.get("detail")) for r in rows), rows


def test_with_no_key_variable_the_standard_key_files_are_tried(live, osc, monkeypatch, tmp_path) -> None:
    """The operator never set a key variable and runs no agent: ssh's own key files are used, the private one."""
    home = tmp_path / "keyhome"
    (home / ".ssh").mkdir(parents=True)
    (home / ".ssh" / "id_rsa").write_text("not a real key")
    (home / ".ssh" / "id_rsa.pub").write_text("not a real key")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("SSH_AUTH_SOCK", raising=False)
    monkeypatch.delenv("BUILD_EXAMPLE_TEST_SSH_KEY_FILE", raising=False)
    monkeypatch.setenv("EXPECTED_KEY", str(home / ".ssh" / "id_rsa"))
    values = {"env:BUILD_EXAMPLE_TEST_USERNAME": "operator"}
    live.secrets.resolve = lambda reference, **_: Secret(values[reference]) if reference in values else None  # type: ignore[assignment]
    candidate = bridge(live)
    candidate.env["HOME"] = str(home)
    candidate = CandidateVerifier(live).verify(candidate)
    assert candidate.verified, candidate.handshake.error


def test_every_key_refused_is_said_with_what_to_do(live, osc, monkeypatch, tmp_path) -> None:
    home = tmp_path / "keyhome2"
    (home / ".ssh").mkdir(parents=True)
    (home / ".ssh" / "id_rsa").write_text("not registered")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("BUILD_EXAMPLE_TEST_SSH_KEY_FILE", raising=False)
    values = {"env:BUILD_EXAMPLE_TEST_USERNAME": "operator"}
    live.secrets.resolve = lambda reference, **_: Secret(values[reference]) if reference in values else None  # type: ignore[assignment]
    candidate = bridge(live)
    candidate.env["HOME"] = str(home)
    candidate = CandidateVerifier(live).verify(candidate)
    details = " ".join(str(r.get("detail")) for r in candidate.handshake.functional)
    assert "every SSH key was tried" in details and "refused its signature" in details, details


def test_each_login_route_gets_its_own_bridge_and_manifest(live) -> None:
    plan = {
        "chosen": {"method": "basic"},
        "alternatives": [{"method": "client", "client": "osc"}],
        "offered": [
            {"host": "api.example.test", "scheme": "Signature", "kind": "client", "method": "client"},
            {"host": "build.example.test", "scheme": "basic", "kind": "header", "method": "basic"},
        ],
    }
    made = client_bridge.generate_all(live, "https://build.example.test", "build.example.test", plan)
    manifests = {c.env["MCP_CLIENT_MANIFEST"] for c, _a, _d in made if c is not None}
    assert len(made) == 2 and len(manifests) == 2, "a later route never rewrites what an entry runs"
    assert {c.name for c, _a, _d in made} == {
        "build-example-test-client-bridge-basic",
        "build-example-test-client-bridge-signature",
    }


# -- repairs -----------------------------------------------------------------------------------------


HEADER_SERVER = """
import json, os, sys
# reads its credential only from EXTRA_HEADERS (and API_KEY, which it does not use here)
headers = os.environ.get("EXTRA_HEADERS", "")
key = os.environ.get("API_KEY", "")
ok = headers == "Authorization: Basic " + os.environ["EXPECTED"]
tools = [{"name": "get_my_account", "inputSchema": {"type": "object", "properties": {}}}]
for line in sys.stdin:
    msg = json.loads(line)
    rid, method = msg.get("id"), msg.get("method")
    if rid is None:
        continue
    if method == "initialize":
        result = {"protocolVersion": msg["params"]["protocolVersion"], "capabilities": {"tools": {}},
                  "serverInfo": {"name": "proxy", "version": "1"}}
    elif method == "tools/list":
        result = {"tools": tools}
    elif method == "tools/call":
        result = ({"content": [{"type": "text", "text": json.dumps({"login": "operator"})}]} if ok else
                  {"isError": True, "content": [{"type": "text", "text": "401 Unauthorized"}]})
    else:
        result = {}
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": rid, "result": result}) + "\\n")
    sys.stdout.flush()
"""


def test_a_credential_under_an_unread_name_is_moved_and_proved(live, tmp_path) -> None:
    script = tmp_path / "proxy.py"
    script.write_text(HEADER_SERVER)
    live.secrets.resolve = lambda reference, **_: Secret(TOKEN) if reference == "env:BUILD_BASE64" else None  # type: ignore[assignment]
    candidate = Candidate(
        kind="stdio",
        name="build-example-test",
        command=[sys.executable, str(script)],
        env={"URL": "https://build.example.test", "EXPECTED": TOKEN},
    )
    candidate.secret_env = {"BUILD_EXAMPLE_TEST_BASE64_OF_USER_PASSWORD": "env:BUILD_BASE64"}
    verifier = CandidateVerifier(live)
    broken = verifier.verify(candidate)
    assert "unread_credential" in repair.classify(broken)
    plan = {"offered": [{"host": "build.example.test", "scheme": "Basic", "kind": "header", "method": "basic"}]}
    context = {
        "settings": live.settings,
        "ctx": live,
        "target": "https://build.example.test",
        "host": "build.example.test",
        "plan": plan,
    }
    fixed, tried = repair.run(broken, context, verifier.verify)
    assert fixed is not None, tried
    assert fixed.env["EXTRA_HEADERS"] == "Authorization: Basic ${BUILD_BASE64}" and fixed.verified


def test_a_scheme_word_as_a_header_name_is_fixed(settings) -> None:
    candidate = Candidate(
        kind="stdio", name="x", env={"MCP_BRIDGE_AUTH_HEADER": "Bearer", "MCP_BRIDGE_AUTH_VALUE": "${BUILD_TOKEN}"}
    )
    candidate.handshake = Handshake(ok=True)
    [fixed] = repair.fix_header_name(candidate, {"settings": settings})
    assert fixed.env["MCP_BRIDGE_AUTH_HEADER"] == "Authorization"
    assert fixed.env["MCP_BRIDGE_AUTH_VALUE"] == "Bearer ${BUILD_TOKEN}"


def test_what_only_the_operator_can_fix_gets_no_repair(settings) -> None:
    candidate = Candidate(kind="stdio", name="x")
    candidate.handshake = Handshake(ok=True, login=login_check.NO_ACCESS, login_reason=login_check.NO_ACCOUNT)
    assert repair.classify(candidate) == ["no_account"]
    assert all(r == "operator" for _, r in repair.repairs_for(settings, ["no_account"]))


def test_a_trial_expands_references_the_way_claude_code_will(live, tmp_path) -> None:
    script = tmp_path / "proxy.py"
    script.write_text(HEADER_SERVER)
    live.secrets.resolve = lambda reference, **_: Secret(TOKEN) if reference == "env:BUILD_BASE64" else None  # type: ignore[assignment]
    candidate = Candidate(
        kind="stdio",
        name="x",
        command=[sys.executable, str(script)],
        env={"EXTRA_HEADERS": "Authorization: Basic ${BUILD_BASE64}", "EXPECTED": TOKEN},
    )
    assert CandidateVerifier(live).verify(candidate).handshake.ok


# -- the checklist and the recheck ------------------------------------------------------------------------


def test_a_waiting_entry_is_on_the_checklist_with_its_recheck(live) -> None:
    from initialize_claude_ai.mcp_auto_discovery.writer import McpWriter, Registration

    candidate = Candidate(kind="stdio", name="build-example-test-client-bridge")
    candidate.handshake = Handshake(
        ok=True, tools=["whoami"], round_trip=True, credential_pending=["BUILD_EXAMPLE_TEST_USERNAME"]
    )
    writer = McpWriter(live)
    registration = Registration(
        name="build-example-test", candidate=candidate, entry={}, needs_env=["BUILD_EXAMPLE_TEST_USERNAME"]
    )
    writer._operator_steps(registration, TargetOutcome(target="https://build.example.test"))
    [step] = writer.operator_steps
    assert "BUILD_EXAMPLE_TEST_USERNAME" in step["step"] and "--mcp-recheck build.example.test" in step["confirm"]


def test_a_recheck_searches_nothing(ctx) -> None:
    from initialize_claude_ai.mcp_auto_discovery.traditional import TraditionalPipeline

    ctx.settings.set("mcp.recheck", True)
    pipeline = TraditionalPipeline(ctx)
    assert pipeline.rechecking
    stage = pipeline.recheck_stage(Stage.PACKAGE)
    assert stage.findings["recheck"] and not stage.candidates


def test_packaged_settings_equal_the_code(settings) -> None:
    assert settings.dict_("mcp.repair.classes") == repair.DEFAULT_CLASSES
    assert settings.int_("mcp.repair.max_variants") == repair.DEFAULT_MAX_VARIANTS
    auth = settings.section("mcp.discovery.auth")
    assert tuple(auth.str_list("instance_bound_patterns")) == secret_policy.DEFAULT_INSTANCE_BOUND
    assert settings.bool_("mcp.recheck") is False
    assert "osc" in settings.dict_("mcp.discovery.clients")
