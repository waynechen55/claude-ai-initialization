"""An endpoint closes the lower tiers only if it could be registered for the target.

confluence.example.com: the registry's name search for "confluence" returned
"0DTE Confluence Pro" at mcp.0dteconfluence.com -- an options-trading server.
It handshaked with 37 tools, was recorded as the target's native endpoint, and
the tier policy skipped the package, bridge and strategy stages. The writer then
refused it for naming nothing of the target, and the address ended with nothing
while the report said "native endpoint found".

These tests hold the generalised fix, for every route an endpoint can reach the
record by: the probe, an agent's candidate, an agent's checklist, a merge.
"""

from __future__ import annotations

import pytest

from initialize_claude_ai.core.report import Status
from initialize_claude_ai.mcp_auto_discovery.agentic import AgenticPipeline
from initialize_claude_ai.mcp_auto_discovery.policy import DueDiligence
from initialize_claude_ai.mcp_auto_discovery.runtime import AgentResult
from initialize_claude_ai.mcp_auto_discovery.traditional import TraditionalPipeline
from initialize_claude_ai.mcp_auto_discovery.types import (
    Candidate,
    Handshake,
    Stage,
    StageOutcome,
    TargetOutcome,
    Transport,
)

TARGET = "https://confluence.example.com"
FOREIGN = "https://mcp.0dteconfluence.com/mcp"
OWN = "https://confluence.example.com/mcp"

#: A server that answered and says nothing about the target.
UNRELATED = Handshake(ok=True, tools=["option_chain"], round_trip=True)
#: A server that answered and names the target in what it advertises.
RELATED = Handshake(
    ok=True,
    tools=["page"],
    round_trip=True,
    advertised=["resource https://confluence.example.com/display/SPACE"],
)


@pytest.fixture(autouse=True)
def _no_checklist_gaps(ctx):
    # The checklist's own completeness rule is not what these tests are about.
    ctx.settings.set("mcp.policy.require_checklist", False)


def url_candidate(url: str, handshake: Handshake | None = None) -> Candidate:
    return Candidate(
        kind="url", name="x", url=url, transport=Transport.STREAMABLE_HTTP, origin=Stage.ENDPOINT, handshake=handshake
    )


def answering(verdicts: dict[str, Handshake]):
    """A verify_all stand-in: each URL answers with its verdict, or not at all."""

    def verify_all(candidates, **kw):
        for candidate in candidates:
            candidate.handshake = verdicts.get(candidate.url, Handshake(ok=False, error="connection refused"))
        return [c for c in candidates if c.verified]

    return verify_all


# --------------------------------------------------------------------------- #
# the record
# --------------------------------------------------------------------------- #


def test_a_ruled_out_endpoint_is_removed_everywhere_and_cannot_come_back() -> None:
    record = DueDiligence(target=TARGET)
    record.note("native_endpoints", [FOREIGN])
    record.note("auth_walls", [FOREIGN])

    assert record.retract(FOREIGN, "names nothing of the target") is True
    assert record.native_endpoints == [] and record.auth_walls == []
    assert not record.native_exists
    assert record.note("native_endpoints", [FOREIGN]) == []
    assert record.native_endpoints == []
    assert record.retract(FOREIGN, "again") is False
    assert record.ruled_out[FOREIGN] == "names nothing of the target", "first reason kept"


def test_a_merge_carries_the_retraction_and_never_re_adds() -> None:
    fallback = DueDiligence(target=TARGET)
    fallback.retract(FOREIGN, "names nothing of the target")
    agent = DueDiligence(target=TARGET)
    agent.note("native_endpoints", [FOREIGN, OWN])

    agent.merge(fallback)
    assert agent.native_endpoints == [OWN]
    assert FOREIGN in agent.ruled_out

    again = DueDiligence(target=TARGET)
    again.retract(FOREIGN, "names nothing")
    claim = DueDiligence(target=TARGET)
    claim.note("native_endpoints", [FOREIGN])
    again.merge(claim)
    assert again.native_endpoints == []


