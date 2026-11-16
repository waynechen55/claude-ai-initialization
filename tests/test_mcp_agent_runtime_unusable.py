"""A failure about the installation is not a failure about the request.

A whole run produced no ``.mcp.json`` at all, and the reason was in its own
output twenty-nine times::

    "api_error_status":404,
    "result":"The model claude-opus-5-5 is not available on your vertex
              deployment. Try --model to switch to claude-opus-5, or ask your
              admin to enable this model."

Three separate faults turned that into silence.

**The message was unreadable.** A failed ``claude -p --output-format json``
prints a whole result object, and the capture took the *last* 600 characters of
it -- which for a JSON document is guaranteed to begin mid-key. What reached the
report was ``ackground":0,"foreground":0,...``. The sentence naming the remedy
was never once legible, and the classifier that should have acted on it was
reading the same slice.

**There was no class for it.** ``failure_kind`` knew refusal, timeout,
transient and other. "not available" is none of those -- note that ``_TRANSIENT``
matches *unavailable*, one word away -- so it landed in ``other``, whose remedy
is "change approach and try again".

**So it was retried, everywhere.** Three attempts a stage, four stages a target,
thirteen targets: the same 404, minutes at a time, for a condition that could
not change between attempts. Every target silently degraded to the deterministic
pipeline, ``developers.google.com`` among them -- it lost the native endpoint
the agentic path had found on a previous machine, and finished with nothing to
register.

The fix is one distinction: a failure about the *runtime* is the same for every
stage and every target, so the answer is not a different attempt. It is to stop
attempting, say so once, and carry on deterministically.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from initialize_claude_ai.mcp_auto_discovery.runtime import (
    OTHER,
    TRANSIENT,
    UNUSABLE,
    _failure_message,
    failure_kind,
)

#: The real payload, as the CLI printed it.
REAL = (
    '{"background":0,"foreground":0,"unset":0,"is_error":true,"num_turns":1,'
    '"api_error_status":404,"result":"The model claude-opus-5-5 is not available '
    "on your vertex deployment. Try --model to switch to claude-opus-5, or ask "
    'your admin to enable this model.","type":"result","duration_ms":5310}'
)


# --------------------------------------------------------------------------- #
# the message
# --------------------------------------------------------------------------- #


def test_the_sentence_survives_instead_of_a_fragment_of_json() -> None:
    message = _failure_message(REAL, 600)
    assert message.startswith("The model claude-opus-5-5 is not available")
    assert "Try --model" in message, "the remedy is the point of keeping it"
    assert "background" not in message


def test_a_stack_trace_still_keeps_its_tail() -> None:
    """Not JSON, and for a traceback the last lines are the informative ones."""
    trace = "Traceback (most recent call last):\n  File x\nValueError: boom"
    assert _failure_message(trace, 600).endswith("ValueError: boom")


def test_an_empty_failure_stays_empty(ctx) -> None:
    assert _failure_message("", 600) == ""
    assert _failure_message("   ", 600) == ""


def test_the_limit_is_honoured() -> None:
    assert len(_failure_message(REAL, 20)) == 20


def test_malformed_json_falls_back_rather_than_raising() -> None:
    assert _failure_message('{"result": "half a doc', 600).endswith("half a doc")


# --------------------------------------------------------------------------- #
# the class
# --------------------------------------------------------------------------- #


def test_the_real_failure_is_classed_as_unusable() -> None:
    assert failure_kind(_failure_message(REAL, 600)) == UNUSABLE


@pytest.mark.parametrize(
    "text",
    [
        "The model x is not available on your vertex deployment",
        "model claude-opus-5 not found",
        "model foo does not exist",
        "invalid_api_key",
        "authentication_error: bad credentials",
        "quota exceeded for this project",
    ],
)
def test_installation_failures_are_all_unusable(text: str) -> None:
    assert failure_kind(text) == UNUSABLE


@pytest.mark.parametrize(
    "text",
    [
        "the service is temporarily unavailable",
        "overloaded_error",
        "rate limit exceeded",
        "HTTP 503",
    ],
)
def test_a_genuinely_transient_failure_is_still_transient(text: str) -> None:
    """The distinction is a single word -- "unavailable" versus "not available"
    -- so the two must be pinned against each other."""
    assert failure_kind(text) == TRANSIENT


def test_an_ordinary_failure_is_still_ordinary() -> None:
    assert failure_kind("the agent produced nothing usable") == OTHER


def test_an_operator_can_add_a_wording(ctx) -> None:
    """The phrasing belongs to whichever runtime is installed, not to us."""
    assert failure_kind("seat licence required") == OTHER
    assert failure_kind("seat licence required", ["seat licence"]) == UNUSABLE


# --------------------------------------------------------------------------- #
# and what the pipeline does about it
# --------------------------------------------------------------------------- #


def pipeline(ctx):
    from initialize_claude_ai.mcp_auto_discovery.agentic import AgenticPipeline

    return AgenticPipeline(ctx)


def test_once_proved_unusable_the_agent_is_not_asked_again(ctx) -> None:
    """The saving is the whole point: 4 stages x 13 targets of doomed calls.

    Each one costs the wall clock of a real agent invocation to be told the
    same thing, and the answer cannot change -- the model is not enabled for
    the installation, not for this request.
    """
    ctx.dry_run = False  # a dry run never invokes an agent, so nothing could latch
    agentic = pipeline(ctx)
    # A runtime is installed: the latch is the subject, not whether this machine
    # has the claude CLI or the agent SDK (a CI runner has neither).
    agentic.runtime = SimpleNamespace(name="stand-in")
    agentic._runtime_unusable = "The model x is not available on your vertex deployment"

    usable, why = agentic.available()
    assert usable is False
    assert "cannot serve any request" in why
    assert "not available on your vertex deployment" in why, "carry the remedy forward"


def test_before_that_the_agent_is_available_as_usual(ctx) -> None:
    agentic = pipeline(ctx)
    assert agentic._runtime_unusable == "", "nothing is latched until something proves it"


def test_the_operators_own_switch_still_wins(ctx) -> None:
    """--no-agent is an answer, not an obstacle, and is checked first."""
    ctx.settings.set("mcp.agentic", False)
    agentic = pipeline(ctx)
    agentic._runtime_unusable = "model not found"
    assert agentic.available() == (False, "--no-agent: agents are turned off for this run")


def test_the_extra_wordings_reach_the_pipeline(ctx) -> None:
    ctx.settings.set("mcp.agent.unusable_markers", "seat licence")
    assert pipeline(ctx)._failure_kind("seat licence required") == UNUSABLE


def test_the_verdict_says_it_once_rather_than_per_stage(ctx) -> None:
    """Twenty-nine identical per-stage notes are not a conclusion.

    The condition applied to every target equally, so the run-level verdict is
    the only place it can be said once and mean what it means.
    """
    from initialize_claude_ai.core.report import PhaseResult
    from initialize_claude_ai.mcp_auto_discovery.types import TargetOutcome
    from initialize_claude_ai.phases.phase4_mcp_auto_discovery import McpPhase

    phase = McpPhase(ctx)
    phase._ran_with = pipeline(ctx)
    phase._ran_with._runtime_unusable = "The model x is not available on your vertex deployment"

    result = PhaseResult(phase="mcp", title="MCP auto-discovery")
    phase._report_summary(result, [TargetOutcome(target="https://example.test")])

    said = " ".join(step.detail for step in result.steps)
    assert "THE AGENT NEVER RAN" in said
    assert "not available on your vertex deployment" in said
    assert "re-running phase 4" in said, "say what to do, not only what happened"


def test_a_healthy_run_says_nothing_of_the_kind(ctx) -> None:
    from initialize_claude_ai.core.report import PhaseResult
    from initialize_claude_ai.mcp_auto_discovery.types import TargetOutcome
    from initialize_claude_ai.phases.phase4_mcp_auto_discovery import McpPhase

    phase = McpPhase(ctx)
    phase._ran_with = pipeline(ctx)
    result = PhaseResult(phase="mcp", title="MCP auto-discovery")
    phase._report_summary(result, [TargetOutcome(target="https://example.test")])

    assert "THE AGENT NEVER RAN" not in " ".join(step.detail for step in result.steps)
