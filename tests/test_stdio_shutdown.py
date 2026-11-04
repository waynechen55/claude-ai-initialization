"""Ending a stdio trial has to take bounded time, whatever the server does.

The failure these guard against: jira.example.com's package stage fell back to the
deterministic trial, launched a package, and the run never logged another line.
Each request was bounded, the shutdown was not. ``_StdioSession.close`` closed
stdout while a reader thread was blocked on it -- which waits for that read,
which ends only when everything holding the pipe exits -- so a server that
ignored stdin closing held the run open forever, and the terminate/kill after
the close was never reached. The same code ends trials in agent and traditional
modes alike, and the writer's launch of each finished entry.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import time

import pytest

from initialize_claude_ai.mcp_auto_discovery.probes import McpProber, _StdioSession
from initialize_claude_ai.mcp_auto_discovery.types import Candidate, Transport
from initialize_claude_ai.mcp_auto_discovery.verifier import CandidateVerifier

pytestmark = pytest.mark.skipif(os.name != "posix", reason="process groups are POSIX")

#: Generous against a loaded CI host, and far below "indefinitely".
BOUND = 15.0


def launch(code: str) -> subprocess.Popen:
    """A child in a group of its own, as probe_stdio starts servers."""
    return subprocess.Popen(
        [sys.executable, "-c", code],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        start_new_session=True,
    )


def closed_in(session: _StdioSession) -> float:
    started = time.monotonic()
    session.close()
    return time.monotonic() - started


def _zombie(pid: int) -> bool:
    """Has ``pid`` exited and only waits to be reaped? Without an init process
    (a container whose PID 1 never calls wait) an orphan stays a zombie, and
    ``kill(pid, 0)`` still succeeds for it."""
    try:
        with open(f"/proc/{pid}/stat", encoding="ascii", errors="replace") as handle:
            return handle.read().rsplit(")", 1)[-1].split()[0] in ("Z", "X")
    except OSError:
        return False


def alive(pid: int) -> bool:
    """Is ``pid`` still running? A zombie has exited, whoever is yet to reap it."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return not _zombie(pid)


IGNORES_STDIN = "import time\nwhile True: time.sleep(1)"
IGNORES_TERM = "import signal, time\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\nwhile True: time.sleep(1)"
# The launcher shape: the process we started exits, a child it left behind keeps
# our stdout. It prints its pid so the test can check it was reached.
LEAVES_A_CHILD = (
    "import subprocess, sys\n"
    "child = subprocess.Popen([sys.executable, '-c', 'import time\\nwhile True: time.sleep(1)'])\n"
    "print(child.pid, flush=True)\n"
)


def test_a_server_that_ignores_stdin_closing_is_stopped() -> None:
    session = _StdioSession(launch(IGNORES_STDIN), 1, shutdown_grace=0.2, stop_grace=1)
    assert closed_in(session) < BOUND
    assert session.process.poll() is not None


def test_one_that_ignores_the_polite_signal_is_killed() -> None:
    session = _StdioSession(launch(IGNORES_TERM), 1, shutdown_grace=0.2, stop_grace=1)
    assert closed_in(session) < BOUND
    assert session.process.poll() is not None


def test_a_grandchild_holding_the_pipe_is_reached_after_its_parent_exits() -> None:
    """The npx/uvx shape, and the one that stalled the run."""
    session = _StdioSession(launch(LEAVES_A_CHILD), 1, shutdown_grace=0.2, stop_grace=1)
    line = session._lines.get(timeout=BOUND)
    grandchild = int(str(line).strip())
    assert closed_in(session) < BOUND
    deadline = time.monotonic() + BOUND
    while alive(grandchild) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not alive(grandchild)


def test_a_server_that_exits_on_eof_is_not_signalled() -> None:
    session = _StdioSession(launch("import sys\nfor _ in sys.stdin: pass"), 1, shutdown_grace=5)
    assert closed_in(session) < BOUND
    assert session.process.returncode == 0


def test_a_pipe_held_outside_the_group_is_abandoned_not_waited_on(caplog) -> None:
    """Something we cannot signal may keep the pipe; the run still moves on."""
    code = (
        "import os, subprocess, sys, time\n"
        "subprocess.Popen([sys.executable, '-c', 'import time\\ntime.sleep(8)'], start_new_session=True)\n"
        "time.sleep(8)\n"
    )
    session = _StdioSession(launch(code), 1, shutdown_grace=0.2, stop_grace=0.5)
    time.sleep(0.5)
    with caplog.at_level(logging.WARNING):
        assert closed_in(session) < BOUND
    assert "abandoned" in caplog.text


def test_the_session_ceiling_caps_every_request() -> None:
    session = _StdioSession(launch(IGNORES_STDIN), 60, ceiling=0.5, shutdown_grace=0, stop_grace=1)
    started = time.monotonic()
    assert session.call({"jsonrpc": "2.0", "id": 1, "method": "initialize"}) is None
    assert time.monotonic() - started < 5
    session.close()


def test_probe_stdio_starts_the_server_in_a_group_of_its_own(ctx, monkeypatch) -> None:
    seen: dict = {}
    real = subprocess.Popen

    def popen(argv, **kwargs):
        seen.update(kwargs)
        return real(argv, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", popen)
    result = McpProber(ctx).probe_stdio([sys.executable, "-c", IGNORES_STDIN], timeout=0.5)
    assert seen["start_new_session"] is True
    assert result.ok is False


def test_a_probe_of_a_stuck_server_returns(ctx) -> None:
    started = time.monotonic()
    McpProber(ctx).probe_stdio([sys.executable, "-c", IGNORES_TERM], timeout=0.5)
    assert time.monotonic() - started < BOUND + ctx.runner.stop_grace_seconds * 2


def test_the_shutdown_is_configurable(settings) -> None:
    assert settings.float_("mcp.verification.stdio_shutdown_grace_seconds") >= 0
    assert settings.float_("mcp.verification.stdio_session_timeout_seconds") >= 0


def test_each_trial_is_announced_before_it_starts(ctx, caplog, monkeypatch) -> None:
    """A silent log after the relevance filter was all the operator had to go on."""
    verifier = CandidateVerifier(ctx)
    monkeypatch.setattr(verifier, "verify", lambda candidate: candidate)
    candidates = [
        Candidate(kind="stdio", name="first", command=["npx", "-y", "a-pkg"], transport=Transport.STDIO),
        Candidate(kind="url", name="second", url="https://x.test/mcp"),
    ]
    with caplog.at_level(logging.INFO):
        verifier.verify_all(candidates, stop_at_first=False)
    assert "trialling 1 of 2" in caplog.text and "npx -y a-pkg" in caplog.text
    assert "trialling 2 of 2" in caplog.text and "https://x.test/mcp" in caplog.text
