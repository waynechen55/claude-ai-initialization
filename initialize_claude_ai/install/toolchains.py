"""Toolchains: which catalog component provides a command, and who needs one.

Phase 4 launches MCP servers with programs phase 1 may never have installed --
``uvx``, ``npx``, ``go``, ``cargo``, ``bunx``, ``docker``. Two things close
that gap, and both answer the same question the same way, from the catalog:

* ``--install-prefetch`` (phase 1) reads the command templates a later phase
  runs (``install.demand_sources``) and installs, ahead of time, the component
  behind the first word of each -- and with ``all``, every toolchain group.
* ``--mcp-provision-missing`` (phase 4, :mod:`.on_demand`) installs the
  component behind a command a candidate turned out to need, when it needs it.

Neither holds a list of program names. A command maps to a component through
the names the component verifies and the ones it ``provides``, so a toolchain
added to ``10-components.toml`` is covered by both without a line of Python.

The allowed values of both settings are defined here once, and the command
line, the phases and the tests all read them from here.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from ..core.config import Settings
from ..core.logging_setup import get_logger
from ..core.proc import Runner
from .components import ComponentSpec

log = get_logger("toolchains")

#: ``install.prefetch`` / ``--install-prefetch``.
PREFETCH_MODES = ("none", "launchers", "all")
#: ``mcp.packages.provision_missing`` / ``--mcp-provision-missing``.
PROVISION_MODES = ("auto", "always", "never")
#: Yes/no spellings of a provisioning mode, for a setting that reads like a switch.
PROVISION_ALIASES = {
    "true": "always",
    "yes": "always",
    "on": "always",
    "1": "always",
    "false": "never",
    "no": "never",
    "off": "never",
    "0": "never",
}


def prefetch_mode(settings: Settings) -> str:
    """The prefetch mode in force; an unknown value prefetches nothing, loudly."""
    raw = settings.str_("install.prefetch", "launchers").strip().lower()
    if raw in PREFETCH_MODES:
        return raw
    log.warning(
        "install.prefetch = %r is not one of %s; prefetching nothing",
        raw,
        ", ".join(PREFETCH_MODES),
    )
    return "none"


def provision_mode(settings: Settings) -> str:
    """The provisioning mode in force; an unknown value installs nothing, loudly.

    "Nothing" because that is what every run did before this setting existed:
    a typo should cost the new behaviour, never add to it.
    """
    raw = settings.str_("mcp.packages.provision_missing", "auto").strip().lower()
    raw = PROVISION_ALIASES.get(raw, raw)
    if raw in PROVISION_MODES:
        return raw
    log.warning(
        "mcp.packages.provision_missing = %r is not one of %s; installing nothing on demand",
        raw,
        ", ".join(PROVISION_MODES),
    )
    return "never"


def command_index(components: Iterable[ComponentSpec]) -> dict[str, ComponentSpec]:
    """``command -> component`` for every command the catalog can put on a machine.

    The first component to name a command keeps it, in catalog order -- the
    same order phase 1 installs in, so prerequisites win over dependants.
    """
    index: dict[str, ComponentSpec] = {}
    for component in components:
        for name in component.commands():
            index.setdefault(name, component)
    return index


def _templates(value: Any) -> list[Sequence[Any]]:
    """The argument vectors inside a demand source, whatever shape it takes.

    A table of templates (``[mcp.packages.launchers]``), a list of them, or a
    single one -- a list of words.
    """
    if isinstance(value, Mapping):
        return [t for v in value.values() for t in _templates(v)]
    if isinstance(value, (list, tuple)):
        if value and all(isinstance(v, str) for v in value):
            return [value]
        return [t for v in value for t in _templates(v)]
    return []


def demanded_commands(settings: Settings) -> list[tuple[str, str]]:
    """``(command, setting)`` for the first word of every template a later phase runs.

    A placeholder (``{python}``) names no program and is left out: it is
    resolved to one phase 1 installs anyway.
    """
    found: list[tuple[str, str]] = []
    for source in settings.str_list("install.demand_sources", ["mcp.packages.launchers", "mcp.bridge.adapters"]):
        for template in _templates(settings.get(source)):
            first = str(template[0]).strip() if template else ""
            if not first or first.startswith("{"):
                continue
            pair = (os.path.basename(first), source)
            if pair not in found:
                found.append(pair)
    return found


def prefetch_components(
    components: Iterable[ComponentSpec],
    settings: Settings,
    *,
    mode: str,
    selected: Iterable[ComponentSpec] = (),
    skip: Iterable[str] = (),
    system: str = "Linux",
    traits: Iterable[str] = (),
) -> tuple[list[tuple[ComponentSpec, str]], list[str]]:
    """What ``mode`` adds to a selection. Returns ``([(component, reason)], notes)``.

    Nothing already selected is added twice, ``skip`` (keys or groups) always
    wins, and a component that does not apply to this machine is left out the
    way the selection leaves it out.
    """
    if mode not in PREFETCH_MODES or mode == "none":
        return [], []
    catalog = list(components)
    index = command_index(catalog)
    wanted: dict[str, tuple[ComponentSpec, str]] = {}
    notes: list[str] = []

    for command, source in demanded_commands(settings):
        component = index.get(command)
        if component is None:
            log.debug("prefetch: %s (from %s) is not a catalog command", command, source)
            continue
        if mode == "launchers" and not component.prefetch:
            notes.append(
                f"not prefetching '{component.key}' for {command} ({source}): its catalog "
                "entry says prefetch = false; --install-prefetch all or --install-only "
                f"{component.key} installs it"
            )
            continue
        wanted.setdefault(component.key, (component, f"{source} launches with {command}"))

    if mode == "all":
        groups = set(settings.str_list("install.prefetch_groups", ["mcp-runtimes"]))
        for component in catalog:
            if component.group in groups:
                wanted.setdefault(component.key, (component, f"in prefetch group {component.group}"))

    already = {c.key for c in selected}
    skip_set = {str(s).strip() for s in skip if str(s).strip()}
    traits = tuple(traits)
    added: list[tuple[ComponentSpec, str]] = []
    for key, (component, reason) in wanted.items():
        if key in already:
            continue
        if key in skip_set or component.group in skip_set:
            notes.append(f"not prefetching '{key}': --install-skip names it")
            continue
        if not component.applies_to(system, traits):
            continue
        added.append((component, reason))
        notes.append(f"prefetching '{key}' ({reason})")
    order = {c.key: i for i, c in enumerate(catalog)}
    added.sort(key=lambda pair: order.get(pair[0].key, len(order)))
    return added, notes


def query_bin_dir(runner: Runner, component: ComponentSpec, binary: str, timeout: float) -> str:
    """Where ``component`` puts the programs it installs, by asking it.

    ``go install`` writes to ``$GOBIN``, else ``$GOPATH/bin``; neither is on a
    fresh machine's PATH, and a server built with it cannot be launched until
    it is. Each ``bin_dir_queries`` entry runs ``binary <args>``; the first
    non-empty answer, plus its ``suffix``, is the directory.
    """
    if not binary:
        return ""
    for query in getattr(component, "bin_dir_queries", None) or ():
        args = [str(a) for a in query.get("args", []) if str(a)]
        if not args:
            continue
        answer = runner.probe([binary, *args], timeout=timeout)
        # stdout only: what a tool says on stderr is not where anything goes.
        lines = (answer.stdout or "").strip().splitlines() if answer.ok else []
        text = lines[0].strip() if lines else ""
        # GOPATH may be a list; the first entry is where `go install` writes.
        first = text.split(os.pathsep)[0].strip() if text else ""
        if not first:
            continue
        directory = Path(first).expanduser()
        suffix = str(query.get("suffix", "")).strip()
        return str(directory / suffix if suffix else directory)
    return ""


__all__ = [
    "PREFETCH_MODES",
    "PROVISION_ALIASES",
    "PROVISION_MODES",
    "command_index",
    "demanded_commands",
    "prefetch_components",
    "prefetch_mode",
    "provision_mode",
    "query_bin_dir",
]
