"""What "verified" has to mean before a server is written anywhere.

An endpoint that returns an ``initialize`` result is the cheapest thing a server
can do, and it is not what phase 4 promises. These tests pin the three things it
does promise -- connected, something callable, and a request answered *after* the
handshake -- plus the fourth line of the report, which is Claude Code's own
verdict on the server it was handed.

The last two sections cover what the phase says once all of that is done: the
tier-1 checklist table, and the closing verdict line, whose final word is the
only thing a wrapping script can read without parsing the report.
"""

from __future__ import annotations

import io

import pytest

from initialize_claude_ai.core.logging_setup import Console
from initialize_claude_ai.core.proc import CommandResult
from initialize_claude_ai.core.report import PhaseResult, Status
from initialize_claude_ai.mcp_auto_discovery.address import configure_local_zones
from initialize_claude_ai.mcp_auto_discovery.policy import DueDiligence
from initialize_claude_ai.mcp_auto_discovery.probes import McpProber
from initialize_claude_ai.mcp_auto_discovery.types import (
    Candidate,
    Handshake,
    TargetOutcome,
    Transport,
)
from initialize_claude_ai.mcp_auto_discovery.verifier import CandidateVerifier
from initialize_claude_ai.mcp_auto_discovery.writer import McpWriter, Registration
from initialize_claude_ai.phases.phase4_mcp_auto_discovery import McpPhase


def handshake(**overrides) -> Handshake:
    """A handshake that passes everything, so a test can fail one thing at a time."""
    base = dict(
        ok=True,
        transport=Transport.STREAMABLE_HTTP,
        url="https://mcp.example.test/mcp",
        protocol_version="2025-06-18",
        server_name="Example",
        server_version="1.0",
        tools=["search"],
        round_trip=True,
        round_trip_method="tools/list",
    )
    return Handshake(**{**base, **overrides})


def candidate(**overrides) -> Candidate:
    base = dict(kind="url", url="https://mcp.example.test/mcp", transport=Transport.STREAMABLE_HTTP)
    return Candidate(**{**base, **overrides})


@pytest.fixture(autouse=True)
def no_real_claude(ctx, monkeypatch) -> None:
    """Nothing here may question a Claude Code that happens to be installed.

    The fourth check runs the real CLI when it finds one, so on a machine that
    has Claude Code these tests would otherwise ask it about servers that do not
    exist. Absent by default; the tests that need one hand it back.
    """
    monkeypatch.setattr(type(ctx), "tool", lambda self, *a, **k: None)


# -- the policy ---------------------------------------------------------------


def test_a_handshake_alone_is_not_a_working_server(ctx) -> None:
    """An endpoint that says hello and then answers nothing is not usable."""
    accepted, why = McpProber(ctx).accepts(handshake(round_trip=False, round_trip_method=""))
    assert not accepted
    assert "both directions" in why


def test_a_server_with_nothing_to_call_is_not_a_result(ctx) -> None:
    accepted, why = McpProber(ctx).accepts(handshake(tools=[]))
    assert not accepted
    assert "no tools" in why


def test_resources_count_as_something_to_call(ctx) -> None:
    accepted, _why = McpProber(ctx).accepts(handshake(tools=[], resources=["file://readme"]))
    assert accepted


def test_both_requirements_are_settings_not_rules(ctx) -> None:
    """Strict by default, relaxable by configuration -- and nothing is hardcoded."""
    ctx.settings.set("mcp.verification.require_round_trip", False)
    ctx.settings.set("mcp.verification.allow_empty_tools", True)
    # The prober snapshots [mcp.verification] when it is built, so it is built here.
    accepted, _why = McpProber(ctx).accepts(handshake(tools=[], round_trip=False))
    assert accepted


# -- proving the round trip ---------------------------------------------------


def test_listing_the_tools_is_what_usually_proves_it(ctx) -> None:
    prober = McpProber(ctx)
    shake = Handshake(ok=True, capabilities={"tools": {}})
    asked: list[str] = []

    def call(method: str, rid: int):
        asked.append(method)
        return {"jsonrpc": "2.0", "id": rid, "result": {"tools": [{"name": "search"}]}}

    prober._list_capabilities(shake, call)
    prober._prove_round_trip(shake, call)

    assert shake.tools == ["search"]
    assert shake.round_trip and shake.round_trip_method == "tools/list"
    assert asked == ["tools/list"], "nothing more should be asked once one answer arrived"


