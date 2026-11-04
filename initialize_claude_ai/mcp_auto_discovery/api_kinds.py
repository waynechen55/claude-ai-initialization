"""Kinds of machine-readable API beyond an OpenAPI document.

smelt.example.net has no OpenAPI description and no MCP endpoint, and it has a
GraphQL API that a published, generic MCP server (``mcp-graphql``) serves
perfectly once it is pointed at it. Discovery only knew how to look for an
OpenAPI document, so it reported "the target publishes no machine-readable API
description", searched the registries for the product's own name, and never
considered the one server that works -- the operator had registered it by hand.

The lesson is not about GraphQL. An API has a *kind*, each kind is found in its
own way, and each kind may already have generic servers that bridge it. So each
``[mcp.discovery.api_kinds.<name>]`` table says

* how to recognise one: ``paths`` to ask at, the ``method``, ``body`` and
  ``content_type`` of the request; ``content_types`` the answer must be served
  as, ``required_keys`` (dotted) its parsed JSON must hold, and
  ``success_markers`` that must appear in it (GraphQL introspection answers
  with ``data.__schema``); ``doc_pages`` and ``link_patterns`` find an endpoint
  that a page for people names;
* or, instead, ``finding`` -- the stage finding another detector already
  records (the OpenAPI search in :mod:`.apispec` records ``openapi_url``);
* ``search_terms`` -- words to search the registries and the web by, besides
  the product's own name;
* ``adapters`` -- generic published servers for the kind, each a ``package`` on
  a ``registry`` (``mcp.packages.registry_launchers``), with ``env`` and
  ``args`` templates filled with ``{api_url}``, ``{base_url}`` and ``{host}``.

A package is pinned to the version its registry calls current at run time
(``mcp.packages.version_lookup``), so the entry that is written is the one
that was trialled. Nothing here names a kind in code; adding one is a table.

What counts as recognition (:func:`answers`) is strict, because a false one
builds a bridge to an API that is not there. smelt.example.net answered
``GET /rest/servicedeskapi/info`` with its HTML 404 page -- which repeats the
requested path -- and the path was the marker, so smelt "served a Jira Service
Management customer API"; the bridge generated for it replaced the working
``mcp-graphql`` entry. So: only a 2xx counts; the answer must have the form
the kind declares; a marker the request itself carries (its URL or body) is
never evidence, since an error page echoes it; and a *control* -- the same
request to a sibling path that cannot exist
(``mcp.discovery.api_kind_control_name``) -- must not be recognised too, or
the server answers everything that way.

* ``operations`` -- for a kind with no published server and no OpenAPI
  document, its documented read-only operations (``name``, ``path``,
  ``summary``, ``query``; ``public`` for one that needs no login, with the
  ``required_keys`` its answer holds -- the generated bridge must answer it
  before it counts, see :func:`public_calls`), and ``security`` (the HTTP scheme a credential is
  sent under). :func:`spec_for` turns them into the description the bridge
  stage generates a bridge from, in both modes. sd.example.com's account could
  not use the core Jira API mcp-atlassian speaks -- it is a Service Desk
  customer -- but its customer API answered; no server spoke that API, and
  nothing could be generated without a description of it.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import quote, unquote, urljoin, urlsplit

from ..core.config import fill
from ..core.context import RunContext
from ..core.logging_setup import get_logger
from . import apispec
from .address import parse as parse_address
from .types import Candidate, Stage, Transport, slugify

log = get_logger("mcp.api_kinds")

_SECTION = "mcp.discovery.api_kinds"

#: The last path segment of the control request (see :func:`detect`); equal
#: to ``mcp.discovery.api_kind_control_name``. Empty switches the control off.
DEFAULT_CONTROL_NAME = "initialize-claude-ai-control"


@dataclass
class Adapter:
    package: str
    registry: str
    env: dict[str, str] = field(default_factory=dict)
    #: How the adapter takes a credential, as env templates with
    #: ``{credential_ref}`` where the ``${VARIABLE}`` goes -- applied only when
    #: the address holds one (:func:`held_credential_variable`).
    credential_env: dict[str, str] = field(default_factory=dict)
    args: list[str] = field(default_factory=list)
    confidence: float = 0.0


@dataclass
class ApiKind:
    name: str
    label: str
    paths: list[str] = field(default_factory=list)
    method: str = ""
    body: str = ""
    content_type: str = ""
    success_markers: list[str] = field(default_factory=list)
    content_types: list[str] = field(default_factory=list)
    required_keys: list[str] = field(default_factory=list)
    doc_pages: list[str] = field(default_factory=list)
    link_patterns: list[str] = field(default_factory=list)
    finding: str = ""
    search_terms: list[str] = field(default_factory=list)
    adapters: list[Adapter] = field(default_factory=list)
    operations: list[dict[str, Any]] = field(default_factory=list)
    security: str = ""

    @property
    def probed(self) -> bool:
        """Found by asking at ``paths`` here, rather than read from another detector's finding."""
        return bool(self.paths or self.doc_pages) and bool(self.success_markers or self.required_keys)

    def evidence(self, url: str) -> list[str]:
        """The markers that can count for a request to ``url``: none the request itself carries.

        An error page that repeats the request -- smelt.example.net's 404 names the
        path it was asked for -- would otherwise carry any marker the request
        did. Compared without case, against the URL (raw and decoded) and the body.
        """
        sent = " ".join([url, unquote(url), self.body]).lower()
        return [m for m in self.success_markers if m and m.lower() not in sent]

    def behind_login(self, response: Any, url: str, send: Callable[..., Any], control: str) -> bool:
        """Is ``url`` this kind of API, there but asking for a login?

        sd.example.com came to answer ``/rest/servicedeskapi/info`` with 401, and
        its customer API went undetected: detection read only a 2xx, so nothing
        downstream -- the login plan, the bridge -- ever saw it. A refusal is
        evidence too when it is the API's own: a 401/403 in an API's form (not
        a web page), while the same request to a sibling that cannot exist is
        answered *differently* (404 there). An echo cannot pass that, and a site
        that refuses every path alike (a login wall) cannot either.
        (``mcp.discovery.api_kinds_behind_login``.)
        """
        status = int(getattr(response, "status", 0) or 0)
        if status not in (401, 403) or not self.probed:
            return False
        if _media_type(response) in ("text/html", "application/xhtml+xml", ""):
            return False
        sibling = control_url(url, control)
        if not sibling or sibling == url:
            return False
        other = send(self.method, sibling, self.content_type, self.body)
        other_status = int(getattr(other, "status", 0) or 0) if other is not None else 0
        return bool(other_status) and other_status != status

    def answers(self, response: Any, url: str) -> bool:
        """Is ``response`` (to the recognising request at ``url``) this kind of API?"""
        return not self.refusal(response, url)

    def refusal(self, response: Any, url: str) -> str:
        """Why ``response`` does not show this kind of API, in words; "" when it does."""
        if response is None:
            return "no answer"
        status = int(getattr(response, "status", 0) or 0)
        if not 200 <= status < 300:
            return f"answered {status or 'nothing'}, not a success"
        served = _media_type(response)
        if self.content_types and served not in {t.lower() for t in self.content_types}:
            return f"answered as {served or 'an unnamed type'}, not {' or '.join(self.content_types)}"
        text = str(getattr(response, "text", "") or "")
        if self.required_keys:
            missing = missing_keys(text, self.required_keys)
            if missing:
                return f"the answer lacks {', '.join(missing)}"
        if self.success_markers:
            usable = self.evidence(url)
            if not usable:
                return "every success marker is part of the request itself, so an echo would carry it"
            if not any(marker in text for marker in usable):
                return f"the answer carries none of {', '.join(usable)}"
        return ""