def test_the_summary_and_report_say_what_was_ruled_out() -> None:
    record = DueDiligence(target=TARGET)
    record.retract(FOREIGN, "names nothing of the target")

    assert "native endpoint found" not in record.summary()
    assert "1 ruled out" in record.summary()
    assert FOREIGN in record.summary()
    assert record.to_dict()["ruled_out"] == [{"url": FOREIGN, "reason": "names nothing of the target"}]


# --------------------------------------------------------------------------- #
# traditional: what may be recorded, and what is vetted
# --------------------------------------------------------------------------- #


def test_an_endpoint_that_cannot_be_registered_is_not_recorded(ctx) -> None:
    pipeline = TraditionalPipeline(ctx)
    outcome = TargetOutcome(target=TARGET)

    urls = pipeline.admissible([url_candidate(FOREIGN, UNRELATED)], outcome)

    assert urls == []
    record = outcome.due_diligence
    assert record.native_endpoints == [] and FOREIGN in record.ruled_out
    assert pipeline.gate(Stage.PACKAGE, outcome) is None, "the package stage may run"


def test_the_targets_own_endpoint_is_still_recorded_and_still_closes_the_tiers(ctx) -> None:
    pipeline = TraditionalPipeline(ctx)
    outcome = TargetOutcome(target=TARGET)

    outcome.due_diligence = DueDiligence(target=TARGET)
    outcome.due_diligence.note("native_endpoints", pipeline.admissible([url_candidate(OWN, RELATED)], outcome))

    assert outcome.due_diligence.native_endpoints == [OWN]
    assert pipeline.gate(Stage.PACKAGE, outcome) is not None


def test_the_rule_can_be_switched_off(ctx) -> None:
    ctx.settings.set("mcp.policy.native_must_be_registrable", False)
    pipeline = TraditionalPipeline(ctx)
    outcome = TargetOutcome(target=TARGET)
    assert pipeline.admissible([url_candidate(FOREIGN, UNRELATED)], outcome) == [FOREIGN]


def test_a_dropped_candidate_is_retracted_from_the_record(ctx) -> None:
    pipeline = TraditionalPipeline(ctx)
    outcome = TargetOutcome(target=TARGET)
    outcome.due_diligence = DueDiligence(target=TARGET)
    outcome.due_diligence.note("native_endpoints", [FOREIGN])
    candidate = url_candidate(FOREIGN, UNRELATED)
    stage = StageOutcome(stage=Stage.ENDPOINT, candidates=[candidate], verified=[candidate], status=Status.OK)

    pipeline.discard_unregistrable(stage, outcome)

    assert stage.verified == []
    assert outcome.due_diligence.native_endpoints == []
    assert FOREIGN in outcome.due_diligence.ruled_out


def test_a_claimed_endpoint_is_vetted_before_the_next_gate(ctx) -> None:
    pipeline = TraditionalPipeline(ctx)
    outcome = TargetOutcome(target=TARGET)
    outcome.due_diligence = DueDiligence(target=TARGET, source="agent")
    outcome.due_diligence.note("native_endpoints", [FOREIGN])
    assert pipeline.gate(Stage.PACKAGE, outcome) is not None
    pipeline.verifier.verify_all = answering({FOREIGN: UNRELATED})  # type: ignore[assignment]

    pipeline.vet_record(outcome)

    assert outcome.due_diligence.native_endpoints == []
    assert pipeline.gate(Stage.PACKAGE, outcome) is None
    assert outcome.stages[-1].status == Status.PARTIAL
    assert outcome.verified == []


