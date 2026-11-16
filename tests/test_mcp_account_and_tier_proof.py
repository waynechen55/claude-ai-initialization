"""Proof decides across tiers; a login with nothing behind it is an account problem, not "unproved".

sd.example.com: the package stage kept ``@atlassian-dc-mcp/jira`` -- never
login-checked, because its entry named the host bare (``JIRA_HOST=sd.example.com``)
and the token this machine sends sd.example.com was never offered to it -- while
the agent's customer-API bridge a tier down had its login accepted, and was not
marked as that API's bridge. mcp-atlassian timed out once (93 s, after starting
in a second on every earlier run) and was replaced as "does not start".

build.opensuse.org: its entry's login got an HTML 404 for every path,
``openSUSE:Factory`` included, while no login and a made-up one got 401. The
login check called that "failed for another reason", and unproved still
counted as working.
"""

from __future__ import annotations

import sys

import pytest

from initialize_claude_ai.core.secrets import Secret
from initialize_claude_ai.mcp_auto_discovery import login_check
from initialize_claude_ai.mcp_auto_discovery.traditional import TraditionalPipeline
from initialize_claude_ai.mcp_auto_discovery.types import Candidate, Handshake, Stage, StageOutcome, TargetOutcome
from initialize_claude_ai.mcp_auto_discovery.verifier import CandidateVerifier

TOKEN = "".join(chr(c) for c in (111, 98, 115, 45, 112, 97, 115, 115, 45, 52, 50))
JSM = {
    "jira_service_management": {
        "label": "Jira Service Management customer",
        "url": "https://sd.example.test/rest/servicedeskapi/info",
    }
}


def waiting(name: str, origin: str, login: str = "") -> Candidate:
    candidate = Candidate(kind="stdio", name=name, command=["python3", f"{name}.py"], origin=origin)
    candidate.handshake = Handshake(ok=True, tools=["t"], round_trip=True, credential_pending=["X_TOKEN"], login=login)
    return candidate


# -- proof across tiers -------------------------------------------------------------------------


def test_an_accepted_login_takes_the_slot_of_an_earlier_unchecked_candidate() -> None:
    outcome = TargetOutcome(target="https://sd.example.test")
    unchecked = waiting("atlassian-dc", Stage.PACKAGE)
    outcome.absorb(StageOutcome(stage=Stage.PACKAGE, candidates=[unchecked]))
    accepted = waiting("bridge", Stage.BRIDGE, login=login_check.ACCEPTED)
    outcome.absorb(StageOutcome(stage=Stage.BRIDGE, candidates=[accepted]))
    assert outcome.provisional == [accepted]
    assert any("proved less" in note for note in accepted.notes)


def test_two_unchecked_candidates_keep_tier_order() -> None:
    outcome = TargetOutcome(target="https://sd.example.test")
    first = waiting("pkg", Stage.PACKAGE)
    outcome.absorb(StageOutcome(stage=Stage.PACKAGE, candidates=[first]))
    outcome.absorb(StageOutcome(stage=Stage.BRIDGE, candidates=[waiting("bridge", Stage.BRIDGE)]))
    assert outcome.provisional == [first]


def test_an_agent_bridge_is_marked_by_its_tools(ctx) -> None:
    pipeline = TraditionalPipeline(ctx)
    pipeline.api_kinds_for = lambda outcome: dict(JSM)  # type: ignore[method-assign]
    bridge = waiting("sd-example-test-bridge", Stage.BRIDGE)
    bridge.handshake.tools = ["get_my_requests", "get_service_desk_info", "something_else"]
    unrelated = waiting("other", Stage.BRIDGE)
    unrelated.handshake.tools = ["get_my_requests"]
    pipeline.mark_api_kinds(StageOutcome(stage=Stage.BRIDGE, candidates=[bridge, unrelated]), TargetOutcome(target="x"))
    assert bridge.api_kind == "jira_service_management" and unrelated.api_kind == ""


