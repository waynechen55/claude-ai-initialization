"""Finding and reading a target's machine-readable API description.

Stage 3 builds a bridge from an OpenAPI document, so a target whose document
is not at one of ``mcp.discovery.api_spec_paths``, or is not JSON, used to get
no bridge at all. build.example.net is the case that showed it: its description is
``OBS-v2.10.50.yaml``, named only inside the Swagger UI page at ``/apidocs/``
and the initializer script that page loads. Nothing about that is particular
to one product -- Swagger UI and ReDoc are how most services publish their
API for people -- so the pages to read and the patterns that find a link in
them are configuration (``api_doc_pages``, ``api_spec_link_patterns``,
``api_doc_script_patterns``).

YAML is read with PyYAML when it is installed. It is optional, like every
dependency beyond the standard library: without it a YAML description is
still found, and its URL is reported for the agent and the operator.
"""

from __future__ import annotations

import json
import re
from typing import Any, Iterable, Mapping
from urllib.parse import urljoin, urlsplit

from ..core.logging_setup import get_logger

log = get_logger("mcp.apispec")

#: What makes a document an API description rather than any JSON or YAML.
_MARKERS = ("openapi", "swagger")


def yaml_available() -> bool:
    try:  # optional
        import yaml  # type: ignore[import-not-found]  # noqa: F401
    except ModuleNotFoundError:
        return False
    return True


def parse(text: str) -> Mapping[str, Any] | None:
    """The document in ``text`` if it is an API description with operations; ``None`` otherwise."""
    body = (text or "").strip()
    if not body:
        return None
    payload: Any = None
    if body[:1] in "{[":
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            payload = None
    if payload is None and yaml_available():
        import yaml  # type: ignore[import-not-found]

        loader = getattr(yaml, "CSafeLoader", None) or yaml.SafeLoader
        try:
            payload = yaml.load(body, Loader=loader)  # noqa: S506 - a safe loader, always
        except Exception as exc:  # noqa: BLE001 - not YAML either
            log.debug("not a YAML document: %s", exc)
            payload = None
    return payload if is_description(payload) else None


def is_description(payload: Any) -> bool:
    return isinstance(payload, Mapping) and bool(payload.get("paths"))


def looks_like_description(text: str) -> bool:
    """Cheap check for a YAML description that cannot be parsed here: its first keys say what it is."""
    head = (text or "").lstrip()[:400].lower()
    return any(re.search(rf"(?m)^['\"]?{marker}['\"]?\s*:", head) for marker in _MARKERS)


def links(
    body: str,
    base_url: str,
    patterns: Iterable[str],
    *,
    same_host_as: str = "",
    limit: int = 10,
) -> list[str]:
    """URLs matched by ``patterns`` in ``body``, resolved against ``base_url``.

    With ``same_host_as`` a link to any other host is dropped: Swagger UI's
    stock initializer points at a public demo description, and a bridge built
    from that would describe somebody else's API.
    """
    host = (urlsplit(same_host_as).hostname or "").lower() if same_host_as else ""
    found: list[str] = []
    for pattern in patterns:
        try:
            regex = re.compile(pattern)
        except re.error as exc:
            log.warning("ignoring invalid pattern %r: %s", pattern, exc)
            continue
        for match in regex.finditer(body or ""):
            raw = (match.group(1) if match.groups() else match.group(0)).strip()
            if not raw or raw.startswith(("#", "data:", "javascript:")):
                continue
            url = urljoin(base_url, raw)
            if urlsplit(url).scheme not in ("http", "https"):
                continue
            if host and (urlsplit(url).hostname or "").lower() != host:
                continue
            if url not in found:
                found.append(url)
            if len(found) >= limit:
                return found
    return found


__all__ = ["is_description", "links", "looks_like_description", "parse", "yaml_available"]