def test_a_server_that_advertises_nothing_is_pinged_instead(ctx) -> None:
    """``ping`` exists in the specification for exactly this question."""
    prober = McpProber(ctx)
    shake = Handshake(ok=True, capabilities={})
    asked: list[str] = []

    def call(method: str, rid: int):
        asked.append(method)
        return {"jsonrpc": "2.0", "id": rid, "result": {}}

    prober._prove_round_trip(shake, call)

    assert asked == ["ping"]
    assert shake.round_trip and shake.round_trip_method == "ping"


def test_an_answer_to_a_different_question_proves_nothing(ctx) -> None:
    """Matching the id is the point: it is what a replay or a proxy cannot fake."""
    shake = Handshake(ok=True, capabilities={})
    McpProber(ctx)._prove_round_trip(shake, lambda method, rid: {"id": rid + 1, "result": {}})
    assert not shake.round_trip


def test_a_server_that_never_answers_is_not_marked_two_way(ctx) -> None:
    shake = Handshake(ok=True, capabilities={})

    def call(method: str, rid: int):
        raise TimeoutError("no answer")

    McpProber(ctx)._prove_round_trip(shake, call)
    assert not shake.round_trip


def test_the_ping_can_be_turned_off(ctx) -> None:
    ctx.settings.set("mcp.protocol.ping_method", "")
    shake = Handshake(ok=True, capabilities={})
    asked: list[str] = []
    McpProber(ctx)._prove_round_trip(shake, lambda method, rid: asked.append(method))
    assert not asked and not shake.round_trip


# -- the report ----------------------------------------------------------------


def test_the_audit_shows_its_working(ctx) -> None:
    checks = CandidateVerifier.audit(candidate(handshake=handshake()))
    # "schema": its tool definitions are ones the client accepts.
    assert [c.name for c in checks] == ["connected", "tools", "two-way", "schema"]
    assert all(c.ok for c in checks)
    assert "protocol 2025-06-18" in checks[0].detail
    assert "1 tool(s)" == checks[1].detail
    assert "tools/list" in checks[2].detail


def test_the_audit_names_the_check_that_failed(ctx) -> None:
    checks = CandidateVerifier.audit(candidate(handshake=handshake(round_trip=False)))
    assert [c.mark for c in checks] == ["yes", "yes", "no", "yes"]
    assert "nothing sent after the handshake" in checks[2].detail


def test_an_unprobed_candidate_audits_as_never_connected(ctx) -> None:
    checks = CandidateVerifier.audit(candidate())
    assert [(c.name, c.ok) for c in checks] == [("connected", False)]


# -- what claude itself says ---------------------------------------------------


def registration(**overrides) -> Registration:
    base = dict(name="mcp-example-test", candidate=candidate(handshake=handshake()), registered=True)
    return Registration(**{**base, **overrides})


def test_without_the_cli_the_fourth_check_is_unknown_never_a_pass(ctx) -> None:
    """A machine with no Claude Code cannot fail the check, and cannot pass it."""
    check = McpWriter(ctx).claude_check(registration())
    assert check.ok is None and check.mark == "-"
    assert "not installed" in check.detail


def test_an_unregistered_server_has_nothing_for_claude_to_check(ctx, monkeypatch) -> None:
    monkeypatch.setattr(type(ctx), "tool", lambda self, *a, **k: "/usr/bin/claude")
    check = McpWriter(ctx).claude_check(registration(registered=False))
    assert check.ok is None
    assert "nothing to check" in check.detail


def with_claude_saying(ctx, monkeypatch, output: str, returncode: int = 0):
    monkeypatch.setattr(type(ctx), "tool", lambda self, *a, **k: "/usr/bin/claude")
    monkeypatch.setattr(
        ctx.runner,
        "probe",
        lambda argv, **kw: CommandResult(argv=list(argv), returncode=returncode, stdout=output),
    )
    return McpWriter(ctx).claude_check(registration())


def test_claude_code_confirming_the_connection_is_the_fourth_check(ctx, monkeypatch) -> None:
    check = with_claude_saying(ctx, monkeypatch, "mcp-example-test:\n  Status: connected\n")
    assert check.ok is True


