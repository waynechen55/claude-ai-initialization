"""An address is never given up while anything could still take the withdrawn server's place.

gitlab.example.net, 2026-10-23, agent mode: the package agent proposed nothing, the
deterministic fallback stage trialled mcp-graphql *before* the address was known
to be a GitLab, and it verified on "describe the API" alone. The package tier
settled the address, so the bridge tier -- where the working gitlab-example-net
bridge was registered -- was skipped, and mcp-gitlab-python (waiting for
GITLAB_TOKEN) was discarded because something had verified. The final re-check
then knew the address was a GitLab, asked "read the current user" anonymously,
withdrew mcp-graphql as "no longer answers" -- and the address ended FAILED,
"no detail recorded", with nothing written.

Learned, for every server and both modes:

* the job is known before anything is judged (one shared stage start; the
  verifier asks for it itself when nothing recorded it);
* one standard per server per run: a verdict reached under a job that has since
  changed is reached again before the choice;
* the final re-check asks what proved a server, and says "gone" only when it is;
* a withdrawal hands over -- another proven server, one waiting for a credential
  (kept until the address is settled), then the tiers the withdrawn server had
  closed, existing registrations first -- and the file check repairs from
  registrations never tried;
* every address names its reason.
"""

from __future__ import annotations

import json
import sys

from initialize_claude_ai.core.report import PhaseResult, Status
from initialize_claude_ai.mcp_auto_discovery import verifier as verifier_module
from initialize_claude_ai.mcp_auto_discovery.traditional import TraditionalPipeline
from initialize_claude_ai.mcp_auto_discovery.types import Candidate, Handshake, Stage, StageOutcome, TargetOutcome
from initialize_claude_ai.mcp_auto_discovery.verifier import CandidateVerifier, Recheck
from initialize_claude_ai.mcp_auto_discovery.writer import McpWriter
from initialize_claude_ai.phases.phase4_mcp_auto_discovery import McpPhase

TARGET = "https://gitlab.example.test"


def graphql(name: str = "gitlab-example-test-mcp-graphql") -> Candidate:
    candidate = Candidate(
        kind="stdio", name=name, command=["npx", "-y", "mcp-graphql@2.0.4"], env={"ENDPOINT": f"{TARGET}/api/graphql"}
    )
    candidate.handshake = Handshake(
        ok=True,
        tools=["introspect-schema", "query-graphql"],
        round_trip=True,
        contract=[{"task": "describe the API", "passed": True}],
    )
    return candidate


def waiting(name: str = "mcp-gitlab-python") -> Candidate:
    candidate = Candidate(kind="stdio", name=name, command=["uvx", "mcp-gitlab"], env={"GITLAB_URL": TARGET})
    candidate.secret_env = {"GITLAB_TOKEN": "env:GITLAB_TOKEN"}
    candidate.handshake = Handshake(ok=True, tools=["get_current_user"], credential_pending=["GITLAB_TOKEN"])
    return candidate


# -- the job is known before anything is judged ------------------------------------------------------------


def test_every_stage_below_the_endpoint_learns_the_job_and_leaves_a_learner(ctx, monkeypatch) -> None:
    pipeline = TraditionalPipeline(ctx)
    learned: list[str] = []
    monkeypatch.setattr(pipeline, "know_job", lambda outcome: learned.append(outcome.target) or ["gitlab"])
    outcome = TargetOutcome(target=TARGET)
    pipeline.begin_stage(Stage.ENDPOINT, outcome)
    assert learned == [] and "gitlab.example.test" not in ctx.state.get("mcp_job_learners", {})
    pipeline.begin_stage(Stage.PACKAGE, outcome)
    assert learned == [TARGET]
    assert "gitlab.example.test" in ctx.state["mcp_job_learners"]


