"""A model credential that stopped working stops the agents once, not per stage.

smelt.example.net lost its agents part-way through a run: the Google Cloud
credential behind the model expired and every agent call answered "Could not
load Google Cloud credentials". No pattern knew that sentence, so it was an
ordinary failure -- retried three times a stage, for every target after it --
and the fallback's advice was to raise ``mcp.agent.max_attempts``. The run
could neither fix it nor say which addresses had to be done again.
"""

from __future__ import annotations

import pytest

from initialize_claude_ai.core.proc import CommandResult
from initialize_claude_ai.core.report import Status
from initialize_claude_ai.mcp_auto_discovery import agent_credentials
from initialize_claude_ai.mcp_auto_discovery.agentic import (
    _FAILURE_REMEDY,
    AgenticPipeline,
    _merge,
)
from initialize_claude_ai.mcp_auto_discovery.runtime import UNUSABLE, CliRuntime, SdkRuntime
from initialize_claude_ai.mcp_auto_discovery.types import (
    Candidate,
    Mode,
    Stage,
    StageOutcome,
    TargetOutcome,
)

GOOGLE = "Could not load Google Cloud credentials. Run gcloud auth application-default login"


def pipeline(ctx) -> AgenticPipeline:
    ctx.dry_run = False
    made = AgenticPipeline(ctx)
    made._runtimes = [SdkRuntime(ctx), CliRuntime(ctx)]
    made._begin_target()
    return made


def failed(detail: str = GOOGLE) -> StageOutcome:
    return StageOutcome(stage=Stage.ENDPOINT, mode=Mode.AGENTIC, detail=detail, error="agent-failed")


def proven() -> StageOutcome:
    return StageOutcome(
        stage=Stage.ENDPOINT,
        mode=Mode.AGENTIC,
        status=Status.OK,
        verified=[Candidate(kind="url", url="https://example.test/mcp")],
    )


def fallback_stage(name: str, _target: TargetOutcome) -> StageOutcome:
    return StageOutcome(stage=name, mode=Mode.TRADITIONAL, status=Status.OK, detail="worked")


class Probe:
    """Stands in for ``runner.probe`` and remembers what it was asked to run."""

    def __init__(self, returncode: int = 0, stderr: str = "") -> None:
        self.returncode, self.stderr, self.calls = returncode, stderr, []

    def __call__(self, argv, **_kwargs) -> CommandResult:
        self.calls.append(list(argv))
        return CommandResult(argv=list(argv), returncode=self.returncode, stdout="ya29.secret", stderr=self.stderr)


def install(ctx, monkeypatch, probe: Probe) -> Probe:
    monkeypatch.setattr(ctx.runner, "probe", probe)
    monkeypatch.setattr(ctx, "tool", lambda name, **_k: f"/usr/bin/{name}")
    return probe


# -- recognising it ----------------------------------------------------------------


def test_the_google_wording_is_an_installation_failure(ctx) -> None:
    assert AgenticPipeline(ctx)._failure_kind(GOOGLE) == UNUSABLE


def test_profiles_come_from_configuration(ctx) -> None:
    names = {profile.name for profile in agent_credentials.profiles(ctx.settings)}
    assert {"google_cloud", "aws"} <= names


def test_a_refresh_named_by_settings_key_resolves_to_that_argv(ctx) -> None:
    profile = agent_credentials.profile_for(ctx.settings, GOOGLE)
    assert profile is not None
    assert profile.refresh == ctx.settings.str_list("auth.google_cloud.commands.print_adc_token")
    assert profile.remedy


def test_a_new_provider_is_one_table(ctx) -> None:
    ctx.settings.set(
        "mcp.agent.credential_failures.acme",
        {"markers": ["acme token expired"], "refresh": [], "remedy": "acme login"},
    )
    assert AgenticPipeline(ctx)._failure_kind("ACME token expired at noon") == UNUSABLE
    assert agent_credentials.profile_for(ctx.settings, "acme token expired").remedy == "acme login"


