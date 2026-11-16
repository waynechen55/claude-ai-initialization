"""A server whose tool definitions the client rejects breaks the whole session.

openSUSE's osc-mcp declares an input property literally named
``path_repository jsonschema:`` on its ``search_packages`` tool. The Anthropic
API accepts property keys matching ``^[a-zA-Z0-9_.-]{1,64}$`` only, and refuses
the *whole request* otherwise -- so once that tool was loaded, every message of
the Claude Code session failed with "Property keys should match pattern",
whatever it asked for.

The program only asked whether a server answered. It now also asks whether what
the server advertises is something its client can load, by the client's rules
(``mcp.verification.client_name_pattern``), and a generated bridge never
advertises a name the client would reject.
"""

from __future__ import annotations

import http.server
import json
import subprocess
import sys
import threading
import urllib.parse

import pytest

from initialize_claude_ai.mcp_auto_discovery.bridge import _BRIDGE_SOURCE, MANIFEST_SCHEMA, BridgeBuilder
from initialize_claude_ai.mcp_auto_discovery.types import Candidate, TargetOutcome, Transport
from initialize_claude_ai.mcp_auto_discovery.verifier import CandidateVerifier
from initialize_claude_ai.mcp_auto_discovery.writer import McpWriter

BAD_KEY = "path_repository jsonschema:"

SERVER = """
import json, sys
tools = [
    {"name": "search_packages", "description": "search",
     "inputSchema": {"type": "object", "properties": {
         "query": {"type": "string"},
         %(bad)r: {"type": "string"},
         "filter": {"type": "object", "properties": {"bad nested": {"type": "string"}}}}}},
    {"name": "list_requests", "description": "",
     "inputSchema": {"type": "object", "properties": {"project": {"type": "string"}}}},
]
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    msg = json.loads(line)
    rid, method = msg.get("id"), msg.get("method")
    if rid is None:
        continue
    if method == "initialize":
        result = {"protocolVersion": msg["params"]["protocolVersion"], "capabilities": {"tools": {}},
                  "serverInfo": {"name": "build-example", "version": "0.2.1"}}
    elif method == "tools/list":
        result = {"tools": tools}
    else:
        result = {}
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": rid, "result": result}) + "\\n")
    sys.stdout.flush()
""" % {"bad": BAD_KEY}


@pytest.fixture
def live(ctx):
    ctx.dry_run = False
    ctx.runner.which = lambda name, **_: name  # type: ignore[assignment]
    return ctx


def osc_like(tmp_path) -> Candidate:
    script = tmp_path / "server.py"
    script.write_text(SERVER)
    return Candidate(
        kind="stdio",
        name="build-example-org",
        transport=Transport.STDIO,
        command=[sys.executable, str(script)],
        env={"API": "https://build.example.org"},
    )


def test_the_offending_keys_are_recorded_nested_ones_too(live, tmp_path) -> None:
    candidate = CandidateVerifier(live).verify(osc_like(tmp_path))
    keys = [p["key"] for p in candidate.handshake.schema_problems]
    assert BAD_KEY in keys and "bad nested" in keys
    assert {p["tool"] for p in candidate.handshake.schema_problems} == {"search_packages"}


def test_such_a_server_is_refused_by_default(live, tmp_path) -> None:
    candidate = CandidateVerifier(live).verify(osc_like(tmp_path))
    assert not candidate.verified
    assert "naming rules" in candidate.handshake.error and BAD_KEY in candidate.handshake.error


def test_warn_registers_it_with_the_problem_said(live, tmp_path, monkeypatch) -> None:
    live.settings.set("mcp.verification.schema_problems", "warn")
    monkeypatch.setattr(McpWriter, "_register", lambda self, r: None)
    candidate = CandidateVerifier(live).verify(osc_like(tmp_path))
    assert candidate.verified
    [registration] = McpWriter(live).publish(TargetOutcome(target="https://build.example.org", verified=[candidate]))
    assert not registration.refused
    assert any(BAD_KEY in line for line in registration.remediation)


def test_off_does_not_look(live, tmp_path) -> None:
    live.settings.set("mcp.verification.schema_problems", "off")
    candidate = CandidateVerifier(live).verify(osc_like(tmp_path))
    assert candidate.verified and candidate.handshake.schema_problems == []


def test_the_audit_names_the_tool_and_key(live, tmp_path) -> None:
    candidate = CandidateVerifier(live).verify(osc_like(tmp_path))
    [schema] = [c for c in CandidateVerifier.audit(candidate) if c.name == "schema"]
    assert schema.ok is False and BAD_KEY in schema.detail


