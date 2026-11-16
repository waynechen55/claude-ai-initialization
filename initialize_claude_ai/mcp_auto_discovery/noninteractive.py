"""Discovery never waits for a person: nothing it starts can open a browser, and a URL one tries to open is a finding.

During a run on another machine a browser window opened by itself on GitHub's
sign-in page and waited: something phase 4 started -- a trial of a launcher
that performs an OAuth login for a remote server, or a check through Claude
Code of an entry with no token -- found the desktop phase 2 had set up for its
own sign-in, and used it. Discovery must run unattended.

So for the whole of phase 4 (:func:`session`):

* the variables that reach the desktop are taken away from every process it
  starts (``mcp.noninteractive.strip``: the display, the session bus);
* the programs that open URLs (``opener_names``: ``xdg-open`` and its kin)
  resolve to a recorder placed first on the search path, and ``BROWSER`` names
  it: it writes the URL down and returns at once;
* :func:`opened_since` reads what was recorded, so the verifier can say which
  candidate tried to open a sign-in page -- "needs a browser login on <host>",
  a step for the operator, never a wait and never a verdict on the server.

Phase 2's own sign-in, which opens a browser for the operator on purpose, is
untouched: this applies only while phase 4 runs, and everything is restored.
"""

from __future__ import annotations

import os
import stat
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlsplit

from ..core.logging_setup import get_logger

log = get_logger("mcp.noninteractive")

#: Fallbacks, equal to the packaged ``[mcp.noninteractive]`` values.
STRIP = ("DISPLAY", "WAYLAND_DISPLAY", "DBUS_SESSION_BUS_ADDRESS")
OPENER_NAMES = ("xdg-open", "sensible-browser", "x-www-browser", "www-browser", "gnome-open", "kde-open")
LOG_VARIABLE = "ICA_BROWSER_REQUESTS"

_RECORDER = """#!/bin/sh
# Written by initialize-claude-ai for its MCP discovery: a URL asked to be
# opened is recorded, never opened -- discovery runs unattended.
printf '%s\\n' "$*" >> "${{{variable}:-/dev/null}}"
exit 0
"""


def enabled(ctx: Any) -> bool:
    return ctx.settings.bool_("mcp.noninteractive.enabled", True)


def log_path(ctx: Any) -> Path:
    return Path(ctx.work_dir) / ctx.settings.str_("mcp.noninteractive.directory", "noninteractive") / "requests.log"


def _write_recorder(directory: Path, names: list[str], variable: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    body = _RECORDER.format(variable=variable)
    for name in names:
        target = directory / name
        if not target.exists() or target.read_text(encoding="utf-8", errors="replace") != body:
            target.write_text(body, encoding="utf-8")
        target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


@contextmanager
def session(ctx: Any) -> Iterator[None]:
    """Phase 4's environment: no desktop for anything it starts, every URL-opener a recorder. Restored on exit."""
    if not enabled(ctx) or getattr(ctx, "dry_run", False):
        yield
        return
    config = ctx.settings.section("mcp.noninteractive")
    variable = config.str_("log_variable", LOG_VARIABLE)
    record = log_path(ctx)
    directory = record.parent / "bin"
    try:
        _write_recorder(directory, config.str_list("opener_names", list(OPENER_NAMES)), variable)
        record.parent.mkdir(parents=True, exist_ok=True)
        record.touch()
    except OSError as exc:
        log.warning("could not prepare the URL recorder (%s); phase 4 runs without it", exc)
        yield
        return
    saved = {name: os.environ.get(name) for name in [*config.str_list("strip", list(STRIP)), "BROWSER", variable]}
    runner = getattr(ctx, "runner", None)
    extra = list(getattr(runner, "extra_paths", []) or []) if runner is not None else None
    for name in config.str_list("strip", list(STRIP)):
        os.environ.pop(name, None)
    os.environ["BROWSER"] = str(directory / config.str_list("opener_names", list(OPENER_NAMES))[0])
    os.environ[variable] = str(record)
    saved_path = os.environ.get("PATH", "")
    os.environ["PATH"] = os.pathsep.join([str(directory), saved_path]) if saved_path else str(directory)
    if runner is not None and extra is not None:
        runner.extra_paths = [str(directory), *[p for p in extra if p != str(directory)]]
    log.debug("phase 4 runs unattended: no display, URL openers record to %s", record)
    try:
        yield
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        os.environ["PATH"] = saved_path
        if runner is not None and extra is not None:
            runner.extra_paths = extra


def mark(ctx: Any) -> int:
    """A position in the record, to read only what came after it (:func:`opened_since`)."""
    try:
        return log_path(ctx).stat().st_size
    except OSError:
        return 0


def opened_since(ctx: Any, position: int) -> list[str]:
    """URLs something asked to open after ``position``, in order, without repeats."""
    try:
        with log_path(ctx).open("r", encoding="utf-8", errors="replace") as handle:
            handle.seek(position)
            lines = [line.strip() for line in handle if line.strip()]
    except OSError:
        return []
    return list(dict.fromkeys(lines))


def hosts_of(urls: list[str]) -> list[str]:
    """The hosts of ``urls`` (the sign-in page each led to), without repeats."""
    out: list[str] = []
    for url in urls:
        for word in url.split():
            host = urlsplit(word).hostname or ""
            if host and host not in out:
                out.append(host)
    return out


def variable(ctx: Any) -> str:
    return ctx.settings.str_("mcp.noninteractive.log_variable", LOG_VARIABLE)


def record_for_launch(ctx: Any) -> Path | None:
    """A fresh record file for one launch, while phase 4's session is active; None otherwise."""
    if not enabled(ctx) or getattr(ctx, "dry_run", False) or variable(ctx) not in os.environ:
        return None
    import uuid

    target = log_path(ctx).parent / "launches" / f"{uuid.uuid4().hex}.log"
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.touch()
    except OSError:
        return None
    return target


def take(record: Path) -> list[str]:
    """The URLs one launch asked to open (its record is removed); [] for none."""
    try:
        lines = [
            line.strip() for line in record.read_text(encoding="utf-8", errors="replace").splitlines() if line.strip()
        ]
        record.unlink()
    except OSError:
        return []
    return list(dict.fromkeys(lines))


__all__ = ["hosts_of", "mark", "opened_since", "record_for_launch", "session", "take", "variable"]
