"""A rejection says what it saw, and a reply that proves nothing is not a "no".

openqa.opensuse.org's endpoint was rejected on another machine with "response
was not a JSON-RPC message": a 2xx that was neither a refusal nor an answer.
The log did not say what came back, the verdict was recorded as "does not speak
MCP", and the one endpoint the target has was discarded -- although the auth
profile for that very host said it wants a credential. Separately, some
endpoints refuse with 200 or 400 and ``{"error":"no api key"}`` rather than 401,
and that was never read as a credential wall.
"""

from __future__ import annotations

from initialize_claude_ai.core.http import Response
from initialize_claude_ai.mcp_auto_discovery.agentic import AgenticPipeline
from initialize_claude_ai.mcp_auto_discovery.probes import McpProber
from initialize_claude_ai.mcp_auto_discovery.types import Candidate
from initialize_claude_ai.mcp_auto_discovery.verifier import CandidateVerifier

CONTROL = "initialize-claude-ai-control-"


class Client:
    """Answers POSTs from a script: one reply per call, the control path always 404."""

    def __init__(self, *replies: tuple[int, str, str]) -> None:
        self.replies = list(replies)
        self.asked: list[str] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def post(self, url, **_kwargs):
        if CONTROL in url:
            return Response(url=url, status=404, headers={"content-type": "text/plain"}, text="no")
        self.asked.append(url)
        status, content_type, text = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        return Response(url=url, status=status, headers={"content-type": content_type}, text=text)

    def stream_sse(self, url, **_kwargs):
        # The verifier's fallback for a URL of unknown transport; not an SSE server.
        return Response(url=url, status=404, headers={"content-type": "text/plain"}), []


def serve(ctx, monkeypatch, client: Client) -> Client:
    ctx.settings.set("mcp.discovery.inconclusive_retry_delay_seconds", 0)
    monkeypatch.setattr(ctx, "http", lambda **_kwargs: client)
    return client


PLACEHOLDER = (200, "text/html", "<html><body>Welcome to the service</body></html>")


def test_a_non_json_rpc_2xx_says_what_came_back(ctx, monkeypatch) -> None:
    serve(ctx, monkeypatch, Client(PLACEHOLDER))
    shake = McpProber(ctx).probe_http("https://svc.example.test/mcp")
    assert shake.inconclusive
    assert shake.error.startswith("response was not a JSON-RPC message (HTTP 200 text/html:")
    assert "Welcome to the service" in shake.error


def test_it_is_asked_once_more_before_it_is_recorded(ctx, monkeypatch) -> None:
    client = serve(ctx, monkeypatch, Client(PLACEHOLDER))
    McpProber(ctx).probe_http("https://svc.example.test/mcp")
    assert len(client.asked) == 2


def test_a_second_answer_replaces_the_first(ctx, monkeypatch) -> None:
    client = serve(ctx, monkeypatch, Client(PLACEHOLDER, (404, "text/plain", "gone")))
    shake = McpProber(ctx).probe_http("https://svc.example.test/mcp")
    assert not shake.inconclusive and shake.error == "HTTP 404"
    assert len(client.asked) == 2


def test_the_excerpt_is_redacted_and_bounded(ctx, monkeypatch) -> None:
    ctx.settings.set("mcp.discovery.reply_excerpt_chars", 20)
    serve(ctx, monkeypatch, Client((200, "text/plain", "x" * 500)))
    shake = McpProber(ctx).probe_http("https://svc.example.test/mcp")
    assert "x" * 20 + "..." in shake.error and "x" * 21 not in shake.error


# -- refusals that are not a 401 ------------------------------------------------


def test_a_200_saying_no_api_key_is_a_credential_wall(ctx, monkeypatch) -> None:
    serve(ctx, monkeypatch, Client((200, "application/json", '{"error":"no api key"}')))
    shake = McpProber(ctx).probe_http("https://svc.example.test/mcp")
    assert shake.requires_auth and shake.auth_evidence
    assert "the reply says 'no api key'" in shake.error


def test_a_400_saying_invalid_personal_access_token_is_one_too(ctx, monkeypatch) -> None:
    serve(ctx, monkeypatch, Client((400, "application/json", '{"error":"invalid personal access token"}')))
    shake = McpProber(ctx).probe_http("https://svc.example.test/mcp")
    assert shake.requires_auth and shake.auth_evidence


