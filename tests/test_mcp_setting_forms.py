"""A setting's form is proved by a call that uses it, not guessed from its name.

The entry for build.opensuse.org was written with
``OSC_MCP_API=https://api.opensuse.org``. osc-mcp's API tools add ``https://``
themselves and worked; only ``search_packages`` answered "unexpected api
address format", because it wants the bare host. A handshake never parses the
setting, the login check called one argument-free tool and saw "accepted", and a
"misconfigured" answer would have been filed as "failed for another reason".

Now several read-only tools are called (arguments filled from configuration or
from the tool's own schema), a "misconfigured" answer is its own verdict, and an
address-valued variable is tried in other forms until one clears it.

The fake server is a real process speaking JSON-RPC over stdio.
"""

from __future__ import annotations

import sys

import pytest

from initialize_claude_ai.core.secrets import Secret
from initialize_claude_ai.mcp_auto_discovery import login_check
from initialize_claude_ai.mcp_auto_discovery.types import Candidate, Transport
from initialize_claude_ai.mcp_auto_discovery.verifier import CandidateVerifier, _address_variants

TOKEN = "".join(chr(c) for c in (102, 111, 114, 109, 45, 116, 111, 107, 101, 110, 45, 55))

#: list_items works with any FAKE_API; search_items rejects one with a scheme,
#: as osc-mcp's search_packages did.
SERVER = """
import json, os, sys
api = os.environ.get("FAKE_API", "")
tools = [
    {"name": "list_items", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "search_items", "inputSchema": {"type": "object", "properties": {"pattern": {"type": "string"}},
     "required": ["pattern"]}},
    {"name": "get_build_log", "inputSchema": {"type": "object", "properties": {"project": {"type": "string"}},
     "required": ["project"]}},
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
                  "serverInfo": {"name": "fake-obs", "version": "1"}}
    elif method == "tools/list":
        result = {"tools": tools}
    elif method == "tools/call" and not os.environ.get("FAKE_TOKEN"):
        # A real service refuses a caller with no credential at all.
        result = {"isError": True, "content": [{"type": "text", "text": "401 Unauthorized"}]}
    elif method == "tools/call" and msg["params"]["name"] == "search_items" and "://" in api:
        result = {"isError": True, "content": [{"type": "text", "text": "unexpected api address format: " + api}]}
    elif method == "tools/call":
        result = {"content": [{"type": "text", "text": json.dumps([{"name": "qemu"}])}]}
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
        Secret(TOKEN) if reference.startswith("env:") and reference.endswith("FAKE_TOKEN") else None
    )
    return ctx


def fake(tmp_path, api: str) -> Candidate:
    script = tmp_path / "server.py"
    script.write_text(SERVER)
    candidate = Candidate(
        kind="stdio",
        name="build-example-org",
        transport=Transport.STDIO,
        command=[sys.executable, str(script)],
        env={"FAKE_API": api},
    )
    candidate.secret_env = {"FAKE_TOKEN": "env:FAKE_TOKEN"}
    return candidate


# -- through a real launch ------------------------------------------------------------------


def test_the_form_a_tool_rejects_is_replaced_by_the_one_it_takes(live, tmp_path) -> None:
    candidate = CandidateVerifier(live).verify(fake(tmp_path, "https://api.example.org"))
    shake = candidate.handshake

    assert candidate.env["FAKE_API"] == "api.example.org", "the proved form is the candidate's -- and the entry's"
    assert shake.address_forms == {"FAKE_API": "api.example.org"}
    assert not shake.misconfigured
    assert {row["tool"] for row in shake.functional} >= {"list_items", "search_items"}
    assert candidate.verified


def test_one_accepted_call_does_not_hide_a_tool_that_rejects_the_setting(live, tmp_path) -> None:
    live.settings.set("mcp.verification.address_forms.enabled", False)
    candidate = CandidateVerifier(live).verify(fake(tmp_path, "https://api.example.org"))
    shake = candidate.handshake
    assert shake.login == login_check.ACCEPTED, "list_items worked"
    assert shake.misconfigured.startswith("search_items"), "and search_items said why it did not"
    row = next(c for c in CandidateVerifier.audit(candidate) if c.name == "configured")
    assert row.ok is False


def test_a_tool_nobody_can_supply_arguments_for_is_named_not_exercised(live, tmp_path) -> None:
    candidate = CandidateVerifier(live).verify(fake(tmp_path, "api.example.org"))
    assert any(t.startswith("get_build_log") for t in candidate.handshake.unexercised)
    row = next(c for c in CandidateVerifier.audit(candidate) if c.name == "configured")
    assert row.ok is True and "not exercised: get_build_log" in row.detail


def test_a_value_already_in_the_accepted_form_is_left_alone(live, tmp_path) -> None:
    candidate = CandidateVerifier(live).verify(fake(tmp_path, "api.example.org"))
    assert candidate.env["FAKE_API"] == "api.example.org"
    assert candidate.handshake.address_forms == {}


# -- the pieces -----------------------------------------------------------------------------


def test_arguments_come_from_configuration_and_from_the_tool_s_own_schema(settings) -> None:
    spec = login_check.tool_spec(
        {
            "name": "get_thing",
            "inputSchema": {
                "type": "object",
                "required": ["kind", "query"],
                "properties": {"kind": {"type": "string", "enum": ["package", "project"]}, "query": {"type": "string"}},
            },
        }
    )
    chosen = login_check.pick([spec], settings, {"product": "opensuse"})
    assert chosen == [("get_thing", {"kind": "package", "query": "opensuse"})]


def test_a_misconfiguration_is_its_own_verdict(settings) -> None:
    reply = {"result": {"isError": True, "content": [{"type": "text", "text": "unexpected api address format: x"}]}}
    assert login_check.judge(reply, settings)[0] == login_check.MISCONFIGURED
    assert login_check.classify_failure("missing protocol scheme", settings) == login_check.MISCONFIGURED


@pytest.mark.parametrize(
    ("value", "first"),
    [
        ("https://api.example.org", "api.example.org"),
        ("api.example.org", "https://api.example.org"),
    ],
)
def test_address_forms(value, first) -> None:
    forms = ["{scheme}://{host}", "{host}", "{scheme}://{host}/", "{scheme}://{host}{path}"]
    assert _address_variants(value, forms, "https")[0] == first


@pytest.mark.parametrize("value", ["0.1.0", "/etc/ssl/ca.pem", "30", "true", "mcp-graphql@2.0.4", "${X}"])
def test_only_addresses_are_varied(value) -> None:
    assert _address_variants(value, ["{host}", "{scheme}://{host}"], "https") == []


def test_the_registry_s_own_words_decide_an_address_s_form(ctx) -> None:
    from initialize_claude_ai.mcp_auto_discovery.registry_client import RegistryClient

    candidate = Candidate(kind="stdio", name="x", command=["uvx", "x"])
    RegistryClient(ctx)._fill_addresses(
        candidate,
        [
            {"name": "SERVICE_API", "description": "hostname of the API server, without the scheme"},
        ],
        "https://api.example.org",
    )
    candidate2 = Candidate(kind="stdio", name="y", command=["uvx", "y"])
    RegistryClient(ctx)._fill_addresses(
        candidate2,
        [
            {"name": "SERVICE_HOST", "description": "full URL of the service, e.g. https://example.org"},
        ],
        "https://api.example.org",
    )
    assert candidate.env.get("SERVICE_API", "api.example.org") == "api.example.org"
    assert candidate2.env["SERVICE_HOST"].startswith("https://")


def test_packaged_settings_equal_the_code(settings) -> None:
    config = settings.section("mcp.verification.login_check")
    assert tuple(config.str_list("read_verbs")) == login_check.DEFAULT_READ_VERBS
    assert config.dict_("arguments") == login_check.DEFAULT_ARGUMENTS
    assert tuple(config.str_list("misconfiguration_markers")) == login_check.DEFAULT_MISCONFIGURATION_MARKERS
    assert config.dict_("remedies") == login_check.DEFAULT_REMEDIES
