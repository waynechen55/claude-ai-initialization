"""Phase 4 is judged on the servers it was given, not on the probes it took.

``--mcp-server a,b,c`` asks three questions, and the answer to each of them is
"is there a working MCP server here". Getting there costs dozens of steps -- a
stage per strategy, a probe per path, a check per endpoint -- and counting those
instead turns a good run into a bad-looking one: a server found by the third
stage reads as two failures and a pass, and an address with a hundred probed
paths drowns out an address with three.

So the scoreboard, the phase status and the safety note are all per address:
one section per server, its own arithmetic inside it, and every step it took
still listed underneath the verdict it did not decide.
"""

from __future__ import annotations

import io
import re

import pytest

from initialize_claude_ai.core.logging_setup import Console
from initialize_claude_ai.core.report import PhaseResult, Safety, Status
from initialize_claude_ai.mcp_auto_discovery.agentic import AgenticPipeline
from initialize_claude_ai.mcp_auto_discovery.types import (
    Candidate,
    Handshake,
    Mode,
    Stage,
    StageOutcome,
    TargetOutcome,
    Transport,
)
from initialize_claude_ai.phases.phase4_mcp_auto_discovery import McpPhase

ALPHA = "https://alpha.test"
BETA = "https://beta.test"
UNPARSEABLE = "http://"


@pytest.fixture(autouse=True)
def no_real_claude(ctx, monkeypatch) -> None:
    """A Claude Code on the test machine must not be asked about these servers."""
    monkeypatch.setattr(type(ctx), "tool", lambda self, *a, **k: None)


def working(target: str) -> TargetOutcome:
    """A target whose second stage found something that answers."""
    found = Candidate(
        kind="url",
        name="alpha",
        url=f"{target}/mcp",
        transport=Transport.STREAMABLE_HTTP,
        handshake=Handshake(
            ok=True,
            transport=Transport.STREAMABLE_HTTP,
            url=f"{target}/mcp",
            server_name="Alpha",
            tools=["search"],
            round_trip=True,
            round_trip_method="tools/list",
        ),
    )
    return TargetOutcome(
        target=target,
        base_url=target,
        stages=[
            StageOutcome(
                stage=Stage.ENDPOINT,
                mode=Mode.TRADITIONAL,
                status=Status.FAILED,
                detail="nothing answered on 166 probed path(s)",
            ),
            StageOutcome(
                stage=Stage.PACKAGE,
                mode=Mode.TRADITIONAL,
                status=Status.OK,
                detail="alpha-mcp 1.2.0 over stdio",
                verified=[found],
            ),
        ],
        verified=[found],
    )


def empty(target: str) -> TargetOutcome:
    """A target where every stage ran and none of them found anything.

    The stages roll up to PARTIAL -- one of them got somewhere without getting
    to a server -- which is the right reading of the search and the wrong one
    of the address.
    """
    return TargetOutcome(
        target=target,
        base_url=target,
        stages=[
            StageOutcome(
                stage=Stage.ENDPOINT,
                mode=Mode.TRADITIONAL,
                status=Status.PARTIAL,
                detail="3 endpoint(s) answered but none completed a handshake",
            ),
            StageOutcome(
                stage=Stage.PACKAGE,
                mode=Mode.TRADITIONAL,
                status=Status.SKIPPED,
                detail="no published server targets this product",
            ),
        ],
    )


def run_phase(ctx, monkeypatch, servers: list[str]) -> tuple[McpPhase, PhaseResult, str]:
    """Discover the given addresses with the pipeline replaced by a fixture."""
    ctx.console = Console(stream=io.StringIO(), color=False, quiet=False, glyph_set="ascii")
    ctx.settings.set("mcp.address.resolve", False)
    ctx.settings.set("mcp.verification.final_recheck", False)
    ctx.state["mcp_servers"] = servers
    monkeypatch.setattr(
        AgenticPipeline,
        "run",
        lambda self, target, stages: working(target) if "alpha" in target else empty(target),
    )
    phase = McpPhase(ctx)
    result = PhaseResult(phase=phase.slug, title=phase.title, group_noun=phase.group_noun)
    phase.execute(result)
    result.finalize()
    return phase, result, ctx.console._out.getvalue()


