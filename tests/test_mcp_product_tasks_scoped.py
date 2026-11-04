"""smelt.example.net's working GraphQL bridge was left out of .mcp.json: it was asked GitLab's question.

* ``[mcp.contract.products.gitlab]``'s "read the current user" sends GitLab's
  own ``{ currentUser { username } }`` through any tool named ``graphql_query``.
  smelt's bridge lists one, answered 400 "Cannot query field currentUser", and
  failed what it is used for -- although it did smelt's job. A product's own
  task is now asked only where the address *is* that product.
* A call refused as a request the server does not have is a task that does not
  fit, never a failure.
* A declared credential slot is not proof a login is needed: a server whose
  usage tasks pass with nothing in it has the slot optional.
"""

from __future__ import annotations

import sys

import pytest

from initialize_claude_ai.mcp_auto_discovery import contract, login_check
from initialize_claude_ai.mcp_auto_discovery.types import Candidate, TargetOutcome
from initialize_claude_ai.mcp_auto_discovery.verifier import CandidateVerifier

SERVER = """
import json, os, sys
GITLAB = os.environ.get("IS_GITLAB") == "1"
tools = [
    {"name": "introspect_schema", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "graphql_query", "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}},
     "required": ["query"]}},
]
for line in sys.stdin:
    msg = json.loads(line)
    rid, method = msg.get("id"), msg.get("method")
    if rid is None:
        continue
    if method == "initialize":
        result = {"protocolVersion": msg["params"]["protocolVersion"], "capabilities": {"tools": {}},
                  "serverInfo": {"name": "graphql", "version": "1"}}
    elif method == "tools/list":
        result = {"tools": tools}
    elif method == "tools/call":
        name, args = msg["params"]["name"], msg["params"].get("arguments") or {}
        query = str(args.get("query") or "")
        if name == "introspect_schema":
            result = {"content": [{"type": "text", "text": '{"__schema": {"queryType": {"name": "Query"}}}'}]}
        elif "currentUser" in query and GITLAB:
            user = "someone" if os.environ.get("THE_HEADERS") else None
            result = {"content": [{"type": "text", "text": json.dumps({"currentUser": user and {"username": user}})}]}
        elif "currentUser" in query:
            result = {"isError": True, "content": [{"type": "text", "text":
                      'GraphQL request failed: Bad Request {"errors":[{"message":"Cannot query field '
                      '\\\\"currentUser\\\\" on type \\\\"Query\\\\"."}]}'}]}
        else:
            result = {"content": [{"type": "text", "text": '{"data": {"incidents": [{"id": 1}]}}'}]}
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
    ctx.secrets.resolve = lambda reference, **_: None  # type: ignore[assignment]
    return ctx


def bridge(tmp_path, host: str, *, gitlab: bool = False) -> Candidate:
    script = tmp_path / "graphql_server.py"
    script.write_text(SERVER)
    env = {"ENDPOINT": f"https://{host}/graphql/"}
    if gitlab:
        env["IS_GITLAB"] = "1"
    candidate = Candidate(kind="stdio", name=host.replace(".", "-"), command=[sys.executable, str(script)], env=env)
    candidate.secret_env = {"THE_HEADERS": "env:THE_HEADERS"}  # the proposer declared a slot
    return candidate


def gitlab_task_names(tasks) -> list[str]:
    return [t.name for t in tasks if t.source == "product gitlab"]


# -- which tasks an address is asked -------------------------------------------------------------


def test_a_product_s_own_task_is_asked_only_where_the_address_is_that_product(settings) -> None:
    assert gitlab_task_names(contract.tasks_for(settings, "smelt.example.test", ["graphql"])) == []
    assert gitlab_task_names(contract.tasks_for(settings, "smelt.example.test", None)) == [], "unknown is not GitLab"
    assert gitlab_task_names(contract.tasks_for(settings, "gitlab.example.test", ["gitlab", "graphql"])) == [
        "read the current user"
    ]


def test_a_value_free_task_stays_with_any_server_listing_its_tool(settings) -> None:
    names = [t.name for t in contract.tasks_for(settings, "smelt.example.test", None)]
    assert "describe the API" in names and "list the projects the account can see" in names


def test_a_task_sending_arguments_belongs_to_its_product_without_a_scope(settings) -> None:
    raw = {"name": "x", "tools": ["^q$"], "arguments": {"^query$": "{ me }"}}
    assert contract.scope_of(settings, {}, raw) == "address"
    assert contract.scope_of(settings, {}, {"name": "y", "tools": ["^info$"]}) == "tools"
    assert contract.scope_of(settings, {"scope": "tools"}, raw) == "tools", "the product's own word wins"
    settings.set("mcp.contract.scope_by_arguments", False)
    assert contract.scope_of(settings, {}, raw) == "tools"


def test_scoping_off_restores_the_old_reading(settings) -> None:
    settings.set("mcp.contract.scope_product_tasks", False)
    assert gitlab_task_names(contract.tasks_for(settings, "smelt.example.test", ["graphql"])) == [
        "read the current user"
    ]


def test_the_pipeline_records_what_the_address_is_for_the_verifier(ctx) -> None:
    from initialize_claude_ai.mcp_auto_discovery.traditional import TraditionalPipeline

    pipeline = TraditionalPipeline(ctx)
    pipeline.auth_plan_for = lambda outcome: {"product": "gitlab"}  # type: ignore[method-assign]
    pipeline.api_kinds_for = lambda outcome: {  # type: ignore[method-assign]
        "graphql": {"label": "GraphQL", "url": "https://api.gitlab.example.test/api/graphql"}
    }
    assert pipeline.know_job(TargetOutcome(target="https://gitlab.example.test")) == ["gitlab", "graphql"]
    jobs = ctx.state["mcp_address_job"]
    assert jobs["gitlab.example.test"] == jobs["api.gitlab.example.test"] == ["gitlab", "graphql"]
    candidate = Candidate(kind="stdio", name="g", command=["x"], env={"URL": "https://api.gitlab.example.test"})
    assert CandidateVerifier(ctx).address_job(candidate, "api.gitlab.example.test") == ["gitlab", "graphql"]
    assert CandidateVerifier(ctx).address_job(Candidate(kind="stdio", name="s"), "smelt.example.test") is None


# -- a request the server does not have ----------------------------------------------------------


def _reply(text: str, error: bool = True) -> dict:
    return {"result": {"isError": error, "content": [{"type": "text", "text": text}]}}


def test_a_request_the_server_does_not_have_does_not_fit_and_is_not_a_failure(settings) -> None:
    call = {"expect": ["username"]}
    for text in (
        "HTTPError: HTTP Error 400: Bad Request",
        'GraphQL request failed: Bad Request {"errors":[{"message":"Cannot query field \\"currentUser\\""}]}',
    ):
        passed, detail = contract.judge(_reply(text), call, settings)
        assert passed is None and detail.startswith(contract.NOT_FITTING)
    assert contract.judge(_reply("401 Unauthorized"), call, settings)[0] is False, "a refused login still counts"
    assert contract.judge(_reply("403 Forbidden"), call, settings)[0] is False


def test_the_verdict_counts_only_the_tasks_that_fit(settings) -> None:
    misfit = {"task": "read the current user", "passed": None, "detail": f"{contract.NOT_FITTING}: 400"}
    passed = {"task": "describe the API", "passed": True, "detail": "passed"}
    assert contract.verdict([passed, misfit])[0] is True
    assert contract.verdict([misfit]) == (None, f"no usage task fits this server (1 {contract.NOT_FITTING})")
    settings.set("mcp.contract.misfit_is_not_a_failure", False)
    assert contract.judge(_reply("HTTP Error 400: Bad Request"), {"expect": []}, settings)[0] is False


def test_a_misfit_is_not_read_as_try_again_later_by_the_repairs() -> None:
    from initialize_claude_ai.mcp_auto_discovery import repair
    from initialize_claude_ai.mcp_auto_discovery.types import Handshake

    candidate = Candidate(kind="stdio", name="x")
    candidate.handshake = Handshake(
        ok=False, contract=[{"task": "t", "passed": None, "detail": f"{contract.NOT_FITTING}: 400"}]
    )
    later = Candidate(kind="stdio", name="y")
    later.handshake = Handshake(ok=False, contract=[{"task": "t", "passed": None, "detail": "inconclusive -- 503"}])
    assert repair.classify(later) == []
    assert contract.fits(candidate.handshake.contract[0]) is False


# -- smelt replayed -------------------------------------------------------------------------------


def test_smelt_s_bridge_does_its_job_and_its_declared_slot_is_optional(live, tmp_path) -> None:
    live.state["mcp_address_job"] = {"smelt.example.test": ["graphql"]}
    candidate = CandidateVerifier(live).verify(bridge(tmp_path, "smelt.example.test"))
    shake = candidate.handshake
    assert all(row["task"] != "read the current user" for row in shake.contract)
    assert candidate.verified, shake.error
    assert shake.credential_optional == ["THE_HEADERS"] and not shake.credential_pending
    assert shake.login_reason == login_check.WITHOUT_LOGIN


def test_even_asked_another_product_s_task_it_is_not_failed_by_it(live, tmp_path) -> None:
    """The old scope, the same server: the 400 is a misfit, and the job it does decides."""
    live.settings.set("mcp.contract.scope_product_tasks", False)
    candidate = CandidateVerifier(live).verify(bridge(tmp_path, "smelt.example.test"))
    rows = {row["task"]: row for row in candidate.handshake.contract}
    assert rows["read the current user"]["passed"] is None and not contract.fits(rows["read the current user"])
    assert candidate.verified, candidate.handshake.error
    assert any("not counted" in note for note in candidate.notes)


def test_a_gitlab_address_is_still_asked_its_current_user(live, tmp_path) -> None:
    live.state["mcp_address_job"] = {"gitlab.example.test": ["gitlab", "graphql"]}
    verifier = CandidateVerifier(live)
    plan = verifier.contract_plan(bridge(tmp_path, "gitlab.example.test", gitlab=True))
    assert plan is not None and "read the current user" in [t.name for t in plan["tasks"]]
    candidate = verifier.verify(bridge(tmp_path, "gitlab.example.test", gitlab=True))
    # Anonymous, GitLab names no current user: the job is not done, the token is still waited for.
    assert not candidate.verified and candidate.handshake.credential_pending == ["THE_HEADERS"]


def test_packaged_settings_equal_the_code(settings) -> None:
    assert settings.bool_("mcp.contract.scope_product_tasks") is contract.DEFAULT_SCOPE_PRODUCT_TASKS
    assert settings.bool_("mcp.contract.scope_by_arguments") is contract.DEFAULT_SCOPE_BY_ARGUMENTS
    assert settings.str_("mcp.contract.default_scope") == contract.DEFAULT_SCOPE
    assert settings.bool_("mcp.contract.misfit_is_not_a_failure") is True
    assert tuple(settings.str_list("mcp.contract.not_fitting_markers")) == contract.DEFAULT_NOT_FITTING_MARKERS
    assert settings.str_("mcp.contract.products.gitlab.scope") == "address"
    assert settings.bool_("mcp.verification.login_check.job_done_is_no_login_needed") is True
