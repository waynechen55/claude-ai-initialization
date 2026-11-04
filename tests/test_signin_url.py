"""Recovering the sign-in URL from a terminal that wrapped it.

The two providers break the URL in two different ways, so the fixtures here are
samples of what each of them really writes -- a hard-wrapped OSC 8 hyperlink for
``claude setup-token``, an unbroken line for ``gcloud``. What is asserted is that
the whole URL comes back out of both, and that it is put somewhere the operator
can reach without selecting anything off the screen.
"""

from __future__ import annotations

import os
import stat
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

from initialize_claude_ai.authenticate.signin_url import SignInUrlRelay
from initialize_claude_ai.core import fileio
from initialize_claude_ai.core.config import load_settings
from initialize_claude_ai.core.context import RunContext
from initialize_claude_ai.core.proc import CommandResult, Runner

CLAUDE_URL = (
    "https://claude.ai/oauth/authorize?code=true&client_id=9d1c250a-e61b-44d9-88ed-5944d1962f5e"
    "&response_type=code&redirect_uri=https%3A%2F%2Fconsole.anthropic.com%2Foauth%2Fcode%2F"
    "callback&scope=org%3Acreate_api_key+user%3Aprofile+user%3Ainference&code_challenge="
    "F9tMNJ1mXQ3jGf7pQ0rLbYc2Vd8sKhAe4WzUxOiTnRg&code_challenge_method=S256&state=Zb1k"
)
GCLOUD_URL = (
    "https://accounts.google.com/o/oauth2/auth?response_type=code&client_id=32555940559.apps."
    "googleusercontent.com&redirect_uri=https%3A%2F%2Fsdk.cloud.google.com%2Fauthcode.html"
    "&scope=openid+https%3A%2F%2Fwww.googleapis.com%2Fauth%2Fuserinfo.email&state=8Qb2Lm"
    "&access_type=offline&code_challenge_method=S256"
)


def hard_wrapped(url: str, width: int = 60) -> str:
    """What ``claude setup-token`` writes: real newlines, and an OSC 8 per row.

    The layout engine breaks the *text* to the terminal width before printing,
    so no terminal can hand the URL back in one piece -- but each fragment is
    still wrapped in a hyperlink escape carrying the whole target, which is what
    makes recovery possible at all.
    """
    rows = [url[i : i + width] for i in range(0, len(url), width)]
    body = "\r\n".join(f"\x1b]8;;{url}\x1b\\\x1b[4m{row}\x1b[0m\x1b]8;;\x1b\\" for row in rows)
    return f"\x1b[2mBrowser didn't open? Use the url below to sign in (c to copy)\x1b[0m\r\n{body}\r\n"


def soft_wrapped(url: str) -> str:
    """What ``gcloud --no-launch-browser`` writes: one line, no escapes."""
    return f"Go to the following link in your browser:\n\n    {url}\n\nEnter authorization code: "


@pytest.fixture(autouse=True)
def graphical_session(monkeypatch: pytest.MonkeyPatch) -> None:
    """A session every clipboard tool can reach, whatever the host running the tests has."""
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-test")
    monkeypatch.setenv("DISPLAY", ":test")


@pytest.fixture
def relay(ctx: RunContext) -> SignInUrlRelay:
    return SignInUrlRelay(ctx)


def tune(ctx: RunContext, **overrides: str) -> None:
    """Reload settings with ``auth.signin`` keys set, as a flag or env var would."""
    ctx.settings = load_settings(
        overrides=[f"auth.signin.{k}={v}" for k, v in overrides.items()],
        environ=dict(os.environ),
    )


# -- reading the URL back out --------------------------------------------------


def test_a_hard_wrapped_url_comes_back_whole(relay) -> None:
    """The case nothing else can fix: the newlines are really in the stream."""
    relay.feed(hard_wrapped(CLAUDE_URL))
    assert relay.seen == [CLAUDE_URL]


