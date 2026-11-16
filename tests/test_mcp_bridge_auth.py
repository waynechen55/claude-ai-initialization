"""A generated bridge asks for the credential the API uses, by names a person can fill.

build.example.net's bridge was registered with ``MCP_BRIDGE_AUTH_HEADER`` and
``MCP_BRIDGE_AUTH_VALUE``. The server had answered ``WWW-Authenticate: basic
realm="API login"``, so the answer was a user name and a password -- but the
names said "a header" and "a value", the value had to be "Basic " plus the
base64 of ``user:password`` typed by hand, and the one there could never have
logged in. The method now comes from evidence (the API description, else the
server's 401), each part is named after the target and described, the bridge
builds the header itself, and a login is checked when the parts are exported.
"""

from __future__ import annotations

import base64
import http.server
import json
import subprocess
import sys
import threading

import pytest

from initialize_claude_ai.mcp_auto_discovery import bridge_auth
from initialize_claude_ai.mcp_auto_discovery.bridge import _BRIDGE_SOURCE, BridgeBuilder
from initialize_claude_ai.mcp_auto_discovery.writer import McpWriter

USER = "operator"
PASSWORD = "".join(chr(c) for c in (112, 52, 115, 115, 119, 48, 114, 100))  # built, never a literal


# -- the method, from evidence ------------------------------------------------------------


@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        ({"components": {"securitySchemes": {"b": {"type": "http", "scheme": "basic"}}}}, "basic"),
        ({"components": {"securitySchemes": {"t": {"type": "http", "scheme": "bearer"}}}}, "bearer"),
        ({"securityDefinitions": {"b": {"type": "basic"}}}, "basic"),
        ({"components": {"securitySchemes": {"k": {"type": "apiKey", "in": "header", "name": "X-Api-Key"}}}}, "apikey"),
        ({}, ""),
    ],
)
def test_the_api_description_names_the_method(spec, expected) -> None:
    assert bridge_auth.method_from_spec(spec).get("scheme", "") == expected


def test_the_description_s_own_requirement_is_preferred() -> None:
    spec = {
        "security": [{"tok": []}],
        "components": {
            "securitySchemes": {"b": {"type": "http", "scheme": "basic"}, "tok": {"type": "http", "scheme": "bearer"}}
        },
    }
    assert bridge_auth.method_from_spec(spec)["scheme"] == "bearer"


@pytest.mark.parametrize(
    ("challenge", "expected"),
    [
        ('basic realm="API login"', {"scheme": "basic"}),
        ("Bearer", {"scheme": "bearer"}),
        ('Token realm="x"', {"scheme": "token", "prefix": "Token"}),
        ("Negotiate", {}),
    ],
)
def test_the_server_s_challenge_names_the_method(challenge, expected) -> None:
    method = bridge_auth.method_from_challenge(challenge)
    assert {k: v for k, v in method.items() if k != "evidence"} == expected


# -- a server that only lets the right login in --------------------------------------------


class ObsLike(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        wanted = "Basic " + base64.b64encode(f"{USER}:{PASSWORD}".encode()).decode()
        if self.headers.get("Authorization") != wanted:
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'basic realm="API login"')
            self.end_headers()
            self.wfile.write(b"<status code='authentication_required'/>")
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/xml")
        self.end_headers()
        self.wfile.write(b"<person login='operator'/>")

    def log_message(self, *_args):
        pass


@pytest.fixture(scope="module")
def obs():
    server = http.server.HTTPServer(("127.0.0.1", 0), ObsLike)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def spec_for(base: str, security: bool) -> dict:
    spec = {
        "openapi": "3.0.0",
        "info": {"title": "obs", "version": "1"},
        "servers": [{"url": base}],
        "paths": {"/person": {"get": {"operationId": "get_person", "responses": {"200": {}}}}},
    }
    if security:
        spec["components"] = {"securitySchemes": {"basic": {"type": "http", "scheme": "basic"}}}
    return spec


@pytest.fixture
def live(ctx, tmp_path, monkeypatch):
    ctx.dry_run = False
    ctx.settings.set("mcp.bridge.output_dir", str(tmp_path / "bridges"))
    monkeypatch.delenv("BUILD_EXAMPLE_DE_USERNAME", raising=False)
    monkeypatch.delenv("BUILD_EXAMPLE_DE_PASSWORD", raising=False)
    return ctx


