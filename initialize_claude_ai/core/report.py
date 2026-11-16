"""Structured results shared by every phase.

Each phase returns a :class:`PhaseResult` made of :class:`StepResult` records.
The same objects drive the human console output and the ``--json`` machine
report, so the two can never disagree.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

from . import redact


class Status:
    """Step/phase outcome. Ordered from best to worst by :data:`SEVERITY`."""

    OK = "ok"
    CHANGED = "changed"
    SKIPPED = "skipped"
    PARTIAL = "partial"
    FAILED = "failed"


SEVERITY = {Status.OK: 0, Status.CHANGED: 1, Status.SKIPPED: 2, Status.PARTIAL: 3, Status.FAILED: 4}

#: Statuses that mean "this did not work".
BAD = {Status.PARTIAL, Status.FAILED}

#: The five statuses collapsed into the three answers an end-of-phase report has
#: to give: what worked, what was left alone, what did not work. Ordered worst
#: first, because that is the order a summary is read in.
OUTCOMES: dict[str, tuple[str, ...]] = {
    "failed": (Status.FAILED, Status.PARTIAL),
    "skipped": (Status.SKIPPED,),
    "passed": (Status.OK, Status.CHANGED),
}

#: Reverse index of :data:`OUTCOMES`, built once rather than searched per step.
OUTCOME_OF: dict[str, str] = {status: outcome for outcome, statuses in OUTCOMES.items() for status in statuses}

#: The same statuses as a plain-English verdict on a whole phase or run.
#: ``ok`` and ``changed`` both mean it did what it set out to do -- the
#: difference is only whether anything on the machine moved, which matters for
#: judging idempotence but is not the answer to "did it work?".
VERDICT: dict[str, str] = {
    Status.OK: "PASSED",
    Status.CHANGED: "PASSED WITH CHANGES",
    Status.SKIPPED: "SKIPPED",
    Status.PARTIAL: "PARTIALLY PASSED",
    Status.FAILED: "FAILED",
}


def verdict_of(status: str) -> str:
    """The overall result in words. Unknown statuses are shouted back verbatim."""
    return VERDICT.get(status, str(status).upper())


class Safety:
    """Whether it is safe to carry on from here.

    Separate from :class:`Status` on purpose. The status says what *happened*;
    this says what it *means for the rest of the run*, and the two do not always
    agree -- a phase can pass and still leave the next one unable to proceed
    (phase 2 verifying only a backend phase 3 was not asked to use), and a phase
    can be skipped entirely without endangering anything.
    """

    SAFE = "safe"
    CAUTION = "caution"
    BLOCKED = "blocked"


SAFETY_LABEL = {
    Safety.SAFE: "SAFE TO CONTINUE",
    Safety.CAUTION: "SAFE TO CONTINUE, WITH CAVEATS",
    Safety.BLOCKED: "NOT SAFE TO CONTINUE",
}

#: Colour and marker for each level. The marker names a key in the glyph set.
SAFETY_STYLE = {
    Safety.SAFE: ("green", "passed"),
    Safety.CAUTION: ("yellow", "caution"),
    Safety.BLOCKED: ("red", "failed"),
}

#: Worst-first, for combining the per-phase levels into one for the run.
SAFETY_SEVERITY = {Safety.SAFE: 0, Safety.CAUTION: 1, Safety.BLOCKED: 2}


def worst_safety(levels: Iterable[str]) -> str:
    items = [level for level in levels if level in SAFETY_SEVERITY]
    return max(items, key=lambda level: SAFETY_SEVERITY[level]) if items else Safety.SAFE


def worst(statuses: Iterable[str]) -> str:
    items = [s for s in statuses if s in SEVERITY]
    if not items:
        return Status.SKIPPED
    return max(items, key=lambda s: SEVERITY[s])


def roll_up(statuses: Iterable[str]) -> str:
    """Collapse many statuses into the one that describes the whole.

    Not simply :func:`worst`, because two of the five statuses are not really
    "worse" in the way the severity order implies:

    * ``failed`` alongside anything that worked is ``partial`` -- some of it
      landed, and the exit-code policy turns on that distinction.
    * ``skipped`` alongside anything that worked is not ``skipped``. Reporting a
      run as SKIPPED because one of four phases had nothing to do would hide the
      three that did the work.
    """
    items = [s for s in statuses if s in SEVERITY]
    if not items:
        return Status.SKIPPED
    status = worst(items)
    did_work = [s for s in items if s in (Status.OK, Status.CHANGED)]
    if not did_work:
        return status
    if status == Status.FAILED:
        return Status.PARTIAL
    if status == Status.SKIPPED:
        return Status.CHANGED if Status.CHANGED in did_work else Status.OK
    return status


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def format_duration(seconds: float) -> str:
    """A duration a human can read. Sub-second work says so rather than '0.0s'."""
    if seconds >= 60:
        return f"{int(seconds // 60)}m{seconds % 60:04.1f}s"
    if seconds >= 1:
        return f"{seconds:.1f}s"
    if seconds >= 0.001:
        return f"{seconds * 1000:.0f}ms"
    return "<1ms"


def bucket(steps: Iterable["StepResult"]) -> dict[str, list["StepResult"]]:
    """Sort steps into failed/skipped/passed, worst bucket first.

    Every bucket is present even when empty, so a report can state "0 failed"
    rather than leaving the reader to infer it from an absence.
    """
    out: dict[str, list[StepResult]] = {name: [] for name in OUTCOMES}
    for step in steps:
        name = OUTCOME_OF.get(step.status)
        if name is not None:
            out[name].append(step)
    return out


@dataclass
class StepResult:
    """One thing a phase did, and how it went.

    The smallest unit the terminal prints and the JSON report records. A step
    carries its own ``remediation`` because advice is only useful attached to
    the thing that needs it -- a list of tips at the end of a run belongs to
    nothing in particular.
    """

    name: str
    status: str = Status.OK
    detail: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    remediation: list[str] = field(default_factory=list)
    duration: float = 0.0
    error: str = ""
    #: Which subject of the phase this step is about -- the server being
    #: discovered, the component being installed. Empty means the step is about
    #: the run as a whole. See :class:`GroupResult`.
    group: str = ""

    @property
    def failed(self) -> bool:
        return self.status in BAD

    def short_name(self, group: str = "") -> str:
        """The step's name without the subject it is already filed under.

        ``https://example.com :: endpoint`` reads as ``endpoint`` inside that
        address's own section, where repeating the address on every row would
        push the part that differs off the right of the box.
        """
        prefix = f"{group} :: "
        return self.name[len(prefix) :] if group and self.name.startswith(prefix) else self.name

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "group": self.group,
            "status": self.status,
            "detail": redact.redact(self.detail),
            "data": redact.redact_mapping(self.data),
            "remediation": [redact.redact(r) for r in self.remediation],
            "duration_seconds": round(self.duration, 3),
            "error": redact.redact(self.error),
        }


@dataclass
class GroupResult:
    """One subject a phase worked through, and every step it took about it.

    A phase that repeats itself over a list -- one server per ``--mcp-server``,
    one component per ``--components`` -- is judged on the subjects, not on the
    steps. Eight probes against a host that ends up with a working MCP server
    is *one* success; reporting it as four failures and four skips buries the
    answer under the method, and makes a run look worse the harder it looked.

    So the subject's verdict is declared by the phase (:meth:`
    PhaseResult.declare_group`) rather than rolled up from its steps: only the
    phase knows that a server found by the third stage passes even though the
    first two came back empty. The steps are still all here, marked and with
    their reasons, underneath the verdict they did not decide.
    """

    key: str
    status: str = Status.SKIPPED
    detail: str = ""
    steps: list[StepResult] = field(default_factory=list)

    @property
    def outcome(self) -> str:
        """Which of the three buckets this subject lands in."""
        return OUTCOME_OF.get(self.status, "skipped")

    def outcomes(self) -> dict[str, list[StepResult]]:
        return bucket(self.steps)

    def tally(self) -> dict[str, int]:
        return {name: len(steps) for name, steps in self.outcomes().items()}

    def to_dict(self) -> dict[str, Any]:
        return {
            "group": self.key,
            "status": self.status,
            "verdict": verdict_of(self.status),
            "detail": redact.redact(self.detail),
            "tally": self.tally(),
            "steps": [s.to_dict() for s in self.steps],
        }


@dataclass
class PhaseResult:
    """Everything one phase did, and the single verdict it rolls up to.

    The verdict is derived from the steps rather than set by the phase, so a
    phase cannot report success over a failed step. Where a phase declares
    groups (see ``group_noun``), the count that reaches the summary is of
    groups -- servers, components -- not of individual steps, because that is
    the unit the operator actually cares about.
    """

    phase: str
    title: str = ""
    status: str = Status.OK
    steps: list[StepResult] = field(default_factory=list)
    data: dict[str, Any] = field(default_factory=dict)
    remediation: list[str] = field(default_factory=list)
    started_at: str = field(default_factory=_now)
    finished_at: str = ""
    duration: float = 0.0
    skipped_reason: str = ""
    #: What this result means for the rest of the run; see :class:`Safety`.
    #: Filled in by the phase, which is the only thing that knows what depends
    #: on it. Default-safe so a bare PhaseResult is never falsely alarming.
    safety: str = Safety.SAFE
    safety_reason: str = ""
    #: The phase hit something it cannot proceed without -- a required setting
    #: with no value anywhere. Set by :meth:`Phase.require`, and honoured by
    #: :meth:`finalize`, which then reports FAILED rather than letting the
    #: steps that happened to pass first soften it into PARTIAL. The
    #: distinction is not cosmetic: PARTIAL lets the run carry on and can exit
    #: 0, which is the wrong answer for a configuration that was never written.
    fatal: bool = False
    #: Verdicts on the subjects this phase worked through, keyed by the string
    #: its steps carry in ``group``. Empty for a phase that does one thing once.
    #: The ``steps`` on each are filled in by :meth:`groups`, which is the only
    #: place the two halves are joined.
    group_verdicts: dict[str, GroupResult] = field(default_factory=dict)
    #: What one of those subjects is called, for the sentences that count them.
    group_noun: str = "group"

    def add(
        self,
        name: str,
        status: str = Status.OK,
        detail: str = "",
        *,
        data: dict[str, Any] | None = None,
        remediation: Iterable[str] = (),
        duration: float = 0.0,
        error: str = "",
        group: str = "",
    ) -> StepResult:
        step = StepResult(
            name=name,
            status=status,
            detail=detail,
            data=dict(data or {}),
            remediation=list(remediation),
            duration=duration,
            error=error,
            group=group,
        )
        self.steps.append(step)
        return step

    # -- subjects --------------------------------------------------------------

    def declare_group(self, key: str, status: str, detail: str = "") -> GroupResult:
        """Record the verdict on one subject, independent of its steps.

        Called once the phase knows how that subject turned out. Declaring the
        same key twice replaces the earlier verdict, so a subject revisited
        later in the phase is not counted twice.
        """
        verdict = GroupResult(key=key, status=status, detail=detail)
        self.group_verdicts[key] = verdict
        return verdict

    def groups(self) -> list[GroupResult]:
        """Every subject, in the order its first step appeared.

        A subject with steps but no declared verdict -- an address that failed
        to parse, so discovery never reached it -- still counts; its verdict is
        rolled up from what little happened to it rather than being left out of
        the scoreboard, which would quietly shrink the denominator.
        """
        order: list[str] = []
        for step in self.steps:
            if step.group and step.group not in order:
                order.append(step.group)
        order.extend(key for key in self.group_verdicts if key not in order)

        out: list[GroupResult] = []
        for key in order:
            steps = [s for s in self.steps if s.group == key]
            declared = self.group_verdicts.get(key)
            status = declared.status if declared else roll_up(s.status for s in steps)
            # An undeclared subject still has to say why it is where it is, and
            # the only thing that knows is the step that decided it -- the worst
            # one, which is also the one that set the status just above.
            worst_step = max(steps, key=lambda s: SEVERITY.get(s.status, 0), default=None)
            out.append(
                GroupResult(
                    key=key,
                    status=status,
                    detail=declared.detail if declared else (worst_step.detail if worst_step else ""),
                    steps=steps,
                )
            )
        return out

    def ungrouped(self) -> list[StepResult]:
        """Steps about the run as a whole rather than about one subject."""
        return [s for s in self.steps if not s.group]

    def group_tally(self) -> dict[str, int]:
        """``{"failed": n, "skipped": n, "passed": n}`` over subjects."""
        out = {name: 0 for name in OUTCOMES}
        for group in self.groups():
            out[group.outcome] += 1
        return out

    def finalize(self, *, started: float | None = None) -> "PhaseResult":
        """Derive the phase status from its steps and stamp the clock."""
        if self.group_verdicts or any(s.group for s in self.steps):
            self.status = self._status_from_groups()
        elif self.steps:
            self.status = roll_up(step.status for step in self.steps)
        elif self.skipped_reason:
            self.status = Status.SKIPPED
        if self.fatal:
            self.status = Status.FAILED
        self.finished_at = _now()
        if started is not None:
            self.duration = time.monotonic() - started
        for step in self.steps:
            for item in step.remediation:
                if item not in self.remediation:
                    self.remediation.append(item)
        return self

    def _status_from_groups(self) -> str:
        """The phase as its subjects went, not as its steps went.

        The whole point of grouping is that the steps are the *method* and the
        subjects are the *result*: a phase that probed two hundred paths to
        find thirteen servers should read as thirteen answers, and a phase
        where none of them yielded anything has FAILED, not half-passed
        because the bookkeeping steps around the search all went fine.

        So the subjects decide the verdict, and the ungrouped steps can only
        make it worse, never better: a run-level failure (a file that could not
        be written) still shows, and a run-level CHANGED is allowed to promote
        an otherwise clean pass to "passed with changes", because that is the
        one thing the subjects cannot say for themselves.
        """
        status = roll_up([g.status for g in self.groups()])
        bad = [s.status for s in self.ungrouped() if s.status in BAD]
        if bad:
            return roll_up([status, *bad])
        if status == Status.OK and any(s.status == Status.CHANGED for s in self.ungrouped()):
            return Status.CHANGED
        return status

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for step in self.steps:
            out[step.status] = out.get(step.status, 0) + 1
        return out

    def outcomes(self) -> dict[str, list[StepResult]]:
        """Steps bucketed into failed/skipped/passed, worst bucket first."""
        return bucket(self.steps)

    def tally(self) -> dict[str, int]:
        """``{"failed": n, "skipped": n, "passed": n}``, in that order."""
        return {name: len(steps) for name, steps in self.outcomes().items()}

    def to_dict(self) -> dict[str, Any]:
        return {
            "phase": self.phase,
            "title": self.title,
            "status": self.status,
            "verdict": verdict_of(self.status),
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_seconds": round(self.duration, 3),
            "skipped_reason": self.skipped_reason,
            "fatal": self.fatal,
            "safety": self.safety,
            "safety_label": SAFETY_LABEL.get(self.safety, ""),
            "safety_reason": redact.redact(self.safety_reason),
            "counts": self.counts(),
            "tally": self.tally(),
            # The subject-level view sits beside the step-level one rather than
            # replacing it: a consumer asking "how many servers worked" and one
            # asking "what exactly was tried" both get a straight answer.
            "group_noun": self.group_noun,
            "group_tally": self.group_tally(),
            "groups": [g.to_dict() for g in self.groups()],
            "steps": [s.to_dict() for s in self.steps],
            "data": redact.redact_mapping(self.data),
            "remediation": [redact.redact(r) for r in self.remediation],
        }


@dataclass
class RunReport:
    """The whole run, as one JSON-serialisable object.

    This is what ``--json`` emits and what the terminal summary is rendered
    from -- the same data both times, so nothing can be on screen and missing
    from the file. It also carries how the run was invoked, which is what makes
    a surprising result traceable to the layer that set the value. Redaction is
    applied on the way in, so no secret ever reaches it.
    """

    tool: str = "initialize-claude-ai"
    version: str = ""
    started_at: str = field(default_factory=_now)
    finished_at: str = ""
    dry_run: bool = False
    platform: dict[str, Any] = field(default_factory=dict)
    config_sources: list[str] = field(default_factory=list)
    phases: list[PhaseResult] = field(default_factory=list)
    environment: dict[str, str] = field(default_factory=dict)
    #: How the run was invoked: the command line, the options given, the settings
    #: that are not at their packaged default, the environment it read. Shown on
    #: screen before the first phase and carried here so that a report read months
    #: later says what it was run with, not merely what it found.
    invocation: dict[str, Any] = field(default_factory=dict)

    def add(self, result: PhaseResult) -> PhaseResult:
        self.phases.append(result)
        return result

    @property
    def fatal(self) -> bool:
        """Did any phase stop for want of a value it could not do without?"""
        return any(p.fatal for p in self.phases)

    @property
    def status(self) -> str:
        """The run as a whole -- except that a fatal phase is not negotiable.

        :func:`roll_up` softens a failure that sits alongside successes into
        PARTIAL, which is the right reading of most failures: three phases did
        their work and one did not. It is the wrong reading of this one. A
        phase that stopped because a required setting was nowhere to be found
        did not half-succeed; it declined to write a configuration it knew to
        be incomplete, and the run has to say so with an exit code that a
        script will notice rather than one it will step over.
        """
        status = roll_up(p.status for p in self.phases)
        return Status.FAILED if self.fatal else status

    @property
    def safety(self) -> str:
        """The run is only as safe to continue from as its least safe phase."""
        return worst_safety(p.safety for p in self.phases)

    def exit_code(self, *, partial_is_failure: bool = False) -> int:
        status = self.status
        if status == Status.FAILED:
            return 1
        if status == Status.PARTIAL:
            return 2 if partial_is_failure else 0
        return 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool": self.tool,
            "version": self.version,
            "status": self.status,
            "verdict": verdict_of(self.status),
            "fatal": self.fatal,
            "safety": self.safety,
            "safety_label": SAFETY_LABEL.get(self.safety, ""),
            "dry_run": self.dry_run,
            "started_at": self.started_at,
            "finished_at": self.finished_at or _now(),
            "platform": self.platform,
            "config_sources": self.config_sources,
            "invocation": redact.redact_mapping(self.invocation),
            "environment": redact.redact_mapping(self.environment),
            "phases": [p.to_dict() for p in self.phases],
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, default=str)