def test_agent_modes_fallback_stage_learns_the_job_first(ctx, monkeypatch) -> None:
    """The path that skipped it: the agent proposes nothing and the deterministic stage runs."""
    from initialize_claude_ai.mcp_auto_discovery.agentic import AgenticPipeline

    pipeline = AgenticPipeline(ctx)
    order: list[str] = []
    monkeypatch.setattr(pipeline.fallback, "begin_stage", lambda name, outcome: order.append(f"begin {name}"))
    monkeypatch.setattr(pipeline, "gate", lambda name, outcome: None)
    monkeypatch.setattr(pipeline.fallback, "settled", lambda *a, **k: None)
    monkeypatch.setattr(pipeline.fallback, "reuse", lambda *a, **k: None)
    monkeypatch.setattr(
        pipeline, "_run_stage", lambda name, outcome, **k: order.append(f"run {name}") or StageOutcome(stage=name)
    )
    monkeypatch.setattr(pipeline.fallback, "finish_stage", lambda *a, **k: order.append("finish"))
    pipeline._work(Stage.PACKAGE, TargetOutcome(target=TARGET), False, False, "no agent")
    assert order == [f"begin {Stage.PACKAGE}", f"run {Stage.PACKAGE}", "finish"]


def test_the_verifier_asks_the_learner_when_nothing_recorded_the_job(ctx) -> None:
    calls: list[str] = []

    def learner():
        calls.append("learn")
        ctx.state.setdefault("mcp_address_job", {})["gitlab.example.test"] = ["gitlab", "graphql"]

    ctx.state["mcp_job_learners"] = {"gitlab.example.test": learner}
    candidate = graphql()
    assert CandidateVerifier(ctx).address_job(candidate, "gitlab.example.test") == ["gitlab", "graphql"]
    assert calls == ["learn"]
    CandidateVerifier(ctx).address_job(candidate, "gitlab.example.test")
    assert calls == ["learn"], "recorded once, asked once"


def test_a_learner_that_asks_again_while_learning_is_not_entered_twice(ctx) -> None:
    depth: list[int] = []
    verifier = CandidateVerifier(ctx)

    def learner():
        depth.append(1)
        verifier.address_job(graphql(), "gitlab.example.test")

    ctx.state["mcp_job_learners"] = {"gitlab.example.test": learner}
    assert verifier.address_job(graphql(), "gitlab.example.test") is None
    assert depth == [1]


# -- one standard per server per run ----------------------------------------------------------------------


def test_a_verdict_reached_before_the_job_was_known_is_stale(ctx) -> None:
    verifier = CandidateVerifier(ctx)
    candidate = graphql()
    verifier.contract_plan(candidate)
    assert candidate.judged_job == ()
    assert verifier.stale_verdicts([candidate]) == []
    ctx.state.setdefault("mcp_address_job", {})["gitlab.example.test"] = ["gitlab", "graphql"]
    assert verifier.stale_verdicts([candidate]) == [candidate]


def test_a_stale_verdict_is_judged_again_before_the_choice(ctx, monkeypatch) -> None:
    phase = McpPhase(ctx)
    working, stale = graphql("a"), graphql("b")
    monkeypatch.setattr(CandidateVerifier, "stale_verdicts", lambda self, cs: [c for c in cs if c is stale])

    def verify(self, candidate):
        # As the real verifier leaves it: a failed usage task clears ``ok``.
        candidate.handshake = Handshake(
            ok=False,
            tools=["query-graphql"],
            round_trip=True,
            error="usage task 'read the current user' failed",
            contract=[{"task": "read the current user", "passed": False, "detail": "the answer lacks username"}],
        )
        return candidate

    monkeypatch.setattr(CandidateVerifier, "verify", verify)
    outcome = TargetOutcome(target=TARGET, verified=[working, stale])
    result = PhaseResult(phase="mcp")
    phase._rejudge_stale(result, outcome)
    assert outcome.verified == [working]
    assert any("judged again" in s.name and "read the current user" in s.detail for s in result.steps)


# -- the re-check asks what proved the server ------------------------------------------------------------


def test_the_recheck_asks_only_what_proved_the_server(ctx, monkeypatch) -> None:
    seen: list = []

    def verify(self, candidate):
        seen.append(list(candidate.recheck_tasks or []))
        return candidate

    monkeypatch.setattr(CandidateVerifier, "verify", verify)
    candidate = graphql()
    CandidateVerifier(ctx).recheck_all([candidate])
    assert seen == [["describe the API"]]
    assert candidate.recheck_tasks is None, "restored afterwards"