def kinds(settings: Any) -> list[ApiKind]:
    """Every configured API kind, in file order."""
    default_confidence = settings.float_("mcp.discovery.api_kind_adapter_confidence", 0.7)
    out: list[ApiKind] = []
    for name, raw in settings.dict_(_SECTION, {}).items():
        if not isinstance(raw, Mapping):
            continue
        adapters = []
        for entry in raw.get("adapters") or []:
            if not isinstance(entry, Mapping) or not str(entry.get("package") or "").strip():
                continue
            adapters.append(
                Adapter(
                    package=str(entry["package"]).strip(),
                    registry=str(entry.get("registry") or "").strip(),
                    env={str(k): str(v) for k, v in (entry.get("env") or {}).items()},
                    credential_env={str(k): str(v) for k, v in (entry.get("credential_env") or {}).items()},
                    args=[str(a) for a in entry.get("args") or []],
                    confidence=float(entry.get("confidence") or default_confidence),
                )
            )
        out.append(
            ApiKind(
                name=str(name),
                label=str(raw.get("label") or name),
                paths=_strings(raw.get("paths")),
                method=str(raw.get("method") or "POST").upper(),
                body=str(raw.get("body") or ""),
                content_type=str(raw.get("content_type") or ""),
                success_markers=_strings(raw.get("success_markers")),
                content_types=[t.lower() for t in _strings(raw.get("content_types"))],
                required_keys=_strings(raw.get("required_keys")),
                doc_pages=_strings(raw.get("doc_pages")),
                link_patterns=_strings(raw.get("link_patterns")),
                finding=str(raw.get("finding") or ""),
                search_terms=_strings(raw.get("search_terms")),
                adapters=adapters,
                operations=[
                    dict(op)
                    for op in raw.get("operations") or []
                    if isinstance(op, Mapping)
                    and str(op.get("name") or "").strip()
                    and str(op.get("path") or "").startswith(("/", "{api_path}"))
                ],
                security=str(raw.get("security") or "").strip().lower(),
            )
        )
    return out


