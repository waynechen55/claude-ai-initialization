"""A credential is proved by an accepted call, never by a listing -- and belongs to one instance.

sd.example.com's entry ran ``uvx mcp-atlassian`` with ``JIRA_URL=https://sd.example.com``.
It started, listed 49 tools and answered ``ping``, so every check this program
had passed. Every tool call then failed with ``500 Server Error ...
/servicedesk/customer/portals``: the instance sent the request to its customer
portal because it did not accept the caller as a Jira user. Around it, four
more gaps: jira.example.com and sd.example.com were both written to read
``JIRA_PERSONAL_TOKEN`` (one value cannot be two instances' token); the report
told the operator to export a variable the entry did not read; the duplicate
check called the two entries one service and advised deleting the working one;
and the same token was "shape unknown" for Jira and described for Confluence.

The fake servers are real processes speaking JSON-RPC over stdio.
"""

from __future__ import annotations

import json
import sys

import pytest

from initialize_claude_ai.core.secrets import Secret
from initialize_claude_ai.mcp_auto_discovery import login_check
from initialize_claude_ai.mcp_auto_discovery.types import Candidate, Handshake, TargetOutcome, Transport
from initialize_claude_ai.mcp_auto_discovery.verifier import CandidateVerifier, real_credential
from initialize_claude_ai.mcp_auto_discovery.writer import McpWriter

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - 3.10
    import tomli as tomllib  # type: ignore[no-redef]

TOKEN = "".join(chr(c) for c in (116, 107, 110, 45, 102, 97, 107, 101, 45, 49, 50, 51, 52))  # built, never a literal

#: ANSWER decides how a tools/call is answered:
#:   portal    -- {"success": false, "error": "500 ... /servicedesk/customer/portals"} (mcp-atlassian on sd)
#:   rejected  -- isError, "401 Unauthorized"
#:   ok        -- a listing that happens to contain numbers like 4031
#:   unsafe    -- the only tools change things or need arguments nobody can supply
SERVER = """
import json, os, sys
answer = os.environ.get("ANSWER", "ok")
calls = os.environ.get("CALLS_LOG", "")
tools = [
    {"name": "jira_create_issue", "inputSchema": {"type": "object", "properties": {}},
     "annotations": {"readOnlyHint": False}},
    {"name": "jira_get_issue", "inputSchema": {"type": "object", "properties": {"issue_key": {"type": "string"}},
     "required": ["issue_key"]}},
]
if answer != "unsafe":
    tools.append({"name": "jira_get_all_projects", "inputSchema": {"type": "object", "properties": {}},
                  "annotations": {"readOnlyHint": True}})
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
                  "serverInfo": {"name": "fake-atlassian", "version": "1.0"}}
    elif method == "tools/list":
        result = {"tools": tools}
    elif method == "tools/call":
        if calls:
            open(calls, "a").write(msg["params"]["name"] + "\\n")
        if os.environ.get("TOKEN_LOG"):
            open(os.environ["TOKEN_LOG"], "a").write(("set" if os.environ.get("JIRA_PERSONAL_TOKEN") else "unset") + "\\n")
        if not os.environ.get("JIRA_PERSONAL_TOKEN"):
            # A real service refuses a caller with no credential at all.
            result = {"isError": True, "content": [{"type": "text", "text": "401 Unauthorized: no credential"}]}
        elif answer == "portal":
            text = json.dumps({"success": False,
                               "error": "500 Server Error:  for url: https://sd.example.test/servicedesk/customer/portals"})
            result = {"content": [{"type": "text", "text": text}]}
        elif answer == "rejected":
            result = {"isError": True, "content": [{"type": "text", "text": "401 Unauthorized"}]}
        else:
            result = {"content": [{"type": "text", "text": json.dumps([{"key": "PROJ", "id": 4031}])}]}
    else:
        result = {}
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": rid, "result": result}) + "\\n")
    sys.stdout.flush()
"""


@pytest.fixture
def live(ctx, monkeypatch):
    ctx.dry_run = False
    ctx.runner.which = lambda name, **_: name  # type: ignore[assignment]
    ctx.settings.set("mcp.packages.trial_timeout_seconds", 20)
    return ctx


def with_token(ctx, value: str | None = TOKEN):
    ctx.secrets.resolve = lambda reference, **_: (  # type: ignore[assignment]
        Secret(value) if value and reference.startswith("env:") and reference.endswith("JIRA_PERSONAL_TOKEN") else None
    )
    return ctx


