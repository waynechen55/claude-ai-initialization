"""build-example-net worked in the run's own shell and not in a new terminal; its password login was tried first every run.

* The check through Claude Code inherited the run's shell, where
  BUILD_EXAMPLE_NET_USERNAME was exported -- "passed" -- while a new terminal had
  nothing and failed. Checks that stand in for Claude Code now see only what a
  new terminal's Claude Code has.
* The user name the old entry supplied was dropped with it; it is now kept in
  Claude Code's settings.json env.
* The password login was chosen first in every run although build.example.net
  refused it every time; which login each host accepted or refused is now
  remembered, and orders the next choice.
"""

from __future__ import annotations

import json

from initialize_claude_ai.mcp_auto_discovery import (
    claude_check,
    client_bridge,
    login_check,
    login_history,
    login_methods,
)
from initialize_claude_ai.mcp_auto_discovery.types import Candidate, Handshake
from initialize_claude_ai.mcp_auto_discovery.writer import McpWriter, Registration
from initialize_claude_ai.provision.settings_writer import SettingsWriter, settings_path


def test_a_variable_only_the_run_shell_sets_is_not_in_a_fresh_terminal(ctx, monkeypatch) -> None:
    monkeypatch.setenv("BUILD_EXAMPLE_TEST_USERNAME", "someone")
    fresh = claude_check.fresh_environ(ctx)
    assert "BUILD_EXAMPLE_TEST_USERNAME" not in fresh and "PATH" in fresh


def test_claude_codes_settings_env_is_in_a_fresh_terminal(ctx) -> None:
    ctx.dry_run = False
    SettingsWriter(ctx, settings_path(ctx)).apply({"env": {"BUILD_EXAMPLE_TEST_USERNAME": "someone"}})
    assert claude_check.fresh_environ(ctx).get("BUILD_EXAMPLE_TEST_USERNAME") == "someone"


def test_an_entry_needing_a_shell_only_variable_is_reported_missing(ctx, monkeypatch) -> None:
    monkeypatch.setenv("BUILD_EXAMPLE_TEST_USERNAME", "someone")
    entry = {"command": "python3", "env": {"BUILD_EXAMPLE_TEST_USERNAME": "${BUILD_EXAMPLE_TEST_USERNAME}"}}
    outcome = claude_check.check(ctx, "build-example-test", entry, Candidate(kind="stdio", name="b"))
    assert outcome.outcome == claude_check.MISSING_VARIABLE
    assert "new terminal" in outcome.detail and "only this run's shell" in outcome.detail


def test_a_value_the_old_entry_supplied_is_kept_for_the_new_one(ctx, sandbox, monkeypatch) -> None:
    from initialize_claude_ai.mcp_auto_discovery.existing import ExistingRegistrations

    ctx.dry_run = False
    monkeypatch.delenv("BUILD_EXAMPLE_TEST_USERNAME", raising=False)
    ctx.settings.set("mcp.mcp_json_path", str(sandbox / ".mcp.json"))
    ctx.settings.set("mcp.discovery.reuse.sources", ["project_file"])
    (sandbox / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "build-example-test": {
                        "command": "python3",
                        "args": ["https://build.example.test"],
                        "env": {"BUILD_EXAMPLE_TEST_USERNAME": "${BUILD_EXAMPLE_TEST_USERNAME:-someone}"},
                    }
                }
            }
        )
    )
    monkeypatch.setattr(ExistingRegistrations, "_instance", None, raising=False)
    candidate = Candidate(kind="stdio", name="bridge", command=["python3", "bridge.py"])
    candidate.handshake = Handshake(ok=True, tools=["whoami"])
    registration = Registration(
        name="build-example-test",
        candidate=candidate,
        written=True,
        entry={"command": "python3", "env": {"BUILD_EXAMPLE_TEST_USERNAME": "${BUILD_EXAMPLE_TEST_USERNAME}"}},
        needs_env=["BUILD_EXAMPLE_TEST_USERNAME"],
    )
    McpWriter(ctx)._keep_supplied_values(registration, "https://build.example.test")
    assert SettingsWriter(ctx, settings_path(ctx)).read()["env"]["BUILD_EXAMPLE_TEST_USERNAME"] == "someone"
    assert registration.needs_env == []


