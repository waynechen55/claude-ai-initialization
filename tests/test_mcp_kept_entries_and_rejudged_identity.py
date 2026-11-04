"""developers.google.com/knowledge/api went missing from .mcp.json.

The claude CLI held developers-google-com -> developerknowledge.googleapis.com/mcp,
connected, and it verified with the credential it sends. It was refused at
reuse -- "nothing ties it to developers.google.com" -- before the endpoint
stage had found developers.google.com documenting that very URL, and was never
judged again. The agent's rediscovered copy carried no credential and got 401;
the writer rightly would not let it replace a connected entry -- and wrote
nothing, so the working server stayed in the CLI only.

Learned: what ties a server to an address is re-judged as evidence arrives, and
covers every copy of the server; a copy carries the credential slots of the one
that works; and an entry kept in place is written to the project file too.
"""

from __future__ import annotations

import json
import logging

from initialize_claude_ai.mcp_auto_discovery import same_server
from initialize_claude_ai.mcp_auto_discovery.types import Candidate, Handshake, Stage, StageOutcome, TargetOutcome

TARGET = "https://github.com"
URL = "https://api.githubcopilot.example/mcp"


def existing() -> Candidate:
    candidate = Candidate(
        kind="url",
        name="github-com",
        url=URL,
        transport="streamable-http",
        origin=Stage.ENDPOINT,
        source="already registered as 'github-com' (the claude CLI)",
        headers={"Authorization": "Bearer ${GITHUB_TOKEN}"},
    )
    candidate.secret_env = {"GITHUB_TOKEN": "env:GITHUB_TOKEN"}
    candidate.existing_name = "github-com"
    candidate.handshake = Handshake(ok=True, tools=["get_me"], round_trip=True)
    return candidate


def rediscovered() -> Candidate:
    candidate = Candidate(
        kind="url",
        name="agent-copy",
        url=URL,
        transport="streamable-http",
        origin=Stage.ENDPOINT,
        source="GitHub's own documentation for the remote GitHub MCP server",
    )
    candidate.handshake = Handshake(
        ok=False,
        requires_auth=True,
        status=401,
        auth_evidence=["WWW-Authenticate: Bearer resource_metadata=.../oauth-protected-resource/mcp"],
    )
    return candidate


def test_a_server_refused_for_want_of_evidence_rejoins_when_a_copy_is_documented(ctx) -> None:
    from initialize_claude_ai.mcp_auto_discovery.traditional import TraditionalPipeline

    pipeline = TraditionalPipeline(ctx)
    old, new = existing(), rediscovered()
    outcome = TargetOutcome(target=TARGET)
    assert not pipeline.registrable(old, outcome), "a registration is a claim: nothing ties it yet"
    assert pipeline.refused_for_identity_only(old, outcome)
    assert pipeline.registrable(new, outcome), "the target's own documentation names it"
    stage = StageOutcome(stage=Stage.ENDPOINT, candidates=[new], provisional=[new])
    assert pipeline.registrable_with(old, outcome, stage)
    assert pipeline.registrable(old, outcome), "the evidence is carried on the server, for every gate after"


def test_kept_aside_and_judged_again_after_the_next_stage(ctx) -> None:
    from initialize_claude_ai.mcp_auto_discovery.traditional import TraditionalPipeline

    pipeline = TraditionalPipeline(ctx)
    old, new = existing(), rediscovered()
    outcome = TargetOutcome(target=TARGET)
    outcome.identity_pending.append(old)
    stage = StageOutcome(stage=Stage.ENDPOINT, candidates=[new], provisional=[new])
    pipeline.rejudge_identity(stage, outcome)
    assert old in stage.verified and outcome.identity_pending == []
    pipeline.fold_copies(stage, outcome)
    assert stage.verified == [old] and stage.provisional == [], "the working copy is the one kept"


def test_an_unrelated_server_is_still_refused(ctx) -> None:
    from initialize_claude_ai.mcp_auto_discovery.traditional import TraditionalPipeline

    pipeline = TraditionalPipeline(ctx)
    stranger = Candidate(
        kind="url",
        name="stranger",
        url="https://mcp.stranger.example/mcp",
        transport="streamable-http",
        source="a web search",
    )
    stranger.handshake = Handshake(ok=True, tools=["a"], round_trip=True)
    stage = StageOutcome(stage=Stage.ENDPOINT, candidates=[rediscovered()], provisional=[rediscovered()])
    assert not pipeline.registrable_with(stranger, TargetOutcome(target=TARGET), stage)


def test_the_kept_copy_carries_the_credential_slots_of_the_one_that_has_them(settings) -> None:
    keeper, other = rediscovered(), existing()
    assert same_server.inherit_credential(keeper, other, settings)
    assert keeper.headers["Authorization"] == "Bearer ${GITHUB_TOKEN}" and keeper.secret_env == other.secret_env
    working = existing()
    assert not same_server.inherit_credential(working, rediscovered(), settings), "a working copy keeps its own"