def fake(tmp_path, answer: str, url: str = "https://sd.example.test") -> Candidate:
    script = tmp_path / "server.py"
    script.write_text(SERVER)
    candidate = Candidate(
        kind="stdio",
        name="fake-atlassian",
        transport=Transport.STDIO,
        command=[sys.executable, str(script)],
        env={"ANSWER": answer, "JIRA_URL": url, "CALLS_LOG": str(tmp_path / "calls.log")},
    )
    candidate.secret_env = {"JIRA_PERSONAL_TOKEN": "env:JIRA_PERSONAL_TOKEN"}
    return candidate


def calls(tmp_path) -> list[str]:
    log = tmp_path / "calls.log"
    return log.read_text().split() if log.exists() else []


# -- the verdicts, through a real launch --------------------------------------------------


def test_a_portal_redirect_means_the_account_has_no_api_access(live, tmp_path) -> None:
    candidate = CandidateVerifier(with_token(live)).verify(fake(tmp_path, "portal"))

    shake = candidate.handshake
    assert shake.ok and shake.tools, "it started and listed its tools -- which proved nothing about the login"
    assert shake.login == login_check.NO_ACCESS
    assert "/servicedesk/customer" in shake.login_detail
    assert shake.login_refused
    assert not candidate.verified, "a server whose service refuses its account is not working"
    assert candidate.needs_credential, "...it is waiting for a working credential, and is registered as such"
    assert calls(tmp_path) == ["jira_get_all_projects"], "only the read-only tool is ever called"


def test_a_refused_token_is_rejected(live, tmp_path) -> None:
    candidate = CandidateVerifier(with_token(live)).verify(fake(tmp_path, "rejected"))
    assert candidate.handshake.login == login_check.REJECTED
    assert not candidate.verified


def test_an_accepted_call_is_verified_even_when_its_data_has_numbers(live, tmp_path) -> None:
    candidate = CandidateVerifier(with_token(live)).verify(fake(tmp_path, "ok"))
    assert candidate.handshake.login == login_check.ACCEPTED, "issue 4031 is not a 403"
    assert candidate.verified


def test_no_safe_tool_means_nothing_is_called(live, tmp_path) -> None:
    candidate = CandidateVerifier(with_token(live)).verify(fake(tmp_path, "unsafe"))
    assert candidate.handshake.login == login_check.UNCHECKED
    assert calls(tmp_path) == [], "a tool that changes things, or needs an argument, is never called"
    assert candidate.verified, "not proved is not refused"


def test_no_real_credential_means_no_login_is_checked(live, tmp_path) -> None:
    """Without a credential, a read may be asked -- anonymously -- to learn whether the server needs one.

    It is never recorded as a login, and no stand-in is ever sent: the server
    refuses an anonymous caller here, so it is still waiting for its credential.
    """
    (tmp_path / "token.log").touch()
    candidate = fake(tmp_path, "portal")
    candidate.env["TOKEN_LOG"] = str(tmp_path / "token.log")
    candidate = CandidateVerifier(with_token(live, None)).verify(candidate)
    assert candidate.handshake.login == "" and candidate.needs_credential
    assert "set" not in (tmp_path / "token.log").read_text().split(), "no credential, real or stand-in, was sent"


def test_without_a_credential_nothing_is_asked_when_switched_off(live, tmp_path) -> None:
    live.settings.set("mcp.verification.login_check.try_without_credential", False)
    candidate = CandidateVerifier(with_token(live, None)).verify(fake(tmp_path, "portal"))
    assert candidate.handshake.login == "" and calls(tmp_path) == []


def test_warn_reports_without_demoting(live, tmp_path) -> None:
    live.settings.set("mcp.verification.login_check.mode", "warn")
    candidate = CandidateVerifier(with_token(live)).verify(fake(tmp_path, "portal"))
    assert candidate.handshake.login == login_check.NO_ACCESS
    assert candidate.verified and not candidate.handshake.login_refused


def test_off_calls_nothing(live, tmp_path) -> None:
    live.settings.set("mcp.verification.login_check.mode", "off")
    candidate = CandidateVerifier(with_token(live)).verify(fake(tmp_path, "portal"))
    assert candidate.handshake.login == "" and calls(tmp_path) == []


def test_the_verification_table_has_an_authenticated_column(live, tmp_path) -> None:
    candidate = CandidateVerifier(with_token(live)).verify(fake(tmp_path, "portal"))
    row = next(c for c in CandidateVerifier.audit(candidate) if c.name == "authenticated")
    assert row.ok is False and "NO API ACCESS" in row.detail


# -- choosing and judging, as units --------------------------------------------------------


def test_pick_prefers_identity_and_never_takes_a_mutating_tool(settings) -> None:
    specs = [
        {"name": "delete_everything", "required": [], "read_only": True},
        {"name": "list_projects", "required": [], "read_only": None},
        {"name": "get_me", "required": [], "read_only": None},
        {"name": "get_issue", "required": ["key"], "read_only": True},
        {"name": "search", "required": [], "read_only": True},
    ]
    # Identity first, then listings, then other read-only tools -- never the
    # mutating one, never one whose required argument nobody can fill.
    assert [name for name, _ in login_check.pick(specs, settings)] == ["get_me", "list_projects", "search"]


