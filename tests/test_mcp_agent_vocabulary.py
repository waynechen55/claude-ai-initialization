"""An agent's reply means what the code reads, and its skills say what that is.

The failure these guard against: the endpoint skill told the agent to report a
WebSocket-only endpoint in ``findings.websocket_urls`` -- the name the
deterministic pipeline uses -- but only the deterministic pipeline turned that
into "a native endpoint that needs a bridge". An agent that followed its skill
to the letter left the bridge stage shut in the one case it exists for. The
same field now means the same thing whichever mode wrote it, and every
``due_diligence`` key a skill's example shows is one the code reads.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

import initialize_claude_ai
from initialize_claude_ai.mcp_auto_discovery.agentic import AgenticPipeline
from initialize_claude_ai.mcp_auto_discovery.policy import CHECKLIST, DueDiligence
from initialize_claude_ai.mcp_auto_discovery.runtime import AgentResult
from initialize_claude_ai.mcp_auto_discovery.types import Stage, StageOutcome, TargetOutcome

SKILLS = Path(initialize_claude_ai.__file__).parent / "skills"
WS = "wss://target.example.test/mcp/ws"


def ingest(ctx, payload: dict, stage: Stage = Stage.ENDPOINT) -> TargetOutcome:
    pipeline = AgenticPipeline(ctx)
    pipeline.verifier.verify = lambda c: c  # type: ignore[assignment]
    target = TargetOutcome(target="https://target.example.test")
    pipeline._ingest(
        StageOutcome(stage=stage, mode="agentic"), AgentResult(ok=True, runtime="t", payload=payload), target
    )
    return target


def test_a_websocket_the_agent_reports_opens_the_bridge_stage(ctx) -> None:
    target = ingest(ctx, {"findings": {"websocket_urls": [WS]}})
    assert target.due_diligence.needs_bridge == [WS]


def test_the_checklist_key_says_the_same(ctx) -> None:
    target = ingest(ctx, {"due_diligence": {"endpoints_needing_a_bridge": [WS]}, "findings": {"websocket_urls": [WS]}})
    assert target.due_diligence.needs_bridge == [WS]


def test_a_websocket_already_counted_native_is_not_also_a_bridge(ctx) -> None:
    target = ingest(ctx, {"due_diligence": {"native_endpoints": [WS]}, "findings": {"websocket_urls": [WS]}})
    assert target.due_diligence.needs_bridge == []


def test_only_the_endpoint_stage_reports_it(ctx) -> None:
    target = ingest(ctx, {"findings": {"websocket_urls": [WS]}}, stage=Stage.PACKAGE)
    assert not isinstance(target.due_diligence, DueDiligence) or not target.due_diligence.needs_bridge


# -- the skills describe what is read -------------------------------------------


def examples() -> list[tuple[str, dict]]:
    found = []
    for skill in sorted(SKILLS.glob("*/SKILL.md")):
        for block in re.findall(r"```json\n(.*?)```", skill.read_text(encoding="utf-8"), re.S):
            try:
                payload = json.loads(block)
            except ValueError:
                continue
            if isinstance(payload, dict):
                found.append((skill.parent.name, payload))
    return found


READ = {key for key, _letter, _title in CHECKLIST} | set(DueDiligence(target="t").to_dict()) - {
    "target",
    "source",
    "native_exists",
    "steps",
    "summary",
}


@pytest.mark.parametrize(("skill", "payload"), examples(), ids=lambda v: v if isinstance(v, str) else "")
def test_every_due_diligence_key_in_an_example_is_read(skill, payload) -> None:
    shown = set((payload.get("due_diligence") or {}).keys())
    assert shown <= READ, f"{skill}: {sorted(shown - READ)} is never read"


def test_every_skill_shows_an_example() -> None:
    assert {name for name, _ in examples()} == {p.parent.name for p in SKILLS.glob("*/SKILL.md")}
