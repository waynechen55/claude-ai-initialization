"""What really worked: a record of entries that did the job, and a look back at it when one stops.

build.opensuse.org's ``osc-mcp`` answered ``get_request 11111`` with real data
on Oct 14 -- a call that needs the login. A day later the address had a newly
written bridge that failed every call, and nothing in the program knew the old
entry had ever worked: there was nothing to compare with and nothing to fall
back on. And build-example-net "worked" only on ``get_about``, which needs no login,
so it never had worked -- and nothing said so.

So:

* an entry is **recorded** when it did the job -- its usage contract passed,
  or, where no task applies, a login was accepted by a call that fails without
  it (never one that only passed public calls). The record holds the entry as
  written, which carries ``${...}`` references and never a value, and the tasks
  it passed. It lives in the program's state directory
  (``mcp.known_good.file``);
* when an address is worked again, its **last working entry is tried first**,
  at its own tier, beside the current registrations (:meth:`candidates`);
* when the address's entry fails, the report says **what changed** since it
  last worked: command, package and version, hosts, the variables it reads,
  how it authenticates (:func:`differences`);
* an entry whose only successes were calls anybody can make is reported as
  **never proved** (:func:`never_proved`).
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any, Mapping

from ..core.context import RunContext
from ..core.logging_setup import get_logger
from . import contract, login_check

log = get_logger("mcp.known_good")

#: The source given to a candidate rebuilt from the record.
KNOWN_GOOD = "known_good"

#: Fallbacks for ``[mcp.known_good]``, equal to the packaged values.
DEFAULT_FILE = "mcp-known-good.json"
DEFAULT_KEEP = 5
DEFAULT_FILES_DIR = "mcp-known-good-files"

_REFERENCE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-[^}]*)?\}")


def enabled(ctx: RunContext) -> bool:
    return ctx.settings.bool_("mcp.known_good.enabled", True)


def path(ctx: RunContext) -> Path:
    return Path(ctx.state_dir) / ctx.settings.str_("mcp.known_good.file", DEFAULT_FILE)


def load(ctx: RunContext) -> dict[str, list[dict[str, Any]]]:
    """``{target: [record, newest first]}``; empty when there is no record yet."""
    try:
        data = json.loads(path(ctx).read_text())
    except (OSError, ValueError):
        return {}
    return {str(k): [r for r in v if isinstance(r, dict)] for k, v in data.items() if isinstance(v, list)}


def proved(candidate: Any) -> tuple[bool, list[str]]:
    """Did this candidate do the job? ``(yes, the tasks it passed)``."""
    shake = getattr(candidate, "handshake", None)
    if shake is None or not getattr(candidate, "verified", False):
        return False, []
    passed, _ = contract.verdict(shake.contract)
    tasks = [str(r.get("task")) for r in shake.contract if r.get("passed")]
    if passed:
        return True, tasks
    if passed is None and shake.login == login_check.ACCEPTED and shake.login_reason != login_check.PUBLIC_ONLY:
        return True, [f"login accepted by {shake.login_tool}"]
    return False, []


def record(ctx: RunContext, target: str, name: str, entry: Mapping[str, Any], candidate: Any) -> bool:
    """Remember ``entry`` as working for ``target`` if it did the job; True when recorded."""
    if not enabled(ctx) or ctx.dry_run:
        return False
    ok, tasks = proved(candidate)
    if not ok or _carries_a_value(entry):
        return False
    data = load(ctx)
    keep = max(1, ctx.settings.int_("mcp.known_good.keep", DEFAULT_KEEP))
    rows = [r for r in data.get(target, []) if r.get("entry") != dict(entry)]
    rows.insert(
        0,
        {
            "name": name,
            "entry": dict(entry),
            "tasks": tasks,
            "recorded": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "files": snapshot(ctx, name, entry),
        },
    )
    data[target] = rows[:keep]
    file = path(ctx)
    file.parent.mkdir(parents=True, exist_ok=True)
    temporary = file.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    os.chmod(temporary, 0o600)
    temporary.replace(file)
    log.info("%s: %s recorded as working (%s)", target, name, "; ".join(tasks))
    return True


def _files_dir(ctx: RunContext) -> Path:
    return Path(ctx.state_dir) / ctx.settings.str_("mcp.known_good.files_dir", DEFAULT_FILES_DIR)


def launched_paths(entry: Mapping[str, Any]) -> list[str]:
    """The absolute paths an entry launches or reads, expanded as Claude Code expands them."""
    from .portability import expand

    texts = [*_launch(entry), *map(str, (entry.get("env") or {}).values())]
    found: list[str] = []
    for text in texts:
        # An absolute path, never a URL's //host (the same rule cleanup uses).
        for match in re.finditer(r"(?<![:/A-Za-z0-9])(/[^\"'\s:,;$]+)", expand(str(text))):
            path = match.group(1)
            if path not in found:
                found.append(path)
    return found


def _unit(path: Path, name: str) -> Path:
    """What to keep for one file: the folder it sits in when that folder is the server's own, else the file."""
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return path.parent if slug and slug in path.parent.name.lower() else path