def test_an_unwrapped_url_is_read_as_plain_text(relay) -> None:
    """gcloud emits no hyperlink, so the text itself has to be searched."""
    relay.feed(soft_wrapped(GCLOUD_URL))
    assert relay.seen == [GCLOUD_URL]


def test_a_url_split_across_two_reads_is_still_found(relay) -> None:
    """A pty hands over whatever happens to be in the buffer, not whole lines."""
    text = soft_wrapped(GCLOUD_URL)
    cut = text.index(GCLOUD_URL) + 30
    relay.feed(text[:cut])
    relay.feed(text[cut:])
    assert relay.seen == [GCLOUD_URL]


def test_the_same_url_printed_many_times_is_delivered_once(relay) -> None:
    """Every row of a hard-wrapped block repeats it, and a redraw repeats it again."""
    relay.feed(hard_wrapped(CLAUDE_URL))
    relay.feed(hard_wrapped(CLAUDE_URL))
    assert relay.seen == [CLAUDE_URL]


def test_two_different_urls_are_both_kept(relay) -> None:
    """gcloud signs in twice: the user account, then application-default."""
    second = GCLOUD_URL.replace("state=8Qb2Lm", "state=T4vNp0")
    relay.feed(soft_wrapped(GCLOUD_URL))
    relay.feed(soft_wrapped(second))
    assert relay.seen == [GCLOUD_URL, second]


@pytest.mark.parametrize("width", [50, 60, 72, 100, 120, 200])
def test_the_first_row_of_a_wrapped_url_is_never_mistaken_for_the_url(ctx, width) -> None:
    """The row a URL wraps at is long enough to look like a whole one.

    Which makes it the most dangerous thing in the stream: a prefix of an
    authorization URL is still a valid-looking authorization URL, and the page
    it opens takes an email address and then fails silently. Delivering that
    instead of -- or after -- the real one would reproduce the exact bug this
    class exists to remove, so every plausible terminal width is checked.
    """
    relay = SignInUrlRelay(ctx)
    relay.feed(hard_wrapped(CLAUDE_URL, width=width))
    assert relay.seen == [CLAUDE_URL]
    assert relay.url_file.read_text(encoding="utf-8").strip() == CLAUDE_URL


def test_a_fragment_seen_first_is_replaced_by_the_whole_url(relay) -> None:
    """A read can end mid-URL, and the rest arrives in the next one."""
    relay.feed(f"  {CLAUDE_URL[:120]}")
    relay.feed(f"{CLAUDE_URL[120:]}\r\n")
    assert relay.seen == [CLAUDE_URL]
    assert relay.url_file.read_text(encoding="utf-8").strip() == CLAUDE_URL


def test_ordinary_short_links_in_the_output_are_ignored(relay) -> None:
    """Both CLIs print documentation links; none of them is the sign-in URL."""
    relay.feed("See https://docs.claude.com/ for help.\r\nPrivacy: https://claude.ai/legal\r\n")
    assert relay.seen == []


def test_a_broken_url_pattern_does_not_take_the_sign_in_down(ctx, caplog) -> None:
    """A bad regex in configuration is a mistake, not a reason to fail a sign-in."""
    tune(ctx, url_pattern="https://(")
    relay = SignInUrlRelay(ctx)
    relay.feed(soft_wrapped(GCLOUD_URL))
    assert relay.seen == [GCLOUD_URL]
    assert "not a valid regex" in caplog.text


# -- putting it within reach ----------------------------------------------------


def test_the_url_is_written_where_another_terminal_can_read_it(relay) -> None:
    relay.feed(soft_wrapped(GCLOUD_URL))
    assert relay.url_file.read_text(encoding="utf-8").strip() == GCLOUD_URL


def test_the_file_is_private_to_its_owner(relay) -> None:
    """Short-lived, but still nobody else's business on a shared machine."""
    relay.feed(soft_wrapped(GCLOUD_URL))
    assert stat.S_IMODE(relay.url_file.stat().st_mode) == 0o600


