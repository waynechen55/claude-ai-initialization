"""A timeout has to bound the wait, not just the child.

The interesting case is not a slow command -- it is a command that starts
something else and leaves. The grandchild inherits the pipes, so reading them
goes on long after the child has gone, and CPython's own ``subprocess.run``
answers a timeout by killing the child it knows about and then waiting on those
pipes with no deadline at all. A 900-second limit on one agent call once held a
run open for ten and a half hours that way.

So: the child gets a session of its own, the stop signals go to the whole
group, every wait after the timeout has an end, and a child that survives the
last signal is abandoned rather than waited for.
"""

from __future__ import annotations

import os
import signal
import sys
import time

import pytest

from initialize_claude_ai.core.proc import Runner

#: Short enough to keep the suite quick, long enough not to race a loaded machine.
LIMIT = 1.0
GRACE = 0.5

#: Prints the pid of a grandchild that outlives it, holding the inherited pipes.
LEAVES_A_GRANDCHILD = (
    "import subprocess, sys;"
    "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)']);"
    "print(p.pid, flush=True)"
)
#: Ignores the polite signal, so only the escalation can end it.
IGNORES_SIGTERM = (
    "import signal, sys, time;signal.signal(signal.SIGTERM, signal.SIG_IGN);print('ready', flush=True);time.sleep(120)"
)


@pytest.fixture
def runner() -> Runner:
    return Runner(default_timeout=LIMIT, stop_grace_seconds=GRACE)


def _zombie(pid: int) -> bool:
    """Has ``pid`` exited and only waits to be reaped? Without an init process
    (a container whose PID 1 never calls wait) an orphan stays a zombie, and
    ``kill(pid, 0)`` still succeeds for it."""
    try:
        with open(f"/proc/{pid}/stat", encoding="ascii", errors="replace") as handle:
            return handle.read().rsplit(")", 1)[-1].split()[0] in ("Z", "X")
    except OSError:
        return False


def gone(pid: int, *, within: float = 5.0) -> bool:
    """Has ``pid`` stopped running? Reaping is not instant, and may never come."""
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except (ProcessLookupError, PermissionError):
            return True
        if _zombie(pid):
            return True
        time.sleep(0.05)
    return False


# -- the success path is untouched ---------------------------------------------


def test_a_command_that_finishes_is_reported_exactly_as_before(runner: Runner) -> None:
    result = runner.run([sys.executable, "-c", "print('hello')"])

    assert result.ok
    assert result.returncode == 0
    assert result.stdout.strip() == "hello"
    assert not result.timed_out


def test_both_streams_and_the_exit_status_still_come_back(runner: Runner) -> None:
    result = runner.run([sys.executable, "-c", "import sys; sys.stderr.write('bad'); sys.exit(3)"])

    assert result.returncode == 3
    assert result.stderr.strip() == "bad"
    assert not result.ok


def test_input_still_reaches_the_child(runner: Runner) -> None:
    result = runner.run(
        [sys.executable, "-c", "import sys; print(sys.stdin.read().strip().upper())"],
        stdin_text="secretless payload\n",
    )

    assert result.stdout.strip() == "SECRETLESS PAYLOAD"


def test_a_command_that_is_not_there_is_still_127(runner: Runner) -> None:
    assert runner.run(["no-such-command-at-all"]).returncode == 127


def test_a_file_that_cannot_be_executed_is_still_126(runner: Runner, tmp_path) -> None:
    script = tmp_path / "not-executable"
    script.write_text("#!/bin/sh\n")

    assert runner.run([str(script)]).returncode == 126


# -- the timeout is a promise about the wait -----------------------------------


@pytest.mark.skipif(os.name != "posix", reason="process groups are a POSIX idea")
def test_a_child_that_walks_away_does_not_hold_the_run_open(runner: Runner) -> None:
    """The one the log caught: the child exits at once, the grandchild keeps the
    pipes, and the old code waited on them for as long as the grandchild lived."""
    started = time.monotonic()
    result = runner.run([sys.executable, "-c", LEAVES_A_GRANDCHILD])
    elapsed = time.monotonic() - started

    assert result.timed_out
    assert result.returncode == 124
    assert elapsed < LIMIT + GRACE * 4, "the wait outlasted the timeout and its grace"
    assert gone(int(result.stdout.split()[0])), "the grandchild was left running"


@pytest.mark.skipif(os.name != "posix", reason="process groups are a POSIX idea")
def test_whatever_the_child_managed_to_say_is_kept(runner: Runner) -> None:
    """A timed-out command is still evidence; throwing its output away would
    make the one failure that needs explaining the one with no detail."""
    result = runner.run([sys.executable, "-c", LEAVES_A_GRANDCHILD])

    assert result.stdout.strip().isdigit()


