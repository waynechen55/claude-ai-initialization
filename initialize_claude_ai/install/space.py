"""What an install step costs in disk, asked before it runs -- and what a failed one left behind.

``gcloud components reinstall`` builds a whole staging copy of the SDK beside
the one in use. On a machine with just over 1 GB free it ran for 90 seconds and
filled the disk; every later step failed, each one blamed on gcloud.
``--force-install`` reinstalls what already works, and must never be what fills
a disk.

* :func:`need_mb` -- what one reinstall needs: the folder that holds the copy
  times the strategy's ``space_factor`` (an in-place reinstall that stages a
  copy declares 2), or the installed size of the packages the package manager
  owns it by (``[install.commands] <mgr>.installed_size``), plus
  ``install.space_margin_mb``;
* :func:`free_mb` -- what the filesystem holding it has;
* :func:`siblings` / :func:`remove_new_siblings` -- what a failed step created
  next to the owned folder, removed only when it carries this program's marker
  (never by its name); :func:`restore_runnable` puts a runnable copy back when the
  step left the tool unable to run.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any, Iterable, Mapping

from ..core.diskspace import size_mb
from ..core.logging_setup import get_logger

log = get_logger("install.space")

#: Fallbacks, equal to the packaged values.
DEFAULT_MARGIN_MB = 256
DEFAULT_SPACE_FACTOR = 1.0


def enabled(settings: Any) -> bool:
    return settings.bool_("install.check_space", True)


def margin_mb(settings: Any) -> int:
    return max(0, settings.int_("install.space_margin_mb", DEFAULT_MARGIN_MB))


def free_mb(path: str | Path) -> int:
    """Free space, in MB, on the filesystem holding ``path`` (its nearest existing parent)."""
    where = Path(str(path) or "/")
    while not where.exists() and where != where.parent:
        where = where.parent
    try:
        return int(shutil.disk_usage(where).free // (1024 * 1024))
    except OSError:
        return -1


def package_mb(installer: Any, packages: Iterable[str]) -> int:
    """Installed size of ``packages`` as the package manager reports it, in MB; 0 when it cannot say."""
    manager = installer.platform.package_manager
    unit = float(installer.commands.get(f"{manager}.installed_size_unit", 1) or 1)
    total = 0.0
    for package in packages:
        for value in installer._query(manager, "installed_size", {"package": package}):
            try:
                total += float(value) * unit
            except ValueError:
                continue
    return int(total // (1024 * 1024))


def need_mb(installer: Any, owner: Any, strategies: list[Mapping[str, Any]]) -> tuple[int, str]:
    """``(MB, how)``: what reinstalling ``owner``'s copy through ``strategies`` needs free; (0, "") when unknown."""
    settings = installer.settings
    spec = strategies[0] if strategies else {}
    factor = float(spec.get("space_factor") or DEFAULT_SPACE_FACTOR)
    margin = margin_mb(settings)
    folder = str(getattr(owner, "folder", "") or "")
    if folder and Path(folder).is_dir():
        held = size_mb(Path(folder))
        return int(held * factor) + margin, f"{folder} holds {held} MB, x{factor:g} to reinstall in place, +{margin} MB"
    packages = list(getattr(owner, "packages", []) or [])
    if packages:
        held = package_mb(installer, packages)
        if held:
            return int(held * factor) + margin, f"{', '.join(packages)} take {held} MB, +{margin} MB"
    return margin, f"at least {margin} MB"


def where(owner: Any) -> str:
    """The path whose filesystem the reinstall writes to."""
    return str(getattr(owner, "folder", "") or getattr(owner, "real", "") or getattr(owner, "path", "") or "/")


def siblings(folder: str) -> set[str]:
    """Entries next to ``folder`` whose names start with its own -- before a step, to compare with after."""
    path = Path(folder)
    if not folder or not path.parent.is_dir():
        return set()
    try:
        return {p.name for p in path.parent.iterdir() if p.name.startswith(path.name) and p.name != path.name}
    except OSError:
        return set()


