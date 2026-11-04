"""An empty answer is not access; a refusal outranks acceptance; an unproved server does not end the search.

sd.example.com: the login check would have called mcp-atlassian's
``jira_get_all_projects`` -- read-only, no arguments -- and the customer account
got ``{"result": "[]"}``: a success with nothing in it. Counted as "accepted",
that made mcp-atlassian *verified*, settled the address, and the customer-API
bridge (the only thing the account can use) was never built. Every issue lookup
went to the portal. With no credential at all the report said nothing about
the customer API it had already found.

The fake server is a real process speaking JSON-RPC over stdio.
"""

from __future__ import annotations

import sys

import pytest

from initialize_claude_ai.core.secrets import Secret
from initialize_claude_ai.mcp_auto_discovery import login_check
from initialize_claude_ai.mcp_auto_discovery.types import (
    Candidate,
    Handshake,
    Stage,
    StageOutcome,
    TargetOutcome,
    Transport,
)
from initialize_claude_ai.mcp_auto_discovery.verifier import CandidateVerifier

TOKEN = "".join(chr(c) for c in (101, 109, 112, 116, 121, 45, 112, 97, 116, 45, 49))

#: PORTAL decides whether a second read tool exists and is sent to the portal.
SERVER = """
import json, os, sys
portal = os.environ.get("PORTAL") == "1"
tools = [{"name": "jira_get_all_projects", "inputSchema": {"type": "object", "properties": {}}}]
if portal:
    tools.append({"name": "jira_get_queue_issues", "inputSchema": {"type": "object", "properties": {}}})
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
                  "serverInfo": {"name": "fake-atlassian", "version": "1"}}
    elif method == "tools/list":
        result = {"tools": tools}
    elif method == "tools/call" and msg["params"]["name"] == "jira_get_all_projects":
        result = {"content": [{"type": "text", "text": "[]"}]}
    elif method == "tools/call":
        text = "500 Server Error: for url: https://sd.example.test/servicedesk/customer/portals"
        result = {"isError": True, "content": [{"type": "text", "text": text}]}
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
        Secret(TOKEN) if reference.startswith("env:") and reference.endswith("JIRA_PERSONAL_TOKEN") else None
    )
    return ctx


def fake(tmp_path, portal: bool) -> Candidate:
    script = tmp_path / "server.py"
    script.write_text(SERVER)
    candidate = Candidate(
        kind="stdio",
        name="sd-example-test",
        transport=Transport.STDIO,
        command=[sys.executable, str(script)],
        env={"JIRA_URL": "https://sd.example.test", "PORTAL": "1" if portal else "0"},
    )
    candidate.secret_env = {"JIRA_PERSONAL_TOKEN": "env:JIRA_PERSONAL_TOKEN"}
    return candidate


# -- an empty answer, and several answers -------------------------------------------------------


def test_an_empty_listing_is_never_accepted(live, tmp_path) -> None:
    candidate = CandidateVerifier(live).verify(fake(tmp_path, portal=False))
    shake = candidate.handshake
    assert shake.login == login_check.UNCHECKED and shake.login_detail == "answered with nothing"
    assert shake.functional[0]["verdict"] == login_check.EMPTY


def test_a_later_refusal_outranks_an_earlier_empty_or_accepted_answer(live, tmp_path) -> None:
    candidate = CandidateVerifier(live).verify(fake(tmp_path, portal=True))
    assert candidate.handshake.login == login_check.NO_ACCESS
    assert not candidate.verified


def test_summarise() -> None:
    accepted, no_access = (login_check.ACCEPTED, "a", ""), (login_check.NO_ACCESS, "b", "portal")
    empty, unchecked = (login_check.EMPTY, "c", "answered with nothing"), (login_check.UNCHECKED, "d", "x")
    assert login_check.summarise([accepted, no_access])[0] == login_check.NO_ACCESS
    assert login_check.summarise([empty, accepted])[0] == login_check.ACCEPTED
    assert login_check.summarise([empty, unchecked]) == (login_check.UNCHECKED, "c", "answered with nothing")


@pytest.mark.parametrize("text", ["[]", "{}", '{"values": [], "total": 0}', '{"total": 0}', "  "])
def test_what_counts_as_empty(settings, text) -> None:
    reply = {"result": {"content": [{"type": "text", "text": text}]}}
    assert login_check.judge(reply, settings)[0] == login_check.EMPTY


def test_content_is_still_accepted(settings) -> None:
    reply = {"result": {"content": [{"type": "text", "text": '{"values": [{"key": "SD-1"}], "total": 1}'}]}}
    assert login_check.judge(reply, settings)[0] == login_check.ACCEPTED


def test_the_rule_can_be_turned_off(settings) -> None:
    settings.set("mcp.verification.login_check.empty_answers_prove_nothing", False)
    assert login_check.judge({"result": {"content": [{"type": "text", "text": "[]"}]}}, settings)[0] == "accepted"


# -- the search, when access is unproved --------------------------------------------------------


def target_with(pipeline, alternative: bool) -> TargetOutcome:
    target = TargetOutcome(target="https://sd.example.test")
    findings = {"origin_status": 200}
    if alternative:
        findings["api_kinds"] = {"jira_service_management": {"url": "https://sd.example.test/rest/servicedeskapi/info"}}
    target.stages.append(StageOutcome(stage=Stage.ENDPOINT, mode=pipeline.mode, findings=findings))
    return target


def unproved() -> Candidate:
    candidate = Candidate(kind="stdio", name="sd-example-test", command=["uvx", "mcp-atlassian"])
    candidate.handshake = Handshake(
        ok=True,
        tools=["jira_get_all_projects"],
        round_trip=True,
        login=login_check.UNCHECKED,
        login_detail="answered with nothing",
    )
    return candidate


def proven_bridge() -> Candidate:
    candidate = Candidate(kind="stdio", name="sd-example-test", command=["python3", "bridge.py"])
    candidate.api_kind = "jira_service_management"
    candidate.handshake = Handshake(
        ok=True,
        tools=["get_my_requests"],
        round_trip=True,
        credential_pending=["SD_EXAMPLE_TEST_TOKEN"],
        login=login_check.ACCEPTED,
    )
    return candidate


def test_unproved_access_with_an_alternative_keeps_searching(ctx) -> None:
    from initialize_claude_ai.mcp_auto_discovery.traditional import TraditionalPipeline

    pipeline = TraditionalPipeline(ctx)
    target = target_with(pipeline, alternative=True)
    target.verified = [unproved()]
    assert pipeline.settled("bridge", target, exhaustive=False) is None


def test_with_no_alternative_nothing_changes(ctx) -> None:
    from initialize_claude_ai.mcp_auto_discovery.traditional import TraditionalPipeline

    pipeline = TraditionalPipeline(ctx)
    target = target_with(pipeline, alternative=False)
    target.verified = [unproved()]
    assert pipeline.settled("bridge", target, exhaustive=False) is not None


def test_the_setting_restores_settling(ctx) -> None:
    from initialize_claude_ai.mcp_auto_discovery.traditional import TraditionalPipeline

    ctx.settings.set("mcp.verification.login_check.unproved_settles", True)
    pipeline = TraditionalPipeline(ctx)
    target = target_with(pipeline, alternative=True)
    target.verified = [unproved()]
    assert pipeline.settled("bridge", target, exhaustive=False) is not None


def test_proven_access_takes_the_slot() -> None:
    target = TargetOutcome(target="https://sd.example.test")
    old = unproved()
    target.absorb(StageOutcome(stage="package", candidates=[old], verified=[old]))
    bridge = proven_bridge()
    target.absorb(StageOutcome(stage="bridge", candidates=[bridge]))
    assert old not in target.verified
    assert target.provisional == [bridge]


# -- telling the operator -------------------------------------------------------------------------


def test_the_alternative_is_named_when_nothing_could_be_checked(ctx) -> None:
    from initialize_claude_ai.mcp_auto_discovery.writer import McpWriter

    target = TargetOutcome(target="https://sd.example.test")
    target.api_kinds = {
        "jira_service_management": {"label": "x", "url": "https://sd.example.test/rest/servicedeskapi/info"}
    }
    candidate = Candidate(
        kind="stdio",
        name="sd-example-test",
        command=["uvx", "mcp-atlassian"],
        handshake=Handshake(ok=True, tools=["t"], round_trip=True),
    )
    hint = McpWriter(ctx).alternative_hint(candidate, ["SD_EXAMPLE_TEST_JIRA_PERSONAL_TOKEN"], target)
    assert "Jira Service Management customer API" in hint and "re-run phase 4" in hint


def test_a_replacement_s_tools_are_named(ctx) -> None:
    from initialize_claude_ai.mcp_auto_discovery.writer import McpWriter, Registration

    writer = McpWriter(ctx)
    registration = Registration(
        name="sd-example-test", candidate=proven_bridge(), entry={"command": "python3", "args": ["bridge.py"]}
    )
    registration.replaces_unusable = "sd-example-test"
    registration.replaced_tools = ["jira_get_issue", "jira_search"]
    writer.published.append(registration)
    replaces = writer.briefing()["servers"]["sd-example-test"]["replaces"]
    assert "jira_get_issue" in replaces and "get_my_requests" in replaces


def test_packaged_settings_equal_the_code(settings) -> None:
    config = settings.section("mcp.verification.login_check")
    assert tuple(config.str_list("empty_total_fields")) == login_check.DEFAULT_EMPTY_TOTAL_FIELDS
    assert tuple(config.str_list("empty_list_fields")) == login_check.DEFAULT_EMPTY_LIST_FIELDS
    assert config.dict_("remedies") == login_check.DEFAULT_REMEDIES
