"""confluence-example-com went missing from .mcp.json -- the third address to, each a different way.

The claude CLI held ``uvx mcp-atlassian`` for confluence.example.com, connected;
confluence.example.com answered 503 all run, so it could only wait for its token.
The writer "held" the existing entry against itself -- "Claude Code connects to
the existing confluence-example-com, and confluence-example-com proved less" -- and
wrote nothing, and the package stage reported "✔ OK confluence-mcp-server
verified" for a server refused minutes later.

Learned: a copy of the existing server is the existing server; the result is
checked -- every address that has a server has an entry in the file; and a
stage whose server was refused never reads as a success.
"""

from __future__ import annotations

import json
import re

from initialize_claude_ai.core.report import Status
from initialize_claude_ai.mcp_auto_discovery.types import Candidate, Handshake, Stage, StageOutcome, TargetOutcome

TARGET = "https://confluence.example.test"
ENTRY = {
    "command": "uvx",
    "args": ["mcp-atlassian"],
    "env": {"CONFLUENCE_URL": TARGET, "CONFLUENCE_PERSONAL_TOKEN": "${CONFLUENCE_PERSONAL_TOKEN}"},
}


def waiting(command: list[str], name: str, existing_name: str = "") -> Candidate:
    candidate = Candidate(
        kind="stdio",
        name=name,
        command=command,
        env={"CONFLUENCE_URL": TARGET},
        origin=Stage.PACKAGE,
        source=f"{name} (test)",
    )
    candidate.secret_env = {"CONFLUENCE_PERSONAL_TOKEN": "env:CONFLUENCE_PERSONAL_TOKEN"}
    candidate.existing_name = existing_name
    candidate.handshake = Handshake(
        ok=True, tools=["confluence_search"], round_trip=True, credential_pending=["CONFLUENCE_PERSONAL_TOKEN"]
    )
    return candidate


def live(ctx, sandbox, monkeypatch, cli: bool = True):
    ctx.dry_run = False
    ctx.settings.set("mcp.mcp_json_path", str(sandbox / ".mcp.json"))
    ctx.settings.set("mcp.registration.verify_as_written", False)
    if cli:
        monkeypatch.chdir(sandbox)
        (sandbox / "home" / ".claude.json").write_text(
            json.dumps({"projects": {str(sandbox): {"mcpServers": {"confluence-example-test": ENTRY}}}})
        )
        ctx.settings.set("mcp.discovery.reuse.sources", ["claude_cli"])
    monkeypatch.setattr(type(ctx), "tool", lambda self, *a, **k: "")
    return ctx


def test_the_confluence_run_ends_with_one_entry_in_the_file(ctx, sandbox, monkeypatch) -> None:
    from initialize_claude_ai.mcp_auto_discovery.writer import McpWriter

    live(ctx, sandbox, monkeypatch)
    existing = waiting(["uvx", "mcp-atlassian"], "confluence-example-test", existing_name="confluence-example-test")
    agents = waiting(["uvx", "mcp-atlassian@0.23.1"], "confluence-example-test-mcp-atlassian")
    outcome = TargetOutcome(target=TARGET, provisional=[existing, agents])
    outcome.connected_existing = {"confluence-example-test": "claude_cli"}
    outcome.answered_existing = {"confluence-example-test": "claude_cli"}
    outcome.identification_undecided = "confluence.example.test answered HTTP 503 while it was being identified"
    writer = McpWriter(ctx)
    [registration] = writer.publish(outcome)
    assert registration.name == "confluence-example-test" and not registration.refused, registration.refused
    written = json.loads((sandbox / ".mcp.json").read_text())["mcpServers"]["confluence-example-test"]
    reference = written["env"]["CONFLUENCE_PERSONAL_TOKEN"]
    assert re.fullmatch(r"\$\{CONFLUENCE_EXAMPLE_TEST_CONFLUENCE_PERSONAL_TOKEN(:-)?\}", reference), (
        "a reference, never a value -- through a name only this entry reads"
    )
    assert writer.missing_from_file(outcome) == []


def test_a_copy_with_another_version_pin_is_the_existing_server(ctx, sandbox, monkeypatch) -> None:
    from initialize_claude_ai.mcp_auto_discovery.writer import McpWriter, Registration

    live(ctx, sandbox, monkeypatch)
    copy_ = waiting(["uvx", "mcp-atlassian@0.23.1"], "agent-copy")
    copy_.secret_env = {}
    writer = McpWriter(ctx)
    registration = Registration(
        name="confluence-example-test", candidate=copy_, entry=writer.entry_for(copy_, "confluence-example-test")
    )
    assert writer._is_existing_server(registration)
    assert "CONFLUENCE_PERSONAL_TOKEN" in copy_.secret_env, "it takes the existing entry's credential slot"
    assert registration.entry["env"]["CONFLUENCE_PERSONAL_TOKEN"].startswith("${")
    other = waiting(["uvx", "some-other-confluence-mcp"], "other")
    assert not writer._is_existing_server(Registration(name="confluence-example-test", candidate=other, entry={}))