def test_claude_code_failing_to_connect_is_reported_as_a_failure(ctx, monkeypatch) -> None:
    """ "failed to connect" contains "connected"; the failure markers are read first."""
    check = with_claude_saying(ctx, monkeypatch, "  Status: failed to connect\n")
    assert check.ok is False
    assert "failed to connect" in check.detail


def test_a_cli_that_reports_no_state_is_not_treated_as_a_verdict(ctx, monkeypatch) -> None:
    check = with_claude_saying(ctx, monkeypatch, "mcp-example-test: https://mcp.example.test/mcp\n")
    assert check.ok is True
    assert "no connection state" in check.detail


# -- the phase's verification report -------------------------------------------


@pytest.fixture
def phase(ctx) -> McpPhase:
    return McpPhase(ctx)


def test_the_phase_reports_every_endpoint_against_every_check(phase, ctx) -> None:
    outcome = TargetOutcome(target="https://mcp.example.test")
    first = candidate(handshake=handshake())
    second = candidate(url="https://mcp.example.test/sse", handshake=handshake(tools=[]))
    registrations = [
        Registration(name="mcp-example-test-mcp", candidate=first),
        Registration(name="mcp-example-test-sse", candidate=second),
    ]
    result = PhaseResult(phase="mcp", title="MCP auto-discovery")

    phase._report_verification(result, McpWriter(ctx), outcome, registrations)

    assert [row["name"] for row in outcome.verification] == [
        "mcp-example-test-mcp",
        "mcp-example-test-sse",
    ]
    # "starts" asks of the finished entry what the first three ask of the
    # candidate it came from -- the distinction three failed servers were lost
    # in. See tests/test_mcp_entry_as_written.py.
    assert [c["check"] for c in outcome.verification[0]["checks"]] == [
        "connected",
        "tools",
        "two-way",
        "schema",
        "starts",
        "claude",
    ]
    step = result.steps[-1]
    assert step.status == Status.PARTIAL, "one endpoint exposed nothing callable"
    assert step.detail.startswith("1/2 endpoint(s)")


def test_nothing_found_means_no_verification_report(phase, ctx) -> None:
    outcome = TargetOutcome(target="https://mcp.example.test")
    result = PhaseResult(phase="mcp", title="MCP auto-discovery")
    phase._report_verification(result, McpWriter(ctx), outcome, [])
    assert not result.steps and not outcome.verification


# -- the file, and how to use it -----------------------------------------------


def test_the_phase_says_where_the_servers_went_and_what_to_do_with_them(phase, ctx) -> None:
    outcome = TargetOutcome(target="https://mcp.example.test", registered=["mcp-example-test"])
    result = PhaseResult(phase="mcp", title="MCP auto-discovery")

    phase._report_configuration_file(result, McpWriter(ctx), [outcome])

    step = result.steps[-1]
    assert step.name == "mcp configuration"
    assert "./.mcp.json" in step.detail
    assert any("Start Claude Code from this directory" in line for line in step.remediation)
    assert any("cp ./.mcp.json" in line for line in step.remediation)
    assert result.data["mcp_json_path"].endswith("/.mcp.json")


def test_a_run_that_finds_nothing_still_takes_the_prose_out_of_the_file(phase, ctx, sandbox, monkeypatch) -> None:
    """The stale key has to go even when the run ends before it writes anything
    -- the early returns in the phase are the common case on a machine where
    --mcp-server was forgotten, and that file is still copyable today."""
    import json

    ctx.dry_run = False
    ctx.runner.dry_run = False
    monkeypatch.setattr(type(ctx), "tool", lambda self, *a, **k: None)
    (sandbox / ".mcp.json").write_text(
        json.dumps({"mcpServers": {}, "_instructions": {"how_to_use_this_file": ["old"]}}),
        encoding="utf-8",
    )
    result = PhaseResult(phase="mcp", title="MCP auto-discovery")

    McpPhase(ctx).execute(result)  # no --mcp-server: the phase stops at once

    assert "_instructions" not in json.loads((sandbox / ".mcp.json").read_text(encoding="utf-8"))
    step = next(s for s in result.steps if s.name == "configuration file")
    assert step.status == Status.CHANGED
    assert "_instructions" in step.detail
    assert "copy-and-paste configuration" in step.detail
    assert [s.name for s in result.steps] == ["configuration file", "servers"]


