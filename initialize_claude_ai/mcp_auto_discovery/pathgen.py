"""Candidate URL generation for stage 1.

There is no registry of "where an MCP endpoint lives on a server", so discovery
is an ordered search: the conventional paths first (``/mcp``), then the versioned
forms the user asked about (``/v1/mcp``, ``/api/v1/mcp``), then the long tail --
and, for hosts that are plausibly local, the ports MCP servers habitually bind.

Everything here is driven by ``[mcp.discovery]``: adding a new path convention is
a one-line config change, not a code change.  The ordering matters more than the
size of the list, because the search stops at the first endpoint that completes a
handshake.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable
from urllib.parse import urljoin, urlsplit, urlunsplit

from ..core.config import Settings
from ..core.logging_setup import get_logger
from .address import is_local, is_loopback
from .address import parse as parse_address
from .types import Transport

log = get_logger("mcp.pathgen")

#: Rank distance between one origin's candidates and the next one's. Wide enough
#: that a whole origin's path list stays together in the ordering, so the second
#: origin is not interleaved with the first when the cap starts discarding.
_ORIGIN_STRIDE = 1000


@dataclass(frozen=True)
class UrlCandidate:
    url: str
    transport: str
    #: Why this URL was tried; shown in the report so the search is auditable.
    reason: str
    #: Lower sorts first.
    rank: int = 100

    def key(self) -> str:
        return f"{self.transport}:{self.url.rstrip('/')}"


def expand_paths(templates: Iterable[str], versions: Iterable[str]) -> list[str]:
    """Expand ``{version}`` in each template, preserving template order.

    A template without ``{version}`` yields itself once; one with it yields a
    path per version segment, so ``/api/{version}/mcp`` covers ``/api/v1/mcp``,
    ``/api/v2/mcp`` and so on.
    """
    versions = list(versions)
    out: list[str] = []
    for template in templates:
        text = str(template)
        if "{version}" not in text:
            _append(out, _normalise_path(text))
            continue
        for version in versions:
            _append(out, _normalise_path(text.replace("{version}", str(version).strip("/"))))
    return out


def base_urls(target: str, config: Settings) -> list[str]:
    """Every origin worth probing for ``target``.

    A bare hostname becomes ``https://host``; a loopback or private address also
    gets ``http://`` (nobody runs TLS on localhost) and the ports from
    ``local_ports``, because that is where a developer's MCP server actually is.

    With ``try_both_schemes`` -- step B of the tier-1 checklist -- the other
    scheme is added for *every* host, after the primary one. Plenty of internal
    services are plaintext-only, and "https did not answer" is not the same
    finding as "this host does not speak MCP". The cleartext origin sorts last,
    and the prober refuses to put a credential on it.
    """
    primary = parse_address(target).url
    parts = urlsplit(primary)
    if not parts.netloc:
        return []

    origins = [_origin(parts)]
    host = parts.hostname or ""
    explicit_port = bool(parts.port)

    ports: list[int] = [int(p) for p in config.list_("extra_ports", []) if str(p).strip().isdigit()]
    if is_local(host):
        ports.extend(int(p) for p in config.list_("local_ports", []) if str(p).strip().isdigit())
        # Plain HTTP first for loopback: it is the common case and avoids a TLS
        # handshake failure on every probe.
        if parts.scheme == "https" and is_loopback(host):
            origins.insert(0, _origin(parts._replace(scheme="http")))

    if not explicit_port:
        for port in dict.fromkeys(ports):
            for scheme in _schemes_for(host, parts.scheme):
                origins.append(f"{scheme}://{host}:{port}")

    if config.bool_("try_both_schemes", True):
        other = "http" if parts.scheme == "https" else "https"
        origins.append(_origin(parts._replace(scheme=other)))

    seen: list[str] = []
    for origin in origins:
        if origin not in seen:
            seen.append(origin)
    return seen


def generate(target: str, config: Settings, *, mandatory: Iterable[str] = ()) -> list[UrlCandidate]:
    """The ordered probe list for ``target``, capped by ``max_candidates``.

    ``mandatory`` is the tier-1 checklist's step A: the paths that have to be
    probed before anyone may say "this target has no MCP endpoint". They are
    emitted first, on the address's own origin, so the candidate cap can never
    be what silently turned the checklist into a partial search.
    """
    versions = config.str_list("version_segments")
    http_paths = expand_paths(config.str_list("path_templates"), versions)
    sse_paths = expand_paths(config.str_list("sse_path_templates"), versions)
    ws_paths = expand_paths(config.str_list("websocket_path_templates", []), versions)
    queries = [str(q).strip() for q in config.str_list("query_templates", []) if str(q).strip()]
    query_bases = config.str_list("query_bases") or [""]
    limit = config.int_("max_candidates", 200)

    candidates: list[UrlCandidate] = []
    seen: set[str] = set()

    def add(url: str, transport: str, reason: str, rank: int) -> None:
        candidate = UrlCandidate(url=url, transport=transport, reason=reason, rank=rank)
        if candidate.key() in seen:
            return
        seen.add(candidate.key())
        candidates.append(candidate)

    origins = base_urls(target, config)
    required = [str(m).strip() for m in dict.fromkeys(mandatory) if str(m).strip()]

    # The checklist first, on every origin -- which is the only part of the
    # search the alternate scheme is guaranteed to get, because probing every
    # template twice would double a run to say something the checklist already
    # says in ten requests.
    for origin_rank, origin in enumerate(origins):
        for index, entry in enumerate(required):
            url, transport = _mandated(origin, entry, ws_paths, sse_paths)
            add(url, transport, f"required by the tier-1 checklist ({entry})", 1 + origin_rank * len(required) + index)

    for origin_rank, origin in enumerate(origins):
        # If the target already names a path, that is the operator's best guess:
        # probe it before anything we invented.
        supplied = parse_address(target).path.strip("/")
        if origin_rank == 0 and supplied:
            add(urljoin(origin + "/", supplied), Transport.STREAMABLE_HTTP, "path supplied with the target", 0)

        base = _ORIGIN_STRIDE * origin_rank
        for path_rank, path in enumerate(http_paths):
            add(f"{origin}{path}", Transport.STREAMABLE_HTTP, f"path template {path}", base + 500 + path_rank)
        for path_rank, path in enumerate(sse_paths):
            add(f"{origin}{path}", Transport.SSE, f"legacy SSE path {path}", base + 600 + path_rank)
        for query_rank, url in enumerate(_query_urls(origin, query_bases, queries)):
            add(url, Transport.STREAMABLE_HTTP, "query-string transport selector", base + 700 + query_rank)
        for ws_rank, path in enumerate(ws_paths):
            add(f"{origin}{path}", Transport.WEBSOCKET, f"WebSocket path {path}", base + 800 + ws_rank)

    candidates.sort(key=lambda c: c.rank)
    if len(candidates) > limit:
        log.debug("capping %d candidates at %d", len(candidates), limit)
        candidates = candidates[:limit]
    return candidates


def _query_urls(origin: str, bases: Iterable[str], queries: Iterable[str]) -> list[str]:
    """``?format=mcp`` and friends, on the origin root and on each base path."""
    out: list[str] = []
    for base in bases:
        path = _normalise_path(base) if str(base).strip() else ""
        for query in queries:
            url = f"{origin}{path}{query if query.startswith('?') else '?' + query}"
            if url not in out:
                out.append(url)
    return out


def _mandated(origin: str, entry: str, ws_paths: Iterable[str], sse_paths: Iterable[str]) -> tuple[str, str]:
    """Turn one checklist entry into a URL and the transport it should be tried as.

    The checklist is written the way an operator says it -- ``/mcp/sse``,
    ``/ws``, ``?format=mcp`` -- so the shape of the entry, not a second list of
    configuration to keep in step with it, decides how it is probed.
    """
    if entry.startswith("?"):
        return f"{origin}{entry}", Transport.STREAMABLE_HTTP
    path = _normalise_path(entry)
    if path in set(ws_paths) or path.rstrip("/").endswith("/ws") or "/ws/" in path:
        return f"{origin}{path}", Transport.WEBSOCKET
    if path in set(sse_paths) or path.rstrip("/").endswith("/sse"):
        return f"{origin}{path}", Transport.SSE
    return f"{origin}{path}", Transport.STREAMABLE_HTTP


def well_known_urls(target: str, config: Settings) -> list[str]:
    """Standards-based metadata URLs, probed once per origin with a cheap GET."""
    origins = base_urls(target, config)[:1]
    return [f"{origin}{_normalise_path(path)}" for origin in origins for path in config.str_list("well_known_paths")]


def api_spec_urls(target: str, config: Settings) -> list[str]:
    """Machine-readable API descriptions, used by the bridge stage."""
    origins = base_urls(target, config)[:1]
    return [f"{origin}{_normalise_path(path)}" for origin in origins for path in config.str_list("api_spec_paths")]


def api_doc_urls(target: str, config: Settings) -> list[str]:
    """Pages that render an API description for people (Swagger UI, ReDoc), read for its link."""
    origins = base_urls(target, config)[:1]
    return [f"{origin}{_normalise_path(path)}" for origin in origins for path in config.str_list("api_doc_pages")]


def doc_urls(target: str, config: Settings) -> list[str]:
    """Pages the target itself might announce an MCP endpoint on (step D).

    Read from ``[mcp.discovery.web_grounding]``, which is a sub-section of the
    discovery settings this module is given, so the lookup goes through it.
    """
    grounding = config.section("web_grounding")
    origins = base_urls(target, config)[:1]
    return [f"{origin}{_normalise_path(path)}" for origin in origins for path in grounding.str_list("doc_paths")]


def covered(urls: Iterable[str], mandatory: Iterable[str]) -> tuple[list[str], list[str]]:
    """Split a checklist into the entries these URLs cover and the ones they miss.

    Compared on the path (and query) alone, because the same checklist entry is
    probed on every origin and covering it once is what step A asks for.
    """
    probed: set[str] = set()
    for url in urls:
        parts = urlsplit(str(url))
        if parts.query:
            probed.add(f"?{parts.query}")
        probed.add(parts.path.rstrip("/") or "/")
        probed.add(parts.path or "/")

    hit: list[str] = []
    missed: list[str] = []
    for entry in mandatory:
        text = str(entry).strip()
        if not text:
            continue
        wanted = text if text.startswith("?") else _normalise_path(text)
        (hit if (wanted in probed or wanted.rstrip("/") in probed) else missed).append(text)
    return hit, missed


def mine_html(body: str, base_url: str, patterns: Iterable[str], *, limit: int = 20) -> list[str]:
    """Pull MCP-looking links out of a landing page.

    Plenty of services document their MCP endpoint on their own home page; this
    is cheaper and more reliable than guessing when it works.
    """
    found: list[str] = []
    for pattern in patterns:
        try:
            regex = re.compile(pattern)
        except re.error as exc:
            log.warning("ignoring invalid html_link_pattern %r: %s", pattern, exc)
            continue
        for match in regex.finditer(body or ""):
            raw = (match.group(1) if match.groups() else match.group(0)).strip()
            if not raw or raw.startswith(("#", "mailto:", "javascript:")):
                continue
            url = raw if raw.startswith(("http://", "https://")) else urljoin(base_url, raw)
            if url not in found:
                found.append(url)
            if len(found) >= limit:
                return found
    return found


# ----------------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------------


def _append(out: list[str], value: str) -> None:
    if value and value not in out:
        out.append(value)


def _normalise_path(path: str) -> str:
    text = str(path).strip()
    if not text:
        return ""
    if not text.startswith("/"):
        text = "/" + text
    # Keep a deliberate trailing slash: "/mcp" and "/mcp/" are different routes
    # on enough frameworks to be worth probing separately.
    return re.sub(r"/{2,}", "/", text)


def _origin(parts) -> str:
    return urlunsplit((parts.scheme, parts.netloc, "", "", ""))


def _schemes_for(host: str, default: str) -> list[str]:
    return ["http"] if is_loopback(host) else [default]


__all__ = [
    "UrlCandidate",
    "generate",
    "base_urls",
    "covered",
    "expand_paths",
    "well_known_urls",
    "api_doc_urls",
    "api_spec_urls",
    "doc_urls",
    "mine_html",
]
