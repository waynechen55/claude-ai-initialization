"""The agent runtimes: what bounds a stage, and what it says while it runs.

A discovery stage delegates to a subagent and then has nothing to report until
that agent finishes. Two things make that dangerous rather than merely quiet: a
stage with no wall-clock ceiling can run until somebody kills the process, and a
stage that prints nothing cannot be told apart from one that has wedged. Both
are properties of the runtime, so both are pinned here.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from fake_secrets import fake

from initialize_claude_ai.mcp_auto_discovery.runtime import (
    AgentDefinitionSpec,
    AgentResult,
    CliRuntime,
    SdkRuntime,
    _activity_of,
)


class Message:
    """An SDK message, in the only shapes the runtime reads."""

    def __init__(self, text: str = "", content: Any = None, **extra: Any) -> None:
        if text:
            self.result = text
        if content is not None:
            self.content = content
        for key, value in extra.items():
            setattr(self, key, value)


class ToolUse:
    def __init__(self, name: str, **payload: Any) -> None:
        self.name = name
        self.input = payload


class Stream:
    """Stands in for the SDK's ``query()`` generator, with a pace of its own."""

    def __init__(self, messages: list[Any], delay: float = 0.0) -> None:
        self.messages = list(messages)
        self.delay = delay
        self.closed = False

    def __aiter__(self) -> "Stream":
        return self

    async def __anext__(self) -> Any:
        if self.delay:
            await asyncio.sleep(self.delay)
        if not self.messages:
            raise StopAsyncIteration
        return self.messages.pop(0)

    async def aclose(self) -> None:
        self.closed = True


def consume(runtime: SdkRuntime, stream: Stream, hook=None) -> tuple[AgentResult, list[str]]:
    result, chunks = AgentResult(runtime=runtime.name), []
    asyncio.run(runtime._consume(stream, result, chunks, hook))
    result.text = "\n".join(chunks).strip()
    return result, chunks


def sdk(ctx, **overrides: Any) -> SdkRuntime:
    for key, value in overrides.items():
        ctx.settings.set(f"mcp.agent.{key}", value)
    # section() snapshots, so every override has to precede the runtime.
    return SdkRuntime(ctx)


# ----------------------------------------------------------------------------
# the ceiling
# ----------------------------------------------------------------------------


def test_both_runtimes_read_the_same_ceiling(ctx) -> None:
    """One setting, or the two runtimes disagree about how long a stage may take.

    The SDK path used to ignore ``timeout_seconds`` entirely, which meant the
    bound on a stage depended on which runtime happened to be available.
    """
    assert SdkRuntime(ctx).time_limit() == CliRuntime(ctx).time_limit() == 900.0


def test_an_agent_that_runs_long_is_stopped(ctx) -> None:
    runtime = sdk(ctx, timeout_seconds=0.05, progress_interval_seconds=0)
    stream = Stream([Message("first"), Message("second")], delay=0.5)

    result, _ = consume(runtime, stream)

    assert "timeout_seconds" in result.error
    assert "0.05" in result.error


def test_what_the_agent_managed_to_say_survives_the_timeout(ctx) -> None:
    """A stage that found the answer and then ran long is worth more than nothing."""
    runtime = sdk(ctx, timeout_seconds=0.15, progress_interval_seconds=0)

    async def paced() -> Any:
        yield Message("the answer")
        await asyncio.sleep(5)
        yield Message("never arrives")

    result, chunks = consume(runtime, paced())

    assert chunks == ["the answer"]
    assert result.error


def test_the_abandoned_stream_is_closed(ctx) -> None:
    """The generator owns the CLI subprocess; a cancelled read would orphan it."""
    runtime = sdk(ctx, timeout_seconds=0.05, progress_interval_seconds=0)
    stream = Stream([Message("one"), Message("two")], delay=0.5)

    consume(runtime, stream)

    assert stream.closed is True


