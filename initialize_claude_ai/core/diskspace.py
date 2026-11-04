"""Free space: checked before the run, recognised when it runs out, and given back afterwards.

A run on another machine started with its disk already full. Phase 1 reported
``✔ OK`` for tools whose reinstall had just failed with "No space left on
device" (an older copy was still there), phases 2 and 3 passed, and phase 4
broke in ways that looked like bugs of its own -- a CA bundle that could not be
written, a traceback per address while copying the agent's skills -- until the
log itself could not be written and the run went silent, leaving no
``.mcp.json``. Nothing in the program knew how much room it needed.

So, here and configured under ``[run.disk]`` and ``[run.cleanup]``:

* :func:`check` -- free space on every filesystem the run writes to (its state
  and work directories, the temporary directory, the project directory, the
  package caches it fills), compared with ``run.disk.min_free_mb``; the CLI
  stops before a phase that would start short, and says which filesystem, by
  how much, and what of this program's lies there.
* :func:`is_out_of_space` -- an ``OSError`` with errno ENOSPC/EDQUOT, a
  ``shutil.Error`` carrying one, or command output with one of
  ``run.disk.out_of_space_markers``. Wherever it is seen, the run stops with
  one verdict instead of pressing on (:class:`OutOfSpaceError`).
* :func:`cleanup` -- this program's own leftovers (``run.cleanup``): temporary
  directories it marked as its own (:func:`temporary_directory`), and
  work-directory subtrees no registered entry names. Shared package caches are
  never touched; their own cleanup commands are named.

A name is not ownership. Cleanup once matched ``/tmp/ica-*`` by prefix alone and
removed a directory someone else had named ``ica-baseline``: any directory whose
name merely starts like ours is somebody's until the marker says otherwise.
"""

from __future__ import annotations

import contextlib
import errno
import json
import os
import re
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

from .errors import InitializeClaudeAiError

#: Fallbacks for ``run.disk.out_of_space_markers``.
OUT_OF_SPACE_MARKERS = ("No space left on device", "ENOSPC", "Disk quota exceeded", "EDQUOT")
#: Fallbacks for ``run.cleanup.temp_prefixes`` and ``run.cleanup.work_subtrees``.
TEMP_PREFIXES = ("mcp-client-", "initialize-claude-ai-", "ica-")
WORK_SUBTREES = ("agent-workspace/artifacts/*", "agent-workspace/.claude/.skills.*", "mcp-trials/*")
#: The file this program puts in every temporary directory it makes. Cleanup
#: removes a temporary directory only when it holds this file
#: (``run.cleanup.require_owner_marker``). The generated client bridge writes
#: the same name; a test holds the two together.
OWNER_MARKER = ".initialize-claude-ai-owned"
#: errno values that mean the filesystem has no room.
OUT_OF_SPACE_ERRNOS = (errno.ENOSPC, getattr(errno, "EDQUOT", errno.ENOSPC))


class OutOfSpaceError(InitializeClaudeAiError):
    """The disk the run writes to is full: nothing further can be written, so nothing further is tried."""

    code = "out_of_space"


@dataclass
class Shortfall:
    """One filesystem with less free space than the run needs."""

    mount: str
    free_mb: int
    needed_mb: int
    paths: list[str] = field(default_factory=list)

    def words(self) -> str:
        return (
            f"{self.mount} has {self.free_mb} MB free, {self.needed_mb} MB is needed (it holds {', '.join(self.paths)})"
        )


def _markers(settings: Any) -> list[str]:
    if settings is None:
        return list(OUT_OF_SPACE_MARKERS)
    return settings.str_list("run.disk.out_of_space_markers", list(OUT_OF_SPACE_MARKERS))


def is_out_of_space(problem: Any, settings: Any = None) -> bool:
    """Does ``problem`` -- an exception or a command's output -- say the disk is full?"""
    if isinstance(problem, OSError) and problem.errno in OUT_OF_SPACE_ERRNOS:
        return True
    if isinstance(problem, shutil.Error):
        return any(is_out_of_space(str(item), settings) for item in (problem.args[0] if problem.args else []))
    if isinstance(problem, BaseException):
        cause = problem.__cause__ or problem.__context__
        if cause is not None and cause is not problem and is_out_of_space(cause, settings):
            return True
        problem = str(problem)
    text = str(problem or "").lower()
    return any(m.lower() in text for m in _markers(settings) if m)