def kind_of_manifest(manifest: Mapping[str, Any], settings: Any) -> str:
    """The API kind a generated bridge's operations manifest serves, by its operations' paths; "" when none matches.

    A bridge read back from an entry (reused, kept, judged again) has lost the
    ``api_kind`` its generation knew, and every rule that prefers the server
    built for the API this host serves passed it over: sd-example-com's
    customer-API bridge lost to mcp-atlassian. Its manifest still lists its
    operations; a kind whose configured operation paths all appear there is the
    kind it serves -- evidence, not a guess from a name.
    """
    paths = {str(op.get("path") or "") for op in (manifest.get("operations") or []) if isinstance(op, Mapping)}
    if not paths:
        return ""
    for kind in kinds(settings):
        wanted = {str(op.get("path") or "") for op in kind.operations if op.get("path")}
        if wanted and wanted <= paths:
            return kind.name
    return ""


def spec_for(kind: ApiKind, api_url: str) -> dict[str, Any] | None:
    """An OpenAPI description of ``kind``'s configured operations, served at ``api_url``'s origin.

    ``None`` when the kind lists none. Path parameters (``{issueIdOrKey}``) are
    required; ``query`` names optional query parameters. ``security`` becomes
    the document's own security scheme, which is how the generated bridge
    learns the credential method without waiting for a 401 (see
    :func:`.bridge_auth.method_from_spec`).
    """
    if not kind.operations:
        return None
    parts = urlsplit(api_url)
    if not parts.scheme or not parts.hostname:
        return None
    origin = f"{parts.scheme}://{parts.netloc}"
    paths: dict[str, Any] = {}
    for op in kind.operations:
        # {api_path} is where this target serves the kind: GraphQL is one URL,
        # /api/graphql on gitlab.example.net and /graphql/ on smelt.example.net.
        path = str(op["path"]).replace("{api_path}", parts.path or "/")
        method = str(op.get("method") or "get").lower()
        parameters = [
            {"name": name, "in": "path", "required": True, "schema": {"type": "string"}}
            for name in re.findall(r"\{([^}/]+)\}", path)
        ]
        parameters += [
            {"name": str(name), "in": "query", "required": False, "schema": {"type": "string"}}
            for name in op.get("query") or []
            if str(name).strip()
        ]
        # Fields of a JSON request body, taken from the tool's arguments: a
        # GraphQL read is a POST of {"query": …}.
        required_body = {str(n) for n in op.get("required_body") or []}
        parameters += [
            {
                "name": str(name),
                "in": "body",
                "required": str(name) in required_body,
                "schema": {"type": "string"},
                "description": str((op.get("descriptions") or {}).get(name, "")),
            }
            for name in op.get("body") or []
            if str(name).strip()
        ]
        operation: dict[str, Any] = {
            "operationId": str(op["name"]),
            "summary": str(op.get("summary") or f"{method.upper()} {path}"),
            "parameters": parameters,
            "responses": {"200": {"description": "OK"}},
        }
        # What the kind says about it, carried to the generated bridge: a POST
        # that only reads, a body it always sends, what it refuses to send.
        for key, extension in (
            ("read_only", "x-read-only"),
            ("body_fixed", "x-body-fixed"),
            ("refuse", "x-refuse"),
            ("json_params", "x-json-params"),
        ):
            if op.get(key):
                operation[extension] = op[key]
        # Two operations on one URL and method (GraphQL's are all POST /graphql)
        # are told apart by a fragment the generated bridge drops again.
        key = path if method not in paths.get(path, {}) else f"{path}#{op['name']}"
        paths.setdefault(key, {})[method] = operation
    spec: dict[str, Any] = {
        "openapi": "3.0.0",
        "info": {"title": f"{kind.label} (mcp.discovery.api_kinds.{kind.name}.operations)", "version": "1"},
        "servers": [{"url": origin}],
        "paths": paths,
    }
    if kind.security:
        spec["components"] = {"securitySchemes": {kind.name: {"type": "http", "scheme": kind.security}}}
        spec["security"] = [{kind.name: []}]
    return spec


def bridgeable(settings: Any, found: Mapping[str, Mapping[str, str]]) -> list[tuple[ApiKind, str]]:
    """``(kind, api_url)`` for each kind the target serves that lists operations to bridge."""
    by_name = {kind.name: kind for kind in kinds(settings)}
    return [
        (by_name[name], str(where.get("url") or ""))
        for name, where in found.items()
        if name in by_name and by_name[name].operations and where.get("url")
    ]


