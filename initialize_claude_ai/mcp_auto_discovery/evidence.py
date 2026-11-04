"""What a run already knows about candidates nothing has proved yet -- so a tie is broken by evidence, not by order.

build.example.net's run had no credential at all, so every candidate was only
"waiting". The run had built the route that works -- osc signing with an SSH
key at api.example.net, waiting for nothing but a user name -- beside a password
bridge to build.example.net, whose API refuses passwords for this account. With
nothing proved the choice fell to confidence, and the password bridge was
written. Yet the run itself had logged "logins offered: basic@build.example.net,
Signature@api.example.net; chosen: client".

So each waiting candidate is given an evidence key (:func:`mark`), lower is
better, read by every chooser after proof (``StageOutcome.note_provisional``
and the cross-stage slot in ``TargetOutcome.absorb``):

1. the host's login history for the candidate's scheme -- accepted before
   first, refused and never accepted last (:mod:`.login_history`);
2. whether the address's API host itself asked for that scheme -- a client of
   the API logs in there; a scheme the address offers nowhere goes last;
3. how many values the operator has not supplied -- a key-file signature needs
   only a user name, a password login a user name *and* a password.

(``mcp.discovery.auth.evidence``.)
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Mapping

from ..core.logging_setup import get_logger

log = get_logger("mcp.evidence")

#: Fallback for ``mcp.discovery.auth.evidence.scheme_patterns``: a scheme told by the credential slots it reads.
SCHEME_PATTERNS: dict[str, list[str]] = {
    "basic": ["PASSWORD$", "PASSWD$", "_PASS$"],
    "signature": ["SSH_KEY$", "SSH_KEY_FILE$", "SIGNING_KEY$"],
    "bearer": ["TOKEN$", "_PAT$"],
}
#: Fallback for ``mcp.discovery.auth.evidence.value_schemes``: a scheme told by
#: the form a slot's value takes (``Authorization:Bearer ...``, ``Basic ...``).
VALUE_SCHEMES: dict[str, list[str]] = {
    "bearer": ["\\bbearer\\b"],
    "basic": ["\\bbasic\\b"],
}
#: Fallback for ``mcp.discovery.auth.evidence.method_schemes``: the schemes the
#: login plan's chosen method stands for -- a product that takes tokens often
#: advertises only OAuth and Basic in its challenge (Jira's personal access
#: tokens are sent as Bearer), so a chosen "token" counts the token schemes as offered.
METHOD_SCHEMES: dict[str, list[str]] = {
    "token": ["bearer", "token"],
    "apikey": ["bearer", "token", "apikey"],
    "basic": ["basic"],
    "client": ["signature"],
}

#: The key of a candidate nothing is known about.
NEUTRAL = (1, 1, 0)


def enabled(settings: Any) -> bool:
    return settings.bool_("mcp.discovery.auth.evidence.enabled", True)


def scheme_of(candidate: Any, settings: Any) -> str:
    """The login scheme a candidate performs: its own word, else the one its credential slots name."""
    own = str(getattr(candidate, "login_scheme", "") or "").lower()
    if own:
        return own
    # The slots it reads, credential or not: a key file's path is a setting
    # (where the key lies), and it still says the server signs with it.
    env = getattr(candidate, "env", {}) or {}
    names = [str(n).upper() for n in [*(getattr(candidate, "secret_env", {}) or {}), *env]]
    # The form a value takes says it best: "Authorization:Bearer ${…}" is a
    # bearer login whatever its slot is called (EXTRA_HEADERS promises nothing).
    values = [str(v) for v in env.values()]
    shapes = settings.dict_("mcp.discovery.auth.evidence.value_schemes", VALUE_SCHEMES) or VALUE_SCHEMES
    for scheme, found in shapes.items():
        if any(re.search(str(p), v, re.IGNORECASE) for p in found or [] for v in values):
            return str(scheme).lower()
    patterns = settings.dict_("mcp.discovery.auth.evidence.scheme_patterns", SCHEME_PATTERNS) or SCHEME_PATTERNS
    for scheme, found in patterns.items():
        if any(re.search(str(p), n) for p in found or [] for n in names):
            return str(scheme).lower()
    return ""


#: Fallbacks for ``mcp.discovery.auth.evidence.url_setting_keys`` /
#: ``url_assignment_pattern``: the settings that say where a server sends its
#: calls (and so its login), and the same said inside a file the entry names.
URL_SETTING_KEYS = (
    r"(^|_)(BASE_)?URL$",
    r"(^|_)API(_URL|_BASE|_HOST)?$",
    r"(^|_)ENDPOINT$",
    r"(^|_)SERVER(_URL)?$",
    r"(^|_)HOST$",
)
URL_ASSIGNMENT_PATTERN = r"""(?i)\b(?:api_?url|base_?url|endpoint|server_?url)\b\s*[=:]\s*["']?(https?://[^\s"',\\]+)"""


def login_hosts(candidate: Any, settings: Any) -> set[str]:
    """The hosts a candidate sends its calls -- and its login -- to: its URL settings, else the files it names.

    build-opensuse-org's agent bridge sent Basic to build.opensuse.org
    (MCP_BRIDGE_BASE_URL), which refuses it; the osc client bridge's manifest
    says ``apiurl = https://api.opensuse.org``, which accepts it. Both read as
    "Basic, asked at the API host", and confidence chose. Empty when nothing says.
    """
    from pathlib import Path
    from urllib.parse import urlsplit

    keys = settings.str_list("mcp.discovery.auth.evidence.url_setting_keys", list(URL_SETTING_KEYS))
    env = dict(getattr(candidate, "env", {}) or {})
    found: set[str] = set()
    for key, value in env.items():
        if any(re.search(k, str(key), re.IGNORECASE) for k in keys if k):
            host = (urlsplit(str(value)).hostname or "") if "://" in str(value) else ""
            if host:
                found.add(host.lower())
    if found:
        return found
    pattern = settings.str_("mcp.discovery.auth.evidence.url_assignment_pattern", URL_ASSIGNMENT_PATTERN)
    for value in [*env.values(), *[str(a) for a in getattr(candidate, "args", []) or []]]:
        path = Path(str(value))
        try:
            if not str(value).startswith("/") or not path.is_file() or path.stat().st_size > 2_000_000:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for url in re.findall(pattern, _decoded(text)):
            host = urlsplit(url).hostname or ""
            if host:
                found.add(host.lower())
    return found


def _decoded(text: str) -> str:
    """A JSON manifest's strings, one per line, so a config held inside one reads as written."""
    import json

    try:
        data = json.loads(text)
    except ValueError:
        return text
    strings: list[str] = []
    stack = [data]
    while stack:
        item = stack.pop()
        if isinstance(item, str):
            strings.append(item)
        elif isinstance(item, dict):
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
    return "\n".join(strings)


def unsupplied(candidate: Any, settings: Any = None) -> int:
    """How many values the operator still has to supply for it -- a header's credential included.

    A URL entry waiting for ``Authorization: Bearer ${TOKEN}`` lists nothing as
    pending: confluence.example.com's /mcp counted 0 values to supply against
    mcp-atlassian's 1 and won the tie, although both wanted the same token.
    (``mcp.discovery.auth.evidence.count_header_credentials``.)
    """
    shake = getattr(candidate, "handshake", None)
    pending = set(getattr(shake, "credential_pending", []) or []) if shake is not None else set()
    counts = settings is None or settings.bool_("mcp.discovery.auth.evidence.count_header_credentials", True)
    if not pending and counts and getattr(candidate, "needs_credential", False):
        pending |= {str(k) for k in (getattr(candidate, "secret_env", None) or {})}
        for value in (getattr(candidate, "headers", None) or {}).values():
            pending |= set(re.findall(r"\$\{([A-Za-z_][A-Za-z0-9_]*)", str(value)))
    return len(pending)


def key(ctx: Any, candidate: Any, host: str, plan: Mapping[str, Any] | None) -> tuple[int, int, int]:
    """``(history, api host, unsupplied)`` for one candidate; lower is better."""
    from . import login_history
    from .login_methods import api_hosts

    settings = ctx.settings
    scheme = scheme_of(candidate, settings)
    history = login_history.rank(ctx, host, scheme) if scheme and host else 1
    offered = [o for o in (plan or {}).get("offered") or [] if isinstance(o, Mapping)]
    apis = set(api_hosts(host, settings)) if host else set()
    at_api = {str(o.get("scheme") or "").lower() for o in offered if str(o.get("host") or "") in apis}
    anywhere = {str(o.get("scheme") or "").lower() for o in offered}
    chosen = str(((plan or {}).get("chosen") or {}).get("method") or "").lower()
    methods = settings.dict_("mcp.discovery.auth.evidence.method_schemes", METHOD_SCHEMES) or METHOD_SCHEMES
    if anywhere:
        anywhere |= {str(x).lower() for x in methods.get(chosen) or []}
    # How well its login fits what the address asks for -- before "values still
    # to supply", which only breaks ties within one level. build-opensuse-org's
    # proxy (one value, a login of no known kind) outranked the client bridge
    # (user name and password, the Basic login the address offers) on "one is
    # fewer than two". 0 the API host asks for it, 1 the address offers it,
    # 2 its kind is unknown, 3 offered nowhere; with nothing offered, all 1.
    sends_to = (
        login_hosts(candidate, settings) if settings.bool_("mcp.discovery.auth.evidence.host_matters", True) else set()
    )
    if not anywhere:
        api_rank = 1
    elif (
        scheme
        and scheme in at_api
        and (
            not sends_to
            or sends_to
            & {
                str(o.get("host") or "").lower()
                for o in offered
                if str(o.get("scheme") or "").lower() == scheme and str(o.get("host") or "") in apis
            }
        )
    ):
        # Asked at the API host, and sent to the host that asked (or nothing says elsewhere).
        api_rank = 0
    elif scheme and scheme in anywhere:
        api_rank = 1
    elif not scheme:
        api_rank = 2 if settings.bool_("mcp.discovery.auth.evidence.unknown_below_offered", True) else 1
    else:
        # A scheme the address offers nowhere it was asked: it cannot log in.
        api_rank = 3 if settings.bool_("mcp.discovery.auth.evidence.unoffered_last", True) else 1
    return history, api_rank, unsupplied(candidate, settings)


def mark(ctx: Any, candidates: Iterable[Any], host: str, plan: Mapping[str, Any] | None) -> None:
    """Give each waiting candidate its evidence key and a sentence saying why."""
    if not enabled(ctx.settings):
        return
    for candidate in candidates:
        if not getattr(candidate, "needs_credential", False):
            continue
        found = key(ctx, candidate, host, plan)
        candidate.evidence = found
        words = []
        scheme = scheme_of(candidate, ctx.settings) or "its login"
        if found[0] == 0:
            words.append(f"{host} accepted {scheme} before")
        elif found[0] == 2:
            words.append(f"{host} refused {scheme} before")
        if found[1] == 0:
            words.append(f"{host}'s API host asks for {scheme}")
        words.append(f"{found[2]} value(s) still to supply")
        candidate.evidence_why = "; ".join(words)
        log.debug(
            "%s: evidence %s for %s (%s); built for the API: %s; worked before: %s",
            host,
            found,
            getattr(candidate, "label", candidate),
            scheme,
            bool(getattr(candidate, "api_kind", "")),
            bool(getattr(candidate, "worked_before", "")),
        )


def order(candidate: Any) -> tuple[int, int, int]:
    return tuple(getattr(candidate, "evidence", ()) or NEUTRAL)  # type: ignore[return-value]


__all__ = ["NEUTRAL", "SCHEME_PATTERNS", "enabled", "key", "login_hosts", "mark", "order", "scheme_of", "unsupplied"]