def test_a_rediscovered_remote_server_adopts_the_existing_entry_s_slots(ctx, sandbox) -> None:
    from initialize_claude_ai.mcp_auto_discovery.verifier import CandidateVerifier

    ctx.settings.set("mcp.discovery.reuse.sources", ["project_file"])
    ctx.settings.set("mcp.mcp_json_path", str(sandbox / ".mcp.json"))
    (sandbox / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "github-com": {"type": "http", "url": URL, "headers": {"Authorization": "Bearer ${GITHUB_TOKEN}"}}
                }
            }
        )
    )
    copy_ = rediscovered()
    CandidateVerifier(ctx)._adopt_existing_slots(copy_)
    assert copy_.headers.get("Authorization") == "Bearer ${GITHUB_TOKEN}"


def test_a_kept_working_cli_entry_is_written_to_the_project_file_too(ctx, sandbox, monkeypatch) -> None:
    from initialize_claude_ai.mcp_auto_discovery.writer import McpWriter

    ctx.dry_run = False
    ctx.settings.set("mcp.mcp_json_path", str(sandbox / ".mcp.json"))
    old = existing()
    outcome = TargetOutcome(target=TARGET)
    outcome.answered_existing = {"github-com": "claude_cli"}
    outcome.connected_existing = {"github-com": "claude_cli"}
    writer = McpWriter(ctx)
    monkeypatch.setattr(writer, "_existing_candidate", lambda o, name: old)
    writer._keep_existing(outcome, [])
    written = json.loads((sandbox / ".mcp.json").read_text())["mcpServers"]["github-com"]
    assert written["url"] == URL and "${" in json.dumps(written), "portable: the credential is a reference"
    assert "also recorded" in outcome.kept_existing[0]["remediation"]


def test_an_entry_in_the_file_is_never_overwritten_by_a_kept_one(ctx, sandbox, monkeypatch) -> None:
    from initialize_claude_ai.mcp_auto_discovery.writer import McpWriter

    ctx.dry_run = False
    ctx.settings.set("mcp.mcp_json_path", str(sandbox / ".mcp.json"))
    (sandbox / ".mcp.json").write_text(json.dumps({"mcpServers": {"github-com": {"type": "http", "url": "https://x"}}}))
    outcome = TargetOutcome(target=TARGET)
    outcome.answered_existing = {"github-com": "claude_cli"}
    outcome.connected_existing = {"github-com": "claude_cli"}
    writer = McpWriter(ctx)
    monkeypatch.setattr(writer, "_existing_candidate", lambda o, name: existing())
    writer._keep_existing(outcome, [])
    assert json.loads((sandbox / ".mcp.json").read_text())["mcpServers"]["github-com"]["url"] == "https://x"


def test_an_unproven_kept_entry_is_reported_as_cli_only(ctx, sandbox, monkeypatch) -> None:
    from initialize_claude_ai.mcp_auto_discovery.writer import McpWriter

    ctx.settings.set("mcp.mcp_json_path", str(sandbox / ".mcp.json"))
    unproven = existing()
    unproven.handshake = Handshake(ok=False, error="it exited")
    outcome = TargetOutcome(target=TARGET)
    outcome.answered_existing = {"github-com": "claude_cli"}
    writer = McpWriter(ctx)
    monkeypatch.setattr(writer, "_existing_candidate", lambda o, name: unproven)
    writer._keep_existing(outcome, [])
    assert "in the claude CLI only" in outcome.kept_existing[0]["remediation"]


def test_the_gate_s_reason_is_logged(ctx, caplog, monkeypatch) -> None:
    from initialize_claude_ai.mcp_auto_discovery.verifier import CandidateVerifier

    verifier = CandidateVerifier(ctx)
    monkeypatch.setattr(verifier, "verify", lambda c: c)
    caplog.set_level(logging.INFO)
    verifier.verify_all([existing()], keep=lambda c: False, why=lambda c: "nothing ties it to github.com")
    assert "nothing ties it to github.com" in caplog.text


def test_packaged_settings_equal_the_code(settings) -> None:
    registration = settings.section("mcp.registration")
    assert registration.bool_("record_kept_entries") and registration.bool_("rejudge_identity")
    assert registration.bool_("inherit_credential_slots")


def test_held_means_the_held_entry_is_written_never_nothing(ctx) -> None:
    from initialize_claude_ai.mcp_auto_discovery.writer import McpWriter

    old, new = existing(), rediscovered()
    outcome = TargetOutcome(target=TARGET, verified=[old], provisional=[new])
    outcome.connected_existing = {"github-com": "claude_cli"}
    registrations = McpWriter(ctx).publish(outcome)
    assert [r.name for r in registrations] == ["github-com"]
    assert registrations[0].candidate is old and not registrations[0].refused