#: Fallback for ``install.owned_marker``: the file this program puts in a
#: folder it creates, the only proof a folder is its own to delete.
OWNED_MARKER = ".initialize-claude-ai-owned"


def owned(path: Path, settings: Any) -> bool:
    """Did this program create ``path``? Only its marker says so -- never a name."""
    marker = settings.str_("install.owned_marker", OWNED_MARKER)
    return bool(marker) and (path / marker).exists()


def remove_new_siblings(folder: str, before: set[str], settings: Any) -> list[str]:
    """Remove what a failed step left next to ``folder`` -- only what carries this program's marker; what was removed.

    A name says nothing about ownership. gcloud's own reinstall built
    ``google-cloud-sdk.staging`` and failed activating it; this removed the
    staging copy by its name, and gcloud was gone from the machine. A folder
    another tool left is reported (:func:`left_by_others`), never deleted.
    (``install.clean_failed_step_leftovers``, ``install.owned_marker``.)
    """
    if not settings.bool_("install.clean_failed_step_leftovers", True):
        return []
    removed: list[str] = []
    parent = Path(folder).parent
    for name in sorted(siblings(folder) - before):
        target = parent / name
        if settings.bool_("install.deleting_needs_ownership", True) and not owned(target, settings):
            continue
        try:
            if target.is_dir() and not target.is_symlink():
                shutil.rmtree(target)
            else:
                target.unlink()
        except OSError as exc:
            log.warning("could not remove %s, left by the failed step: %s", target, exc)
            continue
        removed.append(str(target))
        log.info("removed %s, left behind by the failed reinstall", target)
    return removed


def left_by_others(folder: str, before: set[str], settings: Any) -> list[str]:
    """What a failed step left next to ``folder`` that this program does not own -- reported, never removed."""
    parent = Path(folder).parent
    return [str(parent / n) for n in sorted(siblings(folder) - before) if not owned(parent / n, settings)]


def restore_runnable(folder: str, relative: str, runs: Any, settings: Any) -> str:
    """Put a runnable copy back at ``folder`` when a failed step left the tool unable to run; what was done, or "".

    ``runs(path) -> bool`` asks the tool itself. A copy beside ``folder`` --
    the step's staging, the tool's own backup -- that holds ``relative`` and
    runs is moved into place; what stood at ``folder`` is kept beside it under
    ``<folder>.broken-<time>``, never deleted. Nothing runnable: nothing moves.
    (``install.restore_after_failed_step``.)
    """
    import time

    if not settings.bool_("install.restore_after_failed_step", True) or not folder or not relative:
        return ""
    target = Path(folder)
    if (target / relative).exists() and runs(str(target / relative)):
        return ""
    candidates = sorted(
        (target.parent / n for n in siblings(folder)),
        key=lambda p: p.stat().st_mtime if p.exists() else 0,
        reverse=True,
    )
    for copy_ in candidates:
        tool = copy_ / relative
        if not copy_.is_dir() or copy_.is_symlink() or not tool.exists() or not runs(str(tool)):
            continue
        kept = ""
        try:
            if target.exists() or target.is_symlink():
                aside = target.parent / f"{target.name}.broken-{time.strftime('%Y%m%d-%H%M%S')}"
                target.rename(aside)
                kept = f"; what stood there is kept as {aside}"
            copy_.rename(target)
        except OSError as exc:
            log.warning("could not put %s back at %s: %s", copy_, target, exc)
            return ""
        if runs(str(target / relative)):
            log.info("%s restored from %s, which the failed step left%s", target, copy_, kept)
            return f"restored {target} from {copy_}, a runnable copy the failed step left{kept}"
    return ""


__all__ = [
    "OWNED_MARKER",
    "free_mb",
    "left_by_others",
    "need_mb",
    "owned",
    "package_mb",
    "remove_new_siblings",
    "restore_runnable",
    "siblings",
    "where",
]