def test_a_file_that_is_already_clean_is_not_mentioned_at_all(phase, ctx, sandbox) -> None:
    import json

    (sandbox / ".mcp.json").write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")
    result = PhaseResult(phase="mcp", title="MCP auto-discovery")

    McpPhase(ctx).execute(result)

    assert [s.name for s in result.steps] == ["servers"]


def test_no_server_means_no_instructions_for_a_file_that_was_not_written(phase, ctx) -> None:
    outcome = TargetOutcome(target="https://mcp.example.test")
    result = PhaseResult(phase="mcp", title="MCP auto-discovery")
    phase._report_configuration_file(result, McpWriter(ctx), [outcome])
    assert not result.steps


def briefed(ctx, **settings) -> tuple[PhaseResult, str]:
    """Publish one server that needs everything, and report on it, out loud."""
    ctx.console = Console(stream=io.StringIO(), color=False, quiet=False, glyph_set="ascii")
    for dotted, value in settings.items():
        ctx.settings.set(dotted, value)
    needy = candidate(
        kind="stdio",
        url="",
        command=["npx", "-y", "@vendor/mcp-server"],
        # A launcher has to name the service it talks to or it is refused before
        # it is written -- see mcp_auto_discovery/grounding.py. Any real entry
        # for this target carries the address somewhere; here it is the endpoint.
        env={"VENDOR_ENDPOINT": "https://mcp.example.test/graphql"},
        secret_env={"VENDOR_API_KEY": "env:REAL_VARIABLE_NAME"},
        install=[["npm", "install", "-g", "@vendor/mcp-server"]],
        source="official MCP registry",
        origin="package",
        handshake=handshake(),
    )
    outcome = TargetOutcome(target="https://mcp.example.test", verified=[needy])
    writer = McpWriter(ctx)
    # Dry run: nothing is written, so nothing lands in `registered` -- but the
    # briefing is exactly what a dry run has to show, so the name is supplied.
    outcome.registered = [r.name for r in writer.publish(outcome)]
    result = PhaseResult(phase="mcp", title="MCP auto-discovery")
    McpPhase(ctx)._report_configuration_file(result, writer, [outcome])
    return result, ctx.console._out.getvalue()


def test_the_briefing_is_on_screen_and_impossible_to_miss(ctx) -> None:
    """Everything that used to be a key in .mcp.json is a framed block instead:
    the file is copied wholesale, and the terminal is where an instruction is
    actually read. The console writes into the log transcript too, so this same
    block is the one in the log file."""
    result, output = briefed(ctx)

    assert "HOW TO USE ./.mcp.json" in output
    assert "+---" in output, "framed, not a run of bullets in the middle of the run"
    assert "Export these before you start Claude Code" in output
    # The variable the entry reads: the generic key is namespaced in the entry,
    # so the briefing names the namespaced variable -- never one nothing reads.
    assert "export MCP_EXAMPLE_TEST_VENDOR_API_KEY=..." in output
    assert "REAL_VARIABLE_NAME" not in output, "the variable is named, never read"
    assert "npm install -g @vendor/mcp-server" in output
    assert "official MCP registry" in output, "the per-server detail is there too"
    assert "Start Claude Code from this directory" in output
    assert result.data["mcp_briefing"]["servers"]["mcp-example-test"]["what"] == ("official MCP registry")


def test_the_briefing_is_in_the_log_file_word_for_word(ctx, sandbox) -> None:
    """The terminal scrolls and the run ends; the log is where the operator
    goes back to find what they were told to export."""
    from initialize_claude_ai.core.logging_setup import Transcript

    transcript = Transcript.open(sandbox / "run.log")
    ctx.console = Console(stream=io.StringIO(), color=False, quiet=False, glyph_set="ascii", transcript=transcript)
    needy = candidate(
        kind="stdio",
        url="",
        command=["npx", "-y", "@vendor/mcp-server"],
        # A launcher has to name the service it talks to or it is refused before
        # it is written -- see mcp_auto_discovery/grounding.py. Any real entry
        # for this target carries the address somewhere; here it is the endpoint.
        env={"VENDOR_ENDPOINT": "https://mcp.example.test/graphql"},
        secret_env={"VENDOR_API_KEY": "env:REAL_VARIABLE_NAME"},
        origin="package",
        handshake=handshake(),
    )
    outcome = TargetOutcome(target="https://mcp.example.test", verified=[needy])
    writer = McpWriter(ctx)
    outcome.registered = [r.name for r in writer.publish(outcome)]
    McpPhase(ctx)._report_configuration_file(PhaseResult(phase="mcp", title="MCP auto-discovery"), writer, [outcome])
    transcript.close()

    logged = (sandbox / "run.log").read_text(encoding="utf-8")
    assert "HOW TO USE ./.mcp.json" in logged
    assert "export MCP_EXAMPLE_TEST_VENDOR_API_KEY=..." in logged
    assert "Start Claude Code from this directory" in logged
    assert "REAL_VARIABLE_NAME" not in logged, "the variable is named in the log, never read"