def snapshot(ctx: RunContext, name: str, entry: Mapping[str, Any]) -> dict[str, str]:
    """Copies of the files a working entry launches, so a lost one can be put back (``mcp.known_good.keep_files``).

    gitlab-example-net's working bridge lived in the run's scratch area; it was
    deleted, and the entry that had worked could only be called "does not
    start". Only files under ``snapshot_roots`` (the operator's home, this
    program's state) and no larger than ``snapshot_max_mb`` are kept -- never a
    system file. ``{original path: copy}``.
    """
    import hashlib
    import shutil

    if ctx.dry_run or not ctx.settings.bool_("mcp.known_good.keep_files", True):
        return {}
    roots = [
        Path(os.path.expanduser(str(r))).resolve()
        for r in ctx.settings.str_list("mcp.known_good.snapshot_roots", ["~", str(ctx.state_dir), "."])
        if str(r).strip()
    ]
    limit = ctx.settings.float_("mcp.known_good.snapshot_max_mb", 64) * 1024 * 1024
    home = _files_dir(ctx)
    kept: dict[str, str] = {}
    for raw in launched_paths(entry):
        path = Path(raw)
        try:
            real = path.resolve()
        except (OSError, ValueError):
            continue
        if not real.is_file() or real.stat().st_size > limit or home in real.parents:
            continue
        if not any(root == real or root in real.parents for root in roots):
            continue
        unit = _unit(real, name)
        if unit.is_dir():
            from ..core.diskspace import size_mb

            if size_mb(unit) * 1024 * 1024 > limit:
                unit = real  # a large folder: the file alone
        digest = hashlib.sha256(str(unit).encode()).hexdigest()[:16]
        target = home / digest / unit.name
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            if unit.is_dir():
                shutil.copytree(unit, target, dirs_exist_ok=True)
            else:
                shutil.copy2(unit, target)
        except OSError as exc:
            log.debug("could not keep a copy of %s: %s", unit, exc)
            continue
        kept[str(unit)] = str(target)
    return kept


def restore_missing(ctx: RunContext, target: str, candidate: Any) -> list[str]:
    """Put back the files ``candidate`` launches that are gone, from the copies kept when it last worked.

    Restored into the server's own folder (``mcp.registration.artifacts_home``)
    and the candidate pointed there -- never back into the scratch area that
    lost them. What was restored, as "original -> restored".
    """
    import shutil

    if ctx.dry_run or not ctx.settings.bool_("mcp.known_good.restore_missing_files", True):
        return []
    copies: dict[str, str] = {}
    for row in load(ctx).get(target) or []:
        for original, copy in (row.get("files") or {}).items():
            copies.setdefault(str(original), str(copy))
    from ..core.paths_home import artifacts_home, server_destination

    home = artifacts_home(ctx.settings)
    slug = re.sub(r"[^a-z0-9]+", "-", str(getattr(candidate, "name", "") or "server").lower()).strip("-")
    moved: dict[str, str] = {}

    def place(text: str) -> str:
        if not text.startswith("/") or Path(text).exists():
            return text
        for old, new in moved.items():
            if text == old or text.startswith(old + "/"):
                return new + text[len(old) :]
        for original, copy in copies.items():
            if not (text == original or text.startswith(original + "/")) or not Path(copy).exists():
                continue
            # The one placement rule every mover shares: never a folder nested in itself.
            destination = server_destination(home, slug, Path(original)) if home else Path(original)
            try:
                destination.parent.mkdir(parents=True, exist_ok=True)
                if Path(copy).is_dir():
                    shutil.copytree(copy, destination, dirs_exist_ok=True)
                else:
                    shutil.copy2(copy, destination)
            except OSError as exc:
                log.warning("%s: could not restore %s: %s", target, original, exc)
                return text
            moved[original] = str(destination)
            return str(destination) + text[len(original) :]
        return text

    candidate.command = [place(str(t)) for t in candidate.command]
    candidate.args = [place(str(t)) for t in candidate.args]
    candidate.env = {k: place(str(v)) if isinstance(v, str) else v for k, v in candidate.env.items()}
    lines = [f"{o} -> {n}" for o, n in moved.items()]
    if lines:
        candidate.add_note(
            "its files were gone and were restored from the copy kept when it last worked: " + "; ".join(lines)
        )
        log.info("%s: %s restored files it had lost: %s", target, candidate.label, "; ".join(lines))
    return lines