def test_an_entry_written_and_then_lost_is_written_again(ctx, sandbox, monkeypatch) -> None:
    from initialize_claude_ai.mcp_auto_discovery.writer import McpWriter

    live(ctx, sandbox, monkeypatch, cli=False)
    outcome = TargetOutcome(target=TARGET, provisional=[waiting(["uvx", "mcp-atlassian"], "x")])
    writer = McpWriter(ctx)
    [registration] = writer.publish(outcome)
    (sandbox / ".mcp.json").write_text(json.dumps({"mcpServers": {}}))
    problems = writer.missing_from_file(outcome)
    assert problems and "written again" in problems[0]
    assert registration.name in json.loads((sandbox / ".mcp.json").read_text())["mcpServers"]


def test_an_address_left_without_an_entry_is_said_with_every_refusal(ctx, sandbox, monkeypatch) -> None:
    from initialize_claude_ai.mcp_auto_discovery.writer import McpWriter, Registration

    live(ctx, sandbox, monkeypatch, cli=False)
    refused = waiting(["uvx", "x"], "x")
    outcome = TargetOutcome(target=TARGET, refused=[refused])
    writer = McpWriter(ctx)
    writer.published.append(
        Registration(name="x", candidate=refused, entry={}, target=TARGET, refused="it names no address at all")
    )
    [problem] = writer.missing_from_file(outcome)
    assert "has no entry" in problem and "it names no address at all" in problem


def test_an_address_that_never_had_a_server_is_not_called_missing(ctx, sandbox, monkeypatch) -> None:
    from initialize_claude_ai.mcp_auto_discovery.writer import McpWriter

    live(ctx, sandbox, monkeypatch, cli=False)
    assert McpWriter(ctx).missing_from_file(TargetOutcome(target=TARGET)) == []


def test_a_stage_whose_server_was_refused_is_not_a_success(ctx) -> None:
    from initialize_claude_ai.mcp_auto_discovery.traditional import TraditionalPipeline

    pipeline = TraditionalPipeline(ctx)
    pipeline.api_kinds_for = lambda outcome: {}  # type: ignore[method-assign]
    anywhere = Candidate(
        kind="stdio",
        name="confluence-mcp-server",
        command=["npx", "-y", "confluence-mcp-server"],
        origin=Stage.PACKAGE,
        source="npm search hit",
    )
    anywhere.handshake = Handshake(ok=True, tools=["search"], round_trip=True)
    walled = waiting(["npx", "-y", "confluence-datacenter-mcp@0.2.0"], "atlassian-dc-mcp-confluence")
    stage = StageOutcome(
        stage=Stage.PACKAGE,
        status=Status.OK,
        detail="confluence-mcp-server verified",
        candidates=[anywhere, walled],
        verified=[anywhere],
        provisional=[walled],
    )
    pipeline.discard_unregistrable(stage, TargetOutcome(target=TARGET))
    assert stage.status == Status.PARTIAL and "atlassian-dc-mcp-confluence" in stage.detail


def test_packaged_settings_equal_the_code(settings) -> None:
    assert settings.bool_("mcp.registration.check_in_file") is True


# -- build-example-net and build-opensuse-org: a connected client bridge waiting for a login ---------------------

BUILD = "https://build.example.test"
STRANGER = "https://mcp.stranger.example/api/mcp/public"


def client_bridge(slots: list[str]) -> Candidate:
    candidate = Candidate(
        kind="stdio",
        name="build-example-test",
        origin=Stage.BRIDGE,
        command=["python3", "mcp_client_bridge_build_example_test.py"],
        env={"BUILD_API": "https://api.build.example.test"},
        source="already registered as 'build-example-test' (the claude CLI)",
    )
    candidate.secret_env = {slot: f"env:{slot}" for slot in slots}
    candidate.existing_name = "build-example-test"
    candidate.handshake = Handshake(ok=True, tools=["whoami"], round_trip=True, credential_pending=list(slots))
    return candidate


def third_party() -> Candidate:
    """Verified, and refused: a public server that serves anybody's build service (ai.augenix/build)."""
    candidate = Candidate(
        kind="stdio",
        name="build-example-test-remote",
        origin=Stage.BRIDGE,
        command=["npx", "-y", "mcp-remote", STRANGER],
        source="a web search",
    )
    candidate.handshake = Handshake(ok=True, tools=["search"], round_trip=True)
    return candidate


