"""Phase contract and registry.

Phases 1-3 are deterministic ("traditional mode") because the Claude environment
does not exist yet and cannot be relied on.  Phase 4 is agentic, with the
deterministic pipeline as an operator's choice and a last resort rather than an
answer to a single failed attempt.  All four implement the same interface so the CLI can
select, order, skip and report on them uniformly.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from typing import Iterable

from ..core import diskspace
from ..core.context import RunContext
from ..core.errors import InitializeClaudeAiError, MissingSettingError
from ..core.logging_setup import (
    BANNER_FRAME_STYLE,
    FRAME_STYLE,
    OUTCOME_STYLE,
    Line,
    get_logger,
    styles_for,
)
from ..core.report import (
    OUTCOME_OF,
    SAFETY_LABEL,
    SAFETY_STYLE,
    GroupResult,
    PhaseResult,
    Safety,
    Status,
    StepResult,
    bucket,
    format_duration,
    verdict_of,
)

log = get_logger("phase")

#: Fallback for ``console.max_named_steps`` when no config has been loaded.
DEFAULT_MAX_NAMED_STEPS = 12

#: Minimum width of the label column in a banner's fact rows. Widened per phase
#: when a label needs it, so the values stay in one column either way.
LABEL_WIDTH = 14


class Phase(ABC):
    """One initialization phase."""

    #: Stable identifier used on the command line (``--phases 1,4``).
    slug: str = ""
    #: Numeric order.
    number: int = 0
    #: Human title.
    title: str = ""
    #: One or two sentences shown in the opening banner: what this phase is
    #: about to do, and why, for an operator who did not write it.
    description: str = ""
    #: Config key that gates the phase (``<key>.enabled``).
    enable_key: str = ""
    #: What one subject of this phase is called, for a phase that works through
    #: a list of them and declares a verdict per subject (see
    #: :meth:`~...core.report.PhaseResult.declare_group`). Unused otherwise.
    group_noun: str = "group"

    def __init__(self, ctx: RunContext) -> None:
        self.ctx = ctx
        self.settings = ctx.settings
        self.console = ctx.console
        self.runner = ctx.runner
        self.log = get_logger(self.slug or self.__class__.__name__.lower())

    # -- lifecycle -------------------------------------------------------------

    def is_enabled(self) -> tuple[bool, str]:
        """Return ``(enabled, reason_if_disabled)``."""
        if self.enable_key and not self.settings.bool_(f"{self.enable_key}.enabled", True):
            return False, f"disabled by configuration ({self.enable_key}.enabled = false)"
        return True, ""

    @abstractmethod
    def execute(self, result: PhaseResult) -> None:
        """Do the work, recording steps on ``result``. Raise only on fatal errors."""

    def run(self) -> PhaseResult:
        """Wrap :meth:`execute` with timing, enablement and error handling."""
        result = PhaseResult(phase=self.slug, title=self.title, group_noun=self.group_noun)
        enabled, reason = self.is_enabled()
        self.banner(disabled_reason="" if enabled else reason)
        if not enabled:
            result.skipped_reason = reason
            result.status = Status.SKIPPED
            result.finalize()
            self.summarise(result)
            return result

        started = time.monotonic()
        try:
            self.execute(result)
        except MissingSettingError as exc:
            # ``require`` has already reported the step, naming every place the
            # value could have come from. Repeating that here would say the
            # same thing twice under a less useful name; all that is left to do
            # is mark the phase fatal so it cannot be rolled up to PARTIAL.
            self.log.error("%s", exc.message)
            result.fatal = True
        except InitializeClaudeAiError as exc:
            self.log.error("%s", exc.message)
            result.add(
                "phase",
                Status.FAILED,
                exc.message,
                remediation=[exc.remediation] if exc.remediation else [],
                error=exc.code,
            )
        except KeyboardInterrupt:
            result.add("phase", Status.FAILED, "interrupted by user")
            raise
        except Exception as exc:  # noqa: BLE001 - a phase must not kill the run
            if diskspace.is_out_of_space(exc, self.settings):
                # Not a bug of this phase: the disk is full. One line, no traceback.
                self.ctx.state["out_of_space"] = str(exc)[:300]
                result.add(
                    "free space", Status.FAILED, f"no space left on device: {str(exc)[:300]}", error="out_of_space"
                )
                result.fatal = True
            else:
                self.log.exception("unexpected error in phase %s", self.slug)
                result.add("phase", Status.FAILED, f"unexpected error: {exc}", error=type(exc).__name__)
        if self.ctx.runner.out_of_space and not self.ctx.state.get("out_of_space"):
            # A command in this phase failed for want of disk space -- whatever
            # the phase made of it (an older copy still there reads as "OK").
            self.ctx.state["out_of_space"] = self.ctx.runner.out_of_space
            result.add(
                "free space",
                Status.FAILED,
                f"no space left on device ({self.ctx.runner.out_of_space})",
                error="out_of_space",
            )
            result.fatal = True
        result.finalize(started=started)
        self.summarise(result)
        return result

    # -- presentation ----------------------------------------------------------

    def banner(self, *, disabled_reason: str = "") -> None:
        """Announce the phase: which one, of how many, and what it is for."""
        total = len(all_phases())
        lines: list[Line] = [
            Line(f"PHASE {self.number} OF {total}  {self.glyph('sep')}  {self.title.upper()}", ("bold",))
        ]
        if self.description:
            lines.append(Line(self.description, ("dim",)))
        facts = self.banner_facts()
        if disabled_reason:
            facts = [*facts, ("Skipping", disabled_reason)]
        if facts and self.description:
            lines.append(Line(""))
        # Widen the label column to fit, so a long label never runs into its
        # value. Two spaces of gutter, never less than the common width.
        pad = max([LABEL_WIDTH, *(len(label) + 2 for label, _ in facts)])
        for label, value in facts:
            styles = ("yellow",) if label == "Skipping" else ()
            lines.append(Line(f"{label.ljust(pad)}{value}", styles, hang=pad))
        self.console.write("")
        self.console.box(lines, frame_styles=BANNER_FRAME_STYLE)

    def banner_facts(self) -> list[tuple[str, str]]:
        """``(label, value)`` rows for the banner. Overridden per phase.

        This is the hook a phase uses to say what it is working *with* -- the
        backend, the components, the targets -- before it starts, so that a run
        can be judged wrong before it has done anything.
        """
        rows: list[tuple[str, str]] = []
        if self.ctx.dry_run:
            rows.append(("Mode", "dry run -- nothing is installed, written or signed in"))
        return rows

    def summarise(self, result: PhaseResult) -> None:
        """Report what the phase did: what passed, what was skipped, what failed."""
        limit = self.settings.int_("console.max_named_steps", DEFAULT_MAX_NAMED_STEPS)
        groups = result.groups()
        lines: list[Line] = []

        if not result.steps:
            # Nothing ran, so a three-row scoreboard of zeroes would be noise.
            lines.append(
                Line(
                    f"{self.glyph('skipped')} {result.skipped_reason or 'nothing to do'}",
                    OUTCOME_STYLE["skipped"],
                )
            )
        elif groups:
            # The phase worked through a list, so the scoreboard counts the
            # list. What was done to each of them is a section of its own below.
            lines.extend(self.scoreboard_lines(result.group_tally(), self._group_names(groups), limit))
            lines.extend(self.outcome_lines(result, limit))
            lines.append(Line(""))
            lines.append(Line(f"Every {self.group_noun}, and what happened to it:", ("bold",)))
            for group in groups:
                lines.extend(self.group_lines(group, limit))
            # Steps that belong to no subject -- the ones about the run itself.
            # Only their failures are worth the space here; everything they did
            # right is already implied by the sections above having happened.
            lines.extend(self.reason_lines(bucket(result.ungrouped())))
        else:
            buckets = result.outcomes()
            lines.extend(
                self.scoreboard_lines(
                    {name: len(steps) for name, steps in buckets.items()},
                    {name: [s.name for s in steps] for name, steps in buckets.items()},
                    limit,
                )
            )
            lines.extend(self.reason_lines(buckets))

        footer = self.verdict_lines(result) + self.safety_lines(result)
        if result.remediation:
            footer.append(Line(""))
            footer.append(Line("Next steps:", ("bold",)))
            footer.extend(Line(f"  {self.glyph('sep')} {item}", hang=4) for item in result.remediation)

        self.console.write("")
        self.console.box(
            lines,
            title=f"Phase {self.number} summary: {self.title}",
            footer=footer,
            # The frame carries the verdict too, so the phase that went wrong can
            # be found by scrolling rather than by reading every box.
            frame_styles=FRAME_STYLE.get(result.status, ("dim",)),
        )

    def outcome_lines(self, result: PhaseResult, limit: int) -> list[Line]:
        """Rows a phase adds under the scoreboard, in its own terms; none by default.

        The scoreboard's three rows are shared by every phase, so "did not
        work" has to hold everything short of working. A phase whose subjects
        can end in more than one *kind* of not-working -- phase 4's address
        registered and waiting for a credential, against one with nothing
        registered at all -- names those kinds here.
        """
        return []

    @staticmethod
    def _group_names(groups: Iterable[GroupResult]) -> dict[str, list[str]]:
        """Subject names per bucket, so the scoreboard can name who is where."""
        out: dict[str, list[str]] = {}
        for group in groups:
            out.setdefault(group.outcome, []).append(group.key)
        return out

    def scoreboard_lines(self, counts: dict[str, int], names: dict[str, list[str]], limit: int) -> list[Line]:
        """The three-row count at the top of a summary, worst row first.

        One shape for both kinds of phase, because the rows mean the same thing
        either way -- what worked, what was left alone, what did not work. Only
        what is being counted differs, and the verdict line underneath says which.
        """
        lines: list[Line] = []
        for outcome in OUTCOME_STYLE:
            listed = list(names.get(outcome, []))
            if len(listed) > limit:
                listed = [*listed[:limit], f"... and {len(listed) - limit} more"]
            head = f"{self.glyph(outcome)} {outcome.ljust(8)}{counts.get(outcome, 0):>3}  "
            lines.append(Line(f"{head}{', '.join(listed) or '-'}", OUTCOME_STYLE[outcome], hang=len(head)))
        return lines

    def group_lines(self, group: GroupResult, limit: int) -> list[Line]:
        """One subject: its verdict, its arithmetic, then every step under it.

        The verdict comes first and in the colour of the bucket it landed in,
        because that is the answer; the steps beneath it are the working. They
        are all listed, passed ones included -- knowing *which* attempt was the
        one that worked is most of what makes a success reproducible -- and the
        ones that did not pass carry their reason on the same row, since a mark
        without a reason only tells the reader to go and read the log.
        """
        styles = OUTCOME_STYLE[group.outcome]
        lines: list[Line] = [Line("")]
        lines.append(
            Line(
                f"{self.glyph(group.outcome)} {group.key}  {self.glyph('sep')}  {verdict_of(group.status)}",
                (*styles, "bold"),
                hang=4,
            )
        )
        tally = group.tally()
        counted = (
            f"{tally['passed']} of {len(group.steps)} step(s) passed  {self.glyph('sep')}  "
            f"{tally['skipped']} skipped  {self.glyph('sep')}  {tally['failed']} failed"
        )
        lines.append(Line(f"    {group.detail or 'no detail recorded'}", styles, hang=4))
        lines.append(Line(f"    {counted}", ("dim",), hang=4))

        shown = group.steps[:limit]
        # Size the name column to this subject's own steps: the widths differ
        # wildly between subjects, and one global column would indent every
        # short name by the length of the longest one anywhere in the phase.
        pad = max([len(s.short_name(group.key)) for s in shown] or [0]) + 2
        for step in shown:
            outcome = OUTCOME_OF.get(step.status, "skipped")
            head = f"      {self.glyph(outcome)} {step.short_name(group.key).ljust(pad)}"
            reason = step.detail or ("" if outcome == "passed" else "no detail recorded")
            if step.error:
                reason = f"{reason} [{step.error}]" if reason else f"[{step.error}]"
            lines.append(Line(f"{head}{reason}", OUTCOME_STYLE[outcome], hang=len(head)))
        if len(group.steps) > limit:
            lines.append(
                Line(
                    f"      ... and {len(group.steps) - limit} more step(s); see the log or --json",
                    ("dim",),
                    hang=6,
                )
            )
        return lines

    def reason_lines(self, buckets: dict[str, list[StepResult]]) -> list[Line]:
        """Spell out every step that did not pass, with why.

        A name in a scoreboard says *that* something did not happen; only the
        reason says whether that matters. Skipped steps are included for exactly
        that purpose -- "skipped" covers both "already done, nothing to do" and
        "could not be attempted", and those are not the same news.
        """
        notable = [(outcome, step) for outcome in ("failed", "skipped") for step in buckets.get(outcome, [])]
        if not notable:
            return []
        # Failures are never elided; the cap only ever bites on a long tail of
        # skips, because a phase with fifty real failures has bigger problems
        # than a tall report.
        limit = self.settings.int_("console.max_named_steps", DEFAULT_MAX_NAMED_STEPS)
        shown, hidden = notable[:limit], max(0, len(notable) - limit)

        lines = [Line(""), Line("Why these did not pass:", ("bold",))]
        for outcome, step in shown:
            styles = OUTCOME_STYLE[outcome]
            # The name carries the weight; the reason under it is read once the
            # eye has already stopped there, so it does not need to compete.
            lines.append(Line(f"  {self.glyph(outcome)} {step.name}", (*styles, "bold"), hang=6))
            lines.append(Line(f"      {step.detail or 'no detail recorded'}", styles, hang=6))
            if step.error:
                lines.append(Line(f"      error code: {step.error}", ("dim",), hang=6))
        if hidden:
            lines.append(Line(f"  ... and {hidden} more; see the log or --json for the rest", ("dim",), hang=6))
        return lines

    def safety_lines(self, result: PhaseResult) -> list[Line]:
        """The call on whether to carry on, recorded on the result as well as shown."""
        level, reason = self.safety(result)
        if self.ctx.dry_run:
            # Without this, "settings.json was written" reads as a statement of
            # fact about a host that has not been touched.
            reason = f"{reason} (Dry run: this is what would have happened -- nothing changed.)"
        result.safety, result.safety_reason = level, reason
        color, marker = SAFETY_STYLE.get(level, ("bold", "sep"))
        return [
            Line(""),
            Line(f"{self.glyph(marker)} {SAFETY_LABEL[level]}", (color, "bold")),
            Line(f"  {reason}", (color,), hang=2),
        ]

    def safety(self, result: PhaseResult) -> tuple[str, str]:
        """Is it safe to go on from here, and why? Overridden per phase.

        The default reads only the status, because that is all a phase in
        general knows. A phase that understands what depends on it should say
        so specifically -- "Vertex is verified, so phase 3 can provision" is
        worth far more than "something failed".
        """
        if result.status == Status.FAILED:
            return Safety.BLOCKED, (
                "Nothing usable came out of this phase. Later phases that rely on it will "
                "fail the same way, so fix the causes listed above before going on."
            )
        if result.status == Status.PARTIAL:
            return Safety.CAUTION, (
                "Part of this phase worked and part did not. What succeeded is in place and "
                "usable; what failed is listed above and is not, so avoid depending on it."
            )
        if result.status == Status.SKIPPED:
            why = result.skipped_reason or "nothing in it was selected for this run"
            return Safety.CAUTION, (
                f"This phase did not run ({why}), so nothing it provides has been put in "
                "place or checked here. That is fine if it was already done on this host."
            )
        return Safety.SAFE, "Everything this phase is responsible for is in place and verified."

    def verdict_lines(self, result: PhaseResult) -> list[Line]:
        """The verdict on the phase as a whole: the word first, then the arithmetic.

        Two lines rather than one because they answer different questions. The
        headline is what goes in the handover note ("phase 2 partially passed");
        the line under it is what makes that number checkable.
        """
        headline = f"PHASE {self.number} RESULT: {verdict_of(result.status)}"
        groups = result.groups()
        parts: list[str] = []
        if groups:
            # The subjects are the result, so they are what the verdict counts.
            # The step total keeps its place at the end of the line, because
            # "13 servers, 88 steps" is also how long this took and why.
            tally = result.group_tally()
            noun = self.group_noun
            parts.append(f"{tally['passed']} of {len(groups)} {noun}(s) passed")
            parts.append(f"{tally['skipped']} skipped")
            parts.append(f"{tally['failed']} failed")
            parts.append(f"{len(result.steps)} step(s) in total")
        elif result.steps:
            tally = result.tally()
            parts.append(f"{tally['passed']} of {len(result.steps)} step(s) passed")
            parts.append(f"{tally['skipped']} skipped")
            parts.append(f"{tally['failed']} failed")
        elif result.skipped_reason:
            parts.append(result.skipped_reason)
        if result.duration:
            parts.append(format_duration(result.duration))
        if self.ctx.dry_run:
            parts.append("dry run")
        sep = f"  {self.glyph('sep')}  "
        return [
            Line(headline, (*self.verdict_style(result), "bold")),
            Line(sep.join(parts), ("dim",)),
        ]

    @staticmethod
    def verdict_style(result: PhaseResult) -> tuple[str, ...]:
        return styles_for(result.status, ("bold",))

    def glyph(self, name: str) -> str:
        return self.console.glyphs.get(name, "")

    # -- helpers available to subclasses --------------------------------------

    def report_step(
        self,
        result: PhaseResult,
        name: str,
        status: str,
        detail: str = "",
        *,
        tag: str = "",
        tag_styles: tuple[str, ...] = (),
        **kwargs: object,
    ):
        """Record a step and echo it to the console in one call.

        ``tag``/``tag_styles`` are presentation only -- what the step *was* is
        already in ``data``, so the machine-readable report does not depend on
        how the terminal happened to be decorated. ``group`` (passed through to
        :meth:`~...core.report.PhaseResult.add`) files the step under the
        subject it is about, which is what the end-of-phase summary groups by.
        """
        step = result.add(name, status, detail, **kwargs)  # type: ignore[arg-type]
        self.console.status(status, name, detail, tag=tag, tag_styles=tag_styles)
        return step

    def setting_sources(self, dotted: str) -> list[str]:
        """Every way the operator could supply ``dotted``, in plain words.

        Read from the ``[sources]`` table rather than written out here, so the
        message cannot drift from the ``${VAR}`` chains in the packaged
        defaults that actually do the reading.
        """
        table = self.settings.dict_("sources")
        entry = table.get(dotted)
        if entry is None:
            # ``provision.vertex.model_aliases.opus`` and its two siblings on
            # the other backends are one answer, not three.
            entry = table.get(".".join(dotted.split(".")[-2:]))
        if not isinstance(entry, dict):
            entry = {}

        lines: list[str] = []
        flag = str(entry.get("flag", "") or "").strip()
        if flag:
            lines.append(f"Pass it on the command line: {flag} <value>")
        names = [str(n) for n in entry.get("env", []) if str(n).strip()]
        if names:
            lines.append(
                "Or set one of these in the environment, or in a file given to --env-file: " + ", ".join(names)
            )
        lines.extend(f"Or: {item}" for item in entry.get("also", []) if str(item).strip())
        lines.append(f"Or set it directly: --set {dotted}=<value>")
        return lines

    def setting_origin(self, dotted: str, value: str) -> str:
        """Which layer ``dotted`` actually got its value from, in a word or two.

        Derived from the same ``[sources]`` table as :meth:`setting_sources`
        and from the record :class:`Settings` keeps of what it applied, so it
        stays true to the precedence order without restating it: the command
        line, then an environment variable (from the real environment or an
        ``--env-file``, which are the same thing by the time we look), and
        otherwise the configured default.  The settings file being rewritten
        cannot appear here: a value of its own that reached the resolver would
        have had to come through a variable, and those are taken back out
        beforehand (:func:`~initialize_claude_ai.core.config.demote_echo`).
        Somewhere it *is* consulted, the caller names it itself -- as
        :meth:`~...ProvisionPhase._vertex_project` does.

        Returns an empty string when there is nothing worth saying -- a value
        the operator did not have to know about anyway.
        """
        if f"cli:{dotted}" in self.settings.sources:
            entry = self.settings.dict_("sources").get(dotted) or {}
            flag = str(entry.get("flag", "") or "").strip() if isinstance(entry, dict) else ""
            return flag or f"--set {dotted}"
        for source in self.settings.sources:
            if source.startswith("env:") and source.endswith(f"->{dotted}"):
                return source[len("env:") : -len(f"->{dotted}")]

        entry = self.settings.dict_("sources").get(dotted)
        names = [str(n) for n in entry.get("env", [])] if isinstance(entry, dict) else []
        for name in names:
            if self.settings.environ.get(name, "") == value:
                return name
        return "default" if value else ""

    def require(
        self,
        result: PhaseResult,
        name: str,
        value: object,
        *,
        setting: str,
        why: str,
        remediation: Iterable[str] = (),
    ) -> str:
        """Return ``value``, or stop the phase because there is no such value.

        For the coordinates that have no honest default and cannot be guessed:
        the project Vertex bills, the region Bedrock lives in. Every layer has
        already been consulted by the time this is called -- the flag, the
        config files, the environment and any ``--env-file`` -- so an empty
        value here means there is genuinely nothing to use.

        Inventing something plausible would be the worse outcome. A wrong
        project bills somebody else and then fails with a permission error
        naming a project the operator never chose, which is a far harder thing
        to debug than being told, here, that it was never set. So the step is
        failed, every source that could have supplied it is listed, and the
        phase stops with the whole run failed behind it.
        """
        text = str(value or "").strip()
        if text:
            return text
        self.report_step(
            result,
            name,
            Status.FAILED,
            why,
            remediation=[*remediation, *self.setting_sources(setting)],
            error=MissingSettingError.code,
        )
        raise MissingSettingError(f"{why} ({setting})")


_REGISTRY: dict[str, type[Phase]] = {}


def register(cls: type[Phase]) -> type[Phase]:
    _REGISTRY[cls.slug] = cls
    return cls


def all_phases() -> list[type[Phase]]:
    from . import (  # noqa: F401
        phase1_install,
        phase2_authenticate,
        phase3_provision,
        phase4_mcp_auto_discovery,
    )

    return sorted(_REGISTRY.values(), key=lambda c: c.number)


def select(tokens: Iterable[str]) -> list[type[Phase]]:
    """Resolve ``--phases`` tokens (numbers, slugs, ranges, 'all') to classes."""
    available = all_phases()
    by_number = {str(cls.number): cls for cls in available}
    by_slug = {cls.slug: cls for cls in available}
    chosen: list[type[Phase]] = []

    def add(cls: type[Phase]) -> None:
        if cls not in chosen:
            chosen.append(cls)

    for raw in tokens:
        token = str(raw).strip().lower()
        if not token:
            continue
        if token == "all":
            for cls in available:
                add(cls)
            continue
        if "-" in token and all(part.strip().isdigit() for part in token.split("-", 1)):
            low, high = (int(p) for p in token.split("-", 1))
            for cls in available:
                if low <= cls.number <= high:
                    add(cls)
            continue
        cls = by_number.get(token) or by_slug.get(token)
        if cls is None:
            known = ", ".join(f"{c.number}={c.slug}" for c in available)
            raise InitializeClaudeAiError(
                f"unknown phase {raw!r}",
                remediation=f"Known phases: {known}, or 'all'.",
            )
        add(cls)
    return sorted(chosen, key=lambda c: c.number)
