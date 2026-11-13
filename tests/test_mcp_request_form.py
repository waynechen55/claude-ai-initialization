"""The form of a request is proved by a call that uses it -- and a gateway's web page blames nobody.

build.opensuse.org's bridge sent ``Accept: application/xml, text/xml;q=0.9,
*/*;q=0.1``. With the operator's login every call came back as the HTML 404
page of the gateway in front of the API; no login and a made-up one got 401;
the same calls with ``Accept: */*`` (or any single value) returned the data.
The run that wrote the entry held no credential, so it only ever saw 401 --
and the later check read the 404 as "the login is recognised, and the service
has no account behind it", which was wrong: the account existed.

Now:

* a generated bridge sends ONE Accept value, never a weighted list;
* a read-only call answered 404/406/415 is retried once with ``*/*`` at run
  time, and what worked is kept for the session;
* a logged-in call answered by a gateway page makes the verifier try the
  request-form variables the launch reads before anything is blamed;
* a 404 web page is never "no account".

Fake servers are real processes; nothing here is a real service.
"""

from __future__ import annotations

import base64
import http.server
import json
import subprocess
import sys
import threading

import pytest

from initialize_claude_ai.core.secrets import Secret
from initialize_claude_ai.mcp_auto_discovery import bridge, login_check
from initialize_claude_ai.mcp_auto_discovery.bridge import _BRIDGE_SOURCE, MANIFEST_SCHEMA, BridgeBuilder
from initialize_claude_ai.mcp_auto_discovery.types import Candidate, Transport
from initialize_claude_ai.mcp_auto_discovery.verifier import CandidateVerifier

#: A synthetic login, assembled so no credential-shaped literal is in the source.
USER = "someone"
PASSWORD = "".join(chr(c) for c in (115, 116, 97, 110, 100, 45, 105, 110, 45, 112, 97, 115, 115))
GATEWAY_404 = (
    '404 Not Found: <!DOCTYPE html> <head> <meta charset="utf-8" /> <title>Error 404</title> </head> '
    "<body> <h1>404: Not Found</h1> </body> </html> GET https://api.example.org/person/someone"
)


# -- 1. one Accept value -------------------------------------------------------------------


def test_the_configured_accept_is_one_value(settings) -> None:
    accept = settings.str_("mcp.bridge.generated.accept")
    assert accept == bridge.DEFAULT_ACCEPT, "the code fallback equals the packaged value"
    assert "," not in accept and ";q=" not in accept, "a weighted list is what the gateway rejected"


def test_the_retry_settings_have_equal_fallbacks(settings) -> None:
    assert settings.list_("mcp.bridge.generated.accept_retry_statuses") == list(bridge.DEFAULT_ACCEPT_RETRY)
    assert settings.str_list("mcp.bridge.generated.accept_fallbacks") == list(bridge.DEFAULT_ACCEPT_FALLBACKS)
    assert settings.list_("mcp.verification.request_forms.statuses") == list(login_check.DEFAULT_REQUEST_FORM_STATUSES)


def test_an_operation_documenting_several_types_gets_the_single_default(ctx) -> None:
    spec = {
        "openapi": "3.0.0",
        "servers": [{"url": "https://api.example.org"}],
        "paths": {
            "/one": {"get": {"operationId": "get_one", "responses": {"200": {"content": {"application/xml": {}}}}}},
            "/many": {
                "get": {
                    "operationId": "get_many",
                    "responses": {"200": {"content": {"application/xml": {}, "text/xml": {}}}},
                }
            },
        },
    }
    tools = {t["name"]: t for t in BridgeBuilder(ctx)._operations(spec, "https://api.example.org")["tools"]}
    assert tools["get_one"]["accept"] == "application/xml", "one documented type is asked for exactly"
    assert "accept" not in tools["get_many"], "several are never joined into a list; the manifest's value applies"


# -- 2. the bridge proves the form at run time ------------------------------------------------


