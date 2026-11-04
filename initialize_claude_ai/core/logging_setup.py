"""Logging and console output, with redaction wired in at the sink.

Both the logging pipeline and the human-facing console pass every string through
:func:`initialize_claude_ai.core.redact.redact`, so a secret cannot escape through a log
line even if a caller forgets to scrub it.

Both also end up in the same place on disk.  A :class:`Transcript` is one open
file that the console and the logging pipeline share, so it holds the run as it
happened -- a summary box printed between two debug records appears between them
there too -- rather than two half-records to be reconciled by timestamp
afterwards.
"""

from __future__ import annotations

import logging
import os
import re
import sys
import textwrap
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, NamedTuple, Sequence, TextIO

from . import redact

LOGGER_NAME = "initialize_claude_ai"

_LEVELS = {
    "trace": logging.DEBUG - 5,
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "warn": logging.WARNING,
    "error": logging.ERROR,
    "critical": logging.CRITICAL,
}


class RedactingFilter(logging.Filter):
    """Scrub the rendered message and any string args."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact.redact(record.msg)
        if record.args:
            if isinstance(record.args, dict):
                record.args = {k: _scrub(v) for k, v in record.args.items()}
            else:
                record.args = tuple(_scrub(a) for a in record.args)
        return True


def _scrub(value: Any) -> Any:
    return redact.redact(value) if isinstance(value, str) else value


def level_for(name: str, default: int = logging.INFO) -> int:
    return _LEVELS.get(str(name).strip().lower(), default)


# --------------------------------------------------------------------------- #
# Transcript
# --------------------------------------------------------------------------- #

#: An SGR (colour and weight) escape sequence. Stripped on the way into the
#: transcript: the words are the record, and a file full of ``\x1b[38;5;78m`` is
#: neither readable in a pager nor greppable.
ANSI_SGR = re.compile(r"\x1b\[[0-9;]*m")

#: Timestamp format shared by the log records and the per-run header, so a line
#: and the run it belongs to can be matched by eye.
DEFAULT_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

#: Width of the rules that separate one run from the next inside the file.
BANNER_WIDTH = 78


def strip_ansi(text: str) -> str:
    return ANSI_SGR.sub("", text)


#: How many lines that could not reach the log file are kept for the closing summary.
UNWRITTEN_KEPT = 50


@dataclass
class Transcript:
    """The run's own record of everything it emitted.

    One open file with two producers -- :class:`Console`, for what the operator
    saw, and a logging handler for the detail underneath it. Sharing the handle
    is the whole point: it is what makes the order on disk the order things
    actually happened in.

    The file is appended to rather than replaced, because the question it
    usually answers is "what did the run *before* this one do?". Each run opens
    with a header carrying the version, the time and the redacted command line,
    so an accumulated file can still be read one run at a time.
    """

    path: Path
    stream: TextIO
    date_format: str = DEFAULT_DATE_FORMAT
    #: Why lines stopped reaching the file, once they did (said on the terminal once).
    failure: str = ""
    #: Lines that could not be written, the last few kept for the closing summary.
    unwritten: list[str] = field(default_factory=list)

    @classmethod
    def open(
        cls,
        path: str | os.PathLike[str],
        *,
        max_bytes: int = 0,
        backups: int = 0,
        date_format: str = DEFAULT_DATE_FORMAT,
    ) -> "Transcript | None":
        """Open ``path`` for appending, rotating it first if it has grown too big.

        Returns ``None`` rather than raising when the file cannot be opened: a
        read-only state directory is a reason to run without a transcript, not a
        reason to refuse to initialize the machine.
        """
        target = Path(path).expanduser()
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            rotate(target, max_bytes, backups)
            stream = open(target, "a", encoding="utf-8", errors="replace")
        except OSError as exc:
            get_logger().warning("cannot open log file %s: %s", target, exc)
            return None
        return cls(path=target, stream=stream, date_format=date_format)

    def write(self, text: str = "") -> None:
        """Record one line, stripped of colour and passed through redaction."""
        if self.stream.closed:
            return
        line = redact.redact(strip_ansi(text))
        try:
            self.stream.write(line + "\n")
            self.stream.flush()
        except (OSError, ValueError) as exc:
            # A full disk must not take the run down at the moment it is trying
            # to report what it did -- nor go unsaid: a run once went silent in
            # its log at phase 4 and nobody could tell where it had stopped.
            self.unwritten = [*self.unwritten[-(UNWRITTEN_KEPT - 1) :], line]
            if not self.failure:
                self.failure = str(exc)
                try:
                    sys.stderr.write(
                        f"\n!! the log file {self.path} can no longer be written ({exc}); "
                        "what follows is on this terminal only\n"
                    )
                    sys.stderr.flush()
                except (OSError, ValueError):
                    pass

    def start(self, program: str, version: str, argv: Sequence[str]) -> None:
        """Introduce this run in the file, so appended runs stay separable."""
        rule = "#" * BANNER_WIDTH
        command = " ".join(redact.redact_argv([program, *argv]))
        self.write("")
        self.write(rule)
        self.write(f"# {program} {version}  started {self._now()}  pid {os.getpid()}")
        self.write(f"# command: {command}")
        self.write(f"# code: {code_fingerprint()}  ({Path(__file__).resolve().parent.parent})")
        self.write(rule)

    def close(self, exit_code: int | None = None) -> None:
        """Write the closing line and release the handle. Safe to call twice."""
        if self.stream.closed:
            return
        note = "" if exit_code is None else f"  exit {exit_code}"
        self.write(f"# finished {self._now()}{note}")
        try:
            self.stream.close()
        except OSError:  # pragma: no cover - closing an already broken handle
            pass

    def _now(self) -> str:
        return time.strftime(self.date_format)


def rotate(path: Path, max_bytes: int, backups: int) -> None:
    """Roll ``x.log`` to ``x.log.1`` (and so on) once it passes ``max_bytes``.

    Checked when the file is opened and not on every write, so no run ever
    rotates out from under itself and a transcript stays readable as one piece.
    A file that records every console line grows faster than a log of records
    alone, and an initializer that quietly fills a home directory would have
    traded one problem for a worse one.
    """
    if max_bytes <= 0 or backups <= 0:
        return
    try:
        if path.stat().st_size < max_bytes:
            return
    except OSError:
        return
    # Oldest first, so nothing is overwritten before it has been moved up.
    for index in range(backups, 0, -1):
        source = path if index == 1 else path.with_name(f"{path.name}.{index - 1}")
        if not source.exists():
            continue
        try:
            target = path.with_name(f"{path.name}.{index}")
            target.unlink(missing_ok=True)
            source.rename(target)
        except OSError:  # pragma: no cover - someone else's file, someone else's problem
            return


#: The transcript this process is writing to, if any. Module-level because the
#: sink is process-wide, exactly like :mod:`logging`'s own configuration: the
#: error paths in ``main`` have to record themselves without a ``RunContext`` to
#: reach through.
_ACTIVE: Transcript | None = None


def active_transcript() -> Transcript | None:
    return _ACTIVE


def close_transcript(exit_code: int | None = None) -> None:
    """Close the active transcript, if there is one. Safe to call twice."""
    global _ACTIVE
    if _ACTIVE is None:
        return
    logger = logging.getLogger(LOGGER_NAME)
    # Detach before closing, or the next log record in this process goes to a
    # closed handle. Matters in-process (tests, an embedded caller) rather than
    # for the CLI, which exits straight afterwards.
    for handler in list(logger.handlers):
        if getattr(handler, "stream", None) is _ACTIVE.stream:
            logger.removeHandler(handler)
    _ACTIVE.close(exit_code)
    _ACTIVE = None


def record(text: str) -> None:
    """Send one line to the transcript; a no-op when there is none.

    For the few places that print outside :class:`Console` -- the JSON report on
    stdout and the top-level error handler -- whose output belongs in the record
    like everything else.
    """
    if _ACTIVE is not None:
        _ACTIVE.write(text)


def setup_logging(
    *,
    level: str = "info",
    transcript: Transcript | None = None,
    file_level: str = "debug",
    fmt: str = "%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    date_format: str = DEFAULT_DATE_FORMAT,
    stream: TextIO | None = None,
    style: "Console | None" = None,
) -> logging.Logger:
    """Configure the ``initialize_claude_ai`` logger. Idempotent.

    A ``transcript`` becomes the process-wide record: log records are formatted
    into it at ``file_level``, and it is published through
    :func:`active_transcript` for the console and the error paths to share.

    ``style`` is a :class:`Console` bound to the same stream as the log, used
    only to render each record's level as a badge. Pass it and a ``WARNING``
    from the logger looks like a ``[  WARN  ]`` from the report, because it is
    the same table drawing both; leave it out and the levels come through as
    plain words, which is what a file or a pipe should get.
    """
    global _ACTIVE
    logging.addLevelName(_LEVELS["trace"], "TRACE")
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(_LEVELS["trace"])
    logger.propagate = False
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()

    redacting = RedactingFilter()

    console_handler = logging.StreamHandler(stream or sys.stderr)
    console_handler.setLevel(level_for(level))
    console_handler.setFormatter(
        BadgeFormatter(style) if style is not None else logging.Formatter("%(levelname)-7s %(message)s")
    )
    console_handler.addFilter(redacting)
    logger.addHandler(console_handler)

    if transcript is not None:
        # A StreamHandler on the transcript's own handle rather than a
        # FileHandler on the same path: two handles would interleave by luck,
        # one interleaves by construction.
        file_handler = logging.StreamHandler(transcript.stream)
        file_handler.setLevel(level_for(file_level, logging.DEBUG))
        file_handler.setFormatter(logging.Formatter(fmt, date_format))
        file_handler.addFilter(redacting)
        logger.addHandler(file_handler)
        _ACTIVE = transcript

    return logger


def get_logger(suffix: str = "") -> logging.Logger:
    return logging.getLogger(f"{LOGGER_NAME}.{suffix}" if suffix else LOGGER_NAME)


# --------------------------------------------------------------------------- #
# Console
# --------------------------------------------------------------------------- #

#: Style codes, in five palettes, selected by ``console.palette``.
#:
#: The problem each one solves is the same: the eight ANSI primaries are picked
#: by the terminal's own theme, so the same report is a different colour on every
#: host. The 256-colour cube gives a way out, and where in that cube you land is
#: partly taste, so there is more than one answer here rather than one imposed
#: one.
#:
#: Two numbers describe every choice below. *Contrast* -- the WCAG ratio against
#: the background -- is how readable a colour is, and 4.5:1 is the line at which
#: text counts as readable at all. *Saturation* -- how far the colour is from the
#: grey of the same brightness -- is how colourful it is. They are not the same
#: knob, and the interesting thing is that they pull against each other: the most
#: saturated colours in the cube are its corners, and the corners have wildly
#: different brightnesses, because the eye weights green at 0.72 and blue at 0.07.
#:
#: ``vibrant`` is the default. It takes saturation first: four of its six hues are
#: at the full chroma of the cube (mean 0.81, where a muted set is near 0.5), so
#: the report is properly coloured rather than tinted grey. Readability is then a
#: floor it has to clear, not a thing to maximise: every hue lands between 7:1 and
#: 11.7:1 on black, above the 4.5:1 line by a wide margin and short of the 15:1
#: and up of the neon corners (``#00ff00``, ``#ffff00``), which are the colours
#: people turn colour off to escape. That is a 1.7x spread from quietest hue to
#: loudest, wider than ``contrast`` below, and it is the price of the chroma: a
#: fully saturated emerald simply *is* brighter than any red that is still red.
#:
#: ``contrast`` is the alternative for anyone who wants an even weight instead of
#: the colour. The same six meanings, held inside a 7:1-10:1 band -- a 1.4x spread
#: -- at about two thirds the saturation. It is the calmer thing to read for an
#: hour; ``vibrant`` is the easier thing to scan in ten seconds.
#:
#: In both, red is at the bottom of the band, and that is a fact about eyes rather
#: than a choice: with red weighted 0.21, any red bright enough to reach 11:1 on
#: black has stopped being red and become pink. Failure lines get their weight
#: from ``bold``, the ``FAIL`` word and the glyph instead -- which is the right
#: place for it anyway, since those three survive a pipe.
#:
#: ``daylight`` is the whole design upside down, for a light terminal: every hue
#: at full chroma and taken *down* to a 4.4-6.1:1 band against white, dark enough
#: to read without collapsing into the near-black that makes colour pointless.
#: ``auto`` picks it when the terminal says its background is light (see
#: :func:`background_is_light`).
#:
#: ``soft`` is the quiet one, desaturated for anyone who wants less of all of
#: this. ``basic`` is the plain eight-colour set, used automatically where
#: 256-colour escapes would come out as literal text.
#:
#: In every palette the hue carries the same meaning -- red is the failure, amber
#: the caveat, green the success -- so switching changes only how loud it is. And
#: in none of them does the hue carry that meaning *alone*: every line also has a
#: word (``FAIL``) and a glyph, which is what keeps the report readable for a
#: red-green colour blind reader, and in a pipe where the colour is gone.
PALETTES: dict[str, dict[str, str]] = {
    # Saturation, then contrast against #000 / #1e1e1e, in that order.
    "vibrant": {
        "reset": "\033[0m",
        "bold": "\033[1m",
        "dim": "\033[38;5;244m",  # --   5.3 / 4.2  grey, and the only grey
        "red": "\033[38;5;203m",  # 0.63 7.1 / 5.6  coral red
        "green": "\033[38;5;41m",  # 1.00 10.9 / 8.6 emerald
        "yellow": "\033[38;5;214m",  # 1.00 11.4 / 9.0 amber
        "blue": "\033[38;5;75m",  # 0.63 9.1 / 7.2  azure
        "magenta": "\033[38;5;207m",  # 0.63 8.3 / 6.6  orchid
        "cyan": "\033[38;5;44m",  # 1.00 11.7 / 9.3 cyan
    },
    # Contrast against #ffffff / #fdf6e3 (solarized light), in that order.
    "daylight": {
        "reset": "\033[0m",
        "bold": "\033[1m",
        "dim": "\033[38;5;245m",  # --   3.5 / 3.2  grey: quietest on the page
        "red": "\033[38;5;160m",  # 1.00 5.4 / 5.0  true red
        "green": "\033[38;5;28m",  # 1.00 4.7 / 4.4  forest
        "yellow": "\033[38;5;130m",  # 1.00 4.7 / 4.4  burnt amber; gold vanishes here
        "blue": "\033[38;5;26m",  # 1.00 5.8 / 5.4  ultramarine
        "magenta": "\033[38;5;127m",  # 1.00 6.1 / 5.7  fuchsia
        "cyan": "\033[38;5;30m",  # 1.00 4.4 / 4.0  deep teal
    },
    # Contrast against #000 / #1e1e1e, in that order, from the comment above.
    "contrast": {
        "reset": "\033[0m",
        "bold": "\033[1m",
        "dim": "\033[38;5;244m",  # 5.3 / 4.2  grey: recessive on either background
        "red": "\033[38;5;203m",  # 7.1 / 5.6  coral -- the most saturated hue here
        "green": "\033[38;5;71m",  # 7.8 / 6.2  fern
        "yellow": "\033[38;5;179m",  # 10.2 / 8.1 old gold, not the neon amber
        "blue": "\033[38;5;75m",  # 9.1 / 7.2  azure
        "magenta": "\033[38;5;177m",  # 8.8 / 7.0  violet
        "cyan": "\033[38;5;73m",  # 8.2 / 6.5  teal, muted so azure stays distinct
    },
    "soft": {
        "reset": "\033[0m",
        "bold": "\033[1m",
        "dim": "\033[38;5;245m",  # mid grey, readable either way round
        "red": "\033[38;5;174m",  # dusty rose
        "green": "\033[38;5;108m",  # sage
        "yellow": "\033[38;5;180m",  # sand
        "blue": "\033[38;5;110m",  # slate
        "magenta": "\033[38;5;139m",  # mauve
        "cyan": "\033[38;5;109m",  # pale teal
    },
    "basic": {
        "reset": "\033[0m",
        "bold": "\033[1m",
        "dim": "\033[2m",
        "red": "\033[31m",
        "green": "\033[32m",
        "yellow": "\033[33m",
        "blue": "\033[34m",
        "magenta": "\033[35m",
        "cyan": "\033[36m",
    },
}

#: Other names the same set answers to.
#:
#: The eight ANSI primaries are how nearly every terminal colour scheme is
#: published -- green pass, red fail, amber caveat, cyan in flight -- usually
#: under a name like "the classic standard". Somebody who has read one of those
#: and wants it here should not have to discover that this program files that
#: set under ``basic`` because it is also what a 16-colour terminal falls back
#: to. Same codes, three ways to ask for them.
PALETTE_ALIASES: dict[str, str] = {
    "classic": "basic",
    "standard": "basic",
    "ansi": "basic",
}

#: The palette ``auto`` resolves to wherever the terminal can render it.
DEFAULT_PALETTE = "vibrant"

#: ...unless the terminal says its background is light, in which case ``auto``
#: uses this one instead.
LIGHT_PALETTE = "daylight"


def background_is_light(environ: "dict[str, str] | None" = None) -> bool:
    """Whether the terminal is dark text on a light background.

    Read from ``COLORFGBG``, which several terminals export as ``fg;bg`` with
    ANSI colour numbers -- ``15;0`` is white on black, ``0;15`` black on white.
    It is the only answer available without interrogating the terminal itself,
    and asking (an OSC 11 query) means writing an escape to a stream that may
    not be a terminal and then waiting for a reply that may never come, in a
    program whose first job is to be safe to run anywhere.

    So: absent or unparseable means dark, because that is what a terminal is
    unless somebody has changed it, and because being slightly too bright on a
    light background is the gentler way to be wrong -- the light palette on a
    dark screen is genuinely hard to read.
    """
    raw = (environ if environ is not None else os.environ).get("COLORFGBG", "")
    fields = [part.strip() for part in raw.split(";") if part.strip()]
    # The background is the last field, so a single number is a foreground with
    # no background behind it -- half an answer, which is no answer. (urxvt
    # writes three fields, "0;default;15", hence the last rather than the
    # second.)
    if len(fields) < 2:
        return False
    try:
        background = int(fields[-1])
    except ValueError:
        # Some terminals report "15;default", which tells us nothing.
        return False
    # 7 is light grey and 15 is white; 0-6 and 8-14 are the dark half.
    return background in (7, 15) or background >= 231


class Badge(NamedTuple):
    """How one state looks: its colour, its word, and its sign.

    Three carriers for one meaning, deliberately. Colour is the fastest of them
    to read and the first to disappear -- into a pipe, a greyscale screenshot,
    a reader who does not separate red from green -- so the word and the shape
    are what the report actually rests on, and the colour is the accelerator.
    """

    #: Style keys from the active palette, applied in order.
    styles: tuple[str, ...]
    #: The word inside the brackets. Fixed width across the table so the
    #: messages after the badges line up in a column.
    label: str
    #: Key into the active :data:`GLYPH_SETS` entry -- the shape.
    glyph: str


#: Every state this program can print, in the classic terminal scheme: green
#: for pass, red for fail, amber for the caveat, cyan for work in flight, bold
#: red for an error, grey for the detail underneath. One table rather than one
#: per output path, so a ``WARNING`` from the logger and a ``[PARTIAL]`` step
#: are the same amber and carry the same shape.
#:
#: Two deliberate departures from that scheme as it is usually published:
#:
#: * **Skipped is grey, not amber.** The published scheme has four test states
#:   where this program has five outcomes, and the extra one is ``PARTIAL`` --
#:   "it worked, but read this". That is the amber, and it is the line the
#:   operator must not miss. A skip here is routinely the *good* answer ("node
#:   3.22 already present, nothing to do"), so painting skips amber would put
#:   the commonest line in the run in the colour reserved for the rarest, and
#:   the reader would learn within one screen to skim past both.
#: * **A failed step is bold, not just an error line.** In a test runner a fail
#:   is one of hundreds and an error is the unusual one; here a failed step *is*
#:   the thing the operator has to act on, and there are rarely more than a few.
#:
#: Everything else is the scheme as given: pass green, running cyan, warning
#: amber, error bold red, debug dim, info in the terminal's own colour, because
#: information is the default case and does not need announcing.
BADGES: dict[str, Badge] = {
    # -- outcomes: the five statuses a step or a phase can end in -------------
    "ok": Badge(("green",), "OK", "passed"),
    "changed": Badge(("cyan",), "CHANGED", "changed"),
    "skipped": Badge(("dim",), "SKIP", "skipped"),
    "partial": Badge(("yellow",), "PARTIAL", "caution"),
    "failed": Badge(("red", "bold"), "FAIL", "failed"),
    # -- states that are not outcomes -----------------------------------------
    "running": Badge(("cyan",), "RUNNING", "running"),
    "warning": Badge(("yellow",), "WARN", "caution"),
    "error": Badge(("red", "bold"), "ERROR", "failed"),
    "info": Badge((), "INFO", "note"),
    "debug": Badge(("dim",), "DEBUG", "note"),
    "trace": Badge(("dim",), "TRACE", "note"),
}

#: The badges worth showing side by side, two rows of five: the outcomes a step
#: can end in, then the states that are not outcomes. Split this way because the
#: question a reader has in front of a swatch is "can I tell these apart?", and
#: the answer only matters within a row -- ``OK`` never appears where ``DEBUG``
#: might. Ten at 12 columns each also happens to be two lines at width 80.
BADGE_ROWS: tuple[tuple[str, ...], ...] = (
    ("ok", "changed", "skipped", "partial", "failed"),
    ("running", "warning", "error", "info", "debug"),
)


def styles_for(role: str, default: tuple[str, ...] = ()) -> tuple[str, ...]:
    """The style keys for one state, for callers drawing their own lines.

    Summary rows and box frames are not badges -- they are a whole line taking
    its colour from a verdict -- but they must take it from the *same* verdict
    colour, so they read it from here rather than keeping a second table.
    """
    mark = BADGES.get(role)
    return mark.styles if mark else default


def glyph_for(role: str, default: str = "note") -> str:
    """The glyph *key* for one state, for the same callers as :func:`styles_for`.

    A summary row is not a badge, but its sign has to be the sign the badges
    used, or the same outcome is a triangle in one table and a cross in the one
    below it.
    """
    mark = BADGES.get(role)
    return mark.glyph if mark else default


#: Not an outcome: work that has started and has not finished. Printed before a
#: step that is about to make the terminal go quiet for a minute, so that a long
#: install or a subagent looks like progress rather than a hang.
RUNNING = "running"

#: Log levels in the vocabulary above, so the two streams agree. ``TRACE`` is
#: this program's own level, below ``DEBUG``.
LEVEL_ROLE: dict[str, str] = {
    "CRITICAL": "error",
    "ERROR": "error",
    "WARNING": "warning",
    "INFO": "info",
    "DEBUG": "debug",
    "TRACE": "trace",
}

#: The colour a summary frame takes from the result it is framing.
#:
#: A box is the largest thing on the screen, and leaving all of them grey wasted
#: the one element big enough to be seen without reading: on a four-phase run the
#: failed phase is now a red frame among green ones, findable by scrolling rather
#: than by squinting at verdict lines. The frame is a single column of characters,
#: so this is an accent, not a wash -- and it says nothing the words inside the
#: box do not already say, which is the rule for every colour in this program.
FRAME_STYLE: dict[str, tuple[str, ...]] = {
    "ok": ("green",),
    "changed": ("cyan",),
    "partial": ("yellow",),
    "failed": ("red",),
    "error": ("red",),
    "skipped": ("dim",),
}

#: Banners frame what is *about* to happen, which has no outcome to colour by, so
#: they take the information hue instead -- and, being blue, stay distinct from
#: every verdict colour a summary can end up with.
BANNER_FRAME_STYLE: tuple[str, ...] = ("blue",)

#: Headings and section rules: bold blue, for the same reason as the banners.
#:
#: Blue is the one hue in the table that never means an outcome, so a heading can
#: be coloured without competing with the verdicts underneath it -- and bold gives
#: the scanning eye somewhere to stop when scrolling back through a long run.
HEADING_STYLE: tuple[str, ...] = ("blue", "bold")

#: Frame and marker glyphs, in two sets. Which one is used is decided at run
#: time from the output encoding, because a box drawn in characters the terminal
#: cannot encode is worse than no box at all -- and this program is expected to
#: run on the bare, pre-Claude state of any Linux distribution, including hosts
#: still sitting in a C/POSIX locale.
#:
#: The marker glyphs are shapes rather than emoji, and the shapes are chosen to
#: be distinguishable from each other at a glance: a tick, a cross, a triangle,
#: a dot. Coloured circles (🟢🔴🟡) would say the same thing more loudly, but
#: every one of them is two cells wide with a width the terminal and the font
#: have to agree on -- and this program pads box rows by counting characters, so
#: one wide glyph moves the right-hand frame of every line it appears in. A
#: report that is legible in a C locale and inside a box is worth more than one
#: that is prettier on the terminals where it happens to line up.
GLYPH_SETS: dict[str, dict[str, str]] = {
    "unicode": {
        "tl": "╭",
        "tr": "╮",
        "bl": "╰",
        "br": "╯",
        "ml": "├",
        "mr": "┤",
        "h": "─",
        "v": "│",
        "passed": "✔",
        "failed": "✘",
        "skipped": "•",
        "caution": "▲",
        "sep": "·",
        "changed": "↻",
        "running": "»",
        "note": "·",
    },
    "ascii": {
        "tl": "+",
        "tr": "+",
        "bl": "+",
        "br": "+",
        "ml": "+",
        "mr": "+",
        "h": "-",
        "v": "|",
        "passed": "+",
        "failed": "x",
        "skipped": "-",
        "caution": "!",
        "sep": "-",
        "changed": "*",
        "running": ">",
        "note": ".",
    },
}

#: Styles for each of the three outcome buckets a summary reports on.
#:
#: Skipped is grey rather than amber: it is not a warning, and colouring it like
#: one trains the reader to ignore the colour that is. Only the failure row is
#: bold -- weight is the scarcest thing on a screen, so spending it on all three
#: rows would be the same as spending it on none.
OUTCOME_STYLE: dict[str, tuple[str, ...]] = {
    "failed": ("red", "bold"),
    "skipped": ("dim",),
    "passed": ("green",),
}

#: Widths that are not worth putting in a config file on their own, but are
#: worth not scattering through the rendering code as bare integers.
DEFAULT_WIDTH = 80
MIN_WIDTH = 40

#: Width of the word inside a badge. Every label is centred in it, so the
#: messages after ``[   OK   ]`` and ``[ PARTIAL ]`` start in the same column.
BADGE_WIDTH = 8


class Line(NamedTuple):
    """One line of a :meth:`Console.box`, before wrapping and padding.

    ``hang`` is the indent applied to continuation lines, which is how a
    ``label  value value value`` row keeps its value column when it wraps.
    """

    text: str
    styles: tuple[str, ...] = ()
    hang: int = 0


@dataclass
class Console:
    """Minimal, dependency-free, redaction-safe console writer."""

    stream: TextIO | None = None
    color: bool | None = None
    quiet: bool = False
    json_mode: bool = False
    #: "unicode", "ascii", or "auto" to decide from the output encoding.
    glyph_set: str = "auto"
    #: A key of :data:`PALETTES`, or "auto" to decide from the colour depth the
    #: terminal advertises.
    palette: str = "auto"
    #: Total width of rules, banners and boxes.
    width: int = DEFAULT_WIDTH
    #: When false, banners and summary boxes collapse to plain lines. For log
    #: scrapers and for anyone who simply does not want the frames.
    decorate: bool = True
    #: Whether :meth:`progress` prints at all. See its docstring.
    show_progress: bool = True
    #: Put the date and time in front of every line that is a record of
    #: something happening. Structural lines -- box frames, table rows -- are
    #: never stamped; see :meth:`write`.
    timestamps: bool = True
    #: How that stamp is rendered. The same format the log records use, so the
    #: two halves of the transcript can be read as one timeline.
    date_format: str = DEFAULT_DATE_FORMAT
    #: Where a copy of every line goes, colour stripped. ``None`` records
    #: nothing; the CLI passes the transcript opened from ``paths.log_file``.
    transcript: "Transcript | None" = None
    glyphs: dict[str, str] = field(init=False, repr=False)
    codes: dict[str, str] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._out = self.stream or sys.stdout
        if self.color is None:
            self.color = (
                self._out.isatty() and os.environ.get("NO_COLOR") is None and os.environ.get("TERM", "") != "dumb"
            )
        self.width = max(MIN_WIDTH, int(self.width or DEFAULT_WIDTH))
        self.glyphs = GLYPH_SETS.get(self._resolve_glyph_set(), GLYPH_SETS["ascii"])
        self._local = threading.local()
        self.codes = PALETTES.get(self._resolve_palette(), PALETTES["basic"])

    def _resolve_palette(self) -> str:
        choice = str(self.palette or "auto").strip().lower()
        choice = PALETTE_ALIASES.get(choice, choice)
        if choice in PALETTES:
            return choice
        term = os.environ.get("TERM", "")
        if os.environ.get("COLORTERM") or "256color" in term or "direct" in term:
            return LIGHT_PALETTE if background_is_light() else DEFAULT_PALETTE
        # 256-colour escapes on an 8-colour terminal come out as literal text,
        # which is worse than a saturated red.
        return "basic"

    def _resolve_glyph_set(self) -> str:
        choice = str(self.glyph_set or "auto").strip().lower()
        if choice in GLYPH_SETS:
            return choice
        # Ask the stream whether it could actually encode the frame, rather
        # than guessing from the locale name.
        encoding = getattr(self._out, "encoding", None) or ""
        try:
            GLYPH_SETS["unicode"]["tl"].encode(encoding)
        except (LookupError, UnicodeError, TypeError):
            return "ascii"
        return "unicode"

    @property
    def inner_width(self) -> int:
        """Usable text width inside a box, allowing for ``"| "`` and ``" |"``."""
        return self.width - 4

    def style(self, text: str, *styles: str) -> str:
        """Public form of :meth:`_style`, for callers rendering their own lines."""
        return self._style(text, *styles)

    def _style(self, text: str, *styles: str) -> str:
        if not self.color or not styles:
            return text
        # Deduplicated, and in the order given: callers splat a badge's styles
        # and add their own emphasis on top, so ("red", "bold") + "bold" is a
        # normal thing to be handed rather than a mistake to render twice.
        prefix = "".join(self.codes.get(s, "") for s in dict.fromkeys(styles))
        return f"{prefix}{text}{self.codes['reset']}" if prefix else text

    def badge(self, role: str) -> str:
        """The sign, the colour and the word for one state, as one token.

        Every line that reports a state goes through here -- steps, log
        records, progress -- so there is exactly one place where what a state
        looks like is decided, and no way for two output paths to drift into
        disagreeing about what amber means.
        """
        mark = BADGES.get(role) or Badge((), role.upper()[:BADGE_WIDTH], "note")
        sign = self.glyphs.get(mark.glyph, "")
        return self._style(f"{sign} [{mark.label:^{BADGE_WIDTH}}]", *mark.styles)

    # -- when each line happened -----------------------------------------------

    def mute_transient(self, muted: bool = True) -> None:
        """Silence progress and bullets *on this thread only*.

        Used when addresses are worked concurrently. Those lines are a live
        commentary on one piece of work, and four commentaries at once are not
        four times as informative -- the agent heartbeat does not even name its
        target ("endpoint: still working (30.0s)"), so from four threads it is
        noise. The caller replaces them with one consolidated line and prints
        each address's verdicts as a block when it finishes.

        Thread-local because the main thread must keep printing while the
        workers are quiet.
        """
        self._local.muted = muted

    @property
    def transient_muted(self) -> bool:
        return bool(getattr(self._local, "muted", False))

    def stamp(self) -> str:
        """The clock reading that goes in front of a line, or ``""``.

        One prefix, in one format, on the terminal *and* in the transcript --
        which is the point. The file already timestamps its log records, so
        without this the record of a long run interleaves stamped detail with
        unstamped narration and the two cannot be lined up: "the endpoint stage
        took eleven minutes" is a question the file could not answer about its
        own contents.
        """
        if not self.timestamps:
            return ""
        return f"{time.strftime(self.date_format)} "

    @property
    def stamp_width(self) -> int:
        """How much of the line the stamp occupies, for width arithmetic."""
        return len(self.stamp())

    def write(self, text: str = "", *, stamp: bool = True) -> None:
        """Emit one line to the terminal and to the transcript.

        ``stamp=False`` is for lines whose shape carries meaning and would be
        broken by a prefix: the frames and interiors of boxes, table rows, the
        JSON report. They belong to a section, and the section is introduced by
        a stamped line -- see :meth:`rule` and :meth:`header`.
        """
        line = redact.redact(text)
        if stamp and line.strip():
            # Blank lines stay blank: a timestamp on its own is not a record of
            # anything, and vertical space is doing a job.
            line = f"{self.stamp()}{line}"
        # Recorded before the suppression checks, deliberately: --quiet and
        # --json decide what the terminal shows, not what the run did. A quiet
        # run is exactly the one whose record someone will want afterwards.
        if self.transcript is not None:
            self.transcript.write(line)
        # In --json mode stdout is reserved for the machine-readable report.
        if self.quiet or self.json_mode:
            return
        print(line, file=self._out, flush=True)

    def rule(self, title: str = "", width: int | None = None) -> None:
        # The stamp is part of the line, so the rule is shortened by it rather
        # than pushed past the terminal's width and wrapped.
        width = (width or self.width) - self.stamp_width
        if not title:
            self.write(self._style("-" * max(3, width), "dim"))
            return
        bar = "-" * max(3, width - len(title) - 4)
        self.write(self._style(f"-- {title} {bar}", *HEADING_STYLE))

    def header(self, text: str) -> None:
        self.write("")
        self.write(self._style(text, *HEADING_STYLE))

    # -- framed output ---------------------------------------------------------

    def _edge(self, left: str, right: str, title: str = "", styles: tuple[str, ...] = ()) -> str:
        """One horizontal frame edge, optionally carrying an inset title."""
        g = self.glyphs
        span = self.width - 2
        if not title:
            return self._style(f"{left}{g['h'] * span}{right}", *(styles or ("dim",)))
        label = f" {title} "[: max(0, span - 4)]
        fill = g["h"] * max(2, span - 1 - len(label))
        # The title is styled separately so it stays legible against a dim frame.
        return (
            self._style(f"{left}{g['h']}", *(styles or ("dim",)))
            + self._style(label, "bold")
            + self._style(f"{fill}{right}", *(styles or ("dim",)))
        )

    def box(
        self,
        lines: Iterable[Line | str],
        *,
        title: str = "",
        footer: Iterable[Line | str] = (),
        frame_styles: tuple[str, ...] = ("dim",),
    ) -> None:
        """Draw a framed block of text.

        Falls back to plain, unframed lines when ``decorate`` is off, so the
        same call site serves both presentations and no caller has to branch.
        """
        # Suppression is :meth:`write`'s job, so that a box still reaches the
        # transcript when the terminal is not getting it. The early return is
        # only the shortcut for when nobody at all is listening.
        if (self.quiet or self.json_mode) and self.transcript is None:
            return
        body = [entry if isinstance(entry, Line) else Line(entry) for entry in lines]
        tail = [entry if isinstance(entry, Line) else Line(entry) for entry in footer]

        if not self.decorate:
            if title:
                self.write(self._style(title, "bold"))
            for entry in [*body, *tail]:
                self.write(self._style(entry.text, *entry.styles), stamp=False)
            return

        g = self.glyphs
        # A box is one event, not a line each: stamping the frame would both
        # break its alignment and repeat the same second twenty times. The
        # heading that introduces it carries the time.
        self.write(self._edge(g["tl"], g["tr"], title, frame_styles), stamp=False)
        self._box_body(body, frame_styles)
        if tail:
            self.write(self._edge(g["ml"], g["mr"], styles=frame_styles), stamp=False)
            self._box_body(tail, frame_styles)
        self.write(self._edge(g["bl"], g["br"], styles=frame_styles), stamp=False)

    def _box_body(self, lines: list[Line], frame_styles: tuple[str, ...]) -> None:
        bar = self._style(self.glyphs["v"], *frame_styles)
        inner = self.inner_width
        for entry in lines:
            for chunk in self._wrap(entry.text, inner, entry.hang):
                # Pad before styling: ANSI escapes would corrupt the width.
                self.write(f"{bar} {self._style(chunk.ljust(inner), *entry.styles)} {bar}", stamp=False)

    @staticmethod
    def _wrap(text: str, width: int, hang: int = 0) -> list[str]:
        if not text.strip():
            return [""]
        indent = " " * min(hang, max(0, width - 8))
        wrapped = textwrap.wrap(
            text,
            width=width,
            subsequent_indent=indent,
            break_long_words=False,
            break_on_hyphens=False,
        )
        # A token too long for the box -- a path, a URL, a command -- is broken
        # across lines rather than cut short. Words are kept whole while they
        # fit, because that is what makes a paragraph readable; but a path the
        # operator has to type, silently missing its last twelve characters, is
        # worse than one that wraps.
        if any(len(line) > width for line in wrapped):
            wrapped = textwrap.wrap(
                text,
                width=width,
                subsequent_indent=indent,
                break_long_words=True,
                break_on_hyphens=False,
            )
        return wrapped or [text[:width]]

    def status(
        self,
        status: str,
        message: str,
        detail: str = "",
        *,
        tag: str = "",
        tag_styles: tuple[str, ...] = (),
    ) -> None:
        """One step line: verdict, what it was, optionally how, then the detail.

        ``tag`` is for the second thing a reader needs to know about a step after
        whether it passed -- phase 4 uses it for the mode a stage ran in. It sits
        in its own column, in its own colour, because it is not part of the
        detail and must not be skimmed as part of it.
        """
        line = f"{self.badge(status)} {message}"
        if tag:
            line += "  " + self._style(f"[{tag}]", *tag_styles)
        if detail:
            line += self._style(f"  {detail}", "dim")
        self.write(line)

    def progress(self, message: str, detail: str = "") -> None:
        """Announce work that has started, before the terminal goes quiet.

        Printed by the few steps that can take a minute with nothing to show --
        a package install, a subagent turn -- and by nothing else: a line per
        step that finishes in milliseconds would double the length of the report
        to say what the verdict line says a moment later anyway.

        Turned off with ``console.progress = false`` for anyone who wants the
        report to be only verdicts. It goes through :meth:`write` like every
        other line, so when it is on it reaches the transcript too: the record
        of a run that took four minutes is more use with the four minutes marked
        than without.
        """
        if not self.show_progress or self.transient_muted:
            return
        line = f"{self.badge(RUNNING)} {message}"
        if detail:
            line += self._style(f"  {detail}", "dim")
        self.write(line)

    def bullet(self, text: str, indent: int = 2) -> None:
        """An indented note under the line above it.

        Muted on a worker thread (see :meth:`mute_transient`): the agent
        heartbeat comes through here and does not name its target, so four of
        them at once are unreadable.
        """
        if self.transient_muted:
            return
        self.write(f"{' ' * indent}- {text}")

    def kv(self, key: str, value: Any, indent: int = 2, width: int = 28) -> None:
        rendered = "" if value is None else str(value)
        self.write(f"{' ' * indent}{self._style(key.ljust(width), 'dim')} {rendered}")

    def table(self, rows: list[list[str]], headers: list[str] | None = None, indent: int = 2) -> None:
        if not rows and not headers:
            return
        all_rows = ([headers] if headers else []) + [[str(c) for c in r] for r in rows]
        widths = [max(len(r[i]) for r in all_rows if i < len(r)) for i in range(len(all_rows[0]))]
        pad = " " * indent
        # A table is one answer in several rows, and a stamp in front of each
        # would stop the columns lining up -- which is the only reason a table
        # is a table rather than a list.
        if headers:
            self.write(
                pad + self._style("  ".join(h.ljust(widths[i]) for i, h in enumerate(headers)), "bold"),
                stamp=False,
            )
            self.write(pad + self._style("  ".join("-" * w for w in widths), "dim"), stamp=False)
        for row in rows:
            cells = [str(c).ljust(widths[i]) if i < len(widths) else str(c) for i, c in enumerate(row)]
            self.write(pad + "  ".join(cells).rstrip(), stamp=False)

    def json(self, payload: Any) -> None:
        import json as _json

        rendered = redact.redact(_json.dumps(payload, indent=2, default=str, sort_keys=False))
        if self.transcript is not None:
            self.transcript.write(rendered)
        print(rendered, file=self._out, flush=True)


class BadgeFormatter(logging.Formatter):
    """Format a log record with the same badge the report would give it.

    The report goes to stdout and the log to stderr, and on an interactive run
    they arrive interleaved in one scrollback. Two different renderings of "this
    went wrong" in the same column of the same screen is a small thing that
    costs the reader a beat every time, so both come from :data:`BADGES`.

    A level with no entry in :data:`LEVEL_ROLE` -- one a library set, or one a
    caller invented -- still prints, under its own name: swallowing a record
    because its level is unfamiliar would be the worst possible trade.
    """

    def __init__(self, style: Console, fmt: str = "%(message)s") -> None:
        super().__init__(fmt)
        self.style = style

    def format(self, record: logging.LogRecord) -> str:
        role = LEVEL_ROLE.get(record.levelname, record.levelname.lower())
        # The console's own stamp, not one of this formatter's making: the two
        # producers write to the same screen and the same file, and a warning
        # timed differently from the step it interrupted would be worse than no
        # timestamp at all. The file handler stamps itself with %(asctime)s, so
        # this is the screen's copy only and nothing is stamped twice.
        return f"{self.style.stamp()}{self.style.badge(role)} {super().format(record)}"


def code_fingerprint() -> str:
    """A short hash of the program's own source and packaged data, and the newest file's time.

    A run copied from another machine was judged against code it may not have
    run: its log named a version string that never changes. With this line in
    the header, two logs -- or a log and a checkout -- are compared at a glance.
    """
    import hashlib

    root = Path(__file__).resolve().parent.parent
    digest = hashlib.sha256()
    newest = 0.0
    try:
        files = sorted(
            p for p in root.rglob("*") if p.suffix in (".py", ".toml", ".md") and "__pycache__" not in p.parts
        )
        for path in files:
            digest.update(str(path.relative_to(root)).encode("utf-8"))
            digest.update(path.read_bytes())
            newest = max(newest, path.stat().st_mtime)
    except OSError:
        return "unknown"
    stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(newest)) if newest else "?"
    return f"sha256:{digest.hexdigest()[:12]}, newest file {stamp}"