def test_a_claimed_endpoint_that_never_answers_is_ruled_out_too(ctx) -> None:
    pipeline = TraditionalPipeline(ctx)
    outcome = TargetOutcome(target=TARGET)
    outcome.due_diligence = DueDiligence(target=TARGET, source="agent")
    outcome.due_diligence.note("native_endpoints", [OWN])
    pipeline.verifier.verify_all = answering({})  # type: ignore[assignment]

    pipeline.vet_record(outcome)

    assert OWN in outcome.due_diligence.ruled_out
    assert "did not answer" in outcome.due_diligence.ruled_out[OWN]


def test_a_claimed_endpoint_that_answers_for_the_target_is_confirmed(ctx) -> None:
    pipeline = TraditionalPipeline(ctx)
    outcome = TargetOutcome(target=TARGET)
    outcome.due_diligence = DueDiligence(target=TARGET, source="agent")
    outcome.due_diligence.note("native_endpoints", [OWN])
    pipeline.verifier.verify_all = answering({OWN: RELATED})  # type: ignore[assignment]

    pipeline.vet_record(outcome)

    assert outcome.due_diligence.native_endpoints == [OWN]
    assert [c.url for c in outcome.verified] == [OWN]
    assert outcome.stages[-1].status == Status.CHANGED


def test_a_backed_endpoint_is_not_probed_again(ctx) -> None:
    pipeline = TraditionalPipeline(ctx)
    outcome = TargetOutcome(target=TARGET)
    outcome.due_diligence = DueDiligence(target=TARGET)
    outcome.due_diligence.note("native_endpoints", [OWN])
    outcome.verified.append(url_candidate(OWN, RELATED))
    pipeline.verifier.verify_all = lambda *a, **k: pytest.fail("must not probe")  # type: ignore[assignment]

    pipeline.vet_record(outcome)
    assert outcome.stages == []


def test_vetting_can_be_switched_off(ctx) -> None:
    ctx.settings.set("mcp.policy.vet_record_before_gate", False)
    pipeline = TraditionalPipeline(ctx)
    outcome = TargetOutcome(target=TARGET)
    outcome.due_diligence = DueDiligence(target=TARGET, source="agent")
    outcome.due_diligence.note("native_endpoints", [FOREIGN])
    pipeline.verifier.verify_all = lambda *a, **k: pytest.fail("must not probe")  # type: ignore[assignment]

    pipeline.vet_record(outcome)
    assert outcome.due_diligence.native_endpoints == [FOREIGN]


# --------------------------------------------------------------------------- #
# traditional: the whole run
# --------------------------------------------------------------------------- #


def claiming(url: str):
    """An endpoint stage that records ``url`` on its word, as an agent's checklist does."""

    def stage_endpoint(outcome: TargetOutcome) -> StageOutcome:
        outcome.due_diligence = DueDiligence(target=outcome.target, source="agent")
        outcome.due_diligence.native_endpoints.append(url)
        return StageOutcome(stage=Stage.ENDPOINT, status=Status.SKIPPED, detail="claimed")

    return stage_endpoint


def package_ran(calls: list[str]):
    def stage_package(outcome: TargetOutcome) -> StageOutcome:
        calls.append(outcome.target)
        return StageOutcome(stage=Stage.PACKAGE, status=Status.SKIPPED, detail="looked")

    return stage_package


def test_a_run_does_not_let_a_foreign_endpoint_skip_the_package_stage(ctx) -> None:
    pipeline = TraditionalPipeline(ctx)
    calls: list[str] = []
    pipeline.stage_endpoint = claiming(FOREIGN)  # type: ignore[method-assign]
    pipeline.stage_package = package_ran(calls)  # type: ignore[method-assign]
    pipeline.verifier.verify_all = answering({FOREIGN: UNRELATED})  # type: ignore[assignment]

    outcome = pipeline.run(TARGET, [Stage.ENDPOINT, Stage.PACKAGE])

    assert calls == [TARGET]
    assert not any(s.findings.get("blocked_by_policy") for s in outcome.stages)