def test_the_recheck_restricts_the_contract_to_those_tasks(ctx) -> None:
    ctx.state.setdefault("mcp_address_job", {})["gitlab.example.test"] = ["gitlab", "graphql"]
    verifier = CandidateVerifier(ctx)
    candidate = graphql()
    everything = verifier.contract_plan(candidate)
    candidate.recheck_tasks = ["describe the API"]
    restricted = verifier.contract_plan(candidate)
    names = lambda plan: [t.name for t in (plan or {}).get("tasks", [])]  # noqa: E731
    assert "read the current user" in names(everything)
    assert names(restricted) == ["describe the API"]


def test_the_recheck_says_gone_only_when_it_is(ctx, monkeypatch) -> None:
    def verify(self, candidate):
        if candidate.name == "dead":
            candidate.handshake = Handshake(ok=False, error="exited with rc=1")
        elif candidate.name == "wants-login":
            candidate.handshake = Handshake(ok=True, tools=["t"], credential_pending=["GITLAB_TOKEN"])
        else:
            candidate.handshake = Handshake(
                ok=False,
                tools=["t"],
                round_trip=True,
                error="usage task failed",
                contract=[{"task": "describe the API", "passed": False, "detail": "x"}],
            )
        return candidate

    monkeypatch.setattr(CandidateVerifier, "verify", verify)
    found = CandidateVerifier(ctx).recheck_all([graphql("dead"), graphql("wants-login"), graphql("changed")])
    reasons = dict((c.name, why) for c, why in found.gone)
    assert reasons["dead"].startswith("it no longer answers")
    assert reasons["changed"].startswith("it answers, but no longer does what proved it")
    assert [c.name for c in found.waiting] == ["wants-login"]


# -- a withdrawal hands over -------------------------------------------------------------------------------


def finalise(ctx, outcome: TargetOutcome, phase: McpPhase | None = None) -> tuple[McpPhase, PhaseResult, McpWriter]:
    phase = phase or McpPhase(ctx)
    result = PhaseResult(phase="mcp", title="MCP auto-discovery")
    writer = McpWriter(ctx)
    phase._finalise(result, writer, outcome)
    return phase, result, writer


def test_a_server_waiting_for_a_credential_takes_the_withdrawn_ones_place(ctx, monkeypatch) -> None:
    """mcp-gitlab-python was discarded before the re-check; it is now kept until the address is settled."""
    monkeypatch.setattr(
        CandidateVerifier,
        "recheck_all",
        lambda self, cs: Recheck(gone=[(c, "it answers, but no longer does what proved it") for c in cs]),
    )
    outcome = TargetOutcome(target=TARGET, verified=[graphql()], provisional=[waiting()])
    _phase, result, writer = finalise(ctx, outcome)
    assert [r.candidate.name for r in writer.published] == ["mcp-gitlab-python"]
    assert writer.published[0].provisional is True
    recheck = next(s for s in result.steps if s.name.endswith(":: re-check"))
    assert "no longer answers" not in recheck.detail and "no longer does what proved it" in recheck.detail


def test_a_working_server_still_beats_one_that_would_need_a_token(ctx, monkeypatch) -> None:
    """Unchanged: once the address is settled with a working server, the waiting one is not written beside it."""
    monkeypatch.setattr(CandidateVerifier, "recheck_all", lambda self, cs: Recheck(working=list(cs)))
    outcome = TargetOutcome(target=TARGET, verified=[graphql()], provisional=[waiting()])
    _phase, _result, writer = finalise(ctx, outcome)
    assert [r.candidate.name for r in writer.published] == ["gitlab-example-test-mcp-graphql"]


class Reopener:
    """A pipeline whose skipped tier, reopened, finds the existing bridge."""

    def __init__(self, found: Candidate | None):
        self.found, self.calls = found, 0

    def reopen_withdrawn(self, outcome: TargetOutcome) -> list[str]:
        self.calls += 1
        stage = StageOutcome(
            stage=Stage.BRIDGE, status=Status.OK, detail="the existing gitlab-example-test still works"
        )
        if self.found is not None:
            stage.candidates.append(self.found)
            stage.verified.append(self.found)
            outcome.verified.append(self.found)
        outcome.stages.append(stage)
        return [Stage.BRIDGE]


def bridge() -> Candidate:
    candidate = Candidate(
        kind="stdio",
        name="gitlab-example-test",
        command=[sys.executable, "bridge.py"],
        env={"MCP_BRIDGE_BASE_URL": f"{TARGET}/api/graphql"},
    )
    candidate.handshake = Handshake(ok=True, tools=["get_current_user"], round_trip=True)
    return candidate


