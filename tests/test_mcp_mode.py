"""How phase 4 chooses a mode, and how it says which one it used.

Discovery starts as a Claude subagent and each stage falls back to deterministic
probing on its own, so the mode is normally a *result* rather than an input: the
only way an operator can know whether a "nothing found" came from an agent that
looked or from a fallback that could not is if the run says so -- on the stage
line, in the table, and in the summary.

The one input is ``--no-agent``, which is a decision the fallback cannot make on
anyone's behalf: not "an agent could not run here" but "no agent is to be run".
It takes the deterministic pipeline directly, and it is announced in the banner
before the phase goes quiet rather than inferred from the tags afterwards.

These tests pin both halves: which pipeline runs for each answer, and that the
mode always reaches the terminal and the report.
"""

from __future__ import annotations

import io

import pytest

from initialize_claude_ai import cli
from initialize_claude_ai.core.logging_setup import Console
from initialize_claude_ai.core.report import PhaseResult, Status
from initialize_claude_ai.mcp_auto_discovery import agentic as agentic_module
from initialize_claude_ai.mcp_auto_discovery.agentic import AgenticPipeline
from initialize_claude_ai.mcp_auto_discovery.traditional import TraditionalPipeline
from initialize_claude_ai.mcp_auto_discovery.types import (
    Candidate,
    Mode,
    Stage,
    StageOutcome,
    TargetOutcome,
    mode_tag,
)
from initialize_claude_ai.phases.phase4_mcp_auto_discovery import McpPhase


def outcome(target: str, *modes: str, verified: bool = False) -> TargetOutcome:
    """A finished target whose stages ran in the given modes."""
    stages = [StageOutcome(stage=Stage.ENDPOINT, mode=mode, status=Status.OK, detail=f"ran {mode}") for mode in modes]
    found = [Candidate(kind="url", name="srv", url=f"{target}/mcp")] if verified else []
    return TargetOutcome(target=target, base_url=target, stages=stages, verified=found)


# ----------------------------------------------------------------------------
# the mode is never chosen in advance
# ----------------------------------------------------------------------------


def test_the_pipeline_is_agentic_even_when_no_agent_can_run(ctx) -> None:
    """The fallback lives *inside* the agentic pipeline, so it is always the one
    that runs; swapping in the deterministic pipeline would lose the retry."""
    pipeline, mode, detail = McpPhase(ctx)._pipeline()
    assert isinstance(pipeline, AgenticPipeline)
    # A dry-run context: agents are not invoked, so every stage will fall back.
    assert mode == Mode.TRADITIONAL
    assert "dry run" in detail and "deterministic" in detail


def test_the_expected_mode_is_agentic_when_agents_can_run(ctx, monkeypatch) -> None:
    monkeypatch.setattr(AgenticPipeline, "available", lambda self: (True, ""))
    pipeline, mode, detail = McpPhase(ctx)._pipeline()
    assert isinstance(pipeline, AgenticPipeline)
    assert mode == Mode.AGENTIC
    assert "Claude subagent" in detail


def test_the_run_opens_by_saying_which_mode_it_expects(ctx, monkeypatch) -> None:
    """Before a single stage line appears, the operator is told how it will go."""
    monkeypatch.setattr(AgenticPipeline, "available", lambda self: (True, ""))
    monkeypatch.setattr(
        AgenticPipeline,
        "run",
        lambda self, target, stages: outcome(target, Mode.AGENTIC),
    )
    ctx.settings.set("mcp.address.resolve", False)
    ctx.state["mcp_servers"] = ["example.test"]
    phase = McpPhase(ctx)
    result = PhaseResult(phase=phase.slug, title=phase.title)
    phase.execute(result)

    opening = [s for s in result.steps if s.name == "discovery mode"]
    assert len(opening) == 1
    assert opening[0].data["mode"] == Mode.AGENTIC
    # And it comes before anything it describes.
    assert result.steps.index(opening[0]) < result.steps.index(next(s for s in result.steps if ":: endpoint" in s.name))


def test_the_banner_promises_a_persistent_agent_and_a_last_resort(ctx) -> None:
    """Both halves matter before the phase goes quiet: the stages are the
    agent's to settle, and something deterministic finishes one it cannot."""
    facts = dict(McpPhase(ctx).banner_facts())
    assert facts["Discovery"].startswith("agentic")
    assert "retried up to 3 time(s)" in facts["Discovery"]
    assert "deterministic probing finishes" in facts["Discovery"]


def test_there_is_no_mode_flag_left_to_pass(ctx) -> None:
    from initialize_claude_ai.cli import build_parser

    with pytest.raises(SystemExit):
        build_parser().parse_args(["run", "--phases", "4", "--mcp-mode", "agentic"])


# ----------------------------------------------------------------------------
# labelling
# ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "mode, label",
    [
        (Mode.AGENTIC, "agentic"),
        (Mode.TRADITIONAL, "traditional"),
        (Mode.FALLBACK, "fallback"),
    ],
)
def test_every_mode_has_its_own_word(mode: str, label: str) -> None:
    text, styles = mode_tag(mode)
    assert text == label and styles


