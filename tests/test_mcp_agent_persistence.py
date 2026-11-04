"""Agent mode as an autonomous agent, not as a preamble to the fallback.

Agentic discovery used to make exactly one attempt per stage and hand anything
short of a verified handshake to the deterministic pipeline. Most first answers
fail for a correctable reason -- a package name that does not exist, a transport
the server does not speak, a runtime that would not start -- so that design threw
away the agent's best property: that it can be told what went wrong and try
something else. These tests pin the persistence, and pin that it is *persistence*
rather than repetition: every retry has to differ from the attempt before it.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from initialize_claude_ai.core.report import Status
from initialize_claude_ai.mcp_auto_discovery.agentic import (
    FULL,
    MINIMAL,
    PLAIN,
    AgenticPipeline,
    _best_of,
    _correction,
    _RetryPlan,
    _summarise_list,
)
from initialize_claude_ai.mcp_auto_discovery.runtime import (
    OTHER,
    REFUSAL,
    TIMEOUT,
    TRANSIENT,
    AgentDefinitionSpec,
    CliRuntime,
    SdkRuntime,
    available_runtimes,
    failure_kind,
    select_runtime,
)
from initialize_claude_ai.mcp_auto_discovery.types import (
    Candidate,
    Handshake,
    Mode,
    Stage,
    StageOutcome,
    TargetOutcome,
)


def target() -> TargetOutcome:
    return TargetOutcome(target="https://example.test")


def empty(detail: str = "found nothing", **kwargs) -> StageOutcome:
    return StageOutcome(stage=Stage.ENDPOINT, mode=Mode.AGENTIC, detail=detail, **kwargs)


def proven() -> StageOutcome:
    return StageOutcome(
        stage=Stage.ENDPOINT,
        mode=Mode.AGENTIC,
        status=Status.OK,
        verified=[Candidate(kind="url", url="https://example.test/mcp")],
    )


def attempts(ctx, monkeypatch, replies, **settings):
    """Run the retry loop over a canned sequence of attempt outcomes.

    The plan is one mutable object threaded through the loop, so each attempt's
    view of it is snapshotted: asserting on the live object would only ever show
    the state the *last* attempt left behind.
    """
    for key, value in settings.items():
        ctx.settings.set(f"mcp.agent.{key}", value)
    pipeline = AgenticPipeline(ctx)
    seen: list[dict] = []

    def fake(name, tgt, **kwargs):
        plan = kwargs.get("plan")
        seen.append({**kwargs, "plan": replace(plan) if plan is not None else None})
        return replies[min(len(seen) - 1, len(replies) - 1)]

    monkeypatch.setattr(pipeline, "_agent_stage", fake)
    stage, reason = pipeline._agent_attempts(Stage.ENDPOINT, target())
    return pipeline, stage, reason, seen


# ----------------------------------------------------------------------------
# the agent keeps working
# ----------------------------------------------------------------------------


def test_one_disappointing_answer_is_not_the_end_of_the_stage(ctx, monkeypatch) -> None:
    _, _, _, seen = attempts(ctx, monkeypatch, [empty()], max_attempts=3)

    assert len(seen) == 3


def test_the_agent_stops_the_moment_it_proves_something(ctx, monkeypatch) -> None:
    """Retrying is for failure. A verified handshake ends the stage at once."""
    _, stage, reason, seen = attempts(ctx, monkeypatch, [empty(), proven(), empty()], max_attempts=5)

    assert len(seen) == 2
    assert (stage.verified, reason) == (stage.verified, "")
    assert stage.verified


def test_one_attempt_restores_the_old_single_shot_behaviour(ctx, monkeypatch) -> None:
    _, _, _, seen = attempts(ctx, monkeypatch, [empty()], max_attempts=1)

    assert len(seen) == 1


def test_a_nonsense_attempt_count_still_runs_the_agent_once(ctx, monkeypatch) -> None:
    """0 attempts would mean "agent mode that never runs an agent"."""
    _, stage, _, seen = attempts(ctx, monkeypatch, [empty()], max_attempts=0)

    assert len(seen) == 1
    assert stage is not None


def test_a_stage_no_agent_owns_is_not_retried(ctx, monkeypatch) -> None:
    """There is nothing to try differently when there is nothing to try."""
    pipeline = AgenticPipeline(ctx)
    calls: list[int] = []
    monkeypatch.setattr(pipeline, "_agent_stage", lambda *a, **k: calls.append(1) or None)

    stage, reason = pipeline._agent_attempts(Stage.BRIDGE, target())

    assert (stage, len(calls)) == (None, 1)
    assert reason == "no agent is configured for the bridge stage"


def test_the_budget_cap_still_stops_the_loop(ctx, monkeypatch) -> None:
    """Persistence is not a licence to spend without limit."""
    pipeline, stage, reason, seen = attempts(ctx, monkeypatch, [empty()], max_attempts=5, max_budget_usd=1.0)

    assert len(seen) >= 1
    # The stub never spends, so force the condition the loop is meant to notice.
    pipeline.spent_usd = 2.0
    _, reason = pipeline._agent_attempts(Stage.ENDPOINT, target())
    assert "budget" in reason


def test_how_hard_the_agent_tried_is_on_the_record(ctx, monkeypatch) -> None:
    _, stage, reason, _ = attempts(ctx, monkeypatch, [empty("nothing on the registry")], max_attempts=3)

    assert stage.findings["attempts"] == 3
    assert len(stage.findings["attempt_history"]) == 3
    assert "over 3 agent attempts" in reason


def test_retrying_cannot_lose_ground(ctx, monkeypatch) -> None:
    """A later empty attempt must not erase an earlier one that found candidates."""
    found = empty(candidates=[Candidate(kind="url", url="https://example.test/sse")])
    _, stage, _, _ = attempts(ctx, monkeypatch, [found, empty(), empty()], max_attempts=3)

    assert len(stage.candidates) == 1


@pytest.mark.parametrize(
    "first, second, wins",
    [
        (empty(), proven(), "second"),
        (proven(), empty(), "first"),
        (empty(candidates=[Candidate(kind="url", url="u")]), empty(), "first"),
        (empty(error="agent-failed"), empty(), "second"),
    ],
)
def test_the_furthest_attempt_is_the_one_kept(first, second, wins) -> None:
    assert _best_of(first, second) is (first if wins == "first" else second)


# ----------------------------------------------------------------------------
# every retry is different from the attempt before it
# ----------------------------------------------------------------------------


def test_the_next_attempt_is_told_what_went_wrong(ctx, monkeypatch) -> None:
    """Otherwise the second attempt is the first one again, at twice the price."""
    _, _, _, seen = attempts(ctx, monkeypatch, [empty("no package by that name")], max_attempts=2)

    assert seen[0]["plan"].correction == ""
    assert "no package by that name" in seen[1]["plan"].correction


def test_the_correction_names_the_candidate_that_did_not_answer() -> None:
    candidate = Candidate(kind="stdio", name="acme-mcp", command=["npx", "acme"])
    candidate.handshake = Handshake(error="prerequisite needs sha256sum, which is not installed")

    text = _correction(empty(candidates=[candidate]), attempt=1)

    assert "acme-mcp" in text
    assert "sha256sum" in text


def test_the_correction_forbids_sending_the_same_answer_back() -> None:
    text = _correction(empty(), attempt=1)

    assert "unchanged" in text
    assert "run out of leads" in text


def test_a_discarded_candidate_is_explained_to_the_next_attempt() -> None:
    """An agent that is never told its candidate was dropped proposes it again."""
    stage = empty()
    stage.findings["rejected_unsafe"] = ["a candidate carried a credential in its URL"]

    assert "carried a credential" in _correction(stage, attempt=1)


def test_the_correction_reaches_the_prompt(ctx) -> None:
    pipeline = AgenticPipeline(ctx)
    spec = AgentDefinitionSpec(name="scout", description="", prompt="")

    prompt = pipeline._task_prompt(
        Stage.ENDPOINT,
        spec,
        target(),
        Path("/tmp/ws"),
        Path("/tmp/ws/context.json"),
        _RetryPlan(correction="Attempt 1 did not settle this stage."),
    )

    assert "retried, not started over" in prompt
    assert "Attempt 1 did not settle this stage." in prompt


def test_a_first_attempt_carries_no_retry_language(ctx) -> None:
    pipeline = AgenticPipeline(ctx)
    spec = AgentDefinitionSpec(name="scout", description="", prompt="")

    prompt = pipeline._task_prompt(Stage.ENDPOINT, spec, target(), Path("/tmp/ws"), Path("/tmp/ws/context.json"))

    assert "retried, not started over" not in prompt
    assert "did not reach a model" not in prompt


# ----------------------------------------------------------------------------
# what sort of failure it was decides what changes
# ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "error, kind",
    [
        ("Opus 5's safeguards flagged this message", REFUSAL),
        ("I can't respond to this message", REFUSAL),
        ("the request was refused", REFUSAL),
        ("the agent exceeded mcp.agent.timeout_seconds (900s) and was stopped", TIMEOUT),
        ("the request timed out", TIMEOUT),
        ("upstream is overloaded", TRANSIENT),
        ("429 rate limit exceeded", TRANSIENT),
        ("HTTP 503 from the gateway", TRANSIENT),
        ("connection refused", TRANSIENT),
        ("ModuleNotFoundError: claude_agent_sdk", OTHER),
        ("", OTHER),
    ],
)
def test_a_failure_is_classified_before_it_is_retried(error, kind) -> None:
    assert failure_kind(error) == kind


def refusal(detail: str = "Opus 5's safeguards flagged this message") -> StageOutcome:
    return empty(detail, error="agent-failed")


def test_a_refusal_is_restated_rather_than_resent(ctx, monkeypatch) -> None:
    """The same words were rejected before the model read them; sending them
    again changes nothing at all."""
    _, _, _, seen = attempts(ctx, monkeypatch, [refusal()], max_attempts=2)

    assert seen[1]["plan"].refused is True


def test_a_refused_brief_gets_shorter_and_not_longer(ctx, monkeypatch) -> None:
    """The obvious retry -- append a paragraph explaining the work -- leaves
    every word that caused the rejection in place and adds more around it. The
    three attempts then end three identical ways, which is exactly what a log
    full of [fallback] looks like. Each refusal has to *subtract*."""
    _, _, _, seen = attempts(ctx, monkeypatch, [refusal()], max_attempts=3)

    assert [s["plan"].register for s in seen] == [FULL, PLAIN, MINIMAL]


def test_the_ladder_has_a_floor(ctx, monkeypatch) -> None:
    """A fourth refusal cannot wrap round to the full brief again."""
    _, _, _, seen = attempts(ctx, monkeypatch, [refusal()], max_attempts=5, stop_when_nothing_changes=False)

    assert [s["plan"].register for s in seen] == [FULL, PLAIN, MINIMAL, MINIMAL, MINIMAL]


def test_a_refusal_with_nothing_left_to_change_is_not_sent_again(ctx, monkeypatch) -> None:
    """At the floor, with no other model, another attempt is the same request refused the same way.

    gitlab.example.net's stage was refused three times on one model; the further
    attempts cost minutes and said nothing new.
    """
    monkeypatch.setattr(AgenticPipeline, "_rotate_model", lambda self: "")
    _, _, _, seen = attempts(ctx, monkeypatch, [refusal()], max_attempts=5)

    assert [s["plan"].register for s in seen] == [FULL, PLAIN, MINIMAL]


def test_an_operator_can_start_below_the_full_brief(ctx, monkeypatch) -> None:
    """On a deployment where the full brief is always rejected, paying for the
    attempt that discovers that every time is waste."""
    _, _, _, seen = attempts(ctx, monkeypatch, [refusal()], max_attempts=2, prompt_register="plain")

    assert [s["plan"].register for s in seen] == [PLAIN, MINIMAL]


def test_a_shorter_brief_leaves_the_earlier_findings_on_disk(ctx) -> None:
    """Shorter is not blinder: the findings are still reachable, they are just
    not spent in the prompt."""
    pipeline = AgenticPipeline(ctx)
    spec = AgentDefinitionSpec(name="scout", description="", prompt="")
    outcome = target()
    outcome.stages.append(empty(candidates=[Candidate(kind="url", url="https://example.test/sse")]))

    def prompt(register):
        return pipeline._task_prompt(
            Stage.ENDPOINT,
            spec,
            outcome,
            Path("/tmp/ws"),
            Path("/tmp/ws/context.json"),
            _RetryPlan(register=register),
        )

    assert "```json" in prompt(FULL)
    assert "```json" not in prompt(PLAIN)
    assert "/tmp/ws/context.json" in prompt(PLAIN)


def test_the_shortest_brief_drops_the_lecture_but_not_the_rules(ctx) -> None:
    """What a shorter brief may lose is method and context. What it may never
    lose is the rule that keeps a credential out of the answer."""
    pipeline = AgenticPipeline(ctx)

    full = pipeline._agent_instructions(Stage.ENDPOINT, "find it", "mcp-endpoint-discovery", FULL)
    least = pipeline._agent_instructions(Stage.ENDPOINT, "find it", "mcp-endpoint-discovery", MINIMAL)

    assert "mcp-endpoint-discovery" in full and "mcp-endpoint-discovery" not in least
    assert len(least) < len(full)
    for text in (full, least):
        assert "Never put a password" in text
        assert "initialize" in text


def test_the_mandatory_path_list_is_only_spelled_out_at_full_length(ctx) -> None:
    """The list is what the four-step record means, so the instruction survives
    every register -- but a brief that enumerates a dozen URLs is the first
    thing worth dropping when the last one was never read."""
    pipeline = AgenticPipeline(ctx)
    paths = pipeline.policy.mandatory_paths()
    assert len(paths) > 1, "the packaged policy should ship a path list for this to be about"
    # Not paths[0]: that is /mcp, which the checklist names at every register --
    # "not just /mcp" is the instruction, so finding it proves nothing.
    later = paths[-1]

    full = pipeline._tier_rules(Stage.ENDPOINT, FULL)
    plain = pipeline._tier_rules(Stage.ENDPOINT, PLAIN)

    assert later in full
    assert later not in plain
    assert "range of subpaths" in plain


def test_an_answer_that_merely_failed_to_verify_is_not_a_refusal(ctx, monkeypatch) -> None:
    """The agent ran and answered; nothing about the request needs restating."""
    _, _, _, seen = attempts(ctx, monkeypatch, [empty("none completed a handshake")], max_attempts=2)

    assert seen[1]["plan"].refused is False
    assert seen[1]["plan"].register == FULL


def test_running_out_of_clock_is_not_the_same_as_being_rate_limited(ctx, monkeypatch) -> None:
    """Nothing was overloaded and nothing said no: the brief was bigger than the
    time allowed. Sending it again buys the same wall-clock spend and the same
    ending, so the remedy is a smaller brief and a smaller turn budget."""
    timed_out = empty("the agent exceeded mcp.agent.timeout_seconds (900s) and was stopped", error="agent-failed")
    _, _, _, seen = attempts(ctx, monkeypatch, [timed_out], max_attempts=2)

    assert seen[1]["plan"].register == PLAIN
    assert seen[1]["plan"].turn_scale < 1.0
    assert seen[1]["plan"].refused is False


def test_a_narrowed_attempt_gets_a_smaller_turn_budget(ctx) -> None:
    pipeline = AgenticPipeline(ctx)
    budget = pipeline._agent_for(Stage.ENDPOINT).turn_budget(0)
    assert budget, "the packaged endpoint agent should carry a turn budget"

    plan = _RetryPlan()
    pipeline._change_approach(empty("the agent exceeded mcp.agent.timeout_seconds (900s)", error="agent-failed"), plan)

    assert 0 < int(budget * plan.turn_scale) < budget


def test_a_broken_runtime_is_swapped_not_re_run(ctx, monkeypatch) -> None:
    """A defect in one runtime is not a defect in agent mode."""
    pipeline = AgenticPipeline(ctx)
    pipeline._runtimes = [SdkRuntime(ctx), CliRuntime(ctx)]
    pipeline.runtime = pipeline._runtimes[0]
    pipeline._runtimes_tried = {"sdk"}

    changing = pipeline._change_approach(
        empty("ModuleNotFoundError: claude_agent_sdk", error="agent-failed"), _RetryPlan()
    )

    assert pipeline.runtime.name == "cli"
    assert "cli runtime" in changing


def test_the_last_runtime_is_not_rotated_away_from(ctx) -> None:
    pipeline = AgenticPipeline(ctx)
    pipeline._runtimes = [SdkRuntime(ctx)]
    pipeline.runtime = pipeline._runtimes[0]
    pipeline._runtimes_tried = {"sdk"}

    assert pipeline._rotate_runtime() == ""
    assert pipeline.runtime.name == "sdk"


def test_a_transient_failure_is_simply_tried_again(ctx) -> None:
    pipeline = AgenticPipeline(ctx)
    before = pipeline.runtime.name if pipeline.runtime else ""

    changing = pipeline._change_approach(empty("upstream is overloaded", error="agent-failed"), _RetryPlan())

    assert "same request" in changing
    assert (pipeline.runtime.name if pipeline.runtime else "") == before


def test_no_model_is_named_in_the_program(ctx) -> None:
    """A model name in the defaults is a hardcoded model name. The list is the
    operator's to fill in, or it is derived from what this run resolved."""
    assert ctx.settings.str_list("mcp.agent.retry_models") == []
    assert AgenticPipeline(ctx)._rotate_model() == ""