def test_a_bare_host_is_offered_the_credential_in_use(ctx, sandbox) -> None:
    import json

    from initialize_claude_ai.mcp_auto_discovery.existing import ExistingRegistrations

    ctx.settings.set("mcp.discovery.reuse.sources", ["project_file"])
    ctx.settings.set("mcp.mcp_json_path", str(sandbox / ".mcp.json"))
    (sandbox / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "sd-example-test": {
                        "command": "uvx",
                        "args": ["mcp-atlassian"],
                        "env": {"JIRA_URL": "https://sd.example.test", "JIRA_PERSONAL_TOKEN": TOKEN},
                    },
                }
            }
        )
    )
    ExistingRegistrations.for_run(ctx).registrations()
    candidate = Candidate(kind="stdio", name="dc", command=["npx", "-y", "x"], env={"JIRA_HOST": "sd.example.test"})
    CandidateVerifier(ctx)._borrow_in_use(candidate, {"pending": ["JIRA_API_TOKEN"]})
    assert "JIRA_API_TOKEN" in candidate.in_use and TOKEN not in repr(candidate.in_use)


# -- slow is not broken ----------------------------------------------------------------------------


SLOW_ONCE = """
import json, os, sys, time, pathlib
mark = pathlib.Path(os.environ["MARK"])
if not mark.exists():
    mark.write_text("1")
    time.sleep(30)
for line in sys.stdin:
    msg = json.loads(line)
    rid, method = msg.get("id"), msg.get("method")
    if rid is None:
        continue
    if method == "initialize":
        result = {"protocolVersion": msg["params"]["protocolVersion"], "capabilities": {"tools": {}},
                  "serverInfo": {"name": "slow", "version": "1"}}
    elif method == "tools/list":
        result = {"tools": [{"name": "noop", "inputSchema": {"type": "object"}}]}
    else:
        result = {}
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": rid, "result": result}) + "\\n")
    sys.stdout.flush()
"""


def test_an_existing_registration_that_times_out_once_is_tried_again(ctx, sandbox, tmp_path, monkeypatch) -> None:
    import json

    from initialize_claude_ai.mcp_auto_discovery.existing import ExistingRegistrations

    script = tmp_path / "slow.py"
    script.write_text(SLOW_ONCE)
    ctx.dry_run = False
    ctx.runner.which = lambda name, **_: name  # type: ignore[assignment]
    ctx.settings.set("mcp.packages.trial_timeout_seconds", 2)
    ctx.settings.set("mcp.packages.slow_start_timeout_factor", 5.0)
    ctx.settings.set("mcp.discovery.reuse.sources", ["project_file"])
    ctx.settings.set("mcp.mcp_json_path", str(sandbox / ".mcp.json"))
    (sandbox / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "slow-example-test": {
                        "command": sys.executable,
                        "args": [str(script)],
                        "env": {"MARK": str(tmp_path / "mark"), "BASE_URL": "https://slow.example.test"},
                    },
                }
            }
        )
    )
    existing = ExistingRegistrations.for_run(ctx)
    candidate = existing.candidate(existing.registrations()[0], "package")
    CandidateVerifier(ctx).verify(candidate)
    assert candidate.handshake.ok, candidate.handshake.error


# -- an account with nothing behind it -----------------------------------------------------------


OBS = """
import json, os, sys
password = os.environ.get("OBS_PASSWORD", "")
real = password == os.environ.get("REAL", "")
page = "HTTP 404 from https://obs.example.test{path}: <!DOCTYPE html><html><title>Error 404</title></html>"
denied = "HTTP 401 from https://obs.example.test{path}: Authentication required!"
tools = [{"name": n, "inputSchema": {"type": "object", "properties": {}}}
         for n in ("get_my_account", "list_projects", "get_about")]
for line in sys.stdin:
    msg = json.loads(line)
    rid, method = msg.get("id"), msg.get("method")
    if rid is None:
        continue
    if method == "initialize":
        result = {"protocolVersion": msg["params"]["protocolVersion"], "capabilities": {"tools": {}},
                  "serverInfo": {"name": "obs-bridge", "version": "1"}}
    elif method == "tools/list":
        result = {"tools": tools}
    elif method == "tools/call":
        path = "/" + msg["params"]["name"]
        result = {"isError": True, "content": [{"type": "text", "text": (page if real else denied).format(path=path)}]}
    else:
        result = {}
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": rid, "result": result}) + "\\n")
    sys.stdout.flush()
"""


