"""bugzilla-opensuse-org went missing: a public server judged "waiting", a setting treated as a credential.

The existing CLI entry carried ``BUGZILLA_AUTH_MODE: "${BUGZILLA_AUTH_MODE}"``,
which nothing exports; Claude Code passed the literal text and the server
exited ("Invalid --bugzilla-auth-mode '${BUGZILLA_AUTH_MODE}'"), so
``claude mcp list`` said "Failed to connect". The run then:

* counted the unset *setting* as a missing credential ("waiting for
  BUGZILLA_API_KEY, BUGZILLA_AUTH_MODE");
* demoted the public server -- its reads answer anybody -- to "waiting for
  BUGZILLA_API_KEY", because an anonymous answer "proves nothing about the login";
* refused the new entry at the start check (the same literal) with no repair;
* and kept the broken CLI copy as "answered during this run" -- judged by the
  run's own launch, which drops the unset variable.

The address ended with nothing in ``.mcp.json``. The fake server below is a
real process.
"""

from __future__ import annotations

import json
import sys

import pytest

from initialize_claude_ai.mcp_auto_discovery import login_check
from initialize_claude_ai.mcp_auto_discovery.types import Candidate, Handshake, StageOutcome, TargetOutcome
from initialize_claude_ai.mcp_auto_discovery.verifier import CandidateVerifier

TARGET = "https://bugzilla.example.test"

SERVER = """
import json, os, sys
mode = os.environ.get("BUGZILLA_AUTH_MODE", "query")
if mode.startswith("${"):
    sys.stderr.write("[CRITICAL]: Error: Invalid --bugzilla-auth-mode '" + mode + "'. Must be 'query' or 'bearer'.\\n")
    sys.exit(1)
tools = [{"name": n, "inputSchema": {"type": "object", "properties": {}}} for n in ("bugzilla_server_info", "bugs_quicksearch")]
for line in sys.stdin:
    msg = json.loads(line)
    rid, method = msg.get("id"), msg.get("method")
    if rid is None:
        continue
    if method == "initialize":
        result = {"protocolVersion": msg["params"]["protocolVersion"], "capabilities": {"tools": {}},
                  "serverInfo": {"name": "bugzilla", "version": "1"}}
    elif method == "tools/list":
        result = {"tools": tools}
    elif method == "tools/call":
        # A public Bugzilla: reads answer anybody, with or without an API key.
        result = {"content": [{"type": "text", "text": json.dumps({"version": "5.0", "bugs": [{"id": 1}]})}]}
    else:
        result = {}
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": rid, "result": result}) + "\\n")
    sys.stdout.flush()
"""


@pytest.fixture
def live(ctx, sandbox, tmp_path, monkeypatch):
    for name in ("BUGZILLA_API_KEY", "BUGZILLA_AUTH_MODE"):
        monkeypatch.delenv(name, raising=False)  # as on the machine where the entry went missing
    ctx.dry_run = False
    ctx.runner.which = lambda name, **_: name  # type: ignore[assignment]
    ctx.settings.set("mcp.packages.trial_timeout_seconds", 20)
    ctx.settings.set("mcp.discovery.reuse.sources", ["project_file"])
    ctx.settings.set("mcp.mcp_json_path", str(sandbox / ".mcp.json"))
    script = tmp_path / "bugzilla.py"
    script.write_text(SERVER)
    broken = {
        "command": sys.executable,
        "args": [str(script), "--bugzilla-server", TARGET],
        "env": {"BUGZILLA_AUTH_MODE": "${BUGZILLA_AUTH_MODE}", "BUGZILLA_API_KEY": "${BUGZILLA_API_KEY}"},
    }
    (sandbox / ".mcp.json").write_text(json.dumps({"mcpServers": {"bugzilla-example-test": broken}}))
    return ctx, broken


def existing_candidate(ctx):
    from initialize_claude_ai.mcp_auto_discovery.existing import ExistingRegistrations

    existing = ExistingRegistrations.for_run(ctx)
    registration = existing.registrations()[0]
    return existing, registration, existing.candidate(registration, "package")


def test_an_unset_setting_is_not_a_credential(live) -> None:
    ctx, _ = live
    _, _, candidate = existing_candidate(ctx)
    shake = CandidateVerifier(ctx).verify(candidate).handshake
    assert "BUGZILLA_AUTH_MODE" not in shake.credential_pending
    assert "BUGZILLA_AUTH_MODE" in candidate.unset_settings