def test_the_briefing_is_carried_in_the_report_even_when_the_screen_is_quiet(ctx) -> None:
    """``mcp.instructions.enabled = false`` silences the block; it does not
    throw it away, because --json is the other way this run gets read."""
    result, output = briefed(ctx, **{"mcp.instructions.enabled": "false"})

    assert "HOW TO USE" not in output
    assert "MCP_EXAMPLE_TEST_VENDOR_API_KEY" in result.data["mcp_briefing"]["required_environment_variables"]
    step = result.steps[-1]
    assert step.data["briefing"] == result.data["mcp_briefing"]


# -- the final verdict ----------------------------------------------------------
#
# The last thing phase 4 says, and the only line a wrapping script can read
# without parsing the report: one word, last token, no commentary after it.


def verdict_of(phase: McpPhase, ctx, outcomes) -> tuple[PhaseResult, str]:
    result = PhaseResult(phase="mcp", title="MCP auto-discovery")
    phase._report_verdict(result, McpWriter(ctx), outcomes)
    return result, result.steps[-1].detail


def working_outcome(**overrides) -> TargetOutcome:
    fields = {
        "target": "https://mcp.example.test",
        "verified": [candidate(handshake=handshake())],
        "registered": ["mcp-example-test"],
    }
    fields.update(overrides)
    return TargetOutcome(**fields)


def test_everything_worked_ends_in_success(phase, ctx) -> None:
    result, detail = verdict_of(phase, ctx, [working_outcome()])

    assert detail.endswith("SUCCESS"), "a wrapping script greps for the last token"
    assert result.steps[-1].status == Status.OK
    assert result.data["mcp_verdict"] == "SUCCESS"
    assert "1/1 address(es)" in detail
    assert "registered as mcp-example-test" in detail
    assert "No API key, token or other credential is needed" in detail


def test_a_credential_already_supplied_is_not_called_unneeded(phase, ctx, monkeypatch) -> None:
    """gitlab-example-net's login reads GITLAB_EXAMPLE_NET_TOKEN, set in Claude Code's settings: "nothing is needed" was false."""
    from initialize_claude_ai.mcp_auto_discovery import claude_check

    monkeypatch.setattr(McpWriter, "exported_variables", lambda self, c: ["ICA_TEST_HELD_TOKEN"])
    monkeypatch.setattr(claude_check, "fresh_environ", lambda ctx: {"ICA_TEST_HELD_TOKEN": "x" * 20})
    result, detail = verdict_of(phase, ctx, [working_outcome()])

    assert detail.endswith("SUCCESS")
    assert "No API key, token or other credential is needed" not in detail
    assert "already set where Claude Code reads it (ICA_TEST_HELD_TOKEN)" in detail
    assert "x" * 20 not in detail


def test_nothing_worked_ends_in_failed(phase, ctx) -> None:
    result, detail = verdict_of(phase, ctx, [TargetOutcome(target="https://mcp.example.test")])

    assert detail.endswith("FAILED")
    assert result.steps[-1].status == Status.FAILED
    assert result.data["mcp_verdict"] == "FAILED"


def test_one_address_out_of_two_is_partial(phase, ctx) -> None:
    result, detail = verdict_of(phase, ctx, [working_outcome(), TargetOutcome(target="https://other.example.test")])

    assert detail.endswith("PARTIAL")
    assert result.steps[-1].status == Status.PARTIAL
    assert "1/2 address(es)" in detail


