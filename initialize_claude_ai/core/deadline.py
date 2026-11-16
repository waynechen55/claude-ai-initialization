"""A wall-clock ceiling for the whole run.

Every operation in this program is already bounded -- a probe at 8 seconds, a
stage at 180, an agent turn at 900, a package install at 900. None of that
bounds the *total*: phase 4 against thirteen addresses took 259 minutes with
every individual timeout comfortably unmet, because 259 minutes is what a few
hundred well-behaved operations add up to. A deadline is the only thing that
answers "do not run longer than this", and it cannot be expressed as a timeout
on anything smaller.

It is deliberately **not a kill switch**. Expiring stops *admission*: work that
has not started is dropped, work already running is left to finish. Killing an
agent turn halfway discards tokens that have already been paid for and leaves a
half-written answer behind, which is a worse outcome than finishing a minute
late. The cost of that choice is honest overrun -- an address already running
can take as long as an address takes, which on a slow target is the better part
of an hour -- and the report says so rather than pretending the number was
observed exactly.

Nothing here is on by default. With no ``--timeout`` the run behaves exactly as
it did before this module existed: :func:`Deadline.none` returns an object whose
:attr:`expired` is permanently ``False`` and which no code path has to special
case.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field

#: ``90m``, ``2h``, ``5400s``, ``5400``. Bare digits are seconds, which is what
#: every timeout in the packaged defaults is measured in.
_DURATION = re.compile(r"^\s*(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>[smhd]?)\s*$", re.IGNORECASE)

_UNIT_SECONDS = {"": 1.0, "s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0}


class DurationError(ValueError):
    """A duration that could not be read, with the accepted spellings."""


def parse_duration(text: str | float | int) -> float:
    """Seconds, from ``"2h"``, ``"90m"``, ``"5400s"`` or ``5400``.

    Suffixes because a deadline is typed by a person and ``7200`` is a worse way
    to say two hours than ``2h`` is. Seconds are the bare unit so that a value
    copied out of the TOML, where everything is ``*_seconds``, still means what
    it looks like.
    """
    if isinstance(text, bool):  # bool is an int; nobody means True seconds
        raise DurationError(f"{text!r} is not a duration")
    if isinstance(text, (int, float)):
        seconds = float(text)
    else:
        match = _DURATION.match(str(text))
        if not match:
            raise DurationError(
                f"{text!r} is not a duration: write a number of seconds, or use a suffix -- 45s, 90m, 2h, 1d"
            )
        seconds = float(match.group("value")) * _UNIT_SECONDS[match.group("unit").lower()]
    if seconds <= 0:
        raise DurationError(f"{text!r} is not a duration: it has to be greater than zero")
    return seconds


@dataclass
class Deadline:
    """When this run must stop starting new work.

    ``limit`` of ``0`` means no deadline, which is the default and the state
    every run was in before this existed.
    """

    limit: float = 0.0
    started: float = field(default_factory=time.monotonic)
    #: What was still to be done when the deadline stopped admission, in the
    #: order it would have been done. Filled in by whoever was admitting.
    skipped: list[str] = field(default_factory=list)
    #: Which phase was in progress when it expired, for the final verdict.
    phase: str = ""

    @classmethod
    def none(cls) -> "Deadline":
        """A deadline that never expires -- the default, and not a special case."""
        return cls(limit=0.0)

    @property
    def set(self) -> bool:
        return self.limit > 0

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.started

    @property
    def remaining(self) -> float:
        """Seconds left, or ``inf`` when no deadline is set."""
        if not self.set:
            return float("inf")
        return self.limit - self.elapsed

    @property
    def expired(self) -> bool:
        return self.set and self.remaining <= 0

    def note_skipped(self, *names: str) -> None:
        """Record work that was never attempted, so the report can name it."""
        for name in names:
            text = str(name).strip()
            if text and text not in self.skipped:
                self.skipped.append(text)

    def to_dict(self) -> dict[str, object]:
        return {
            "limit_seconds": round(self.limit, 2) if self.set else None,
            "elapsed_seconds": round(self.elapsed, 2),
            "expired": self.expired,
            "phase_in_progress": self.phase,
            "never_attempted": list(self.skipped),
        }


__all__ = ["Deadline", "DurationError", "parse_duration"]