def held_by_cli(ctx, sandbox, monkeypatch, bridge: Candidate):
    ctx.dry_run = False
    ctx.settings.set("mcp.mcp_json_path", str(sandbox / ".mcp.json"))
    ctx.settings.set("mcp.registration.verify_as_written", False)
    ctx.settings.set("mcp.discovery.reuse.sources", ["claude_cli"])
    monkeypatch.chdir(sandbox)
    entry = {
        "command": bridge.command[0],
        "args": bridge.command[1:],
        "env": {**bridge.env, **{slot: "${" + slot + "}" for slot in bridge.secret_env}},
    }
    (sandbox / "home" / ".claude.json").write_text(
        json.dumps({"projects": {str(sandbox): {"mcpServers": {"build-example-test": entry}}}})
    )
    monkeypatch.setattr(type(ctx), "tool", lambda self, *a, **k: "")
    (sandbox / "mcp_client_bridge_build_example_test.py").write_text("")
    return ctx


def outcome_for(bridge: Candidate) -> TargetOutcome:
    outcome = TargetOutcome(target=BUILD, provisional=[bridge])
    outcome.connected_existing = {"build-example-test": "claude_cli"}
    outcome.answered_existing = {"build-example-test": "claude_cli"}
    return outcome


def assert_written(ctx, sandbox, outcome: TargetOutcome, slots: list[str]) -> None:
    from initialize_claude_ai.mcp_auto_discovery.writer import McpWriter

    writer = McpWriter(ctx)
    [registration] = writer.publish(outcome)
    assert registration.name == "build-example-test" and not registration.refused, registration.refused
    written = json.loads((sandbox / ".mcp.json").read_text())["mcpServers"]["build-example-test"]
    for slot in slots:
        # A reference, never a value -- under the address's own name for a generic key (SSH_AUTH_SOCK).
        value = written["env"][slot]
        assert value.startswith("${") and value.endswith("}") and slot in value, value
    assert writer.missing_from_file(outcome) == []


def test_build_opensuse_org_a_client_bridge_waiting_for_a_password_is_written(ctx, sandbox, monkeypatch) -> None:
    slots = ["BUILD_EXAMPLE_TEST_USERNAME", "BUILD_EXAMPLE_TEST_PASSWORD"]
    bridge = client_bridge(slots)
    held_by_cli(ctx, sandbox, monkeypatch, bridge)
    assert_written(ctx, sandbox, outcome_for(bridge), slots)


def test_build_example_net_a_client_bridge_waiting_for_an_ssh_key_is_written(ctx, sandbox, monkeypatch) -> None:
    slots = ["BUILD_EXAMPLE_TEST_SSH_PUBLIC_KEY", "SSH_AUTH_SOCK"]
    bridge = client_bridge(slots)
    held_by_cli(ctx, sandbox, monkeypatch, bridge)
    assert_written(ctx, sandbox, outcome_for(bridge), slots)


def test_a_refused_third_party_server_beside_it_changes_nothing_in_either_mode(ctx, sandbox, monkeypatch) -> None:
    from initialize_claude_ai.mcp_auto_discovery.agentic import AgenticPipeline
    from initialize_claude_ai.mcp_auto_discovery.traditional import TraditionalPipeline

    slots = ["BUILD_EXAMPLE_TEST_USERNAME", "BUILD_EXAMPLE_TEST_PASSWORD"]
    held_by_cli(ctx, sandbox, monkeypatch, client_bridge(slots))
    for pipeline in (TraditionalPipeline(ctx), AgenticPipeline(ctx).fallback):
        pipeline.api_kinds_for = lambda outcome: {}  # type: ignore[method-assign]
        bridge, stranger = client_bridge(slots), third_party()
        stage = StageOutcome(
            stage=Stage.BRIDGE,
            status=Status.OK,
            detail=f"{stranger.label} verified",
            candidates=[stranger, bridge],
            verified=[stranger],
            provisional=[bridge],
        )
        outcome = outcome_for(bridge)
        outcome.provisional = []
        pipeline.discard_unregistrable(stage, outcome)
        assert stranger not in stage.verified, "not this address's: refused"
        assert stage.status == Status.PARTIAL and bridge.label in stage.detail
        outcome.absorb(stage)
        assert bridge in outcome.provisional
        (sandbox / ".mcp.json").unlink(missing_ok=True)
        assert_written(ctx, sandbox, outcome, slots)
