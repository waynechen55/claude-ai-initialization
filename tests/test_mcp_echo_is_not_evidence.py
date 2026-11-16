"""An echo of the request is not evidence about the server; an unproven candidate does not displace a working entry.

smelt.example.net answered ``GET /rest/servicedeskapi/info`` with its HTML 404
page, which repeats the requested path -- and the path was the success marker,
so smelt "served a Jira Service Management customer API" (any answer with a
status counted, not only a 2xx). A bridge was generated for that API; it
listed its tools, was recorded as waiting for a token, and was written over
the ``smelt-example-net`` entry for ``mcp-graphql`` that Claude Code reported as
connected. Its ``get_service_desk_info`` tool, which needs no login, would
have answered with the same 404 page.

The fake smelt below is a real HTTP server.
"""

from __future__ import annotations

import http.server
import json
import threading

import pytest

from initialize_claude_ai.core.http import Response
from initialize_claude_ai.mcp_auto_discovery import api_kinds, login_check
from initialize_claude_ai.mcp_auto_discovery.bridge import BridgeBuilder
from initialize_claude_ai.mcp_auto_discovery.traditional import TraditionalPipeline
from initialize_claude_ai.mcp_auto_discovery.types import Candidate, Handshake, Mode, Stage, StageOutcome, TargetOutcome
from initialize_claude_ai.mcp_auto_discovery.verifier import CandidateVerifier

INFO = "/rest/servicedeskapi/info"
SCHEMA = {"data": {"__schema": {"queryType": {"name": "Query"}}}}


def page_404(path: str) -> bytes:
    return f"<!DOCTYPE html><html><body><h1>Not Found</h1><p>{path} was not found</p></body></html>".encode()


class Smelt(http.server.BaseHTTPRequestHandler):
    """GraphQL at /graphql/; an HTML 404 that repeats the path everywhere else."""

    def _send(self, status: int, payload: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):  # noqa: N802
        self._send(404, page_404(self.path), "text/html; charset=utf-8")

    def do_POST(self):  # noqa: N802
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if self.path == "/graphql/":
            return self._send(200, json.dumps(SCHEMA).encode(), "application/json")
        self._send(404, page_404(self.path), "text/html; charset=utf-8")

    def log_message(self, *_args):
        pass


@pytest.fixture(scope="module")
def smelt():
    server = http.server.HTTPServer(("127.0.0.1", 0), Smelt)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def kind(settings, name: str) -> api_kinds.ApiKind:
    return next(k for k in api_kinds.kinds(settings) if k.name == name)


def reply(url: str, status: int, text: str, content_type: str) -> Response:
    return Response(url=url, status=status, headers={"content-type": content_type}, text=text)


class Fake:
    """``answer(url) -> (status, text, content type)`` for every request."""

    def __init__(self, answer) -> None:
        self.answer, self.sent = answer, []

    def send(self, method, url, content_type, body):
        self.sent.append(url)
        return reply(url, *self.answer(url))

    def fetch(self, url):
        return reply(url, 404, "", "text/html")


def detect(settings, name: str, fake: Fake) -> str:
    return api_kinds.detect(kind(settings, name), "https://x.example.test", send=fake.send, fetch=fake.fetch)


# -- recognising an API kind ------------------------------------------------------------------


def test_a_404_page_that_repeats_the_path_is_not_the_api(settings) -> None:
    fake = Fake(lambda url: (404, page_404(url).decode(), "text/html"))
    assert detect(settings, "jira_service_management", fake) == ""


def test_a_200_html_page_is_not_the_api_whatever_it_says(settings) -> None:
    text = '<html>{"version": "1", "platformVersion": "2", "_links": {"self": "x"}} "__schema"</html>'
    fake = Fake(lambda url: (200, text, "text/html"))
    assert detect(settings, "jira_service_management", fake) == ""
    assert detect(settings, "graphql", fake) == ""