def operations_in_words(settings: Any, found: Mapping[str, Mapping[str, str]]) -> list[str]:
    """One line per bridgeable operation, for an agent's brief."""
    lines = []
    for kind, url in bridgeable(settings, found):
        for op in kind.operations:
            lines.append(
                f"  - {kind.label}: {str(op.get('method') or 'GET').upper()} "
                f"{str(op['path']).replace('{api_path}', urlsplit(url).path or '/')} -- "
                f"{op.get('summary') or op['name']} (served from {url})"
            )
    return lines


def public_calls(settings: Any, name: str) -> list[dict[str, Any]]:
    """``[{"tool", "required_keys"}]`` for each operation of kind ``name`` that needs no login.

    A bridge generated from a kind's operations is proved by answering one of
    these with real content -- with no credential at all. smelt.example.net's
    bridge would have answered ``get_service_desk_info`` with an HTML 404, and
    it was recorded as "waiting for a credential" instead.
    """
    kind = next((k for k in kinds(settings) if k.name == name), None)
    if kind is None:
        return []
    out = []
    for op in kind.operations:
        if not op.get("public"):
            continue
        keys = _strings(op.get("required_keys"))
        if not keys and str(op["path"]) in kind.paths:
            keys = list(kind.required_keys)
        out.append({"tool": str(op["name"]), "required_keys": keys})
    return out


def public_answer(reply: Any, keys: list[str] | tuple[str, ...]) -> tuple[bool, str]:
    """Did a public tool call answer with real content? ``(answered, detail)``.

    An error, an empty answer, or one without ``keys`` (when the operation
    names any) is not real content: a bridge pointed at a host that does not
    serve its API answers every call with that host's error page.
    """
    result = reply.get("result") if isinstance(reply, Mapping) else None
    if not isinstance(result, Mapping):
        error = reply.get("error") if isinstance(reply, Mapping) else None
        return False, str((error or {}).get("message") or "no answer") if isinstance(error, Mapping) else "no answer"
    text = " ".join(
        str(part.get("text") or "") for part in result.get("content") or [] if isinstance(part, Mapping)
    ).strip()
    excerpt = re.sub(r"\s+", " ", re.sub(r"<[^>]*>", " ", text)).strip()[:160]
    if result.get("isError"):
        return False, excerpt or "an error"
    if not text:
        return False, "an empty answer"
    if keys:
        missing = missing_keys(text, keys)
        if missing:
            return False, f"an answer without {', '.join(missing)}: {excerpt}"
    return True, excerpt


def missing_keys(text: str, keys: list[str] | tuple[str, ...]) -> list[str]:
    """Which dotted ``keys`` the JSON in ``text`` lacks -- all of them when it is not JSON."""
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return list(keys)
    missing = []
    for key in keys:
        value: Any = data
        for part in [p for p in key.split(".") if p]:
            value = value.get(part) if isinstance(value, Mapping) else None
        if value is None:
            missing.append(key)
    return missing


def control_url(url: str, name: str) -> str:
    """A sibling of ``url`` that cannot exist: its last segment replaced by ``name``."""
    return urljoin(url, name) if name else ""


def _media_type(response: Any) -> str:
    served = getattr(response, "content_type", "")
    if callable(served):
        served = served()
    if not served:
        headers = getattr(response, "headers", {}) or {}
        served = str(headers.get("content-type") or "")
    return str(served).split(";")[0].strip().lower()


def _strings(value: Any) -> list[str]:
    return [str(v) for v in value or [] if str(v).strip()]


# -- recognising one ----------------------------------------------------------


def detect(
    kind: ApiKind,
    origin: str,
    *,
    send: Callable[..., Any],
    fetch: Callable[[str], Any],
    same_host: bool = True,
    limit: int = 10,
    control: str = DEFAULT_CONTROL_NAME,
    behind_login: bool = True,
) -> str:
    """The URL at which ``origin`` serves ``kind``, or ``""``.

    ``send(method, url, content_type, body)`` makes the recognising request;
    ``fetch(url)`` reads a page for people. Only an answer :meth:`ApiKind.answers`
    accepts counts -- a 2xx of the declared form, with the declared keys and a
    marker the request did not carry -- and only when the same request to a
    sibling path named ``control`` is *not* accepted (see :func:`confirm`).
    """
    tried: set[str] = set()
    unreachable = False

    def ask(url: str) -> bool:
        nonlocal unreachable
        if url in tried or unreachable:
            return False
        tried.add(url)
        response = send(kind.method, url, kind.content_type, kind.body)
        status = getattr(response, "status", 0) if response is not None else 0
        # No answer at all is the host, not the path: the other paths would
        # each wait out the same timeout to say so.
        unreachable = response is not None and not status
        why = kind.refusal(response, url) if status else "no answer"
        if why and behind_login and kind.behind_login(response, url, send, control):
            log.info(
                "%s: a %s API, asking for a login (%s; a path that cannot exist is answered otherwise)",
                url,
                kind.label,
                status,
            )
            return True
        if why:
            log.debug("%s: not a %s API: %s", url, kind.label, why)
            return False
        return _control_passes(kind, url, send, control)

    base = origin.rstrip("/") + "/"
    for path in kind.paths:
        url = urljoin(base, path.lstrip("/"))
        if ask(url):
            return url
    if unreachable:
        return ""
    for page in kind.doc_pages:
        page_url = urljoin(base, page.lstrip("/"))
        response = fetch(page_url)
        if not getattr(response, "ok", False):
            continue
        found = apispec.links(
            response.text,
            getattr(response, "final_url", "") or page_url,
            kind.link_patterns,
            same_host_as=origin if same_host else "",
            limit=limit,
        )
        for url in found:
            if ask(url):
                return url
    return ""


