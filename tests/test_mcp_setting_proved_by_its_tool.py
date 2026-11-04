"""build-opensuse-org shipped with an OSC_MCP_API its search_packages rejects.

Only search_packages reads osc-mcp's address setting, it needs three arguments
nobody could fill, and so it was never called: no tool said "misconfigured",
and the form trials never ran. Even when they ran, they varied only the form of
the host given (the website, build.opensuse.org), never the host that serves the
API (api.opensuse.org). And which form is right depends on the version: the
release parses a URL (``https://api.…``), the build in use compares raw text
(``api.…``). Only a call to the binary that will run decides.
"""

from __future__ import annotations

import sys

import pytest

from initialize_claude_ai.mcp_auto_discovery.types import Candidate

SERVER = """
import json, os, sys
from urllib.parse import urlsplit
api = os.environ.get("OSC_MCP_API", "")
version = os.environ.get("FAKE_VERSION", "raw")
tools = [
    {"name": "list_requests", "inputSchema": {"type": "object", "properties": {}}, "annotations": {"readOnlyHint": True}},
    {"name": "search_packages", "annotations": {"readOnlyHint": True}, "inputSchema": {"type": "object",
     "properties": {"path": {"type": "string"}, "path_repository": {"type": "string"}, "pattern": {"type": "string"}},
     "required": ["path", "path_repository", "pattern"]}},
]
def address_ok():
    if version == "raw":
        return api.startswith("api.")
    return (urlsplit(api).hostname or "").startswith("api.")
for line in sys.stdin:
    msg = json.loads(line)
    rid, method = msg.get("id"), msg.get("method")
    if rid is None:
        continue
    if method == "initialize":
        result = {"protocolVersion": msg["params"]["protocolVersion"], "capabilities": {"tools": {}},
                  "serverInfo": {"name": "osc-mcp", "version": "1"}}
    elif method == "tools/list":
        result = {"tools": tools}
    elif method == "tools/call":
        name = msg["params"]["name"]
        if name == "list_requests":
            # The account lives on the API host: the website refuses it (401).
            on_api = (urlsplit(api if "://" in api else "//" + api).hostname or "").startswith("api.")
            ok = os.environ.get("OSC_MCP_PASSWORD") == "right" and on_api
            result = ({"content": [{"type": "text", "text": json.dumps({"Requests": [{"ID": "605246"}]})}]} if ok else
                      {"isError": True, "content": [{"type": "text", "text": "401 Unauthorized"}]})
        elif not address_ok():
            result = {"isError": True, "content": [{"type": "text", "text": "unexpected api address format: " + api}]}
        else:
            result = {"isError": True, "content": [{"type": "text", "text": "download failed with status: 404 Not Found"}]}
    else:
        result = {}
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": rid, "result": result}) + "\\n")
    sys.stdout.flush()
"""


@pytest.fixture
def live(ctx, tmp_path, monkeypatch):
    ctx.dry_run = False
    ctx.runner.which = lambda name, **_: name  # type: ignore[assignment]
    ctx.settings.set("mcp.packages.trial_timeout_seconds", 20)
    monkeypatch.setenv("OBS_TEST_PASSWORD", "right")
    script = tmp_path / "osc_mcp.py"
    script.write_text(SERVER)
    ctx.state["mcp_api_hosts"] = {"build.example.test": ["api.example.test"]}
    return ctx, script


def candidate(script, version: str) -> Candidate:
    c = Candidate(
        kind="stdio",
        name="build-example-test",
        command=[sys.executable, str(script)],
        env={"OSC_MCP_API": "https://build.example.test", "OSC_MCP_USER": "operator", "FAKE_VERSION": version},
    )
    c.secret_env = {"OSC_MCP_PASSWORD": "env:OBS_TEST_PASSWORD"}
    return c


@pytest.mark.parametrize(("version", "accepted"), [("raw", "api.example.test"), ("url", "https://api.example.test")])
def test_the_form_and_host_its_reading_tool_accepts_is_found(live, version, accepted) -> None:
    from initialize_claude_ai.mcp_auto_discovery.verifier import CandidateVerifier

    ctx, script = live
    proved = CandidateVerifier(ctx).verify(candidate(script, version))
    assert proved.env["OSC_MCP_API"] == accepted, (proved.env, proved.notes[-3:])
    rows = {r["tool"]: r for r in proved.handshake.functional}
    assert rows["search_packages"]["verdict"] != "misconfigured"