def test_the_entry_as_written_is_refused_for_the_same_reason(live, tmp_path) -> None:
    candidate = osc_like(tmp_path)
    entry = McpWriter(live).entry_for(candidate, "build-example-org")
    shake = CandidateVerifier(live).verify_as_written(entry, "build-example-org")
    assert shake is not None and not shake.ok and "naming rules" in shake.error


def test_the_final_verdict_names_the_server(live, tmp_path) -> None:
    from initialize_claude_ai.core.report import PhaseResult, Status
    from initialize_claude_ai.mcp_auto_discovery.types import Stage, StageOutcome
    from initialize_claude_ai.phases.phase4_mcp_auto_discovery import McpPhase

    candidate = CandidateVerifier(live).verify(osc_like(tmp_path))
    outcome = TargetOutcome(target="https://build.example.org")
    outcome.stages.append(StageOutcome(stage=Stage.PACKAGE, status=Status.PARTIAL, candidates=[candidate]))
    phase = McpPhase(live)
    result = PhaseResult(phase=phase.slug, title=phase.title)
    phase._report_verdict(result, McpWriter(live), [outcome])
    assert "Tool definitions Claude Code's API rejects: build-example-org" in result.steps[-1].detail
    assert result.data["mcp_schema_problems"][0]["server"] == "build-example-org"


# -- a generated bridge never advertises a name the client rejects ----------------------


class Echo(http.server.BaseHTTPRequestHandler):
    seen: list[str] = []

    def do_GET(self):  # noqa: N802
        Echo.seen.append(self.path)
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *_args):
        pass


@pytest.fixture(scope="module")
def echo():
    server = http.server.HTTPServer(("127.0.0.1", 0), Echo)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def test_a_bridge_exposes_a_safe_name_and_sends_the_apis_own(ctx, tmp_path, echo) -> None:
    spec = {
        "openapi": "3.0.0",
        "servers": [{"url": echo}],
        "paths": {
            "/search": {
                "get": {
                    "operationId": "search",
                    "parameters": [
                        {"name": "package[]", "in": "query", "schema": {"type": "string"}},
                        {"name": "package_", "in": "query", "schema": {"type": "string"}},
                    ],
                    "responses": {"200": {}},
                }
            }
        },
    }
    operations = BridgeBuilder(ctx)._operations(spec, echo)
    [tool] = operations["tools"]
    keys = list(tool["inputSchema"]["properties"])
    assert all(k.replace("_", "a").replace(".", "a").replace("-", "a").isalnum() for k in keys), keys
    assert len(set(keys)) == 2, "a cleaned-up name never collides with a real one"
    arg = next(p["arg"] for p in tool["parameters"] if p["name"] == "package[]")

    script = tmp_path / "bridge.py"
    script.write_text(_BRIDGE_SOURCE)
    manifest = {"name": "t", "schema": MANIFEST_SCHEMA, "script": "bridge.py", "base_url": echo, "operations": [tool]}
    (tmp_path / "t.operations.json").write_text(json.dumps(manifest))
    messages = (
        "\n".join(
            json.dumps(m)
            for m in [
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {},
                        "clientInfo": {"name": "t", "version": "1"},
                    },
                },
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {"name": "search", "arguments": {arg: "qemu"}},
                },
            ]
        )
        + "\n"
    )
    subprocess.run(
        [sys.executable, str(script)],
        input=messages,
        capture_output=True,
        text=True,
        timeout=30,
        env={"MCP_BRIDGE_MANIFEST": str(tmp_path / "t.operations.json"), "PATH": "/usr/bin:/bin"},
    )
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(Echo.seen[-1]).query)
    assert query == {"package[]": ["qemu"]}, "the API receives its own parameter name"


def test_an_existing_registration_with_such_tools_is_reported_left_in_place(live, tmp_path, sandbox) -> None:
    from initialize_claude_ai.mcp_auto_discovery.traditional import TraditionalPipeline
    from initialize_claude_ai.mcp_auto_discovery.types import Stage

    script = tmp_path / "server.py"
    script.write_text(SERVER)
    (sandbox / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "build-example-org": {
                        "type": "stdio",
                        "command": sys.executable,
                        "args": [str(script)],
                        "env": {"API": "https://build.example.org"},
                    }
                }
            }
        )
    )
    live.settings.set("mcp.discovery.reuse.sources", ["project_file"])
    outcome = TargetOutcome(target="https://build.example.org")
    TraditionalPipeline(live).reuse(Stage.PACKAGE, outcome)
    [broken] = outcome.broken_existing
    assert broken["name"] == "build-example-org" and "naming rules" in broken["error"]