class Gateway(http.server.BaseHTTPRequestHandler):
    """Answers like api.opensuse.org: 401 without a login; with one, a 404 page for a multi-value Accept."""

    seen: list[str] = []

    def _answer(self):
        accept = self.headers.get("Accept", "")
        Gateway.seen.append(accept)
        auth = self.headers.get("Authorization", "")
        expected = "Basic " + base64.b64encode(f"{USER}:{PASSWORD}".encode()).decode()
        if auth != expected:
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="example"')
            self.end_headers()
            self.wfile.write(b"<html><body>401 Authorization Required</body></html>")
            return
        if "," in accept:
            self.send_response(404)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<!DOCTYPE html><html><head><title>Error 404</title></head></html>")
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/xml")
        self.end_headers()
        self.wfile.write(b"<person><login>someone</login></person>")

    do_GET = _answer  # noqa: N815 - the stdlib's name
    do_POST = _answer  # noqa: N815

    def log_message(self, *_args):
        pass


@pytest.fixture(scope="module")
def gateway():
    server = http.server.HTTPServer(("127.0.0.1", 0), Gateway)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def run_bridge(tmp_path, base_url: str, calls: list[str], *, method: str = "GET", accept: str = "") -> tuple[list, str]:
    """Each named call through the real generated bridge, as a process; its answers and its stderr."""
    script = tmp_path / "bridge.py"
    script.write_text(_BRIDGE_SOURCE)
    tool = {
        "name": "get_account",
        "method": method,
        "path": "/person/someone",
        "parameters": [],
        "inputSchema": {"type": "object", "properties": {}},
    }
    manifest = {
        "name": "t",
        "schema": MANIFEST_SCHEMA,
        "script": "bridge.py",
        "base_url": base_url,
        "accept": accept or "application/xml, text/xml;q=0.9, */*;q=0.1",
        "accept_retry": {"statuses": [404, 406, 415], "values": ["*/*"]},
        "auth": {"scheme": "basic", "variables": {"username": "T_USERNAME", "password": "T_PASSWORD"}},
        "operations": [tool],
    }
    path = tmp_path / "t.operations.json"
    path.write_text(json.dumps(manifest))
    init = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}},
    }
    messages = [init, {"jsonrpc": "2.0", "method": "notifications/initialized"}]
    messages += [
        {"jsonrpc": "2.0", "id": 10 + i, "method": "tools/call", "params": {"name": name, "arguments": {}}}
        for i, name in enumerate(calls)
    ]
    run = subprocess.run(
        [sys.executable, str(script)],
        input="\n".join(json.dumps(m) for m in messages) + "\n",
        capture_output=True,
        text=True,
        timeout=60,
        env={
            "MCP_BRIDGE_MANIFEST": str(path),
            "PATH": "/usr/bin:/bin",
            "T_USERNAME": USER,
            "T_PASSWORD": PASSWORD,
        },
    )
    answers = [json.loads(line) for line in run.stdout.splitlines() if line.strip()][1:]
    return answers, run.stderr


def text_of(answer: dict) -> str:
    return "".join(c.get("text", "") for c in answer["result"]["content"])


def test_a_rejected_request_form_is_retried_with_one_value(tmp_path, gateway) -> None:
    Gateway.seen.clear()
    [answer], stderr = run_bridge(tmp_path, gateway, ["get_account"])
    assert "<login>someone</login>" in text_of(answer), "the data, not the gateway's page"
    assert Gateway.seen == ["application/xml, text/xml;q=0.9, */*;q=0.1", "*/*"]
    assert "MCP_BRIDGE_ACCEPT" in stderr, "the operator is told which value to keep"


def test_what_worked_is_kept_for_the_session(tmp_path, gateway) -> None:
    Gateway.seen.clear()
    answers, _ = run_bridge(tmp_path, gateway, ["get_account", "get_account"])
    assert all("<login>someone</login>" in text_of(a) for a in answers)
    assert Gateway.seen == ["application/xml, text/xml;q=0.9, */*;q=0.1", "*/*", "*/*"], "asked once, then remembered"