def test_a_failure_is_read_for_markers_and_a_success_never_is(settings) -> None:
    ok = {"result": {"content": [{"type": "text", "text": "403 issues, 401 open"}]}}
    assert login_check.judge(ok, settings)[0] == login_check.ACCEPTED
    flagged = {"result": {"content": [{"type": "text", "text": '{"success": false, "error": "forbidden"}'}]}}
    assert login_check.judge(flagged, settings)[0] == login_check.NO_ACCESS
    other = {"result": {"isError": True, "content": [{"type": "text", "text": "bad argument"}]}}
    assert login_check.judge(other, settings)[0] == login_check.UNCHECKED


def test_real_credential_needs_every_credential_set() -> None:
    assert real_credential({"JIRA_PERSONAL_TOKEN": TOKEN, "JIRA_URL": "https://x"})
    assert not real_credential({"JIRA_PERSONAL_TOKEN": "", "JIRA_URL": "https://x"})
    assert not real_credential({"JIRA_PERSONAL_TOKEN": "${JIRA_PERSONAL_TOKEN}"})
    assert real_credential({"Authorization": "Bearer " + TOKEN + TOKEN})
    assert not real_credential({"JIRA_URL": "https://x"})


def test_the_packaged_settings_are_the_code_s() -> None:
    from pathlib import Path

    data = Path(login_check.__file__).resolve().parent.parent / "data" / "40-mcp.toml"
    packaged = tomllib.loads(data.read_text(encoding="utf-8"))["mcp"]["verification"]["login_check"]
    assert tuple(packaged["tool_patterns"]) == login_check.DEFAULT_TOOL_PATTERNS
    assert tuple(packaged["never_patterns"]) == login_check.DEFAULT_NEVER_PATTERNS
    assert tuple(packaged["rejected_markers"]) == login_check.DEFAULT_REJECTED_MARKERS
    assert tuple(packaged["no_access_markers"]) == login_check.DEFAULT_NO_ACCESS_MARKERS
    assert tuple(packaged["failure_fields"]) == login_check.DEFAULT_FAILURE_FIELDS
    assert packaged["max_calls"] == login_check.DEFAULT_MAX_CALLS
    assert packaged["remedies"] == login_check.DEFAULT_REMEDIES


def test_a_start_up_failure_that_names_a_refused_login_is_said_so(live) -> None:
    shake = Handshake(error="HTTPError: 401 Client Error: Unauthorized for url: https://x.example.test/rest")
    assert CandidateVerifier(live).credential_failure(shake) == login_check.REJECTED
    assert shake.login == login_check.REJECTED


# -- the report ------------------------------------------------------------------------------


@pytest.fixture
def writer_live(live):
    live.settings.set("mcp.registration.verify_as_written", False)
    return live


def published(writer: McpWriter, target: str, candidate: Candidate):
    return writer.publish(TargetOutcome(target=target, verified=[candidate]))[0]


def test_a_refused_credential_is_reported_with_its_remedy(writer_live, tmp_path) -> None:
    candidate = CandidateVerifier(with_token(writer_live)).verify(fake(tmp_path, "portal"))
    writer = McpWriter(writer_live)
    outcome = TargetOutcome(target="https://sd.example.test", provisional=[candidate])
    registration = writer.publish(outcome)[0]

    assert registration.provisional
    assert "sd.example.test" in registration.credential_hint
    assert "API access" in registration.credential_hint
    briefing = writer.briefing()
    assert registration.name in briefing["these_were_given_a_credential_their_service_refused"]
    assert registration.name not in briefing.get("these_need_a_real_credential_before_they_work", {})
    assert outcome.to_dict()["credential_refused"][0]["verdict"] == login_check.NO_ACCESS


# -- one credential, one instance -----------------------------------------------------------


def atlassian(tmp_path, url: str) -> Candidate:
    candidate = Candidate(
        kind="stdio",
        name="",
        transport=Transport.STDIO,
        command=["uvx", "mcp-atlassian"],
        env={"JIRA_URL": url},
        handshake=Handshake(ok=True, tools=["jira_get_issue"], round_trip=True),
    )
    candidate.secret_env = {"JIRA_PERSONAL_TOKEN": "env:JIRA_PERSONAL_TOKEN"}
    return candidate


