"""The tier order: the target's own endpoint first, and prove it.

Phase 4 can obtain MCP access three ways -- the target's native endpoint, a
published package, a bridge written here -- and descending a tier while a
higher one was available leaves somebody maintaining software that does a job
the vendor was already doing. That ordering is the one thing in phase 4 that can
*stop* work from happening, so what it refuses, what it allows and what it says
while doing either are pinned here rather than left to the stages' good manners.

Three properties are under test: a native endpoint shuts the lower tiers, an
unfinished four-step checklist shuts them too, and the one documented exception
-- a native endpoint on a transport Claude Code cannot dial -- opens tier 3
rather than closing it. The rest covers the checklist's own arithmetic: evidence
is what separates "done" from "claimed", an excused step is not a gap, and two
half-finished records merge into the better of each half.
"""

from __future__ import annotations

from initialize_claude_ai.core.report import Status
from initialize_claude_ai.mcp_auto_discovery.policy import CHECKLIST, DueDiligence, TierPolicy
from initialize_claude_ai.mcp_auto_discovery.traditional import TraditionalPipeline
from initialize_claude_ai.mcp_auto_discovery.types import Stage, TargetOutcome


def complete(target: str = "https://example.test", **kwargs) -> DueDiligence:
    """A record whose checklist is finished, so a test can be about one thing."""
    record = DueDiligence(target=target, **kwargs)
    for key, _letter, _title in CHECKLIST:
        record.record(key, Status.OK, "done", evidence=[f"{key} evidence"])
    return record


# ----------------------------------------------------------------------------
# what the gate refuses
# ----------------------------------------------------------------------------


def test_a_native_endpoint_forbids_a_package_and_a_bridge(ctx) -> None:
    policy = TierPolicy(ctx)
    record = complete(native_endpoints=["https://example.test/mcp"])

    assert policy.gate(Stage.ENDPOINT, record) == (True, "")
    for stage in (Stage.PACKAGE, Stage.BRIDGE, Stage.STRATEGY):
        allowed, why = policy.gate(stage, record)
        assert not allowed
        assert "https://example.test/mcp" in why
        assert "forbid_lower_tiers_when_native" in why


def test_an_endpoint_behind_a_401_still_counts_as_one(ctx) -> None:
    """A credential is a thing the operator can supply; absence is not."""
    record = complete(auth_walls=["https://example.test/mcp"])

    allowed, why = TierPolicy(ctx).gate(Stage.PACKAGE, record)
    assert not allowed
    assert "behind authentication" in why
    remediation = " ".join(TierPolicy(ctx).remediation(Stage.PACKAGE, record))
    assert "INITIALIZE_CLAUDE_AI_MCP_TOKEN_REF" in remediation
    assert "never the value" in remediation


def test_an_unfinished_checklist_shuts_the_lower_tiers(ctx) -> None:
    record = DueDiligence(target="https://example.test")
    record.record("paths", Status.OK, "probed 40 paths", evidence=["/mcp 404"])

    allowed, why = TierPolicy(ctx).gate(Stage.PACKAGE, record)
    assert not allowed
    assert "require_checklist" in why
    assert "D (" in why, "the refusal names the steps nobody carried out"


def test_no_endpoint_stage_means_no_lower_tier(ctx) -> None:
    allowed, why = TierPolicy(ctx).gate(Stage.BRIDGE, None)
    assert not allowed
    assert "ruled out first" in why


def test_a_finished_checklist_and_no_endpoint_opens_the_lower_tiers(ctx) -> None:
    assert TierPolicy(ctx).gate(Stage.PACKAGE, complete()) == (True, "")


def test_a_step_that_could_not_run_here_is_not_a_gap(ctx) -> None:
    """Offline is a fact about this machine, not a finding about the target --
    and a program that refuses to work without a network is not usable."""
    record = complete()
    record.record("grounding", Status.SKIPPED, "", excused="no network access")

    assert record.gaps() == []
    assert TierPolicy(ctx).gate(Stage.PACKAGE, record) == (True, "")


# ----------------------------------------------------------------------------
# the exception, and the switches
# ----------------------------------------------------------------------------


