"""Making a failing entry work, before the stage that produced it is settled.

An entry that does not start is a defect to fix, not a finding to report -- and
the fixes are a short, knowable list, because each one is a *difference* between
the launch that was proven and the launch that was written.

Where this runs is the whole design. It used to run at publish, after every
stage had finished, which put it too late to do either of the two useful things:

* the **agent** could not help, because its turn was over;
* the **tier descent** could not respond, because it had already happened -- so
  an entry that could not be made to work still occupied its target's answer,
  and tiers 3 and 4 were never asked.

So it runs inside the stage loop instead, next to
:meth:`~.traditional.TraditionalPipeline.discard_unregistrable`, where "we found
a server" and "we can write this server down" are already asked together. A
candidate whose entry will not start is discarded there, the stage does not
settle, and the lower tiers get their turn without anything special being
arranged.

The order of remedies mirrors the tier policy itself -- the better-informed
party first:

1. **An agent**, when there is one -- the strategist, briefed with the entry,
   the failure, how the proven launch differs from it and what discovery knew.
   It can change the command, the environment, the interpreter or the files
   on disk, and a repair it proves becomes the candidate's launch.
2. **The deterministic repairs** below, which need no model and run in
   ``--no-agent`` too. Each is one known difference between our launch and the
   client's.
3. **Neither** -- the candidate is dropped, with everything that was tried
   recorded, and the target keeps looking.

Every repair is *re-proved* before the next is tried. A repair that is not
re-verified is a guess with extra steps, and a repair that did not help is
discarded rather than accumulated, so whatever finally ships was launched in
exactly that form.
"""

from __future__ import annotations

import copy
import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Mapping

from ..core import redact
from ..core.context import RunContext
from ..core.logging_setup import get_logger
from . import login_check
from .portability import ENTRY_VARIABLE, expand, search_path
from .types import Candidate


def _names_a_file(value: str) -> bool:
    """Is ``value`` the absolute path of a file that exists?"""
    text = str(value or "")
    if not text.startswith("/") or "\n" in text:
        return False
    try:
        return Path(text).is_file()
    except OSError:
        return False


#: What a candidate's launch is made of: the fields an agent's repair may change
#: and the writer builds the entry from. What it *is* -- name, provenance, the
#: handshake that proved it -- stays with the original.
LAUNCH_FIELDS = ("kind", "transport", "url", "headers", "command", "args", "env", "secret_env", "install")


def adopt_launch(candidate: Candidate, revised: Candidate) -> None:
    """Give ``candidate`` the launch of ``revised``, in place."""
    for name in LAUNCH_FIELDS:
        if hasattr(revised, name) and hasattr(candidate, name):
            setattr(candidate, name, copy.deepcopy(getattr(revised, name)))


#: ``${NAME:-}`` and nothing else -- the writer's slot for a read with no value.
OPTIONAL_SLOT = re.compile(r"\$\{[A-Za-z_][A-Za-z0-9_]*:-\}")