def test_a_server_answering_every_path_alike_is_caught_by_the_control(settings) -> None:
    body = json.dumps({"version": "1", "platformVersion": "2", "_links": {"self": "x"}})
    fake = Fake(lambda url: (200, body, "application/json"))
    assert detect(settings, "jira_service_management", fake) == ""
    assert any(api_kinds.DEFAULT_CONTROL_NAME in url for url in fake.sent)


def test_the_real_answers_are_still_recognised(settings) -> None:
    info = json.dumps(
        {"version": "5.12", "platformVersion": "9.12", "_links": {"self": "https://x.example.test" + INFO}}
    )
    fake = Fake(
        lambda url: (
            (200, info, "application/json;charset=UTF-8") if url.endswith(INFO) else (404, "", "application/xml")
        )
    )
    assert detect(settings, "jira_service_management", fake).endswith(INFO)

    fake = Fake(
        lambda url: (
            (200, json.dumps(SCHEMA), "application/json") if url.endswith("/graphql/") else (404, "", "text/html")
        )
    )
    assert detect(settings, "graphql", fake).endswith("/graphql/")


def test_a_marker_the_request_carries_never_counts(settings) -> None:
    settings.set(
        "mcp.discovery.api_kinds.echoed",
        {"label": "Echoed", "paths": ["/api/thing"], "method": "GET", "success_markers": ["/api/thing"]},
    )
    echoed = kind(settings, "echoed")
    assert echoed.evidence("https://x.example.test/api/thing") == []
    fake = Fake(lambda url: (200, f"you asked for {url}", "text/plain"))
    assert detect(settings, "echoed", fake) == ""


def test_the_control_can_be_turned_off(settings) -> None:
    body = json.dumps({"version": "1", "platformVersion": "2", "_links": {"self": "x"}})
    fake = Fake(lambda url: (200, body, "application/json"))
    jsm = kind(settings, "jira_service_management")
    assert api_kinds.detect(jsm, "https://x.example.test", send=fake.send, fetch=fake.fetch, control="")


def test_smelt_serves_graphql_and_not_the_customer_api(ctx, smelt) -> None:
    pipeline = TraditionalPipeline(ctx)
    target = TargetOutcome(target=smelt)
    found = pipeline.api_kinds_for(target)
    assert "graphql" in found and "jira_service_management" not in found


def test_an_agent_s_recorded_kind_is_confirmed_before_it_is_used(ctx, smelt) -> None:
    pipeline = TraditionalPipeline(ctx)
    target = TargetOutcome(target=smelt)
    target.stages.append(
        StageOutcome(
            stage=Stage.ENDPOINT,
            mode=Mode.AGENTIC,
            findings={
                "origin_status": 200,
                "api_kinds": {
                    "jira_service_management": {"url": smelt + INFO},
                    "graphql": {"url": smelt + "/graphql/"},
                },
            },
        )
    )
    found = pipeline.api_kinds_for(target)
    assert set(found) == {"graphql"}


# -- proving a generated bridge ------------------------------------------------------------------


@pytest.fixture
def live(ctx, tmp_path, monkeypatch):
    ctx.dry_run = False
    ctx.settings.set("mcp.bridge.output_dir", str(tmp_path / "bridges"))
    ctx.settings.set("mcp.packages.trial_timeout_seconds", 20)
    ctx.settings.set("mcp.discovery.reuse.sources", [])
    return ctx


def bridge_at(live, origin: str) -> Candidate:
    jsm = kind(live.settings, "jira_service_management")
    candidate, _, detail = BridgeBuilder(live).generate_from_openapi(
        origin, api_kinds.spec_for(jsm, origin + INFO), base_url=origin
    )
    assert candidate is not None, detail
    candidate.api_kind = jsm.name
    return candidate


def test_the_public_operation_is_configured(settings) -> None:
    assert api_kinds.public_calls(settings, "jira_service_management") == [
        {"tool": "get_service_desk_info", "required_keys": ["version", "platformVersion", "_links.self"]}
    ]