def test_the_file_is_removed_when_the_sign_in_is_over(relay) -> None:
    """A stale authorization link is a thing somebody finds later and tries."""
    relay.feed(soft_wrapped(GCLOUD_URL))
    relay.cleanup()
    assert not relay.url_file.exists()
    relay.cleanup()  # and again, on a run that never printed one


def test_the_clipboard_tool_is_fed_on_stdin(ctx, monkeypatch) -> None:
    """The URL goes in by stdin, never as an argument where ps would show it."""
    monkeypatch.setattr(ctx.runner, "which", lambda name, *a, **k: f"/usr/bin/{name}")
    calls: list[tuple[list[str], dict]] = []

    def record(argv, **kwargs) -> CommandResult:
        calls.append(([str(a) for a in argv], kwargs))
        return CommandResult(argv=list(argv), returncode=0)

    monkeypatch.setattr(ctx.runner, "run", record)
    SignInUrlRelay(ctx).feed(soft_wrapped(GCLOUD_URL))
    argv, kwargs = calls[0]
    assert len(calls) == 1 and argv == ["/usr/bin/wl-copy"]
    assert kwargs["stdin_text"] == GCLOUD_URL


def test_the_clipboard_tool_is_given_no_pipe_to_hold(ctx, monkeypatch) -> None:
    """A clipboard tool forks and stays alive owning the selection.

    Captured, it would hold the pipe open for the lifetime of the copy and this
    call would never return -- not even on its timeout, which kills the process
    it started and then waits again on pipes the surviving grandchild holds.
    """
    monkeypatch.setattr(ctx.runner, "which", lambda name, *a, **k: f"/usr/bin/{name}")
    seen: list[dict] = []
    monkeypatch.setattr(
        ctx.runner,
        "run",
        lambda argv, **kw: (seen.append(kw), CommandResult(argv=list(argv), returncode=0))[1],
    )
    SignInUrlRelay(ctx).feed(soft_wrapped(GCLOUD_URL))
    assert seen[0]["capture"] is False and seen[0]["discard_output"] is True


