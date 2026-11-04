"""Who owns the copy that is installed -- so ``--force-install`` repairs that copy, through that owner.

A forced reinstall used to run a component's *fresh-install* strategies in
catalog order. On a machine where everything was already installed that went
wrong three ways in one run:

* npm's only openSUSE strategy named ``npm22``; the machine's npm came from
  ``npm26`` (behind the ``nodejs-common`` launcher at /usr/bin/npm), and zypper
  said "No provider of 'npm22'". The system was never asked which package
  provides npm.
* gcloud had been put in ~/google-cloud-sdk by Google's script; the script was
  run again, downloaded everything and refused: the folder exists. Repairing a
  script install in place is the tool's own job (``gcloud components
  reinstall``).
* node's nvm fallback installed a *second* node beside /usr/bin/node -- which
  the search path still finds -- and node was reported changed.

So before a forced reinstall the owner of the binary in use is found
(:func:`find`): a strategy whose own folders hold it (``owns_paths``,
``adds_bin_dir``), else the package manager that says it owns the file or
provides the command (``[install.commands] <mgr>.owner`` / ``<mgr>.provider``),
else a kind recognised from the path (``[install.ownership.path_markers]``).
Only the owner reinstalls (:func:`plan`): its packages, or its own in-place
form (``reinstall_argv`` / ``reinstall_shell``, or ``reinstalls_in_place``
for an installer that overwrites its own copy when run again). A strategy that would install
a parallel copy is not run, and an owner with no in-place form keeps what is
there. What changed is judged on the binary itself (:func:`fingerprint`).
"""

from __future__ import annotations

import copy
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from ..core.logging_setup import get_logger

log = get_logger("install.ownership")

#: Fallback for ``install.ownership.path_markers``, equal to the packaged value.
PATH_MARKERS: dict[str, list[str]] = {
    # Most enclosing first: Homebrew's node carries its npm in a node_modules.
    "brew": ["/homebrew/", "/linuxbrew/", "/Cellar/", "/Caskroom/"],
    "npm": ["/node_modules/"],
    "snap": ["/snap/"],
    "flatpak": ["/flatpak/"],
    "uv_tool": ["/uv/tools/"],
}


@dataclass
class Owner:
    """What installed the binary in use, and how it is reinstalled."""

    kind: str = ""
    #: The owning strategy's position in the component's ``install`` list; -1 for none.
    index: int = -1
    #: For ``kind == "system"``: the installed packages to reinstall.
    packages: list[str] = field(default_factory=list)
    path: str = ""
    real: str = ""
    how: str = ""
    #: The owning strategy's folder that holds the copy, "" when a package or a kind owns it.
    folder: str = ""

    @property
    def known(self) -> bool:
        return bool(self.kind)


def enabled(settings: Any) -> bool:
    return settings.bool_("install.reinstall_owner_only", True)


def binary_paths(checks: Sequence[Any]) -> list[str]:
    return [str(c.path) for c in checks if getattr(c, "ok", False) and getattr(c, "path", "")]


def fingerprint(checks: Sequence[Any]) -> list[tuple[Any, ...]]:
    """Each verified binary as (path, real path, inode, change time, version): a reinstall changes one of them."""
    out: list[tuple[Any, ...]] = []
    for check in checks:
        path = str(getattr(check, "path", "") or "")
        if not getattr(check, "ok", False) or not path:
            continue
        real = os.path.realpath(path)
        try:
            st = os.stat(real)
            ident: tuple[Any, ...] = (st.st_ino, st.st_ctime_ns)
        except OSError:
            ident = (None, None)
        out.append((path, real, *ident, str(getattr(check, "version", "") or "")))
    return out


def _owned_dirs(installer: Any, spec: Mapping[str, Any]) -> list[str]:
    raw = [*(spec.get("owns_paths") or [])]
    if spec.get("adds_bin_dir"):
        raw.append(spec["adds_bin_dir"])
    if spec.get("kind") == "download" and spec.get("install_dir"):
        raw.append(spec["install_dir"])
    out: list[str] = []
    for value in raw:
        expanded = installer.expand(str(value))
        if not expanded or "$" in expanded or "{" in expanded:
            continue  # an unset variable names no folder
        out.append(os.path.realpath(os.path.expanduser(expanded)))
    return out


def _under(path: str, folder: str) -> bool:
    try:
        return bool(path) and Path(path).resolve().is_relative_to(Path(folder))
    except (OSError, ValueError):
        return False


#: Fallback for ``install.ownership.bin_folder_names``.
BIN_FOLDER_NAMES = ("bin", "sbin")


def _top(folder: str, bin_names: Sequence[str]) -> str:
    """The folder the strategy owns at its own level: ~/google-cloud-sdk for ~/google-cloud-sdk/bin."""
    found = Path(folder)
    # adds_bin_dir names the bin folder inside an installation; its parent is the installation.
    if found.name in bin_names and found.parent != found:
        return str(found.parent)
    return str(found)