def test_a_duplicate_entry_is_repeated_in_the_verdict_and_makes_it_partial(phase, ctx) -> None:
    """Two entries for one service is a decision left with the operator, which
    is not the same result as a clean run -- and a bare pass/fail hides it."""
    outcome = working_outcome(
        duplicates=[{"name": "hand-written", "target": "https://mcp.example.test/sse", "why": "same host."}]
    )
    result, detail = verdict_of(phase, ctx, [outcome])

    assert detail.endswith("PARTIAL")
    assert "Duplicate configuration: hand-written" in detail
    assert result.data["mcp_duplicates"] == outcome.duplicates


def test_the_verdict_names_every_variable_that_must_be_exported(phase, ctx) -> None:
    """Needing a credential is not a defect -- verification already proved the
    server answers with it -- but it is the commonest reason a registered server
    fails later, so the last paragraph repeats it."""
    needy = candidate(handshake=handshake(), secret_env={"VENDOR_API_KEY": "env:REAL_NAME"})
    result, detail = verdict_of(phase, ctx, [working_outcome(verified=[needy])])

    assert detail.endswith("SUCCESS"), "a credential to export is not a failed run"
    assert "export VENDOR_API_KEY" in detail
    assert "REAL_NAME" not in detail, "the reference is never printed, only the variable name"
    assert result.data["mcp_required_environment"] == ["VENDOR_API_KEY"]


def test_an_unfinished_checklist_is_said_again_at_the_end(phase, ctx) -> None:
    outcome = working_outcome()
    outcome.due_diligence = DueDiligence(target=outcome.target)
    _result, detail = verdict_of(phase, ctx, [outcome])

    assert "Tier-1 due diligence was not completed" in detail
    assert "anything below tier 1 was refused" in detail


def test_the_checklist_is_reported_as_a_table_of_its_own(phase, ctx) -> None:
    """The stage lines say what was found; this says how hard it was looked for,
    which is what has to be on the page before "no MCP server here" means
    anything."""
    outcome = TargetOutcome(target="https://mcp.example.test")
    record = DueDiligence(target=outcome.target)
    record.record("paths", Status.OK, "42 paths probed", evidence=["/mcp -> 404"])
    outcome.due_diligence = record
    result = PhaseResult(phase="mcp", title="MCP auto-discovery")

    phase._report_due_diligence(result, outcome)

    step = result.steps[-1]
    assert step.name == "https://mcp.example.test :: due diligence"
    assert step.status == Status.PARTIAL, "three of the four steps never ran"
    assert step.detail == "1/4 checks; no native endpoint answered"
    assert "mcp.policy.require_checklist" in " ".join(step.remediation)
    assert step.data["steps"][0]["evidence"] == ["/mcp -> 404"]


def test_a_native_endpoint_is_reported_as_the_intended_outcome(phase, ctx) -> None:
    outcome = TargetOutcome(target="https://mcp.example.test")
    record = DueDiligence(target=outcome.target, native_endpoints=["https://mcp.example.test/mcp"])
    for key in ("paths", "bypasses", "handshake", "grounding"):
        record.record(key, Status.OK, "done", evidence=["evidence"])
    outcome.due_diligence = record
    result = PhaseResult(phase="mcp", title="MCP auto-discovery")

    phase._report_due_diligence(result, outcome)

    step = result.steps[-1]
    assert step.status == Status.OK
    assert "nothing was installed or generated" in " ".join(step.remediation)


# -- keeping a credential off somebody else's wire ----------------------------


def test_a_bearer_token_is_withheld_from_a_cleartext_probe_of_a_public_host(ctx) -> None:
    headers = McpProber(ctx).safe_headers(
        "http://mcp.example.com/mcp", {"Authorization": "Bearer t", "Accept": "application/json"}
    )
    assert "authorization" not in headers
    assert headers["accept"] == "application/json", "only the credential is dropped"


def test_the_token_still_goes_to_loopback(ctx) -> None:
    headers = McpProber(ctx).safe_headers("http://127.0.0.1:8080/mcp", {"Authorization": "Bearer t"})
    assert headers["authorization"] == "Bearer t"


def test_the_token_goes_to_a_zone_the_operator_declared_theirs(ctx) -> None:
    """Withholding it would make an internal server answer 401 and be written off
    as not speaking MCP -- a wrong answer produced by not knowing the network."""
    url = "http://mcp.corp/mcp"
    assert "authorization" not in McpProber(ctx).safe_headers(url, {"Authorization": "Bearer t"})

    configure_local_zones([".corp"])
    assert McpProber(ctx).safe_headers(url, {"Authorization": "Bearer t"})["authorization"] == "Bearer t"


