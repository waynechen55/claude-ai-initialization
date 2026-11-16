"""Working several addresses at once, without loosening anything.

Phase 4 is about 98% agent thinking, and addresses are independent, so
overlapping them is the only change that shortens a long run. It reduces
*waiting*, not work.

What must not change is everything else. The tier order, the four-step
checklist, the agent owning its stage until it settles it, the deterministic
pipeline as a last resort rather than a first excuse -- none of those is allowed
to weaken because four targets are in flight. Two of them could have, and both
are pinned here:

* the spend cap must not cut one target's agent loop short because *another*
  target spent the money, so it is applied when a target is admitted;
* a rate limit provoked by our own parallelism must not consume one of the
  agent's attempts, or the flag would buy a fallback the agent never earned.

And the default must be untouched. At a concurrency of 1 no thread is started:
the sequential path is not a second implementation that can drift, it is this
one with the strategy set to "inline".
"""

from __future__ import annotations

import io
import threading
import time

from initialize_claude_ai.core.logging_setup import Console
from initialize_claude_ai.core.report import PhaseResult, Status
from initialize_claude_ai.mcp_auto_discovery.address import ServerAddress
from initialize_claude_ai.mcp_auto_discovery.agentic import AgenticPipeline
from initialize_claude_ai.mcp_auto_discovery.types import (
    Candidate,
    Handshake,
    Mode,
    Stage,
    StageOutcome,
    TargetOutcome,
    Transport,
)
from initialize_claude_ai.phases.phase4_mcp_auto_discovery import McpPhase

TARGETS = ["https://a.example.test", "https://b.example.test", "https://c.example.test"]


class Recorder:
    """A pipeline stand-in that records which thread ran each target."""

    def __init__(self, delay: float = 0.0) -> None:
        self.delay, self.threads, self.started = delay, {}, []
        self._lock = threading.Lock()

    def run(self, target: str, stages) -> TargetOutcome:
        with self._lock:
            self.started.append(target)
        self.threads[target] = threading.current_thread().name
        if self.delay:
            time.sleep(self.delay)
        outcome = TargetOutcome(target=target, base_url=target)
        outcome.stages.append(
            StageOutcome(stage=Stage.ENDPOINT, mode=Mode.AGENTIC, status=Status.OK, detail=f"{target} settled")
        )
        outcome.verified.append(
            Candidate(
                kind="url",
                url=f"{target}/mcp",
                transport=Transport.STREAMABLE_HTTP,
                handshake=Handshake(ok=True, tools=["a"], round_trip=True),
            )
        )
        return outcome


def addresses() -> list[ServerAddress]:
    return [ServerAddress(raw=t, url=t) for t in TARGETS]


def walk(ctx, concurrency: int, delay: float = 0.0):
    ctx.settings.set("mcp.discovery.target_concurrency", str(concurrency))
    phase, pipeline = McpPhase(ctx), Recorder(delay)
    got = list(phase._work_through(addresses(), pipeline, [Stage.ENDPOINT]))
    return got, pipeline


# -- the default is untouched ---------------------------------------------------


def test_one_at_a_time_starts_no_thread(ctx) -> None:
    """The sequential path must execute no concurrency machinery whatever."""
    before = threading.active_count()
    got, pipeline = walk(ctx, concurrency=1)

    assert [a.url for a, _ in got] == TARGETS
    assert threading.active_count() == before, "a thread was started for the default path"
    assert set(pipeline.threads.values()) == {threading.current_thread().name}


def test_one_at_a_time_is_the_default(ctx) -> None:
    assert McpPhase(ctx).concurrency() == 1


def test_the_flag_raises_it(ctx) -> None:
    ctx.settings.set("mcp.discovery.target_concurrency", "4")
    assert McpPhase(ctx).concurrency() == 4


# -- what concurrency changes, and what it must not -----------------------------


def test_addresses_really_do_overlap(ctx) -> None:
    got, pipeline = walk(ctx, concurrency=3, delay=0.2)
    assert len(got) == 3
    assert len(set(pipeline.threads.values())) > 1, "expected more than one worker"


def test_results_arrive_in_the_order_the_addresses_were_given(ctx) -> None:
    """The report and the registration order are a contract, not a race.

    Yielding in completion order would shuffle the report between runs and
    interleave one server's lines with another's.
    """
    got, _ = walk(ctx, concurrency=3, delay=0.05)
    assert [a.url for a, _ in got] == TARGETS
    assert [o.target for _, o in got] == TARGETS


