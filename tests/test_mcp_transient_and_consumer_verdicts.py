"""A dropped connection is "try again", and Claude Code's own verdict outranks ours.

On 2026-10-22 github-com's ``get_me`` met ``TLS handshake timeout`` and
``unexpected EOF`` on a slow first connection to api.github.com. Neither matched
a "try again later" marker, so they were read as answers. When Claude Code
itself then called ``get_me`` and it failed, the target was still reported
"PASSED", because a failing check through Claude Code only added a line to the
remediation.

Learned, for every server and both modes:

* a connection that dropped or stalled on the way is temporary -- retried once,
  then inconclusive; never a refused credential, a failed task or a reason to
  replace an entry. A certificate or "fetch failed" is not temporary: it is a
  trust store to repair (``transport_failure_markers``, ``heal_transport``);
* a failed check through Claude Code is asked once more before it is believed,
  and a target whose entry Claude Code could not use is written but never
  counted as working.
"""

from __future__ import annotations

import sys

import pytest

from initialize_claude_ai.mcp_auto_discovery import claude_check, contract, login_check
from initialize_claude_ai.mcp_auto_discovery.types import Candidate, Handshake, TargetOutcome
from initialize_claude_ai.mcp_auto_discovery.writer import McpWriter


def failed(text: str) -> dict:
    return {"jsonrpc": "2.0", "id": 3, "result": {"isError": True, "content": [{"type": "text", "text": text}]}}


# -- a dropped connection is temporary ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        'failed to get user: Get "https://api.github.com/user": unexpected EOF',
        'failed to get user: Get "https://api.github.com/user": net/http: TLS handshake timeout',
        "read ECONNRESET",
        "getaddrinfo EAI_AGAIN api.example.test",
        "dial tcp: i/o timeout",
        "context deadline exceeded (Client.Timeout exceeded while awaiting headers)",
        "Error: socket hang up",
        "Temporary failure in name resolution",
    ],
)
def test_a_connection_that_dropped_is_temporary(settings, text) -> None:
    verdict, _detail = login_check.judge(failed(text), settings)
    assert verdict == login_check.TEMPORARY


@pytest.mark.parametrize(
    "text",
    [
        "Failed to introspect schema: TypeError: fetch failed",
        "UNABLE_TO_GET_ISSUER_CERT_LOCALLY",
        "401 Bad credentials",
        "403 Resource not accessible by personal access token",
    ],
)
def test_a_trust_store_or_a_refusal_is_not_temporary(settings, text) -> None:
    """A certificate is repaired, a refusal is a refusal -- neither is waited out."""
    verdict, _detail = login_check.judge(failed(text), settings)
    assert verdict != login_check.TEMPORARY


def test_a_temporary_failure_never_fails_a_usage_task(settings) -> None:
    passed, detail = contract.judge(failed("unexpected EOF"), {"expect": []}, settings)
    assert passed is None and "inconclusive" in detail


def test_no_answer_at_all_still_counts_as_a_failed_call(settings) -> None:
    """Unchanged on purpose: a server that answers no call at all is how a hang is recognised."""
    verdict, detail = login_check.judge(None, settings)
    assert verdict == login_check.UNCHECKED and detail == "the call was not answered"


SERVER_DROPS_ONCE = """
import json, sys
from pathlib import Path
count = Path(sys.argv[1])
for line in sys.stdin:
    msg = json.loads(line)
    rid, method = msg.get("id"), msg.get("method")
    if rid is None:
        continue
    if method == "initialize":
        result = {"protocolVersion": msg["params"]["protocolVersion"], "capabilities": {"tools": {}},
                  "serverInfo": {"name": "x", "version": "1"}}
    elif method == "tools/list":
        result = {"tools": [{"name": "get_me", "inputSchema": {"type": "object", "properties": {}}}]}
    elif method == "tools/call":
        n = int(count.read_text() or 0) if count.exists() else 0
        count.write_text(str(n + 1))
        result = ({"isError": True, "content": [{"type": "text", "text": "Get https://api.example.test/user: unexpected EOF"}]}
                  if n == 0 else {"content": [{"type": "text", "text": json.dumps({"login": "alice"})}]})
    else:
        result = {}
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": rid, "result": result}) + "\\n")
    sys.stdout.flush()
"""