# -- one refresh, then a retry that costs nothing ------------------------------------


def test_a_refresh_that_works_retries_without_spending_an_attempt(ctx, monkeypatch) -> None:
    probe = install(ctx, monkeypatch, Probe(0))
    ctx.settings.set("mcp.agent.max_attempts", 1)
    made = pipeline(ctx)
    replies = iter([failed(), proven()])
    monkeypatch.setattr(made, "_agent_stage", lambda *a, **k: next(replies))

    stage, reason = made._agent_attempts(Stage.ENDPOINT, TargetOutcome(target="https://example.test"))

    assert stage.verified and reason == ""
    assert made._runtime_unusable == ""
    assert probe.calls == [["/usr/bin/gcloud", "auth", "application-default", "print-access-token"]]


def test_the_refresh_is_tried_once_per_run(ctx, monkeypatch) -> None:
    probe = install(ctx, monkeypatch, Probe(0))
    made = pipeline(ctx)
    monkeypatch.setattr(made, "_agent_stage", lambda *a, **k: failed())

    made._agent_attempts(Stage.ENDPOINT, TargetOutcome(target="https://a.example.test"))

    assert len(probe.calls) == 1, "refreshed, refused again, latched -- not refreshed again"
    assert made._runtime_unusable


def test_a_refresh_that_fails_latches_with_the_remedy(ctx, monkeypatch) -> None:
    install(ctx, monkeypatch, Probe(1, "ERROR: Reauthentication failed"))
    made = pipeline(ctx)
    calls = []
    monkeypatch.setattr(made, "_agent_stage", lambda *a, **k: calls.append(1) or failed())

    stage, reason = made._agent_attempts(Stage.ENDPOINT, TargetOutcome(target="https://a.example.test"))

    assert len(calls) == 1, "no second attempt at a credential that cannot load"
    assert "Could not load Google Cloud credentials" in made._runtime_unusable
    assert made.runtime_remedy == "gcloud auth application-default login --no-launch-browser"
    assert stage.findings["agent_failure_kind"] == UNUSABLE
    assert "cannot serve any request" in reason


def test_the_refresh_can_be_turned_off(ctx, monkeypatch) -> None:
    probe = install(ctx, monkeypatch, Probe(0))
    ctx.settings.set("mcp.agent.credential_refresh", False)
    made = pipeline(ctx)
    monkeypatch.setattr(made, "_agent_stage", lambda *a, **k: failed())

    made._agent_attempts(Stage.ENDPOINT, TargetOutcome(target="https://a.example.test"))

    assert probe.calls == [] and made._runtime_unusable


def test_a_missing_tool_is_not_run(ctx, monkeypatch) -> None:
    probe = Probe(0)
    monkeypatch.setattr(ctx.runner, "probe", probe)
    monkeypatch.setattr(ctx, "tool", lambda name, **_k: None)
    profile = agent_credentials.profile_for(ctx.settings, GOOGLE)
    first, ok, why = agent_credentials.Refresher(ctx).refresh(profile)
    assert first and not ok and "not installed" in why and probe.calls == []


def test_a_retry_model_does_not_exempt_a_credential(ctx, monkeypatch) -> None:
    install(ctx, monkeypatch, Probe(1))
    ctx.settings.set("mcp.agent.model", "base")
    ctx.settings.set("mcp.agent.retry_models", "m1")
    made = pipeline(ctx)
    assert made._rotate_model() == "m1"
    monkeypatch.setattr(made, "_agent_stage", lambda *a, **k: failed())

    made._agent_attempts(Stage.ENDPOINT, TargetOutcome(target="https://a.example.test"))

    assert made._runtime_unusable, "the next model would load the same credential"


# -- which addresses lost the agent, and what to do about it --------------------------