def last(ctx: RunContext, target: str) -> dict[str, Any] | None:
    rows = load(ctx).get(target) or []
    return rows[0] if rows else None


def candidates(ctx: RunContext, target: str, stage: str) -> list[Any]:
    """The last working entry for ``target``, as a candidate for ``stage`` -- when it belongs to that tier."""
    if not enabled(ctx) or not ctx.settings.bool_("mcp.known_good.try_first", True):
        return []
    row = last(ctx, target)
    if row is None:
        return []
    from .existing import ExistingRegistrations, Registration  # existing imports the writer side

    existing = ExistingRegistrations.for_run(ctx)
    entry = dict(row.get("entry") or {})
    if existing.tier_of(entry) != stage:
        return []
    if any(r.entry == entry for r in existing.registrations()):
        return []  # it is registered as it was: tried as an existing registration already
    candidate = existing.candidate(
        Registration(str(row.get("name") or ""), entry, KNOWN_GOOD), stage, record_origin=False
    )
    candidate.source = f"the entry that last worked here (recorded {row.get('recorded', 'earlier')})"
    candidate.add_note(f"{candidate.source}, when it passed: {', '.join(row.get('tasks') or [])}")
    candidate.worked_before = f"it last worked here {row.get('recorded', 'earlier')} ({', '.join(row.get('tasks') or []) or 'its checks passed'})"
    return [candidate]


def differences(previous: Mapping[str, Any], current: Mapping[str, Any]) -> list[str]:
    """What changed from the entry that last worked to the one in place now, in words -- never a value."""
    out: list[str] = []
    before, after = _launch(previous), _launch(current)
    if before != after:
        out.append(f"launch: {' '.join(before) or '(url)'}  ->  {' '.join(after) or '(url)'}")
    if str(previous.get("url") or "") != str(current.get("url") or ""):
        out.append(f"url: {previous.get('url') or '-'}  ->  {current.get('url') or '-'}")
    vars_before, vars_after = _variables(previous), _variables(current)
    if vars_before != vars_after:
        gone, new = sorted(vars_before - vars_after), sorted(vars_after - vars_before)
        out.append(
            "variables read: "
            + "; ".join(
                filter(None, [f"no longer {', '.join(gone)}" if gone else "", f"now {', '.join(new)}" if new else ""])
            )
        )
    keys_before, keys_after = set(previous.get("env") or {}), set(current.get("env") or {})
    if keys_before != keys_after:
        gone, new = sorted(keys_before - keys_after), sorted(keys_after - keys_before)
        out.append(
            "settings: "
            + "; ".join(
                filter(None, [f"removed {', '.join(gone)}" if gone else "", f"added {', '.join(new)}" if new else ""])
            )
        )
    for key in sorted(keys_before & keys_after):
        one, two = str(previous["env"][key]), str(current["env"][key])
        if one != two and not _REFERENCE.search(one + two) and _looks_plain(one) and _looks_plain(two):
            out.append(f"{key}: {one}  ->  {two}")
    return out


def never_proved(candidate: Any) -> bool:
    """Its only successes were calls anybody can make: it never proved the login."""
    shake = getattr(candidate, "handshake", None)
    return bool(shake is not None and shake.login_reason == login_check.PUBLIC_ONLY)


def _launch(entry: Mapping[str, Any]) -> list[str]:
    return [str(entry.get("command") or ""), *map(str, entry.get("args") or [])] if entry.get("command") else []


def _variables(entry: Mapping[str, Any]) -> set[str]:
    texts = [
        *_launch(entry),
        str(entry.get("url") or ""),
        *map(str, (entry.get("env") or {}).values()),
        *map(str, (entry.get("headers") or {}).values()),
    ]
    return {name for text in texts for name in _REFERENCE.findall(text)}


def _looks_plain(value: str) -> bool:
    """A setting, not a secret: short, and naming a host, a path, a number or a word."""
    from .secret_policy import looks_opaque

    return len(value) <= 200 and not looks_opaque(value)


def _carries_a_value(entry: Mapping[str, Any]) -> bool:
    """Does the entry hold a literal credential? Such an entry is never recorded."""
    from .secret_policy import holds_a_credential

    for block in ("env", "headers"):
        for key, value in (entry.get(block) or {}).items():
            text = str(value)
            if _REFERENCE.fullmatch(text.strip()) or "${" in text:
                continue
            if holds_a_credential(str(key), text):
                return True
    return False


__all__ = [
    "KNOWN_GOOD",
    "candidates",
    "differences",
    "last",
    "launched_paths",
    "load",
    "never_proved",
    "proved",
    "record",
    "restore_missing",
    "snapshot",
]