def test_with_nothing_left_the_tiers_it_had_closed_are_reopened(ctx, monkeypatch) -> None:
    """The gitlab.example.net case end to end through _finalise."""
    monkeypatch.setattr(
        CandidateVerifier,
        "recheck_all",
        lambda self, cs: Recheck(gone=[(c, "it answers, but no longer does what proved it") for c in cs]),
    )
    phase = McpPhase(ctx)
    phase._ran_with = Reopener(bridge())
    outcome = TargetOutcome(target=TARGET, verified=[graphql()])
    _phase, result, writer = finalise(ctx, outcome, phase)
    assert phase._ran_with.calls == 1
    assert [r.candidate.name for r in writer.published] == ["gitlab-example-test"]
    hand_over = next(s for s in result.steps if s.name.endswith(":: hand-over"))
    assert hand_over.status == Status.CHANGED and "reopened bridge" in hand_over.detail
    assert any(s.name.endswith(f":: {Stage.BRIDGE}") for s in result.steps), "the reopened stage is reported"


def test_a_hand_over_that_finds_nothing_says_so_and_never_loops(ctx, monkeypatch) -> None:
    monkeypatch.setattr(
        CandidateVerifier,
        "recheck_all",
        lambda self, cs: Recheck(gone=[(c, "it no longer answers: exited") for c in cs]),
    )
    phase = McpPhase(ctx)
    phase._ran_with = Reopener(None)
    outcome = TargetOutcome(target=TARGET, verified=[graphql()])
    finalise(ctx, outcome, phase)
    assert phase._ran_with.calls == 1, "owed once (mcp.handover.max_rounds)"
    assert outcome.detail, "an address always names its reason"


def test_the_hand_over_is_configurable(ctx, monkeypatch) -> None:
    ctx.settings.set("mcp.handover.enabled", False)
    monkeypatch.setattr(CandidateVerifier, "recheck_all", lambda self, cs: Recheck(gone=[(c, "gone") for c in cs]))
    phase = McpPhase(ctx)
    phase._ran_with = Reopener(bridge())
    finalise(ctx, TargetOutcome(target=TARGET, verified=[graphql()]), phase)
    assert phase._ran_with.calls == 0


# -- reopening, in the pipelines ----------------------------------------------------------------------------


def test_a_tier_skipped_as_settled_is_owed_its_turn_once_the_settler_is_withdrawn(ctx, monkeypatch) -> None:
    pipeline = TraditionalPipeline(ctx)
    monkeypatch.setattr(pipeline, "gate", lambda name, outcome: None)  # the tier policy allows it
    outcome = TargetOutcome(target=TARGET, verified=[graphql()])
    skipped = pipeline.settled(Stage.BRIDGE, outcome, exhaustive=False)
    assert skipped is not None and skipped.findings.get("settled_by")
    outcome.stages.append(skipped)
    assert pipeline.reopenable_after_withdrawal(outcome, exhaustive=False) == [], "still settled"
    outcome.verified = []
    assert pipeline.reopenable_after_withdrawal(outcome, exhaustive=False) == [Stage.BRIDGE]
    pipeline.withdraw_settled(outcome, Stage.BRIDGE)
    assert outcome.stage(Stage.BRIDGE) is None, "the settled skip is replaced by the stage that now runs"


def test_a_server_claude_code_could_not_use_settles_nothing(ctx) -> None:
    pipeline = TraditionalPipeline(ctx)
    candidate = graphql()
    outcome = TargetOutcome(target=TARGET, verified=[candidate])
    assert pipeline.settled(Stage.BRIDGE, outcome, exhaustive=False) is not None
    outcome.consumer_failed.append(candidate.identity())
    assert pipeline.settled(Stage.BRIDGE, outcome, exhaustive=False) is None


def test_both_pipelines_reopen_through_their_own_stage_loop(ctx) -> None:
    from initialize_claude_ai.mcp_auto_discovery.agentic import AgenticPipeline

    assert callable(getattr(TraditionalPipeline(ctx), "reopen_withdrawn", None))
    assert callable(getattr(AgenticPipeline(ctx), "reopen_withdrawn", None))