def test_the_three_modes_do_not_share_a_colour() -> None:
    """The colour is the whole point: it has to be distinguishable at a glance."""
    colours = {mode_tag(m)[1] for m in (Mode.AGENTIC, Mode.TRADITIONAL, Mode.FALLBACK)}
    assert len(colours) == 3


def test_an_unknown_mode_is_still_shown_rather_than_hidden() -> None:
    assert mode_tag("something-new") == ("something-new", ("dim",))
    assert mode_tag("") == ("unknown", ("dim",))


def test_the_tag_is_written_in_its_own_column_and_colour() -> None:
    buffer = io.StringIO()
    console = Console(stream=buffer, color=True, palette="basic", decorate=False)
    console.status(Status.OK, "example.test :: endpoint", "1 candidate", tag="fallback", tag_styles=("yellow",))
    line = buffer.getvalue()
    assert "[fallback]" in line
    # Coloured separately from the verdict marker and from the detail.
    assert console.codes["yellow"] + "[fallback]" in line


def test_a_step_without_a_tag_gains_no_empty_brackets() -> None:
    buffer = io.StringIO()
    console = Console(stream=buffer, color=False, decorate=False)
    console.status(Status.OK, "install", "done")
    assert "[]" not in buffer.getvalue()


# ----------------------------------------------------------------------------
# falling back, and admitting it
# ----------------------------------------------------------------------------


def test_a_stage_the_agent_could_not_finish_is_labelled_a_fallback(ctx, monkeypatch) -> None:
    """Neither "agentic" nor "traditional" would be true of a merged stage."""
    pipeline = AgenticPipeline(ctx)
    inconclusive = StageOutcome(stage=Stage.ENDPOINT, mode=Mode.AGENTIC, detail="found nothing")
    monkeypatch.setattr(pipeline, "_agent_stage", lambda name, target, **_: inconclusive)
    monkeypatch.setattr(
        pipeline,
        "_delegate",
        lambda name, target: StageOutcome(stage=name, mode=Mode.TRADITIONAL, status=Status.OK, detail="probed"),
    )
    stage = pipeline._run_stage(
        Stage.ENDPOINT, TargetOutcome(target="https://example.test"), agentic=True, unavailable=""
    )
    assert stage.mode == Mode.FALLBACK
    assert stage.findings["agentic_inconclusive"].startswith("found nothing")


def test_a_stage_the_agent_finished_keeps_its_own_mode(ctx, monkeypatch) -> None:
    pipeline = AgenticPipeline(ctx)
    proven = StageOutcome(
        stage=Stage.ENDPOINT,
        mode=Mode.AGENTIC,
        status=Status.OK,
        verified=[Candidate(kind="url", url="https://example.test/mcp")],
    )
    monkeypatch.setattr(pipeline, "_agent_stage", lambda name, target, **_: proven)
    stage = pipeline._run_stage(
        Stage.ENDPOINT, TargetOutcome(target="https://example.test"), agentic=True, unavailable=""
    )
    assert stage.mode == Mode.AGENTIC


def test_a_reason_that_is_true_of_every_stage_is_not_repeated_per_stage(ctx, monkeypatch) -> None:
    """The phase says "agents cannot run here" once, in its opening step, and the
    [traditional] tag repeats it on every line; a copy under "Next steps" for
    each of the four stages would bury the advice that is actually per stage."""
    pipeline = AgenticPipeline(ctx)
    monkeypatch.setattr(
        pipeline,
        "_delegate",
        lambda name, target: StageOutcome(stage=name, mode=Mode.TRADITIONAL, status=Status.OK),
    )
    stage = pipeline._run_stage(
        Stage.ENDPOINT,
        TargetOutcome(target="https://example.test"),
        agentic=False,
        unavailable="no runtime",
    )
    assert stage.mode == Mode.TRADITIONAL
    assert stage.remediation == []
    # Dropped from the advice, never from the record.
    assert stage.findings["agentic_skipped"] == "no runtime"


def test_a_stage_specific_reason_does_get_a_next_step(ctx, monkeypatch) -> None:
    pipeline = AgenticPipeline(ctx)
    monkeypatch.setattr(pipeline, "_agent_stage", lambda name, target, **_: None)
    monkeypatch.setattr(
        pipeline,
        "_delegate",
        lambda name, target: StageOutcome(stage=name, mode=Mode.TRADITIONAL, status=Status.OK),
    )
    stage = pipeline._run_stage(
        Stage.BRIDGE, TargetOutcome(target="https://example.test"), agentic=True, unavailable=""
    )
    assert stage.remediation == ["Ran the bridge stage deterministically: no agent is configured for the bridge stage"]


# ----------------------------------------------------------------------------
# what survives to the summary
# ----------------------------------------------------------------------------


def test_each_address_reports_the_modes_its_stages_ran_in(ctx) -> None:
    ran = McpPhase(ctx)._modes_of(outcome("https://a.test", Mode.AGENTIC, Mode.FALLBACK))
    assert ran == "agentic, fallback"


def test_an_address_whose_stages_never_ran_says_so_rather_than_lying(ctx) -> None:
    assert McpPhase(ctx)._modes_of(TargetOutcome(target="https://a.test")) == "-"


