"""What a stage line says about *who* did the work.

Reading a real log, five stage lines said `[traditional]`. Every one of them was
a `SKIP`, and the run had used an agent for every stage it actually executed.
The tags come from :class:`Mode`, which answers "how was this stage carried
out":

    agentic      a Claude subagent drove it and its candidates were verified
    traditional  deterministic probing and rule-based reasoning, start to finish
    fallback     "agentic+traditional" -- agent first, deterministic after it
                 came back short

A skipped stage was not carried out at all, so none of the three is true of it.
It got `traditional` because `settled()` lives on `TraditionalPipeline` and
stamped `self.mode`, even when the agentic pipeline called it through
`fallback` -- so an agentic run's skips read as though the agent had given up.
"""

from __future__ import annotations

from initialize_claude_ai.core.report import Status
from initialize_claude_ai.mcp_auto_discovery.policy import DueDiligence
from initialize_claude_ai.mcp_auto_discovery.traditional import TraditionalPipeline
from initialize_claude_ai.mcp_auto_discovery.types import (
    Candidate,
    Handshake,
    Mode,
    Stage,
    StageOutcome,
    TargetOutcome,
    Transport,
)
from initialize_claude_ai.phases.phase4_mcp_auto_discovery import McpPhase, _never_ran

WORKS = Handshake(ok=True, tools=["a"], round_trip=True)


def answered(ctx, target: str = "https://x.example.test") -> TargetOutcome:
    """A target whose package stage found something, so lower tiers are settled."""
    outcome = TargetOutcome(target=target)
    outcome.due_diligence = DueDiligence(target=target)
    outcome.verified.append(
        Candidate(
            kind="url",
            name="x",
            url=f"{target}/mcp",
            transport=Transport.STREAMABLE_HTTP,
            handshake=WORKS,
        )
    )
    return outcome


def test_an_agentic_runs_skip_is_not_labelled_traditional(ctx) -> None:
    """The decision was the agentic pipeline's; the method it borrowed is not."""
    pipeline = TraditionalPipeline(ctx)
    skip = pipeline.settled(Stage.BRIDGE, answered(ctx), exhaustive=False, mode=Mode.AGENTIC)
    assert skip is not None
    assert skip.mode == Mode.AGENTIC


def test_and_a_deterministic_runs_skip_still_is(ctx) -> None:
    pipeline = TraditionalPipeline(ctx)
    skip = pipeline.settled(Stage.BRIDGE, answered(ctx), exhaustive=False)
    assert skip is not None
    assert skip.mode == Mode.TRADITIONAL


def test_a_stage_that_never_ran_is_recognised_as_such() -> None:
    skipped = StageOutcome(stage=Stage.BRIDGE, mode=Mode.AGENTIC, status=Status.SKIPPED)
    assert _never_ran(skipped)

    ran_and_found_nothing = StageOutcome(stage=Stage.BRIDGE, mode=Mode.AGENTIC, status=Status.SKIPPED)
    ran_and_found_nothing.duration = 12.5
    assert not _never_ran(ran_and_found_nothing), "it ran; a real result with a mode"


def test_the_tally_counts_work_rather_than_skips() -> None:
    """The one figure saying how much the agent did must not include work nobody did."""
    outcome = TargetOutcome(target="https://x.example.test")
    ran = StageOutcome(stage=Stage.ENDPOINT, mode=Mode.AGENTIC, status=Status.OK)
    ran.duration = 3.0
    fell_back = StageOutcome(stage=Stage.PACKAGE, mode=Mode.FALLBACK, status=Status.PARTIAL)
    fell_back.duration = 9.0
    outcome.stages = [
        ran,
        fell_back,
        StageOutcome(stage=Stage.BRIDGE, mode=Mode.AGENTIC, status=Status.SKIPPED),
        StageOutcome(stage=Stage.STRATEGY, mode=Mode.AGENTIC, status=Status.SKIPPED),
    ]

    tally = McpPhase._mode_tally([outcome])
    assert tally.get("agentic") == 1
    assert tally.get("fallback") == 1
    assert tally.get("skipped") == 2
    assert "traditional" not in tally


def test_the_per_address_mode_column_ignores_skips() -> None:
    outcome = TargetOutcome(target="https://x.example.test")
    ran = StageOutcome(stage=Stage.ENDPOINT, mode=Mode.AGENTIC, status=Status.OK)
    ran.duration = 3.0
    outcome.stages = [ran, StageOutcome(stage=Stage.BRIDGE, mode=Mode.AGENTIC, status=Status.SKIPPED)]
    assert McpPhase._modes_of(outcome) == "agentic"


def test_a_refusal_is_counted_because_it_costs_the_stage_outright(ctx) -> None:
    """The other failure classes mean the agent worked and came up short.

    A refusal means no agent worked at all -- and it was visible only by
    grepping the log for a stack trace.
    """
    from initialize_claude_ai.mcp_auto_discovery.runtime import OTHER, REFUSAL

    outcome = TargetOutcome(target="https://x.example.test")
    refused = StageOutcome(stage=Stage.ENDPOINT, mode=Mode.FALLBACK, status=Status.PARTIAL)
    refused.findings["agent_failure_kind"] = REFUSAL
    other = StageOutcome(stage=Stage.PACKAGE, mode=Mode.FALLBACK, status=Status.PARTIAL)
    other.findings["agent_failure_kind"] = OTHER
    outcome.stages = [refused, other]

    assert McpPhase._refusals([outcome]) == 1