def test_the_rotation_falls_back_to_the_models_this_run_resolved(ctx, monkeypatch) -> None:
    """ "Change your model" is the remedy the rejection itself names, and on a
    host where nobody filled in retry_models it used to be unavailable. Phase 3
    already resolved a real model id per tier against the live backend; those
    are the names to rotate through, and they are facts about this deployment
    rather than constants in the source."""
    pipeline = AgenticPipeline(ctx)
    [template, *_] = pipeline._model_tier_templates()
    assert "{TIER}" in template
    monkeypatch.setitem(ctx.settings.environ, template.replace("{TIER}", "SONNET"), "resolved-sonnet-id")

    assert pipeline._rotate_model() == "resolved-sonnet-id"
    assert [r.model_override for r in pipeline._runtimes] == ["resolved-sonnet-id"] * len(pipeline._runtimes)


def test_the_operators_own_list_beats_the_derived_one(ctx, monkeypatch) -> None:
    [template, *_] = AgenticPipeline(ctx)._model_tier_templates()
    monkeypatch.setitem(ctx.settings.environ, template.replace("{TIER}", "SONNET"), "derived")
    ctx.settings.set("mcp.agent.retry_models", "chosen")

    assert AgenticPipeline(ctx)._rotate_model() == "chosen"


def test_nothing_resolved_means_nothing_to_rotate_to(ctx) -> None:
    ctx.settings.set("mcp.agent.retry_model_tiers", [])

    assert AgenticPipeline(ctx)._rotate_model() == ""