def test_a_slow_child_times_out_and_says_so(runner: Runner) -> None:
    result = runner.run([sys.executable, "-c", "import time; time.sleep(120)"])

    assert result.timed_out
    assert result.returncode == 124
    assert f"timed out after {LIMIT}s" in result.stderr


def test_the_timeout_argument_still_beats_the_default(runner: Runner) -> None:
    started = time.monotonic()
    result = runner.run([sys.executable, "-c", "import time; time.sleep(120)"], timeout=0.2)

    assert result.timed_out
    assert time.monotonic() - started < LIMIT + GRACE * 4


@pytest.mark.skipif(os.name != "posix", reason="signal escalation is a POSIX idea")
def test_a_child_that_ignores_the_polite_signal_is_killed_anyway(runner: Runner) -> None:
    started = time.monotonic()
    result = runner.run([sys.executable, "-c", IGNORES_SIGTERM])
    elapsed = time.monotonic() - started

    assert result.returncode == 124
    assert elapsed >= LIMIT + GRACE, "SIGKILL was sent before SIGTERM was given its grace"
    assert elapsed < LIMIT + GRACE * 4


@pytest.mark.skipif(os.name != "posix", reason="signal escalation is a POSIX idea")
def test_a_child_that_survives_the_last_signal_is_abandoned_not_waited_for() -> None:
    """Configured with a signal that cannot end anything, there is no way to
    stop the child -- and the run still has to continue without it."""
    runner = Runner(default_timeout=0.3, stop_signals=["WINCH"], stop_grace_seconds=0.3)

    started = time.monotonic()
    result = runner.run([sys.executable, "-c", "import time; time.sleep(3)"])
    elapsed = time.monotonic() - started

    assert result.returncode == 124
    assert result.timed_out
    assert elapsed < 2.0, "the run waited on a child it could not stop"


@pytest.mark.skipif(os.name != "posix", reason="sessions are a POSIX idea")
def test_an_interrupted_wait_does_not_leave_the_child_behind(runner: Runner, monkeypatch) -> None:
    """A captured child is in a session of its own, so Ctrl-C no longer reaches
    it through the terminal; the wait that was interrupted has to end it."""
    import subprocess

    real = subprocess.Popen.communicate
    seen: dict[str, int] = {}

    def interrupted(self, input=None, timeout=None):  # noqa: A002 - matching the signature
        if not seen:
            seen["pid"] = self.pid
            raise KeyboardInterrupt
        return real(self, input=input, timeout=timeout)

    monkeypatch.setattr(subprocess.Popen, "communicate", interrupted)

    with pytest.raises(KeyboardInterrupt):
        runner.run([sys.executable, "-c", "import time; time.sleep(120)"])

    assert gone(seen["pid"])


def test_check_still_raises_on_a_timeout(runner: Runner) -> None:
    from initialize_claude_ai.core.errors import CommandError

    with pytest.raises(CommandError):
        runner.run([sys.executable, "-c", "import time; time.sleep(120)"], check=True)


# -- how the stopping is configured --------------------------------------------


def test_the_ladder_is_whatever_the_host_configured() -> None:
    assert Runner(stop_signals=["TERM", "KILL"]).stop_ladder() == [
        int(signal.SIGTERM),
        int(signal.SIGKILL),
    ]
    assert Runner(stop_signals=["sigint", "9"]).stop_ladder() == [int(signal.SIGINT), 9]


def test_an_unusable_ladder_still_ends_in_something_final() -> None:
    """A typo in a config file must not be able to remove the only guarantee
    this code makes."""
    assert Runner(stop_signals=[]).stop_ladder() == [int(signal.SIGKILL)]
    assert Runner(stop_signals=["SIGNOTHING", " "]).stop_ladder() == [int(signal.SIGKILL)]


@pytest.mark.skipif(os.name != "posix", reason="sessions are a POSIX idea")
def test_a_child_talking_to_the_operator_keeps_our_session() -> None:
    """Its own session would take it out of the terminal's foreground group,
    and Ctrl-C would stop reaching the prompt it is showing."""
    assert Runner._detachable(capture=True, discard=False)
    assert Runner._detachable(capture=False, discard=True)
    assert not Runner._detachable(capture=False, discard=False)


def test_a_dry_run_still_starts_nothing() -> None:
    result = Runner(dry_run=True).run(["rm", "-rf", "/"], simulate_stdout="nothing happened")

    assert result.simulated
    assert result.ok
    assert result.stdout == "nothing happened"