# -- one subject per address ---------------------------------------------------


def test_every_address_gets_one_verdict_of_its_own(ctx, monkeypatch) -> None:
    _phase, result, _output = run_phase(ctx, monkeypatch, ["alpha.test", "beta.test"])

    assert [g.key for g in result.groups()] == [ALPHA, BETA]
    assert [g.status for g in result.groups()] == [Status.OK, Status.FAILED]
    assert result.group_tally() == {"failed": 1, "skipped": 0, "passed": 1}


def test_the_verdict_on_an_address_is_the_server_not_the_probes(ctx, monkeypatch) -> None:
    """The endpoint stage failed and the address still passed, because the
    question was "is there a working MCP server here", and there is."""
    _phase, result, _output = run_phase(ctx, monkeypatch, ["alpha.test"])

    alpha = result.groups()[0]
    assert alpha.status == Status.OK
    assert alpha.detail.startswith("working MCP server:")
    assert alpha.tally()["failed"] >= 1, "the stage that found nothing is still counted"
    assert result.status == Status.OK


def test_the_phase_fails_when_no_address_yielded_anything(ctx, monkeypatch) -> None:
    """However well the bookkeeping around the search went."""
    _phase, result, _output = run_phase(ctx, monkeypatch, ["beta.test"])

    assert result.status == Status.FAILED
    assert [s.status for s in result.ungrouped() if s.name == "discovery mode"] == [Status.OK]


def test_a_productive_search_that_found_nothing_is_still_a_failure(ctx) -> None:
    """ "Partially passed" beside "no working MCP server was found" is a
    contradiction: the stages can be partly useful and the answer still be no."""
    outcome = empty(BETA)
    assert outcome.status == Status.SKIPPED, "the stage roll-up has not run yet"
    McpPhase(ctx)._finalise(PhaseResult(phase="mcp"), None, outcome)
    assert outcome.status == Status.PARTIAL, "the search did get somewhere"
    assert McpPhase(ctx)._verdict_on(outcome) == Status.FAILED


def test_an_endpoint_waiting_on_a_credential_is_genuinely_partial(ctx) -> None:
    """It answered; it is real; it is not usable until a token is exported."""
    outcome = TargetOutcome(
        target=BETA,
        provisional=[Candidate(kind="url", url=f"{BETA}/mcp")],
        status=Status.PARTIAL,
    )
    assert McpPhase(ctx)._verdict_on(outcome) == Status.PARTIAL


def test_a_mixed_run_is_partial(ctx, monkeypatch) -> None:
    _phase, result, _output = run_phase(ctx, monkeypatch, ["alpha.test", "beta.test"])
    assert result.status == Status.PARTIAL


def test_every_per_address_step_is_filed_under_that_address(ctx, monkeypatch) -> None:
    _phase, result, _output = run_phase(ctx, monkeypatch, ["alpha.test", "beta.test"])

    for step in result.steps:
        if step.group:
            assert step.name.startswith(step.group) or step.name.startswith("address ")
        else:
            # Only steps about the run as a whole are left without a subject.
            assert ALPHA not in step.name and BETA not in step.name


def test_the_run_level_steps_belong_to_no_address(ctx, monkeypatch) -> None:
    """They report on the run, so counting them against a server would make an
    address look worse or better than the search of it actually went."""
    _phase, result, _output = run_phase(ctx, monkeypatch, ["alpha.test", "beta.test"])

    named = {s.name for s in result.ungrouped()}
    assert {"discovery mode", "discovery", "final verdict"} <= named


# -- an address that never parsed ----------------------------------------------