def test_a_call_that_may_change_something_is_never_repeated(tmp_path, gateway) -> None:
    Gateway.seen.clear()
    [answer], _ = run_bridge(tmp_path, gateway, ["get_account"], method="POST")
    assert answer["result"]["isError"] and "HTTP 404" in text_of(answer)
    assert len(Gateway.seen) == 1, "a POST is sent once, whatever it got"


def test_a_single_value_needs_no_retry(tmp_path, gateway) -> None:
    Gateway.seen.clear()
    [answer], stderr = run_bridge(tmp_path, gateway, ["get_account"], accept="application/xml")
    assert "<login>someone</login>" in text_of(answer)
    assert Gateway.seen == ["application/xml"] and not stderr.strip()


# -- 3./5. the verdict: a gateway page is the request's form, never the account ----------------


def test_a_404_web_page_is_not_a_missing_account(settings) -> None:
    verdict, detail = login_check.judge(
        {"result": {"isError": True, "content": [{"type": "text", "text": GATEWAY_404}]}}, settings
    )
    assert verdict == login_check.UNCHECKED and login_check.GATEWAY_PAGE_WORDS in detail
    assert not login_check.account_missing("get_my_account", verdict, detail, settings)


def test_a_404_in_the_api_s_own_form_still_is(settings) -> None:
    detail = 'the call failed for another reason: 404 {"error": "unknown user"}'
    assert login_check.account_missing("get_my_account", login_check.UNCHECKED, detail, settings)


def test_a_bridge_s_own_words_for_a_web_page_count_as_one(settings) -> None:
    words = "404 Not Found: an HTML page (title: Error 404), not the API's format: the gateway answered"
    assert login_check.is_gateway_page(words, settings)


def test_only_an_inconclusive_web_page_with_a_listed_status_is_a_rejected_request(settings) -> None:
    page = {
        "tool": "get_my_account",
        "verdict": login_check.UNCHECKED,
        "detail": f"answered HTTP 404 {login_check.GATEWAY_PAGE_WORDS}",
    }
    assert login_check.request_form_rejected([page], settings).startswith("get_my_account")
    assert not login_check.request_form_rejected([{**page, "verdict": login_check.REJECTED}], settings), (
        "a refusal said something"
    )
    assert not login_check.request_form_rejected(
        [{**page, "detail": "answered HTTP 500 " + login_check.GATEWAY_PAGE_WORDS}], settings
    )
    assert not login_check.request_form_rejected([{**page, "detail": '404 {"error": "not found"}'}], settings)


# -- through a real launch: the verifier tries the request's form ------------------------------

#: whoami answers 401 without FAKE_TOKEN, the gateway's 404 page with it unless
#: MCP_BRIDGE_ACCEPT is a single value, and the account with both -- unless
#: ALWAYS_PAGE is set, when no form helps.
SERVER = """
import json, os, sys
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
                  "serverInfo": {"name": "fake-gateway", "version": "1"}}
    elif method == "tools/list":
        result = {"tools": [{"name": "whoami", "annotations": {"readOnlyHint": True},
                             "inputSchema": {"type": "object", "properties": {}}}]}
    elif method == "tools/call" and not os.environ.get("FAKE_TOKEN"):
        result = {"isError": True, "content": [{"type": "text", "text": "401 Unauthorized"}]}
    elif method == "tools/call" and (os.environ.get("ALWAYS_PAGE") or "," in os.environ.get("MCP_BRIDGE_ACCEPT", "")):
        result = {"isError": True, "content": [{"type": "text", "text": PAGE}]}
    elif method == "tools/call":
        result = {"content": [{"type": "text", "text": json.dumps({"login": "someone", "name": "Some One"})}]}
    else:
        result = {}
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": rid, "result": result}) + "\\n")
    sys.stdout.flush()
"""


