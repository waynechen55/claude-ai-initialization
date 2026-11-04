"""The second run on this machine: sd.example.com answered 502, build.example.net refused every login.

* sd.example.com answered ``502 Bad Gateway`` while it was identified: "product
  unknown; logins offered: none seen", its customer API undetected -- and a
  waiting mcp-atlassian closed the bridge stage. Identification cut short by
  "try again later" is asked again, and while it stays undecided nothing
  unproven settles the address.
* build.example.net's openapi-proxy still searched packages; it was replaced by a
  server that could do nothing until a variable nobody exports was set. An
  existing entry shown short of its job stays over one that proved less.
* build.example.net refused the encoded login and the SSH signature alike -- said
  once, plainly, with what to check.
* The agent runtime's Claude Code was too old for the model ("does not support
  this model"): another runtime, else that runtime's default model.
"""

from __future__ import annotations

from initialize_claude_ai.core.http import Response
from initialize_claude_ai.core.report import PhaseResult
from initialize_claude_ai.mcp_auto_discovery import login_check, runtime
from initialize_claude_ai.mcp_auto_discovery.types import Candidate, Handshake, Stage, StageOutcome, TargetOutcome

TARGET = "https://sd.example.test"


def waiting(name: str = "mcp-atlassian") -> Candidate:
    candidate = Candidate(kind="stdio", name=name, command=["uvx", name], origin=Stage.PACKAGE)
    candidate.handshake = Handshake(
        ok=True, tools=["jira_get_all_projects"], round_trip=True, credential_pending=["JIRA_PERSONAL_TOKEN"]
    )
    return candidate


class BusyClient:
    """Answers 502 to everything, as sd.example.com did."""

    def __init__(self) -> None:
        self.asked = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get(self, url, **_):
        self.asked += 1
        return Response(url=url, status=502, headers={}, text="Bad Gateway")


def test_a_busy_server_leaves_identification_undecided_and_asked_again(ctx, monkeypatch) -> None:
    from initialize_claude_ai.mcp_auto_discovery.traditional import TraditionalPipeline

    ctx.settings.set("mcp.discovery.auth.read_challenges", True)
    client = BusyClient()
    monkeypatch.setattr(type(ctx), "http", lambda self, **_: client)
    outcome = TargetOutcome(target=TARGET)
    pipeline = TraditionalPipeline(ctx)
    pipeline.auth_plan_for(outcome)
    assert "HTTP 502" in outcome.identification_undecided
    once = client.asked
    ctx.settings.set("mcp.discovery.identify_retries", 0)
    client.asked = 0
    TraditionalPipeline(ctx).auth_plan_for(TargetOutcome(target=TARGET))
    assert once > client.asked, "asked again before deciding anything from it"


def test_nothing_unproven_settles_an_undecided_address(ctx) -> None:
    from initialize_claude_ai.mcp_auto_discovery.traditional import TraditionalPipeline

    pipeline = TraditionalPipeline(ctx)
    pipeline.api_kinds_for = lambda outcome: {}  # type: ignore[method-assign]
    outcome = TargetOutcome(target=TARGET, provisional=[waiting()])
    assert pipeline.settled(Stage.BRIDGE, outcome, exhaustive=False) is not None, "as before when decided"
    outcome.identification_undecided = "sd.example.test answered HTTP 502 while it was being identified"
    assert pipeline.settled(Stage.BRIDGE, outcome, exhaustive=False) is None
    proven = waiting("proven")
    proven.handshake.login = login_check.ACCEPTED
    outcome.provisional.append(proven)
    assert pipeline.settled(Stage.BRIDGE, outcome, exhaustive=False) is not None, "proof still settles it"


def test_an_undecided_address_is_reported_with_when_to_run_again(ctx) -> None:
    from initialize_claude_ai.mcp_auto_discovery.writer import McpWriter
    from initialize_claude_ai.phases.phase4_mcp_auto_discovery import McpPhase

    outcome = TargetOutcome(target=TARGET)
    outcome.identification_undecided = "sd.example.test answered HTTP 502 while it was being identified"
    result = PhaseResult(phase="mcp")
    McpPhase(ctx)._report_address_facts(result, McpWriter(ctx), outcome)
    text = " ".join(r for s in result.steps for r in s.remediation)
    assert "--mcp-server sd.example.test" in text and "502" in text


# -- an existing entry is replaced only by something better proven ------------------------------------


