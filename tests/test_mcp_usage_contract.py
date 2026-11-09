"""Working means doing the job: the usage contract, ground truth, and Claude Code's own use of the entry.

Every server that failed the operator this week had passed the program's
checks. sd-example-com's mcp-atlassian answered ``[]`` to every listing;
build-example-net "worked" on ``get_about``; a token exported inside a session never
reached the server. A server now has to do what it is for -- the operator's own
checks and each kind of server's tasks -- and Claude Code itself is asked to
use the entry once it is written.
"""

from __future__ import annotations

import json
import os
import sys

import pytest

from initialize_claude_ai.core.secrets import Secret
from initialize_claude_ai.mcp_auto_discovery import claude_check, contract, login_check
from initialize_claude_ai.mcp_auto_discovery.types import Candidate, Handshake
from initialize_claude_ai.mcp_auto_discovery.verifier import CandidateVerifier

TOKEN = "".join(chr(c) for c in (117, 115, 101, 45, 116, 111, 107, 45, 55))

SERVER = """
import json, os, sys
answers = json.loads(os.environ["ANSWERS"])
tools = [{"name": n, "inputSchema": {"type": "object", "properties": {"issueIdOrKey": {"type": "string"}},
          "required": ["issueIdOrKey"] if n == "get_request" else []}} for n in answers]
for line in sys.stdin:
    msg = json.loads(line)
    rid, method = msg.get("id"), msg.get("method")
    if rid is None:
        continue
    if method == "initialize":
        result = {"protocolVersion": msg["params"]["protocolVersion"], "capabilities": {"tools": {}},
                  "serverInfo": {"name": "fake", "version": "1"}}
    elif method == "tools/list":
        result = {"tools": tools}
    elif method == "tools/call":
        if not os.environ.get("THE_TOKEN"):
            result = {"isError": True, "content": [{"type": "text", "text": "401 Unauthorized"}]}
        else:
            text = answers[msg["params"]["name"]].replace("{key}", str(msg["params"]["arguments"].get("issueIdOrKey", "")))
            result = {"content": [{"type": "text", "text": text}]}
    else:
        result = {}
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": rid, "result": result}) + "\\n")
    sys.stdout.flush()
"""


@pytest.fixture
def live(ctx):
    ctx.dry_run = False
    ctx.runner.which = lambda name, **_: name  # type: ignore[assignment]
    ctx.settings.set("mcp.packages.trial_timeout_seconds", 20)
    ctx.secrets.resolve = lambda reference, **_: (
        Secret(TOKEN) if reference.startswith("env:") and reference.endswith("THE_TOKEN") else None
    )  # type: ignore[assignment]
    return ctx


def fake(tmp_path, answers: dict[str, str]) -> Candidate:
    script = tmp_path / "server.py"
    script.write_text(SERVER)
    candidate = Candidate(
        kind="stdio",
        name="sd-example-test",
        command=[sys.executable, str(script)],
        env={"BASE_URL": "https://sd.example.test", "ANSWERS": json.dumps(answers)},
    )
    candidate.secret_env = {"THE_TOKEN": "env:THE_TOKEN"}
    return candidate


# -- the contract ---------------------------------------------------------------------------------


def test_a_kind_s_task_applies_to_any_server_listing_its_tool(live, tmp_path) -> None:
    """mcp-atlassian for a customer account: every listing [] -- it does not do what a Jira server is for."""
    candidate = CandidateVerifier(live).verify(fake(tmp_path, {"jira_get_all_projects": "[]"}))
    shake = candidate.handshake
    assert shake.contract and shake.contract[0]["passed"] is False
    assert not candidate.verified and "usage task" in shake.error


def test_the_operator_s_own_check_must_pass(live, tmp_path) -> None:
    live.settings.set(
        "mcp.contract.targets",
        {
            "sd.example.test": {
                "checks": [
                    {
                        "name": "read one request",
                        "tools": ["^get_request$"],
                        "arguments": {"issueIdOrKey": "SD-204666"},
                        "expect": ["SD-204666"],
                    },
                ]
            }
        },
    )
    good = CandidateVerifier(live).verify(fake(tmp_path, {"get_request": '{"issueKey": "{key}", "status": "open"}'}))
    assert good.verified and good.handshake.contract[0]["passed"]
    assert good.handshake.contract[0]["arguments"] == {"issueIdOrKey": "SD-204666"}

    bad = CandidateVerifier(live).verify(fake(tmp_path, {"get_request": '{"errorMessage": "portal"}'}))
    assert not bad.verified and "read one request" in bad.handshake.error