def test_stages_after_the_latch_are_recorded_as_agent_unavailable(ctx, monkeypatch) -> None:
    made = pipeline(ctx)
    made._runtime_unusable = GOOGLE
    monkeypatch.setattr(made, "_agent_attempts", lambda *a, **k: pytest.fail("the agent was asked again"))
    monkeypatch.setattr(made, "_delegate", fallback_stage)
    target = TargetOutcome(target="b.example.test")

    stage = made._run_stage(Stage.PACKAGE, target, agentic=True, unavailable="")
    made._run_stage(Stage.BRIDGE, target, agentic=False, unavailable=made._unusable_reason())

    assert stage.findings["agent_unavailable"] == GOOGLE
    assert made.agent_unavailable == {"b.example.test": [Stage.PACKAGE, Stage.BRIDGE]}


def test_the_stage_that_learned_it_is_recorded_too(ctx, monkeypatch) -> None:
    install(ctx, monkeypatch, Probe(1))
    made = pipeline(ctx)
    monkeypatch.setattr(made, "_agent_stage", lambda *a, **k: failed())
    monkeypatch.setattr(made, "_delegate", fallback_stage)

    made._run_stage(Stage.ENDPOINT, TargetOutcome(target="a.example.test"), agentic=True, unavailable="")

    assert made.agent_unavailable == {"a.example.test": [Stage.ENDPOINT]}


def test_a_run_without_agents_by_choice_records_nothing(ctx, monkeypatch) -> None:
    made = pipeline(ctx)
    monkeypatch.setattr(made, "_delegate", fallback_stage)
    made._run_stage(Stage.ENDPOINT, TargetOutcome(target="a.example.test"), agentic=False, unavailable="--no-agent")
    assert made.agent_unavailable == {}


def test_the_remedy_no_longer_says_raise_max_attempts(ctx) -> None:
    agent = failed()
    agent.findings["agent_failure_kind"] = UNUSABLE
    merged = _merge(agent, fallback_stage(Stage.ENDPOINT, TargetOutcome(target="x")), "why")
    said = " ".join(merged.remediation)
    assert "max_attempts" not in said
    assert _FAILURE_REMEDY[UNUSABLE] in said


def verdict(ctx, made: AgenticPipeline):
    from initialize_claude_ai.core.report import PhaseResult
    from initialize_claude_ai.phases.phase4_mcp_auto_discovery import McpPhase

    phase = McpPhase(ctx)
    phase._ran_with = made
    result = PhaseResult(phase="mcp", title="MCP auto-discovery")
    phase._report_summary(result, [TargetOutcome(target="https://a.example.test")])
    return phase, " ".join(step.detail for step in result.steps)


def test_a_mid_run_failure_is_not_called_never_ran(ctx) -> None:
    made = pipeline(ctx)
    made._runtime_unusable = GOOGLE
    made.agent_answered = True
    made.agent_unavailable = {"b.example.test": [Stage.ENDPOINT]}
    _, said = verdict(ctx, made)
    assert "THE AGENT STOPPED PART-WAY" in said and "NEVER RAN" not in said


def test_the_verdict_prints_the_remedy_and_a_rerun_command(ctx) -> None:
    from initialize_claude_ai.cli import PROGRAM

    made = pipeline(ctx)
    made._runtime_unusable = GOOGLE
    made.runtime_remedy = "gcloud auth application-default login --no-launch-browser"
    made.agent_unavailable = {"a.example.test": [Stage.PACKAGE], "b.example.test": [Stage.ENDPOINT]}
    phase, _ = verdict(ctx, made)

    lines = phase._agent_unavailable_lines()

    assert any("gcloud auth application-default login" in line for line in lines)
    assert lines[-1].endswith(f"{PROGRAM} run --phases 4 --mcp-server a.example.test,b.example.test")


def test_a_healthy_run_prints_no_rerun_command(ctx) -> None:
    phase, _ = verdict(ctx, pipeline(ctx))
    assert phase._agent_unavailable_lines() == []