def _tried(scheme: str, verdict: str) -> Candidate:
    candidate = Candidate(kind="stdio", name=f"bridge-{scheme}", command=["x"], login_scheme=scheme)
    candidate.handshake = Handshake(ok=True, login=verdict, credential_sent=True, login_credential="account-print")
    return candidate


def test_a_login_refused_before_is_tried_last(ctx) -> None:
    ctx.dry_run = False
    login_history.record(
        ctx, "build.example.test", [_tried("basic", login_check.REJECTED), _tried("signature", login_check.ACCEPTED)]
    )
    assert login_history.rank(ctx, "build.example.test", "basic") == 2
    assert login_history.rank(ctx, "build.example.test", "signature") == 0
    plan = {
        "offered": [
            {"scheme": "basic", "host": "build.example.test"},
            {"scheme": "signature", "host": "api.example.test"},
        ],
        "history": login_history.summary(ctx, "build.example.test"),
    }
    client = {"logins": {"basic": ["USERNAME", "PASSWORD"], "signature": ["USERNAME", "SSH_KEY_FILE"]}}
    assert [r[0] for r in client_bridge.routes(client, plan, "build.example.test", ctx.settings)][0] == "signature"


def test_nothing_sent_is_not_recorded_as_a_refusal(ctx) -> None:
    ctx.dry_run = False
    candidate = Candidate(kind="stdio", name="b", command=["x"], login_scheme="basic")
    candidate.handshake = Handshake(ok=True, login=login_check.REJECTED, credential_sent=False)
    login_history.record(ctx, "build.example.test", [candidate])
    assert login_history.rank(ctx, "build.example.test", "basic") == 1


def test_the_login_plan_puts_a_refused_method_last(settings) -> None:
    offered = [
        {"scheme": "basic", "host": "build.example.test", "method": "basic", "kind": "header"},
        {"scheme": "bearer", "host": "build.example.test", "method": "token", "kind": "header"},
    ]
    plan = login_methods.plan("build.example.test", "", offered, settings, history={"bearer": "refused"})
    assert plan["chosen"].get("method") == "basic"


def test_a_password_is_never_borrowed_into_a_username_slot(ctx, sandbox, monkeypatch) -> None:
    from initialize_claude_ai.mcp_auto_discovery.existing import ExistingRegistrations
    from initialize_claude_ai.mcp_auto_discovery.verifier import CandidateVerifier

    monkeypatch.delenv("BUILD_EXAMPLE_TEST_PASSWORD", raising=False)
    ctx.settings.set("mcp.mcp_json_path", str(sandbox / ".mcp.json"))
    ctx.settings.set("mcp.discovery.reuse.sources", ["project_file"])
    (sandbox / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "build-example-test": {
                        "command": "python3",
                        "args": ["https://build.example.test"],
                        "env": {
                            "BUILD_EXAMPLE_TEST_PASSWORD": "${BUILD_EXAMPLE_TEST_PASSWORD:-a-long-password-value-1234}"
                        },
                    }
                }
            }
        )
    )
    monkeypatch.setattr(ExistingRegistrations, "_instance", None, raising=False)
    candidate = Candidate(
        kind="stdio",
        name="bridge",
        command=["python3", "bridge.py"],
        env={"MCP_CLIENT_SERVICE": "https://build.example.test"},
    )
    CandidateVerifier(ctx)._borrow_in_use(candidate, {"pending": ["BUILD_EXAMPLE_TEST_USERNAME"]})
    assert "BUILD_EXAMPLE_TEST_USERNAME" not in candidate.in_use
    # ...and the same password does fill a password slot.
    other = Candidate(
        kind="stdio",
        name="bridge",
        command=["python3", "bridge.py"],
        env={"MCP_CLIENT_SERVICE": "https://build.example.test"},
    )
    CandidateVerifier(ctx)._borrow_in_use(other, {"pending": ["BUILD_EXAMPLE_TEST_PASSWORD"]})
    assert "BUILD_EXAMPLE_TEST_PASSWORD" in other.in_use