def test_an_operator_check_the_server_cannot_answer_is_said(live, tmp_path) -> None:
    live.settings.set(
        "mcp.contract.targets",
        {"sd.example.test": {"checks": [{"name": "read one request", "tools": ["^get_request$"]}]}},
    )
    candidate = CandidateVerifier(live).verify(fake(tmp_path, {"list_things": '[{"a": 1}]'}))
    assert any("could not be asked" in note for note in candidate.notes)


def test_nothing_applies_changes_nothing(live, tmp_path) -> None:
    candidate = CandidateVerifier(live).verify(fake(tmp_path, {"list_things": '[{"a": 1}]'}))
    assert candidate.handshake.contract == [] and candidate.handshake.ok


def test_mode_off_asks_no_task(live, tmp_path) -> None:
    live.settings.set("mcp.verification.login_check.mode", "off")
    assert contract.tasks_for(live.settings, "sd.example.test") == []


def test_judging_one_answer(settings) -> None:
    call = {"expect": ["SD-1"]}
    ok = {"result": {"content": [{"type": "text", "text": '{"issueKey": "SD-1"}'}]}}
    assert contract.judge(ok, call, settings) == (True, "passed")
    assert contract.judge({"result": {"content": [{"type": "text", "text": "[]"}]}}, call, settings)[0] is False
    page = {"result": {"content": [{"type": "text", "text": "<!DOCTYPE html><html>SD-1</html>"}]}}
    assert contract.judge(page, call, settings)[0] is False
    passed, detail = contract.judge(ok, {"expect": ["SD-2"]}, settings)
    assert not passed and "SD-2" not in detail, "expected values are never quoted back"


def test_ground_truth_compares_with_the_target_read_directly(settings) -> None:
    answer = {"result": {"content": [{"type": "text", "text": '{"version": "5.12", "name": "x"}'}]}}
    call = {"expect": [], "truth": {"path": "/info", "fields": ["version"]}}
    assert contract.judge(answer, call, settings, lambda spec: (["5.12"], ""))[0] is True
    assert contract.judge(answer, call, settings, lambda spec: (["9.99"], ""))[0] is False
    assert contract.judge(answer, call, settings, lambda spec: ([], "HTTP 401"))[0] is True
    assert contract.truth_values('{"a": {"b": [7]}}', ["a.b.0"]) == ["7"]


def test_the_score_ranks_by_tasks_then_login() -> None:
    def scored(passed: int, login: str = "") -> Candidate:
        c = Candidate(kind="stdio", name="x")
        c.handshake = Handshake(ok=True, login=login, contract=[{"passed": True}] * passed + [{"passed": False}])
        return c

    assert contract.score(scored(2)) > contract.score(scored(1, login_check.ACCEPTED)) > contract.score(scored(1))


# -- replacing by score -------------------------------------------------------------------------------


def test_an_existing_entry_that_passed_more_keeps_its_place(ctx, sandbox) -> None:
    from initialize_claude_ai.mcp_auto_discovery.existing import ExistingRegistrations
    from initialize_claude_ai.mcp_auto_discovery.types import StageOutcome, TargetOutcome
    from initialize_claude_ai.mcp_auto_discovery.writer import McpWriter, Registration

    ctx.settings.set("mcp.discovery.reuse.sources", ["project_file"])
    ctx.settings.set("mcp.mcp_json_path", str(sandbox / ".mcp.json"))
    (sandbox / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "sd-example-test": {
                        "command": "python3",
                        "args": ["bridge.py"],
                        "env": {"BASE_URL": "https://sd.example.test"},
                    }
                }
            }
        )
    )
    existing = ExistingRegistrations.for_run(ctx)
    old = existing.candidate(existing.registrations()[0], "package")
    old.handshake = Handshake(ok=True, tools=["get_request"], round_trip=True, contract=[{"passed": True}])
    new = Candidate(kind="stdio", name="sd-example-test", command=["uvx", "other"])
    new.handshake = Handshake(ok=True, tools=["t"], round_trip=True, login=login_check.ACCEPTED)
    outcome = TargetOutcome(target="https://sd.example.test")
    outcome.stages.append(StageOutcome(stage="package", candidates=[old, new]))
    registration = Registration(name="sd-example-test", candidate=new, entry={"command": "uvx", "args": ["other"]})
    assert "passed more of what it is used for" in McpWriter(ctx)._holds_its_place(registration, outcome, {})


