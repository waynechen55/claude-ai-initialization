"""What the real runs on this machine showed, after the fixes before them passed every test.

* sd.example.com came to refuse every API path to anonymous callers. Recognition and
  API-kind detection read only a 2xx: "product unknown", the customer API never
  found, no login plan -- and the waiting mcp-atlassian settled the address.
  A 401 in the API's own form, while a path that cannot exist is answered
  otherwise, is evidence the API is there behind a login.
* Its bridge's "needs no login" call answered 401 and the bridge was called
  broken; it was the API asking for a login.
* ``--mcp-server a,b`` reached the same-login lookup as one host "a,b".
* build-example-net's entry passed SSH-key slots while its manifest read a password.
* ``SSH_AUTH_SOCK`` (an agent's socket) was filed as a credential.
* ``${NAME:-default}`` reached a trial as literal text.
"""

from __future__ import annotations

import json
import sys
from types import SimpleNamespace

from initialize_claude_ai.mcp_auto_discovery import api_kinds, inventory, login_methods
from initialize_claude_ai.mcp_auto_discovery.secret_policy import is_secret_name
from initialize_claude_ai.mcp_auto_discovery.types import Candidate, Handshake


def answer(status: int, content_type: str = "application/xml", text: str = "") -> SimpleNamespace:
    return SimpleNamespace(
        status=status,
        headers={"content-type": content_type},
        content_type=content_type,
        text=text,
        ok=200 <= status < 300,
    )


def site(table: dict[str, SimpleNamespace], default: SimpleNamespace):
    def send(method, url, content_type="", body=""):
        return next((v for k, v in table.items() if url.endswith(k)), default)

    return send


def test_an_api_behind_a_login_is_detected_with_a_control(settings) -> None:
    jsm = next(k for k in api_kinds.kinds(settings) if k.name == "jira_service_management")
    send = site({"/rest/servicedeskapi/info": answer(401)}, answer(404))
    assert api_kinds.detect(jsm, "https://sd.example.test", send=send, fetch=lambda u: answer(404)).endswith("/info")


def test_a_login_wall_on_every_path_is_not_an_api(settings) -> None:
    jsm = next(k for k in api_kinds.kinds(settings) if k.name == "jira_service_management")
    walled = site({}, answer(401))
    assert api_kinds.detect(jsm, "https://sd.example.test", send=walled, fetch=lambda u: answer(401)) == ""
    page = site({"/rest/servicedeskapi/info": answer(401, "text/html")}, answer(404))
    assert api_kinds.detect(jsm, "https://sd.example.test", send=page, fetch=lambda u: answer(404)) == "", "a web page"


def test_a_product_behind_a_login_is_recognised(settings) -> None:
    get = site({"/rest/api/2/serverInfo": answer(401)}, answer(404, "text/html"))
    assert login_methods.recognise("sd.example.test", settings, lambda url: get("GET", url)) == "jira"


def test_a_product_whose_page_redirects_to_sso_is_recognised_by_its_api(settings) -> None:
    get = site({"/login.action": answer(302, "text/html"), "/rest/api/user/current": answer(401)}, answer(404))
    assert login_methods.recognise("wiki.example.test", settings, lambda url: get("GET", url)) == "confluence"


def test_a_public_call_answered_with_a_login_refusal_is_waiting_not_broken(ctx) -> None:
    from initialize_claude_ai.mcp_auto_discovery.verifier import CandidateVerifier

    candidate = Candidate(kind="stdio", name="bridge", api_kind="jira_service_management")
    shake = Handshake(
        ok=True,
        public_check={
            "tool": "get_service_desk_info",
            "answered": False,
            "detail": 'HTTP 401: {"message":"Client must be authenticated"}',
        },
    )
    assert CandidateVerifier(ctx).public_refusal(candidate, shake) == ""
    shake.public_check["detail"] = "HTTP 404: not found"
    assert "broken" in CandidateVerifier(ctx).public_refusal(candidate, shake)


def test_a_comma_separated_address_list_is_split() -> None:
    ctx = SimpleNamespace(
        state={"mcp_servers": ["sd.example.com,build.example.de"]},
        settings=SimpleNamespace(str_list=lambda *a, **k: []),
    )
    assert inventory.run_hosts(ctx) == ["sd.example.com", "build.example.de"]


