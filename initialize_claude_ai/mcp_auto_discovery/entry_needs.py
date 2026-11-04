"""What the MCP servers Claude Code starts from here need on this machine -- read by phase 1.

``.mcp.json`` is written to be copied, so it travels to machines this program
never ran a discovery on. Two things then go missing, and nothing noticed:

* **a program an entry runs.** ``github-com`` launches ``github-mcp-server``,
  which the new machine never had; a client bridge calls ``osc`` for every
  request. Phase 4 installs what a server needs while it trials one, but
  entries nobody asks phase 4 about are never trialled. :func:`missing_programs`
  lists them, so phase 1 installs them from its catalog (or the client's own
  ``install`` steps) and names the rest.
* **this machine's path for a program variable.** Every absolute path is
  written ``${ICA_MCP_UVX:-/root/.local/bin/uvx}`` -- the default is a fact
  about the machine that wrote it. On the next machine nothing set
  ``ICA_MCP_UVX``, the default did not exist, and Claude Code reported
  ``ENOENT: posix_spawn '${ICA_MCP_UVX}'`` for bugzilla-opensuse-org and
  confluence-example-com while ``uvx`` sat in ``/usr/bin``. :func:`machine_paths`
  finds the same program here and puts the variable where a new terminal's
  Claude Code reads it.

Entries are read once, through :func:`.existing.loaded_entries` -- the readers
phase 4 uses -- and expanded as a new terminal's Claude Code expands them
(:func:`.claude_check.fresh_environ`).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from ..core.context import RunContext
from ..core.logging_setup import get_logger
from .portability import ENTRY_VARIABLE, expand

log = get_logger("mcp.entry_needs")

#: The roles a program plays for an entry.
LAUNCH, CALL = "launch", "call"


@dataclass
class Need:
    """A program a registered entry runs that this machine does not have."""

    server: str
    where: str
    program: str
    #: ``launch`` -- the entry's command; ``call`` -- run by the server for every request.
    role: str
    #: A client's own install steps (``[mcp.discovery.clients.<name>] install``), for a ``call``.
    ways: list[list[str]] = field(default_factory=list)
    address: str = ""

    def words(self) -> str:
        what = "starts with" if self.role == LAUNCH else "runs, for every call,"
        return f"{self.server} ({self.where}) {what} {self.program}"


@dataclass
class MachinePaths:
    """What :func:`machine_paths` found and did."""

    #: Set here: variable -> this machine's path.
    set: dict[str, str] = field(default_factory=dict)
    #: Already set where a new terminal's Claude Code reads it: left alone.
    kept: list[str] = field(default_factory=list)
    #: Variable -> why no path here serves every entry that reads it.
    unresolved: dict[str, str] = field(default_factory=dict)
    #: Variable -> {entry: the path it names that does not exist here} -- only the entries it fails.
    missing: dict[str, dict[str, str]] = field(default_factory=dict)
    #: Variable -> the entries that read it.
    users: dict[str, list[str]] = field(default_factory=dict)
    #: Entry -> (status, words) from Claude Code itself, after the change.
    checks: dict[str, tuple[str, str]] = field(default_factory=dict)
    #: Where the values were written.
    places: list[str] = field(default_factory=list)


def _fresh(ctx: RunContext) -> dict[str, str]:
    from .claude_check import fresh_environ

    try:
        return dict(fresh_environ(ctx))
    except Exception as exc:  # noqa: BLE001 - without it, this shell's environment is the nearest answer
        log.debug("fresh environment not read: %s", exc)
        return dict(os.environ)


def _entries(ctx: RunContext) -> dict[str, tuple[dict[str, Any], str]]:
    from .existing import loaded_entries

    return loaded_entries(ctx)


def _strings(entry: Mapping[str, Any]) -> list[str]:
    """The strings of an entry Claude Code expands: command, args, env values."""
    texts = [str(entry.get("command") or "")]
    texts += [str(a) for a in entry.get("args") or [] if a is not None]
    texts += [str(v) for v in (entry.get("env") or {}).values() if v is not None]
    return [t for t in texts if t]


def _program(argv0: str) -> str:
    return os.path.basename(str(argv0).strip())


def _runnable(ctx: RunContext, argv0: str) -> bool:
    """Can ``argv0`` run here -- not merely exist (:func:`.launch_form.runnable`)?"""
    from .launch_form import runnable

    if not argv0 or "${" in argv0:
        return False
    found = argv0 if os.sep in argv0 else ctx.runner.which(argv0)
    return bool(found) and not runnable(str(found), ctx.settings, which=ctx.runner.which)


def _with_program_variables(ctx: RunContext, writer: Any, text: str, environ: Mapping[str, str]) -> dict[str, str]:
    """``environ`` plus what each of this program's variables in ``text`` stands for here, where it is unset."""
    prefix = ctx.settings.str_("mcp.registration.path_variable_prefix", "ICA_MCP")
    values = dict(environ)
    for match in ENTRY_VARIABLE.finditer(text):
        variable = match.group(1)
        if prefix and variable.startswith(prefix + "_") and not values.get(variable):
            local = writer.resolve_program_variable(variable)
            if local:
                values[variable] = local
    return values


