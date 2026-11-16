"""bugzilla.example.com got two entries for one server, in an ordinary run.

The existing ``bugzilla-example-com`` (``uvx mcp-bugzilla --bugzilla-server
https://bugzilla.example.com``) answered ``429 Too Many Requests`` -- a rate limit
the run's own checks had provoked -- and was judged failed. The repair loop
"repaired" it (the rate limit had ended meanwhile), and an agent's proposal of
the same package with ``--read-only`` added verified too. Every comparison was
of whole command lines, so the two were two servers and both were written:
``bugzilla-example-com`` and ``bugzilla-example-com-mcp-bugzilla``.

Learned, for every server and every mode:

* "try again later" is not a verdict: inconclusive, retried once, never repaired;
* calls to a host are paced, and the same call is asked once per run;
* a repair is kept only if the original, asked again, still falls short;
* a copy of one server -- the same package or URL reaching the same hosts,
  other flags -- is folded into the best of them; different servers never are,
  so the count stays what the default, ``--exhaustive-mcp`` and
  ``--the-best-mcp`` decide.
"""

from __future__ import annotations

import json
import sys

import pytest

from initialize_claude_ai.mcp_auto_discovery import claude_check, contract, login_check, repair, same_server
from initialize_claude_ai.mcp_auto_discovery.types import Candidate, Handshake, StageOutcome, TargetOutcome
from initialize_claude_ai.mcp_auto_discovery.writer import McpWriter

TARGET = "https://bugzilla.example.test"
LAUNCH = ["uvx", "mcp-bugzilla", "--bugzilla-server", TARGET, "--transport", "stdio"]


def bugzilla(extra: tuple[str, ...] = (), name: str = "bugzilla-example-test", **shake) -> Candidate:
    candidate = Candidate(kind="stdio", name=name, command=[*LAUNCH, *extra])
    candidate.handshake = Handshake(ok=True, tools=["bug_info"], round_trip=True, **shake)
    return candidate


def busy(text: str = "429 Too Many Requests") -> dict:
    return {"result": {"isError": True, "content": [{"type": "text", "text": text}]}}


# -- 1-2. "try again later" ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "HTTP 429 Too Many Requests",
        "rate limit exceeded",
        "status 503",
        "502 Bad Gateway",
        "Service Unavailable, try again later",
    ],
)
def test_try_again_later_is_temporary(settings, text) -> None:
    assert login_check.is_temporary(text, settings)


@pytest.mark.parametrize("text", ["401 Unauthorized", "bug 503123 not found", "Invalid API key", ""])
def test_a_real_failure_is_not_temporary(settings, text) -> None:
    assert not login_check.is_temporary(text, settings)


def test_a_temporary_answer_is_inconclusive_for_the_usage_contract(settings) -> None:
    passed, detail = contract.judge(busy(), {"expect": []}, settings)
    assert passed is None and "inconclusive" in detail
    rows = [{"task": "a", "passed": True}, {"task": "b", "passed": None}]
    assert contract.verdict(rows)[0] is True, "an inconclusive task never fails the rest"
    assert contract.verdict([{"task": "b", "passed": None}])[0] is None


def test_a_temporary_answer_through_claude_code_is_could_not_run(settings) -> None:
    events = [
        {"type": "system", "subtype": "init", "mcp_servers": [{"name": "x", "status": "connected"}]},
        {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "1", "name": "mcp__x__t"}]}},
        {
            "type": "user",
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "1",
                        "is_error": True,
                        "content": [{"type": "text", "text": "429 Too Many Requests"}],
                    }
                ]
            },
        },
    ]
    outcome = claude_check.judge_stream("\n".join(json.dumps(e) for e in events), "x", "mcp__x__t", 0, settings)
    assert outcome.outcome == claude_check.COULD_NOT_RUN


# -- 3-4. paced, asked once, retried once ------------------------------------------------------


def test_a_temporary_answer_is_asked_once_more_and_the_answer_reused(ctx) -> None:
    from initialize_claude_ai.mcp_auto_discovery.probes import McpProber, fresh_answers

    answers = [busy(), {"result": {"content": [{"type": "text", "text": "{}"}]}}]
    asked: list[str] = []

    def call(method, rid, params=None):
        asked.append(method)
        return answers.pop(0)

    prober = McpProber(ctx)
    waited: list[float] = []
    ask = prober.patient(call, "launch", "bugzilla.example.test", waited)
    params = {"name": "bug_info", "arguments": {"id": 1}}
    assert not ask("tools/call", 1, params)["result"].get("isError")
    assert ask("tools/call", 2, params)["result"]["content"], "the same call is answered from the run's memo"
    assert asked == ["tools/call", "tools/call"] and waited
    with fresh_answers():
        answers.append({"result": {"content": []}})
        ask("tools/call", 3, params)
    assert len(asked) == 3, "the ranking's repeats are asked anew"


