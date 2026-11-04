"""Two defects found by calling build.example.net's bridge after it was registered.

* **Every call answered 406.** The generated bridge asked for
  ``Accept: application/json`` and nothing else. Open Build Service serves XML,
  so it refused every request. The bridge now asks for what each operation
  documents (``responses.*.content`` or ``produces``), then the manifest's
  configured fallback, and never refuses a format.
* **A credential hid as a default.** The entry's auth variable was written
  ``${BUILD_EXAMPLE_NET_MCP_BRIDGE_AUTH_VALUE:-<token>}``: a reference to read, a
  literal in the file. The writer now strips a credential-shaped default
  whatever put it there.
"""

from __future__ import annotations

import http.server
import json
import subprocess
import sys
import threading

import pytest

from initialize_claude_ai.mcp_auto_discovery.bridge import _BRIDGE_SOURCE, BridgeBuilder, _media_types
from initialize_claude_ai.mcp_auto_discovery.types import Candidate, Transport
from initialize_claude_ai.mcp_auto_discovery.writer import McpWriter

# -- what an operation says it returns ------------------------------------------------


def test_openapi3_response_media_types_are_read() -> None:
    operation = {
        "responses": {
            "200": {"content": {"application/xml": {}, "text/plain": {}}},
            "404": {"content": {"application/json": {}}},
        }
    }
    assert _media_types(operation, {}) == ["application/xml", "text/plain"]


def test_swagger2_produces_is_read_from_the_operation_or_the_spec() -> None:
    assert _media_types({"produces": ["application/xml"]}, {}) == ["application/xml"]
    assert _media_types({}, {"produces": ["application/xml"]}) == ["application/xml"]


def test_the_manifest_carries_what_each_operation_documents(ctx) -> None:
    spec = {
        "openapi": "3.0.0",
        "servers": [{"url": "https://obs.example"}],
        "paths": {
            "/about": {"get": {"operationId": "get_about", "responses": {"200": {"content": {"application/xml": {}}}}}},
            "/plain": {"get": {"operationId": "get_plain", "responses": {"200": {}}}},
        },
    }
    operations = BridgeBuilder(ctx)._operations(spec, "https://obs.example")
    by_name = {t["name"]: t for t in operations["tools"]}
    assert by_name["get_about"]["accept"] == "application/xml"
    assert "accept" not in by_name["get_plain"], "nothing documented, so the manifest's fallback applies"


# -- the generated bridge against a server that serves only XML --------------------------


class XmlOnly(http.server.BaseHTTPRequestHandler):
    """Answers like OBS: XML, or 406 to a client that will not take it."""

    def do_GET(self):  # noqa: N802 - the stdlib's name
        accept = self.headers.get("Accept", "")
        if "xml" not in accept and "*/*" not in accept:
            self.send_response(406)
            self.end_headers()
            self.wfile.write(b'{"status":406,"error":"Not Acceptable"}')
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/xml")
        self.end_headers()
        self.wfile.write(b"<about><title>Open Build Service API</title></about>")

    def log_message(self, *_args):
        pass


@pytest.fixture(scope="module")
def xml_server():
    server = http.server.HTTPServer(("127.0.0.1", 0), XmlOnly)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def call_about(tmp_path, base_url: str, manifest_accept: str = "", tool_accept: str = "", env=None) -> str:
    from initialize_claude_ai.mcp_auto_discovery.bridge import MANIFEST_SCHEMA

    script = tmp_path / "bridge.py"
    script.write_text(_BRIDGE_SOURCE)
    tool = {
        "name": "get_about",
        "method": "GET",
        "path": "/about",
        "parameters": [],
        "inputSchema": {"type": "object", "properties": {}},
    }
    if tool_accept:
        tool["accept"] = tool_accept
    manifest = {
        "name": "t",
        "schema": MANIFEST_SCHEMA,
        "script": "bridge.py",
        "base_url": base_url,
        "operations": [tool],
    }
    if manifest_accept:
        manifest["accept"] = manifest_accept
    path = tmp_path / "t.operations.json"
    path.write_text(json.dumps(manifest))
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
                {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "get_about", "arguments": {}}},
            ]
        )
        + "\n"
    )
    run = subprocess.run(
        [sys.executable, str(script)],
        input=messages,
        capture_output=True,
        text=True,
        timeout=30,
        env={"MCP_BRIDGE_MANIFEST": str(path), "PATH": "/usr/bin:/bin", **(env or {})},
    )
    return run.stdout.strip().splitlines()[-1]


def test_a_bridge_with_no_preference_is_never_refused(tmp_path, xml_server) -> None:
    assert "Open Build Service API" in call_about(tmp_path, xml_server)


def test_the_operations_documented_type_is_asked_for(tmp_path, xml_server) -> None:
    out = call_about(tmp_path, xml_server, manifest_accept="application/json", tool_accept="application/xml")
    assert "Open Build Service API" in out


def test_the_operator_can_override_it(tmp_path, xml_server) -> None:
    out = call_about(tmp_path, xml_server, env={"MCP_BRIDGE_ACCEPT": "application/json"})
    assert "406" in out, "the operator's choice is what is sent"


def test_the_configured_fallback_takes_xml_too(ctx, tmp_path, xml_server) -> None:
    fallback = ctx.settings.str_("mcp.bridge.generated.accept")
    assert "Open Build Service API" in call_about(tmp_path, xml_server, manifest_accept=fallback)


# -- a credential hiding as a default ---------------------------------------------------


def test_a_credential_default_is_stripped_from_the_entry(ctx) -> None:
    token = "".join(
        chr(c)
        for c in (65, 98, 51, 100, 69, 53, 102, 71, 55, 104, 74, 57, 107, 76, 49, 109, 78, 50, 112, 81, 52, 114, 83, 54)
    )
    candidate = Candidate(
        kind="stdio",
        name="build-example-bridge",
        transport=Transport.STDIO,
        command=[sys.executable, "/srv/bridge.py"],
        env={
            "MCP_BRIDGE_BASE_URL": "https://build.example.de",
            "MCP_BRIDGE_AUTH_VALUE": "${BUILD_EXAMPLE_MCP_BRIDGE_AUTH_VALUE:-" + token + "}",
            "MCP_BRIDGE_TIMEOUT": "${BUILD_EXAMPLE_MCP_BRIDGE_TIMEOUT:-30}",
        },
    )
    entry = McpWriter(ctx).entry_for(candidate, "build-example-de")
    assert entry["env"]["MCP_BRIDGE_AUTH_VALUE"] == "${BUILD_EXAMPLE_MCP_BRIDGE_AUTH_VALUE}"
    assert token not in json.dumps(entry)
    assert entry["env"]["MCP_BRIDGE_TIMEOUT"] == "${BUILD_EXAMPLE_MCP_BRIDGE_TIMEOUT:-30}", "a setting stays"
    assert any("credential as the default" in n for n in candidate.notes)