def capabilities(component: Any) -> list[str]:
    """What the component's commands are called, as a package manager may know them."""
    names: list[str] = []
    for spec in getattr(component, "verify", []) or []:
        if isinstance(spec, Mapping) and spec.get("kind") == "binary":
            names += [str(n) for n in spec.get("names") or []]
    names += [str(n) for n in getattr(component, "provides", []) or []]
    names += [str(n) for n in getattr(component, "capabilities", []) or []]
    return list(dict.fromkeys(n for n in names if n))


def find(installer: Any, component: Any, checks: Sequence[Any]) -> Owner:
    """The owner of ``component``'s verified binary; an unknown owner when nothing says."""
    paths = binary_paths(checks)
    if not paths:
        return Owner()
    path = paths[0]
    real = os.path.realpath(path)
    strategies = list(getattr(component, "install", []) or [])
    for index, spec in enumerate(strategies):
        if not isinstance(spec, Mapping):
            continue
        for folder in _owned_dirs(installer, spec):
            if _under(real, folder) or _under(path, folder):
                kind = str(spec.get("kind", ""))
                return Owner(
                    kind=kind,
                    index=index,
                    path=path,
                    real=real,
                    folder=_top(
                        folder,
                        installer.settings.str_list("install.ownership.bin_folder_names", list(BIN_FOLDER_NAMES)),
                    ),
                    how=f"{path} is in {folder}, which its {kind} strategy installs",
                )
    owners = installer.owning_packages(list(dict.fromkeys([path, real])))
    if owners:
        providers = installer.providing_packages(capabilities(component))
        packages = list(dict.fromkeys([*owners, *providers]))
        index = next((i for i, s in enumerate(strategies) if isinstance(s, Mapping) and s.get("kind") == "system"), -1)
        manager = installer.platform.package_manager
        return Owner(
            kind="system",
            index=index,
            packages=packages,
            path=path,
            real=real,
            how=f"{manager} says {', '.join(packages)} provide{'s' if len(packages) == 1 else ''} {path}",
        )
    markers = installer.settings.dict_("install.ownership.path_markers", PATH_MARKERS) or PATH_MARKERS
    matched: list[Owner] = []
    for kind, found in markers.items():
        if any(str(m) and str(m) in real for m in found or []):
            index = next((i for i, s in enumerate(strategies) if isinstance(s, Mapping) and s.get("kind") == kind), -1)
            matched.append(
                Owner(kind=str(kind), index=index, path=path, real=real, how=f"{real} is where {kind} installs")
            )
    if matched:
        # The first, in the configured order -- unless a later one is a way this component installs.
        return next((o for o in matched if o.index >= 0), matched[0]) if matched[0].index < 0 else matched[0]
    return Owner(path=path, real=real)


def plan(installer: Any, component: Any, owner: Owner) -> tuple[list[dict[str, Any]], str]:
    """``(strategies, why_none)``: how ``owner`` reinstalls in place; no strategies, and why, when it cannot."""
    strategies = list(getattr(component, "install", []) or [])
    spec: Mapping[str, Any] = strategies[owner.index] if 0 <= owner.index < len(strategies) else {}
    if owner.kind == "system":
        manager = installer.platform.package_manager
        forced = copy.deepcopy(dict(spec)) if spec else {"kind": "system"}
        forced["packages"] = {**(forced.get("packages") or {}), manager: list(owner.packages)}
        return [forced], ""
    if not spec:
        return [], f"{owner.how}, and no strategy here installs that way"
    # {binary} is the copy in use: `{binary} components reinstall` repairs the gcloud that is there.
    if spec.get("reinstall_argv"):
        return [
            {
                "kind": "command",
                "argv": [str(a).replace("{binary}", owner.path) for a in spec["reinstall_argv"]],
                "timeout_seconds": spec.get("timeout_seconds"),
                **_cost(spec),
            }
        ], ""
    if spec.get("reinstall_shell"):
        return [
            {
                "kind": "command",
                "shell": str(spec["reinstall_shell"]).replace("{binary}", owner.path),
                "timeout_seconds": spec.get("timeout_seconds"),
                **_cost(spec),
            }
        ], ""
    if spec.get("reinstalls_in_place"):
        return [dict(spec)], ""  # an installer that overwrites its own copy when run again
    if str(spec.get("kind")) in installer.settings.str_list("install.ownership.in_place_kinds", list(IN_PLACE_KINDS)):
        return [dict(spec)], ""
    return [], f"{owner.how}, and that installer cannot reinstall in place (no reinstall_argv or reinstall_shell)"


def _cost(spec: Mapping[str, Any]) -> dict[str, Any]:
    """What the strategy says its in-place form costs in disk (``space_factor``), carried along."""
    return {"space_factor": spec["space_factor"]} if spec.get("space_factor") else {}


#: Fallback for ``install.ownership.in_place_kinds``: strategies whose forcing form reinstalls the same copy.
IN_PLACE_KINDS = ("system", "brew", "npm", "pip", "uv_tool", "snap", "flatpak", "download")


__all__ = ["Owner", "binary_paths", "capabilities", "enabled", "find", "fingerprint", "plan"]