def test_an_address_that_cannot_be_parsed_is_still_one_of_the_servers(ctx, monkeypatch) -> None:
    """It has no discovery outcome, but it is still a server that was asked for
    and still one that produced nothing; dropping it would shrink the
    denominator below the number the operator typed."""
    phase, result, _output = run_phase(ctx, monkeypatch, ["alpha.test", UNPARSEABLE])

    assert [g.key for g in result.groups()] == [ALPHA, UNPARSEABLE]
    assert result.group_tally() == {"failed": 1, "skipped": 0, "passed": 1}
    assert phase._address_count(result, ctx.state["mcp_outcomes"]) == 2


def test_every_count_in_the_phase_quotes_the_same_denominator(ctx, monkeypatch) -> None:
    """Three counts are printed for one run -- the summary row, the verdict and
    the safety note -- and two of them used to be out of a smaller number."""
    phase, result, _output = run_phase(ctx, monkeypatch, ["alpha.test", "beta.test", UNPARSEABLE])

    summary = next(s for s in result.steps if s.name == "discovery")
    verdict = next(s for s in result.steps if s.name == "final verdict")
    _safety, note = phase.safety(result)

    assert "1/3 address(es)" in summary.detail
    assert "1/3 address(es)" in verdict.detail
    assert "of 3 server(s)" in note


def test_an_unusable_address_is_named_in_the_safety_note(ctx, monkeypatch) -> None:
    phase, result, _output = run_phase(ctx, monkeypatch, ["alpha.test", UNPARSEABLE])
    safety, note = phase.safety(result)
    assert safety == Safety.CAUTION
    assert UNPARSEABLE in note


# -- what the operator reads ---------------------------------------------------


def test_the_summary_box_is_a_section_per_server(ctx, monkeypatch) -> None:
    phase, result, _output = run_phase(ctx, monkeypatch, ["alpha.test", "beta.test"])
    phase.summarise(result)
    box = ctx.console._out.getvalue().split("Phase 4 summary")[-1]

    assert "Every server, and what happened to it:" in box
    assert "1 of 2 server(s) passed" in box
    assert "PHASE 4 RESULT: PARTIALLY PASSED" in box
    # And the reason the one that did not pass did not pass, under its own name.
    assert re.search(rf"{re.escape(BETA)}\s+-\s+FAILED", box)
    assert re.search(rf"{re.escape(ALPHA)}\s+-\s+PASSED", box)
    # The address line wraps inside the box; read it as one line of text.
    flat = re.sub(r"[\s|│]+", " ", box)
    assert "nothing registered" in flat and "no working MCP server was found" in flat


def test_a_failed_stage_under_a_passing_server_keeps_its_reason(ctx, monkeypatch) -> None:
    phase, result, _output = run_phase(ctx, monkeypatch, ["alpha.test"])
    phase.summarise(result)
    box = ctx.console._out.getvalue().split("Phase 4 summary")[-1]

    assert "nothing answered on 166 probed path(s)" in box
    assert "PHASE 4 RESULT: PASSED" in box


def test_the_run_table_counts_servers_too(ctx, monkeypatch) -> None:
    """The run table and the phase's own summary cannot disagree about how well
    the phase went: "1 server(s) passed" in both, never 4 in one and 1 in the
    other."""
    from initialize_claude_ai.cli import _run_summary

    _phase, result, _output = run_phase(ctx, monkeypatch, ["alpha.test", "beta.test"])
    ctx.report.add(result)
    ctx.console = Console(stream=io.StringIO(), color=False, quiet=False, glyph_set="ascii")
    _run_summary(ctx)
    table = ctx.console._out.getvalue()

    assert "1 server(s) passed, 0 skipped, 1 failed" in table


def test_the_report_carries_the_servers_for_a_machine_to_read(ctx, monkeypatch) -> None:
    _phase, result, _output = run_phase(ctx, monkeypatch, ["alpha.test", "beta.test"])
    payload = result.to_dict()

    assert payload["group_noun"] == "server"
    assert payload["group_tally"] == {"failed": 1, "skipped": 0, "passed": 1}
    assert [g["group"] for g in payload["groups"]] == [ALPHA, BETA]
