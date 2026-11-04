"""build-opensuse-org vanished from .mcp.json: its fresh client bridge was refused because "the existing
build-opensuse-org ranks above it on what is known" -- an existing copy that was unusable (Claude Code
reported it fails to connect), was the very same client bridge, and ranked above only on the default
user name its slot carried. The keep rule had none of the older rules' guards, and the final file
check reported the empty address instead of repairing it."""

from __future__ import annotations

import json

from initialize_claude_ai.mcp_auto_discovery.types import Candidate, Handshake, TargetOutcome

BRIDGE = ["python3.13", "/opt/mcp-servers/build-example-test/mcp_client_bridge_build_example_test.py"]


def _bridge(env: dict, pending: list[str]) -> Candidate:
    c = Candidate(
        kind="stdio",
        name="build-example-test",
        command=list(BRIDGE),
        env={"MCP_CLIENT_MANIFEST": "/opt/mcp-servers/build-example-test/m.client.json", **env},
        secret_env={p: f"env:{p}" for p in pending},
    )
    c.handshake = Handshake(ok=True, tools=["whoami"], credential_pending=list(pending))
    c.evidence = (1, 0, len(pending))
    return c


def _writer(ctx, monkeypatch, copies):
    from initialize_claude_ai.mcp_auto_discovery.writer import McpWriter

    writer = McpWriter(ctx)
    monkeypatch.setattr(writer, "_existing_candidates", lambda o, n: list(copies))
    monkeypatch.setattr(writer, "_keepable", lambda o, n: True)
    return writer


def _registration(candidate):
    from initialize_claude_ai.mcp_auto_discovery.writer import Registration

    return Registration(
        name="build-example-test", candidate=candidate, entry={"command": BRIDGE[0], "args": BRIDGE[1:]}
    )


def test_the_same_server_refreshes_its_existing_copy(ctx, monkeypatch) -> None:
    old = _bridge({"BUILD_EXAMPLE_TEST_USERNAME": "someone"}, ["BUILD_EXAMPLE_TEST_PASSWORD"])
    new = _bridge({}, ["BUILD_EXAMPLE_TEST_USERNAME", "BUILD_EXAMPLE_TEST_PASSWORD"])
    writer = _writer(ctx, monkeypatch, [old])
    outcome = TargetOutcome(target="https://build.example.test")
    outcome.answered_existing = {"build-example-test": "claude_cli"}
    assert writer._holds_its_place(_registration(new), outcome, {}) == ""


def test_an_unusable_existing_copy_holds_no_place(ctx, monkeypatch) -> None:
    proxy = Candidate(
        kind="stdio",
        name="build-example-test",
        command=["uvx", "mcp-openapi-proxy@0.4.0"],
        env={"BUILD_EXAMPLE_TEST_USERNAME": "someone"},
        worked_before="once",
    )
    proxy.handshake = Handshake(ok=True, tools=["t"], credential_pending=["EXTRA_HEADERS"])
    proxy.evidence = (1, 0, 0)
    new = _bridge({}, ["BUILD_EXAMPLE_TEST_USERNAME", "BUILD_EXAMPLE_TEST_PASSWORD"])
    writer = _writer(ctx, monkeypatch, [proxy])
    outcome = TargetOutcome(target="https://build.example.test")
    unusable = {"build-example-test": (proxy, "Claude Code reports it fails to connect")}
    assert writer._holds_its_place(_registration(new), outcome, unusable) == ""


def test_values_an_existing_entry_supplies_count_for_both_when_compared(ctx, monkeypatch) -> None:
    other = Candidate(
        kind="stdio",
        name="build-example-test",
        command=["python3", "other.py"],
        env={"BUILD_EXAMPLE_TEST_USERNAME": "someone", "MCP_CLIENT_MANIFEST": "/opt/other.client.json"},
        secret_env={"BUILD_EXAMPLE_TEST_PASSWORD": "env:BUILD_EXAMPLE_TEST_PASSWORD"},
    )
    other.handshake = Handshake(ok=True, tools=["t"], credential_pending=["BUILD_EXAMPLE_TEST_PASSWORD"])
    other.evidence = (1, 0, 1)
    new = _bridge({}, ["BUILD_EXAMPLE_TEST_USERNAME", "BUILD_EXAMPLE_TEST_PASSWORD"])
    writer = _writer(ctx, monkeypatch, [other])
    assert not writer._ranks_above(other, new), "the user name it supplies is carried into what is written"
    ctx.settings.set("mcp.registration.compare_after_carrying", False)
    assert _writer(ctx, monkeypatch, [other])._ranks_above(other, new)


def test_an_address_is_never_left_empty_when_a_held_back_candidate_waits(ctx, sandbox, monkeypatch) -> None:
    from initialize_claude_ai.mcp_auto_discovery.writer import McpWriter, Registration

    ctx.dry_run = False
    ctx.settings.set("mcp.mcp_json_path", str(sandbox / ".mcp.json"))
    writer = McpWriter(ctx)
    new = _bridge({}, ["BUILD_EXAMPLE_TEST_PASSWORD"])
    held = Registration(
        name="build-example-test",
        candidate=new,
        entry={"command": "python3", "args": ["b.py"]},
        target="https://build.example.test",
        refused="the existing one ranks above",
        held_back=True,
    )
    gated = Registration(
        name="build-example-test-x",
        candidate=new,
        entry={"command": "x"},
        target="https://build.example.test",
        refused="it names no address at all",
    )
    writer.published += [gated, held]
    outcome = TargetOutcome(target="https://build.example.test", provisional=[new])
    problems = writer.missing_from_file(outcome)
    servers = json.loads((sandbox / ".mcp.json").read_text())["mcpServers"]
    assert "build-example-test" in servers and "build-example-test-x" not in servers
    assert any("not left empty" in p for p in problems)


def test_packaged_settings_equal_the_code(settings) -> None:
    reg = settings.section("mcp.registration")
    assert reg.bool_("one_keep_gate") and reg.bool_("compare_after_carrying") and reg.bool_("never_an_empty_address")