def test_the_same_outcomes_either_way(ctx) -> None:
    """One path, two strategies: the results cannot differ."""
    sequential, _ = walk(ctx, concurrency=1)
    concurrent, _ = walk(ctx, concurrency=3)
    assert [(a.url, o.target, o.status, len(o.verified)) for a, o in sequential] == [
        (a.url, o.target, o.status, len(o.verified)) for a, o in concurrent
    ]


# -- the principles, under concurrency ------------------------------------------


def test_the_budget_is_applied_at_admission_not_inside_a_stage(ctx) -> None:
    """Otherwise another target's spend truncates this one's agent loop.

    "The agent owns its stage until it settles it" would become "until somebody
    else spent the money", which is the fallback becoming a first excuse.
    """
    pipeline = AgenticPipeline(ctx)
    assert pipeline.budget_checked_at_admission is False, "unchanged when sequential"

    ctx.settings.set("mcp.discovery.target_concurrency", "3")
    list(McpPhase(ctx)._work_through(addresses(), pipeline, [Stage.ENDPOINT]))
    assert pipeline.budget_checked_at_admission is True


def test_a_transient_failure_does_not_consume_an_attempt(ctx) -> None:
    """N sessions at once provokes rate limiting; the agent must not pay for it."""
    pipeline = AgenticPipeline(ctx)
    pipeline.available = lambda: (True, "")  # type: ignore[assignment]
    seen: list[int] = []

    def flaky(name, target, *, attempt, attempts, plan):
        seen.append(attempt)
        stage = StageOutcome(stage=name, mode=Mode.AGENTIC, status=Status.PARTIAL)
        if len(seen) <= 2:
            stage.error = "agent-failed"
            stage.detail = "API Error: 529 overloaded_error, service temporarily unavailable"
        else:
            stage.detail = "the agent found no candidate MCP server"
        return stage

    pipeline._agent_stage = flaky  # type: ignore[assignment]
    _stage, _reason = pipeline._agent_attempts(Stage.ENDPOINT, TargetOutcome(target="x"))

    # Two transient failures, then three real attempts: five calls, and the
    # attempt counter never went past its limit.
    assert seen == [1, 1, 1, 2, 3], seen


def test_the_free_retries_are_finite(ctx) -> None:
    """A service that is down forever must not loop forever."""
    ctx.settings.set("mcp.agent.transient_free_retries", "2")
    pipeline = AgenticPipeline(ctx)
    pipeline.available = lambda: (True, "")  # type: ignore[assignment]
    calls: list[int] = []

    def always_transient(name, target, *, attempt, attempts, plan):
        calls.append(attempt)
        stage = StageOutcome(stage=name, mode=Mode.AGENTIC, status=Status.PARTIAL)
        stage.error, stage.detail = "agent-failed", "503 service unavailable"
        return stage

    pipeline._agent_stage = always_transient  # type: ignore[assignment]
    pipeline._agent_attempts(Stage.ENDPOINT, TargetOutcome(target="x"))

    assert len(calls) == 5, "2 free retries + 3 attempts, then it stops"


def test_no_agent_still_forces_traditional_for_every_address(ctx) -> None:
    ctx.settings.set("mcp.agentic", "false")
    ctx.settings.set("mcp.discovery.target_concurrency", "3")
    pipeline, mode, _why = McpPhase(ctx)._pipeline()
    assert mode == Mode.TRADITIONAL
    assert not isinstance(pipeline, AgenticPipeline)


# -- output stays grouped -------------------------------------------------------


def test_each_server_is_reported_as_one_contiguous_block(ctx) -> None:
    """Interleaved servers would make a concurrent run unreadable."""
    buf = io.StringIO()
    ctx.console = Console(stream=buf, color=False, glyph_set="ascii")
    result = PhaseResult(phase="mcp", title="MCP auto-discovery")
    phase = McpPhase(ctx)
    ctx.settings.set("mcp.discovery.target_concurrency", "3")

    for _address, outcome in phase._work_through(addresses(), Recorder(0.05), [Stage.ENDPOINT]):
        phase._report_target(result, outcome)

    printed = buf.getvalue()
    positions = [printed.index(t) for t in TARGETS if t in printed]
    assert positions == sorted(positions), "a server's lines appeared out of order"

    # Every line mentioning a target must sit between that target's rule and
    # the next one -- no line from b in the middle of a's block.
    blocks = {t: [] for t in TARGETS}
    current = None
    for line in printed.splitlines():
        for t in TARGETS:
            if line.rstrip().endswith("-") and t in line:
                current = t
        if current:
            blocks[current].append(line)
    for target, lines in blocks.items():
        others = [t for t in TARGETS if t != target]
        assert not any(o in "\n".join(lines) for o in others), f"{target} block was polluted"


