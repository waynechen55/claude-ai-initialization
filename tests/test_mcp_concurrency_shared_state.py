"""What addresses worked at the same time have in common, and nothing more.

``--concurrency`` runs several addresses through *one* pipeline object on
separate threads. A run of thirteen addresses at four at a time registered a
handful of servers, where the same thirteen one after another registered all of
them, and the log named four ways shared state had leaked between addresses:

* every address's first stage prepared the agent workspace at the same moment,
  and each deleted and re-copied the skills directory underneath the others --
  ``FileExistsError`` and vanished files, seven endpoint stages lost;
* a stage that raised was recorded FAILED without the deterministic stage being
  tried, so the endpoint record stayed empty and the tier policy refused every
  tier beneath it with "the endpoint stage has not run";
* a refusal on one address rotated the *shared* model and runtime, so the other
  addresses' requests went out on a model nobody chose for them, and the
  rotation wrapped round to the model that had just refused;
* smaller races: one ``artifacts`` directory for everybody, a lazily built
  helper, a negative-control probe another thread could read half-finished.

None of the fixes names a server. Each guard is keyed by what is shared -- a
path, a command line, an origin, a thread -- so an address nobody has heard of
is covered exactly like the ones that exposed the problem. And one address at a
time is one thread: every guard is taken uncontended and changes nothing.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from initialize_claude_ai import cli
from initialize_claude_ai.core.report import Status
from initialize_claude_ai.mcp_auto_discovery import agentic as agentic_mod
from initialize_claude_ai.mcp_auto_discovery.agentic import AgenticPipeline, _replace_tree
from initialize_claude_ai.mcp_auto_discovery.locks import INSTALL_STEPS, KeyedLocks
from initialize_claude_ai.mcp_auto_discovery.probes import McpProber
from initialize_claude_ai.mcp_auto_discovery.runtime import CliRuntime, SdkRuntime
from initialize_claude_ai.mcp_auto_discovery.types import (
    Candidate,
    Mode,
    Stage,
    StageOutcome,
    TargetOutcome,
)
from initialize_claude_ai.phases.phase4_mcp_auto_discovery import BARE_CONCURRENCY, McpPhase

THREADS = 8


def in_threads(count: int, work) -> list:
    """Run ``work(index)`` on ``count`` threads released at the same instant."""
    barrier = threading.Barrier(count)
    results: list = [None] * count
    errors: list[BaseException] = []

    def run(index: int) -> None:
        barrier.wait()
        try:
            results[index] = work(index)
        except BaseException as exc:  # noqa: BLE001 - reported below
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(i,)) for i in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert not errors, errors
    return results


def on_thread(work):
    """Run ``work()`` on a fresh thread and return what it returned."""
    box: list = []
    thread = threading.Thread(target=lambda: box.append(work()))
    thread.start()
    thread.join(timeout=30)
    return box[0]


def pipeline_with_runtimes(ctx) -> AgenticPipeline:
    pipeline = AgenticPipeline(ctx)
    pipeline._runtimes = [SdkRuntime(ctx), CliRuntime(ctx)]
    pipeline._runtimes_tried = {"sdk"}
    pipeline._begin_target()
    return pipeline


def target(name: str = "https://example.test") -> TargetOutcome:
    return TargetOutcome(target=name)


def empty(detail: str = "found nothing", **kwargs) -> StageOutcome:
    return StageOutcome(stage=Stage.ENDPOINT, mode=Mode.AGENTIC, detail=detail, **kwargs)


def proven() -> StageOutcome:
    return StageOutcome(
        stage=Stage.ENDPOINT,
        mode=Mode.AGENTIC,
        status=Status.OK,
        verified=[Candidate(kind="url", url="https://example.test/mcp")],
    )


# --------------------------------------------------------------------------- #
# the workspace
# --------------------------------------------------------------------------- #


@pytest.fixture
def live_workspace(ctx, tmp_path: Path):
    """A context that really writes, with a small skills tree to copy."""
    skills = tmp_path / "skills"
    for name in ("alpha", "beta"):
        (skills / name).mkdir(parents=True)
        (skills / name / "SKILL.md").write_text(f"# {name}\n", encoding="utf-8")
    ctx.dry_run = False
    ctx.settings.set("mcp.agent.workspace", str(tmp_path / "workspace"))
    ctx.settings.set("mcp.agent.skills_dir", str(skills))
    return ctx, tmp_path / "workspace"


def test_every_address_asking_for_the_workspace_at_once_gets_one_copy(live_workspace, monkeypatch) -> None:
    ctx, workspace = live_workspace
    pipeline = AgenticPipeline(ctx)
    copies: list[int] = []
    real = agentic_mod._replace_tree

    def counted(source: Path, dest: Path) -> None:
        copies.append(1)
        time.sleep(0.05)  # widen the window an unguarded check would lose in
        real(source, dest)

    monkeypatch.setattr(agentic_mod, "_replace_tree", counted)

    results = in_threads(THREADS, lambda _: pipeline.prepare_workspace())

    assert len(copies) == 1
    assert set(results) == {workspace}
    skills = workspace / ".claude" / "skills"
    assert sorted(p.parent.name for p in skills.glob("*/SKILL.md")) == ["alpha", "beta"]


def test_the_skills_tree_is_replaced_whole_and_leaves_nothing_behind(tmp_path: Path) -> None:
    source = tmp_path / "source"
    (source / "new").mkdir(parents=True)
    (source / "new" / "SKILL.md").write_text("new", encoding="utf-8")
    dest = tmp_path / "ws" / "skills"
    (dest / "stale").mkdir(parents=True)
    (dest / "stale" / "SKILL.md").write_text("stale", encoding="utf-8")

    _replace_tree(source, dest)

    assert [p.name for p in dest.iterdir()] == ["new"]
    assert [p.name for p in dest.parent.iterdir()] == ["skills"]


def test_each_address_gets_its_own_artefacts_directory(live_workspace) -> None:
    ctx, _ = live_workspace
    pipeline = AgenticPipeline(ctx)
    workspace = pipeline.prepare_workspace()

    one = pipeline._artifacts_dir(workspace, target("https://one.example.test"))
    two = pipeline._artifacts_dir(workspace, target("https://two.example.test"))

    assert one != two
    assert one.is_dir() and two.is_dir()
    assert one.parent == two.parent == workspace / "artifacts"


# --------------------------------------------------------------------------- #
# rotation is per address
# --------------------------------------------------------------------------- #


def test_one_addresses_model_rotation_does_not_move_another_mid_request(ctx) -> None:
    ctx.settings.set("mcp.agent.model", "base")
    ctx.settings.set("mcp.agent.retry_models", "m1,m2")
    pipeline = pipeline_with_runtimes(ctx)
    b_started, a_rotated = threading.Event(), threading.Event()
    seen: dict[str, str] = {}

    def address_a() -> None:
        pipeline._begin_target()
        b_started.wait(5)
        seen["a"] = pipeline._rotate_model()
        a_rotated.set()

    def address_b() -> None:
        pipeline._begin_target()
        b_started.set()
        a_rotated.wait(5)
        seen["b"] = pipeline.runtime.model()

    threads = [threading.Thread(target=address_a), threading.Thread(target=address_b)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert seen == {"a": "m1", "b": "base"}


def test_an_address_never_rotates_back_to_the_model_that_refused_it(ctx) -> None:
    ctx.settings.set("mcp.agent.model", "base")
    ctx.settings.set("mcp.agent.retry_models", "m1,base,m2")
    pipeline = pipeline_with_runtimes(ctx)

    assert pipeline._rotate_model() == "m1"
    assert pipeline._rotate_model() == "m2"
    assert pipeline._rotate_model() == ""


def test_a_new_address_starts_where_the_last_rotation_landed(ctx) -> None:
    """What one address learned the hard way is where the next one begins --
    the behaviour one address at a time always had."""
    ctx.settings.set("mcp.agent.model", "base")
    ctx.settings.set("mcp.agent.retry_models", "m1")
    pipeline = pipeline_with_runtimes(ctx)
    assert pipeline._rotate_model() == "m1"

    def next_address() -> str:
        pipeline._begin_target()
        return pipeline.runtime.model()

    assert on_thread(next_address) == "m1"


def test_an_address_still_on_a_failed_runtime_follows_the_others_off_it(ctx) -> None:
    pipeline = pipeline_with_runtimes(ctx)
    ready, rotated = threading.Event(), threading.Event()
    seen: dict[str, str] = {}

    def first() -> None:
        pipeline._begin_target()
        ready.wait(5)
        seen["first"] = pipeline._rotate_runtime()
        rotated.set()

    def second() -> None:
        pipeline._begin_target()
        seen["second_before"] = pipeline.runtime.name
        ready.set()
        rotated.wait(5)
        seen["second"] = pipeline._rotate_runtime()

    threads = [threading.Thread(target=first), threading.Thread(target=second)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert seen == {"first": "cli", "second_before": "sdk", "second": "cli"}


def test_a_new_address_reuses_a_pool_thread_without_its_history(ctx) -> None:
    ctx.settings.set("mcp.agent.model", "base")
    ctx.settings.set("mcp.agent.retry_models", "m1")
    pipeline = pipeline_with_runtimes(ctx)
    pipeline._rotate_model()
    assert "base" in pipeline._models_tried

    pipeline._begin_target()

    assert pipeline._models_tried == set()


# --------------------------------------------------------------------------- #
# an unusable retry model is not an unusable runtime
# --------------------------------------------------------------------------- #


def unavailable(model: str) -> StageOutcome:
    return empty(f"The model {model} is not available on your vertex deployment", error="agent-failed")


def test_an_unserved_retry_model_is_left_not_latched(ctx, monkeypatch) -> None:
    ctx.settings.set("mcp.agent.model", "base")
    ctx.settings.set("mcp.agent.retry_models", "m1")
    ctx.settings.set("mcp.agent.max_attempts", 3)
    pipeline = pipeline_with_runtimes(ctx)
    assert pipeline._rotate_model() == "m1"
    replies = iter([unavailable("m1"), proven()])
    monkeypatch.setattr(pipeline, "_agent_stage", lambda *a, **k: next(replies))

    stage, reason = pipeline._agent_attempts(Stage.ENDPOINT, target())

    assert pipeline._runtime_unusable == ""
    assert pipeline._models_unusable == {"m1"}
    assert pipeline.runtime.model() == "base"
    assert stage.verified and reason == ""
    # ...and no address rotates onto it again.
    pipeline._begin_target()
    assert pipeline._rotate_model() == ""


def test_the_configured_model_being_unserved_still_latches(ctx, monkeypatch) -> None:
    ctx.settings.set("mcp.agent.model", "base")
    pipeline = pipeline_with_runtimes(ctx)
    monkeypatch.setattr(pipeline, "_agent_stage", lambda *a, **k: unavailable("base"))

    pipeline._agent_attempts(Stage.ENDPOINT, target())

    assert pipeline._runtime_unusable


def test_a_latch_set_by_another_address_is_honoured_at_the_next_stage(ctx, monkeypatch) -> None:
    pipeline = pipeline_with_runtimes(ctx)
    pipeline._runtime_unusable = "the model is not enabled"
    monkeypatch.setattr(pipeline, "_agent_attempts", lambda *a, **k: pytest.fail("the agent was asked again"))
    monkeypatch.setattr(pipeline, "_delegate", fallback_stage)

    stage = pipeline._run_stage(Stage.ENDPOINT, target(), agentic=True, unavailable="")

    assert "cannot serve any request" in stage.findings["agentic_skipped"]


# --------------------------------------------------------------------------- #
# a stage that raised is not a stage that was worked
# --------------------------------------------------------------------------- #


def fallback_stage(name: str, tgt: TargetOutcome) -> StageOutcome:
    return StageOutcome(stage=name, mode=Mode.TRADITIONAL, status=Status.OK, detail="worked")


def test_a_crashed_agent_stage_is_done_deterministically(ctx, monkeypatch) -> None:
    pipeline = AgenticPipeline(ctx)
    monkeypatch.setattr(pipeline, "_delegate", fallback_stage)

    stage = pipeline._after_crash(Stage.ENDPOINT, target(), FileExistsError("skills"), agentic=True)

    assert stage.status != Status.FAILED
    assert "FileExistsError" in stage.findings["agent_crashed"]


def test_the_crash_is_recorded_through_the_stage_loop(ctx, monkeypatch) -> None:
    pipeline = AgenticPipeline(ctx)
    monkeypatch.setattr(pipeline, "_run_stage", lambda *a, **k: (_ for _ in ()).throw(OSError("x")))
    monkeypatch.setattr(pipeline, "_delegate", fallback_stage)
    outcome = target()

    pipeline._work(Stage.ENDPOINT, outcome, False, True, "")

    [stage] = outcome.stages
    assert stage.status != Status.FAILED
    assert "agent_crashed" in stage.findings


@pytest.mark.parametrize("agentic", [False, True])
def test_a_crash_the_fallback_cannot_cover_stays_failed(ctx, monkeypatch, agentic) -> None:
    """With no agent the crash *was* the deterministic stage; with one, the
    deterministic stage raising too leaves nothing to report but the failure."""
    pipeline = AgenticPipeline(ctx)

    def boom(name, tgt):
        raise RuntimeError("fallback broke")

    monkeypatch.setattr(pipeline, "_delegate", boom)

    stage = pipeline._after_crash(Stage.ENDPOINT, target(), RuntimeError("x"), agentic=agentic)

    assert stage.status == Status.FAILED


# --------------------------------------------------------------------------- #
# probes and installs
# --------------------------------------------------------------------------- #


def test_one_origin_is_control_probed_once_however_many_ask(ctx, monkeypatch) -> None:
    prober = McpProber(ctx)
    calls: list[str] = []

    def slow_probe(origin: str):
        calls.append(origin)
        time.sleep(0.1)
        return (404, "text/html")

    monkeypatch.setattr(prober, "_control_probe", slow_probe)

    answers = in_threads(THREADS, lambda i: prober.control_answer(f"https://example.test/path-{i}"))

    assert calls == ["https://example.test"]
    assert answers == [(404, "text/html")] * THREADS


def test_a_failed_control_probe_is_not_repeated_or_left_pending(ctx, monkeypatch) -> None:
    prober = McpProber(ctx)
    calls: list[str] = []

    def broken(origin: str):
        calls.append(origin)
        raise RuntimeError("socket closed")

    monkeypatch.setattr(prober, "_control_probe", broken)

    with pytest.raises(RuntimeError):
        prober.control_answer("https://example.test/mcp")
    assert prober.control_answer("https://example.test/mcp") is None
    assert calls == ["https://example.test"]


def test_install_locks_are_shared_per_command_and_separate_across_commands() -> None:
    locks = KeyedLocks()

    assert locks(("pip", "install", "a")) is locks(("pip", "install", "a"))
    assert locks(("pip", "install", "a")) is not locks(("pip", "install", "b"))
    with INSTALL_STEPS(("x",)), INSTALL_STEPS(("x",)):
        pass  # re-entrant: a repair re-running its own install cannot deadlock


# --------------------------------------------------------------------------- #
# the flag
# --------------------------------------------------------------------------- #


def overrides(*argv: str) -> list[str]:
    args = cli.build_parser().parse_args(cli._normalise_argv(["run", *argv]))
    return [o for o in cli._overrides_from_args(args) if "target_concurrency" in o]


def test_bare_concurrency_defers_its_number_to_configuration() -> None:
    assert overrides("--concurrency") == [f"mcp.discovery.target_concurrency={BARE_CONCURRENCY}"]


def test_an_explicit_concurrency_is_passed_on() -> None:
    assert overrides("--concurrency", "3") == ["mcp.discovery.target_concurrency=3"]


def test_no_flag_means_no_override() -> None:
    assert overrides() == []


@pytest.mark.parametrize("value", ["0", "-2", "many"])
def test_a_concurrency_that_would_work_nothing_is_refused(value) -> None:
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(cli._normalise_argv(["run", f"--concurrency={value}"]))


def test_the_bare_keyword_resolves_to_the_configured_count(ctx) -> None:
    ctx.settings.set("mcp.discovery.target_concurrency", BARE_CONCURRENCY)
    ctx.settings.set("mcp.discovery.target_concurrency_bare", 6)

    assert McpPhase(ctx).concurrency() == 6


def test_an_absent_flag_still_means_one_at_a_time(ctx) -> None:
    assert McpPhase(ctx).concurrency() == 1


# --------------------------------------------------------------------------- #
# the whole pipeline, one at a time and all at once
# --------------------------------------------------------------------------- #


def walk_real_pipeline(ctx, monkeypatch, concurrency: int) -> dict[str, list[tuple]]:
    """Work six addresses through a real AgenticPipeline, the agent faked.

    Every second address's agent raises -- the crash the shared workspace used
    to cause -- and the others are refused once and then answer, which rotates
    models the way the refusals in the failing run did.
    """
    from initialize_claude_ai.mcp_auto_discovery.address import ServerAddress

    ctx.settings.set("mcp.discovery.target_concurrency", concurrency)
    ctx.settings.set("mcp.agent.model", "base")
    ctx.settings.set("mcp.agent.retry_models", "m1,m2")
    pipeline = AgenticPipeline(ctx)
    pipeline._runtimes = [SdkRuntime(ctx)]
    monkeypatch.setattr(pipeline, "available", lambda: (True, ""))
    monkeypatch.setattr(pipeline, "_delegate", fallback_stage)
    models: dict[str, list[str]] = {}
    lock = threading.Lock()

    def agent(name, tgt, **kwargs):
        with lock:
            seen = models.setdefault(tgt.target, [])
            seen.append(pipeline.runtime.model())
        time.sleep(0.01)
        if tgt.target.endswith(("1.example.test", "3.example.test", "5.example.test")):
            raise FileExistsError("skills")
        if len(seen) == 1:
            return empty("[cyber] this request was refused by the safeguards", error="agent-failed")
        return StageOutcome(stage=name, mode=Mode.AGENTIC, status=Status.OK, detail="answered")

    monkeypatch.setattr(pipeline, "_agent_stage", agent)
    addresses = [ServerAddress(raw=f"https://s{i}.example.test", url=f"https://s{i}.example.test") for i in range(6)]
    walked = McpPhase(ctx)._work_through(addresses, pipeline, [Stage.ENDPOINT])
    outcomes = {address.url: [(s.stage, s.status, s.mode) for s in outcome.stages] for address, outcome in walked}
    return outcomes, models


def test_concurrency_changes_the_wait_not_the_outcome(ctx, monkeypatch) -> None:
    alone, alone_models = walk_real_pipeline(ctx, monkeypatch, 1)
    together, together_models = walk_real_pipeline(ctx, monkeypatch, 4)

    assert together == alone
    assert all(status != Status.FAILED for stages in together.values() for _, status, _ in stages)
    # A refused address moves to a model it has not been refused on, and never
    # back to the one that refused it -- whoever else is being refused beside it.
    for trail in together_models.values():
        if len(trail) > 1:
            assert trail[1] != trail[0]
