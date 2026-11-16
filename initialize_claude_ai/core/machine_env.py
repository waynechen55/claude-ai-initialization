"""One writer for every per-machine variable: each place a new terminal's Claude Code reads, made right.

A value that belongs to one machine -- the CA bundle Claude Code needs to trust
an internal root, a runtime setting, a raised start-up wait -- reached only
Claude Code's ``settings.json``, by three routines that agreed on nothing. A
Claude Code started from a login shell, or a server it launches, reads the
shell profiles too; a line there can undo the setting: openqa-example-net failed
on another machine with ``UNABLE_TO_GET_ISSUER_CERT_LOCALLY``, and the
operator's ``~/.bashrc`` and ``~/.profile`` both exported a
``NODE_EXTRA_CA_CERTS`` naming a deleted ``/tmp`` file, which Node ignores
with a warning.

:class:`MachineEnv` brings every place (``[machine_env] places``) to the same
end state, whatever it held:

* a line commented out is uncommented (and corrected if its value is wrong);
* a missing line is added -- in this program's marked block;
* a wrong line (``valid`` says the value does not do its job) is corrected in
  place, also when another tool wrote it;
* a correct line is recognised and left alone.

The last assignment in a file is the one that counts, and a later file can
undo an earlier one, so afterwards a fresh login shell and a fresh interactive
shell are asked what they end up with (``effective_shells``). Every place and
its outcome is logged and said on every run, whether anything changed or not
-- never in ``.mcp.json``. A file is backed up before it changes; a place the
run may not write (``/etc/profile.d`` without root) is reported as a step for
the operator, with the exact line.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Sequence

from .fileio import write_private
from .logging_setup import get_logger

log = get_logger("machine_env")

#: What became of one place.
ADDED = "added"
UNCOMMENTED = "uncommented"
CORRECTED = "corrected"
ALREADY_CORRECT = "already correct"
NOT_PERMITTED = "not permitted"
COULD_NOT_WRITE = "could not write"
WOULD_CHANGE = "would change (dry run)"

#: Fallbacks for ``[machine_env]``, equal to the packaged values.
PLACES = ("settings", "~/.bashrc", "~/.profile", "/etc/profile.d/initialize-claude-ai.sh")
PLACES_IF_PRESENT = ("~/.bash_profile", "~/.bash_login", "~/.zshrc", "~/.zprofile")
#: Startup files read by a new terminal, scanned for lines that already set a
#: variable: each one found is a place too. Another installer had written
#: /etc/profile.d/node_extra_ca_certs.sh exporting a deleted /tmp bundle, which
#: a login shell read after everything this program would have written.
SCAN = (
    "/etc/profile",
    "/etc/profile.local",
    "/etc/bash.bashrc",
    "/etc/bash.bashrc.local",
    "/etc/profile.d/*.sh",
    "~/.bash_profile",
    "~/.bash_login",
    "~/.profile",
    "~/.bashrc",
    "~/.zshrc",
    "~/.zprofile",
)
BLOCK_BEGIN = "# >>> initialize-claude-ai: per-machine values (managed; see its log) >>>"
BLOCK_END = "# <<< initialize-claude-ai: per-machine values <<<"
BACKUP_SUFFIX = "%Y%m%d-%H%M%S"
EFFECTIVE_SHELLS = {
    "a new login shell": ["bash", "-lc", "{probe}"],
    "a new interactive shell": ["bash", "-ic", "{probe}"],
}
MARKER = "__ICA_VALUE__"

SETTINGS = "settings"


@dataclass
class PlaceOutcome:
    """One place and what became of it."""

    place: str
    outcome: str
    line: int = 0
    was: str = ""
    why_wrong: str = ""
    owner: str = ""
    detail: str = ""

    def words(self, variable: str) -> str:
        where = self.place + (f" line {self.line}" if self.line else "")
        text = f"{where}: {self.outcome}"
        if self.outcome in (CORRECTED, UNCOMMENTED) and self.was:
            text += f" (was {self.was!r}" + (f", {self.why_wrong}" if self.why_wrong else "") + ")"
        if self.owner:
            text += f" -- {self.owner}"
        if self.detail:
            text += f" -- {self.detail}"
        return text


@dataclass
class MachineValue:
    """One variable brought to one value everywhere, and what each place and shell said."""

    variable: str
    value: str
    why: str
    places: list[PlaceOutcome] = field(default_factory=list)
    shells: dict[str, str] = field(default_factory=dict)
    shells_wrong: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.shells_wrong and not any(p.outcome in (COULD_NOT_WRITE,) for p in self.places)

    def lines(self) -> list[str]:
        out = [f"{self.variable}={self.value} -- {self.why}"]
        out += [f"  {p.words(self.variable)}" for p in self.places]
        for shell, value in self.shells.items():
            mark = (
                "as intended" if shell not in self.shells_wrong else "NOT as intended -- something else sets it later"
            )
            out.append(f"  {shell} ends with {self.variable}={value or '(unset)'}: {mark}")
        return out

    def to_dict(self) -> dict[str, Any]:
        return {
            "variable": self.variable,
            "value": self.value,
            "why": self.why,
            "places": [p.__dict__ for p in self.places],
            "shells": dict(self.shells),
            "shells_wrong": list(self.shells_wrong),
        }


_RECORD = "machine_env_record"


class MachineEnv:
    """The writer. ``MachineEnv(ctx).apply(variable, value, why, valid=...)``."""

    def __init__(self, ctx: Any) -> None:
        self.ctx = ctx
        self.settings = ctx.settings

    @property
    def config(self) -> Any:
        return self.settings.section("machine_env")

    @property
    def enabled(self) -> bool:
        return self.config.bool_("enabled", True)

    # -- where ------------------------------------------------------------------

    def places(self, variable: str = "") -> list[str]:
        """The places, in order: the configured ones, those of ``places_if_present`` that exist, and every
        startup file (``scan``) that already assigns ``variable`` -- commented out or not."""
        import glob

        out = [str(p) for p in self.config.str_list("places", list(PLACES)) if str(p).strip()]
        seen = {os.path.realpath(os.path.expanduser(p)) for p in out if p != SETTINGS}
        for extra in self.config.str_list("places_if_present", list(PLACES_IF_PRESENT)):
            real = os.path.realpath(os.path.expanduser(str(extra)))
            if extra and os.path.isfile(real) and real not in seen:
                out.append(str(extra))
                seen.add(real)
        if variable:
            assigns = re.compile(rf"^\s*(#+\s*)?(export\s+)?{re.escape(variable)}=", re.MULTILINE)
            for pattern in self.config.str_list("scan", list(SCAN)):
                for path in sorted(glob.glob(os.path.expanduser(str(pattern)))):
                    real = os.path.realpath(path)
                    if real in seen or not os.path.isfile(real):
                        continue
                    try:
                        text = Path(real).read_text(encoding="utf-8", errors="replace")
                    except OSError:
                        continue
                    if assigns.search(text):
                        out.append(path)
                        seen.add(real)
        return out

    def exported_at(self, variable: str) -> list[str]:
        """``file:line`` for every startup file (``scan``) line that exports ``variable`` -- live lines only."""
        import glob

        if not variable:
            return []
        assigns = re.compile(rf"^\s*(export\s+)?{re.escape(variable)}=")
        found: list[str] = []
        seen: set[str] = set()
        for pattern in self.config.str_list("scan", list(SCAN)):
            for path in sorted(glob.glob(os.path.expanduser(str(pattern)))):
                real = os.path.realpath(path)
                if real in seen or not os.path.isfile(real):
                    continue
                seen.add(real)
                try:
                    lines = Path(real).read_text(encoding="utf-8", errors="replace").splitlines()
                except OSError:
                    continue
                found.extend(f"{path}:{number}" for number, line in enumerate(lines, 1) if assigns.match(line))
        return found

    def _settings_path(self) -> Path:
        from ..provision.settings_writer import settings_path

        return settings_path(self.ctx)

    # -- the work ---------------------------------------------------------------

    def apply(self, variable: str, value: str, why: str, *, valid: Callable[[str], str] | None = None) -> MachineValue:
        """Bring ``variable`` to ``value`` in every place; ``valid(v)`` says why a value is wrong ("" when right).

        A value other than ``value`` that ``valid`` accepts is still corrected
        to ``value``: the caller chose ``value`` knowing what is there (a
        bundle that already holds the root is kept by passing it as ``value``).
        """
        record = MachineValue(variable=variable, value=value, why=why)
        if not self.enabled or not variable:
            return record
        for place in self.places(variable):
            try:
                if place == SETTINGS:
                    record.places.append(self._settings(variable, value))
                else:
                    record.places.append(self._shell_file(place, variable, value, valid))
            except OSError as exc:
                record.places.append(PlaceOutcome(place=self._shown(place), outcome=COULD_NOT_WRITE, detail=str(exc)))
        if self.config.bool_("check_effective", True) and not self.ctx.dry_run:
            self._effective(record)
        self._say(record)
        self.ctx.state.setdefault(_RECORD, {})[variable] = record
        self.ctx.state.pop("fresh_shell_values", None)  # a new terminal now gets something else
        return record

    def _settings(self, variable: str, value: str) -> PlaceOutcome:
        from ..provision.settings_writer import SettingsWriter

        path = self._settings_path()
        shown = f"{path} (env)"
        writer = SettingsWriter(self.ctx, path)
        try:
            current = (writer.read().get("env") or {}).get(variable)
        except Exception as exc:  # noqa: BLE001 - an unreadable file is said, never overwritten
            return PlaceOutcome(place=shown, outcome=COULD_NOT_WRITE, detail=str(exc))
        if current == value:
            return PlaceOutcome(place=shown, outcome=ALREADY_CORRECT)
        if self.ctx.dry_run:
            return PlaceOutcome(place=shown, outcome=WOULD_CHANGE, was=str(current or ""))
        result = writer.apply({"env": {variable: value}})
        if getattr(result, "error", ""):
            return PlaceOutcome(place=shown, outcome=COULD_NOT_WRITE, detail=result.error)
        return PlaceOutcome(place=shown, outcome=ADDED if current is None else CORRECTED, was=str(current or ""))

    def _shell_file(self, place: str, variable: str, value: str, valid: Callable[[str], str] | None) -> PlaceOutcome:
        path = Path(os.path.expanduser(place))
        shown = str(path)
        text = path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""
        lines = text.splitlines()
        assign = re.compile(
            rf"^(?P<indent>\s*)(?P<hash>#+\s*)?(?P<export>export\s+)?{re.escape(variable)}=(?P<value>.*?)\s*$"
        )
        found = [(i, m) for i, m in ((i, assign.match(line)) for i, line in enumerate(lines)) if m]
        active = [(i, m) for i, m in found if not m.group("hash")]
        wanted = f"export {variable}={shlex.quote(value)}"

        def owner(index: int) -> str:
            return self._owner(lines, index)

        if active:
            # The last assignment is what the shell ends with.
            index, match = active[-1]
            current = _unquote(match.group("value"))
            if current == value:
                return PlaceOutcome(place=shown, outcome=ALREADY_CORRECT, line=index + 1)
            why_wrong = (valid(current) if valid else "") or "not the value this machine needs"
            lines[index] = f"{match.group('indent')}{wanted}"
            return self._write(
                path,
                lines,
                text,
                PlaceOutcome(
                    place=shown, outcome=CORRECTED, line=index + 1, was=current, why_wrong=why_wrong, owner=owner(index)
                ),
            )
        if found:
            index, match = found[-1]
            current = _unquote(match.group("value"))
            lines[index] = f"{match.group('indent')}{wanted}"
            fixed = current == value
            return self._write(
                path,
                lines,
                text,
                PlaceOutcome(
                    place=shown,
                    outcome=UNCOMMENTED if fixed else CORRECTED,
                    line=index + 1,
                    was=f"# {current}",
                    why_wrong=""
                    if fixed
                    else "commented out, and "
                    + ((valid(current) if valid else "") or "not the value this machine needs"),
                    owner=owner(index),
                ),
            )
        lines = self._into_block(lines, wanted)
        return self._write(path, lines, text, PlaceOutcome(place=shown, outcome=ADDED, line=lines.index(wanted) + 1))

    def _into_block(self, lines: list[str], wanted: str) -> list[str]:
        begin = self.config.str_("block_begin", BLOCK_BEGIN)
        end = self.config.str_("block_end", BLOCK_END)
        if begin in lines and end in lines[lines.index(begin) :]:
            at = lines.index(end, lines.index(begin))
            return [*lines[:at], wanted, *lines[at:]]
        tail = [""] if lines and lines[-1].strip() else []
        return [*lines, *tail, begin, wanted, end]

    def _owner(self, lines: Sequence[str], index: int) -> str:
        """Who wrote the line, in words: this program's block, or the comment heading another tool's lines."""
        begin = self.config.str_("block_begin", BLOCK_BEGIN)
        end = self.config.str_("block_end", BLOCK_END)
        for i in range(index, -1, -1):
            if lines[i] == end and i != index:
                break
            if lines[i] == begin:
                return ""
        for i in range(index - 1, max(-1, index - 6), -1):
            line = lines[i].strip()
            if not line:
                break
            if line.startswith("#") and not re.match(r"#+\s*(export\s+)?[A-Za-z_]\w*=", line):
                return f"a line another tool wrote ({line.lstrip('#').strip()[:80]}), corrected in place"
        return ""

    def _write(self, path: Path, lines: list[str], before: str, outcome: PlaceOutcome) -> PlaceOutcome:
        after = "\n".join(lines) + "\n"
        if after == before:
            outcome.outcome = ALREADY_CORRECT
            return outcome
        if self.ctx.dry_run:
            outcome.detail = f"{outcome.outcome}, not written: dry run"
            outcome.outcome = WOULD_CHANGE
            return outcome
        parent = path.parent
        if not os.access(parent if parent.exists() else parent.parent, os.W_OK) or (
            path.exists() and not os.access(path, os.W_OK)
        ):
            outcome.detail = (
                f"this run may not write {path}; as root, add the line "
                f"`{lines[outcome.line - 1] if outcome.line else ''}` to it"
            )
            outcome.outcome = NOT_PERMITTED
            return outcome
        mode = path.stat().st_mode & 0o777 if path.exists() else 0o644
        # One backup per file per run, of the file as the run found it; none
        # of a file this run created.
        touched = self.ctx.state.setdefault("machine_env_touched", set())
        key = str(path.resolve()) if path.exists() else str(path)
        if path.exists() and key not in touched and self.config.bool_("backup", True):
            stamp = datetime.now().strftime(self.config.str_("backup_suffix", BACKUP_SUFFIX))
            backup = path.with_name(f"{path.name}.{stamp}.bak")
            write_private(backup, path.read_bytes(), mode=mode)
            outcome.detail = f"backed up to {backup}"
        parent.mkdir(parents=True, exist_ok=True)
        write_private(path, after, mode=mode)
        touched.add(str(path.resolve()))
        return outcome

    def _effective(self, record: MachineValue) -> None:
        """Ask a fresh login shell and a fresh interactive shell what they end with."""
        shells = self.config.dict_("effective_shells", EFFECTIVE_SHELLS) or EFFECTIVE_SHELLS
        probe = f"printf '\\n{MARKER}%s\\n' \"${{{record.variable}}}\""
        env = {k: v for k, v in os.environ.items() if k in ("HOME", "USER", "LOGNAME", "PATH", "LANG", "TERM", "SHELL")}
        for name, argv in shells.items():
            command = [str(a).replace("{probe}", probe) for a in (argv or [])]
            if not command:
                continue
            try:
                done = subprocess.run(
                    command,
                    capture_output=True,
                    text=True,
                    stdin=subprocess.DEVNULL,  # noqa: S603
                    timeout=self.config.float_("shell_timeout_seconds", 20.0),
                    env=env,
                )
            except (OSError, subprocess.TimeoutExpired):
                continue
            value = next(
                (line[len(MARKER) :] for line in reversed((done.stdout or "").splitlines()) if line.startswith(MARKER)),
                None,
            )
            if value is None:
                continue
            record.shells[name] = value
            if value != record.value:
                record.shells_wrong.append(name)

    def _say(self, record: MachineValue) -> None:
        lines = record.lines()
        for line in lines:
            log.info("%s", line)
        console = getattr(self.ctx, "console", None)
        if console is not None and self.config.bool_("say_on_console", True):
            for line in lines:
                try:
                    console.bullet(line.strip(), indent=4 if line.startswith("  ") else 2)
                except Exception:  # noqa: BLE001 - saying it must never cost the run
                    break

    @staticmethod
    def record(ctx: Any) -> dict[str, MachineValue]:
        """Every variable this run brought to a value, and what each place said (for the report)."""
        return dict(ctx.state.get(_RECORD, {}))


def _unquote(raw: str) -> str:
    text = raw.strip()
    try:
        parts = shlex.split(text, comments=True, posix=True)
    except ValueError:
        return text.strip("'\"")
    return parts[0] if len(parts) == 1 else text


__all__ = [
    "ADDED",
    "ALREADY_CORRECT",
    "CORRECTED",
    "COULD_NOT_WRITE",
    "MachineEnv",
    "MachineValue",
    "NOT_PERMITTED",
    "PlaceOutcome",
    "UNCOMMENTED",
    "WOULD_CHANGE",
]