def test_a_dropped_connection_is_asked_again_and_the_second_answer_counts(ctx, tmp_path) -> None:
    from initialize_claude_ai.mcp_auto_discovery.probes import McpProber

    ctx.dry_run = False
    script = tmp_path / "server.py"
    script.write_text(SERVER_DROPS_ONCE)
    shake = McpProber(ctx).probe_stdio(
        [sys.executable, str(script), str(tmp_path / "count")],
        env={"EXAMPLE_PERSONAL_ACCESS_TOKEN": "a-real-one"},
        timeout=20,
        login=True,
    )
    assert shake.ok, shake.error
    assert int((tmp_path / "count").read_text()) >= 2, "asked once more after the dropped connection"
    assert shake.login not in login_check.REFUSED, "a dropped connection never reads as a refused credential"


def test_the_packaged_markers_equal_the_code(settings) -> None:
    check = settings.section("mcp.verification.login_check")
    assert tuple(check.str_list("temporary_markers")) == login_check.DEFAULT_TEMPORARY_MARKERS
    transport = settings.str_list("mcp.verification.transport_failure_markers")
    assert "fetch failed" in transport and "fetch failed" not in login_check.DEFAULT_TEMPORARY_MARKERS


# -- Claude Code's own verdict ------------------------------------------------------------------------------


def test_a_failed_check_through_claude_code_is_asked_again(ctx, monkeypatch) -> None:
    answers = [
        claude_check.Outcome(claude_check.FAILED, "has an output schema but did not return structured content"),
        claude_check.Outcome(claude_check.PASSED, "through Claude Code: passed"),
    ]
    monkeypatch.setattr(claude_check, "_check_once", lambda *a, **k: answers.pop(0))
    assert claude_check.check(ctx, "x", {}, None).outcome == claude_check.PASSED
    assert answers == []


def test_a_failure_asked_twice_is_believed(ctx, monkeypatch) -> None:
    calls: list[int] = []

    def once(*_a, **_k):
        calls.append(1)
        return claude_check.Outcome(claude_check.FAILED, "the tool failed")

    monkeypatch.setattr(claude_check, "_check_once", once)
    assert claude_check.check(ctx, "x", {}, None).outcome == claude_check.FAILED
    assert len(calls) == 2


def test_the_retry_is_configurable(ctx, monkeypatch) -> None:
    calls: list[int] = []
    monkeypatch.setattr(
        claude_check, "_check_once", lambda *a, **k: calls.append(1) or claude_check.Outcome(claude_check.FAILED, "x")
    )
    ctx.settings.set("mcp.registration.claude_check.retries_on_failure", 0)
    claude_check.check(ctx, "x", {}, None)
    assert len(calls) == 1
    assert ctx.settings.int_("mcp.registration.claude_check.retries_on_failure") == 0


def test_passing_and_inconclusive_checks_are_not_asked_again(ctx, monkeypatch) -> None:
    for outcome in (claude_check.PASSED, claude_check.COULD_NOT_RUN, claude_check.MISSING_VARIABLE):
        calls: list[int] = []
        monkeypatch.setattr(
            claude_check, "_check_once", lambda *a, _o=outcome, **k: calls.append(1) or claude_check.Outcome(_o, "x")
        )
        claude_check.check(ctx, "x", {}, None)
        assert len(calls) == 1, outcome


def test_a_target_claude_code_could_not_use_is_not_working() -> None:
    candidate = Candidate(kind="url", name="x", url="https://x.example.test/mcp")
    outcome = TargetOutcome(target="https://x.example.test", verified=[candidate])
    assert outcome.succeeded
    outcome.consumer_failed.append(candidate.identity())
    assert not outcome.succeeded
    assert outcome.to_dict()["consumer_failed"] == [candidate.identity()]


