"""Subprocess execution with dry-run support, timeouts and secret hygiene.

Rules enforced here:

* Secrets are **never** placed on a command line (they would be visible in
  ``ps`` output and in shell history).  Use ``secret_env=`` or ``stdin_secret=``.
* Every logged command line and every captured stream is redacted.
* ``dry_run`` suppresses *mutating* commands only; read-only probes still run so
  that a dry run can still verify the machine's real state.
* A timeout is an upper bound on the *wait*, not merely on the child.  Nothing
  here waits on a process or a pipe without a deadline; see :meth:`Runner._stop`.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import time
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Mapping, Protocol, Sequence

from . import redact
from .errors import CommandError
from .logging_setup import get_logger

log = get_logger("proc")


class DisplayRewriter(Protocol):
    """Changes what a proxied child shows on screen, never what it is sent.

    ``feed`` takes text as the child wrote it and returns what to paint now; it
    may hold a tail back (a URL still arriving) until more comes. ``flush``
    returns whatever is held, and is called when the child goes quiet and when
    it ends, so nothing it wrote is ever swallowed.
    """

    def feed(self, text: str) -> str: ...

    def flush(self) -> str: ...


@dataclass
class CommandResult:
    argv: list[str]
    returncode: int
    stdout: str = ""
    stderr: str = ""
    duration: float = 0.0
    simulated: bool = False
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out

    @property
    def output(self) -> str:
        """stdout, falling back to stderr for tools that log to the wrong stream."""
        return (self.stdout or self.stderr).strip()

    def first_line(self) -> str:
        return self.output.splitlines()[0].strip() if self.output else ""

    def to_dict(self, *, include_output: bool = True) -> dict[str, object]:
        payload: dict[str, object] = {
            "argv": redact.redact_argv(self.argv),
            "returncode": self.returncode,
            "duration_seconds": round(self.duration, 3),
            "simulated": self.simulated,
            "timed_out": self.timed_out,
        }
        if include_output:
            payload["stdout"] = redact.redact(self.stdout)[-4000:]
            payload["stderr"] = redact.redact(self.stderr)[-4000:]
        return payload


@dataclass
class Runner:
    """Executes commands on behalf of the phases."""

    dry_run: bool = False
    default_timeout: float = 300.0
    #: Extra directories prepended to PATH for lookups (populated by phase 1).
    extra_paths: list[str] = field(default_factory=list)
    #: Base environment overlay applied to every child process.
    base_env: dict[str, str] = field(default_factory=dict)
    #: Signals sent, in order, to a child that outran its timeout. Names, with
    #: or without the ``SIG`` prefix, or numbers; the last one should be one the
    #: child cannot catch.
    stop_signals: list[str] = field(default_factory=lambda: ["TERM", "KILL"])
    #: Seconds given to the child after each of those signals before the next
    #: one is sent -- and after the last of them before the child is abandoned.
    stop_grace_seconds: float = 5.0
    #: Wordings of a full disk in a failed command's output (``run.disk.out_of_space_markers``).
    out_of_space_markers: list[str] = field(
        default_factory=lambda: ["No space left on device", "ENOSPC", "Disk quota exceeded", "EDQUOT"]
    )
    #: Set when a command failed for want of disk space: the first such output, for the run to stop on.
    out_of_space: str = ""

    # -- discovery -------------------------------------------------------------

    def search_path(self) -> str:
        parts = [p for p in self.extra_paths if p]
        parts.extend(p for p in (self.base_env.get("PATH") or os.environ.get("PATH", "")).split(os.pathsep) if p)
        seen: set[str] = set()
        ordered = [p for p in parts if not (p in seen or seen.add(p))]
        return os.pathsep.join(ordered)

    def which(self, name: str, extra_dirs: Iterable[str] = ()) -> str | None:
        """Locate ``name``, honouring extra search directories."""
        if os.path.sep in name:
            candidate = Path(name).expanduser()
            return str(candidate) if candidate.is_file() and os.access(candidate, os.X_OK) else None
        path = self.search_path()
        extra = [str(Path(d).expanduser()) for d in extra_dirs if d]
        if extra:
            path = os.pathsep.join(extra + [path])
        return shutil.which(name, path=path)

    # -- execution -------------------------------------------------------------

    def build_env(
        self,
        extra: Mapping[str, str | None] | None = None,
        secret_env: Mapping[str, "object"] | None = None,
    ) -> dict[str, str]:
        """The environment a child starts with. ``None`` in ``extra`` removes a name."""
        env = dict(os.environ)
        env.update(self.base_env)
        env["PATH"] = self.search_path()
        for key, value in (extra or {}).items():
            if value is None:
                env.pop(key, None)
            else:
                env[key] = str(value)
        if secret_env:
            for key, value in secret_env.items():
                env[key] = value.reveal() if hasattr(value, "reveal") else str(value)
        return env

    def run(
        self,
        argv: Sequence[str],
        *,
        check: bool = False,
        timeout: float | None = None,
        cwd: str | os.PathLike[str] | None = None,
        env: Mapping[str, str | None] | None = None,
        secret_env: Mapping[str, object] | None = None,
        stdin_text: str | None = None,
        stdin_secret: object | None = None,
        mutating: bool = True,
        capture: bool = True,
        discard_output: bool = False,
        simulate_stdout: str = "",
        label: str | None = None,
    ) -> CommandResult:
        """Run ``argv`` and return a :class:`CommandResult`.

        ``mutating=False`` marks a read-only probe: it executes even in dry-run
        mode, because verification must reflect reality.

        ``capture=False`` hands the child this process's own streams, so it can
        talk to the operator directly. ``discard_output=True`` goes further and
        sends them to ``/dev/null``, which is the right way to run a tool that
        daemonises itself: a forked grandchild inherits the pipes, so with
        ``capture=True`` nothing is read until the grandchild closes them and
        the command sits there for its whole timeout however quickly the child
        itself finished. Clipboard tools all do this.
        """
        argv = [str(a) for a in argv]
        shown = " ".join(redact.redact_argv(argv))
        display = f"{label}: {shown}" if label else shown

        if self.dry_run and mutating:
            log.info("[dry-run] would run: %s", display)
            return CommandResult(argv=argv, returncode=0, stdout=simulate_stdout, simulated=True)

        log.debug("run: %s", display)
        effective_timeout = self.default_timeout if timeout is None else timeout
        payload = stdin_secret.reveal() if hasattr(stdin_secret, "reveal") else stdin_text
        started = time.monotonic()
        streams: dict[str, object] = {"stdout": subprocess.PIPE, "stderr": subprocess.PIPE} if capture else {}
        if not capture and discard_output:
            streams = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
        try:
            proc = subprocess.Popen(  # noqa: S603 - argv is a list, never shell
                argv,
                cwd=str(cwd) if cwd else None,
                env=self.build_env(env, secret_env),
                stdin=subprocess.PIPE if payload is not None else None,
                **streams,
                text=True,
                start_new_session=self._detachable(capture=capture, discard=discard_output),
            )
        except FileNotFoundError as exc:
            result = CommandResult(argv=argv, returncode=127, stderr=str(exc), duration=time.monotonic() - started)
        except PermissionError as exc:
            result = CommandResult(argv=argv, returncode=126, stderr=str(exc), duration=time.monotonic() - started)
        else:
            stdout, stderr, timed_out = self._wait(proc, payload, effective_timeout, display=display)
            result = CommandResult(
                argv=argv,
                returncode=124 if timed_out or proc.returncode is None else proc.returncode,
                stdout=stdout,
                stderr=stderr or (f"timed out after {effective_timeout}s" if timed_out else ""),
                duration=time.monotonic() - started,
                timed_out=timed_out,
            )

        if not result.ok:
            log.debug(
                "command failed rc=%s: %s\n%s",
                result.returncode,
                display,
                redact.redact(result.stderr)[-2000:],
            )
            said = f"{result.stdout}\n{result.stderr}"
            marker = next((m for m in self.out_of_space_markers if m and m.lower() in said.lower()), "")
            if marker and not self.out_of_space:
                # A full disk: every later step would fail too, in ways that look
                # like something else. Recorded once, for the run to stop on.
                self.out_of_space = f"{display}: {marker}"
        if check and not result.ok:
            raise CommandError(
                f"command failed (rc={result.returncode}): {display}",
                argv=argv,
                returncode=result.returncode,
                stderr=redact.redact(result.stderr)[-2000:],
            )
        return result

    def launch(self, argv: Sequence[str], *, settle: float = 0.0, label: str | None = None) -> CommandResult:
        """Start ``argv`` and leave it running: a browser, not a command.

        :meth:`run` waits for the child, which is right for everything except a
        program the operator is meant to use while this one carries on -- a
        sign-in page, opened while the sign-in command waits for its code. So
        the child gets a session of its own and no stream of ours: nothing it
        does can hold a pipe open, repaint the terminal, or receive the Ctrl-C
        meant for the sign-in.

        ``settle`` seconds are spent watching it, because a browser that cannot
        reach a display dies at once, and "opened" must not be reported for a
        window that never appeared. One still running afterwards, or one that
        handed the page to an instance already open and exited cleanly, counts
        as opened.
        """
        argv = [str(a) for a in argv]
        shown = " ".join(redact.redact_argv(argv))
        display = f"{label}: {shown}" if label else shown
        if self.dry_run:
            log.info("[dry-run] would launch: %s", display)
            return CommandResult(argv=argv, returncode=0, simulated=True)
        log.debug("launch: %s", display)
        started = time.monotonic()
        try:
            proc = subprocess.Popen(  # noqa: S603 - argv is a list, never shell
                argv,
                env=self.build_env(),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except FileNotFoundError as exc:
            return CommandResult(argv=argv, returncode=127, stderr=str(exc))
        except PermissionError as exc:
            return CommandResult(argv=argv, returncode=126, stderr=str(exc))
        code: int | None = None
        if settle > 0:
            with suppress(subprocess.TimeoutExpired):
                code = proc.wait(timeout=settle)
        result = CommandResult(argv=argv, returncode=code or 0, duration=time.monotonic() - started)
        if not result.ok:
            log.debug("launch failed rc=%s: %s", result.returncode, display)
        return result

    def probe(self, argv: Sequence[str], *, timeout: float | None = None, **kwargs) -> CommandResult:
        """Read-only convenience wrapper (always executes, never raises).

        Every caller names its timeout from configuration; one that does not
        gets ``default_timeout`` (``run.command_timeout_seconds``) like
        :meth:`run`, rather than a figure of its own.
        """
        kwargs.setdefault("mutating", False)
        kwargs.setdefault("check", False)
        return self.run(argv, timeout=timeout, **kwargs)

    # -- ending a child that will not end itself ---------------------------------

    @staticmethod
    def _detachable(*, capture: bool, discard: bool) -> bool:
        """May the child be started in a session of its own?

        A session of its own is what makes a child's *descendants* reachable:
        they are all in one process group, so one ``killpg`` ends the lot, and
        a grandchild cannot outlive the timeout still holding the pipes.

        A child that is talking to the operator through this process's own
        terminal must stay where it is, though. Outside our session it is no
        longer in the terminal's foreground process group, and Ctrl-C would
        stop reaching it -- the operator would be left with a prompt they can
        answer but not abandon.
        """
        return os.name == "posix" and (capture or discard)

    def stop_ladder(self) -> list[int]:
        """The signals to send, in order, to a child that outran its timeout."""
        ladder: list[int] = []
        for name in self.stop_signals or ():
            wanted = str(name).strip().upper()
            if not wanted:
                continue
            if wanted.lstrip("-").isdigit():
                ladder.append(abs(int(wanted)))
                continue
            found = getattr(signal, wanted if wanted.startswith("SIG") else f"SIG{wanted}", None)
            if found is None:
                log.debug("unknown stop signal %r, ignoring it", name)
                continue
            ladder.append(int(found))
        # An empty or wholly unrecognised ladder would leave nothing to end the
        # child with, which is the one outcome this code exists to prevent.
        return ladder or [int(getattr(signal, "SIGKILL", signal.SIGTERM))]

    @staticmethod
    def _signal(proc: subprocess.Popen, sig: int) -> None:
        """Signal the child's whole process group, or the child if it shares ours."""
        with suppress(OSError, ValueError):
            if os.name == "posix":
                group = os.getpgid(proc.pid)
                if group != os.getpgid(0):
                    os.killpg(group, sig)
                    return
            proc.send_signal(sig)

    def _wait(
        self,
        proc: subprocess.Popen,
        payload: str | None,
        timeout: float,
        *,
        display: str,
    ) -> tuple[str, str, bool]:
        """Wait for ``proc``; return ``(stdout, stderr, timed_out)``."""
        limit = timeout if timeout and timeout > 0 else None
        try:
            stdout, stderr = proc.communicate(payload, timeout=limit)
        except subprocess.TimeoutExpired:
            log.warning("timed out after %ss, stopping: %s", limit, display)
            stdout, stderr = self._stop(proc, display=display)
            return stdout, stderr, True
        except BaseException:  # Ctrl-C, or anything else that cuts the wait short
            self._stop(proc, display=display)
            raise
        return stdout or "", stderr or "", False

    def _stop(self, proc: subprocess.Popen, *, display: str) -> tuple[str, str]:
        """Signal ``proc`` until it ends, waiting a bounded time for each signal.

        This is the difference between a timeout and a promise. CPython's own
        ``subprocess.run`` kills the child when the timeout fires and then waits
        on the pipes *without a deadline*; a grandchild that inherited them can
        hold that wait open indefinitely, and one 900-second limit here once
        cost a run ten and a half hours. So every wait below has an end: each
        signal in :attr:`stop_signals` gets :attr:`stop_grace_seconds`, and if
        the last of them goes unanswered the pipes are dropped and the run moves
        on without the child.
        """
        grace = max(self.stop_grace_seconds, 0.0)
        for sig in self.stop_ladder():
            self._signal(proc, sig)
            try:
                stdout, stderr = proc.communicate(timeout=grace)
            except subprocess.TimeoutExpired:
                continue
            except (OSError, ValueError):  # pragma: no cover - pipes already gone
                break
            return stdout or "", stderr or ""
        log.error("a child outlived every stop signal and was abandoned: %s", display)
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            if stream is not None:
                with suppress(OSError, ValueError):
                    stream.close()
        return "", ""

    def _reap(self, proc: subprocess.Popen, *, display: str) -> int | None:
        """Collect a child that should already have finished; None if it never does."""
        try:
            return proc.wait(timeout=max(self.stop_grace_seconds, 0.0))
        except subprocess.TimeoutExpired:
            log.debug("child outlived its terminal, stopping it: %s", display)
            return self._stop_child(proc, display=display)

    def _stop_child(self, proc: subprocess.Popen, *, display: str) -> int | None:
        """:meth:`_stop` for a child we hold no pipes to; None if it never ends."""
        grace = max(self.stop_grace_seconds, 0.0)
        for sig in self.stop_ladder():
            self._signal(proc, sig)
            try:
                return proc.wait(timeout=grace)
            except subprocess.TimeoutExpired:
                continue
        log.error("a child outlived every stop signal and was abandoned: %s", display)
        return None

    # -- interactive execution -------------------------------------------------

    def tty_available(self) -> bool:
        """Can we sit between the operator and a child process at all?"""
        if os.name != "posix":
            return False
        try:
            return sys.stdin.isatty() and sys.stdout.isatty()
        except (AttributeError, ValueError):  # closed or replaced streams
            return False

    def run_tty(
        self,
        argv: Sequence[str],
        *,
        timeout: float | None = None,
        env: Mapping[str, str | None] | None = None,
        columns: int = 0,
        rows: int = 0,
        on_output: Callable[[str], None] | None = None,
        screen: DisplayRewriter | None = None,
        label: str | None = None,
    ) -> CommandResult:
        """Run an interactive command on a pseudo-terminal we hold the other end of.

        Handing the child our own terminal (``run(capture=False)``) is simpler
        and is still the fallback, but it makes this process blind: whatever the
        child prints goes straight to the screen and nowhere else. For a sign-in
        that is the one thing we cannot afford, because the URL on that screen is
        several hundred characters long, the child wraps it to the terminal width
        before printing, and a wrapped URL cannot be selected back out in one
        piece. Sitting in the middle costs a pty and buys the text.

        The bytes are forwarded in both directions unaltered -- the child sees a
        real terminal of the real size and draws exactly what it would have
        drawn -- and a copy of everything it writes is handed to ``on_output``.
        Nothing typed *into* the child is inspected or kept: the verification
        code the operator pastes back is the one thing in this exchange that is
        actually sensitive.

        ``screen``, when given, is the one exception to "unaltered": it sees
        the child's output before the screen does and may change what is
        painted (``on_output`` still gets the original). Without a terminal to
        proxy there is nothing to rewrite, and the child's own output is shown.
        """
        argv = [str(a) for a in argv]
        display = f"{label}: {' '.join(redact.redact_argv(argv))}" if label else " ".join(redact.redact_argv(argv))
        if self.dry_run:
            log.info("[dry-run] would run: %s", display)
            return CommandResult(argv=argv, returncode=0, simulated=True)
        if not self.tty_available():
            log.debug("no terminal to proxy, running %s directly", display)
            return self.run(argv, timeout=timeout, env=env, capture=False, label=label)

        import fcntl
        import pty
        import struct
        import termios
        import tty as tty_mod

        log.debug("run (pty): %s", display)
        started = time.monotonic()
        deadline = None
        effective = self.default_timeout if timeout is None else timeout
        if effective and effective > 0:
            deadline = started + effective

        master, slave = pty.openpty()
        size = _window_size(sys.stdout.fileno(), columns=columns, rows=rows)
        try:
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", *size, 0, 0))
        except OSError:  # pragma: no cover - a pty that will not be sized still works
            log.debug("could not size the pty; the child will assume a default")

        def _take_the_terminal() -> None:  # pragma: no cover - runs in the child
            # setsid alone is not enough: without a controlling terminal the line
            # discipline has no foreground process group to send SIGINT to, and
            # Ctrl-C during a sign-in would do nothing at all.
            os.setsid()
            fcntl.ioctl(0, termios.TIOCSCTTY, 0)

        try:
            proc = subprocess.Popen(  # noqa: S603 - argv is a list, never shell
                argv,
                stdin=slave,
                stdout=slave,
                stderr=slave,
                env=self.build_env(env),
                preexec_fn=_take_the_terminal,  # noqa: PLW1509 - this process is single-threaded
                close_fds=True,
            )
        except OSError as exc:
            os.close(master)
            os.close(slave)
            code = 127 if isinstance(exc, FileNotFoundError) else 126
            return CommandResult(argv=argv, returncode=code, stderr=str(exc), duration=time.monotonic() - started)
        os.close(slave)

        try:
            saved = termios.tcgetattr(sys.stdin.fileno())
        except termios.error:  # pragma: no cover - a terminal lost between checks
            saved = None
        timed_out = False
        try:
            if saved is not None:
                # Raw, so that every keystroke reaches the child's own line
                # discipline rather than being cooked twice. TCSADRAIN rather
                # than setraw's default TCSAFLUSH, which would throw away
                # anything already typed -- including a code pasted a moment too
                # early, and anything a wrapper piped in before this process was
                # even started.
                tty_mod.setraw(sys.stdin.fileno(), termios.TCSADRAIN)
            timed_out = _pump(master, deadline=deadline, on_output=on_output, screen=screen)
        finally:
            # The terminal may be the thing that went away -- a closed window is
            # one of the ways a sign-in ends. Restoring it is then both
            # impossible and pointless, and must not bury the result.
            if saved is not None:
                with suppress(termios.error, OSError):
                    termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, saved)
            os.close(master)

        if timed_out:
            log.warning("interactive command timed out after %ss, stopping: %s", effective, display)
            returncode = self._stop_child(proc, display=display)
        else:
            # The pty going quiet usually means the child exited, but a child
            # that forked and left is quiet too -- so this wait has a deadline
            # like every other one, and ends the child if it passes.
            returncode = self._reap(proc, display=display)
        abandoned = returncode is None
        result = CommandResult(
            argv=argv,
            # A proxied stream is not kept: it is the operator's screen, it may
            # contain whatever they typed, and nothing downstream needs it.
            returncode=124 if timed_out or abandoned else returncode,
            stderr=(
                f"timed out after {effective}s"
                if timed_out
                else "did not exit when its terminal closed"
                if abandoned
                else ""
            ),
            duration=time.monotonic() - started,
            timed_out=timed_out,
        )
        if not result.ok:
            log.debug("interactive command failed rc=%s: %s", result.returncode, display)
        return result