def _existing(path: Path) -> Path:
    """``path``, or its nearest existing ancestor -- where a file written under it would land."""
    path = path.expanduser()
    while not path.exists() and path != path.parent:
        path = path.parent
    return path


def watched_paths(ctx: Any) -> list[Path]:
    """Every place the run writes to: configured templates plus the run's own directories."""
    paths = [Path(str(p)) for p in ctx.settings.str_list("run.disk.paths", []) if str(p).strip()]
    paths += [Path(tempfile.gettempdir()), Path.cwd()]
    for attribute in ("work_dir", "state_dir"):
        value = getattr(ctx, attribute, None)
        if value:
            paths.append(Path(value))
    seen: list[Path] = []
    for path in paths:
        if path not in seen:
            seen.append(path)
    return seen


def check(ctx: Any) -> list[Shortfall]:
    """Filesystems the run writes to that have less than ``run.disk.min_free_mb`` free."""
    needed = ctx.settings.int_("run.disk.min_free_mb", 1024)
    by_device: dict[int, Shortfall] = {}
    for path in watched_paths(ctx):
        where = _existing(path)
        try:
            device = where.stat().st_dev
            usage = shutil.disk_usage(where)
        except OSError:
            continue
        free_mb = int(usage.free // (1024 * 1024))
        if free_mb >= needed:
            continue
        shortfall = by_device.setdefault(device, Shortfall(_mount_of(where), free_mb, needed))
        if str(path) not in shortfall.paths:
            shortfall.paths.append(str(path))
    return list(by_device.values())


def _mount_of(path: Path) -> str:
    path = path.resolve()
    while not os.path.ismount(path) and path != path.parent:
        path = path.parent
    return str(path)


def size_mb(path: Path) -> int:
    """Disk use of ``path`` in MB (0 when missing); never follows symlinks."""
    total = 0
    if not path.exists():
        return 0
    seen: set[tuple[int, int]] = set()
    for root, _dirs, files in os.walk(path, followlinks=False):
        for name in files:
            try:
                info = (Path(root) / name).lstat()
            except OSError:
                continue
            # Each file once, by what it occupies -- as du counts: a package
            # cache hard-links the same file into many places.
            if (info.st_dev, info.st_ino) in seen:
                continue
            seen.add((info.st_dev, info.st_ino))
            total += getattr(info, "st_blocks", 0) * 512 or info.st_size
    return int(total // (1024 * 1024))


def hints(ctx: Any) -> list[str]:
    """What can be given back, largest first: this program's own directories, then shared caches with their commands."""
    rows: list[tuple[int, str]] = []
    for attribute in ("work_dir",):
        value = getattr(ctx, attribute, None)
        if value:
            rows.append(
                (
                    size_mb(Path(value)),
                    f"{value} -- this program's work directory (--do-cleanup removes what no entry uses)",
                )
            )
    for path, command in ctx.settings.dict_("run.disk.cache_cleanup", {}).items():
        where = Path(str(path)).expanduser()
        if where.exists():
            rows.append((size_mb(where), f"{where} -- a shared package cache; `{command}` shrinks it"))
    rows.sort(reverse=True)
    return [f"{mb} MB: {text}" for mb, text in rows if mb]


def remediation(ctx: Any, shortfalls: Sequence[Shortfall]) -> list[str]:
    lines = [f"Free space on {s.mount}: {s.words()}." for s in shortfalls]
    lines.append("Find what fills it: du -xh --max-depth=2 <mount> | sort -h | tail")
    lines += hints(ctx)
    return lines


# -- this program's own leftovers ------------------------------------------------


#: ``${NAME}`` / ``${NAME:-default}`` -- the reference syntax Claude Code expands in an entry.
_REFERENCE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")
#: An absolute path inside an expanded string -- never a URL's ``//host``.
_ABSOLUTE = re.compile(r'(?<![:/A-Za-z0-9])(/[^"\'\s:]+)')


def _strings(value: Any) -> Iterable[str]:
    """Every string value in a parsed JSON document, keys included."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield str(key)
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def _program_variables(ctx: Any) -> dict[str, str]:
    """What this program's own path variables (``ICA_MCP_*``) stand for here; {} when unknown."""
    try:
        from ..mcp_auto_discovery.writer import McpWriter  # the MCP layer sits above this one

        return dict(McpWriter(ctx).program_variables())
    except Exception:  # noqa: BLE001 - a cleanup must never fail on the way to protecting things
        return {}


def _expansions(text: str, variables: dict[str, str]) -> list[str]:
    """``text`` as Claude Code would launch it: with this shell's values, and with the defaults alone.

    Both, because the shell Claude Code is started from may lack a variable
    this run has -- either way, the file the entry launches is in use.
    """

    def expand(environ: dict[str, str]) -> str:
        def one(match: re.Match[str]) -> str:
            name, default = match.group(1), match.group(2)
            if environ.get(name):
                return environ[name]
            return default if default is not None else variables.get(name, match.group(0))

        return _REFERENCE.sub(one, text)

    return list(dict.fromkeys([expand(dict(os.environ)), expand({})]))


def _referenced_paths(ctx: Any) -> set[Path]:
    """Every absolute path an MCP entry this machine has launches or reads -- read as Claude Code reads them.

    Each source is parsed, and every string in it is expanded the way the
    client expands it (``${NAME}``, ``${NAME:-default}``, this program's own
    path variables) before paths are taken from it. Matching the raw text
    instead protected ``${ICA_MCP_ARTIFACTS:-/…/artifacts}`` -- the directory
    above every build -- and not ``/…/artifacts/gitlab-example-net/…``, the bridge
    the entry runs, so cleanup would have removed a working server's files.
    """
    found: set[Path] = set()
    sources = [Path.cwd() / ".mcp.json"]
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR") or str(Path.home())
    claude_config = Path(config_dir) / ".claude.json"
    sources.append(claude_config)
    # Every project Claude Code knows, not only this directory's: a run started
    # elsewhere must not remove files another project's entries launch.
    try:
        projects = json.loads(claude_config.read_text(encoding="utf-8")).get("projects") or {}
    except (OSError, ValueError, AttributeError):
        projects = {}
    sources += [Path(str(project)) / ".mcp.json" for project in projects]
    # ...and this program's own records of entries (what it wrote, what last worked).
    state = getattr(ctx, "state_dir", None)
    if state:
        sources += sorted(Path(state).glob("*.json"))
    variables = _program_variables(ctx)
    texts: list[str] = []
    for source in dict.fromkeys(sources):
        try:
            raw = source.read_text(encoding="utf-8")
        except OSError:
            continue
        try:
            texts += list(_strings(json.loads(raw)))
        except ValueError:
            texts.append(raw)  # not JSON: searched as text, expanded all the same
    for text in texts:
        for expanded in _expansions(text, variables):
            for match in _ABSOLUTE.finditer(expanded):
                # A path to protect exists, or its directory does (a server
                # whose file is missing right now still owns that directory).
                candidate = Path(match.group(1).rstrip(",;)]}"))
                if candidate.exists() or (candidate.parent != Path("/") and candidate.parent.exists()):
                    found.add(candidate)
    return found


def mark_owned(path: str | os.PathLike[str]) -> str:
    """Mark ``path`` as made by this program, so a later cleanup may remove it; returns ``path``."""
    try:
        (Path(path) / OWNER_MARKER).write_text(
            "made by initialize-claude-ai; removed by its cleanup when unused\n", encoding="utf-8"
        )
    except OSError:
        pass  # unmarked only means a later cleanup leaves it alone
    return str(path)


def owned(path: Path) -> bool:
    """Did this program make ``path``? Only its marker says so -- never its name."""
    return (path / OWNER_MARKER).is_file()


@contextlib.contextmanager
def temporary_directory(prefix: str) -> Iterator[str]:
    """:class:`tempfile.TemporaryDirectory`, marked as this program's (see :data:`OWNER_MARKER`)."""
    with tempfile.TemporaryDirectory(prefix=prefix) as path:
        yield mark_owned(path)


def _in_use(path: Path, referenced: Iterable[Path]) -> bool:
    for ref in referenced:
        try:
            ref.relative_to(path)
            return True
        except ValueError:
            continue
    return False


@dataclass
class CleanupResult:
    removed: list[str] = field(default_factory=list)
    kept: list[str] = field(default_factory=list)
    #: Named like this program's leftovers but without its marker: somebody else's, left alone.
    unproven: list[str] = field(default_factory=list)
    freed_mb: int = 0
    errors: list[str] = field(default_factory=list)


def cleanup(ctx: Any, *, apply: bool = True) -> CleanupResult:
    """Remove this program's own leftovers that nothing uses (``run.cleanup``); never a shared cache.

    * temporary directories under the temp directory whose names start with one
      of ``temp_prefixes``, are older than ``temp_min_age_minutes`` and hold
      :data:`OWNER_MARKER` -- the client bridge made one per start (683 were
      left on one machine). One without the marker is listed in ``unproven``
      and never removed: the prefix only says where to look;
    * subtrees of the work directory listed in ``work_subtrees`` (agent build
      files, trial environments) that no MCP entry on this machine names, oldest
      first, until what is left is under ``work_budget_mb``.

    Never in a dry run; every removal is reported. ``apply=False`` decides
    the same way and removes nothing -- ``removed`` then lists what would go.
    """
    result = CleanupResult()
    cfg = ctx.settings.section("run.cleanup")
    if getattr(ctx, "dry_run", False):
        return result
    referenced = _referenced_paths(ctx)
    now = time.time()
    min_age = cfg.float_("temp_min_age_minutes", 60) * 60
    require_marker = cfg.bool_("require_owner_marker", True)
    temp_root = Path(tempfile.gettempdir())
    for prefix in cfg.str_list("temp_prefixes", list(TEMP_PREFIXES)):
        for entry in sorted(temp_root.glob(f"{prefix}*")):
            try:
                if not entry.is_dir() or entry.is_symlink() or now - entry.stat().st_mtime < min_age:
                    continue
                if require_marker and not owned(entry):
                    result.unproven.append(str(entry))
                    continue
                if _in_use(entry, referenced):
                    result.kept.append(str(entry))
                    continue
                mb = size_mb(entry)
                if apply:
                    shutil.rmtree(entry)
                result.removed.append(str(entry))
                result.freed_mb += mb
            except OSError as exc:
                result.errors.append(f"{entry}: {exc}")
    work = Path(str(getattr(ctx, "work_dir", "") or ""))
    budget = cfg.int_("work_budget_mb", 512)
    if work and work.is_dir():
        candidates: list[tuple[float, Path]] = []
        for pattern in cfg.str_list("work_subtrees", list(WORK_SUBTREES)):
            for entry in work.glob(pattern):
                if entry.is_dir() and not entry.is_symlink():
                    candidates.append((entry.stat().st_mtime, entry))
        candidates.sort()
        total = size_mb(work)
        for _mtime, entry in candidates:
            if total <= budget:
                break
            if _in_use(entry, referenced):
                result.kept.append(str(entry))
                continue
            try:
                mb = size_mb(entry)
                if apply:
                    shutil.rmtree(entry)
                result.removed.append(str(entry))
                result.freed_mb += mb
                total -= mb
            except OSError as exc:
                result.errors.append(f"{entry}: {exc}")
    return result


__all__ = [
    "CleanupResult",
    "OUT_OF_SPACE_MARKERS",
    "OWNER_MARKER",
    "OutOfSpaceError",
    "Shortfall",
    "check",
    "cleanup",
    "hints",
    "is_out_of_space",
    "mark_owned",
    "owned",
    "remediation",
    "size_mb",
    "temporary_directory",
    "watched_paths",
]