# -- the file check repairs from registrations never tried --------------------------------------------------


def test_the_file_check_tries_an_existing_registration_this_run_never_tried(ctx, sandbox, monkeypatch) -> None:
    ctx.dry_run = False
    ctx.settings.set("mcp.mcp_json_path", str(sandbox / ".mcp.json"))
    ctx.settings.set("mcp.discovery.reuse.sources", ["project_file"])
    entry = {
        "type": "stdio",
        "command": sys.executable,
        "args": ["bridge.py"],
        "env": {"MCP_BRIDGE_BASE_URL": f"{TARGET}/api/graphql"},
    }
    (sandbox / ".mcp.json").write_text(json.dumps({"mcpServers": {"gitlab-example-test": entry}}))
    writer = McpWriter(ctx)
    (sandbox / ".mcp.json").write_text(json.dumps({"mcpServers": {}}))  # gone from the file, still registered elsewhere
    from initialize_claude_ai.mcp_auto_discovery.existing import ExistingRegistrations

    monkeypatch.setattr(
        ExistingRegistrations, "for_target", lambda self, target, stage: [bridge()] if stage == Stage.BRIDGE else []
    )
    monkeypatch.setattr(ExistingRegistrations, "registration_for", lambda self, c: None)

    def verify(self, candidate):
        candidate.handshake = Handshake(ok=True, tools=["t"], round_trip=True)
        return candidate

    monkeypatch.setattr(CandidateVerifier, "verify", verify)
    outcome = TargetOutcome(target=TARGET)
    outcome.refused.append(graphql())
    writer.missing_from_file(outcome)
    assert "gitlab-example-test" in outcome.answered_existing
    assert outcome.stage(Stage.BRIDGE) is not None


# -- every address names its reason --------------------------------------------------------------------------


def test_an_address_always_names_its_reason(ctx) -> None:
    phase = McpPhase(ctx)
    result = PhaseResult(phase="mcp")
    outcome = TargetOutcome(target=TARGET)
    phase.report_step(result, f"{TARGET} :: re-check", Status.FAILED, "it no longer answers: exited", group=TARGET)
    assert "re-check: it no longer answers" in phase._ensure_detail(result, None, outcome)
    empty = TargetOutcome(target="https://other.example.test")
    assert phase._ensure_detail(PhaseResult(phase="mcp"), None, empty).startswith("nothing registered")


def test_the_settings_are_packaged(settings) -> None:
    assert settings.bool_("mcp.handover.enabled") is True
    assert settings.int_("mcp.handover.max_rounds") == 1
    assert settings.bool_("mcp.handover.reopen_settled_tiers") is True
    assert settings.bool_("mcp.handover.after_publish") is True
    assert settings.bool_("mcp.verification.learn_job_when_judging") is True
    assert settings.bool_("mcp.verification.recheck_proved_tasks") is True
    assert settings.bool_("mcp.verification.rejudge_stale_verdicts") is True
    assert settings.bool_("mcp.registration.repair_from_untried_existing") is True
    assert verifier_module.Recheck().working == []


# -- found by the real run: the "no credential" comparison and proof across tiers -----------------------------

SERVER_REPORTS_TOKEN = """
import json, os, sys
for line in sys.stdin:
    msg = json.loads(line)
    rid = msg.get("id")
    if rid is None:
        continue
    method = msg.get("method")
    if method == "initialize":
        result = {"protocolVersion": msg["params"]["protocolVersion"], "capabilities": {"tools": {}},
                  "serverInfo": {"name": "x", "version": "1"}}
    elif method == "tools/list":
        result = {"tools": [{"name": "get_current_user", "inputSchema": {"type": "object", "properties": {}}}]}
    elif method == "tools/call":
        who = "alice" if os.environ.get("EXAMPLE_GITLAB_TOKEN") else None
        result = {"content": [{"type": "text", "text": json.dumps({"currentUser": who})}]}
    else:
        result = {}
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": rid, "result": result}) + "\\n")
    sys.stdout.flush()
"""