def test_a_tool_that_daemonises_does_not_hold_the_sign_in_up(ctx, sandbox: Path, monkeypatch) -> None:
    """The guard above, proved against a tool that really does fork.

    Not the host's own clipboard -- a test has no business replacing what the
    developer had copied -- but a stand-in that daemonises the same way, run for
    real. Before the fix this call never came back at all.
    """
    tool = sandbox / "forking-copy"
    tool.write_text("#!/bin/sh\ncat >/dev/null\nsleep 30 &\nexit 0\n", encoding="utf-8")
    tool.chmod(tool.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setattr(ctx.runner, "which", lambda name, *a, **k: str(tool) if name == "wl-copy" else None)
    ctx.runner.dry_run = False  # the point is to really start it

    relay = SignInUrlRelay(ctx)
    started = time.monotonic()
    relay.feed(soft_wrapped(GCLOUD_URL))
    assert time.monotonic() - started < 5.0
    assert any("wl-copy" in note for note in relay.delivered)


def test_the_next_clipboard_tool_is_tried_when_the_first_is_absent(ctx, monkeypatch) -> None:
    """Wayland and X11 want different tools; whichever is here is the right one."""
    monkeypatch.setattr(ctx.runner, "which", lambda name, *a, **k: "/usr/bin/xclip" if name == "xclip" else None)
    seen: list[list[str]] = []
    monkeypatch.setattr(
        ctx.runner,
        "run",
        lambda argv, **kw: (seen.append([str(a) for a in argv]), CommandResult(argv=[], returncode=0))[1],
    )
    relay = SignInUrlRelay(ctx)
    relay.feed(soft_wrapped(GCLOUD_URL))
    assert seen == [["/usr/bin/xclip", "-selection", "clipboard"]]
    assert any("xclip" in note for note in relay.delivered)


def test_an_installed_tool_without_its_session_is_not_tried(ctx, monkeypatch) -> None:
    """The reported log: wl-copy installed on an X display, failing every time.

    It was tried, failed, and the announcement still named it -- a clipboard the
    URL never reached. A tool whose session variable is unset is skipped.
    """
    monkeypatch.delenv("WAYLAND_DISPLAY")
    monkeypatch.setattr(ctx.runner, "which", lambda name, *a, **k: f"/usr/bin/{name}")
    seen: list[list[str]] = []
    monkeypatch.setattr(
        ctx.runner,
        "run",
        lambda argv, **kw: (seen.append([str(a) for a in argv]), CommandResult(argv=[], returncode=0))[1],
    )
    relay = SignInUrlRelay(ctx)
    relay.feed(soft_wrapped(GCLOUD_URL))
    assert seen == [["/usr/bin/xclip", "-selection", "clipboard"]]
    assert "copied to the clipboard (xclip on :test)" in relay.delivered


def test_the_announcement_names_the_clipboard_actually_reached(ctx, monkeypatch) -> None:
    """Over ssh the display's clipboard may not be the operator's own; say which it is."""
    monkeypatch.delenv("WAYLAND_DISPLAY")
    monkeypatch.setattr(ctx.runner, "tty_available", lambda: True)
    monkeypatch.setattr(ctx.runner, "which", lambda name, *a, **k: f"/usr/bin/{name}")
    said = " ".join(SignInUrlRelay(ctx).announce())
    assert "xclip, display :test" in said and "wl-copy" not in said


def test_a_tool_that_needs_no_session_is_always_eligible(ctx, monkeypatch) -> None:
    monkeypatch.delenv("WAYLAND_DISPLAY")
    monkeypatch.delenv("DISPLAY")
    monkeypatch.setattr(
        ctx.runner, "which", lambda name, *a, **k: "/usr/bin/pbcopy" if name == "pbcopy" else f"/usr/bin/{name}"
    )
    assert [name for name, *_ in SignInUrlRelay(ctx).clipboards()] == ["pbcopy", "clip.exe"]


def test_installed_but_unreachable_is_not_called_missing(ctx, monkeypatch) -> None:
    monkeypatch.delenv("WAYLAND_DISPLAY")
    monkeypatch.delenv("DISPLAY")
    monkeypatch.setattr(ctx.runner, "tty_available", lambda: True)
    monkeypatch.setattr(ctx.runner, "which", lambda name, *a, **k: "/usr/bin/xclip" if name == "xclip" else None)
    said = " ".join(SignInUrlRelay(ctx).announce())
    assert "No clipboard is reachable" in said and "DISPLAY" in said
    assert "--install-only clipboard" not in said


def test_each_clipboard_names_the_session_it_needs(settings) -> None:
    needs = settings.dict_("auth.signin.clipboard_needs")
    assert set(needs) <= set(settings.dict_("auth.signin.clipboard"))
    assert needs["wl-copy"] == ["WAYLAND_DISPLAY"]


def test_no_clipboard_tool_is_not_a_failure(ctx, monkeypatch) -> None:
    """The file is the guarantee; the clipboard is the convenience."""
    monkeypatch.setattr(ctx.runner, "which", lambda name, *a, **k: None)
    relay = SignInUrlRelay(ctx)
    relay.feed(soft_wrapped(GCLOUD_URL))
    assert relay.url_file.exists()
    assert relay.delivered == [f"written to {relay.url_file}"]


def test_an_unwritable_state_directory_does_not_stop_the_sign_in(ctx, monkeypatch) -> None:
    """Losing the convenience must never cost the credential."""
    monkeypatch.setattr(ctx.runner, "which", lambda name, *a, **k: None)
    relay = SignInUrlRelay(ctx)
    monkeypatch.setattr(fileio.tempfile, "mkstemp", lambda *a, **k: (_ for _ in ()).throw(OSError("nope")))
    relay.feed(soft_wrapped(GCLOUD_URL))
    assert relay.seen == [GCLOUD_URL]
    assert relay.delivered == []


# -- deciding whether to watch at all -------------------------------------------


def test_watching_is_off_when_there_is_no_terminal_to_sit_in(ctx, monkeypatch) -> None:
    """Under CI or a pipe there is no screen to rescue anything from."""
    monkeypatch.setattr(ctx.runner, "tty_available", lambda: False)
    assert SignInUrlRelay(ctx).enabled() is False


def test_watching_is_on_when_a_terminal_is_attached(ctx, monkeypatch) -> None:
    monkeypatch.setattr(ctx.runner, "tty_available", lambda: True)
    assert SignInUrlRelay(ctx).enabled() is True


@pytest.mark.parametrize(("mode", "expected"), [("never", False), ("always", True)])
def test_the_operator_can_settle_it_by_hand(ctx, monkeypatch, mode, expected) -> None:
    """A pty confuses some terminals; ``auth.signin.pty`` is the way out."""
    monkeypatch.setattr(ctx.runner, "tty_available", lambda: not expected)
    tune(ctx, pty=mode)
    assert SignInUrlRelay(ctx).enabled() is expected


# -- what the operator is told --------------------------------------------------


def test_the_announcement_names_the_file_and_the_clipboard(ctx, monkeypatch) -> None:
    monkeypatch.setattr(ctx.runner, "tty_available", lambda: True)
    monkeypatch.setattr(ctx.runner, "which", lambda name, *a, **k: "/usr/bin/xclip")
    relay = SignInUrlRelay(ctx)
    said = " ".join(relay.announce())
    assert str(relay.url_file) in said and "wl-copy" in said


def test_the_announcement_says_how_to_get_a_clipboard_tool(ctx, monkeypatch) -> None:
    """The advice has to name a component phase 1 actually knows about."""
    monkeypatch.setattr(ctx.runner, "tty_available", lambda: True)
    monkeypatch.setattr(ctx.runner, "which", lambda name, *a, **k: None)
    said = " ".join(SignInUrlRelay(ctx).announce())
    assert "--install-only clipboard" in said


def test_nothing_is_announced_when_nothing_will_be_captured(ctx, monkeypatch) -> None:
    """Promising a captured copy that never arrives is worse than saying nothing."""
    monkeypatch.setattr(ctx.runner, "tty_available", lambda: False)
    assert SignInUrlRelay(ctx).announce() == []


# -- the pseudo-terminal underneath ---------------------------------------------


class Terminal:
    """The operator's end of the stand-in terminal: what they see, what they type."""

    def __init__(self, master: int, screen: bytearray, reader: threading.Thread, stop: threading.Event) -> None:
        self._master = master
        self._screen = screen
        self._reader = reader
        self._stop = stop
        self.open = True

    @property
    def shown(self) -> bytes:
        return bytes(self._screen)

    def type(self, text: str) -> None:
        os.write(self._master, text.encode())

    def hang_up(self) -> None:
        """Close the window, or run out of piped input.

        The reader has to be stopped first: a thread blocked in ``read`` holds
        the master open however many times it is closed, and then the other end
        never sees the hang-up at all.
        """
        if not self.open:
            return
        self.open = False
        self._stop.set()
        self._reader.join(timeout=5)
        os.close(self._master)


@contextmanager
def fake_terminal(monkeypatch: pytest.MonkeyPatch):
    """Give this process a terminal, since pytest's own stdin is a pipe.

    ``run_tty`` puts the real stdin into raw mode and reads the real stdout's
    window size, so there is no way to exercise it without one. A pty pair
    standing in for the operator's terminal is the honest substitute: the code
    under test does exactly what it would do on a real one, and a reader thread
    keeps the far end drained so nothing deadlocks on a full buffer.

    A context manager rather than a fixture because pytest reinstates its own
    ``sys.stdout`` when the call phase begins, which would undo anything a
    fixture had put there during setup.
    """
    import pty as pty_mod

    master, slave = pty_mod.openpty()
    stdin = os.fdopen(os.dup(slave), "rb", buffering=0)
    stdout = os.fdopen(slave, "wb", buffering=0)
    monkeypatch.setattr(sys, "stdin", stdin)
    monkeypatch.setattr(sys, "stdout", stdout)

    screen = bytearray()
    stop = threading.Event()

    def drain() -> None:
        import select

        while not stop.is_set():
            try:
                if not select.select([master], [], [], 0.05)[0]:
                    continue
                chunk = os.read(master, 65536)
            except OSError:  # the slave was closed
                return
            if not chunk:
                return
            screen.extend(chunk)

    reader = threading.Thread(target=drain, daemon=True)
    reader.start()
    terminal = Terminal(master, screen, reader, stop)
    try:
        yield terminal
    finally:
        stdin.close()
        stdout.close()
        terminal.hang_up()


def emit(text: str) -> tuple[list[str], dict[str, str]]:
    """A child that writes ``text`` to its terminal, argv and env.

    Through the environment rather than argv so the sample keeps its escape
    sequences exactly, without a layer of shell quoting to get wrong.
    """
    return (
        [sys.executable, "-c", "import os, sys; sys.stdout.write(os.environ['SAMPLE'])"],
        {"SAMPLE": text},
    )


def test_a_url_wrapped_by_the_child_is_recovered_off_the_wire(ctx, monkeypatch) -> None:
    """End to end: a real child, a real pty, the whole URL out the other side."""
    runner = Runner(dry_run=False, default_timeout=30.0)
    ctx.runner = runner
    relay = SignInUrlRelay(ctx)
    argv, env = emit(hard_wrapped(CLAUDE_URL))

    with fake_terminal(monkeypatch) as terminal:
        result = runner.run_tty(argv, env=env, timeout=30.0, on_output=relay.feed)

    assert result.ok
    assert relay.seen == [CLAUDE_URL]
    assert terminal.shown, "the operator still sees what the child printed"


def test_the_proxied_stream_is_not_kept(monkeypatch) -> None:
    """It is the operator's screen: it may hold whatever they typed into it."""
    runner = Runner(dry_run=False, default_timeout=30.0)
    argv, env = emit("a code goes here\r\n")
    with fake_terminal(monkeypatch):
        result = runner.run_tty(argv, env=env, timeout=30.0)
    assert result.stdout == "" and result.stderr == ""


def test_the_child_is_given_the_size_it_should_wrap_to(monkeypatch) -> None:
    """Whoever configures a width gets it; the child is asking the pty, not us."""
    runner = Runner(dry_run=False, default_timeout=30.0)
    seen: list[str] = []
    with fake_terminal(monkeypatch):
        runner.run_tty(
            [sys.executable, "-c", "import os; print(os.get_terminal_size(1).columns)"],
            timeout=30.0,
            columns=137,
            on_output=seen.append,
        )
    assert "137" in "".join(seen)


def test_a_sign_in_that_never_finishes_is_cut_off(monkeypatch) -> None:
    """Nobody is coming back to this terminal; it must not hold the run forever."""
    runner = Runner(dry_run=False, default_timeout=30.0)
    with fake_terminal(monkeypatch):
        result = runner.run_tty([sys.executable, "-c", "import time; time.sleep(30)"], timeout=0.5)
    assert result.timed_out and result.returncode == 124


def test_a_code_pasted_early_is_not_thrown_away(monkeypatch) -> None:
    """Whatever was typed before the child started still reaches it.

    Putting a terminal into raw mode discards pending input unless asked not to,
    and a verification code pasted a moment too early is exactly the pending
    input nobody can retype -- it only exists in the browser they have since
    navigated away from.
    """
    runner = Runner(dry_run=False, default_timeout=30.0)
    seen: list[str] = []
    reader = [
        sys.executable,
        "-c",
        "import sys; sys.stdout.write('got[' + sys.stdin.readline().strip() + ']')",
    ]
    with fake_terminal(monkeypatch) as terminal:
        terminal.type("4/0AVMBsJi-pasted-early\n")
        result = runner.run_tty(reader, timeout=30.0, on_output=seen.append)
    assert result.ok
    assert "got[4/0AVMBsJi-pasted-early]" in "".join(seen)


def test_losing_our_end_of_the_terminal_does_not_kill_the_sign_in(monkeypatch) -> None:
    """The child is told the input ended; it is not cut off mid-flight.

    A closed window or a wrapper whose piped input ran out used to end the pump
    outright, which closed the child's terminal under it. It should see
    end-of-input, the way a real terminal signals one, and get to finish.
    """
    runner = Runner(dry_run=False, default_timeout=30.0)
    seen: list[str] = []
    waiting = [
        sys.executable,
        "-c",
        "import sys\ntry:\n input()\nexcept EOFError:\n sys.stdout.write('END')\n",
    ]
    with fake_terminal(monkeypatch) as terminal:
        threading.Timer(0.3, terminal.hang_up).start()
        result = runner.run_tty(waiting, timeout=20.0, on_output=seen.append)
    assert result.ok and not result.timed_out
    assert "END" in "".join(seen)


def test_a_missing_command_is_reported_not_raised(monkeypatch) -> None:
    runner = Runner(dry_run=False, default_timeout=5.0)
    with fake_terminal(monkeypatch):
        result = runner.run_tty(["/nonexistent/sign-in"], timeout=5.0)
    assert result.returncode == 127 and not result.ok


def test_without_a_terminal_the_child_simply_gets_ours(monkeypatch) -> None:
    """Under CI there is nothing to proxy, and the sign-in must still run."""
    runner = Runner(dry_run=False, default_timeout=5.0)
    monkeypatch.setattr(runner, "tty_available", lambda: False)
    calls: list[dict] = []
    monkeypatch.setattr(
        runner,
        "run",
        lambda argv, **kw: (calls.append(kw), CommandResult(argv=list(argv), returncode=0))[1],
    )
    assert runner.run_tty(["claude", "setup-token"], timeout=7.0).ok
    assert calls == [{"timeout": 7.0, "env": None, "capture": False, "label": None}]


def test_a_dry_run_starts_nothing(monkeypatch) -> None:
    runner = Runner(dry_run=True, default_timeout=5.0)
    monkeypatch.setattr(runner, "tty_available", lambda: pytest.fail("should not be asked"))
    result = runner.run_tty(["claude", "setup-token"], label="claude sign-in")
    assert result.simulated and result.ok


def test_the_screen_can_be_rewritten_without_touching_what_is_read(ctx, monkeypatch) -> None:
    """End to end: the operator sees the rewritten URL, the relay the child's own."""

    class Upper:
        def feed(self, text: str) -> str:
            return text.replace("CHILD", "SHOWN")

        def flush(self) -> str:
            return ""

    runner = Runner(dry_run=False, default_timeout=30.0)
    seen: list[str] = []
    argv, env = emit("before CHILD after\n")
    with fake_terminal(monkeypatch) as terminal:
        result = runner.run_tty(argv, env=env, timeout=30.0, on_output=seen.append, screen=Upper())
    assert result.ok
    assert "CHILD" in "".join(seen)
    assert b"SHOWN" in terminal.shown and b"CHILD" not in terminal.shown


def test_text_held_back_is_painted_when_the_child_ends(ctx, monkeypatch) -> None:
    class HoldAll:
        def __init__(self) -> None:
            self.held = ""

        def feed(self, text: str) -> str:
            self.held += text
            return ""

        def flush(self) -> str:
            held, self.held = self.held, ""
            return held

    runner = Runner(dry_run=False, default_timeout=30.0)
    argv, env = emit("nothing is swallowed")
    with fake_terminal(monkeypatch) as terminal:
        runner.run_tty(argv, env=env, timeout=30.0, screen=HoldAll())
        deadline = time.monotonic() + 5
        while b"swallowed" not in terminal.shown and time.monotonic() < deadline:
            time.sleep(0.05)
    assert b"nothing is swallowed" in terminal.shown