def missing_programs(ctx: RunContext) -> list[Need]:
    """Programs the registered entries run that are on no path here -- not at the written path, not on PATH.

    A command whose written path is missing while the program is on PATH, or
    while a program variable in it stands for a path here, is not missing:
    that is :func:`machine_paths`' business -- the two agree by asking the
    same resolver (:func:`_with_program_variables`).
    """
    from .client_bridge import requirements
    from .types import Candidate
    from .writer import McpWriter

    environ = _fresh(ctx)
    writer = McpWriter(ctx)
    needs: list[Need] = []
    seen: set[tuple[str, str]] = set()
    for name, (entry, where) in _entries(ctx).items():
        if entry.get("url") or not entry.get("command"):
            continue
        argv0 = expand(str(entry.get("command")), environ)
        program = _program(argv0)
        found: list[Need] = []
        local = expand(
            str(entry.get("command")), _with_program_variables(ctx, writer, str(entry.get("command")), environ)
        )
        if (
            program
            and "${" not in program
            and not _runnable(ctx, argv0)
            and not _runnable(ctx, local)
            and not ctx.runner.which(program)
        ):
            found.append(Need(name, where, program, LAUNCH))
        candidate = Candidate(
            kind="stdio",
            name=name,
            command=[argv0],
            args=[expand(str(a), environ) for a in entry.get("args") or []],
            env={str(k): str(v) for k, v in (entry.get("env") or {}).items()},
        )
        for called, ways in requirements(candidate, ctx.settings).items():
            if called and not ctx.runner.which(called):
                found.append(Need(name, where, _program(called), CALL, [list(w) for w in ways]))
        for need in found:
            if (need.server, need.program) in seen:
                continue
            seen.add((need.server, need.program))
            need.address = writer.address_of_entry(name, entry)
            needs.append(need)
    return needs


def repair_private_launchers(ctx: RunContext, *, apply: bool) -> list[str]:
    """Rewrite every project-file entry launched through an installer's private launcher to its portable form.

    ``.mcp.json`` travels; an installer's environment does not. On the machine
    it was copied to, bugzilla-opensuse-org ran ``.../uv/tools/mcp-bugzilla/bin/mcp-bugzilla``
    and could never start. Phase 1 rewrites it to ``uvx mcp-bugzilla``
    (:meth:`.writer.McpWriter.launcher_repair`) when ``uvx`` is here; the
    changes, one line each. Nothing is written in a dry run.
    """
    import json as _json

    from .writer import McpWriter

    writer = McpWriter(ctx)
    path = writer.json_path
    try:
        document = _json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    except (OSError, ValueError):
        return []
    servers = dict(document.get("mcpServers") or {}) if isinstance(document, Mapping) else {}
    changes: list[str] = []
    for name in sorted(servers):
        entry = dict(servers[name] or {})
        change = writer.launcher_repair(entry)
        if change:
            servers[name] = entry
            changes.append(f"{name}: {change}")
    if changes and apply and not ctx.dry_run:
        document["mcpServers"] = servers
        writer._atomic_write(path, _json.dumps(document, indent=2, sort_keys=False) + "\n")
    return changes


def recheck_command(address: str) -> str:
    """The run that finds (or replaces) the server for ``address``; every entry's when it is not known."""
    from ..cli import PROGRAM  # the CLI imports the phases; import late

    return f"{PROGRAM} run --phases 4 --mcp-recheck {address or 'all'}"


def _works(text: str, environ: Mapping[str, str]) -> bool:
    """Does ``text``, expanded with ``environ``, name something that exists -- when it names a path at all?"""
    expanded = expand(text, environ)
    if "${" in expanded:
        return False
    if not expanded.startswith("/"):
        return True
    return Path(expanded).exists()


