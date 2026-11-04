"""The report answers "which address has an entry, which does not, and why".

build.opensuse.org ended with nothing in .mcp.json. The summary counted it as
one of nine "failed" addresses -- eight of which *had* entries, waiting for a
credential. The final verdict listed the twelve entries that were written and
never named the address that got none. The best lead, openSUSE's own osc-mcp,
was visible only inside an agent's quoted prose, and the broken registration
the CLI still held for it was not mentioned at all.

Every view now sorts addresses into the same three kinds -- working, waiting
for a credential, nothing registered -- and an address with nothing says so,
with the closest candidate and what it needs.
"""

from __future__ import annotations

from initialize_claude_ai.core.report import PhaseResult, Status
from initialize_claude_ai.mcp_auto_discovery.types import (
    Candidate,
    Handshake,
    Stage,
    StageOutcome,
    TargetOutcome,
    Transport,
)
from initialize_claude_ai.mcp_auto_discovery.writer import McpWriter
from initialize_claude_ai.phases.phase4_mcp_auto_discovery import McpPhase

TARGET = "https://build.example.org"


def lead() -> Candidate:
    candidate = Candidate(
        kind="stdio",
        name="obs-tool",
        transport=Transport.STDIO,
        command=["go", "run", "example.org/obs-tool@v1"],
        env={"OBS_TOOL_API": "https://build.example.org"},
        confidence=0.7,
        source="published by the target's own organisation",
    )
    candidate.secret_env = {"OBS_TOOL_PASSWORD": "env:OBS_TOOL_PASSWORD", "OBS_TOOL_USER": "env:OBS_TOOL_USER"}
    candidate.handshake = Handshake(ok=False, transport=Transport.STDIO, error="user not set for apiurl")
    return candidate


def nothing_for(ctx) -> tuple[McpPhase, PhaseResult, TargetOutcome, McpWriter]:
    phase = McpPhase(ctx)
    result = PhaseResult(phase=phase.slug, title=phase.title)
    outcome = TargetOutcome(target=TARGET)
    outcome.stages.append(StageOutcome(stage=Stage.PACKAGE, status=Status.PARTIAL, candidates=[lead()]))
    outcome.broken_existing.append(
        {"name": "build-example-org", "source": "claude_cli", "error": "${ICA_MCP_PYTHON} is not installed"}
    )
    writer = McpWriter(ctx)
    phase._finalise(result, writer, outcome)
    return phase, result, outcome, writer


def test_an_address_with_nothing_says_so_and_names_the_best_lead(ctx) -> None:
    _phase, _result, outcome, writer = nothing_for(ctx)
    assert outcome.detail.startswith(f"nothing registered -- {writer.display_path} has no entry for build-example-org")
    assert "best lead: obs-tool" in outcome.detail
    assert "OBS_TOOL_PASSWORD, OBS_TOOL_USER" in outcome.detail
    assert outcome.to_dict()["best_lead"]["name"] == "obs-tool"


def test_a_registration_left_in_place_that_does_not_start_is_reported(ctx) -> None:
    _phase, result, _outcome, _writer = nothing_for(ctx)
    [step] = [s for s in result.steps if s.name.endswith(":: existing build-example-org")]
    assert step.status == Status.PARTIAL
    assert "ICA_MCP_PYTHON" in step.detail
    assert any("remove it" in r for r in step.remediation)


def test_the_verdict_names_the_address_that_got_nothing(ctx) -> None:
    phase, result, outcome, writer = nothing_for(ctx)
    phase._report_verdict(result, writer, [outcome])
    detail = result.steps[-1].detail
    assert f"Not in {writer.display_path}: {TARGET} (no build-example-org)" in detail
    assert "best lead: obs-tool" in detail
    assert "Left in place and not starting: build-example-org" in detail
    assert result.data["mcp_not_registered"] == [TARGET]


def test_each_address_is_in_exactly_one_kind() -> None:
    servers = [
        {"target": "a", "succeeded": True, "registered": ["a"]},
        {"target": "b", "succeeded": False, "registered": ["b"]},
        {"target": "c", "succeeded": False, "registered": []},
    ]
    kinds = McpPhase.address_outcomes(servers)
    assert [s["target"] for s in kinds["working"]] == ["a"]
    assert [s["target"] for s in kinds["waiting"]] == ["b"]
    assert [s["target"] for s in kinds["unregistered"]] == ["c"]


def test_the_summary_box_has_a_row_per_kind(ctx) -> None:
    phase = McpPhase(ctx)
    result = PhaseResult(phase=phase.slug, title=phase.title)
    result.data["servers"] = [
        {"target": "https://a.example", "succeeded": True, "registered": ["a-example"]},
        {"target": "https://b.example", "succeeded": False, "registered": ["b-example"]},
        {"target": TARGET, "succeeded": False, "registered": []},
    ]
    text = "\n".join(line.text for line in phase.outcome_lines(result, 10))
    assert "working: 1  https://a.example (a-example)" in text
    assert "waiting for a credential: 1  https://b.example (b-example)" in text
    assert f"nothing registered: 1  {TARGET}" in text


def test_safety_never_calls_an_address_with_an_entry_missing(ctx) -> None:
    phase = McpPhase(ctx)
    result = PhaseResult(phase=phase.slug, title=phase.title)
    result.data["servers"] = [
        {"target": "https://a.example", "succeeded": True, "registered": ["a-example"]},
        {
            "target": "https://b.example",
            "succeeded": False,
            "registered": ["b-example"],
            "provisional": [{"name": "b"}],
        },
    ]
    phase.report_step(
        result, "https://a.example :: register a-example", Status.CHANGED, "recorded", group="https://a.example"
    )
    phase.report_step(
        result, "https://b.example :: register b-example", Status.PARTIAL, "waiting", group="https://b.example"
    )
    result.declare_group("https://a.example", Status.OK, "working")
    result.declare_group("https://b.example", Status.PARTIAL, "waiting")
    _safety, note = phase.safety(result)
    assert "Nothing registered for" not in note
    assert "every other address has an entry written" in note
