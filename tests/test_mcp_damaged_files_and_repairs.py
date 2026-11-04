"""A run after a full disk: a half-written file is never left, a rewritten file re-opens its verdict, and repair comes before replacement.

sd-example-com's working customer-API bridge had an *empty* operations manifest
-- truncated by a plain ``write_text`` on a full disk -- and was judged "does
not start". Later in the same run the bridge stage regenerated that manifest,
but the verdict stood, and mcp-atlassian (which a customer account cannot use)
was written; the stage had also kept an agent's bridge over the program's own
customer-API bridge. smelt-example-net's working mcp-graphql met ``fetch failed``
-- a trust store, not its job -- and was replaced.
"""

from __future__ import annotations

import errno
import json
import os

import pytest

from initialize_claude_ai.core import fileio
from initialize_claude_ai.mcp_auto_discovery.types import Candidate, Handshake, Stage, StageOutcome, TargetOutcome


def test_a_failed_write_leaves_the_old_file_whole(tmp_path, monkeypatch) -> None:
    target = tmp_path / "x.operations.json"
    target.write_text('{"operations": []}')
    real = os.fdopen

    class Full:
        def __init__(self, handle, mode):
            self.stream = real(handle, mode)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            self.stream.close()

        def write(self, _data):
            raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(os, "fdopen", lambda handle, mode="r", *a, **k: Full(handle, mode))
    with pytest.raises(OSError):
        fileio.write_generated(target, '{"operations": [1]}')
    monkeypatch.setattr(os, "fdopen", real)
    assert target.read_text() == '{"operations": []}', "never truncated"
    assert [p.name for p in tmp_path.iterdir()] == ["x.operations.json"], "nothing left behind"


def test_a_generated_script_stays_executable_and_is_remembered(tmp_path) -> None:
    script = tmp_path / "bridge.py"
    fileio.write_generated(script, "#!/usr/bin/env python3\n", executable=True)
    assert os.access(script, os.X_OK)
    assert str(script.resolve()) in fileio.REWRITTEN


def test_a_verdict_taken_before_its_file_was_rewritten_is_taken_again(ctx, tmp_path, monkeypatch) -> None:
    from initialize_claude_ai.mcp_auto_discovery.writer import McpWriter

    manifest = tmp_path / "desk.operations.json"
    fileio.write_generated(manifest, '{"operations": []}')
    existing = Candidate(
        kind="stdio",
        name="desk",
        command=["python3", "/b/bridge.py"],
        env={"MCP_BRIDGE_MANIFEST": str(manifest)},
        origin=Stage.BRIDGE,
    )
    existing.existing_name = "desk"
    existing.start_failure = "the operations manifest is not valid JSON"
    outcome = TargetOutcome(target="https://desk.example.test")
    outcome.unstartable.append(existing)
    outcome.broken_existing.append({"name": "desk", "source": "project_file", "error": "not valid JSON"})
    writer = McpWriter(ctx)

    def verify(candidates, **kw):
        for c in candidates:
            c.handshake = Handshake(ok=True, tools=["get_my_requests"], round_trip=True, credential_pending=["TOKEN"])
        return []

    monkeypatch.setattr(writer.verifier, "verify_all", verify)
    assert writer.retrial_rewritten(outcome) == ["desk"]
    assert existing in outcome.provisional and not outcome.broken_existing and not existing.start_failure


def test_a_stage_picks_the_server_built_for_the_detected_api_among_equals() -> None:
    def waiting(name: str, kind: str, confidence: float) -> Candidate:
        c = Candidate(kind="stdio", name=name, command=[name], api_kind=kind, confidence=confidence)
        c.handshake = Handshake(ok=True, tools=["t"], round_trip=True, credential_pending=["TOKEN"])
        return c

    agents = waiting("agent-bridge", "", 0.9)
    own = waiting("own-bridge", "jira_service_management", 0.75)
    stage = StageOutcome(stage=Stage.BRIDGE, candidates=[agents, own])
    assert stage.note_provisional(1) == [own]
    assert StageOutcome(stage=Stage.BRIDGE, candidates=[agents, own]).note_provisional(
        1, prefer_detected_api=False
    ) == [agents]