def generate(ctx, base: str, security: bool):
    candidate, artifacts, detail = BridgeBuilder(ctx).generate_from_openapi(
        "build.example.de", spec_for(base, security), base_url=base
    )
    assert candidate is not None, detail
    return candidate, artifacts


@pytest.mark.parametrize("security", [True, False], ids=["from the description", "from the 401"])
def test_the_bridge_asks_for_a_user_name_and_a_password(live, obs, security) -> None:
    candidate, artifacts = generate(live, obs, security)
    manifest = json.loads(artifacts.manifest.read_text())
    assert manifest["auth"]["scheme"] == "basic"
    assert manifest["auth"]["variables"] == {
        "username": "BUILD_EXAMPLE_DE_USERNAME",
        "password": "BUILD_EXAMPLE_DE_PASSWORD",
    }
    assert set(candidate.secret_env) == {"BUILD_EXAMPLE_DE_USERNAME", "BUILD_EXAMPLE_DE_PASSWORD"}
    assert "your user name on build.example.de" in candidate.secret_formats["BUILD_EXAMPLE_DE_USERNAME"]
    note = next(n for n in candidate.notes if "HTTP Basic" in n)
    assert "export BUILD_EXAMPLE_DE_USERNAME" in note and "BUILD_EXAMPLE_DE_PASSWORD" in note


def test_the_entry_names_the_parts_and_nothing_vague(live, obs) -> None:
    candidate, _artifacts = generate(live, obs, True)
    env = McpWriter(live).entry_for(candidate, "build-example-de")["env"]
    assert env["BUILD_EXAMPLE_DE_USERNAME"].startswith("${BUILD_EXAMPLE_DE_USERNAME")
    assert env["BUILD_EXAMPLE_DE_PASSWORD"].startswith("${BUILD_EXAMPLE_DE_PASSWORD")
    assert not any("AUTH_HEADER" in k or "AUTH_VALUE" in k for k in env), sorted(env)


def test_a_credential_in_parts_keeps_both_parts_when_waiting(live, obs) -> None:
    candidate, _artifacts = generate(live, obs, True)
    assert McpWriter(live).prepare_provisional(candidate, "build-example-de") == [
        "BUILD_EXAMPLE_DE_PASSWORD",
        "BUILD_EXAMPLE_DE_USERNAME",
    ]


def test_the_bridge_builds_the_basic_header_itself(live, obs) -> None:
    _candidate, artifacts = generate(live, obs, True)
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
                {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "get_person", "arguments": {}}},
            ]
        )
        + "\n"
    )
    run = subprocess.run(
        [sys.executable, str(artifacts.script)],
        input=messages,
        capture_output=True,
        text=True,
        timeout=30,
        env={
            "MCP_BRIDGE_MANIFEST": str(artifacts.manifest),
            "PATH": "/usr/bin:/bin",
            "BUILD_EXAMPLE_DE_USERNAME": USER,
            "BUILD_EXAMPLE_DE_PASSWORD": PASSWORD,
        },
    )
    assert "<person login='operator'/>" in run.stdout.splitlines()[-1]


@pytest.mark.parametrize(("password", "verdict"), [(PASSWORD, "accepted"), ("wrong", "REJECTED")])
def test_an_exported_login_is_checked(live, obs, monkeypatch, password, verdict) -> None:
    """In the login check's own words, so a bridge and a package are reported alike."""
    monkeypatch.setenv("BUILD_EXAMPLE_DE_USERNAME", USER)
    monkeypatch.setenv("BUILD_EXAMPLE_DE_PASSWORD", password)
    candidate, _artifacts = generate(live, obs, True)
    note = next(n for n in candidate.notes if "the credential was" in n)
    assert verdict in note and password not in note


def test_no_evidence_means_no_credential_variables(live) -> None:
    live.settings.set("mcp.bridge.generated.auth.probe_challenge", False)
    candidate, artifacts = generate(live, "http://127.0.0.1:9", False)
    assert "auth" not in json.loads(artifacts.manifest.read_text())
    assert candidate.secret_env == {}


def test_the_source_names_no_credential_variable() -> None:
    """The entry carries only the parts the method needs, because the script names none."""
    assert "AUTH_HEADER" not in _BRIDGE_SOURCE and "AUTH_VALUE" not in _BRIDGE_SOURCE