def test_worker_commentary_is_muted_so_it_cannot_interleave(ctx) -> None:
    """The agent heartbeat does not name its target: four at once is noise."""
    buf = io.StringIO()
    console = Console(stream=buf, color=False, glyph_set="ascii")
    ctx.console = console

    console.mute_transient()
    console.progress("a.example.test :: endpoint", "tier 1")
    console.bullet("endpoint: still working (30.0s)", indent=6)
    console.write("a verdict, which is not transient")
    console.mute_transient(False)
    console.bullet("visible again")

    printed = buf.getvalue()
    assert "still working" not in printed
    assert "tier 1" not in printed
    assert "a verdict, which is not transient" in printed, "verdicts are never muted"
    assert "visible again" in printed


def test_muting_is_per_thread(ctx) -> None:
    """The main thread has to keep printing while the workers are quiet."""
    console = Console(stream=io.StringIO(), color=False)
    seen: dict[str, bool] = {}

    def worker() -> None:
        console.mute_transient()
        seen["worker"] = console.transient_muted

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join()

    assert seen["worker"] is True
    assert console.transient_muted is False, "the main thread was silenced too"


# -- the deadline, and what it does to work in flight ---------------------------
#
# --timeout is a wall-clock ceiling for the whole run. It stops the program
# *starting* new work; an address already being worked is left to finish,
# because killing an agent mid-turn discards tokens already paid for. So the run
# can overrun by as long as one address takes, and the addresses that are
# dropped are the tail of the list -- which is why order matters with a deadline.


def test_without_a_deadline_every_address_is_worked(ctx) -> None:
    """The default, and the regression guard for everything below."""
    got, pipeline = walk(ctx, concurrency=1)
    assert [a.url for a, _ in got] == TARGETS
    assert ctx.deadline.skipped == []


def test_one_at_a_time_stops_admitting_when_the_time_is_up(ctx) -> None:
    from initialize_claude_ai.core.deadline import Deadline

    ctx.deadline = Deadline(limit=10.0, started=time.monotonic() - 11.0)
    got, pipeline = walk(ctx, concurrency=1)

    assert got == [], "no address may be started after the deadline"
    assert pipeline.started == []
    assert ctx.deadline.skipped == TARGETS, "and every one of them is named"


def test_one_at_a_time_lets_the_address_in_progress_finish(ctx) -> None:
    """The deadline passes while the first address is being worked.

    It is not interrupted -- the check is at admission -- so exactly one
    address completes and the remaining two are named as never attempted.
    """
    from initialize_claude_ai.core.deadline import Deadline

    ctx.deadline = Deadline(limit=0.05)
    got, pipeline = walk(ctx, concurrency=1, delay=0.2)

    assert [a.url for a, _ in got] == TARGETS[:1]
    assert pipeline.started == TARGETS[:1], "the deadline must not interrupt work in flight"
    assert ctx.deadline.skipped == TARGETS[1:]


def test_queued_addresses_are_dropped_and_running_ones_are_not(ctx) -> None:
    """Concurrently: what has not begun is cancelled, what has begun finishes.

    Two workers, three addresses, and a deadline already gone: the two the pool
    picked up immediately run to completion, the third was still queued and is
    dropped. Cancelling it is instant and costs nothing; cancelling the other
    two would throw away agent turns already paid for.
    """
    from initialize_claude_ai.core.deadline import Deadline

    ctx.deadline = Deadline(limit=10.0, started=time.monotonic() - 11.0)
    got, pipeline = walk(ctx, concurrency=2, delay=0.2)

    worked = [a.url for a, _ in got]
    assert worked == TARGETS[:2], f"the two in flight should finish, got {worked}"
    assert ctx.deadline.skipped == TARGETS[2:], "the queued one is named, not silently gone"
    assert TARGETS[2] not in pipeline.started, "a dropped address must never be started"