# -- prerequisites ------------------------------------------------------------
#
# An agent proposes the commands that have to run before a package can be
# launched. They are executed one at a time, as argv, with no shell -- so what
# they are allowed to be, and what happens when one fails, decides whether a
# candidate is rejected on its own merits or on ours.


@pytest.fixture
def live(ctx, monkeypatch):
    """A context that would really run things, with the running stubbed out."""
    monkeypatch.setattr(ctx, "dry_run", False)
    monkeypatch.setattr(ctx.runner, "dry_run", False)
    return ctx


def stdio_candidate(install) -> Candidate:
    return Candidate(kind="stdio", name="vendor-mcp", command=["vendor-mcp"], install=install)


def test_a_prerequisite_whose_command_is_missing_is_never_run(live, monkeypatch) -> None:
    """Saying which command is absent beats a rc=127 the reader has to decode.

    With --mcp-provision-missing never, as every run was before it existed;
    what provisioning does instead is tests/test_mcp_provisioning.py's subject.
    """
    live.settings.set("mcp.packages.provision_missing", "never")
    ran: list[list[str]] = []
    monkeypatch.setattr(live.runner, "which", lambda name, *a, **k: None if name == "cargo" else "/usr/bin/" + name)
    monkeypatch.setattr(live.runner, "run", lambda argv, **k: ran.append(list(argv)))

    result = CandidateVerifier(live)._verify_stdio(stdio_candidate([["cargo", "install", "x"]]))

    assert "cargo" in result.error and "not installed" in result.error
    assert ran == []


def test_a_failed_prerequisite_reports_what_the_command_said(live, monkeypatch) -> None:
    """Otherwise the reason lives only in the debug log, and the report says
    'prerequisite failed' about a command that was never runnable."""
    monkeypatch.setattr(live.runner, "which", lambda name, *a, **k: "/usr/bin/" + name)
    monkeypatch.setattr(
        live.runner,
        "run",
        lambda argv, **k: CommandResult(
            argv=list(argv), returncode=1, stderr="sha256sum: against: No such file or directory"
        ),
    )

    result = CandidateVerifier(live)._verify_stdio(stdio_candidate([["sha256sum", "-c", "against", "checksums.txt"]]))

    assert "No such file or directory" in result.error


def test_a_prerequisite_may_be_what_puts_the_next_one_on_the_path(live, monkeypatch) -> None:
    """Resolution is per step, in order: 'npm install -g x' then 'x --version'."""
    present = {"npm", "vendor-mcp"}
    ran: list[list[str]] = []

    def which(name, *a, **k):
        return "/usr/bin/" + name if name in present else None

    def run(argv, **k):
        if argv[0] == "npm":
            present.add("vendor-tool")
        ran.append(list(argv))
        return CommandResult(argv=list(argv), returncode=0)

    monkeypatch.setattr(live.runner, "which", which)
    monkeypatch.setattr(live.runner, "run", run)
    monkeypatch.setattr(McpProber, "probe_stdio", lambda self, *a, **k: handshake())

    result = CandidateVerifier(live)._verify_stdio(
        stdio_candidate([["npm", "install", "-g", "vendor-tool"], ["vendor-tool", "--version"]])
    )

    assert [step[0] for step in ran] == ["npm", "vendor-tool"]
    assert result.ok


# -- an endpoint that answered only to ask who you are --------------------------
#
# A 401 or 403 is a positive finding: the endpoint exists, it speaks MCP, and it
# will not talk to an anonymous client. It is written out with its credential
# slot empty, and from there on it has to be visibly *not* a working server --
# in the audit, in Claude Code's check, in the tally and in the verdict.


def walled(**overrides) -> Candidate:
    base = dict(
        ok=False,
        status=401,
        requires_auth=True,
        auth_hint='Bearer realm="api"',
        auth_evidence=["it answered application/json, not a web page"],
    )
    return candidate(handshake=Handshake(**{**base, **overrides}))


def test_an_authenticated_endpoint_audits_as_not_asked_never_as_failed(ctx) -> None:
    """ "no tools" would be a finding about the server; nobody asked it."""
    checks = CandidateVerifier.audit(walled())

    assert [c.mark for c in checks] == ["-", "-", "-"]
    assert "will not talk to an anonymous client" in checks[0].detail
    assert 'Bearer realm="api"' in checks[0].detail