def machine_paths(ctx: RunContext, *, apply: bool) -> MachinePaths:
    """Give this machine's path to each program variable a registered entry reads and cannot resolve here.

    Only this program's own variables (``mcp.registration.path_variable_prefix``),
    only when the value the entry would get here names nothing that exists,
    and only with a path that serves *every* entry reading the variable --
    found the way this program names them (:meth:`.writer.McpWriter.resolve_program_variable`:
    a configured root, a launcher, a binary on PATH). A variable already set
    where a new terminal's Claude Code reads it is never changed. Values go
    to ``install.machine_paths.persist_to`` (Claude Code's ``settings.json``
    env by default), and each entry reading one is then asked of Claude Code
    itself (:func:`.claude_check.connect_check`).
    """
    from .writer import McpWriter

    config = ctx.settings.section("install.machine_paths")
    result = MachinePaths()
    writer = McpWriter(ctx)
    prefix = ctx.settings.str_("mcp.registration.path_variable_prefix", "ICA_MCP")
    if not prefix:
        return result
    entries = _entries(ctx)
    uses: dict[str, list[tuple[str, str]]] = {}
    for name, (entry, _where) in entries.items():
        for text in _strings(entry):
            for match in ENTRY_VARIABLE.finditer(text):
                variable = match.group(1)
                if variable.startswith(prefix + "_"):
                    uses.setdefault(variable, []).append((name, text))
                    if name not in result.users.setdefault(variable, []):
                        result.users[variable].append(name)
    from .writer import recorded_path_variables

    recorded = recorded_path_variables(ctx)
    environ = _fresh(ctx)
    for variable, places in sorted(uses.items()):
        if environ.get(variable):
            result.kept.append(variable)
            continue
        if all(_works(text, environ) for _name, text in places):
            continue  # the written default exists here: nothing to do
        # What this program recorded writing here comes first: it is the path
        # the entries were proved with on this machine. /usr/bin/uvx (0.7.18)
        # was found on the search path before the recorded linuxbrew 0.9.5.
        options = [
            p
            for p in dict.fromkeys(
                [
                    recorded.get(variable, ""),
                    writer.resolve_program_variable(variable),
                    writer.program_variable_on_path(variable),
                ]
            )
            if p
        ]
        local = next((p for p in options if all(_works(text, {**environ, variable: p}) for _name, text in places)), "")
        if local:
            result.set[variable] = local
            continue
        local = options[0] if options else ""
        values = {**environ, variable: local} if local else environ
        failing = result.missing.setdefault(variable, {})
        for server, text in places:
            if not _works(text, values):
                failing.setdefault(server, expand(text, values))
        result.unresolved[variable] = (
            "the file it names is not on this machine" if local else "nothing on this machine stands for it"
        )
    if not result.set or not apply or ctx.dry_run:
        return result
    where = config.str_("persist_to", "settings").strip().lower()
    if where in ("env_file", "both"):
        for variable, value in result.set.items():
            ctx.env.set(variable, value, passthrough=True)
        result.places.append("this program's environment file")
    from ..core.machine_env import MachineEnv

    if where in ("settings", "both") and MachineEnv(ctx).enabled:
        # Every place a new terminal's Claude Code reads, through the one
        # per-machine writer, logged place by place (core/machine_env.py).
        for variable, value in result.set.items():
            record = MachineEnv(ctx).apply(
                variable,
                value,
                f"where {variable} stands for on this machine "
                f"(read by {', '.join(result.users.get(variable, [])) or 'its entries'})",
            )
            result.places.extend(p.place for p in record.places if p.place not in result.places)
    elif where in ("settings", "both"):
        from ..provision.settings_writer import SettingsWriter, settings_path

        path = settings_path(ctx)
        written = SettingsWriter(ctx, path).apply({"env": dict(result.set)})
        if getattr(written, "error", ""):
            log.warning("could not set %s in %s: %s", ", ".join(result.set), path, written.error)
        else:
            result.places.append(str(path))
    os.environ.update(result.set)  # the rest of this run starts them as Claude Code will
    if config.bool_("check_with_claude", True):
        from .claude_check import connect_check

        for name in sorted({n for v in result.set for n in result.users.get(v, [])}):
            outcome = connect_check(ctx, name, entries[name][0])
            result.checks[name] = (outcome.outcome, outcome.detail)
    return result


__all__ = [
    "CALL",
    "LAUNCH",
    "MachinePaths",
    "Need",
    "machine_paths",
    "missing_programs",
    "recheck_command",
    "repair_private_launchers",
]