def confirm(
    kind: ApiKind,
    url: str,
    *,
    send: Callable[..., Any],
    control: str = DEFAULT_CONTROL_NAME,
    behind_login: bool = True,
) -> str:
    """Why ``url`` is *not* shown to serve ``kind``, in words; "" when it is.

    For a URL somebody else recorded -- an agent's ``findings.api_kinds`` -- so
    it is held to the same test as one :func:`detect` found: the *same* test,
    a login wall in the API's own form included. sd.example.com's customer API was
    detected behind its login and then, re-confirmed from the agent's record,
    "does not serve it: answered 401" -- six times in one run -- so the bridge
    built for it ranked as evidence of nothing.
    """
    if not kind.probed:
        return ""
    response = send(kind.method, url, kind.content_type, kind.body)
    why = kind.refusal(response, url)
    if why and behind_login and response is not None and kind.behind_login(response, url, send, control):
        return ""
    if why:
        return why
    if not _control_passes(kind, url, send, control):
        return f"{control_url(url, control)}, which cannot exist, is answered the same way"
    return ""


def _control_passes(kind: ApiKind, url: str, send: Callable[..., Any], control: str) -> bool:
    """The same request to a path that cannot exist is *not* recognised."""
    sibling = control_url(url, control)
    if not sibling or sibling == url:
        return True
    response = send(kind.method, sibling, kind.content_type, kind.body)
    if response is not None and getattr(response, "status", 0) and kind.answers(response, sibling):
        log.info(
            "%s: answers as a %s API, but so does %s, which cannot exist -- not recognised", url, kind.label, sibling
        )
        return False
    return True


def described(settings: Any, findings: Mapping[str, Any], *, host: str = "") -> dict[str, dict[str, str]]:
    """Every kind the findings show, as ``{name: {"label", "url"}}``.

    Probed kinds are read from ``findings["api_kinds"]`` -- ``{name: {"url"}}``
    or ``{name: url}``, whichever an agent wrote; a kind configured with
    ``finding`` is read from that key, so an OpenAPI description found by the
    description search is reported through the same door. With ``host`` (and
    ``mcp.discovery.api_kinds_same_host_only``) a URL on any other host is
    ignored: a generic server pointed at somebody else's API is not a server
    for this target.
    """
    recorded = findings.get("api_kinds") if isinstance(findings.get("api_kinds"), Mapping) else {}
    only = host.lower() if host and settings.bool_("mcp.discovery.api_kinds_same_host_only", True) else ""
    out: dict[str, dict[str, str]] = {}
    for kind in kinds(settings):
        entry = recorded.get(kind.name) if isinstance(recorded, Mapping) else None
        url = str(entry.get("url") or "") if isinstance(entry, Mapping) else str(entry or "")
        if not url and kind.finding:
            url = str(findings.get(kind.finding) or "")
        parts = urlsplit(url.strip())
        if parts.scheme not in ("http", "https") or not parts.hostname:
            continue
        if only and parts.hostname.lower() != only:
            continue
        out[kind.name] = {"label": kind.label, "url": url.strip()}
    return out


# -- the servers that bridge one ---------------------------------------------