def test_another_verified_server_still_counts(ctx) -> None:
    one = Candidate(kind="url", name="a", url="https://x.example.test/a")
    two = Candidate(kind="url", name="b", url="https://x.example.test/b")
    outcome = TargetOutcome(target="https://x.example.test", verified=[one, two], consumer_failed=[one.identity()])
    assert outcome.succeeded


def test_publishing_records_what_claude_code_could_not_use(ctx, monkeypatch) -> None:
    """Through publish: the entry is written, and the target stops counting as working."""
    ctx.dry_run = False
    monkeypatch.setattr(claude_check, "enabled", lambda _s: True)
    monkeypatch.setattr(
        claude_check, "connect_check", lambda *a, **k: claude_check.Outcome(claude_check.PASSED, "connected")
    )
    monkeypatch.setattr(
        claude_check,
        "check",
        lambda *a, **k: claude_check.Outcome(claude_check.FAILED, "through Claude Code: failed", "t"),
    )
    candidate = Candidate(kind="url", name="example-test", url="https://example.test/mcp")
    candidate.handshake = Handshake(ok=True, tools=["t"], round_trip=True)
    outcome = TargetOutcome(target="https://example.test", verified=[candidate])
    [registration] = McpWriter(ctx).publish(outcome)
    assert registration.written
    assert outcome.consumer_failed == [candidate.identity()]
    assert not outcome.succeeded


# -- a server this run did not launch is not one that "does not start" ------------------------------------


def test_a_dry_run_records_that_nothing_was_launched(ctx, monkeypatch) -> None:
    from initialize_claude_ai.mcp_auto_discovery.verifier import CandidateVerifier

    assert ctx.dry_run
    # The launcher is installed: the dry run is the subject, not whether this
    # machine has uvx (a bare container does not, and says so first).
    monkeypatch.setattr(ctx.runner, "which", lambda name, *a, **k: f"/usr/bin/{name}")
    candidate = CandidateVerifier(ctx).verify(Candidate(kind="stdio", name="x", command=["uvx", "some-mcp-server"]))
    assert candidate.handshake.not_launched == "dry run"
    assert candidate.handshake.to_dict()["not_launched"] == "dry run"


def test_the_file_is_read_before_it_is_said_to_have_no_entry(ctx, sandbox) -> None:
    import json as _json

    from initialize_claude_ai.phases.phase4_mcp_auto_discovery import McpPhase

    ctx.settings.set("mcp.mcp_json_path", str(sandbox / ".mcp.json"))
    (sandbox / ".mcp.json").write_text(_json.dumps({"mcpServers": {"bugzilla-example-test": {"command": "uvx"}}}))
    phase, writer = McpPhase(ctx), McpWriter(ctx)
    kept = phase._nothing_registered(writer, TargetOutcome(target="https://bugzilla.example.test"), "dry run")
    absent = phase._nothing_registered(writer, TargetOutcome(target="https://jira.example.test"), "nothing found")
    assert "keeps its existing bugzilla-example-test" in kept and "has no entry" not in kept
    assert "has no entry for jira-example-test" in absent


# -- one operation's 403 is not the account's -------------------------------------------------------------

ACCEPTED_ROW = (login_check.ACCEPTED, "confluence_search", "")
SCOPED_403 = (
    login_check.NO_ACCESS,
    "confluence_search_user",
    "Authentication failed for Confluence API (403). Token may be expired or invalid.",
)
PORTAL = (login_check.NO_ACCESS, "jira_get_issue", "redirected to /servicedesk/customer/portal/1")


def test_content_proves_the_credential_and_one_operations_403_is_a_permission_limit(settings) -> None:
    """confluence.example.com: search returned the operator's page; user search answered 403."""
    verdict, tool, detail = login_check.summarise([ACCEPTED_ROW, SCOPED_403], settings)
    assert verdict == login_check.ACCEPTED and tool == "confluence_search"
    assert "permission limit" in detail and "confluence_search_user" in detail


def test_a_redirect_to_a_portal_still_outranks_acceptance(settings) -> None:
    """sd.example.com's customer account: the refusal is about the account, and stays the truth."""
    assert login_check.summarise([ACCEPTED_ROW, PORTAL], settings)[0] == login_check.NO_ACCESS