def test_a_stage_refused_on_a_retracted_endpoint_is_reopened(ctx) -> None:
    """The safety net: with vetting off, reconcile's retraction still reopens the stage."""
    ctx.settings.set("mcp.policy.vet_record_before_gate", False)
    pipeline = TraditionalPipeline(ctx)
    calls: list[str] = []
    pipeline.stage_endpoint = claiming(FOREIGN)  # type: ignore[method-assign]
    pipeline.stage_package = package_ran(calls)  # type: ignore[method-assign]
    pipeline.verifier.verify_all = answering({FOREIGN: UNRELATED})  # type: ignore[assignment]

    outcome = pipeline.run(TARGET, [Stage.ENDPOINT, Stage.PACKAGE])

    assert calls == [TARGET]
    package = [s for s in outcome.stages if s.stage == Stage.PACKAGE]
    assert len(package) == 1, "the refusal is replaced, not reported beside the run"
    assert package[0].findings.get("reopened")


def test_reopening_can_be_switched_off(ctx) -> None:
    ctx.settings.set("mcp.policy.vet_record_before_gate", False)
    ctx.settings.set("mcp.policy.reopen_when_native_retracted", False)
    pipeline = TraditionalPipeline(ctx)
    calls: list[str] = []
    pipeline.stage_endpoint = claiming(FOREIGN)  # type: ignore[method-assign]
    pipeline.stage_package = package_ran(calls)  # type: ignore[method-assign]
    pipeline.verifier.verify_all = answering({FOREIGN: UNRELATED})  # type: ignore[assignment]

    outcome = pipeline.run(TARGET, [Stage.ENDPOINT, Stage.PACKAGE])

    assert calls == []
    assert any(s.findings.get("blocked_by_policy") for s in outcome.stages)


def test_a_real_native_endpoint_still_keeps_the_package_stage_shut(ctx) -> None:
    pipeline = TraditionalPipeline(ctx)
    calls: list[str] = []
    pipeline.stage_endpoint = claiming(OWN)  # type: ignore[method-assign]
    pipeline.stage_package = package_ran(calls)  # type: ignore[method-assign]
    pipeline.verifier.verify_all = answering({OWN: RELATED})  # type: ignore[assignment]

    outcome = pipeline.run(TARGET, [Stage.ENDPOINT, Stage.PACKAGE])

    assert calls == []
    assert [c.url for c in outcome.verified] == [OWN]


# --------------------------------------------------------------------------- #
# agentic: the same rule on the agent's route
# --------------------------------------------------------------------------- #


def test_an_agent_endpoint_that_cannot_be_registered_closes_no_tier(ctx) -> None:
    pipeline = AgenticPipeline(ctx)
    pipeline.verifier.verify_all = answering({FOREIGN: UNRELATED})  # type: ignore[assignment]
    stage = StageOutcome(stage=Stage.ENDPOINT, mode="agentic")
    target = TargetOutcome(target=TARGET)

    pipeline._ingest(
        stage,
        AgentResult(ok=True, runtime="test", payload={"candidates": [{"kind": "url", "url": FOREIGN}]}),
        target,
    )

    assert stage.verified == []
    record = target.due_diligence
    assert record.native_endpoints == []
    assert FOREIGN in record.ruled_out, "recorded, so the retry is told"
    assert not record.native_exists


def test_an_agent_checklist_claim_is_ruled_out_when_it_is_not_the_targets(ctx) -> None:
    pipeline = AgenticPipeline(ctx)
    pipeline.fallback.verifier.verify_all = answering({FOREIGN: UNRELATED})  # type: ignore[assignment]
    target = TargetOutcome(target=TARGET)
    target.due_diligence = DueDiligence(target=TARGET, source="agent")
    target.due_diligence.note("native_endpoints", [FOREIGN])

    pipeline.fallback.vet_record(target)

    assert target.due_diligence.native_endpoints == []
    assert FOREIGN in target.due_diligence.ruled_out
    assert pipeline.fallback.gate(Stage.PACKAGE, target) is None