def test_a_reused_bridge_knows_its_api_kind_from_its_manifest(ctx, tmp_path) -> None:
    from initialize_claude_ai.mcp_auto_discovery import api_kinds
    from initialize_claude_ai.mcp_auto_discovery.existing import PROJECT_FILE, ExistingRegistrations, Registration

    kind = next(k for k in api_kinds.kinds(ctx.settings) if k.operations)
    manifest = tmp_path / "x.operations.json"
    manifest.write_text(json.dumps({"operations": [{"name": o["name"], "path": o["path"]} for o in kind.operations]}))
    entry = {
        "command": "python3",
        "args": [str(tmp_path / "mcp_bridge_x.py")],
        "env": {"MCP_BRIDGE_MANIFEST": str(manifest)},
    }
    candidate = ExistingRegistrations.for_run(ctx).candidate(
        Registration(name="x", entry=entry, source=PROJECT_FILE), Stage.BRIDGE
    )
    assert candidate.api_kind == kind.name


def test_a_connection_failure_is_repaired_before_the_server_is_judged(ctx, tmp_path, monkeypatch) -> None:
    from initialize_claude_ai.mcp_auto_discovery import trust
    from initialize_claude_ai.mcp_auto_discovery.traditional import TraditionalPipeline

    bundle = tmp_path / "good.pem"
    bundle.write_text("-----BEGIN CERTIFICATE-----\nx\n-----END CERTIFICATE-----\n")
    monkeypatch.setattr(trust, "variables_for", lambda command, settings: ["NODE_EXTRA_CA_CERTS"])
    monkeypatch.setattr(trust, "system_bundle", lambda settings: str(bundle))
    candidate = Candidate(
        kind="stdio",
        name="graphql",
        command=["npx", "-y", "mcp-graphql"],
        env={"NODE_EXTRA_CA_CERTS": str(tmp_path / "missing.pem")},
    )
    candidate.handshake = Handshake(
        ok=True,
        tools=["introspect-schema"],
        round_trip=True,
        contract=[
            {
                "task": "describe the API",
                "passed": False,
                "detail": "Failed to introspect schema: TypeError: fetch failed",
            }
        ],
    )
    pipeline = TraditionalPipeline(ctx)
    monkeypatch.setattr("initialize_claude_ai.mcp_auto_discovery.writer.McpWriter.run_bundle_for", lambda self, c: "")

    def verify(candidates, **kw):
        for c in candidates:
            c.handshake = Handshake(
                ok=True,
                tools=["introspect-schema"],
                round_trip=True,
                contract=[{"task": "describe the API", "passed": True}],
            )
        return list(candidates)

    monkeypatch.setattr(pipeline.verifier, "verify_all", verify)
    assert pipeline.heal_transport(candidate, TargetOutcome(target="https://smelt.example.test"))
    assert str(bundle) in candidate.env["NODE_EXTRA_CA_CERTS"] and "repaired, not replaced" in " ".join(candidate.notes)


def test_a_job_failure_is_not_mistaken_for_a_connection_failure(ctx) -> None:
    from initialize_claude_ai.mcp_auto_discovery.traditional import TraditionalPipeline

    candidate = Candidate(kind="stdio", name="x", command=["npx", "x"])
    candidate.handshake = Handshake(
        ok=True,
        tools=["t"],
        round_trip=True,
        contract=[{"task": "list things", "passed": False, "detail": "answered with nothing"}],
    )
    assert TraditionalPipeline(ctx).transport_failure(candidate) == ""


def test_packaged_settings_equal_the_code(settings) -> None:
    from initialize_claude_ai.mcp_auto_discovery.traditional import TRANSPORT_FAILURE_MARKERS

    assert tuple(settings.str_list("mcp.verification.transport_failure_markers")) == TRANSPORT_FAILURE_MARKERS
    assert settings.bool_("mcp.verification.heal_transport") is True
    assert settings.bool_("mcp.registration.retrial_rewritten") is True