def adapter_candidates(
    ctx: RunContext,
    target: str,
    found: Mapping[str, Mapping[str, str]],
    *,
    base_url: str = "",
    origin: str = Stage.PACKAGE,
) -> list[Candidate]:
    """A launchable candidate for each adapter of each kind in ``found``."""
    settings = ctx.settings
    by_name = {kind.name: kind for kind in kinds(settings)}
    launchers = settings.dict_("mcp.packages.registry_launchers", {})
    address = parse_address(target)
    host = address.host or ""
    base_url = base_url or address.url or target
    out: list[Candidate] = []
    for name, where in found.items():
        kind = by_name.get(name)
        if kind is None:
            continue
        tokens = {"api_url": str(where.get("url") or ""), "base_url": base_url, "host": host}
        held = held_credential_variable(ctx, host)
        for adapter in kind.adapters:
            launcher = str(launchers.get(adapter.registry) or "")
            template = settings.list_(f"mcp.packages.launchers.{launcher}", []) if launcher else []
            if not template:
                log.debug(
                    "%s: no launcher for registry %r; adapter %s skipped", name, adapter.registry, adapter.package
                )
                continue
            spec = pinned(ctx, adapter.registry, adapter.package)
            candidate = Candidate(
                kind="stdio",
                name=f"{slugify(host or target)}-{slugify(adapter.package)}",
                command=[str(part).replace("{package}", spec) for part in template],
                args=[fill(a, tokens) for a in adapter.args],
                env={k: fill(v, tokens) for k, v in adapter.env.items()},
                transport=Transport.STDIO,
                origin=origin,
                source=f"generic {kind.label} server {adapter.package} for the {kind.label} API at {tokens['api_url']}",
                confidence=adapter.confidence,
                auth_method_evidence=f"{adapter.package}, a published server for any {kind.label} API",
            )
            candidate.add_note(
                f"{host or target} serves a {kind.label} API at {tokens['api_url']}; "
                f"{adapter.package} is a published server for any {kind.label} API"
            )
            if held and adapter.credential_env:
                # The address holds a credential (an entry here sends it, or the
                # shell exports it): the adapter is given it the way it takes one
                # (credential_env), so it is proved with the account's own login
                # rather than only as anybody. gitlab.example.net's mcp-graphql 2.x
                # replaced the token-carrying entry with one that had no slot.
                candidate.env.update(
                    {
                        k: fill(v, {**tokens, "credential_ref": "${" + held + "}"})
                        for k, v in adapter.credential_env.items()
                    }
                )
                candidate.add_note(f"given the credential {host} holds ({held}) the way {adapter.package} takes one")
            out.append(candidate)
    return out


#: A value that is one reference and nothing else -- ``${NAME}`` or ``${NAME:-}``:
#: whoever supplies NAME supplies the slot's whole value.
WHOLE_REFERENCE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(:-)?\}")
#: The variable a credential slot reads when nothing names one already:
#: ``{SLUG}`` is the entry's name in upper case.
DEFAULT_CREDENTIAL_VARIABLE = "{SLUG}_TOKEN"


def adapter_of(ctx: RunContext, candidate: Candidate) -> Adapter | None:
    """The adapter with ``credential_env`` whose launch this candidate is (registry launcher, package, any version)."""
    if candidate.kind != "stdio":
        return None
    launchers = ctx.settings.dict_("mcp.packages.registry_launchers", {})
    command = [str(t) for t in candidate.full_command()]
    for kind in kinds(ctx.settings):
        for adapter in kind.adapters:
            if not adapter.credential_env:
                continue
            launcher = str(launchers.get(adapter.registry) or "")
            template = (
                [str(t) for t in ctx.settings.list_(f"mcp.packages.launchers.{launcher}", [])] if launcher else []
            )
            if "{package}" not in template:
                continue
            at = template.index("{package}")
            names = [Path(t).name for t in command[:at]]
            if len(command) <= at or names != [Path(t).name for t in template[:at]]:
                continue
            package = command[at]
            if package == adapter.package or package.startswith((adapter.package + "@", adapter.package + "==")):
                return adapter
    return None


def _whole_reference(value: object) -> bool:
    return bool(WHOLE_REFERENCE.fullmatch(str(value or "").strip()))


def give_held_credential(ctx: RunContext, candidate: Candidate, host: str) -> bool:
    """An adapter's copy with no credential in the form it takes one, while the address holds one, is given it.

    The same launch (registry launcher, package name, any version) as an
    adapter with ``credential_env``, whose keys the entry leaves unset -- or
    sets to one bare reference, a variable that would have to hold the whole
    carrier: the templates are added with the held ``${VARIABLE}``, and the
    copy is proved with the account's login. gitlab.example.net's remembered
    mcp-graphql 2.x had been recorded as working on anonymous calls alone, and
    the token-carrying entry lost to it; the next run's agent proposed it with
    ``HEADERS: ${GITLAB_EXAMPLE_NET_HEADERS:-}`` -- a JSON object nobody exports --
    and the held GITLAB_EXAMPLE_NET_TOKEN was never given. Returns whether
    anything was added.
    """
    if not ctx.settings.bool_("mcp.discovery.reuse.give_held_credentials", True):
        return False
    adapter = adapter_of(ctx, candidate)
    if adapter is None:
        return False
    if any(k in candidate.env and not _whole_reference(candidate.env[k]) for k in adapter.credential_env):
        return False
    held = held_credential_variable(ctx, host)
    if not held:
        return False
    for key in adapter.credential_env:
        candidate.secret_env.pop(key, None)
        candidate.composed.pop(key, None)
        candidate.references.pop(key, None)
    candidate.env.update({k: fill(v, {"credential_ref": "${" + held + "}"}) for k, v in adapter.credential_env.items()})
    candidate.handshake = None
    candidate.add_note(f"given the credential {host} holds ({held}) the way {adapter.package} takes one")
    return True