def test_the_summary_counts_stages_by_mode(ctx) -> None:
    tally = McpPhase(ctx)._mode_tally(
        [
            outcome("https://a.test", Mode.AGENTIC, Mode.AGENTIC),
            outcome("https://b.test", Mode.FALLBACK),
        ]
    )
    assert tally == {"agentic": 2, "fallback": 1}


def test_the_closing_line_repeats_the_tally_once_the_stages_have_scrolled(ctx) -> None:
    phase = McpPhase(ctx)
    result = PhaseResult(phase=phase.slug, title=phase.title)
    phase._report_summary(
        result,
        [
            outcome("https://a.test", Mode.AGENTIC, verified=True),
            outcome("https://b.test", Mode.TRADITIONAL),
        ],
    )
    summary = result.steps[-1]
    assert "stages ran:" in summary.detail
    assert "1 agentic" in summary.detail and "1 traditional" in summary.detail
    assert result.data["modes"] == {"agentic": 1, "traditional": 1}


# ----------------------------------------------------------------------------
# --no-agent: the one way to choose in advance
# ----------------------------------------------------------------------------
#
# The fallback decides between the two modes whenever the *circumstances* rule
# an agent out. It cannot decide that an agent must not be started at all --
# a policy about money, repeatability or what is allowed to run on this host --
# so that is a flag, and these pin that it is a real one rather than a hint.


def test_no_agent_sets_the_setting_it_names(ctx) -> None:
    args = cli.build_parser().parse_args(["run", "--phases", "4", "--no-agent"])
    assert "mcp.agentic=false" in cli._overrides_from_args(args)


def test_nothing_is_turned_off_when_the_flag_is_absent(ctx) -> None:
    args = cli.build_parser().parse_args(["run", "--phases", "4"])
    assert not any(o.startswith("mcp.agentic") for o in cli._overrides_from_args(args))
    assert McpPhase(ctx)._agentic_wanted() is True


def test_no_agent_runs_the_deterministic_pipeline_itself(ctx) -> None:
    """Not the agentic pipeline with agents disabled: the deterministic one. The
    difference is that nothing is prepared for a turn that will never happen."""
    ctx.settings.set("mcp.agentic", False)
    pipeline, mode, detail = McpPhase(ctx)._pipeline()
    assert isinstance(pipeline, TraditionalPipeline)
    assert not isinstance(pipeline, AgenticPipeline)
    assert mode == Mode.TRADITIONAL
    assert "--no-agent" in detail


def test_no_runtime_is_even_looked_for(ctx, monkeypatch) -> None:
    """Finding a Claude runtime means asking the filesystem and the environment
    about one. A run that has said no agent should not pay for the question."""
    monkeypatch.setattr(agentic_module, "available_runtimes", lambda ctx: pytest.fail("looked for a runtime"))
    ctx.settings.set("mcp.agentic", False)
    McpPhase(ctx)._pipeline()


def test_an_agentic_pipeline_built_anyway_still_refuses(ctx, monkeypatch) -> None:
    """The phase is not the only caller. Whoever builds one gets the same answer,
    and the operator's decision is reported ahead of the circumstantial ones."""
    monkeypatch.setattr(AgenticPipeline, "runtime", object(), raising=False)
    ctx.settings.set("mcp.agentic", False)
    usable, why = AgenticPipeline(ctx).available()
    assert usable is False
    assert "--no-agent" in why


def test_the_mode_is_announced_in_the_banner_before_anything_runs(ctx) -> None:
    """The phase's longest silence comes before its first verdict line, so the
    mode has to be readable in the banner rather than only afterwards."""
    assert "agentic" in dict(McpPhase(ctx).banner_facts())["Discovery"]

    ctx.settings.set("mcp.agentic", False)
    fact = dict(McpPhase(ctx).banner_facts())["Discovery"]
    assert fact.startswith("traditional (--no-agent)")
    assert "no Claude subagent is started" in fact


def test_the_run_says_it_is_traditional_and_no_agent_is_ever_asked(ctx, monkeypatch) -> None:
    """The whole of it, in one run: the opening step says the mode, the report
    records it, and no agent was consulted to arrive at it."""
    monkeypatch.setattr(AgenticPipeline, "run", lambda self, target, stages: pytest.fail("an agent ran"))
    monkeypatch.setattr(
        TraditionalPipeline,
        "run",
        lambda self, target, stages: outcome(target, Mode.TRADITIONAL),
    )
    ctx.settings.set("mcp.agentic", False)
    ctx.settings.set("mcp.address.resolve", False)
    ctx.state["mcp_servers"] = ["example.test"]
    phase = McpPhase(ctx)
    result = PhaseResult(phase=phase.slug, title=phase.title)
    phase.execute(result)

    opening = next(s for s in result.steps if s.name == "discovery mode")
    assert opening.data["mode"] == Mode.TRADITIONAL
    assert "--no-agent" in opening.detail
    assert all(s.data.get("mode", Mode.TRADITIONAL) == Mode.TRADITIONAL for s in result.steps)