def test_a_configured_alternative_model_is_tried_once(ctx) -> None:
    ctx.settings.set("mcp.agent.retry_models", "model-a,model-b")
    pipeline = AgenticPipeline(ctx)
    pipeline._runtimes = [SdkRuntime(ctx), CliRuntime(ctx)]
    pipeline.runtime = pipeline._runtimes[0]

    assert pipeline._rotate_model() == "model-a"
    assert [r.model_override for r in pipeline._runtimes] == ["model-a", "model-a"]
    assert pipeline._rotate_model() == "model-b"
    assert pipeline._rotate_model() == ""


def test_the_override_is_what_the_runtime_actually_uses(ctx) -> None:
    ctx.settings.set("mcp.agent.model", "configured-model")
    runtime = SdkRuntime(ctx)

    assert runtime.model() == "configured-model"
    runtime.model_override = "other-model"
    assert runtime.model() == "other-model"


# ----------------------------------------------------------------------------
# the fallback is a last resort, and says so
# ----------------------------------------------------------------------------


def test_the_deterministic_pipeline_runs_only_once_the_agent_is_spent(ctx, monkeypatch) -> None:
    ctx.settings.set("mcp.agent.max_attempts", 3)
    pipeline = AgenticPipeline(ctx)
    tries: list[int] = []
    delegated: list[int] = []
    monkeypatch.setattr(pipeline, "_agent_stage", lambda *a, **k: tries.append(1) or empty())
    monkeypatch.setattr(
        pipeline,
        "_delegate",
        lambda name, tgt: delegated.append(1) or StageOutcome(stage=name, mode=Mode.TRADITIONAL, status=Status.OK),
    )

    stage = pipeline._run_stage(Stage.ENDPOINT, target(), agentic=True, unavailable="")

    assert (len(tries), len(delegated)) == (3, 1)
    assert stage.mode == Mode.FALLBACK


