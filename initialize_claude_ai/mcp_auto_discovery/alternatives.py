"""The other servers an address was left waiting with -- kept, so the run after a credential appears proves them too.

build.example.net's run had no credential, wrote the password bridge, and the route
that works -- osc signing at api.example.net with an SSH key -- went nowhere. When
the operator then supplied values, nothing could try that route again without a
whole new discovery.

So when an address is written with a server that is still waiting for a
credential, every *other* candidate that waited for one is recorded here, as
the entry it would have been (``${...}`` references, never a value). Each later
run -- ``--mcp-recheck`` included -- tries them at their own tier beside the
registered entry (:func:`candidates`), and proof decides which one stays. An
address written with a working server has nothing waiting, and its record is
cleared. (``[mcp.alternatives]``.)
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

from ..core.context import RunContext
from ..core.logging_setup import get_logger

log = get_logger("mcp.alternatives")

#: The source given to a candidate rebuilt from the record.
ALTERNATIVE = "alternative"

#: Fallbacks for ``[mcp.alternatives]``, equal to the packaged values.
DEFAULT_FILE = "mcp-alternatives.json"
DEFAULT_KEEP = 4


def enabled(ctx: RunContext) -> bool:
    return ctx.settings.bool_("mcp.alternatives.enabled", True)


def path(ctx: RunContext) -> Path:
    return Path(ctx.state_dir) / ctx.settings.str_("mcp.alternatives.file", DEFAULT_FILE)


def load(ctx: RunContext) -> dict[str, list[dict[str, Any]]]:
    try:
        data = json.loads(path(ctx).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {str(k): [r for r in v if isinstance(r, dict)] for k, v in data.items() if isinstance(v, list)}


def _save(ctx: RunContext, data: Mapping[str, Any]) -> None:
    file = path(ctx)
    file.parent.mkdir(parents=True, exist_ok=True)
    temporary = file.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    temporary.replace(file)


def record(ctx: RunContext, target: str, rows: Sequence[Mapping[str, Any]]) -> None:
    """Replace ``target``'s alternatives with ``rows`` (``{name, entry, why}``); [] clears them. Never in a dry run."""
    if not enabled(ctx) or ctx.dry_run:
        return
    from .known_good import _carries_a_value

    keep = max(0, ctx.settings.int_("mcp.alternatives.keep", DEFAULT_KEEP))
    kept = [
        {
            "name": str(r.get("name") or ""),
            "entry": dict(r.get("entry") or {}),
            "why": str(r.get("why") or ""),
            "recorded": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        }
        for r in rows
        if r.get("entry") and not _carries_a_value(dict(r.get("entry") or {}))
    ][:keep]
    data = load(ctx)
    if not kept and target not in data:
        return
    if kept:
        data[target] = kept
    else:
        data.pop(target, None)
    try:
        _save(ctx, data)
    except OSError as exc:
        log.debug("could not record alternatives: %s", exc)
        return
    if kept:
        log.info(
            "%s: kept %d waiting alternative(s) for the next run: %s",
            target,
            len(kept),
            ", ".join(r["name"] for r in kept),
        )


def candidates(ctx: RunContext, target: str, stage: str) -> list[Any]:
    """``target``'s recorded alternatives that belong to ``stage``'s tier and are not registered as they are."""
    if not enabled(ctx) or not ctx.settings.bool_("mcp.alternatives.try_again", True):
        return []
    from .existing import ExistingRegistrations, Registration

    existing = ExistingRegistrations.for_run(ctx)
    registered = [r.entry for r in existing.registrations()]
    out: list[Any] = []
    for row in load(ctx).get(target) or []:
        entry = dict(row.get("entry") or {})
        if not entry or existing.tier_of(entry) != stage or entry in registered:
            continue
        candidate = existing.candidate(
            Registration(str(row.get("name") or ""), entry, ALTERNATIVE), stage, record_origin=False
        )
        candidate.existing_name = ""  # an alternative, not the registration in place
        candidate.source = f"a waiting alternative kept from an earlier run ({row.get('recorded', 'earlier')})"
        candidate.add_note(candidate.source + (f": {row['why']}" if row.get("why") else ""))
        out.append(candidate)
    return out


__all__ = ["ALTERNATIVE", "candidates", "load", "record"]
