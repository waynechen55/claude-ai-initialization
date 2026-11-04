"""The two global time controls: a deadline for the run, a scale for each operation.

A single ``--timeout`` value substituted into every timeout is the obvious
design and a bad one. The operation timeouts here span 5 seconds (a TCP
connect) to 1800 (waiting for a person at a browser) -- a 360x range -- so one
number would either kill agent turns at 30 seconds or leave dead connections
hanging for half an hour. The two things people actually want are separable:

``--timeout``
    a wall-clock ceiling for the whole run. Not expressible as a timeout on
    anything smaller: phase 4 took 259 minutes against thirteen addresses with
    every individual timeout comfortably unmet, because that is what a few
    hundred well-behaved operations add up to. It stops *admission*; work
    already running is left to finish.

``--timeout-scale``
    one multiplier over every operation timeout, preserving the relationships.

The largest section here is the last one, which asserts that a run with neither
flag is byte-for-byte the run that existed before they did.
"""

from __future__ import annotations

import time

import pytest

from initialize_claude_ai.core.deadline import Deadline, DurationError, parse_duration

# --------------------------------------------------------------------------- #
# reading a duration
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("text", "seconds"),
    [
        ("45s", 45.0),
        ("90m", 5400.0),
        ("2h", 7200.0),
        ("1d", 86400.0),
        ("5400", 5400.0),
        (5400, 5400.0),
        (90.5, 90.5),
        ("2H", 7200.0),
        (" 2h ", 7200.0),
    ],
)
def test_a_duration_can_be_written_the_way_people_say_it(text, seconds) -> None:
    """`2h` is a better way to say two hours than `7200`, and bare means seconds."""
    assert parse_duration(text) == seconds


@pytest.mark.parametrize("text", ["", "   ", "0", "0s", "-5", "-5m", "2x", "abc", "2 hours", True])
def test_a_duration_that_is_not_one_is_refused_at_the_command_line(text) -> None:
    """Refused where it was typed, not three phases later as a broken setting."""
    with pytest.raises(DurationError):
        parse_duration(text)


def test_the_refusal_names_the_spellings_that_work() -> None:
    with pytest.raises(DurationError) as caught:
        parse_duration("tomorrow")
    assert "45s" in str(caught.value) and "2h" in str(caught.value)


# --------------------------------------------------------------------------- #
# the deadline
# --------------------------------------------------------------------------- #


def test_no_deadline_is_the_default_and_never_expires() -> None:
    """`none()` is an object, not a None to be checked for at every call site."""
    deadline = Deadline.none()
    assert deadline.set is False
    assert deadline.expired is False
    assert deadline.remaining == float("inf")


def test_a_deadline_expires_when_its_time_is_up() -> None:
    past = Deadline(limit=10.0, started=time.monotonic() - 11.0)
    assert past.set and past.expired
    assert past.remaining < 0

    future = Deadline(limit=3600.0)
    assert future.set and not future.expired
    assert 3599 < future.remaining <= 3600


def test_what_was_never_attempted_is_named_in_order_and_once() -> None:
    """The useful next step is re-running exactly these, so they are a list."""
    deadline = Deadline(limit=1.0)
    deadline.note_skipped("https://a.test", "https://b.test", "https://a.test", "")
    assert deadline.skipped == ["https://a.test", "https://b.test"]


def test_the_deadline_is_carried_in_the_report() -> None:
    deadline = Deadline(limit=60.0, started=time.monotonic() - 61.0, phase="mcp")
    payload = deadline.to_dict()
    assert payload["expired"] is True
    assert payload["limit_seconds"] == 60.0
    assert payload["phase_in_progress"] == "mcp"


# --------------------------------------------------------------------------- #
# the scale
# --------------------------------------------------------------------------- #


def test_a_scale_stretches_every_operation_timeout(ctx) -> None:
    ctx.settings.set("run.timeout_scale", 2.0)
    ctx.settings._scale = None  # re-read; the value is memoised per run

    assert ctx.settings.float_("http.timeout_seconds") == 30.0  # 15
    assert ctx.settings.int_("mcp.agent.timeout_seconds") == 1800  # 900
    assert ctx.settings.float_("http.connect_timeout_seconds") == 10.0  # 5


def test_it_reaches_a_timeout_read_through_a_section_view(ctx) -> None:
    """Most timeouts are read as `config.float_("timeout_seconds")`.

    The key is matched on its last segment for exactly this reason: a section
    view has no full path to match against, and matching full paths would have
    silently missed most of them.
    """
    ctx.settings.set("run.timeout_scale", 3.0)
    ctx.settings._scale = None
    section = ctx.settings.section("mcp.registration")
    assert section.float_("timeout_seconds") == 270.0  # 90


@pytest.mark.parametrize(
    ("key", "unscaled"),
    [
        # A heartbeat, not a timeout: stretching it makes the run quieter.
        ("mcp.agent.progress_interval_seconds", 30.0),
        # Thresholds for *scoring* a server. Stretching these changes which
        # server wins a ranking, which is not what "allow more time" means.
        ("mcp.ranking.latency_target_seconds", 1.0),
        ("mcp.ranking.latency_ceiling_seconds", 8.0),
        # The gap between SIGTERM and SIGKILL.
        ("run.stop_grace_seconds", 5.0),
    ],
)
def test_it_leaves_alone_the_things_that_only_look_like_timeouts(ctx, key, unscaled) -> None:
    ctx.settings.set("run.timeout_scale", 4.0)
    ctx.settings._scale = None
    assert ctx.settings.float_(key) == unscaled