def _window_size(fd: int, *, columns: int = 0, rows: int = 0) -> tuple[int, int]:
    """``(rows, columns)`` for the child's terminal; 0 means "same as ours"."""
    import fcntl
    import struct
    import termios

    current_rows, current_columns = 24, 80
    try:
        packed = fcntl.ioctl(fd, termios.TIOCGWINSZ, b"\0" * 8)
        current_rows, current_columns = struct.unpack("HHHH", packed)[:2]
    except OSError:  # pragma: no cover - not every stdout answers this
        pass
    return (rows or current_rows or 24, columns or current_columns or 80)


def _pump(
    master: int,
    *,
    deadline: float | None,
    on_output: Callable[[str], None] | None,
    screen: DisplayRewriter | None = None,
) -> bool:
    """Shuttle bytes between this terminal and ``master`` until the child ends.

    Returns True if the deadline passed first. The child's output is copied to
    ``on_output`` as well as to the screen -- ``on_output`` first, so that a
    ``screen`` rewriter consulting what it learned sees this chunk too. What
    arrives on stdin is forwarded and not looked at.
    """
    import codecs
    import select

    stdin = sys.stdin.fileno()
    stdout = sys.stdout.fileno()
    watching = [stdin, master]
    state = {"showing": True}
    # A multi-byte character can straddle two reads; decoding each read on its
    # own would paint replacement characters where the rewriter sees text.
    decoder = codecs.getincrementaldecoder("utf-8")("replace")

    def paint(data: bytes) -> None:
        if not data or not state["showing"]:
            return
        try:
            os.write(stdout, data)
        except OSError:
            # The screen went away, not the child. Stop painting and let it
            # finish; what it says still reaches the caller, and killing a
            # sign-in over a closed window would be a poor trade.
            state["showing"] = False

    def release() -> None:
        if screen is not None:
            paint(screen.flush().encode("utf-8"))

    while True:
        if deadline is not None and time.monotonic() >= deadline:
            release()
            return True
        wait = 0.5 if deadline is None else max(0.0, min(0.5, deadline - time.monotonic()))
        try:
            readable, _w, _x = select.select(watching, [], [], wait)
        except InterruptedError:  # pragma: no cover - a resize or a stopped job
            continue
        if not readable:
            # Quiet: whatever the rewriter is holding is all there is for now.
            release()
            continue
        if master in readable:
            try:
                chunk = os.read(master, 65536)
            except OSError:  # the child closed its side
                release()
                return False
            if not chunk:
                release()
                return False
            if on_output is not None:
                on_output(chunk.decode("utf-8", "replace"))
            if screen is None:
                paint(chunk)
            else:
                paint(screen.feed(decoder.decode(chunk)).encode("utf-8"))
        if stdin in readable:
            try:
                typed = os.read(stdin, 65536)
            except OSError:
                typed = b""
            if typed:
                os.write(master, typed)
                continue
            # Our end of the terminal is gone -- a closed window, a wrapper
            # whose input ran out. The child must not be cut off mid-sign-in
            # for it: stop listening, tell it end-of-input the way a terminal
            # does, and keep showing what it writes until it finishes on its
            # own or the deadline arrives.
            watching.remove(stdin)
            try:
                os.write(master, _end_of_input(master))
            except OSError:  # pragma: no cover - the child is already gone
                pass


def _end_of_input(fd: int) -> bytes:
    """The terminal's own end-of-input character, ``^D`` where it cannot be read."""
    import termios

    try:
        return bytes(termios.tcgetattr(fd)[6][termios.VEOF])
    except (OSError, IndexError, TypeError):  # pragma: no cover - an unusual pty
        return b"\x04"