def test_a_bridge_whose_public_call_returns_an_error_page_is_broken_not_waiting(live, smelt) -> None:
    candidate = CandidateVerifier(live).verify(bridge_at(live, smelt))
    shake = candidate.handshake
    assert shake.public_check["answered"] is False
    assert not shake.ok and not shake.credential_pending and "broken" in shake.error
    assert not candidate.verified and not candidate.needs_credential


@pytest.mark.parametrize(
    ("text", "answered"),
    [
        (json.dumps({"version": "1", "platformVersion": "2", "_links": {"self": "x"}}), True),
        ('{"version": "1"}', False),
        ("<html>404</html>", False),
    ],
)
def test_what_a_public_answer_must_hold(text, answered) -> None:
    keys = ["version", "platformVersion", "_links.self"]
    assert api_kinds.public_answer({"result": {"content": [{"type": "text", "text": text}]}}, keys)[0] is answered
    assert (
        api_kinds.public_answer({"result": {"isError": True, "content": [{"type": "text", "text": text}]}}, keys)[0]
        is False
    )


def test_an_agent_names_the_kind_of_a_bridge_it_wrote(ctx) -> None:
    from initialize_claude_ai.mcp_auto_discovery.agentic import AgenticPipeline

    pipeline = AgenticPipeline(ctx)
    raw = {"kind": "stdio", "name": "x", "command": ["python3", "bridge.py"]}
    candidate, _ = pipeline._candidate_from({**raw, "api_kind": "jira_service_management"}, origin="bridge")
    assert candidate is not None and candidate.api_kind == "jira_service_management"
    candidate, _ = pipeline._candidate_from({**raw, "api_kind": "nothing_configured"}, origin="bridge")
    assert candidate is not None and candidate.api_kind == ""


# -- a working entry keeps its place -----------------------------------------------------------


@pytest.fixture
def held(ctx, sandbox):
    from initialize_claude_ai.mcp_auto_discovery.writer import McpWriter, Registration

    ctx.settings.set("mcp.mcp_json_path", str(sandbox / ".mcp.json"))
    (sandbox / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "smelt-example-test": {
                        "command": "npx",
                        "args": ["-y", "mcp-graphql@2.0.4"],
                        "env": {"ENDPOINT": "https://smelt.example.test/graphql/"},
                    },
                }
            }
        )
    )
    outcome = TargetOutcome(target="https://smelt.example.test")
    outcome.answered_existing["smelt-example-test"] = "project_file"
    bridge = Candidate(kind="stdio", name="smelt-example-test-bridge", command=["python3", "bridge.py"])
    bridge.handshake = Handshake(
        ok=True, tools=["get_request"], round_trip=True, credential_pending=["SMELT_EXAMPLE_TEST_TOKEN"]
    )
    registration = Registration(
        name="smelt-example-test", candidate=bridge, entry={"command": "python3", "args": ["bridge.py"]}
    )
    return McpWriter(ctx), registration, outcome


def test_a_waiting_candidate_does_not_overwrite_an_entry_that_answered(held) -> None:
    writer, registration, outcome = held
    why = writer._holds_its_place(registration, outcome, {})
    assert "answered during this run" in why and "SMELT_EXAMPLE_TEST_TOKEN" in why


def test_a_proven_candidate_or_an_unusable_entry_lets_it_through(held) -> None:
    writer, registration, outcome = held
    assert writer._holds_its_place(registration, outcome, {"smelt-example-test": (None, "does not start")}) == ""
    registration.candidate.handshake.login = login_check.ACCEPTED
    assert writer._holds_its_place(registration, outcome, {}) == ""


def test_keeping_it_can_be_turned_off(held) -> None:
    from initialize_claude_ai.mcp_auto_discovery.writer import McpWriter

    writer, registration, outcome = held
    writer.ctx.settings.set("mcp.registration.keep_answering_existing", False)
    assert McpWriter(writer.ctx)._holds_its_place(registration, outcome, {}) == ""


def test_packaged_settings_equal_the_code(settings) -> None:
    assert settings.str_("mcp.discovery.api_kind_control_name") == api_kinds.DEFAULT_CONTROL_NAME
    assert settings.bool_("mcp.registration.keep_answering_existing") is True