def test_a_stream_that_finishes_is_closed_too(ctx) -> None:
    runtime = sdk(ctx, progress_interval_seconds=0)
    stream = Stream([Message("done")])

    result, _ = consume(runtime, stream)

    assert stream.closed is True
    assert result.error == ""


def test_zero_seconds_means_no_ceiling(ctx) -> None:
    """The old behaviour stays reachable for anyone who wants it."""
    runtime = sdk(ctx, timeout_seconds=0, progress_interval_seconds=0)
    stream = Stream([Message("slow but finished")], delay=0.05)

    result, chunks = consume(runtime, stream)

    assert runtime.time_limit() == 0
    assert chunks == ["slow but finished"]
    assert result.error == ""


def test_a_negative_ceiling_is_read_as_no_ceiling_not_as_expired(ctx) -> None:
    runtime = sdk(ctx, timeout_seconds=-30, progress_interval_seconds=0)
    stream = Stream([Message("ran")])

    result, _ = consume(runtime, stream)

    assert result.error == ""


# ----------------------------------------------------------------------------
# the heartbeat
# ----------------------------------------------------------------------------


def test_the_agent_says_it_is_still_working(ctx) -> None:
    """Otherwise eight minutes of legitimate work looks like a hung process."""
    runtime = sdk(ctx, progress_interval_seconds=0.02)
    stream = Stream([Message("done")], delay=0.2)
    lines: list[str] = []

    consume(runtime, stream, lines.append)

    assert any("still working" in line for line in lines)


def test_the_heartbeat_can_be_turned_off(ctx) -> None:
    runtime = sdk(ctx, progress_interval_seconds=0)
    stream = Stream([Message("done")], delay=0.1)
    lines: list[str] = []

    consume(runtime, stream, lines.append)

    assert lines == []


def test_a_timeout_is_announced_where_the_waiting_happened(ctx) -> None:
    runtime = sdk(ctx, timeout_seconds=0.05, progress_interval_seconds=0)
    stream = Stream([Message("one"), Message("two")], delay=0.5)
    lines: list[str] = []

    consume(runtime, stream, lines.append)

    assert any("timeout" in line for line in lines)


def test_without_a_hook_nothing_is_reported_and_nothing_breaks(ctx) -> None:
    runtime = sdk(ctx, progress_interval_seconds=0.01)
    stream = Stream([Message("done")], delay=0.05)

    result, _ = consume(runtime, stream, None)

    assert result.error == ""


# ----------------------------------------------------------------------------
# verbose: what the agent is actually doing
# ----------------------------------------------------------------------------


def test_verbose_names_each_tool_call(ctx) -> None:
    runtime = sdk(ctx, verbose=True, progress_interval_seconds=0)
    stream = Stream([Message(content=[ToolUse("WebFetch", url="https://example.com/mcp")])])
    lines: list[str] = []

    consume(runtime, stream, lines.append)

    assert lines == ["using WebFetch: https://example.com/mcp"]


def test_quiet_is_the_default(ctx) -> None:
    """The setting existed and was read nowhere; now it is read, off stays off."""
    runtime = sdk(ctx, progress_interval_seconds=0)
    stream = Stream([Message(content=[ToolUse("Bash", command="curl -s https://example.com")])])
    lines: list[str] = []

    consume(runtime, stream, lines.append)

    assert runtime.verbose() is False
    assert lines == []


def test_a_tool_call_is_redacted_like_everything_else(ctx) -> None:
    runtime = sdk(ctx, verbose=True, progress_interval_seconds=0)
    token = fake("sk-ant-", "api03-ab", "cdefghij", "klmnop12", "34")
    stream = Stream([Message(content=[ToolUse("Bash", command=f"curl -H 'X: Bearer {token}'")])])
    lines: list[str] = []

    consume(runtime, stream, lines.append)

    assert lines and token not in "".join(lines)