def test_the_no_credential_comparison_withholds_what_it_dropped(ctx) -> None:
    verifier = CandidateVerifier(ctx)
    launch = {
        "command": ["x"],
        "env": {"EXAMPLE_GITLAB_TOKEN": "t", "MODE": "m"},
        "secret_env": {"EXAMPLE_API_KEY": "k"},
        "cwd": None,
    }
    anonymous = verifier._without_credentials(launch)
    assert "EXAMPLE_GITLAB_TOKEN" not in anonymous["env"] and anonymous["env"]["MODE"] == "m"
    assert set(anonymous["withhold"]) >= {"EXAMPLE_GITLAB_TOKEN", "EXAMPLE_API_KEY"}
    standin = verifier._without_credentials(launch, standin="stand-in")
    assert standin["env"]["EXAMPLE_GITLAB_TOKEN"] == "stand-in", "a stand-in is supplied, so never withheld"


def test_an_inherited_token_never_reaches_the_no_credential_comparison(ctx, tmp_path, monkeypatch) -> None:
    """gitlab-example-net's comparison inherited the token a Claude Code session exports, and its login 'proved nothing'."""
    from initialize_claude_ai.mcp_auto_discovery.probes import McpProber

    ctx.dry_run = False
    script = tmp_path / "server.py"
    script.write_text(SERVER_REPORTS_TOKEN)
    monkeypatch.setenv("EXAMPLE_GITLAB_TOKEN", "exported-by-the-session")
    launch = {
        "command": [sys.executable, str(script)],
        "env": {"EXAMPLE_GITLAB_TOKEN": "exported-by-the-session"},
        "secret_env": {},
        "cwd": None,
    }
    anonymous = CandidateVerifier(ctx)._without_credentials(launch)
    from initialize_claude_ai.mcp_auto_discovery import login_check

    shake = McpProber(ctx).probe_stdio(**anonymous, timeout=20, login=True)
    assert shake.ok, shake.error
    # Answer content is never recorded; the verdict on it is: a user named is an
    # accepted login, a null user an empty answer.
    assert shake.login != login_check.ACCEPTED, "launched without the credential, it answers as anybody does"


def proven(name: str) -> Candidate:
    candidate = Candidate(kind="stdio", name=name, command=[sys.executable, f"{name}.py"])
    candidate.handshake = Handshake(ok=True, tools=["t"], round_trip=True, login="accepted")
    return candidate


def unproved(name: str) -> Candidate:
    candidate = Candidate(kind="stdio", name=name, command=["npx", "-y", name])
    candidate.handshake = Handshake(ok=True, tools=["t"], round_trip=True, login="unchecked")
    return candidate


def test_a_proven_login_below_replaces_what_never_proved_access_above(ctx) -> None:
    outcome = TargetOutcome(target=TARGET, verified=[unproved("efficient-gitlab")])
    stage = StageOutcome(stage=Stage.BRIDGE, verified=[proven("gitlab-example-test")])
    outcome.absorb(stage, proven_replaces_unproved=True)
    assert [c.name for c in outcome.verified] == ["gitlab-example-test"]


def test_without_the_rule_or_under_exhaustive_both_are_kept(ctx) -> None:
    outcome = TargetOutcome(target=TARGET, verified=[unproved("efficient-gitlab")])
    outcome.absorb(StageOutcome(stage=Stage.BRIDGE, verified=[proven("gitlab-example-test")]))
    assert len(outcome.verified) == 2, "direct callers keep the old behaviour by default"
    ctx.settings.set("mcp.exhaustive", True)
    pipeline = TraditionalPipeline(ctx)
    outcome = TargetOutcome(target=TARGET, verified=[unproved("efficient-gitlab")])
    pipeline.absorb(
        outcome, StageOutcome(stage=Stage.BRIDGE, verified=[proven("gitlab-example-test")]), exhaustive=True
    )
    assert len(outcome.verified) == 2, "--exhaustive-mcp keeps everything"


def test_the_pipeline_applies_the_rule_by_default(ctx) -> None:
    pipeline = TraditionalPipeline(ctx)
    outcome = TargetOutcome(target=TARGET, verified=[unproved("efficient-gitlab")])
    pipeline.absorb(
        outcome, StageOutcome(stage=Stage.BRIDGE, verified=[proven("gitlab-example-test")]), exhaustive=False
    )
    assert [c.name for c in outcome.verified] == ["gitlab-example-test"]
    assert ctx.settings.bool_("mcp.discovery.proven_replaces_unproved") is True
