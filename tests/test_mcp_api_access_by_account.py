"""A server the account cannot use is not the answer -- one for an API it can use is.

sd.example.com: the token was valid (the Service Desk API answered it), but the
account is a service-desk *customer*, so every call mcp-atlassian makes to the
core Jira API was sent to the customer portal. The run registered that server,
settled the address on it, and searched no further; no server spoke the API the
account could use, and nothing could be generated without a description of it.

Now: an account with *no API access* does not settle the descent; the target's
other API is recognised (``mcp.discovery.api_kinds.<name>``) and, with no
server of its own, bridged from its documented operations; the token this
machine already sends that host is tried on the bridge (in memory, read-only);
and the bridge takes the unusable server's place -- in the file and in the CLI.

The fake instance below is a real HTTP server.
"""

from __future__ import annotations

import http.server
import json
import threading

import pytest

from initialize_claude_ai.core.secrets import Secret
from initialize_claude_ai.mcp_auto_discovery import api_kinds, login_check
from initialize_claude_ai.mcp_auto_discovery.bridge import BridgeBuilder
from initialize_claude_ai.mcp_auto_discovery.existing import ExistingRegistrations
from initialize_claude_ai.mcp_auto_discovery.traditional import TraditionalPipeline
from initialize_claude_ai.mcp_auto_discovery.types import Candidate, Handshake, StageOutcome, TargetOutcome
from initialize_claude_ai.mcp_auto_discovery.verifier import CandidateVerifier

TOKEN = "".join(chr(c) for c in (99, 117, 115, 116, 111, 109, 101, 114, 45, 112, 97, 116, 45, 57, 57))


class ServiceDesk(http.server.BaseHTTPRequestHandler):
    """Core API -> the customer portal; customer API -> answers the token."""

    def _send(self, status: int, body: object, headers: dict | None = None) -> None:
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):  # noqa: N802
        base = f"http://{self.headers.get('Host')}"
        if self.path.startswith("/rest/servicedeskapi/info"):
            return self._send(
                200,
                {"version": "5.12", "platformVersion": "9.12", "_links": {"self": base + "/rest/servicedeskapi/info"}},
            )
        if self.path.startswith("/rest/servicedeskapi/"):
            if self.headers.get("Authorization") != f"Bearer {TOKEN}":
                return self._send(401, {"errorMessage": "unauthorized"})
            return self._send(200, {"size": 1, "values": [{"issueKey": "SD-204666"}]})
        return self._send(500, {"message": f"500 Server Error: for url: {base}/servicedesk/customer/portals"})

    def log_message(self, *_args):
        pass


@pytest.fixture(scope="module")
def desk():
    server = http.server.HTTPServer(("127.0.0.1", 0), ServiceDesk)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


@pytest.fixture
def live(ctx, sandbox, tmp_path, monkeypatch, desk):
    ctx.dry_run = False
    ctx.settings.set("mcp.bridge.output_dir", str(tmp_path / "bridges"))
    ctx.settings.set("mcp.packages.trial_timeout_seconds", 20)
    ctx.settings.set("mcp.discovery.reuse.sources", ["project_file"])
    ctx.settings.set("mcp.mcp_json_path", str(sandbox / ".mcp.json"))
    ctx.settings.set("mcp.discovery.api_kinds_same_host_only", False)
    for name in ("SD_TOKEN", "JIRA_PERSONAL_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    (sandbox / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "desk": {
                        "type": "stdio",
                        "command": "uvx",
                        "args": ["mcp-atlassian"],
                        "env": {"JIRA_URL": desk, "JIRA_PERSONAL_TOKEN": TOKEN},
                    },
                }
            }
        )
    )
    ctx.state.pop("mcp_existing_registrations", None)
    return ctx


def jsm(settings):
    return next(k for k in api_kinds.kinds(settings) if k.name == "jira_service_management")


# -- recognising and describing the API the account can use --------------------------------


def test_the_customer_api_is_recognised_without_a_login(live, desk) -> None:
    def send(method, url, content_type, body):
        with live.http(timeout=5) as client:
            return client.get(url)

    assert api_kinds.detect(jsm(live.settings), desk, send=send, fetch=lambda url: None).endswith(
        "/rest/servicedeskapi/info"
    )


def test_its_operations_become_a_description_with_its_credential_method(live, desk) -> None:
    spec = api_kinds.spec_for(jsm(live.settings), desk + "/rest/servicedeskapi/info")
    assert spec["servers"] == [{"url": desk}]
    assert "/rest/servicedeskapi/request/{issueIdOrKey}" in spec["paths"]
    assert spec["components"]["securitySchemes"]["jira_service_management"]["scheme"] == "bearer"


# -- the bridge, proved with the token this machine already sends that host -----------------


def bridge_for(live, desk) -> Candidate:
    spec = api_kinds.spec_for(jsm(live.settings), desk + "/rest/servicedeskapi/info")
    candidate, artifacts, detail = BridgeBuilder(live).generate_from_openapi(desk, spec, base_url=desk)
    assert candidate is not None, detail
    return candidate