# -- Claude Code's own use of the entry ----------------------------------------------------------------


def test_a_missing_variable_is_found_before_any_request(monkeypatch) -> None:
    monkeypatch.delenv("SD_EXAMPLE_TEST_TOKEN", raising=False)
    entry = {"command": "python3", "env": {"T": "${SD_EXAMPLE_TEST_TOKEN}", "O": "${OPTIONAL:-x}"}}
    assert claude_check.missing_variables(entry, os.environ) == ["SD_EXAMPLE_TEST_TOKEN"]


def events(status: str = "connected", answer: str | None = '{"issueKey": "SD-1"}', error: bool = False) -> str:
    lines = [
        {"type": "system", "subtype": "init", "mcp_servers": [{"name": "sd", "status": status}]},
        {
            "type": "assistant",
            "message": {"content": [{"type": "tool_use", "id": "t1", "name": "mcp__sd__get_request"}]},
        },
    ]
    if answer is not None:
        lines.append(
            {
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "t1",
                            "is_error": error,
                            "content": [{"type": "text", "text": answer}],
                        }
                    ]
                },
            }
        )
    lines.append({"type": "result", "subtype": "success", "is_error": False, "result": "DONE"})
    return "\n".join(json.dumps(line) for line in lines)


def test_the_tool_s_own_answer_is_judged_not_the_model_s_summary(settings) -> None:
    assert claude_check.judge_stream(events(), "sd", "mcp__sd__get_request", 0, settings).outcome == claude_check.PASSED
    assert (
        claude_check.judge_stream(events(answer="[]"), "sd", "mcp__sd__get_request", 0, settings).outcome
        == claude_check.FAILED
    )
    assert (
        claude_check.judge_stream(events(answer="401", error=True), "sd", "mcp__sd__get_request", 0, settings).outcome
        == claude_check.FAILED
    )
    assert (
        claude_check.judge_stream(events(status="failed"), "sd", "mcp__sd__get_request", 0, settings).outcome
        == claude_check.FAILED
    )
    assert (
        claude_check.judge_stream(events(answer=None), "sd", "mcp__sd__get_request", 0, settings).outcome
        == claude_check.COULD_NOT_RUN
    )


def test_the_check_runs_claude_with_exactly_the_entry(ctx, tmp_path, monkeypatch) -> None:
    """A fake claude: it records what it was given and answers like the real stream."""
    record = tmp_path / "argv.json"
    fake_claude = tmp_path / "claude"
    fake_claude.write_text(
        f"#!{sys.executable}\nimport json, sys\n"
        "argv = sys.argv[1:]\n"
        f"json.dump({{'argv': argv, 'config': json.load(open(argv[argv.index('--mcp-config') + 1]))}}, open({str(record)!r}, 'w'))\n"
        f"print({events()!r})\n"
    )
    fake_claude.chmod(0o755)
    ctx.dry_run = False
    ctx.settings.set("paths.claude", str(fake_claude))
    candidate = Candidate(kind="stdio", name="sd")
    candidate.handshake = Handshake(
        ok=True, contract=[{"passed": True, "tool": "get_request", "arguments": {"issueIdOrKey": "SD-1"}}]
    )
    entry = {"command": "python3", "args": ["bridge.py"], "env": {"BASE_URL": "https://sd.example.test"}}
    outcome = claude_check.check(ctx, "sd", entry, candidate)
    assert outcome.outcome == claude_check.PASSED, outcome.detail
    seen = json.loads(record.read_text())
    assert seen["config"] == {"mcpServers": {"sd": entry}} and "--strict-mcp-config" in seen["argv"]
    assert "mcp__sd__get_request" in seen["argv"]


def test_a_check_that_cannot_run_is_inconclusive(ctx, monkeypatch) -> None:
    candidate = Candidate(kind="stdio", name="sd")
    candidate.handshake = Handshake(ok=True)
    outcome = claude_check.check(ctx, "sd", {"command": "python3"}, candidate)
    assert outcome.outcome == claude_check.COULD_NOT_RUN


def test_packaged_settings_equal_the_code(settings) -> None:
    config = settings.section("mcp.registration.claude_check")
    assert tuple(config.list_("command")) == claude_check.DEFAULT_COMMAND
    assert config.str_("prompt") == claude_check.DEFAULT_PROMPT
    assert settings.section("mcp.contract").int_("max_tasks") == contract.DEFAULT_MAX_TASKS
    assert settings.section("mcp.contract").bool_("enabled") is contract.DEFAULT_ENABLED
