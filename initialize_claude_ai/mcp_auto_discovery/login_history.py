"""Which login each host refused and accepted, run after run -- so the next run does not start with a refused one.

build.example.net refused its password login in every run for days, and every run
chose that login first again ("chosen: basic"), because the choice came from a
fixed ranking. The SSH-key route on api.example.net, the one its documents name,
was always tried second -- and buried under the first one's "REJECTED".

So every login verdict with a credential actually sent is recorded per host and
scheme (``mcp.discovery.auth.history``): how often it was accepted and refused,
and when last -- with a fingerprint of the account (never a name or a value),
so a different account starts afresh. :func:`rank` orders schemes by it: one
accepted before comes first, one refused and never accepted for this account
comes last. A refusal with nothing sent is no refusal and is not recorded.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Iterable, Mapping

from ..core.context import RunContext
from ..core.fileio import write_private
from ..core.logging_setup import get_logger
from . import login_check

log = get_logger("mcp.login_history")

#: Fallback for ``mcp.discovery.auth.history.file``.
DEFAULT_FILE = "mcp-login-history.json"


def enabled(ctx: RunContext) -> bool:
    return ctx.settings.bool_("mcp.discovery.auth.history.enabled", True)


def path(ctx: RunContext) -> Path:
    return Path(ctx.state_dir) / ctx.settings.str_("mcp.discovery.auth.history.file", DEFAULT_FILE)


def load(ctx: RunContext) -> dict[str, dict[str, dict[str, Any]]]:
    """``{host: {scheme: {"accepted", "refused", "last", "account"}}}``; empty when there is none."""
    try:
        data = json.loads(path(ctx).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {
        str(h): {str(s): dict(r) for s, r in v.items() if isinstance(r, dict)}
        for h, v in data.items()
        if isinstance(v, dict)
    }


def record(ctx: RunContext, host: str, candidates: Iterable[Any]) -> None:
    """Note the login verdict of every candidate for ``host`` that sent a credential. Never in a dry run."""
    if not enabled(ctx) or ctx.dry_run or not host:
        return
    data = load(ctx)
    changed = False
    for candidate in candidates:
        scheme = str(getattr(candidate, "login_scheme", "") or "").lower()
        shake = getattr(candidate, "handshake", None)
        if not scheme or shake is None or not shake.login or login_check.nothing_sent(shake):
            continue
        if shake.login_reason in (login_check.SAME_AS_ANONYMOUS, login_check.NOTHING_SENT):
            continue  # nothing shows the credential reached the server: no verdict about the login
        if shake.credential_sent is not True and not shake.login_credential:
            continue
        accepted = shake.login == login_check.ACCEPTED
        refused = shake.login in login_check.REFUSED
        if not (accepted or refused):
            continue
        account = str(shake.login_credential or "")
        row = data.setdefault(host.lower(), {}).get(scheme) or {}
        if row.get("account") and account and row.get("account") != account:
            row = {}  # another account: what the last one got says nothing about this one
        row["accepted"] = int(row.get("accepted", 0)) + (1 if accepted else 0)
        row["refused"] = int(row.get("refused", 0)) + (1 if refused else 0)
        row["last"] = "accepted" if accepted else "refused"
        row["when"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        if account:
            row["account"] = account
        data[host.lower()][scheme] = row
        changed = True
    if not changed:
        return
    try:
        target = path(ctx)
        target.parent.mkdir(parents=True, exist_ok=True)
        write_private(target, json.dumps(data, indent=2, sort_keys=True) + "\n")
    except OSError as exc:
        log.debug("could not record login history: %s", exc)


def rank(ctx: RunContext, host: str, scheme: str) -> int:
    """0 accepted last time, 1 nothing known, 2 refused and never accepted -- for ``host``."""
    if not enabled(ctx):
        return 1
    row = load(ctx).get(str(host or "").lower(), {}).get(str(scheme or "").lower()) or {}
    if row.get("last") == "accepted" or int(row.get("accepted", 0)) > 0:
        return 0
    if int(row.get("refused", 0)) > 0:
        return 2
    return 1


def summary(ctx: RunContext, host: str) -> dict[str, str]:
    """``{scheme: "accepted" | "refused"}`` last seen for ``host`` -- for the plan, the brief and the report."""
    rows: Mapping[str, Mapping[str, Any]] = load(ctx).get(str(host or "").lower(), {})
    return {s: str(r.get("last") or "") for s, r in rows.items() if r.get("last")}


__all__ = ["load", "path", "rank", "record", "summary"]