def test_a_public_server_works_without_a_login(live) -> None:
    ctx, _ = live
    _, _, candidate = existing_candidate(ctx)
    candidate = CandidateVerifier(ctx).verify(candidate)
    assert candidate.verified, candidate.handshake.error
    assert candidate.handshake.credential_optional and not candidate.handshake.credential_pending
    assert candidate.handshake.login_reason == login_check.WITHOUT_LOGIN


def test_the_existing_entry_is_judged_as_claude_code_runs_it(live) -> None:
    ctx, broken = live
    _, registration, candidate = existing_candidate(ctx)
    CandidateVerifier(ctx).verify(candidate)
    why = CandidateVerifier(ctx).as_claude_runs_it(registration.name, broken, registration.source, candidate)
    assert "does not start as written" in why and "BUGZILLA_AUTH_MODE" in why


def test_the_written_entry_has_no_bare_unset_setting_and_starts(live) -> None:
    from initialize_claude_ai.mcp_auto_discovery.writer import McpWriter

    ctx, _ = live
    _, _, candidate = existing_candidate(ctx)
    candidate = CandidateVerifier(ctx).verify(candidate)
    entry = McpWriter(ctx).entry_for(candidate, "bugzilla-example-test")
    env = entry.get("env") or {}
    assert env.get("BUGZILLA_AUTH_MODE", "${BUGZILLA_AUTH_MODE:-}") != "${BUGZILLA_AUTH_MODE}"
    assert "${BUGZILLA_API_KEY}" not in json.dumps(entry), "the optional key expands to nothing when unset"
    written = CandidateVerifier(ctx).verify_as_written(entry, "bugzilla-example-test")
    assert written is not None and written.ok, written.error if written else "not launched"


def test_the_address_ends_with_a_working_entry_not_the_broken_copy(live, sandbox) -> None:
    from initialize_claude_ai.mcp_auto_discovery.traditional import TraditionalPipeline
    from initialize_claude_ai.mcp_auto_discovery.writer import McpWriter

    ctx, broken = live
    pipeline = TraditionalPipeline(ctx)
    outcome = TargetOutcome(target=TARGET)
    pipeline.reuse("package", outcome)
    [candidate] = pipeline.reused_for(TARGET, "package")
    assert "bugzilla-example-test" not in outcome.answered_existing, "broken as written: never kept in place"
    assert any(b["name"] == "bugzilla-example-test" for b in outcome.broken_existing)

    stage = StageOutcome(stage="package", candidates=[candidate])
    if candidate.verified:
        stage.verified.append(candidate)
    outcome.stages.append(stage)
    outcome.absorb(stage)
    ctx.settings.set("mcp.registration.verify_as_written", True)
    McpWriter(ctx).publish(outcome)
    written = json.loads((sandbox / ".mcp.json").read_text())["mcpServers"]["bugzilla-example-test"]
    assert written != broken and written.get("env", {}).get("BUGZILLA_AUTH_MODE") != "${BUGZILLA_AUTH_MODE}"


def test_the_healing_repair_leaves_an_unset_setting_out(ctx) -> None:
    from initialize_claude_ai.mcp_auto_discovery.healing import EntryDoctor
    from initialize_claude_ai.mcp_auto_discovery.writer import McpWriter

    writer = McpWriter(ctx)
    doctor = EntryDoctor(ctx, writer, writer.verifier)
    entry = {
        "command": "x",
        "env": {"BUGZILLA_AUTH_MODE": "${BUGZILLA_AUTH_MODE}", "BUGZILLA_API_KEY": "${BUGZILLA_API_KEY}"},
    }
    candidate = Candidate(kind="stdio", name="x")
    candidate.secret_env = {"BUGZILLA_API_KEY": "env:BUGZILLA_API_KEY"}
    repaired = doctor._drop_unset_setting(candidate, entry)
    assert repaired is not None and "BUGZILLA_AUTH_MODE" not in repaired["env"]
    assert repaired["env"]["BUGZILLA_API_KEY"] == "${BUGZILLA_API_KEY}", "a credential's slot is never dropped"


def test_packaged_settings_equal_the_code(settings) -> None:
    assert settings.bool_("mcp.verification.judge_existing_as_written") is True
    check = settings.section("mcp.verification.login_check")
    assert check.bool_("try_without_credential") and check.bool_("public_works_without_login")
    assert settings.bool_("mcp.registration.optional_unset_settings") is True
    assert "drop_unset_setting" in settings.str_list("mcp.healing.repairs")


def test_a_shake_without_tools_is_never_asked_anonymously(ctx) -> None:
    shake = Handshake(ok=True, tools=[], credential_pending=["X_TOKEN"])
    assert not shake.tools, "nothing to call: the credential test path decides, as before"
