"""A WebSocket is an MCP endpoint only when something says it carries MCP.

openqa.opensuse.org's ``/api/v1/ws/`` answers ``404`` with an
``Upgrade: websocket`` header: it is the web UI's live-update socket. The probe
counted the header as "a native endpoint that needs a bridge", and that one
record closed the package tier and asked for a relay to a socket that has never
spoken JSON-RPC. The same was true of any agent that reported such a route in
``websocket_urls``: its word was taken without a probe.
"""

from __future__ import annotations

from initialize_claude_ai.core.http import Response
from initialize_claude_ai.mcp_auto_discovery.agentic import AgenticPipeline
from initialize_claude_ai.mcp_auto_discovery.policy import DueDiligence, TierPolicy
from initialize_claude_ai.mcp_auto_discovery.probes import (
    WEBSOCKET_HINT,
    WEBSOCKET_UNPROVEN_HINT,
    McpProber,
)
from initialize_claude_ai.mcp_auto_discovery.runtime import AgentResult
from initialize_claude_ai.mcp_auto_discovery.traditional import TraditionalPipeline
from initialize_claude_ai.mcp_auto_discovery.types import Stage, StageOutcome, TargetOutcome


class Client:
    """Answers every GET with one canned response, and remembers what it was sent."""

    def __init__(self, status: int, headers: dict[str, str] | None = None) -> None:
        self.status, self.headers, self.sent = status, headers or {}, []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get(self, url, headers=None, **_kwargs):
        self.sent.append((url, dict(headers or {})))
        return Response(url=url, status=self.status, headers=dict(self.headers))


def serve(ctx, monkeypatch, client: Client) -> Client:
    monkeypatch.setattr(ctx, "http", lambda **_kwargs: client)
    return client


def test_a_refusal_carrying_an_upgrade_header_is_not_an_upgrade(ctx, monkeypatch) -> None:
    serve(ctx, monkeypatch, Client(404, {"upgrade": "websocket"}))
    shake = McpProber(ctx).probe_websocket("https://openqa.example.test/api/v1/ws")
    assert WEBSOCKET_HINT not in shake.protocol_hints
    assert "no WebSocket upgrade (HTTP 404)" in shake.error


def test_a_bare_upgrade_is_a_hint_and_nothing_more(ctx, monkeypatch) -> None:
    serve(ctx, monkeypatch, Client(101, {"upgrade": "websocket"}))
    shake = McpProber(ctx).probe_websocket("https://host.example.test/api/v1/ws")
    assert WEBSOCKET_HINT not in shake.protocol_hints
    assert WEBSOCKET_UNPROVEN_HINT in shake.protocol_hints
    assert "closes no tier" in shake.error


def test_an_accepted_mcp_subprotocol_is_evidence(ctx, monkeypatch) -> None:
    client = serve(ctx, monkeypatch, Client(101, {"upgrade": "websocket", "sec-websocket-protocol": "mcp"}))
    shake = McpProber(ctx).probe_websocket("https://host.example.test/api/v1/ws")
    assert WEBSOCKET_HINT in shake.protocol_hints
    assert client.sent[0][1].get("sec-websocket-protocol") == "mcp", "the subprotocol is offered"


def test_a_route_named_for_mcp_is_evidence(ctx, monkeypatch) -> None:
    serve(ctx, monkeypatch, Client(101, {"upgrade": "websocket"}))
    shake = McpProber(ctx).probe_websocket("https://host.example.test/mcp/ws")
    assert WEBSOCKET_HINT in shake.protocol_hints


def test_the_evidence_rule_can_be_turned_off(ctx, monkeypatch) -> None:
    ctx.settings.set("mcp.discovery.websocket_requires_mcp_evidence", False)
    serve(ctx, monkeypatch, Client(101, {"upgrade": "websocket"}))
    shake = McpProber(ctx).probe_websocket("https://host.example.test/api/v1/ws")
    assert WEBSOCKET_HINT in shake.protocol_hints


# -- an agent's word is checked the same way ------------------------------------


def agent_reports(ctx, url: str) -> tuple[AgenticPipeline, TargetOutcome]:
    pipeline = AgenticPipeline(ctx)
    target = TargetOutcome(target="https://host.example.test")
    payload = {"findings": {"websocket_urls": [url]}}
    pipeline._ingest(
        StageOutcome(stage=Stage.ENDPOINT, mode="agentic"),
        AgentResult(ok=True, runtime="t", payload=payload),
        target,
    )
    return pipeline, target


def test_an_agent_reported_socket_with_no_sign_of_mcp_is_retracted(ctx, monkeypatch) -> None:
    serve(ctx, monkeypatch, Client(101, {"upgrade": "websocket"}))
    pipeline, target = agent_reports(ctx, "wss://host.example.test/api/v1/ws")
    assert target.due_diligence.needs_bridge, "recorded on the agent's word first"

    pipeline.fallback.vet_record(target)

    assert target.due_diligence.needs_bridge == []
    assert "wss://host.example.test/api/v1/ws" in target.due_diligence.ruled_out


def test_an_agent_reported_socket_that_carries_mcp_is_kept(ctx, monkeypatch) -> None:
    client = serve(ctx, monkeypatch, Client(101, {"upgrade": "websocket", "sec-websocket-protocol": "mcp"}))
    pipeline, target = agent_reports(ctx, "wss://host.example.test/api/v1/ws")

    pipeline.fallback.vet_record(target)
    pipeline.fallback.vet_record(target)

    assert target.due_diligence.needs_bridge == ["wss://host.example.test/api/v1/ws"]
    assert client.sent[0][0] == "https://host.example.test/api/v1/ws", "probed over https"
    assert len(client.sent) == 1, "asked once per run"


def test_the_traditional_probe_verdict_is_reused(ctx, monkeypatch) -> None:
    client = serve(ctx, monkeypatch, Client(101, {"upgrade": "websocket"}))
    pipeline = TraditionalPipeline(ctx)
    url = "https://host.example.test/api/v1/ws"
    pipeline.remember_socket(url, pipeline.prober.probe_websocket(url))
    assert pipeline.socket_verdict(url)
    assert len(client.sent) == 1


# -- the gate says what it saw ---------------------------------------------------


def test_the_gate_names_a_bridge_needing_endpoint_for_what_it_is(ctx) -> None:
    record = DueDiligence(target="https://host.example.test")
    record.note("needs_bridge", ["https://host.example.test/mcp/ws"])
    allowed, why = TierPolicy(ctx).gate(Stage.PACKAGE, record)
    assert not allowed
    assert "needs a bridge" in why
    assert "behind authentication" not in why