def test_an_optional_reference_is_kept_too(ctx, sandbox, monkeypatch) -> None:
    from initialize_claude_ai.mcp_auto_discovery.existing import ExistingRegistrations

    ctx.dry_run = False
    monkeypatch.delenv("BUILD_EXAMPLE_TEST_USERNAME", raising=False)
    ctx.settings.set("mcp.mcp_json_path", str(sandbox / ".mcp.json"))
    ctx.settings.set("mcp.discovery.reuse.sources", ["project_file"])
    (sandbox / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "build-example-test": {
                        "command": "python3",
                        "args": ["https://build.example.test"],
                        "env": {"BUILD_EXAMPLE_TEST_USERNAME": "${BUILD_EXAMPLE_TEST_USERNAME:-someone}"},
                    }
                }
            }
        )
    )
    monkeypatch.setattr(ExistingRegistrations, "_instance", None, raising=False)
    registration = Registration(
        name="build-example-test",
        candidate=Candidate(kind="stdio", name="b"),
        written=True,
        entry={"command": "python3", "env": {"BUILD_EXAMPLE_TEST_USERNAME": "${BUILD_EXAMPLE_TEST_USERNAME:-}"}},
    )
    McpWriter(ctx)._keep_supplied_values(registration, "https://build.example.test")
    assert SettingsWriter(ctx, settings_path(ctx)).read()["env"]["BUILD_EXAMPLE_TEST_USERNAME"] == "someone"


# -- a credential that may not have arrived is never blamed ---------------------------------------


def test_a_refusal_identical_to_anonymous_ranks_as_missing_and_is_not_recorded(ctx) -> None:
    from initialize_claude_ai.mcp_auto_discovery.types import proof_rank

    ctx.dry_run = False
    candidate = Candidate(kind="stdio", name="b", command=["x"], login_scheme="bearer")
    candidate.handshake = Handshake(
        ok=True,
        login=login_check.REJECTED,
        credential_sent=True,
        login_credential="print",
        login_reason=login_check.SAME_AS_ANONYMOUS,
    )
    assert proof_rank(candidate) == 1
    login_history.record(ctx, "sd.example.test", [candidate])
    assert login_history.rank(ctx, "sd.example.test", "bearer") == 1


# -- the slot check reads the script first, and --help never hangs -------------------------------


def test_help_is_read_with_input_closed(ctx, tmp_path) -> None:
    import sys
    import time

    from initialize_claude_ai.mcp_auto_discovery.verifier import CandidateVerifier

    script = tmp_path / "waits.py"
    script.write_text("import sys\nfor line in sys.stdin:\n    pass\n")
    started = time.monotonic()
    CandidateVerifier(ctx)._help_text(
        {"command": [sys.executable, str(script)], "env": {}, "cwd": None},
        ctx.settings.section("mcp.verification.delivery"),
    )
    assert time.monotonic() - started < 10, "a server that ignores --help sees EOF and exits"


def test_a_slot_the_script_never_reads_is_renamed_to_the_one_it_does(ctx, tmp_path) -> None:
    import sys

    from initialize_claude_ai.mcp_auto_discovery.verifier import CandidateVerifier

    script = tmp_path / "bridge.py"
    script.write_text('import os\nTOKEN = os.environ.get("JIRA_PERSONAL_TOKEN", "")\n')
    candidate = Candidate(kind="stdio", name="sd-bridge", command=[sys.executable, str(script)])
    candidate.secret_env = {"SD_EXAMPLE_TEST_TOKEN": "env:SD_EXAMPLE_TEST_TOKEN"}
    CandidateVerifier(ctx)._align_slots_to_source(candidate)
    assert list(candidate.secret_env) == ["JIRA_PERSONAL_TOKEN"]
    assert candidate.exported_names.get("JIRA_PERSONAL_TOKEN") == "SD_EXAMPLE_TEST_TOKEN"


# -- connected holds nothing by itself ------------------------------------------------------------