def credential_in_taken_form(
    ctx: RunContext, candidate: Candidate, env: dict[str, str], name: str, host: str = ""
) -> list[str]:
    """Rewrite, in ``env``, each credential slot written as one bare reference into the form the adapter takes.

    ``HEADERS: ${GITLAB_EXAMPLE_NET_HEADERS:-}`` asks the operator for a JSON
    object, and empty it is no JSON at all: mcp-graphql exited at start-up,
    and the repair that followed withdrew the slot -- with it, the only place
    the token could go. The adapter's ``credential_env`` template is written
    instead, around a variable that holds the credential itself: the one the
    address already holds, else one named after the entry
    (``mcp.discovery.reuse.credential_variable``). Optional stays optional, and
    the template is a valid value with the variable empty. A variable that has
    a value where Claude Code runs is the operator's whole value and is kept.
    Returns the keys rewritten.
    """
    if not ctx.settings.bool_("mcp.discovery.reuse.credential_in_taken_form", True):
        return []
    adapter = adapter_of(ctx, candidate)
    if adapter is None:
        return []
    supplied = _claude_env(ctx)
    held = ""
    changed: list[str] = []
    for key, template in adapter.credential_env.items():
        if key not in env and _short_of_a_login(ctx, candidate):
            # No slot at all, while a task that needs the account failed: an
            # optional one in the adapter's form, so the login has somewhere
            # to go. gitlab-example-net was kept with no HEADERS at all, every
            # call anonymous, and the operator had nowhere to put the token.
            env[key] = "${" + _SLOT_MARKER + ":-}"
        match = WHOLE_REFERENCE.fullmatch(str(env.get(key) or "").strip())
        if not match or os.environ.get(match.group(1)) or supplied.get(match.group(1)):
            continue
        held = held or (held_credential_variable(ctx, host) if host else "")
        variable = held or fill(
            ctx.settings.str_("mcp.discovery.reuse.credential_variable", DEFAULT_CREDENTIAL_VARIABLE),
            {"SLUG": slugify(name or candidate.name).upper().replace("-", "_")},
        )
        reference = "${" + variable + (":-" if match.group(2) else "") + "}"
        env[key] = fill(template, {"credential_ref": reference})
        changed.append(key)
        candidate.add_note(
            f"{key} is written in the form {adapter.package} takes a credential, around {variable}: "
            f"export the credential itself as {variable}, not the whole {key} value"
        )
    return changed


#: The reference a missing slot is given before it is put in its form below.
_SLOT_MARKER = "ICA_CREDENTIAL_SLOT"


def _short_of_a_login(ctx: RunContext, candidate: Candidate) -> bool:
    """Did the server answer, while a usage task that is not a misfit failed -- the account was not logged in?

    (``mcp.discovery.reuse.slot_when_short_of_login``.)
    """
    from . import contract

    if not ctx.settings.bool_("mcp.discovery.reuse.slot_when_short_of_login", True):
        return False
    shake = candidate.handshake
    if shake is None or not shake.tools:
        return False
    rows = [r for r in shake.contract if contract.fits(r)]
    return any(r.get("passed") is False for r in rows)


def short_of_login_key(argv: Sequence[str], env: Mapping[str, Any]) -> str:
    """What a launch is, across copies: its tokens' last path parts, versions unpinned, and the hosts it names."""
    from .grounding import hosts_named

    tokens = []
    for token in argv:
        text = re.sub(r"\$\{[A-Za-z_][A-Za-z0-9_]*(?::-([^}]*))?\}", lambda m: m.group(1) or "", str(token))
        text = Path(text).name if "/" in text else text
        tokens.append(re.sub(r"(?<=.)(@v?\d[\w.\-]*|==[\w.\-]+)$", "", text))
    hosts = sorted({h.lower() for h in hosts_named([str(v) for v in (env or {}).values()] + [str(a) for a in argv])})
    return json.dumps([tokens, hosts])


def note_short_of_login(ctx: RunContext, candidate: Candidate) -> None:
    """Remember, for the final file check, a launch that answered while a task needing the account failed."""
    if candidate.kind == "stdio" and _short_of_a_login(ctx, candidate):
        ctx.state.setdefault("mcp_short_of_login", set()).add(
            short_of_login_key(candidate.full_command(), candidate.env)
        )