def test_two_instances_of_one_package_read_two_variables(writer_live, tmp_path) -> None:
    writer = McpWriter(writer_live)
    first = published(writer, "https://jira.one.test", atlassian(tmp_path, "https://jira.one.test"))
    second = published(writer, "https://jira.two.test", atlassian(tmp_path, "https://jira.two.test"))

    # Each instance reads a name of its own -- the first too, not only the
    # second: which instance came first must not decide who owns the generic name.
    one = first.entry["env"]["JIRA_PERSONAL_TOKEN"].strip("${}")
    variable = second.entry["env"]["JIRA_PERSONAL_TOKEN"].strip("${}")
    assert one != variable and "JIRA_PERSONAL_TOKEN" not in (one, variable)
    assert one.endswith("_JIRA_PERSONAL_TOKEN") and variable.endswith("_JIRA_PERSONAL_TOKEN")
    assert variable in writer.briefing()["required_environment_variables"], "the instruction names it"
    assert not second.duplicates, "two instances are not one service"


def test_the_waiting_instruction_names_the_variable_the_entry_reads(writer_live, tmp_path) -> None:
    candidate = atlassian(tmp_path, "https://sd.example.test")
    candidate.handshake.credential_pending = ["JIRA_PERSONAL_TOKEN"]
    writer = McpWriter(writer_live)
    registration = writer.publish(TargetOutcome(target="https://sd.example.test", provisional=[candidate]))[0]
    read = sorted(v.strip("${}:-") for v in registration.entry["env"].values() if "PERSONAL_TOKEN" in v)
    assert registration.needs_env == read


def test_entries_already_sharing_a_credential_across_services_are_reported(writer_live, sandbox) -> None:
    (sandbox / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "jira-one": {
                        "command": "uvx",
                        "args": ["mcp-atlassian"],
                        "env": {"JIRA_URL": "https://jira.one.test", "JIRA_PERSONAL_TOKEN": "${JIRA_PERSONAL_TOKEN}"},
                    },
                    "sd-two": {
                        "command": "uvx",
                        "args": ["mcp-atlassian"],
                        "env": {"JIRA_URL": "https://sd.two.test", "JIRA_PERSONAL_TOKEN": "${JIRA_PERSONAL_TOKEN}"},
                    },
                }
            }
        )
    )
    writer_live.settings.set("mcp.registration.json_path", str(sandbox / ".mcp.json"))
    findings = McpWriter(writer_live).shared_secret_findings()
    assert [f["variable"] for f in findings] == ["JIRA_PERSONAL_TOKEN"]
    assert sorted(findings[0]["servers"]) == ["jira-one", "sd-two"]


def test_one_command_pointed_at_two_hosts_is_not_a_duplicate(writer_live, sandbox, tmp_path) -> None:
    (sandbox / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "jira-one": {
                        "command": "uvx",
                        "args": ["mcp-atlassian"],
                        "env": {"JIRA_URL": "https://jira.one.test"},
                    },
                }
            }
        )
    )
    writer_live.settings.set("mcp.registration.json_path", str(sandbox / ".mcp.json"))
    writer = McpWriter(writer_live)
    other = published(writer, "https://sd.two.test", atlassian(tmp_path, "https://sd.two.test"))
    same = published(writer, "https://jira.one.test", atlassian(tmp_path, "https://jira.one.test"))
    assert not other.duplicates
    assert [d["name"] for d in same.duplicates] == ["jira-one"]


# -- describing the credential --------------------------------------------------------------


def test_a_personal_access_token_is_described_with_its_host(writer_live, tmp_path) -> None:
    writer = McpWriter(writer_live)
    candidate = atlassian(tmp_path, "https://sd.example.test")
    hint = writer.credential_hint(candidate, "JIRA_PERSONAL_TOKEN")
    assert "personal access token" in hint
    assert writer.service_host(candidate) == "sd.example.test"


def test_a_shape_learned_for_one_target_serves_the_sibling(writer_live, tmp_path) -> None:
    writer = McpWriter(writer_live)
    confluence = atlassian(tmp_path, "https://wiki.example.test")
    confluence.secret_env = {"CONFLUENCE_API_SECRET": "env:CONFLUENCE_API_SECRET"}
    confluence.secret_formats = {"CONFLUENCE_API_SECRET": "an opaque string from the vendor"}
    jira = atlassian(tmp_path, "https://jira.example.test")
    jira.secret_env = {"JIRA_API_SECRET": "env:JIRA_API_SECRET"}
    assert writer.credential_shape(confluence, "CONFLUENCE_API_SECRET")[0] == "an opaque string from the vendor"
    assert writer.credential_shape(jira, "JIRA_API_SECRET")[0] == "an opaque string from the vendor"


# -- generated bridges ---------------------------------------------------------------------


def test_a_generated_bridge_marks_its_tools_read_only() -> None:
    from initialize_claude_ai.mcp_auto_discovery.bridge import _BRIDGE_SOURCE

    assert '"readOnlyHint"' in _BRIDGE_SOURCE