def test_a_login_page_saying_so_is_still_a_login_page(ctx, monkeypatch) -> None:
    page = (200, "text/html", "<html>Authentication required. Please log in.</html>")
    serve(ctx, monkeypatch, Client(page))
    shake = McpProber(ctx).probe_http("https://svc.example.test/mcp")
    candidate = Candidate(kind="url", url="https://svc.example.test/mcp", handshake=shake)
    assert not candidate.needs_credential


def test_an_unlisted_status_is_not_read(ctx, monkeypatch) -> None:
    serve(ctx, monkeypatch, Client((404, "application/json", '{"error":"no api key"}')))
    shake = McpProber(ctx).probe_http("https://svc.example.test/mcp")
    assert not shake.requires_auth and shake.error == "HTTP 404"


# -- an inconclusive reply, read in the light of what is known -------------------


def verify(ctx, monkeypatch, candidate: Candidate) -> Candidate:
    serve(ctx, monkeypatch, Client(PLACEHOLDER))
    return CandidateVerifier(ctx).verify(candidate)


def test_with_no_reason_to_expect_a_credential_it_stays_rejected(ctx, monkeypatch) -> None:
    candidate = verify(ctx, monkeypatch, Candidate(kind="url", url="https://svc.example.test/mcp"))
    assert not candidate.needs_credential and not candidate.verified


def test_an_auth_profile_for_the_host_makes_it_provisional(ctx, monkeypatch) -> None:
    candidate = verify(ctx, monkeypatch, Candidate(kind="url", url="https://openqa.example.test/mcp"))
    assert candidate.needs_credential, candidate.handshake.error
    assert not candidate.verified
    assert "auth profile" in candidate.handshake.error


def test_the_profile_rule_can_be_turned_off(ctx, monkeypatch) -> None:
    ctx.settings.set("mcp.discovery.auth.profile_is_auth_claim", False)
    candidate = verify(ctx, monkeypatch, Candidate(kind="url", url="https://openqa.example.test/mcp"))
    assert not candidate.needs_credential


def test_an_agents_claim_makes_it_provisional(ctx, monkeypatch) -> None:
    pipeline = AgenticPipeline(ctx)
    candidate, _ = pipeline._candidate_from(
        {
            "kind": "url",
            "url": "https://svc.example.test/mcp",
            "requires_auth": True,
            "auth_evidence": "the product docs say /mcp takes a personal access token",
            "secret_env": {"SVC_EXAMPLE_TEST_TOKEN": "env:SVC_EXAMPLE_TEST_TOKEN"},
        },
        origin="endpoint",
    )
    assert "discovery agent says it needs a credential" in candidate.auth_claim
    verify(ctx, monkeypatch, candidate)
    assert candidate.needs_credential
    assert not candidate.verified


def test_a_claim_never_overrules_a_real_answer(ctx, monkeypatch) -> None:
    serve(ctx, monkeypatch, Client((404, "text/plain", "gone")))
    candidate = Candidate(kind="url", url="https://svc.example.test/mcp", auth_claim="docs say so")
    CandidateVerifier(ctx).verify(candidate)
    assert not candidate.needs_credential


# -- the endpoint agent is told how to report an auth wall ----------------------


def test_the_endpoint_brief_names_the_profiles_variable(ctx) -> None:
    from initialize_claude_ai.mcp_auto_discovery.types import Stage, TargetOutcome

    target = TargetOutcome(target="openqa.example.test", base_url="https://openqa.example.test")
    brief = AgenticPipeline(ctx)._auth_wall_brief(Stage.ENDPOINT, target)
    assert '"requires_auth": true' in brief
    assert "OPENQA_EXAMPLE_TEST_" in brief and "env:OPENQA_EXAMPLE_TEST_" in brief
    assert "username:key:secret" in brief
    assert "{variable}" not in brief and "{shape}" not in brief


def test_no_other_stage_is_told(ctx) -> None:
    from initialize_claude_ai.mcp_auto_discovery.types import Stage, TargetOutcome

    target = TargetOutcome(target="svc.example.test", base_url="https://svc.example.test")
    assert AgenticPipeline(ctx)._auth_wall_brief(Stage.PACKAGE, target) == ""