def fell_back(ctx, monkeypatch, reply) -> StageOutcome:
    pipeline = AgenticPipeline(ctx)
    monkeypatch.setattr(pipeline, "_agent_stage", lambda *a, **k: reply)
    monkeypatch.setattr(
        pipeline,
        "_delegate",
        lambda name, tgt: StageOutcome(stage=name, mode=Mode.TRADITIONAL, status=Status.OK),
    )
    return pipeline._run_stage(Stage.ENDPOINT, target(), agentic=True, unavailable="")


def test_the_fallback_note_says_the_agent_was_exhausted_not_skipped(ctx, monkeypatch) -> None:
    stage = fell_back(ctx, monkeypatch, empty())

    advice = " ".join(stage.remediation)
    assert "did not settle this stage" in advice
    assert "max_attempts" in advice


@pytest.mark.parametrize(
    "detail, cause, advice",
    [
        ("Opus 5's safeguards flagged this message", "rejected before a model read it", "retry_models"),
        ("the agent exceeded mcp.agent.timeout_seconds (900s)", "ran out of wall clock", "timeout_seconds"),
        ("upstream is overloaded", "service was unavailable", "running again"),
    ],
)
def test_a_fallback_says_which_of_the_four_things_went_wrong(ctx, monkeypatch, detail, cause, advice) -> None:
    """ "[fallback]" on its own tells an operator that the agent did not finish
    and nothing about why, which is the difference between a diagnosis and a
    shrug. Each failure class has a different thing to do about it, so each one
    has to reach the page."""
    stage = fell_back(ctx, monkeypatch, empty(detail, error="agent-failed"))

    assert cause in stage.findings["agentic_inconclusive"]
    assert advice in " ".join(stage.remediation)