@pytest.fixture
def live(ctx):
    ctx.dry_run = False
    ctx.runner.which = lambda name, **_: name  # type: ignore[assignment]
    ctx.settings.set("mcp.packages.trial_timeout_seconds", 20)
    ctx.secrets.resolve = lambda reference, **_: (  # type: ignore[assignment]
        Secret(PASSWORD) if reference.startswith("env:") and reference.endswith("FAKE_TOKEN") else None
    )
    return ctx


def fake(tmp_path, *, always_page: bool = False) -> Candidate:
    script = tmp_path / "server.py"
    script.write_text(f"PAGE = {GATEWAY_404!r}\n" + SERVER)
    env = {"MCP_BRIDGE_ACCEPT": "application/xml, text/xml;q=0.9, */*;q=0.1"}
    if always_page:
        env["ALWAYS_PAGE"] = "1"
    candidate = Candidate(
        kind="stdio",
        name="build-example-org",
        transport=Transport.STDIO,
        command=[sys.executable, str(script)],
        env=env,
    )
    candidate.secret_env = {"FAKE_TOKEN": "env:FAKE_TOKEN"}
    return candidate


def test_the_request_form_the_gateway_rejects_is_replaced_by_one_it_takes(live, tmp_path) -> None:
    candidate = CandidateVerifier(live).verify(fake(tmp_path))
    shake = candidate.handshake
    assert candidate.env["MCP_BRIDGE_ACCEPT"] == "*/*", "the proved form is the candidate's -- and the entry's"
    assert shake.request_forms == {"MCP_BRIDGE_ACCEPT": "*/*"}
    assert shake.login == login_check.ACCEPTED
    assert shake.login_reason != login_check.NO_ACCOUNT
    assert any("gateway's web page" in n for n in candidate.notes)


def test_when_no_form_helps_it_is_said_and_only_then_the_account_is_judged(live, tmp_path) -> None:
    candidate = CandidateVerifier(live).verify(fake(tmp_path, always_page=True))
    shake = candidate.handshake
    assert shake.login != login_check.ACCEPTED and shake.request_forms == {}
    assert candidate.env["MCP_BRIDGE_ACCEPT"] == "application/xml, text/xml;q=0.9, */*;q=0.1", (
        "nothing kept that did not help"
    )
    assert any("tried MCP_BRIDGE_ACCEPT=*/*" in h for h in shake.protocol_hints)


def test_switching_it_off_leaves_the_request_as_it_was(live, tmp_path) -> None:
    live.settings.set("mcp.verification.request_forms.enabled", False)
    candidate = CandidateVerifier(live).verify(fake(tmp_path))
    assert candidate.env["MCP_BRIDGE_ACCEPT"] == "application/xml, text/xml;q=0.9, */*;q=0.1"
    assert candidate.handshake.request_forms == {}
    assert candidate.handshake.login_reason != login_check.NO_ACCOUNT or any(
        "no form could be tried" in h for h in candidate.handshake.protocol_hints
    )


def test_words_around_a_gateway_page_do_not_outrank_its_status(settings) -> None:
    """The real bridge explained its 404 page mentioning 401; that explanation is not a refusal."""
    words = (
        "404 Not Found: an HTML page (title: Error 404), not the API's format: the gateway in front of the API "
        "answered -- a login was sent; if the same call without it is refused with 401, the login is recognised"
    )
    verdict, detail = login_check.judge(
        {"result": {"isError": True, "content": [{"type": "text", "text": words}]}}, settings
    )
    assert verdict == login_check.UNCHECKED and "HTTP 404" in detail and login_check.GATEWAY_PAGE_WORDS in detail
    assert login_check.request_form_rejected(
        [{"tool": "get_my_account", "verdict": verdict, "detail": detail}], settings
    )


def test_a_401_login_wall_page_is_still_a_refusal(settings) -> None:
    page = "401 Unauthorized: <!DOCTYPE html><html><body>Authorization Required</body></html>"
    verdict, _ = login_check.judge({"result": {"isError": True, "content": [{"type": "text", "text": page}]}}, settings)
    assert verdict == login_check.REJECTED