def test_an_entry_short_of_its_job_stays_over_one_that_proved_less(ctx) -> None:
    from initialize_claude_ai.mcp_auto_discovery.writer import McpWriter, Registration

    outcome = TargetOutcome(target="https://build.example.test")
    old = Candidate(kind="stdio", name="build-example-test", command=["uvx", "mcp-openapi-proxy==0.4.0"])
    old.handshake = Handshake(
        ok=False,
        tools=["get_search_package"],
        round_trip=True,
        login=login_check.UNCHECKED,
        contract=[
            {"task": "search packages", "passed": True},
            {"task": "read the account's own record", "passed": False},
        ],
    )
    new = Candidate(kind="stdio", name="obs-openapi-mcp", command=["npx", "-y", "@ivotoby/openapi-mcp-server"])
    new.handshake = Handshake(ok=True, tools=["x"], round_trip=True, credential_pending=["API_HEADERS"])
    unusable = {"build-example-test": (old, "failed what it is used for (usage task failed)")}
    registration = Registration(name="build-example-test", candidate=new, entry={"command": "npx"})
    held = McpWriter(ctx)._holds_its_place(registration, outcome, unusable)
    assert "still did more" in held
    better = Candidate(kind="stdio", name="bridge", command=["python3", "bridge.py"])
    better.handshake = Handshake(
        ok=True,
        tools=["get_my_account"],
        round_trip=True,
        login=login_check.ACCEPTED,
        contract=[{"task": "a", "passed": True}, {"task": "b", "passed": True}],
    )
    registration = Registration(name="build-example-test", candidate=better, entry={"command": "python3"})
    assert "still did more" not in McpWriter(ctx)._holds_its_place(registration, outcome, unusable)


def test_an_entry_that_does_not_start_is_still_replaced(ctx) -> None:
    from initialize_claude_ai.mcp_auto_discovery.writer import McpWriter, Registration

    old = Candidate(kind="stdio", name="x", command=["x"], start_failure="it exited")
    old.handshake = Handshake(ok=False, contract=[{"task": "a", "passed": True}])
    new = waiting("y")
    registration = Registration(name="x", candidate=new, entry={"command": "y"})
    held = McpWriter(ctx)._holds_its_place(registration, TargetOutcome(target=TARGET), {"x": (old, "does not start")})
    assert "still did more" not in held


# -- refused every way: said once --------------------------------------------------------------------------


def refused(name: str, source: str) -> Candidate:
    candidate = Candidate(kind="stdio", name=name, command=["python3", f"{name}.py"])
    candidate.handshake = Handshake(
        ok=True,
        tools=["get_my_account"],
        round_trip=True,
        login=login_check.REJECTED,
        login_refused=True,
        login_credential="fp",
        login_source=source,
        login_reason=login_check.SAME_AS_ANONYMOUS,
    )
    return candidate


def test_a_login_refused_every_way_is_said_once_with_what_to_check(ctx) -> None:
    from initialize_claude_ai.phases.phase4_mcp_auto_discovery import McpPhase

    outcome = TargetOutcome(target="https://build.example.test")
    outcome.stages.append(
        StageOutcome(
            stage=Stage.BRIDGE,
            candidates=[
                refused("bridge", "what you export for build.example.test"),
                refused("osc-bridge", "BUILD_EXAMPLE_TEST_SSH_PUBLIC_KEY"),
            ],
        )
    )
    result = PhaseResult(phase="mcp")
    McpPhase(ctx)._report_refused_every_way(result, outcome)
    [step] = [s for s in result.steps if s.name.endswith(":: your login")]
    assert (
        "sign in on its web page" in step.remediation[0] and "BUILD_EXAMPLE_TEST_SSH_PUBLIC_KEY" in step.remediation[0]
    )


def test_nothing_is_said_when_something_proved_access(ctx) -> None:
    from initialize_claude_ai.phases.phase4_mcp_auto_discovery import McpPhase

    ok = refused("good", "x")
    ok.handshake.login, ok.handshake.login_refused, ok.handshake.login_reason = login_check.ACCEPTED, False, ""
    outcome = TargetOutcome(target="https://build.example.test")
    outcome.stages.append(StageOutcome(stage=Stage.BRIDGE, candidates=[refused("bad", "y"), ok]))
    result = PhaseResult(phase="mcp")
    McpPhase(ctx)._report_refused_every_way(result, outcome)
    assert not result.steps


# -- the agent runtime and its model ------------------------------------------------------------------------


def test_a_runtime_too_old_for_the_model_is_its_own_kind() -> None:
    error = "API Error: 400 Claude Code 2.1.259 does not support this model; version 2.1.300 or later is needed"
    assert runtime.failure_kind(error) == runtime.UNSUPPORTED_MODEL
    assert runtime.failure_kind("The model x is not available on your vertex deployment") == runtime.UNUSABLE


def test_without_another_runtime_its_default_model_is_used(ctx) -> None:
    from initialize_claude_ai.mcp_auto_discovery.runtime import AgentRuntime

    ctx.settings.set("mcp.agent.model", "configured-model")
    rt = next(iter(cls for cls in AgentRuntime.__subclasses__()))(ctx)
    assert rt.model() == "configured-model"
    rt.configured_model_unsupported = True
    assert rt.model() == "", "the runtime's own default"


def test_packaged_settings_equal_the_code(settings) -> None:
    packaged = settings.dict_("mcp.agent.failure_patterns")
    assert tuple(packaged["unsupported_model"]) == runtime.FAILURE_PATTERNS["unsupported_model"]
    assert settings.int_("mcp.discovery.identify_retries") == 2
    assert settings.bool_("mcp.discovery.undecided_keeps_searching") is True
    assert settings.bool_("mcp.registration.keep_more_proven_existing") is True