def test_the_register_the_agent_reached_is_on_the_record(ctx, monkeypatch) -> None:
    """How far down the ladder a stage got is the first thing worth knowing when
    the same stage keeps falling back."""
    stage = fell_back(ctx, monkeypatch, empty("safeguards flagged this message", error="agent-failed"))

    assert stage.findings["prompt_register"] == MINIMAL
    assert stage.findings["retry_changes"]


def test_turning_the_fallback_off_leaves_the_agents_verdict_standing(ctx, monkeypatch) -> None:
    ctx.settings.set("mcp.agent.fallback_to_traditional", False)
    pipeline = AgenticPipeline(ctx)
    monkeypatch.setattr(pipeline, "_agent_stage", lambda *a, **k: empty())
    monkeypatch.setattr(pipeline, "_delegate", lambda *a: pytest.fail("fell back anyway"))

    stage = pipeline._run_stage(Stage.ENDPOINT, target(), agentic=True, unavailable="")

    assert stage.mode == Mode.AGENTIC


# ----------------------------------------------------------------------------
# what earlier stages found, cut down to what a brief can carry
# ----------------------------------------------------------------------------


def test_a_short_list_travels_whole() -> None:
    assert _summarise_list(["a", "b"], 8) == ["a", "b"]


def test_a_long_list_becomes_a_count_and_a_few_examples() -> None:
    """One endpoint stage records a hundred and sixty URLs it asked about. That
    belongs in the report, and pasting it into the next stage's brief spends the
    context window on data no reader will act on -- in the shape most likely to
    be mistaken for something it is not."""
    summary = _summarise_list([f"https://example.test/p{n}" for n in range(160)], 3)

    assert summary["count"] == 160
    assert len(summary["examples"]) == 3
    assert "157 more" in summary["note"]