def test_the_deadline_itself_is_never_scaled(ctx) -> None:
    """The one duration that must mean exactly what was typed.

    This is why the setting is `run.deadline_seconds` and not
    `run.timeout_seconds` -- a name outside the suffix rule is a better guard
    than an exception list nobody remembers to maintain.
    """
    ctx.settings.set("run.timeout_scale", 10.0)
    ctx.settings.set("run.deadline_seconds", 7200)
    ctx.settings._scale = None
    assert ctx.settings.float_("run.deadline_seconds") == 7200.0


def test_a_fractional_scale_shortens_them(ctx) -> None:
    ctx.settings.set("run.timeout_scale", 0.5)
    ctx.settings._scale = None
    assert ctx.settings.float_("http.timeout_seconds") == 7.5


# --------------------------------------------------------------------------- #
# and with neither flag, nothing whatsoever changes
# --------------------------------------------------------------------------- #

#: Every time-related setting in the packaged defaults, with the value the
#: program read before either flag existed. A run with no --timeout and no
#: --timeout-scale must produce exactly these, by value *and* by type -- 8 must
#: not quietly become 8.0 on its way to a subprocess.
UNCHANGED = {
    "auth.anthropic_api.probe_timeout_seconds": 30,
    "auth.claude_oauth.probe_timeout_seconds": 120,
    "auth.interactive_timeout_seconds": 1800,
    "auth.reset_timeout_seconds": 120,
    "http.connect_timeout_seconds": 5,
    "http.timeout_seconds": 15,
    "install.download_timeout_seconds": 600,
    "install.package_timeout_seconds": 900,
    "mcp.address.timeout_seconds": 6,
    "mcp.agent.progress_interval_seconds": 30,
    "mcp.agent.timeout_seconds": 900,
    "mcp.discovery.per_request_timeout_seconds": 8,
    "mcp.discovery.stage_timeout_seconds": 180,
    "mcp.discovery.web_grounding.timeout_seconds": 8,
    "mcp.packages.trial_timeout_seconds": 90,
    "mcp.ranking.probe_timeout_seconds": 8,
    "mcp.registration.timeout_seconds": 90,
    "mcp.verification.handshake_timeout_seconds": 20,
    "mcp.verification.stdio_startup_timeout_seconds": 25,
    "provision.model_discovery_timeout_seconds": 30,
    "run.command_timeout_seconds": 600,
    "run.stop_grace_seconds": 5,
}


@pytest.mark.parametrize(("key", "expected"), sorted(UNCHANGED.items()))
def test_without_the_flags_every_timeout_is_exactly_what_it_was(ctx, key, expected) -> None:
    assert ctx.settings.float_(key) == float(expected)
    assert ctx.settings.int_(key) == int(expected)


def test_the_default_scale_does_no_arithmetic_at_all(ctx, monkeypatch) -> None:
    """Not "multiplies by 1.0" -- takes the value and returns it.

    A no-op that still runs the arithmetic is a no-op until the day a float
    creeps in. This asserts the short circuit rather than the result.
    """
    assert ctx.settings.timeout_scale() == 1.0
    sentinel = object()
    assert ctx.settings._scaled("anything.timeout_seconds", sentinel) is sentinel


def test_the_default_run_has_no_deadline(ctx) -> None:
    assert ctx.deadline.set is False
    assert ctx.deadline.expired is False
    assert ctx.settings.float_("run.deadline_seconds", 0.0) == 0.0


# --------------------------------------------------------------------------- #
# --agent-max-turns: one budget for every subagent
# --------------------------------------------------------------------------- #
#
# Each agent defines its own (endpoint 10, package 10, bridge 12, strategy 10),
# and `mcp.agent.max_turns` = 24 is only a fallback for an agent that sets none.
# Since all four set one, that fallback is unreachable -- so a flag pointed at
# it would have done nothing at all, which is why the override is a separate
# value that beats the definitions.

from initialize_claude_ai.mcp_auto_discovery.runtime import AgentDefinitionSpec  # noqa: E402


def spec(max_turns: int = 0) -> AgentDefinitionSpec:
    return AgentDefinitionSpec(name="a", description="d", prompt="p", max_turns=max_turns)


def test_an_agents_own_budget_beats_the_fallback() -> None:
    """Which is why raising mcp.agent.max_turns changes nothing today."""
    assert spec(10).turn_budget(24) == 10


def test_the_fallback_is_used_only_by_an_agent_that_sets_none() -> None:
    assert spec(0).turn_budget(24) == 24


def test_the_override_beats_both() -> None:
    assert spec(10).turn_budget(24, override=6) == 6
    assert spec(12).turn_budget(24, override=30) == 30
    assert spec(0).turn_budget(24, override=6) == 6


def test_no_override_leaves_every_agent_exactly_as_it_was() -> None:
    for own in (10, 12):
        assert spec(own).turn_budget(24, override=0) == own


@pytest.mark.parametrize(
    ("agent", "turns"),
    [
        ("mcp-endpoint-scout", 10),
        ("mcp-package-scout", 10),
        ("mcp-bridge-engineer", 12),
        ("mcp-strategist", 10),
    ],
)
def test_the_shipped_budgets_are_what_the_readme_says(ctx, agent, turns) -> None:
    """Pinned because the README quotes them and the flag is described against them."""
    assert ctx.settings.int_(f"mcp.agent.definitions.{agent}.max_turns") == turns


def test_the_override_is_off_by_default(ctx) -> None:
    assert ctx.settings.int_("mcp.agent.max_turns_override", -1) == 0