def test_an_entry_and_its_manifest_must_agree(settings, tmp_path) -> None:
    from initialize_claude_ai.mcp_auto_discovery.traditional import manifest_mismatch

    manifest = tmp_path / "build.client.json"
    manifest.write_text(json.dumps({"parts": {"B_USERNAME": "user = {value}", "B_PASSWORD": "pass = {value}"}}))
    entry = {
        "command": "python3",
        "env": {
            "MCP_CLIENT_MANIFEST": str(manifest),
            "B_USERNAME": "${B_USERNAME}",
            "B_SSH_PUBLIC_KEY": "${B_SSH_PUBLIC_KEY:-}",
        },
    }
    assert "B_PASSWORD" in manifest_mismatch(entry, settings)
    entry["env"]["B_PASSWORD"] = "${B_PASSWORD}"
    assert manifest_mismatch(entry, settings) == ""


def test_a_location_is_not_a_credential() -> None:
    assert not is_secret_name("SSH_AUTH_SOCK") and not is_secret_name("BUILD_EXAMPLE_NET_SSH_KEY_FILE")
    assert is_secret_name("BUILD_EXAMPLE_NET_PASSWORD") and is_secret_name("JIRA_PERSONAL_TOKEN")


SERVER = """
import json, os, sys
value = os.environ.get("OPTIONAL_SETTING", "<unset>")
tools = [{"name": "show", "inputSchema": {"type": "object", "properties": {}}}]
for line in sys.stdin:
    msg = json.loads(line)
    rid, method = msg.get("id"), msg.get("method")
    if rid is None:
        continue
    if method == "initialize":
        result = {"protocolVersion": msg["params"]["protocolVersion"], "capabilities": {"tools": {}},
                  "serverInfo": {"name": "x", "version": "1"}, "instructions": "value=" + value}
    elif method == "tools/list":
        result = {"tools": tools}
    else:
        result = {}
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": rid, "result": result}) + "\\n")
    sys.stdout.flush()
"""


def test_a_trial_expands_an_optional_reference_like_claude_code(ctx, tmp_path, monkeypatch) -> None:
    from initialize_claude_ai.mcp_auto_discovery.verifier import CandidateVerifier

    monkeypatch.delenv("SOME_UNSET_VARIABLE", raising=False)
    ctx.dry_run = False
    ctx.runner.which = lambda name, **_: name  # type: ignore[assignment]
    script = tmp_path / "server.py"
    script.write_text(SERVER)
    candidate = Candidate(
        kind="stdio",
        name="x",
        command=[sys.executable, str(script)],
        env={"OPTIONAL_SETTING": "${SOME_UNSET_VARIABLE:-fallback}"},
    )
    shake = CandidateVerifier(ctx).verify(candidate).handshake
    assert shake.ok and "value=fallback" in (shake.instructions or ""), shake.instructions


def test_packaged_settings_equal_the_code(settings) -> None:
    from initialize_claude_ai.mcp_auto_discovery.secret_policy import LOCATION_WORDS
    from initialize_claude_ai.mcp_auto_discovery.verifier import DEFAULT_PUBLIC_CALL_LOGIN_MARKERS

    assert tuple(settings.str_list("mcp.secret_policy.location_words")) == LOCATION_WORDS
    assert tuple(settings.str_list("mcp.verification.public_call_login_markers")) == DEFAULT_PUBLIC_CALL_LOGIN_MARKERS
    assert settings.bool_("mcp.discovery.api_kinds_behind_login") and settings.bool_(
        "mcp.discovery.auth.recognise_behind_login"
    )
    assert settings.bool_("mcp.discovery.reuse.check_manifests")


def test_a_login_refused_every_way_is_said_even_beside_a_public_third_party_server(ctx) -> None:
    from initialize_claude_ai.core.report import PhaseResult
    from initialize_claude_ai.mcp_auto_discovery import login_check
    from initialize_claude_ai.mcp_auto_discovery.types import StageOutcome, TargetOutcome
    from initialize_claude_ai.phases.phase4_mcp_auto_discovery import McpPhase

    def refused(name: str, source: str) -> Candidate:
        c = Candidate(kind="stdio", name=name, command=["python3", f"{name}.py"])
        c.handshake = Handshake(
            ok=True,
            tools=["whoami"],
            round_trip=True,
            login=login_check.REJECTED,
            login_refused=True,
            login_credential="fp",
            login_source=source,
        )
        return c

    stranger = Candidate(kind="url", name="build-remote", url="https://mcp.stranger.example/public")
    stranger.handshake = Handshake(
        ok=True, tools=["search"], round_trip=True
    )  # verified, no login: refused by the gate
    outcome = TargetOutcome(target="https://build.example.test")
    outcome.stages.append(
        StageOutcome(
            stage="bridge",
            candidates=[
                refused("basic-bridge", "what you export for build.example.test"),
                refused("signature-bridge", "~/.ssh keys"),
                stranger,
            ],
        )
    )
    result = PhaseResult(phase="mcp")
    McpPhase(ctx)._report_address_facts(result, None, outcome)
    assert any(s.name.endswith(":: your login") for s in result.steps)
