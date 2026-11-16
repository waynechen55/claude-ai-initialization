"""Which ``[mcp.discovery.auth.profiles.*]`` entry describes a URL's credential.

Shared by the writer (what to call the variable and what its value looks
like), the verifier (a profile is the operator saying, in advance, "this
product wants a credential") and the agents' briefs (so an endpoint behind an
auth wall is proposed with the variable it will be read from, rather than not
proposed at all). Kept apart from all three so none has to import another.
"""

from __future__ import annotations

import re
from typing import Any, Mapping

from ..core.logging_setup import get_logger
from .address import parse as parse_address

log = get_logger("mcp.auth_profiles")


def profile_for(settings: Any, url: str) -> dict[str, str]:
    """The first profile whose ``match`` pattern finds ``url``'s host; ``{}`` if none does."""
    host = parse_address(url).host.lower() if url else ""
    if not host:
        return {}
    for key, raw in settings.dict_("mcp.discovery.auth.profiles", {}).items():
        if not isinstance(raw, Mapping):
            continue
        pattern = str(raw.get("match") or "").strip()
        if not pattern:
            continue
        try:
            if not re.search(pattern, host):
                continue
        except re.error as exc:
            log.warning("auth profile %s has an unusable match pattern: %s", key, exc)
            continue
        return {str(k): str(v) for k, v in raw.items()}
    return {}


__all__ = ["profile_for"]