def test_a_failure_past_the_setting_is_not_a_setting_error(live) -> None:
    """With the right address, search_packages still fails (no index file): not "misconfigured", nothing changed."""
    from initialize_claude_ai.mcp_auto_discovery.verifier import CandidateVerifier

    ctx, script = live
    right = candidate(script, "raw")
    right.env["OSC_MCP_API"] = "api.example.test"
    proved = CandidateVerifier(ctx).verify(right)
    assert proved.env["OSC_MCP_API"] == "api.example.test" and not proved.handshake.misconfigured


def test_a_setting_probe_is_never_a_login_verdict(ctx) -> None:
    from initialize_claude_ai.mcp_auto_discovery import login_check

    spec = {
        "name": "search_packages",
        "required": ["path", "limit", "exact"],
        "types": {"path": "string", "limit": "integer", "exact": "boolean"},
        "defaults": {},
    }
    assert login_check.setting_probe_arguments(spec, ctx.settings) == {
        "path": "initialize-claude-ai-probe",
        "limit": 1,
        "exact": False,
    }
    spec["types"]["path"] = "object"
    assert login_check.setting_probe_arguments(spec, ctx.settings) is None, "no stand-in of that kind: not asked"


def test_packaged_settings_equal_the_code(settings) -> None:
    from initialize_claude_ai.mcp_auto_discovery import login_check

    config = settings.section("mcp.verification.login_check")
    assert config.dict_("setting_probe_values") == login_check.DEFAULT_SETTING_PROBE_VALUES
    assert config.bool_("probe_settings") and config.int_("setting_probe_max") == 12
    forms = settings.section("mcp.verification.address_forms")
    assert forms.bool_("try_api_hosts") and forms.int_("max_variants") == 10


def test_a_login_refused_on_the_website_host_is_tried_on_the_api_host(live) -> None:
    """https://build.opensuse.org refused a good openSUSE login; api.opensuse.org knows it. The host is tried before the credential is blamed."""
    from initialize_claude_ai.mcp_auto_discovery import login_check
    from initialize_claude_ai.mcp_auto_discovery.verifier import CandidateVerifier

    ctx, script = live
    proved = CandidateVerifier(ctx).verify(candidate(script, "raw"))
    assert proved.env["OSC_MCP_API"] == "api.example.test"
    assert proved.handshake.login == login_check.ACCEPTED


def test_without_an_api_host_a_refusal_stays_a_refusal(live) -> None:
    from initialize_claude_ai.mcp_auto_discovery import login_check
    from initialize_claude_ai.mcp_auto_discovery.verifier import CandidateVerifier

    ctx, script = live
    ctx.state["mcp_api_hosts"] = {}
    proved = CandidateVerifier(ctx).verify(candidate(script, "raw"))
    assert proved.env["OSC_MCP_API"] == "https://build.example.test" and proved.handshake.login in login_check.REFUSED


def test_a_literal_you_also_export_is_read_from_the_export(ctx, monkeypatch) -> None:
    from initialize_claude_ai.core.secrets import Secret
    from initialize_claude_ai.mcp_auto_discovery.verifier import CandidateVerifier

    monkeypatch.setenv("BUILD_EXAMPLE_TEST_PASSWORD", "same-value")
    c = Candidate(kind="stdio", name="x", command=["x"])
    c.in_use = {"OSC_MCP_PASSWORD": Secret("same-value")}
    CandidateVerifier(ctx)._read_what_you_export(c, "build.example.test")
    assert c.held_names == {"OSC_MCP_PASSWORD": "BUILD_EXAMPLE_TEST_PASSWORD"}
    c.held_names, c.in_use = {}, {"OSC_MCP_PASSWORD": Secret("another-value")}
    CandidateVerifier(ctx)._read_what_you_export(c, "build.example.test")
    assert c.held_names == {}, "only an equal value is read from the export"