def test_a_string_is_not_a_list_of_characters() -> None:
    assert _summarise_list("https://example.test/mcp", 3) == "https://example.test/mcp"


def test_the_brief_carries_the_summary_and_not_the_whole_list(ctx) -> None:
    pipeline = AgenticPipeline(ctx)
    outcome = target()
    stage = empty()
    stage.findings["paths_probed"] = [f"https://example.test/p{n}" for n in range(160)]
    outcome.stages.append(stage)

    findings = pipeline._prompt_findings(outcome)

    assert findings["paths_probed"]["count"] == 160
    assert "p159" not in str(findings)


# ----------------------------------------------------------------------------
# knowing what else could have run
# ----------------------------------------------------------------------------


def test_selecting_a_runtime_is_still_taking_the_first_available_one(ctx) -> None:
    runtimes, reason = available_runtimes(ctx)
    chosen, chosen_reason = select_runtime(ctx)

    assert reason == chosen_reason
    assert (chosen.name if chosen else None) == (runtimes[0].name if runtimes else None)


def test_an_explicit_preference_offers_no_alternative(ctx) -> None:
    """`runtime = "cli"` is an instruction, not a starting point."""
    ctx.settings.set("mcp.agent.runtime", "cli")

    names = [r.name for r in available_runtimes(ctx)[0]]

    assert names in ([], ["cli"])