def test_a_temporary_answer_is_never_remembered(ctx) -> None:
    from initialize_claude_ai.mcp_auto_discovery.probes import McpProber

    ask = McpProber(ctx).patient(lambda *a: busy(), "launch-2", "bugzilla.example.test")
    ask("tools/call", 1, {"name": "t"})
    assert not any("launch-2" in k for k in ctx.state.get("mcp_call_memo", {}))


def test_calls_to_one_host_are_paced(ctx) -> None:
    from initialize_claude_ai.mcp_auto_discovery.probes import McpProber

    ctx.settings.set("mcp.verification.pacing.enabled", True)
    ctx.settings.set("mcp.verification.pacing.min_gap_seconds", 0.05)
    prober = McpProber(ctx)
    assert prober._pace("paced.example.test") == 0.0
    assert prober._pace("paced.example.test") > 0.0
    assert prober._pace("other.example.test") == 0.0, "the budget is per host"


# -- 6-7. repair only what is broken ---------------------------------------------------------------


def test_a_temporary_failure_is_not_repaired() -> None:
    candidate = bugzilla(login=login_check.TEMPORARY)
    assert repair.classify(candidate) == []
    candidate = bugzilla(contract=[{"task": "read a bug", "passed": None}], login=login_check.REJECTED)
    assert repair.classify(candidate) == []


def test_the_original_counts_only_if_it_meets_the_repaired_standard() -> None:
    fixed = bugzilla(login=login_check.ACCEPTED)
    fixed.handshake.contract = [{"task": "read a bug", "passed": True}]
    unread = bugzilla(login=login_check.ACCEPTED)
    unread.handshake.contract = [{"task": "read a bug", "passed": True}]
    unread.handshake.shape_problems = [{"remedy": "API_TOKEN is not a variable the server reads"}]
    assert not repair._as_good(unread, fixed, None), "a shape fault is still there when asked again"
    anonymous = bugzilla(login=login_check.UNCHECKED)
    anonymous.handshake.contract = [{"task": "read a bug", "passed": True}]
    assert not repair._as_good(anonymous, fixed, None), "less proof of the login"
    again = bugzilla(login=login_check.ACCEPTED)
    again.handshake.contract = [{"task": "read a bug", "passed": True}]
    assert repair._as_good(again, fixed, None)


# -- 10. copies of one server ---------------------------------------------------------------------


def test_the_same_package_with_other_flags_is_a_copy(settings) -> None:
    key = same_server.key
    assert key(bugzilla(), settings) == key(bugzilla(("--read-only",)), settings)
    pinned = Candidate(kind="stdio", command=["uvx", "mcp-bugzilla@1.2.0", "--bugzilla-server", TARGET])
    assert key(pinned, settings) == key(bugzilla(), settings), "a version is not another server"


def test_different_servers_are_never_copies(settings) -> None:
    key = same_server.key
    elsewhere = Candidate(kind="stdio", command=["uvx", "mcp-bugzilla", "--bugzilla-server", "https://other.test"])
    assert key(elsewhere, settings) != key(bugzilla(), settings), "another host"
    other = Candidate(kind="stdio", command=["uvx", "other-bugzilla-mcp", "--bugzilla-server", TARGET])
    assert key(other, settings) != key(bugzilla(), settings), "another package"
    mcp = Candidate(kind="url", url="https://mcp.example.test/mcp", transport="streamable-http")
    sse = Candidate(kind="url", url="https://mcp.example.test/sse", transport="sse")
    assert key(mcp, settings) != key(sse, settings), "another endpoint"
    same_url_sse = Candidate(kind="url", url="https://mcp.example.test/mcp", transport="sse")
    assert key(mcp, settings) != key(same_url_sse, settings), "another transport"


def test_the_best_proved_copy_is_kept_and_the_existing_one_wins_a_tie(settings) -> None:
    existing, agents = bugzilla(), bugzilla(("--read-only",), name="mcp-bugzilla")
    existing.existing_name = "bugzilla-example-test"
    kept, folded = same_server.fold([agents, existing], settings)
    assert kept == [existing] and folded == [(agents, existing)]
    better = bugzilla(("--read-only",), name="mcp-bugzilla", login=login_check.ACCEPTED)
    better.handshake.contract = [{"task": "read a bug", "passed": True}]
    assert same_server.fold([existing, better], settings)[0] == [better], "more proof wins"
    waiting = bugzilla(("--read-only",))
    waiting.handshake = Handshake(ok=True, credential_pending=["BUGZILLA_API_KEY"])
    assert same_server.fold([waiting, existing], settings)[0] == [existing], "working beats waiting"


def test_folding_can_be_turned_off(settings) -> None:
    settings.set("mcp.registration.fold_copies", False)
    assert len(same_server.fold([bugzilla(), bugzilla(("--read-only",))], settings)[0]) == 2


@pytest.mark.parametrize("mode", ["default", "exhaustive", "the_best"])
def test_the_bugzilla_case_ends_with_one_entry_named_for_the_address(ctx, mode) -> None:
    if mode == "exhaustive":
        ctx.settings.set("mcp.exhaustive", True)
    if mode == "the_best":
        ctx.settings.set("mcp.pick_best", True)
    existing, agents = bugzilla(), bugzilla(("--read-only",), name="mcp-bugzilla")
    existing.existing_name = "bugzilla-example-test"
    registrations = McpWriter(ctx).publish(TargetOutcome(target=TARGET, verified=[existing, agents]))
    assert [r.name for r in registrations] == ["bugzilla-example-test"]
    assert registrations[0].candidate is existing