def test_a_connected_entry_that_proved_nothing_yields_to_the_bridge_for_the_detected_api(ctx, monkeypatch) -> None:
    from initialize_claude_ai.mcp_auto_discovery.types import TargetOutcome

    outcome = TargetOutcome(target="https://sd.example.test")
    outcome.connected_existing = {"sd-example-test": "claude_cli"}
    old = Candidate(
        kind="stdio", name="sd-example-test", command=["uvx", "mcp-atlassian"], existing_name="sd-example-test"
    )
    old.handshake = Handshake(ok=True, tools=["jira_search"], credential_pending=["JIRA_PERSONAL_TOKEN"])
    bridge = Candidate(kind="stdio", name="sd-bridge", command=["python3", "b.py"], api_kind="jira_service_management")
    bridge.handshake = Handshake(ok=True, tools=["get_my_requests"], credential_pending=["SD_EXAMPLE_TEST_TOKEN"])
    writer = McpWriter(ctx)
    monkeypatch.setattr(writer, "_existing_candidate", lambda outcome, name: old)
    registration = Registration(name="sd-example-test", candidate=bridge, entry={"command": "python3"})
    assert writer._holds_its_place(registration, outcome, {}) == ""


# -- the step only the operator can take, in the server's own words -------------------------------


def test_a_key_nobody_registered_is_named_with_the_servers_own_challenge(ctx) -> None:
    from initialize_claude_ai.mcp_auto_discovery.types import StageOutcome, TargetOutcome

    outcome = TargetOutcome(target="https://build.example.test")
    outcome.auth_plan = {
        "offered": [
            {
                "host": "api.example.test",
                "scheme": "Signature",
                "evidence": 'Signature realm="Use your developer account"',
            },
            {"host": "build.example.test", "scheme": "Basic", "evidence": 'basic realm="API login"'},
        ]
    }
    bridge = Candidate(kind="stdio", name="build-bridge-signature", command=["x"], login_scheme="signature")
    bridge.handshake = Handshake(
        ok=True,
        login=login_check.REJECTED,
        login_detail=(
            "401 Unauthorized: every SSH key was tried -- /root/.ssh/id_ed25519: the service refused its signature "
            "(the key is not registered with your account)"
        ),
    )
    outcome.stages.append(StageOutcome(stage="bridge", candidates=[bridge]))
    [step] = McpWriter(ctx).user_steps(outcome)
    assert "api.example.test only accepts the Signature login" in step
    assert "Use your developer account" in step and "/root/.ssh/id_ed25519" in step


# -- what keeps its place is written; an address never ends with no entry -------------------------


def _waiting_existing(name: str) -> Candidate:
    candidate = Candidate(
        kind="stdio", name=name, command=["github-mcp-server", "stdio", "--read-only"], existing_name=name
    )
    candidate.secret_env = {"GITHUB_PERSONAL_ACCESS_TOKEN": "env:GITHUB_PERSONAL_ACCESS_TOKEN"}
    candidate.handshake = Handshake(ok=True, tools=["get_me"], credential_pending=["GITHUB_PERSONAL_ACCESS_TOKEN"])
    return candidate


def test_an_entry_that_held_its_place_is_recorded_in_the_file(ctx, sandbox, monkeypatch) -> None:
    from initialize_claude_ai.mcp_auto_discovery.types import TargetOutcome

    ctx.dry_run = False
    ctx.settings.set("mcp.mcp_json_path", str(sandbox / ".mcp.json"))
    outcome = TargetOutcome(target="https://github.example.test")
    outcome.answered_existing = {"github-example-test": "claude_cli"}
    writer = McpWriter(ctx)
    existing = _waiting_existing("github-example-test")
    monkeypatch.setattr(writer, "_existing_candidate", lambda outcome, name: existing)
    writer._held_names.setdefault(outcome.target, set()).add("github-example-test")
    assert writer._record_kept(outcome, "github-example-test")
    entry = json.loads((sandbox / ".mcp.json").read_text())["mcpServers"]["github-example-test"]
    assert "${" in json.dumps(entry) and "command" in entry


def test_the_file_check_repairs_an_address_that_would_end_empty(ctx, sandbox, monkeypatch) -> None:
    from initialize_claude_ai.mcp_auto_discovery.types import TargetOutcome

    ctx.dry_run = False
    ctx.settings.set("mcp.mcp_json_path", str(sandbox / ".mcp.json"))
    outcome = TargetOutcome(target="https://github.example.test")
    outcome.answered_existing = {"github-example-test": "claude_cli"}
    writer = McpWriter(ctx)
    existing = _waiting_existing("github-example-test")
    monkeypatch.setattr(writer, "_existing_candidate", lambda outcome, name: existing)
    problems = writer.missing_from_file(outcome)
    assert "github-example-test" in json.loads((sandbox / ".mcp.json").read_text())["mcpServers"]
    assert any("recorded there now" in p for p in problems)


