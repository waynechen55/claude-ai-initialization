"""build-example-net written with the route that cannot work; confluence's token blamed; smelt's bundle short of a root.

* With no credential in the run, every candidate only waited, and the tie fell
  to confidence: a password bridge to build.example.net (refused for this account)
  was written over the SSH-key route at api.example.net that the run's own plan had
  chosen. Ties among unproved candidates are now broken by evidence, and the
  others are kept for the next run to prove.
* mcp-atlassian said "Authentication failed … (403). Token may be expired or
  invalid": 403 is an account without access, and the stated status decides.
* The run's trust bundle carried smelt.example.net's chain only to a cross-signed
  root; Node failed. A chain is completed to a self-signed root from issuers
  this machine has, and expiry is named.
* Written entries pointed into the run's scratch area; their files now move to
  the server's own folder. Plain-text credentials in the project file move to
  Claude Code's settings env. The agent SDK's stderr is kept.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from fake_secrets import fake

from initialize_claude_ai.core.paths_home import artifacts_home
from initialize_claude_ai.mcp_auto_discovery import alternatives, evidence, login_check, login_history, trust
from initialize_claude_ai.mcp_auto_discovery.types import Candidate, Handshake, StageOutcome

sys.path.insert(0, str(Path(__file__).parent))
import test_mcp_trust_chain_complete as _tls  # noqa: E402  (its real TLS server and PKI)

HOST = _tls.HOST
pki, server, real_tls = _tls.pki, _tls.server, _tls.real_tls

PLAN = {
    "offered": [
        {"method": "basic", "scheme": "basic", "host": "build.example.test"},
        {"method": "client", "scheme": "signature", "host": "api.example.test", "kind": "client"},
    ]
}


def waiting(name: str, pending: list[str], *, scheme: str = "", confidence: float = 0.5) -> Candidate:
    candidate = Candidate(kind="stdio", name=name, command=["python3", f"/x/{name}.py"], confidence=confidence)
    candidate.secret_env = {p: f"env:{p}" for p in pending}
    candidate.login_scheme = scheme
    candidate.handshake = Handshake(ok=True, tools=["whoami"], credential_pending=list(pending))
    return candidate


# -- ties broken by evidence -------------------------------------------------------------------------


def test_the_route_its_api_host_asks_for_and_needing_fewer_values_wins_the_tie(ctx) -> None:
    password = waiting("password-bridge", ["BUILD_USERNAME", "BUILD_PASSWORD"], confidence=0.9)
    signature = waiting("signature-bridge", ["BUILD_USERNAME"], scheme="signature", confidence=0.5)
    evidence.mark(ctx, [password, signature], "build.example.test", PLAN)
    assert evidence.scheme_of(password, ctx.settings) == "basic"
    assert signature.evidence < password.evidence and "API host asks for signature" in signature.evidence_why
    stage = StageOutcome(stage="bridge", candidates=[password, signature])
    assert stage.note_provisional(1) == [signature]


def test_a_scheme_the_host_refused_before_goes_last(ctx, monkeypatch) -> None:
    monkeypatch.setattr(login_history, "rank", lambda c, host, scheme: 2 if scheme == "basic" else 1)
    refused = waiting("refused", ["TOKEN"], scheme="basic", confidence=0.9)
    other = waiting("other", ["A", "B"], scheme="bearer", confidence=0.1)
    evidence.mark(ctx, [refused, other], "build.example.test", {})
    assert StageOutcome(stage="bridge", candidates=[refused, other]).note_provisional(1) == [other]


def test_evidence_takes_a_slot_across_stages(ctx) -> None:
    from initialize_claude_ai.mcp_auto_discovery.types import TargetOutcome

    outcome = TargetOutcome(target="https://build.example.test")
    password = waiting("password-bridge", ["U", "P"], confidence=0.9)
    signature = waiting("signature-bridge", ["U"], scheme="signature")
    evidence.mark(ctx, [password, signature], "build.example.test", PLAN)
    first = StageOutcome(stage="package", candidates=[password])
    first.note_provisional(1)
    outcome.absorb(first)
    second = StageOutcome(stage="bridge", candidates=[signature])
    second.note_provisional(1)
    outcome.absorb(second)
    assert outcome.provisional == [signature]
    assert any("run knows more" in n for n in signature.notes)


def test_switched_off_nothing_is_marked(ctx) -> None:
    ctx.settings.set("mcp.discovery.auth.evidence.enabled", False)
    candidate = waiting("x", ["U"])
    evidence.mark(ctx, [candidate], "h.example.test", PLAN)
    assert candidate.evidence == ()


# -- alternatives kept ---------------------------------------------------------------------------------


def test_alternatives_are_kept_without_values_and_cleared_when_working(ctx) -> None:
    ctx.dry_run = False
    entry = {"type": "stdio", "command": "python3", "args": ["/b.py"], "env": {"U": "${U}"}}
    literal = {
        "type": "stdio",
        "command": "python3",
        "env": {"API_TOKEN": fake("glpat-", "abcdefgh", "ijklmnop", "qrst") + "u"},
    }
    alternatives.record(
        ctx,
        "https://build.example.test",
        [{"name": "sig", "entry": entry, "why": "w"}, {"name": "lit", "entry": literal}],
    )
    rows = alternatives.load(ctx)["https://build.example.test"]
    assert [r["name"] for r in rows] == ["sig"], "an entry carrying a value is never kept"
    alternatives.record(ctx, "https://build.example.test", [])
    assert "https://build.example.test" not in alternatives.load(ctx)


# -- a stated status outranks wording -----------------------------------------------------------------


def _failed(text: str) -> dict:
    return {"result": {"isError": True, "content": [{"type": "text", "text": text}]}}


def test_a_stated_403_is_no_access_whatever_the_client_calls_it(settings) -> None:
    verdict, _ = login_check.judge(
        _failed(
            "Error calling tool 'search': Authentication failed for Confluence API (403). "
            "Token may be expired or invalid. Please verify credentials."
        ),
        settings,
    )
    assert verdict == login_check.NO_ACCESS
    assert (
        login_check.judge(_failed("HTTP 401 from https://x.example.test/person/me"), settings)[0]
        == login_check.REJECTED
    )
    assert login_check.judge(_failed("401 then 403, unauthorized"), settings)[0] == login_check.REJECTED, (
        "two statuses: the wordings decide"
    )
    settings.set("mcp.verification.login_check.status_outranks_wording", False)
    assert login_check.judge(_failed("Authentication failed (403)"), settings)[0] == login_check.REJECTED


# -- a chain completed to its root --------------------------------------------------------------------


def test_a_chain_short_of_its_root_is_completed_from_issuers_the_machine_has(real_tls, pki, server, tmp_path) -> None:
    root_dir = tmp_path / "anchors"
    root_dir.mkdir()
    (root_dir / "root.pem").write_text(Path(pki["complete"]).read_text())
    real_tls.set("mcp.trust.bundle", pki["intermediate_only"])  # the host's store lacks the root
    real_tls.set("mcp.trust.issuer_sources", [str(root_dir / "*.pem")])
    real_tls.set("mcp.trust.issuers_from_entries", False)
    store = _tls.store(real_tls)
    check = trust.check_named_bundles(
        {"NODE_EXTRA_CA_CERTS": pki["intermediate_only"]}, [(HOST, server)], real_tls, store
    )
    replaced = check.replaced.get("NODE_EXTRA_CA_CERTS", "")
    assert replaced and store.chain_complete(HOST, server, replaced) is True
    assert check.unfixable == []


def test_without_an_issuer_nothing_is_invented(real_tls, pki, server) -> None:
    real_tls.set("mcp.trust.bundle", pki["intermediate_only"])
    real_tls.set("mcp.trust.issuer_sources", [])
    real_tls.set("mcp.trust.issuers_from_entries", False)
    store = _tls.store(real_tls)
    assert store.complete_to_root(HOST, server) == []


def test_a_chain_expiring_soon_is_named(real_tls, pki, server) -> None:
    check = trust.check_named_bundles(
        {"NODE_EXTRA_CA_CERTS": pki["complete"]}, [(HOST, server)], real_tls, _tls.store(real_tls)
    )
    assert check.expiring and "Example Issuing CA" in check.expiring[0] and check.notes == []
    real_tls.set("mcp.trust.expiry_warning_days", 0)
    assert (
        trust.check_named_bundles(
            {"NODE_EXTRA_CA_CERTS": pki["complete"]}, [(HOST, server)], real_tls, _tls.store(real_tls)
        ).expiring
        == []
    )


# -- a written entry owns its files -------------------------------------------------------------------


def test_files_in_the_scratch_area_move_to_the_server_s_own_folder(ctx) -> None:
    from initialize_claude_ai.mcp_auto_discovery.writer import McpWriter

    ctx.dry_run = False
    work = Path(ctx.work_dir)
    own = work / "agent-workspace" / "artifacts" / "smelt-example-test"
    own.mkdir(parents=True)
    (own / "bridge.py").write_text("print('hi')\n")
    (own / "ca.pem").write_text("pem\n")
    shared = work / "bridges"
    shared.mkdir(parents=True)
    (shared / "manifest.json").write_text(json.dumps({"ca": str(own / "ca.pem")}))
    candidate = Candidate(
        kind="stdio",
        name="smelt-example-test",
        command=["python3", str(own / "bridge.py")],
        env={"CA": str(own / "ca.pem"), "MANIFEST": str(shared / "manifest.json"), "SYSTEM": "/etc/ssl/ca-bundle.pem"},
    )
    moved = McpWriter(ctx).own_files(candidate, "smelt-example-test")
    home = artifacts_home(ctx.settings) / "smelt-example-test"
    # The server's own folder *is* its folder: never home/<slug>/<slug>/ (a
    # nested path a copied .mcp.json then named, and missed).
    assert candidate.command[1] == str(home / "bridge.py")
    assert candidate.env["CA"] == str(home / "ca.pem")
    assert candidate.env["MANIFEST"] == str(home / "manifest.json"), "a shared folder: only the file moves"
    assert candidate.env["SYSTEM"] == "/etc/ssl/ca-bundle.pem"
    assert json.loads((home / "manifest.json").read_text())["ca"] == str(home / "ca.pem")
    assert moved and Path(candidate.command[1]).is_file()


def test_nothing_moves_in_a_dry_run(ctx) -> None:
    from initialize_claude_ai.mcp_auto_discovery.writer import McpWriter

    path = Path(ctx.work_dir) / "bridges" / "b.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x")
    candidate = Candidate(kind="stdio", name="b", command=["python3", str(path)])
    assert McpWriter(ctx).own_files(candidate, "b") == [] and candidate.command[1] == str(path)


# -- plain-text credentials in the project file ---------------------------------------------------------


def test_literal_credentials_move_to_settings_env(ctx, sandbox) -> None:
    from initialize_claude_ai.mcp_auto_discovery import claude_check
    from initialize_claude_ai.mcp_auto_discovery.writer import McpWriter
    from initialize_claude_ai.provision.settings_writer import SettingsWriter, settings_path

    ctx.dry_run = False
    ctx.settings.set("mcp.mcp_json_path", str(sandbox / ".mcp.json"))
    SettingsWriter(ctx, settings_path(ctx)).apply({"env": {"TAKEN_TOKEN": "someone-elses-value-123456"}})
    (sandbox / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "conf-example-test": {
                        "type": "stdio",
                        "command": "uvx",
                        "env": {
                            "CONF_TOKEN": fake("bpqgn0rT", "mNXfU5JG", "Nz8Bs2wP", "qjfbpixP", "15xmBQSw", "P"),
                            "CONF_PASSWORD": "${CONF_PASSWORD:-D3l0r3s@W3stW0r1d!}",
                            "TAKEN_TOKEN": fake("glpat-", "abcdefgh", "ijklmnop", "qrst") + "u",
                            "CONF_URL": "https://conf.example.test",
                        },
                    },
                    "oqa-example-test": {
                        "type": "http",
                        "url": "https://oqa.example.test/mcp",
                        "headers": {"Authorization": "Bearer user:3A6E2753C35AA32E:A6554519AB89C39D"},
                    },
                }
            }
        )
    )
    moved, left = McpWriter(ctx).move_literal_secrets()
    servers = json.loads((sandbox / ".mcp.json").read_text())["mcpServers"]
    env = claude_check._claude_settings_env(ctx)
    assert servers["conf-example-test"]["env"]["CONF_TOKEN"] == "${CONF_TOKEN}"
    assert servers["conf-example-test"]["env"]["CONF_PASSWORD"] == "${CONF_PASSWORD}"
    assert servers["conf-example-test"]["env"]["CONF_URL"] == "https://conf.example.test"
    assert servers["oqa-example-test"]["headers"]["Authorization"] == "${OQA_EXAMPLE_TEST_AUTHORIZATION}"
    assert env["CONF_PASSWORD"] == "D3l0r3s@W3stW0r1d!" and env["OQA_EXAMPLE_TEST_AUTHORIZATION"].startswith("Bearer ")
    assert env["TAKEN_TOKEN"] == "someone-elses-value-123456", "never over another value"
    assert servers["conf-example-test"]["env"]["TAKEN_TOKEN"] == fake("glpat-", "abcdefgh", "ijklmnop", "qrst") + "u"
    assert any("TAKEN_TOKEN" in line for line in left) and len(moved) == 3
    assert not any("D3l0r3s" in line for line in [*moved, *left]), "never a value in what is reported"


# -- the agent SDK's own words ----------------------------------------------------------------------


def test_the_sdk_failure_carries_what_its_cli_said(ctx, tmp_path) -> None:
    from initialize_claude_ai.mcp_auto_discovery.runtime import SdkRuntime

    runtime = SdkRuntime(ctx)

    async def failing(*_a, **_k):
        runtime._stderr_lines.append("Error: model claude-x is not available on your deployment")
        raise RuntimeError("Command failed with exit code 1 -- Check stderr output for details")

    runtime._run_async = failing  # type: ignore[method-assign]
    agent = type("A", (), {"name": "a"})()
    result = runtime.run("p", agent=agent, workspace=tmp_path)
    assert "not available on your deployment" in result.error


def test_packaged_settings_equal_the_code(settings) -> None:
    assert settings.bool_("mcp.discovery.auth.evidence.enabled") is True
    assert settings.dict_("mcp.discovery.auth.evidence.scheme_patterns") == evidence.SCHEME_PATTERNS
    assert settings.str_("mcp.alternatives.file") == alternatives.DEFAULT_FILE
    assert settings.int_("mcp.alternatives.keep") == alternatives.DEFAULT_KEEP
    check = settings.section("mcp.verification.login_check")
    assert check.dict_("status_verdicts") == login_check.DEFAULT_STATUS_VERDICTS
    assert check.str_("status_pattern") == login_check.DEFAULT_STATUS_PATTERN
    assert check.bool_("confirm_by_own_record") is True
    from initialize_claude_ai.core import trust_store

    assert tuple(settings.str_list("mcp.trust.issuer_sources")) == trust_store.ISSUER_SOURCES
    assert settings.bool_("mcp.registration.own_files") and settings.bool_("mcp.registration.move_literal_secrets")
    assert settings.bool_("mcp.agent.capture_stderr") is True