def test_an_endpoint_claude_code_cannot_dial_opens_the_bridge_stage(ctx) -> None:
    """A WebSocket route is tier 1 that needs a shim. The bridge then goes in
    front of the target's own endpoint, which is the policy working rather than
    a way around it -- so tier 3 is allowed and tier 2 is not."""
    policy = TierPolicy(ctx)
    record = complete(needs_bridge=["wss://example.test/ws"])

    assert policy.gate(Stage.BRIDGE, record) == (True, "")
    assert policy.gate(Stage.STRATEGY, record) == (True, "")
    allowed, why = policy.gate(Stage.PACKAGE, record)
    assert not allowed, "a package would still be reimplementing the endpoint"
    assert "wss://example.test/ws" in why


def test_a_working_endpoint_outranks_a_websocket_one(ctx) -> None:
    """The exception is for an endpoint that cannot be reached as it stands. One
    that can be reached leaves nothing for a bridge to do."""
    record = complete(native_endpoints=["https://example.test/mcp"], needs_bridge=["wss://example.test/ws"])
    allowed, _why = TierPolicy(ctx).gate(Stage.BRIDGE, record)
    assert not allowed


def test_the_operator_can_turn_the_gate_off(ctx) -> None:
    record = complete(native_endpoints=["https://example.test/mcp"])
    ctx.settings.set("mcp.policy.forbid_lower_tiers_when_native", "false")
    assert TierPolicy(ctx).gate(Stage.PACKAGE, record) == (True, "")

    gapped = DueDiligence(target="https://example.test")
    ctx.settings.set("mcp.policy.require_checklist", "false")
    assert TierPolicy(ctx).gate(Stage.PACKAGE, gapped) == (True, "")

    ctx.settings.set("mcp.policy.native_first", "false")
    assert TierPolicy(ctx).gate(Stage.BRIDGE, None) == (True, "")


def test_which_stage_is_which_tier_is_configuration(ctx) -> None:
    ctx.settings.set("mcp.policy.tiers.package", "1")
    policy = TierPolicy(ctx)
    assert policy.tier(Stage.PACKAGE) == 1
    assert policy.gate(Stage.PACKAGE, None) == (True, ""), "tier 1 is never gated"


def test_the_mandatory_paths_come_from_settings(ctx) -> None:
    paths = TierPolicy(ctx).mandatory_paths()
    assert "/mcp" in paths and "/sse" in paths
    assert len(paths) > 4, "step A is about breadth; a two-path list would not prove it"


# ----------------------------------------------------------------------------
# a blocked stage is reported, not silently dropped
# ----------------------------------------------------------------------------


def test_a_blocked_stage_is_reported_with_the_rule_and_the_override(ctx) -> None:
    target = TargetOutcome(target="https://example.test")
    target.due_diligence = complete(native_endpoints=["https://example.test/mcp"])

    stage = TraditionalPipeline(ctx).gate(Stage.PACKAGE, target)

    assert stage is not None
    assert stage.status == Status.SKIPPED
    assert stage.findings["tier"] == 2
    assert "forbid_lower_tiers_when_native" in stage.findings["blocked_by_policy"]
    joined = " ".join(stage.remediation)
    assert "https://example.test/mcp is the target's own MCP server" in joined
    assert "forbid_lower_tiers_when_native = false" in joined


def test_an_allowed_stage_is_not_blocked(ctx) -> None:
    target = TargetOutcome(target="https://example.test")
    target.due_diligence = complete()
    assert TraditionalPipeline(ctx).gate(Stage.PACKAGE, target) is None


# ----------------------------------------------------------------------------
# the checklist's own arithmetic
# ----------------------------------------------------------------------------


def test_the_checklist_starts_empty_and_nothing_is_assumed_done(ctx) -> None:
    record = DueDiligence(target="https://example.test")
    assert [s.letter for s in record.steps] == ["A", "B", "C", "D"]
    assert not record.complete
    assert not record.native_exists
    assert "no native endpoint answered" in record.summary()


def test_an_agent_that_shows_no_evidence_is_believed_only_halfway(ctx) -> None:
    """ "I checked" with nothing to show is not a checked step -- but calling it a
    lie would be wrong too, so it lands on PARTIAL and the table says so."""
    record = DueDiligence.from_payload(
        "https://example.test",
        {
            "paths": {"done": True, "detail": "probed the list", "evidence": ["/mcp -> 404"]},
            "bypasses": {"done": True, "detail": "tried both schemes"},
            "handshake": {"done": False, "detail": "no Accept header sent"},
            "grounding": {"unavailable": "no search tool in this environment"},
        },
    )

    assert record is not None
    assert record.source == "agent"
    assert record.step("paths").status == Status.OK
    assert record.step("bypasses").status == Status.PARTIAL
    assert record.step("handshake").status == Status.FAILED
    assert record.step("grounding").mark == "n/a"
    assert [s.letter for s in record.gaps()] == ["C"]