def _readable(seconds: float) -> str:
    """A duration a person can compare at a glance: "2 days", "40 minutes"."""
    for size, unit in ((86400.0, "day"), (3600.0, "hour"), (60.0, "minute")):
        if seconds >= size:
            count = int(seconds // size)
            return f"{count} {unit}{'s' if count != 1 else ''}"
    return f"{int(seconds)} seconds"


#: An absolute path mentioned in prose -- what a server writes when it cannot
#: read a file. Stops at whitespace, quotes and the punctuation that ends a
#: sentence, so a trailing full stop is not taken for part of the name.
_PATHS_IN_TEXT = re.compile(r"/[^\s\"\'<>,;)]+[^\s\"\'<>,;).]")

log = get_logger("mcp.healing")

#: Asked to repair a candidate whose entry will not start. Given the candidate,
#: the name it will be registered under and what went wrong; returns a revised
#: candidate, or ``None`` when it has nothing to offer. The agentic pipeline
#: supplies one; the deterministic pipeline does not, and falls straight to the
#: repairs below -- which is the fallback, expressed as an absent argument.
AgentRepair = Callable[[Candidate, str, str], "Candidate | None"]


@dataclass
class Healing:
    """What happened when an entry was examined, and possibly repaired."""

    ok: bool = False
    #: ``None`` when nothing could be launched here -- a remote entry, a dry
    #: run. Distinct from a failure: nobody asked, so nobody may conclude.
    tested: bool = True
    entry: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    #: Repairs that worked, in the order they were applied.
    applied: list[str] = field(default_factory=list)
    #: Everything tried and what came of it, including the ones that changed
    #: nothing. "We tried and failed" and "we did not try" are different
    #: reports, and the operator deciding what to do next needs the difference.
    attempts: list[str] = field(default_factory=list)
    #: The host files the entry depends on, with their size and age, gathered
    #: only when it could not be made to start. See :meth:`EntryDoctor.file_evidence`.
    files: list[str] = field(default_factory=list)
    #: How the launch that was proven differs from the entry as the client runs
    #: it -- variable names and directories, never values -- gathered only when
    #: it could not be made to start. See :meth:`EntryDoctor.launch_difference`.
    differences: list[str] = field(default_factory=list)

    @property
    def healed(self) -> bool:
        return self.ok and bool(self.applied)

    def to_dict(self) -> dict[str, Any]:
        return {
            "starts": None if not self.tested else self.ok,
            "repairs_applied": list(self.applied),
            "repairs_attempted": list(self.attempts),
            "error": redact.redact(self.error),
            "files": list(self.files),
            "differences": list(self.differences),
        }


class EntryDoctor:
    """Builds an entry, launches it as the client would, and repairs it.

    Shared deliberately. The stage loops use it so a broken entry cannot settle
    a target, and the writer uses it again at publish as the last word -- one
    implementation, so the answer cannot differ between the moment a stage is
    judged and the moment the file is written.
    """

    def __init__(self, ctx: RunContext, writer: Any, verifier: Any) -> None:
        self.ctx = ctx
        self.writer = writer
        self.verifier = verifier
        self.config = ctx.settings.section("mcp.healing")
        #: A note the repair being tried wants on the candidate once it works.
        self._note = ""

    # -- the question ----------------------------------------------------------

    def examine(
        self,
        candidate: Candidate,
        name: str,
        *,
        agent_repair: AgentRepair | None = None,
        announce: bool = True,
    ) -> Healing:
        """Does this candidate produce an entry that starts? Fix it if not.

        ``mcp.healing.enabled = false`` turns off the *repairs*, not the
        question: the entry is still launched and its verdict still counts.
        Turning both off together made ``verify_as_written`` and the stage's
        start check go quiet without saying so.
        """
        entry = self.writer.entry_for(candidate, name)
        standins = self.standins(candidate, entry)
        shake = self.verifier.verify_as_written(entry, name, standins=standins, kept=candidate.launch_values)
        if shake is None:
            # A remote entry starts no process, and a dry run may start none.
            # Neither is a verdict, and neither may be reported as one.
            return Healing(ok=True, tested=False, entry=entry)
        if shake.ok:
            return Healing(ok=True, entry=entry)

        error = redact.redact((shake.error or "it exited without completing a handshake").strip())
        result = Healing(ok=False, entry=entry, error=error)
        if shake.login in login_check.REFUSED:
            # Its service refused the credential the entry passed. No repair to
            # the entry changes that -- only a different credential or account
            # does -- so say so instead of trying.
            result.attempts.append(
                f"not repaired: {login_check.in_words(shake.login)} ({shake.login_detail}); a repair to the "
                "entry cannot change what the service accepts"
            )
            return result
        if not self.config.bool_("enabled", True):
            result.attempts.append("repairs are off (mcp.healing.enabled = false)")
            result.differences = self.launch_difference(candidate, entry)
            result.files = self.file_evidence(entry, error)
            return result
        if announce:
            self.ctx.console.bullet(
                f"{name}: the entry does not start ({redact.redact(error)}); trying to repair it",
                indent=4,
            )
        log.info("%s: entry does not start (%s); healing", name, redact.redact(error))
        # Worked out once, before anyone is asked to repair it: the agent is
        # told the same gap the report will name.
        result.differences = self.launch_difference(candidate, entry)

        if agent_repair is not None and self.config.bool_("agent_first", True):
            healed = self._try_agent(candidate, name, result, agent_repair, announce)
            if healed is not None:
                return healed

        healed = self._try_repairs(candidate, name, result, announce)
        if healed is not None:
            return healed

        if agent_repair is not None and not self.config.bool_("agent_first", True):
            healed = self._try_agent(candidate, name, result, agent_repair, announce)
            if healed is not None:
                return healed

        result.files = self.file_evidence(result.entry, result.error)
        if announce:
            self.ctx.console.bullet(
                f"{name}: could not be repaired -- {'; '.join(result.attempts) or 'nothing to try'}",
                indent=4,
            )
            for line in [*result.differences, *result.files]:
                self.ctx.console.bullet(line, indent=6)
        log.info("%s: not repairable (%s)", name, "; ".join(result.attempts) or "nothing to try")
        for line in [*result.differences, *result.files]:
            log.info("%s: %s", name, line)
        return result

    @staticmethod
    def standins(candidate: Candidate, entry: Mapping[str, Any]) -> list[str]:
        """Variables to fill with the stand-in when launching ``entry``.

        Only for a server that did not start without its credential and did
        with a stand-in (:meth:`CandidateVerifier._credential_test`): its entry
        keeps the ``${NAME}`` slot, and the launch that proved it had a value
        there. One that started *without* the credential is written with
        ``${NAME:-}`` and is launched without it, as it was proven.
        """
        shake = candidate.handshake
        if shake is None or shake.ok or not shake.credential_pending:
            return []
        env = entry.get("env") or {}
        names: list[str] = []
        for key in shake.credential_pending:
            for match in ENTRY_VARIABLE.finditer(str(env.get(key, ""))):
                if match.group(2) is None and match.group(1) not in names:
                    names.append(match.group(1))
        return names

    def launch_difference(self, candidate: Candidate, entry: Mapping[str, Any]) -> list[str]:
        """How the proven launch and the entry differ, by name only.

        The trial inherits this run's whole environment and runs in the
        install sandbox; the client passes only the entry's ``env`` and
        ``mcp.verification.client_env.passthrough``, from its own directory.
        When an entry that was proven will not start, the cause is almost
        always in that gap -- and confluence.example.com's ``FileNotFoundError``
        was reported without saying which side of it held the missing file.
        Names and directories only: a value may be a credential.
        """
        if not self.config.bool_("report_differences", True) or not str(entry.get("command", "")).strip():
            return []
        declared = {str(k) for k in (entry.get("env") or {})}
        lines: list[str] = []
        trial_only = sorted(k for k, v in (candidate.env or {}).items() if str(v).strip() and k not in declared)
        if trial_only:
            lines.append(f"the proven launch set {', '.join(trial_only)}, which the entry does not declare")
        passthrough = set(self.ctx.settings.str_list("mcp.verification.client_env.passthrough", []))
        inherited_files = sorted(
            name
            for name, value in os.environ.items()
            if name not in declared and name not in passthrough and _names_a_file(value)
        )
        if inherited_files:
            lines.append(
                "the proven launch inherited "
                + ", ".join(inherited_files)
                + " from this run's environment, each naming a file, and a client passes none of "
                "them -- a server that needs one fails to find it"
            )
        where = getattr(self.verifier, "trial_dir", None)
        trial_dir = where() if callable(where) else ""
        if trial_dir and trial_dir != os.getcwd():
            lines.append(
                f"the proven launch ran in {trial_dir}; the client starts it from its own "
                f"directory ({os.getcwd()} here), so a relative path resolves differently"
            )
        return lines

    def file_evidence(self, entry: Mapping[str, Any], failure: str = "") -> list[str]:
        """The host files this entry needs, with their age and size.

        A bridge is a script plus the data it reads, and the commonest way one
        breaks is that somebody regenerated half of it. The evidence is right
        there in the filesystem -- a script modified today beside a manifest
        from yesterday -- and nobody looks, because nothing says to look.

        So when an entry cannot be made to start, the files it depends on are
        listed with their modification times, and a set whose timestamps do not
        agree is called out. That single line is what turns "Connection closed"
        into "these two were generated a day apart".

        Paths are taken from the entry *and from the failure text*, because a
        server that cannot use a file usually names it -- and the file it names
        is often one the entry never mentions. A bridge locates its own manifest
        beside itself, so the entry lists the script and nothing else; the
        complaint is the only place the manifest appears, and it is exactly the
        file whose age matters.

        Generic on purpose: it knows nothing about bridges or manifests, only
        that a server named a path and that paths living together are claiming
        to belong together.
        """
        if not self.config.bool_("report_files", True):
            return []
        seen: list[str] = []
        texts = [str(entry.get("command", "")), *[str(a) for a in entry.get("args") or []]]
        texts += [str(v) for v in (entry.get("env") or {}).values()]
        # ...and whatever the server itself pointed at when it refused.
        texts += _PATHS_IN_TEXT.findall(str(failure or ""))
        for text in texts:
            resolved = expand(text)
            if search_path(resolved) is not None:
                continue  # a list of places to look, not a file the entry needs
            if not resolved.startswith("/") or resolved in seen:
                continue
            seen.append(resolved)

        lines: list[str] = []
        by_directory: dict[str, list[tuple[str, float]]] = {}
        for path_text in seen:
            path = Path(path_text)
            try:
                stat = path.stat()
            except OSError:
                lines.append(f"{path_text} -- MISSING")
                continue
            when = datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M")
            lines.append(f"{path_text} -- {stat.st_size} bytes, modified {when}")
            by_directory.setdefault(str(path.parent), []).append((path.name, stat.st_mtime))

        # Only files that live *together* are compared. An interpreter from last
        # year beside a CA bundle from last month is not a stale pair, it is an
        # ordinary system; a script and the data it reads, in one directory,
        # written a day apart, is the thing worth saying. Sharing a directory is
        # the closest this can get to "generated as a set" without being told.
        limit = self.config.float_("files_agree_within_seconds", 3600)
        for directory, entries in sorted(by_directory.items()):
            if len(entries) < 2:
                continue
            spread = max(t for _, t in entries) - min(t for _, t in entries)
            if spread <= limit:
                continue
            names = ", ".join(sorted(n for n, _ in entries))
            lines.append(
                f"in {directory}: {names} were last written {_readable(spread)} apart. "
                "Files that sit together and are generated together should not; one of them "
                "is probably stale, and regenerating the set is likely the fix"
            )
        return lines

    # -- the agent -------------------------------------------------------------

    def _try_agent(
        self,
        candidate: Candidate,
        name: str,
        result: Healing,
        repair: AgentRepair,
        announce: bool = True,
    ) -> Healing | None:
        """Let the agent change whatever it thinks is wrong, then re-prove it.

        The agent gets first refusal because it can change more than any one
        rule here: given the entry, the failure and what discovery knew, it can
        fix an argument, a variable, an interpreter or a file a bridge wrote.
        What it may not do is decide whether the fix worked -- the revised
        candidate is launched like any other.
        """
        try:
            failure = "\n".join([result.error, *result.differences])
            revised = repair(candidate, name, failure)
        except Exception as exc:  # noqa: BLE001 - a failed repair must not kill the stage
            log.warning("%s: the agent repair raised %s", name, exc)
            result.attempts.append(f"agent: raised {type(exc).__name__}")
            return None
        if revised is None:
            result.attempts.append("agent: nothing offered")
            return None

        entry = self.writer.entry_for(revised, name)
        shake = self.verifier.verify_as_written(
            entry, name, standins=self.standins(revised, entry), kept=revised.launch_values
        )
        if shake is not None and shake.ok:
            result.attempts.append("agent: worked")
            if announce:
                self.ctx.console.bullet(f"{name}: the agent repaired it and it starts", indent=6)
            log.info("%s: repaired by the agent", name)
            # The candidate takes the launch that was proven, so every later
            # step -- the writer included -- builds this entry from it. Kept
            # only in the result, the repair was lost at publish: the writer
            # rebuilt the entry from the unrepaired candidate and refused it.
            adopt_launch(candidate, revised)
            return Healing(ok=True, entry=entry, applied=["agent"], attempts=result.attempts)
        why = redact.redact(getattr(shake, "error", "") or "still did not start")
        result.attempts.append(f"agent: {why}")
        if announce:
            self.ctx.console.bullet(f"{name}: the agent's repair did not help ({why})", indent=6)
        return None

    # -- the deterministic half ------------------------------------------------

    def strategies(self) -> dict[str, Callable[[Candidate, dict], "dict | None"]]:
        """Named repairs, so an installation can drop one or reorder them."""
        return {
            "declare_inherited_env": self._declare_inherited_env,
            "absolute_command": self._absolute_command,
            "absolute_args": self._absolute_args,
            "withdraw_optional_reads": self._withdraw_optional_reads,
            "isolate_user_config": self._isolate_user_config,
            "restore_path_defaults": self._restore_path_defaults,
            "drop_unset_setting": self._drop_unset_setting,
        }

    def _try_repairs(self, candidate: Candidate, name: str, result: Healing, announce: bool = True) -> Healing | None:
        available = self.strategies()
        for wanted in self.config.str_list(
            "repairs",
            [
                "declare_inherited_env",
                "absolute_command",
                "absolute_args",
                "withdraw_optional_reads",
                "isolate_user_config",
                "restore_path_defaults",
                "drop_unset_setting",
            ],
        ):
            strategy = available.get(wanted)
            if strategy is None:
                log.warning("unknown repair %r in mcp.healing.repairs", wanted)
                continue
            self._note = ""
            changed = strategy(candidate, result.entry)
            if changed is None:
                result.attempts.append(f"{wanted}: nothing to change")
                continue
            shake = self.verifier.verify_as_written(
                changed, name, standins=self.standins(candidate, changed), kept=candidate.launch_values
            )
            if shake is not None and shake.ok:
                result.attempts.append(f"{wanted}: worked")
                # A repair's note is the candidate's only once the repair is
                # proven; one that did not help must not leave a claim behind.
                if self._note:
                    candidate.add_note(self._note)
                if announce:
                    self.ctx.console.bullet(f"{name}: repaired by {wanted}", indent=6)
                log.info("%s: repaired by %s", name, wanted)
                return Healing(ok=True, entry=changed, applied=[wanted], attempts=result.attempts)
            why = redact.redact(getattr(shake, "error", "") or "still did not start")
            result.attempts.append(f"{wanted}: {why}")
        return None

    def _isolate_user_config(self, candidate: Candidate, entry: dict) -> dict | None:
        """Give the entry a configuration scope of its own (``[mcp.healing.isolation]``).

        For a server that reads the operator's own configuration -- a dotfile
        under ``HOME`` or ``XDG_CONFIG_HOME`` -- before its entry's, and is
        broken by it: osc-mcp took the operator's oscrc apiurl, a example.net API,
        over ``OSC_MCP_API`` and refused to start. The variables point at an
        empty directory of this server's own, written relocatable; the
        operator's files are never read for values and never edited.
        """
        if str(entry.get("command", "")).strip() == "":
            return None
        config = self.ctx.settings.section("mcp.healing.isolation")
        if not config.bool_("enabled", True):
            return None
        scopes = config.dict_("env", {"HOME": "", "XDG_CONFIG_HOME": ".config"})
        env = dict(entry.get("env") or {})
        if not scopes or all(str(env.get(k) or "").strip() for k in scopes):
            return None
        name = re.sub(r"[^A-Za-z0-9_.-]+", "-", candidate.label).strip("-") or "server"
        root = config.str_("directory", "${cfg:mcp.agent.workspace}/artifacts/isolated-config/{slug}")
        if "${" in root:
            return None
        directory = Path(root.replace("{slug}", name))
        roots = self.writer.path_roots()
        for variable, sub in scopes.items():
            path = directory / str(sub) if str(sub) else directory
            try:
                path.mkdir(parents=True, exist_ok=True)
            except OSError:
                return None
            env[variable] = self.writer.portable(str(path), roots, key=variable)
        self._note = (
            f"{', '.join(sorted(scopes))} point at a configuration scope of its own: the server reads the "
            "operator's own configuration before its entry's, and on this machine that configuration "
            "stopped it from starting"
        )
        return {**entry, "env": dict(sorted(env.items()))}

    def _drop_unset_setting(self, candidate: Candidate, entry: dict) -> dict | None:
        """Leave out a setting whose ``${NAME}`` nothing sets, so the server's own default applies.

        bugzilla-opensuse-org's entry carried ``BUGZILLA_AUTH_MODE: "${BUGZILLA_AUTH_MODE}"``
        with nothing exporting it; Claude Code passed the literal text and the
        server refused to start -- "not repairable" until now. A credential's
        slot is never dropped (it says what to export); only settings.
        """
        from .portability import ENTRY_VARIABLE
        from .secret_policy import is_secret_name

        env = dict(entry.get("env") or {})
        dropped = []
        for key, value in list(env.items()):
            text = str(value)
            if is_secret_name(str(key)) or key in candidate.secret_env:
                continue
            unset = [
                m.group(1)
                for m in ENTRY_VARIABLE.finditer(text)
                if m.group(2) is None and not os.environ.get(m.group(1)) and not is_secret_name(m.group(1))
            ]
            if unset:
                env.pop(key)
                dropped.append(key)
        if not dropped:
            return None
        self._note = f"{', '.join(dropped)} left out: nothing sets {'it' if len(dropped) == 1 else 'them'}, and the server's own default applies"
        return {**entry, "env": dict(sorted(env.items()))}

    def _restore_path_defaults(self, candidate: Candidate, entry: dict) -> dict | None:
        """Give this program's own path variables back their ``${NAME:-default}``.

        An entry naming ``${ICA_MCP_PYTHON}`` bare starts nowhere the variable
        is unset -- Claude Code listed build.opensuse.org's bridge as "Failed to
        connect" for exactly that. Only names this program defines
        (:meth:`McpWriter.program_variables`) and only where the default points
        at something that exists here; a missing one means the entry came from
        another machine, which a default cannot repair -- that is said instead.
        """
        known = self.writer.program_variables()
        if not known:
            return None
        missing: list[str] = []
        changed = False

        def restore(text: str) -> str:
            nonlocal changed

            def one(match: re.Match[str]) -> str:
                nonlocal changed
                variable, default = match.group(1), match.group(2)
                if default is not None or variable not in known or os.environ.get(variable):
                    return match.group(0)
                if not Path(known[variable]).exists():
                    missing.append(f"{variable} ({known[variable]})")
                    return match.group(0)
                changed = True
                return f"${{{variable}:-{known[variable]}}}"

            return ENTRY_VARIABLE.sub(one, text)

        revised = dict(entry)
        if "command" in revised:
            revised["command"] = restore(str(revised["command"]))
        if revised.get("args"):
            revised["args"] = [restore(str(a)) for a in revised["args"]]
        if revised.get("env"):
            revised["env"] = {k: restore(str(v)) for k, v in revised["env"].items()}
        if missing:
            candidate.add_note(
                f"the entry names {', '.join(missing)}, which is not on this machine -- it was probably "
                "copied from another one; regenerate it here rather than repair it"
            )
        if not changed:
            return None
        self._note = "this program's own path variables were given back their defaults"
        return revised

    def _declare_inherited_env(self, candidate: Candidate, entry: dict) -> dict | None:
        """Write down a variable the proven launch had and the entry lacks.

        The commonest repair by far. Our trial inherits the operator's
        environment; a client passes only what the entry declares. So a server
        can be proven here on a variable nobody wrote down and fail there for
        want of it -- a defect in the *entry*, and one we hold the answer to,
        because the candidate knew the value all along.
        """
        if str(entry.get("command", "")).strip() == "":
            return None
        declared = {str(k) for k in (entry.get("env") or {})}
        missing = {
            key: value for key, value in (candidate.env or {}).items() if key not in declared and str(value).strip()
        }
        if not missing:
            return None
        changed = json.loads(json.dumps(entry))
        env = dict(changed.get("env") or {})
        roots = self.writer.path_roots()
        for key, value in missing.items():
            env[key] = self.writer.portable(str(value), roots, key=key)
        changed["env"] = dict(sorted(env.items()))
        # Re-assert the guarantee: a repair copies variables forward, and one of
        # them could be the very thing the weakening filter exists to remove.
        changed["env"] = self.writer._without_tls_weakening(changed["env"], candidate)
        if not changed["env"]:
            changed.pop("env", None)
        return changed

    def _absolute_command(self, candidate: Candidate, entry: dict) -> dict | None:
        """Pin a bare command, which the client resolves through its own PATH."""
        command = str(entry.get("command", "")).strip()
        if not command or command.startswith(("/", "$")):
            return None
        resolved = self.ctx.runner.which(command)
        if not resolved:
            return None
        changed = json.loads(json.dumps(entry))
        changed["command"] = self.writer.portable(str(resolved), self.writer.path_roots())
        return changed

    def _absolute_args(self, candidate: Candidate, entry: dict) -> dict | None:
        """Absolutise a relative path against the directory we proved it in."""
        args = [str(a) for a in entry.get("args") or []]
        where = getattr(self.verifier, "trial_dir", None)
        proven_in = Path(where()) if callable(where) else Path.cwd()
        rewritten, moved = [], False
        for arg in args:
            if not arg or arg.startswith(("-", "/", "$")):
                rewritten.append(arg)
                continue
            path = proven_in / arg
            if not path.exists():
                rewritten.append(arg)
                continue
            rewritten.append(self.writer.portable(str(path.resolve()), self.writer.path_roots()))
            moved = True
        if not moved:
            return None
        changed = json.loads(json.dumps(entry))
        changed["args"] = rewritten
        return changed

    def _withdraw_optional_reads(self, candidate: Candidate, entry: dict) -> dict | None:
        """Drop the ``${NAME:-}`` slots, for a server that minds set-but-empty.

        The writer names every variable a launched script reads, and one it has
        no value for is written ``${NAME:-}`` -- empty while unset. Almost every
        script treats empty and unset alike (``os.environ.get(X) or ...``); one
        that tests ``X in os.environ`` does not, and would start differently
        from the launch that was proven. If the entry as written will not start,
        this withdraws exactly those slots and lets the start check decide.

        Only values that are *exactly* an empty-default reference are touched:
        a slot with a default is the script's own value, and a bare ``${NAME}``
        is a credential the server asked for.

        A credential slot is never withdrawn: it is the only place the login
        can go, and the start check passes without it because nobody logs in.
        gitlab-example-net lost ``HEADERS`` this way and every call went out as
        anybody. One the server takes in a form is rewritten in that form
        (:func:`.api_kinds.credential_in_taken_form`) -- valid with the variable
        empty -- and otherwise kept, for the start failure to be reported.
        (``mcp.healing.keep_credential_slots``.)
        """
        env = dict(entry.get("env") or {})
        optional = [key for key, value in env.items() if OPTIONAL_SLOT.fullmatch(str(value))]
        if not optional:
            return None
        changed = json.loads(json.dumps(entry))
        kept: list[str] = []
        if self.config.bool_("keep_credential_slots", True):
            from . import api_kinds
            from .verifier import CandidateVerifier

            reformed = api_kinds.credential_in_taken_form(
                self.ctx, candidate, env, candidate.name, CandidateVerifier._host_of(candidate)
            )
            kept = [key for key in optional if key not in reformed and self._credential_slot(candidate, key)]
            optional = [key for key in optional if key not in reformed and key not in kept]
            if not optional and not reformed:
                return None
        for key in optional:
            env.pop(key, None)
        if env:
            changed["env"] = dict(sorted(env.items()))
        else:
            changed.pop("env", None)
        self._note = (
            (
                f"{', '.join(sorted(optional))} withdrawn from env: this server would not start "
                "with them set to an empty string. The server reads them from the environment "
                "Claude Code is started from, so export them there if it needs them."
            )
            if optional
            else ""
        )
        if kept:
            self._note = " ".join(
                filter(
                    None,
                    [
                        self._note,
                        (
                            f"{', '.join(sorted(kept))} kept: a credential slot is never withdrawn, "
                            "since it is the only place the login can go."
                        ),
                    ],
                )
            )
        return changed

    def _credential_slot(self, candidate: Candidate, key: str) -> bool:
        """Does ``key`` carry a login -- one the candidate declared, a secret name, or an adapter's credential key?"""
        from . import api_kinds
        from .secret_policy import is_secret_name

        if key in candidate.secret_env or is_secret_name(key):
            return True
        adapter = api_kinds.adapter_of(self.ctx, candidate)
        return bool(adapter and key in adapter.credential_env)


__all__ = ["AgentRepair", "EntryDoctor", "Healing", "LAUNCH_FIELDS", "adopt_launch"]