def test_a_refused_credential_is_never_turned_into_acceptance(settings) -> None:
    """With a 401 anywhere, the refinement stands aside: the verdict stays a refusal."""
    rejected = (login_check.REJECTED, "get_me", "401 Bad credentials")
    assert login_check.summarise([ACCEPTED_ROW, SCOPED_403, rejected], settings)[0] in login_check.REFUSED
    assert login_check.summarise([ACCEPTED_ROW, rejected], settings)[0] == login_check.REJECTED


def test_an_empty_answer_never_outranks_a_refusal(settings) -> None:
    empty = (login_check.EMPTY, "jira_get_all_projects", "answered with nothing")
    assert login_check.summarise([empty, SCOPED_403], settings)[0] == login_check.NO_ACCESS


def test_the_refinement_is_configurable(settings) -> None:
    settings.set("mcp.verification.login_check.content_outranks_scoped_refusal", False)
    assert login_check.summarise([ACCEPTED_ROW, SCOPED_403], settings)[0] == login_check.NO_ACCESS
    assert (
        tuple(settings.str_list("mcp.verification.login_check.account_wide_markers"))
        == login_check.DEFAULT_ACCOUNT_WIDE_MARKERS
    )


def test_a_waiting_entry_with_nothing_left_to_set_never_asks_for_nothing(ctx) -> None:
    """confluence-example-com read "until  is set" once its token was kept in settings."""
    from initialize_claude_ai.mcp_auto_discovery.writer import Registration

    writer = McpWriter(ctx)
    candidate = Candidate(kind="stdio", name="x", command=["x"])
    in_place = Registration(
        name="x",
        candidate=candidate,
        provisional=True,
        needs_env=[],
        claude_usage={"outcome": "passed", "detail": "through Claude Code: passed"},
    )
    check = writer.claude_check(in_place)
    assert "until  is set" not in check.detail and check.detail == "through Claude Code: passed"
    assert check.ok is True
    empty = Registration(name="x", candidate=candidate, provisional=True, needs_env=["X_TOKEN"])
    assert "until X_TOKEN is set" in writer.claude_check(empty).detail


def test_a_service_answering_outside_its_apis_form_is_temporary(settings) -> None:
    """bugzilla.opensuse.org's /rest/time once answered HTTP 200 "application/x-cgi"."""
    text = (
        "Failed to fetch bugzilla server info Reason: Bugzilla returned a non-JSON response from "
        "https://bugzilla.example.test/rest/time (HTTP 200, content-type 'application/x-cgi')"
    )
    assert login_check.judge(failed(text), settings)[0] == login_check.TEMPORARY
    assert (
        login_check.judge(failed("SyntaxError: Unexpected token < in JSON at position 0"), settings)[0]
        == login_check.TEMPORARY
    )


def test_an_existing_entry_that_started_is_never_said_not_to_start(ctx) -> None:
    """bugzilla-opensuse-org started and handshaked; one call met a glitch -- "does not start" was false."""
    from initialize_claude_ai.phases.phase4_mcp_auto_discovery import McpPhase

    phase, writer = McpPhase(ctx), McpWriter(ctx)
    outcome = TargetOutcome(target="https://bugzilla.example.test")
    outcome.broken_existing = [
        {
            "name": "bugzilla-example-test",
            "source": "project",
            "started": True,
            "error": "usage task 'describe the server' failed",
        },
        {"name": "other-example-test", "source": "project", "started": False, "error": "exited with rc=1"},
    ]
    seen = []
    phase.report_step = lambda _result, title, status, detail, **kw: seen.append((title, detail, kw))
    phase._report_existing_left(None, writer, outcome)
    started = next((d, kw) for t, d, kw in seen if "bugzilla-example-test" in t)
    stopped = next(d for t, d, _ in seen if "other-example-test" in t)
    assert "does not start" not in started[0] and "did not do its job" in started[0]
    assert "--mcp-recheck bugzilla.example.test" in started[1]["remediation"][0]
    assert "does not start" in stopped