def test_a_call_with_no_credential_does_not_wait_long(ctx, tmp_path) -> None:
    import sys
    import time

    from initialize_claude_ai.mcp_auto_discovery.probes import McpProber

    script = tmp_path / "silent.py"
    script.write_text(
        "import json, sys\n"
        "for line in sys.stdin:\n"
        "    msg = json.loads(line)\n"
        "    if msg.get('method') == 'initialize':\n"
        "        print(json.dumps({'jsonrpc': '2.0', 'id': msg['id'], 'result': {'protocolVersion': '2025-06-18',"
        " 'capabilities': {'tools': {}}, 'serverInfo': {'name': 's', 'version': '1'}}}), flush=True)\n"
        "    elif msg.get('method') == 'tools/list':\n"
        "        print(json.dumps({'jsonrpc': '2.0', 'id': msg['id'], 'result': {'tools': [{'name': 'get_me',"
        " 'inputSchema': {'type': 'object'}, 'annotations': {'readOnlyHint': True}}]}}), flush=True)\n"
        "    # tools/call is never answered\n"
    )
    ctx.settings.set("mcp.verification.no_credential_call_timeout_seconds", 2)
    started = time.monotonic()
    McpProber(ctx).probe_stdio([sys.executable, str(script)], timeout=60, login=True)
    assert time.monotonic() - started < 8, "one unanswered call, then the session gives up"


# -- discovery never waits for a person ---------------------------------------------------------


def test_nothing_phase_4_starts_can_reach_the_desktop(ctx, monkeypatch, tmp_path) -> None:
    import os
    import shutil

    from initialize_claude_ai.mcp_auto_discovery import noninteractive

    ctx.dry_run = False
    monkeypatch.setattr(type(ctx), "work_dir", property(lambda self: tmp_path / "work"))
    monkeypatch.setenv("DISPLAY", ":1")
    with noninteractive.session(ctx):
        assert "DISPLAY" not in os.environ
        opener = shutil.which("xdg-open")
        assert opener and str(tmp_path) in opener, "xdg-open resolves to the recorder"
    assert os.environ.get("DISPLAY") == ":1", "restored afterwards"


def test_a_launch_that_opens_a_url_is_recorded_and_needs_a_browser_login(ctx, monkeypatch, tmp_path) -> None:
    import sys

    from initialize_claude_ai.mcp_auto_discovery import noninteractive
    from initialize_claude_ai.mcp_auto_discovery.probes import McpProber
    from initialize_claude_ai.mcp_auto_discovery.verifier import CandidateVerifier

    ctx.dry_run = False
    monkeypatch.setattr(type(ctx), "work_dir", property(lambda self: tmp_path / "work"))
    script = tmp_path / "opens.py"
    script.write_text(
        "import json, subprocess, sys\n"
        "subprocess.run(['xdg-open', 'https://login.example.test/oauth/authorize?client_id=x'])\n"
        "for line in sys.stdin:\n"
        "    msg = json.loads(line)\n"
        "    if msg.get('method') == 'initialize':\n"
        "        print(json.dumps({'jsonrpc': '2.0', 'id': msg['id'], 'result': {'protocolVersion': '2025-06-18',"
        " 'capabilities': {'tools': {}}, 'serverInfo': {'name': 's', 'version': '1'}}}), flush=True)\n"
        "    elif msg.get('method') == 'tools/list':\n"
        "        print(json.dumps({'jsonrpc': '2.0', 'id': msg['id'], 'result': {'tools': []}}), flush=True)\n"
    )
    with noninteractive.session(ctx):
        shake = McpProber(ctx).probe_stdio([sys.executable, str(script)], timeout=20)
    assert shake.browser_login and "login.example.test" in shake.browser_login[0]
    candidate = Candidate(kind="stdio", name="opens", command=[sys.executable, str(script)])
    candidate.handshake = shake
    CandidateVerifier(ctx)._apply_browser_login(candidate)
    assert not candidate.verified and candidate.handshake.login_reason == login_check.BROWSER_LOGIN