def test_different_servers_of_one_address_are_all_still_written_by_default(ctx) -> None:
    """The count is the modes' business: deepwiki's /mcp and /sse stay two entries."""
    mcp = Candidate(kind="url", url="https://mcp.example.test/mcp", transport="streamable-http")
    sse = Candidate(kind="url", url="https://mcp.example.test/sse", transport="sse")
    for c in (mcp, sse):
        c.handshake = Handshake(ok=True, tools=["a"], round_trip=True)
    names = [
        r.name for r in McpWriter(ctx).publish(TargetOutcome(target="https://mcp.example.test", verified=[mcp, sse]))
    ]
    assert sorted(names) == ["mcp-example-test-mcp", "mcp-example-test-sse"]


def test_both_modes_fold_copies_where_a_stage_is_verified(ctx) -> None:
    from initialize_claude_ai.mcp_auto_discovery.traditional import TraditionalPipeline

    existing, agents = bugzilla(), bugzilla(("--read-only",), name="mcp-bugzilla")
    waiting = bugzilla(("--read-only", "--x"))
    waiting.handshake = Handshake(ok=True, credential_pending=["BUGZILLA_API_KEY"])
    stage = StageOutcome(
        stage="package", candidates=[existing, agents, waiting], verified=[existing, agents], provisional=[waiting]
    )
    TraditionalPipeline(ctx).fold_copies(stage, TargetOutcome(target=TARGET))
    assert stage.verified == [existing] and stage.provisional == []
    assert len(stage.findings["copies"]) == 2 and agents in stage.candidates, "copies stay as fallbacks"


# -- 13. the CLI is asked to remove only what it holds -----------------------------------------------


def test_an_entry_only_in_the_project_file_is_never_removed_through_the_cli(ctx, sandbox) -> None:
    ctx.settings.set("mcp.mcp_json_path", str(sandbox / ".mcp.json"))
    ctx.settings.set("mcp.discovery.reuse.sources", ["project_file"])
    (sandbox / ".mcp.json").write_text(json.dumps({"mcpServers": {"bugzilla-example-test": {"command": "uvx"}}}))
    writer = McpWriter(ctx)
    assert writer._only_in_project_file("bugzilla-example-test")
    assert not writer._only_in_project_file("somewhere-else")


# -- end to end: a real server process that is busy once ------------------------------------------------

SERVER = """
import json, os, sys
from pathlib import Path
count = Path(sys.argv[1])
tools = [{"name": "bug_info", "inputSchema": {"type": "object", "properties": {}}}]
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
        n = int(count.read_text() or 0) if count.exists() else 0
        count.write_text(str(n + 1))
        if n == 0:
            result = {"isError": True, "content": [{"type": "text", "text": "HTTP 429 Too Many Requests"}]}
        else:
            result = {"content": [{"type": "text", "text": json.dumps({"bugs": [{"id": 1}]})}]}
    else:
        result = {}
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": rid, "result": result}) + "\\n")
    sys.stdout.flush()
"""


def test_a_server_busy_once_is_answered_not_failed(ctx, tmp_path) -> None:
    from initialize_claude_ai.mcp_auto_discovery.probes import McpProber

    ctx.dry_run = False
    script = tmp_path / "bugzilla.py"
    script.write_text(SERVER)
    shake = McpProber(ctx).probe_stdio(
        [sys.executable, str(script), str(tmp_path / "count")],
        env={"BUGZILLA_SERVER": TARGET},
        timeout=20,
        login=True,
    )
    assert shake.ok, shake.error
    assert int((tmp_path / "count").read_text()) >= 2, "asked once more after the busy answer"
    assert shake.login != login_check.TEMPORARY and shake.login not in login_check.REFUSED, shake.login
    assert not any(r.get("verdict") == login_check.TEMPORARY for r in shake.functional)


# -- the packaged settings equal the code ----------------------------------------------------------------


def test_packaged_settings_equal_the_code(settings) -> None:
    check = settings.section("mcp.verification.login_check")
    assert tuple(check.str_list("temporary_markers")) == login_check.DEFAULT_TEMPORARY_MARKERS
    assert tuple(int(c) for c in check.list_("temporary_statuses")) == login_check.DEFAULT_TEMPORARY_STATUSES
    pacing = settings.section("mcp.verification.pacing")
    assert pacing.float_("min_gap_seconds") == 0.2 and pacing.int_("max_per_minute") == 40


def test_the_suite_runs_unpaced(settings) -> None:
    """conftest turns pacing and the retry wait off; the packaged values are on."""
    assert settings.bool_("mcp.verification.pacing.enabled") is False
    assert settings.float_("mcp.verification.login_check.temporary_retry_seconds") == 0
    assert settings.bool_("mcp.registration.fold_copies") is True