def test_claude_is_not_asked_about_an_entry_whose_credential_is_empty(ctx, monkeypatch) -> None:
    monkeypatch.setattr(type(ctx), "tool", lambda self, *a, **k: "/usr/bin/claude")
    check = McpWriter(ctx).claude_check(
        Registration(
            name="mcp-example-test",
            candidate=walled(),
            registered=True,
            provisional=True,
            needs_env=["MCP_EXAMPLE_TEST_TOKEN"],
        )
    )
    assert check.ok is None and check.mark == "-"
    assert "MCP_EXAMPLE_TEST_TOKEN" in check.detail


def test_an_empty_credential_is_never_counted_as_an_endpoint_that_passed(phase, ctx) -> None:
    outcome = TargetOutcome(target="https://mcp.example.test")
    registrations = [
        Registration(name="works", candidate=candidate(handshake=handshake())),
        Registration(
            name="needs-a-token",
            candidate=walled(),
            provisional=True,
            needs_env=["MCP_EXAMPLE_TEST_TOKEN"],
        ),
    ]
    result = PhaseResult(phase="mcp", title="MCP auto-discovery")

    phase._report_verification(result, McpWriter(ctx), outcome, registrations)

    step = result.steps[-1]
    assert step.detail.startswith("1/1 endpoint(s)"), "the walled one is not in the denominator"
    assert "1 endpoint(s) answered only to say a credential is required" in step.detail
    assert step.status == Status.PARTIAL
    assert outcome.verification[1]["provisional"] is True
    assert any("MCP_EXAMPLE_TEST_TOKEN" in line for line in step.remediation)


def test_the_verdict_spells_out_that_a_real_token_is_still_needed(phase, ctx, sandbox, monkeypatch) -> None:
    """The one thing the run could not do for the operator, said last and loudly."""
    monkeypatch.setattr(type(ctx), "tool", lambda self, *a, **k: None)
    ctx.dry_run = False
    writer = McpWriter(ctx)
    outcome = TargetOutcome(target="https://mcp.example.test", provisional=[walled()])
    writer.publish(outcome)

    result = PhaseResult(phase="mcp", title="MCP auto-discovery")
    phase._report_verdict(result, writer, [outcome])
    detail = result.steps[-1].detail

    assert "ACTION REQUIRED" in detail
    assert "MCP_EXAMPLE_TEST_TOKEN" in detail
    assert "EMPTY credential" in detail
    assert detail.endswith("PARTIAL"), "an endpoint found but unusable is neither pass nor fail"
    assert result.data["mcp_pending_environment"] == ["MCP_EXAMPLE_TEST_TOKEN"]
    assert "No API key, token or other credential is needed" not in detail


def test_the_briefing_puts_the_empty_credential_first_and_in_the_warning_colour(ctx) -> None:
    """The one thing the run could not do for the operator, at the top of the block."""
    ctx.console = Console(stream=io.StringIO(), color=False, quiet=False, glyph_set="ascii")
    writer = McpWriter(ctx)
    outcome = TargetOutcome(target="https://mcp.example.test", provisional=[walled()])
    outcome.registered = [r.name for r in writer.publish(outcome)]
    result = PhaseResult(phase="mcp", title="MCP auto-discovery")

    McpPhase(ctx)._report_configuration_file(result, writer, [outcome])
    output = ctx.console._out.getvalue()

    assert "ACTION REQUIRED -- registered with an EMPTY credential" in output
    # Not "<your real token>". That sentence was shown for every variable,
    # including ones that hold no token -- API_HEADERS wants `Name:value`
    # pairs, and an operator who followed the instruction exactly pasted a bare
    # token into it and the server exited. Nothing is known about this one's
    # shape, so the placeholder says that instead of inventing a kind.
    assert "export MCP_EXAMPLE_TEST_TOKEN=<value -- shape unknown" in output
    assert "NOT yet working" in output
    assert output.index("ACTION REQUIRED") < output.index("| Servers")
    waiting = result.data["mcp_briefing"]["these_need_a_real_credential_before_they_work"]
    assert list(waiting) == ["mcp-example-test"]
    assert any(line.startswith("MCP_EXAMPLE_TEST_TOKEN=") for line in waiting["mcp-example-test"])