def test_an_agent_reporting_an_endpoint_closes_no_tier_by_itself(ctx) -> None:
    """confluence.example.com's /mcp -- a login redirect, as for every missing path -- was reported
    "behind authentication" and closed the package, bridge and strategy stages."""
    from initialize_claude_ai.mcp_auto_discovery import policy

    record = DueDiligence.from_payload(
        "https://example.test",
        {"native_endpoints": ["https://example.test/api/mcp"], "auth_required_endpoints": ["https://example.test/mcp"]},
    )
    assert record is not None and not record.native_exists
    assert record.claimed == {
        "native_endpoints": ["https://example.test/api/mcp"],
        "auth_walls": ["https://example.test/mcp"],
    }
    assert (
        TierPolicy(ctx).gate(Stage.BRIDGE, record)[0] or "incomplete" in TierPolicy(ctx).gate(Stage.BRIDGE, record)[1]
    )
    policy.CLAIMS_CLOSE_TIERS[0] = True
    try:
        believed = DueDiligence.from_payload(
            "https://example.test", {"native_endpoints": ["https://example.test/api/mcp"]}
        )
        assert believed.native_exists and not TierPolicy(ctx).gate(Stage.BRIDGE, believed)[0]
    finally:
        policy.CLAIMS_CLOSE_TIERS[0] = False


def test_an_empty_payload_is_no_record_at_all(ctx) -> None:
    assert DueDiligence.from_payload("https://example.test", {}) is None
    assert DueDiligence.from_payload("https://example.test", "nothing") is None


def test_two_half_finished_records_merge_into_one_finished_one(ctx) -> None:
    """The agent does step D properly and gives up on step A; the deterministic
    fallback does the opposite. Between them the checklist is done, and the gate
    should see that rather than punish the target for the handover."""
    probed = DueDiligence(target="https://example.test")
    probed.record("paths", Status.OK, "42 paths probed", evidence=["/mcp -> 404"])
    probed.record("bypasses", Status.OK, "both schemes", evidence=["http+https"])
    probed.record("handshake", Status.OK, "Accept sent", evidence=["no SSE frames"])
    probed.record("grounding", Status.PARTIAL, "registry only", evidence=["registry: 0 hits"])

    agentic = DueDiligence(target="https://example.test", source="agent")
    agentic.record("grounding", Status.OK, "vendor docs read", evidence=["changelog 4.2"])
    agentic.native_endpoints.append("https://example.test/api/v2/mcp")

    merged = probed.merge(agentic)

    assert merged.complete
    assert merged.step("grounding").status == Status.OK
    assert merged.step("grounding").evidence == ["registry: 0 hits", "changelog 4.2"]
    assert merged.source == "probing+agent"
    assert "native endpoint found at https://example.test/api/v2/mcp" in merged.summary()


def test_a_weaker_report_never_undoes_a_stronger_one(ctx) -> None:
    """Whichever mode ran second, a step already carried out stays carried out."""
    probed = complete()
    agentic = DueDiligence(target="https://example.test", source="agent")
    agentic.record("paths", Status.FAILED, "ran out of turns")

    merged = probed.merge(agentic)

    assert merged.step("paths").status == Status.OK
    assert merged.complete


def test_the_record_serialises_without_losing_the_verdicts(ctx) -> None:
    record = complete(needs_bridge=["wss://example.test/ws"])
    payload = record.to_dict()
    assert payload["complete"] is True
    assert payload["native_exists"] is True
    assert payload["endpoints_needing_a_bridge"] == ["wss://example.test/ws"]
    assert [row[0] for row in record.rows()] == ["A", "B", "C", "D"]
    # Round-tripping is how the agentic pipeline hands a record to the next
    # stage, so the JSON it writes has to be the JSON it reads.
    again = DueDiligence.from_payload(record.target, {**payload, **_steps_payload(record)})
    assert again is not None
    assert again.complete
    assert again.needs_bridge == ["wss://example.test/ws"]


def _steps_payload(record: DueDiligence) -> dict[str, object]:
    return {step.key: {"done": True, "detail": step.detail, "evidence": step.evidence} for step in record.steps}