def slot_entry(
    ctx: RunContext, entry: Mapping[str, Any], name: str, host: str = ""
) -> tuple[dict[str, Any] | None, list[str]]:
    """``(entry, keys added)``: an adapter entry with no credential slot, whose launch was short of a login this run,
    given its slots -- optional, in the adapter's form. ``entry`` is None when nothing was added.

    Every entry the file holds passes the final file check, whichever branch
    kept it: gitlab-example-net's kept mcp-graphql went through one that wrote it
    "as it was", with no HEADERS. (``mcp.discovery.reuse.slot_when_short_of_login``.)
    """
    if not ctx.settings.bool_("mcp.discovery.reuse.slot_when_short_of_login", True) or entry.get("url"):
        return None, []
    command = [str(entry.get("command") or ""), *[str(a) for a in entry.get("args") or []]]
    env = dict(entry.get("env") or {})
    probe = Candidate(
        kind="stdio",
        name=name,
        command=[re.sub(r"\$\{[A-Za-z_][A-Za-z0-9_]*(?::-([^}]*))?\}", lambda m: m.group(1) or "", t) for t in command],
    )
    adapter = adapter_of(ctx, probe)
    if adapter is None or all(k in env for k in adapter.credential_env):
        return None, []
    if short_of_login_key(command, env) not in ctx.state.get("mcp_short_of_login", set()):
        return None, []
    variable = (held_credential_variable(ctx, host) if host else "") or fill(
        ctx.settings.str_("mcp.discovery.reuse.credential_variable", DEFAULT_CREDENTIAL_VARIABLE),
        {"SLUG": slugify(name).upper().replace("-", "_")},
    )
    added = [k for k in adapter.credential_env if k not in env]
    for key in added:
        env[key] = fill(adapter.credential_env[key], {"credential_ref": "${" + variable + ":-}"})
    fixed = dict(entry)
    fixed["env"] = dict(sorted(env.items()))
    return fixed, added


def _claude_env(ctx: RunContext) -> dict[str, str]:
    try:
        from . import claude_check

        return claude_check._claude_settings_env(ctx)
    except Exception:  # noqa: BLE001 - unreadable settings supply nothing
        return {}


def held_credential_variable(ctx: RunContext, host: str) -> str:
    """The variable holding the credential this machine has for ``host`` -- one an entry for it reads now, else
    a token the shell exports for it; "" for none. Never a value."""
    from . import inventory
    from .existing import ExistingRegistrations  # imports the writer side

    try:
        existing = ExistingRegistrations.for_run(ctx)
        for secret in existing.credentials_for(host):
            variable = existing.variable_for(secret)
            if variable:
                return variable
    except Exception:  # noqa: BLE001 - a lookup must never cost the candidate
        pass
    token = inventory.held(host, ctx.settings, inventory.known_environ(ctx)).get("token") or {}
    return str(next(iter(token.values()), "") or "")


def pinned(ctx: RunContext, registry: str, package: str) -> str:
    """``package`` pinned to its registry's current version, or ``package`` when that cannot be told."""
    settings = ctx.settings
    if not settings.bool_("mcp.packages.pin_versions", True):
        return package
    lookup = settings.dict_(f"mcp.packages.version_lookup.{registry}", {})
    url = str(lookup.get("url") or "")
    spec = str(lookup.get("spec") or "")
    if not url or not spec:
        return package
    cache = ctx.state.setdefault("mcp_pinned_versions", {})
    key = (registry, package)
    if key not in cache:
        cache[key] = _current_version(
            ctx, url.replace("{package}", quote(package, safe="@")), str(lookup.get("field") or "")
        )
    version = cache[key]
    return fill(spec, {"package": package, "version": version}) if version else package


def _current_version(ctx: RunContext, url: str, dotted: str) -> str:
    timeout = ctx.settings.float_(
        "mcp.packages.version_lookup_timeout_seconds", ctx.settings.float_("http.timeout_seconds", 15.0)
    )
    with ctx.http(timeout=timeout) as client:
        response = client.get(url, timeout=timeout)
    if not response.ok:
        log.debug("%s did not name a version: %s", url, response.problem)
        return ""
    value: Any = response.json()
    for part in [p for p in dotted.split(".") if p]:
        value = value.get(part) if isinstance(value, Mapping) else None
    version = str(value or "").strip()
    # A version string is short and plain; anything else is not pinned to.
    return version if re.fullmatch(r"[0-9A-Za-z][0-9A-Za-z.+_-]{0,63}", version) else ""


def search_terms(settings: Any, found: Mapping[str, Any]) -> list[str]:
    """Words to search by for the kinds in ``found``."""
    terms: list[str] = []
    for kind in kinds(settings):
        if kind.name in found:
            terms += [t for t in kind.search_terms if t not in terms]
    return terms


__all__ = [
    "Adapter",
    "ApiKind",
    "DEFAULT_CONTROL_NAME",
    "adapter_candidates",
    "bridgeable",
    "confirm",
    "control_url",
    "missing_keys",
    "public_answer",
    "public_calls",
    "operations_in_words",
    "spec_for",
    "described",
    "detect",
    "kinds",
    "pinned",
    "search_terms",
]