@pytest.fixture
def obs(ctx, tmp_path):
    script = tmp_path / "obs.py"
    script.write_text(OBS)
    ctx.dry_run = False
    ctx.runner.which = lambda name, **_: name  # type: ignore[assignment]
    ctx.settings.set("mcp.packages.trial_timeout_seconds", 20)
    ctx.secrets.resolve = lambda reference, **_: (
        Secret(TOKEN) if reference.startswith("env:") and reference.endswith("OBS_PASSWORD") else None
    )  # type: ignore[assignment]
    candidate = Candidate(
        kind="stdio",
        name="obs-example-test",
        command=[sys.executable, str(script)],
        env={"BASE_URL": "https://obs.example.test", "REAL": TOKEN},
    )
    candidate.secret_env = {"OBS_PASSWORD": "env:OBS_PASSWORD"}
    return candidate


def test_404_with_the_credential_and_401_with_a_stand_in_is_no_account(ctx, obs) -> None:
    candidate = CandidateVerifier(ctx).verify(obs)
    shake = candidate.handshake
    assert shake.login == login_check.NO_ACCESS and shake.login_reason == login_check.NO_ACCOUNT
    assert not candidate.verified and TOKEN not in shake.login_detail


def test_the_remedy_names_the_account_not_the_password(settings) -> None:
    text = login_check.remedy(
        login_check.NO_ACCESS,
        host="obs.example.test",
        variables=["OBS_PASSWORD"],
        detail="x",
        settings=settings,
        reason=login_check.NO_ACCOUNT,
    )
    assert "sign in once" in text and "credential itself is fine" in text


def test_an_identity_tool_s_404_is_no_account(settings) -> None:
    assert login_check.account_missing("get_my_account", login_check.UNCHECKED, "HTTP 404 from /person/x", settings)
    assert not login_check.account_missing("list_projects", login_check.UNCHECKED, "HTTP 404", settings)


def test_a_web_page_is_the_gateway_not_the_api(settings) -> None:
    reply = {"result": {"isError": True, "content": [{"type": "text", "text": "HTTP 404: <!DOCTYPE html><html>x"}]}}
    verdict, detail = login_check.judge(reply, settings)
    assert verdict == login_check.UNCHECKED and "web page" in detail and "HTTP 404" in detail


def test_every_call_failing_is_not_working(ctx) -> None:
    rows = [
        {"tool": t, "verdict": login_check.UNCHECKED, "detail": "the call failed for another reason: HTTP 500"}
        for t in ("a", "b")
    ]
    shake = Handshake(ok=True, tools=["a", "b"], round_trip=True, login=login_check.UNCHECKED, functional=rows)
    CandidateVerifier(ctx)._unverify_failed_calls(shake)
    assert not shake.ok and "every call" in shake.error

    one = Handshake(ok=True, tools=["a"], round_trip=True, login=login_check.UNCHECKED, functional=rows[:1])
    CandidateVerifier(ctx)._unverify_failed_calls(one)
    assert one.ok, "one failed call is below failed_calls_min"


def test_packaged_settings_equal_the_code(settings) -> None:
    config = settings.section("mcp.verification.login_check")
    assert tuple(config.str_list("identity_tool_patterns")) == login_check.DEFAULT_IDENTITY_PATTERNS
    assert tuple(config.str_list("gateway_page_patterns")) == login_check.DEFAULT_GATEWAY_PAGE_PATTERNS
    assert config.int_("failed_calls_min") == login_check.DEFAULT_FAILED_CALLS_MIN
    assert config.dict_("remedies") == login_check.DEFAULT_REMEDIES
    assert config.bool_("compare_with_standin") and config.bool_("failed_calls_unverify")
    assert settings.int_("mcp.discovery.api_kind_min_shared_tools") == 2
    assert settings.int_("mcp.packages.slow_start_retries") == 1
    assert settings.float_("mcp.packages.slow_start_timeout_factor") == 2.0