@pytest.mark.parametrize(
    "block, expected",
    [
        (ToolUse("WebSearch", query="github mcp server"), "using WebSearch: github mcp server"),
        (ToolUse("Read", file_path="/tmp/notes.md"), "using Read: /tmp/notes.md"),
        (ToolUse("Task"), "using Task"),
        ({"name": "Grep", "input": {"pattern": "mcp"}}, "using Grep: mcp"),
    ],
)
def test_one_line_per_tool_call_whatever_shape_the_block_is(block, expected) -> None:
    assert _activity_of(Message(content=[block])) == expected


def test_prose_is_not_reported_as_activity() -> None:
    """The model drafting its answer is not progress a reader can act on."""
    assert _activity_of(Message("I will now look at the registry.")) == ""


def test_a_long_argument_is_clipped_to_one_line() -> None:
    line = _activity_of(Message(content=[ToolUse("Bash", command="echo " + "x" * 400)]))

    assert len(line) < 140
    assert line.endswith("...")


# ----------------------------------------------------------------------------
# accounting still works
# ----------------------------------------------------------------------------


def test_cost_and_turns_are_still_read_off_the_messages(ctx) -> None:
    runtime = sdk(ctx, progress_interval_seconds=0)
    stream = Stream([Message("done", total_cost_usd=0.42, num_turns=7)])

    result, _ = consume(runtime, stream)

    assert (result.cost_usd, result.turns) == (0.42, 7)


def test_an_error_message_is_kept_as_the_error(ctx) -> None:
    runtime = sdk(ctx, progress_interval_seconds=0)
    stream = Stream([Message("it went wrong", is_error=True)])

    result, _ = consume(runtime, stream)

    assert result.error == "it went wrong"


# ----------------------------------------------------------------------------
# where the progress goes
# ----------------------------------------------------------------------------


def agentic(ctx, **console_kwargs):
    import io

    from initialize_claude_ai.core.logging_setup import Console
    from initialize_claude_ai.mcp_auto_discovery.agentic import AgenticPipeline

    stream = io.StringIO()
    ctx.console = Console(color=False, stream=stream, **console_kwargs)
    return AgenticPipeline(ctx), stream


def test_every_progress_line_says_which_stage_it_came_from(ctx) -> None:
    """Four stages run in sequence against one target; an unlabelled heartbeat
    would not say which of them is the slow one."""
    pipeline, stream = agentic(ctx)

    hook = pipeline._progress_hook("package")
    assert hook is not None
    hook("still working (8m02s)")

    assert "package: still working (8m02s)" in stream.getvalue()


def test_verdicts_only_means_no_heartbeat_either(ctx) -> None:
    """console.progress = false already means "do not tell me about work in
    flight"; a heartbeat is exactly that."""
    pipeline, _ = agentic(ctx, show_progress=False)

    assert pipeline._progress_hook("package") is None


def test_the_cli_runtime_hands_the_ceiling_to_the_process(ctx, monkeypatch) -> None:
    """0 has to arrive as 0: the runner reads that as "no ceiling", while None
    would quietly mean "whatever the process-wide default is"."""
    from pathlib import Path

    seen: list[object] = []
    monkeypatch.setattr(type(ctx), "tool", lambda self, *a, **k: "/usr/bin/claude")
    monkeypatch.setattr(CliRuntime, "supported_flags", lambda self: set())
    monkeypatch.setattr(ctx.runner, "run", lambda argv, **kw: seen.append(kw.get("timeout")) or _ok_result(argv))

    for configured, expected in ((900, 900.0), (0, 0.0)):
        ctx.settings.set("mcp.agent.timeout_seconds", configured)
        CliRuntime(ctx).run(
            "go",
            agent=AgentDefinitionSpec(name="scout", description="", prompt=""),
            workspace=Path("."),
        )

        assert seen[-1] == expected


def _ok_result(argv):
    from initialize_claude_ai.core.proc import CommandResult

    return CommandResult(argv=list(argv), returncode=0, stdout='{"result": "{}"}')