def test_the_bridge_is_checked_with_the_token_already_in_use_for_the_host(live, desk) -> None:
    candidate = CandidateVerifier(live).verify(bridge_for(live, desk))
    shake = candidate.handshake

    assert shake.ok and shake.credential_pending, "its own variable is unset"
    assert shake.login == login_check.ACCEPTED, shake.login_detail
    assert shake.login_tool == "get_my_requests", "the identity-like listing, not the public info call"
    assert "already uses for 127.0.0.1" in shake.login_source
    assert TOKEN not in json.dumps(shake.to_dict(), default=str)
    assert TOKEN not in " ".join(candidate.notes)


def test_borrowing_can_be_turned_off(live, desk) -> None:
    live.settings.set("mcp.verification.login_check.borrow_in_use", False)
    candidate = CandidateVerifier(live).verify(bridge_for(live, desk))
    assert candidate.handshake.login == ""


def test_borrowing_needs_one_value_for_the_host(live, desk, sandbox) -> None:
    (sandbox / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "a": {"command": "uvx", "args": ["x"], "env": {"JIRA_URL": desk, "JIRA_PERSONAL_TOKEN": TOKEN}},
                    "b": {
                        "command": "uvx",
                        "args": ["y"],
                        "env": {"JIRA_URL": desk, "JIRA_PERSONAL_TOKEN": TOKEN + "x"},
                    },
                }
            }
        )
    )
    live.state.pop("mcp_existing_registrations", None)
    assert len(ExistingRegistrations.for_run(live).credentials_for("127.0.0.1")) == 2
    candidate = CandidateVerifier(live).verify(bridge_for(live, desk))
    assert candidate.handshake.login == "", "two tokens for one host: nothing says which to try"


# -- no API access does not end the search, and gives up its place ---------------------------


def no_access(label: str = "desk") -> Candidate:
    candidate = Candidate(kind="stdio", name=label, command=["uvx", "mcp-atlassian"])
    candidate.handshake = Handshake(
        ok=True, tools=["t"], round_trip=True, credential_pending=[], login=login_check.NO_ACCESS, login_refused=True
    )
    return candidate


def waiting(label: str) -> Candidate:
    candidate = Candidate(kind="stdio", name=label, command=["python3", "bridge.py"])
    candidate.handshake = Handshake(
        ok=True, tools=["t"], round_trip=True, credential_pending=["SD_TOKEN"], login=login_check.ACCEPTED
    )
    return candidate


def test_no_api_access_keeps_the_descent_going(live) -> None:
    outcome = TargetOutcome(target="https://sd.example.test", provisional=[no_access()])
    assert TraditionalPipeline(live).settled("bridge", outcome, exhaustive=False) is None
    live.settings.set("mcp.verification.login_check.no_access_settles", True)
    assert TraditionalPipeline(live).settled("bridge", outcome, exhaustive=False) is not None


def test_a_rejected_credential_still_settles(live) -> None:
    rejected = no_access()
    rejected.handshake.login = login_check.REJECTED
    outcome = TargetOutcome(target="https://sd.example.test", provisional=[rejected])
    assert TraditionalPipeline(live).settled("bridge", outcome, exhaustive=False) is not None


def test_the_unusable_server_gives_up_its_slot(live) -> None:
    outcome = TargetOutcome(target="https://sd.example.test")
    outcome.absorb(StageOutcome(stage="package", candidates=[no_access()]))
    replacement = waiting("bridge")
    outcome.absorb(StageOutcome(stage="bridge", candidates=[replacement]))
    assert outcome.provisional == [replacement]


def test_an_agent_stage_with_only_unusable_servers_is_not_conclusive(live) -> None:
    from initialize_claude_ai.mcp_auto_discovery.agentic import AgenticPipeline

    stage = StageOutcome(stage="package", candidates=[no_access()])
    stage.provisional = [no_access()]
    assert AgenticPipeline(live)._stage_is_conclusive(stage) is False


def test_the_replacement_takes_the_unusable_registration_s_place(live, desk) -> None:
    from initialize_claude_ai.mcp_auto_discovery.writer import McpWriter

    existing = ExistingRegistrations.for_run(live)
    old = existing.candidate(existing.registrations()[0], "package")
    old.handshake = no_access().handshake
    replacement = waiting("desk")
    outcome = TargetOutcome(target=desk, provisional=[replacement])
    outcome.stages.append(StageOutcome(stage="package", candidates=[old]))
    live.settings.set("mcp.registration.verify_as_written", False)
    writer = McpWriter(live)
    assert writer._unusable_existing(outcome)["desk"][0] is old


def test_the_in_use_value_is_a_secret(live) -> None:
    values = ExistingRegistrations.for_run(live).credentials_for("127.0.0.1")
    assert values and all(isinstance(v, Secret) for v in values)
    assert TOKEN not in repr(values)
