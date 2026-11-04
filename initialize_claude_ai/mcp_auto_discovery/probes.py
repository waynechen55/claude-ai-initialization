"""The MCP handshake, over all three transports.

This module is phase 4's arbiter of truth.  A URL that returns 200, a package
whose README says "MCP server", an agent that is confident -- none of that counts.
A candidate is only ever accepted after the exchange below completes:

1. ``initialize``               client offers a protocol version and capabilities
2. ``notifications/initialized`` client confirms it is ready
3. ``tools/list``               (and ``resources``/``prompts`` when advertised)
4. ``ping``                     only if step 3 asked nothing, so that every
   accepted server has answered at least one request *after* the handshake --
   see :meth:`McpProber._prove_round_trip`

Transport details that matter and are easy to get wrong:

* Streamable HTTP requires ``Accept: application/json, text/event-stream`` on the
  POST -- a server is entitled to reject the request without both.
* The server may answer a POST with either a JSON body or an SSE stream; both
  carry the same JSON-RPC envelope.
* ``Mcp-Session-Id`` comes back on the initialize response and must be echoed on
  every subsequent request.
* From protocol 2025-06-18 onward the client must also send
  ``MCP-Protocol-Version`` on every request after initialize.
* The deprecated HTTP+SSE transport works the other way round: GET the SSE URL,
  wait for an ``endpoint`` event, then POST messages to the URL it names.
* Protocol version negotiation is a real negotiation: a server that does not know
  the offered version answers with one it does know, or errors -- so we retry
  down the list rather than assuming.
* A certificate this machine will not verify is a fact about the network, not an
  answer about MCP. Step B of the tier-1 checklist requires the probe to be
  repeated once with verification off; the endpoint that only answers that way is
  reported as ``insecure_tls`` rather than quietly treated as equivalent.
* A probe must never be what leaks a credential: the configured bearer token is
  stripped before any cleartext request to a non-local host.
"""

from __future__ import annotations

import contextlib
import json
import os
import queue
import re
import signal
import subprocess
import threading
import time
import uuid
from collections import deque
from contextlib import suppress
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import urljoin, urlsplit

from .. import __version__
from ..core import redact
from ..core.config import Settings
from ..core.context import RunContext
from ..core.logging_setup import get_logger
from . import login_check
from .address import is_local
from .types import Handshake, Transport

#: How many stderr lines a trialled server may keep, counting from the *end*.
#: Bounded because this reads whatever a third party prints, and generous
#: because the useful line is the last one: a deep startup chain in someone
#: else's framework routinely runs past forty lines before it raises.
_STDERR_LINES = 200

#: ``SomeError: what went wrong`` -- the line a Python traceback ends with, and
#: the only one in it that names a cause. Deliberately loose about the exception
#: name: this runs against whatever a third party wrote, in whatever language,
#: and "Word(s)Error: text" is the shape almost all of them share.
_EXCEPTION_LINE = re.compile(r"^[A-Za-z_][\w.]*(?:Error|Exception|Exit|Failure)\b\s*:\s*\S")


def _property_keys(schema: Any, depth: int = 0) -> list[str]:
    """Every property key a JSON schema declares, nested ones included."""
    if not isinstance(schema, Mapping) or depth > 32:
        return []
    keys: list[str] = []
    properties = schema.get("properties")
    if isinstance(properties, Mapping):
        for key, sub in properties.items():
            keys.append(str(key))
            keys.extend(_property_keys(sub, depth + 1))
    for container in ("items", "additionalProperties"):
        keys.extend(_property_keys(schema.get(container), depth + 1))
    for combined in ("anyOf", "oneOf", "allOf"):
        for sub in schema.get(combined) or []:
            keys.extend(_property_keys(sub, depth + 1))
    for container in ("$defs", "definitions"):
        defs = schema.get(container)
        if isinstance(defs, Mapping):
            for sub in defs.values():
                keys.extend(_property_keys(sub, depth + 1))
    return keys


def name_schema_problems(problems: Sequence[Mapping[str, str]]) -> str:
    """The offending tool names and property keys, listed: ``tool 'x' declares 'y'``."""
    named = [
        f"tool {p.get('tool')!r} declares {p.get('key')!r}" if p.get("key") else f"tool name {p.get('tool')!r}"
        for p in problems
    ]
    return ", ".join(named[:5]) + (f" (+{len(named) - 5} more)" if len(named) > 5 else "")


def describe_schema_problems(problems: Sequence[Mapping[str, str]]) -> str:
    """One sentence naming each tool definition the client will reject."""
    shown = name_schema_problems(problems)
    return (
        f"its tool definitions break Claude Code's naming rules: {shown}. Once such a tool is "
        "loaded, the API refuses every request of the session (mcp.verification.client_name_pattern). "
        "The fix is in the server: a newer or patched build that declares plain names"
    )


def _last_exception_line(lines: Sequence[str]) -> str:
    """The most informative line of a traceback, or "" when it is not one."""
    for line in reversed(lines):
        if _EXCEPTION_LINE.match(line):
            return line
    return ""


#: A line that carries nothing but layout -- the ``^^^^`` and ``~~~`` markers
#: and the lone ``)`` of a wrapped call that a modern Python traceback prints
#: under the code. confluence.example.com's failure was reported as
#: ``FileNotFoundError: [Errno 2] No such file or directory | ) | ^``: the two
#: lines of context it kept were exactly these, and the frame that named the
#: call -- the one line that says which file was missing -- was dropped.
_LAYOUT_ONLY = re.compile(r"^[\s^~()\[\]{},.|]*$")

#: Where a traceback says the failure happened: Python's ``File "x", line N``
#: and the ``at f (file:line)`` of Node and the JVM.
_FRAME_LINE = re.compile(r'^(?:File "[^"]+", line \d+|at \S)')


#: Fallback for ``[mcp.verification.stderr_causes] classes``, equal to the
#: packaged table: what a server's own stderr line says a failed call was about.
STDERR_CAUSES = {
    "trust": [
        r"(?i)(SSL_CERT_FILE|SSL_CERT_DIR|REQUESTS_CA_BUNDLE|CURL_CA_BUNDLE|NODE_EXTRA_CA_CERTS)\W.*(does not exist|no such file|not found|invalid)",
        r"(?i)certificate verify failed",
        r"CERTIFICATE_VERIFY_FAILED",
        r"UNABLE_TO_(GET_ISSUER|VERIFY_LEAF)",
        r"(?i)self[- ]signed certificate",
        r"(?i)load_verify_locations",
    ],
    "transport": [
        r"(?i)connection refused",
        r"(?i)name or service not known",
        r"(?i)temporary failure in name resolution",
        r"ECONNREFUSED",
        r"ENOTFOUND",
        r"EAI_AGAIN",
        r"(?i)\bConnectError\b",
        r"(?i)network is unreachable",
    ],
}


def stderr_cause(lines: Sequence[str], settings: Any) -> dict[str, str]:
    """``{class, line}`` for the last stderr line naming a cause class; {} when none does."""
    classes = (
        settings.dict_("mcp.verification.stderr_causes.classes", STDERR_CAUSES)
        if settings is not None
        else STDERR_CAUSES
    )
    for line in reversed([str(x) for x in lines if str(x).strip()]):
        for name, patterns in (classes or STDERR_CAUSES).items():
            for pattern in patterns or []:
                try:
                    if re.search(str(pattern), line):
                        return {"class": str(name), "line": redact.redact(" ".join(line.split()))[:200]}
                except re.error:
                    continue
    return {}


def _failure_lines(lines: Sequence[str]) -> list[str]:
    """The exception first, then the frame that raised it, then what is left.

    The frame is the innermost one (the last before the exception) together
    with the code line Python prints beneath it; layout-only lines are dropped
    before anything is counted, so they can never crowd out a line that says
    something.
    """
    kept = [line for line in lines if line and not _LAYOUT_ONLY.match(line)]
    summary = _last_exception_line(kept)
    if not summary:
        return kept[-3:]
    cut = len(kept) - 1 - kept[::-1].index(summary)
    frame: list[str] = []
    for index in range(cut - 1, -1, -1):
        if _FRAME_LINE.match(kept[index]):
            frame = kept[index : min(index + 2, cut)]
            break
    rest = [line for line in kept[-3:] if line != summary and line not in frame]
    return [summary, *frame, *rest]


_PACE_LOCK = threading.Lock()
#: A setting probe's row when the tool did not reject its settings -- not a login verdict.
SETTINGS_ACCEPTED = "settings_accepted"

#: Per thread: inside fresh_answers(), nothing is answered from the run's memo.
_FRESH = threading.local()


#: Fallbacks for ``mcp.verification.launcher_retries`` / ``launcher_transient_patterns``:
#: a launcher's own momentary failure, never the server's -- a package cache
#: being filled by another launch at the same moment.
LAUNCHER_RETRIES = 1
LAUNCHER_TRANSIENT_PATTERNS = (
    r"npm error enoent",
    r"_npx[/\\][^\s]*package\.json",
    r"npm error code (?:EEXIST|ENOTEMPTY|EINTEGRITY|ECOMPROMISED)",
    r"Failed to (?:acquire|create) (?:lock|temporary directory)",
    r"(?:resource|file) (?:is )?(?:temporarily unavailable|busy)",
)


#: Fallbacks for ``mcp.verification.launcher_warming_patterns`` /
#: ``launcher_warm_timeout_seconds``: a launcher still fetching what the server
#: needs (a cold uv or npm cache) -- slow, not broken. confluence-example-com's
#: mcp-atlassian was trialled fine, then "did not start" as written while uvx
#: was "Downloading pydantic-core ... lxml ... rapidfuzz": discarded, and a
#: server taking 87 s to start was written instead.
LAUNCHER_WARMING_PATTERNS = (
    r"(?i)\bdownloading\b",
    r"(?i)\binstalled \d+ packages?\b",
    r"(?i)\bresolved \d+ packages?\b",
    r"(?i)\bprepared \d+ packages?\b",
    r"(?i)\bbuilding wheel",
    r"(?i)\badded \d+ packages?\b",
    r"(?i)\bnpm (warn|http) ",
)
LAUNCHER_WARM_TIMEOUT_SECONDS = 300.0


def start_key(argv: Any, env: Mapping[str, Any] | None = None) -> str:
    """The key a launch's slowest start is kept under: the command's name, its arguments, its runtime tuning.

    Two variants of one command that differ in a runtime-tuning setting start
    differently: smelt-example-net's old entry, with a 30 s network wait in
    NODE_OPTIONS, set the "slowest start" of the rewritten entry without it,
    and the run raised MCP_TIMEOUT for a server that starts in 3 s.
    """
    from .verifier import RUNTIME_OPTION_VARIABLES

    tokens = [str(t) for t in argv or []]
    if not tokens:
        return ""
    tuning = sorted(
        f"{k}={v}" for k, v in (env or {}).items() if any(re.fullmatch(p, str(k)) for p in RUNTIME_OPTION_VARIABLES)
    )
    return " ".join([os.path.basename(tokens[0]), *tokens[1:], *tuning])


@contextlib.contextmanager
def fresh_answers():
    """A block in which every ``tools/call`` is asked anew -- the ranking's repeated handshakes measure the server."""
    _FRESH.depth = getattr(_FRESH, "depth", 0) + 1
    try:
        yield
    finally:
        _FRESH.depth -= 1


log = get_logger("mcp.probe")

#: JSON-RPC error code a server returns for a version it cannot speak.
_INVALID_PARAMS = -32602

#: Substrings that identify a transport failure as a *certificate* failure
#: rather than an absent server. Defaults only -- ``mcp.discovery.tls_error_markers``
#: overrides them, because the wording comes from whichever TLS stack is
#: installed and not from anything this program controls.
TLS_ERROR_MARKERS = (
    "certificate",
    "ssl",
    "tls",
    "self-signed",
    "self signed",
    "hostname mismatch",
    "unable to get local issuer",
)

#: Header names never sent over cleartext to a public host. Defaults only;
#: ``mcp.discovery.cleartext_stripped_headers`` overrides them.
CLEARTEXT_STRIPPED_HEADERS = ("authorization", "proxy-authorization", "cookie", "x-api-key")

#: Recorded on a handshake whose target offered a WebSocket upgrade *and* gave a
#: sign that the socket carries MCP. Read by the endpoint stage, which counts it
#: as a native endpoint that needs a bridge.
WEBSOCKET_HINT = "the server offered a WebSocket upgrade"

#: Recorded instead of :data:`WEBSOCKET_HINT` when a socket answered with nothing
#: to say it speaks MCP. A service's own live-update socket is not an MCP server,
#: and counting it as one closes the lower tiers on the strength of it.
WEBSOCKET_UNPROVEN_HINT = "the server offered a WebSocket upgrade with no sign it carries MCP"


def _schema_strings(node: Any, depth: int, keys: Sequence[str], items: int) -> list[str]:
    """Every string a JSON Schema carries that a server may have put an address in.

    ``keys`` are the schema keys worth reading and come from
    ``mcp.verification.advertised.schema_keys``; a generic server is configured
    through exactly those -- a ``base_url`` whose ``default`` is the target is
    the server saying what it serves -- and they are otherwise discarded with
    the rest of the schema.

    Bounded by ``depth`` and ``items`` because a schema can be arbitrarily
    nested and this runs against whatever a third party published.
    """
    if depth <= 0:
        return []
    out: list[str] = []
    if isinstance(node, Mapping):
        for key, value in node.items():
            if isinstance(value, str) and value.strip():
                if key in keys:
                    out.append(value.strip())
            elif isinstance(value, (Mapping, list)):
                out.extend(_schema_strings(value, depth - 1, keys, items))
    elif isinstance(node, list):
        for item in node[:items]:
            if isinstance(item, str) and item.strip():
                out.append(item.strip())
            else:
                out.extend(_schema_strings(item, depth - 1, keys, items))
    return out


class McpProber:
    """Performs MCP handshakes. Stateless between calls; safe to reuse."""

    def __init__(self, ctx: RunContext, config: Settings | None = None) -> None:
        self.ctx = ctx
        self.config = config or ctx.settings.section("mcp.protocol")
        self.verification = ctx.settings.section("mcp.verification")
        self.discovery = ctx.settings.section("mcp.discovery")
        self.versions = self.config.str_list(
            "versions",
            [
                "2026-07-28",
                "2025-11-25",
                "2025-06-18",
                "2025-03-26",
                "2024-11-05",
            ],
        )
        self.client_name = self.config.str_("client_name", "initialize-claude-ai-discovery")
        self.client_version = self.config.str_("client_version", "") or __version__
        self.accept = self.config.str_("accept_header", "application/json, text/event-stream")
        self.session_header = self.config.str_("session_header", "Mcp-Session-Id")
        self.version_header = self.config.str_("protocol_version_header", "MCP-Protocol-Version")
        self.timeout = self.verification.float_("handshake_timeout_seconds", 20)
        #: One control answer per origin, for the run. See :meth:`control_answer`.
        self._controls: dict[str, tuple[int, str] | None] = {}
        #: Origins whose control probe is in flight on another thread. Stage
        #: probes run in a pool, and addresses run side by side under
        #: --concurrency, so the second asker of an origin waits for the first
        #: one's answer instead of being told "no control" while it is pending
        #: -- which would decide a 401 differently depending on scheduling.
        self._control_pending: dict[str, threading.Event] = {}
        self._control_guard = threading.Lock()

    # -- request payloads ------------------------------------------------------

    def initialize_params(self, version: str) -> dict[str, Any]:
        return {
            "protocolVersion": version,
            # Declared honestly: this client consumes, it does not host.
            "capabilities": {},
            "clientInfo": {"name": self.client_name, "version": self.client_version},
        }

    @staticmethod
    def rpc(method: str, params: Any = None, *, request_id: int | None = 1) -> dict[str, Any]:
        payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if request_id is not None:
            payload["id"] = request_id
        if params is not None:
            payload["params"] = params
        return payload

    # -- Streamable HTTP -------------------------------------------------------

    def probe_http(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        timeout: float | None = None,
        verify_tls: bool = True,
        login: bool = False,
        login_tokens: Mapping[str, str] | None = None,
        contract: Mapping[str, Any] | None = None,
    ) -> Handshake:
        """Try the current HTTP transport at ``url``, bypassing TLS once if need be.

        ``login``: check the credential in ``headers`` with one read-only call
        (see :meth:`_check_login`); only for a caller whose credential is real.

        Step B of the tier-1 checklist: a probe that fails on the certificate has
        established nothing about MCP, so it is repeated with verification off.
        If *that* answers, the endpoint is real and is reported -- flagged
        ``insecure_tls``, penalised in the ranking, and accompanied by the
        remediation, because an unverifiable peer is not the same as a verified
        one and the operator has to be the one who decides to live with it.
        """
        result = self._attempt_http(
            url,
            headers=headers,
            timeout=timeout,
            verify_tls=verify_tls,
            login=login,
            login_tokens=login_tokens,
            contract=contract,
        )
        result = self._settle_inconclusive(
            result,
            lambda: self._attempt_http(
                url,
                headers=headers,
                timeout=timeout,
                verify_tls=verify_tls,
                login=login,
                login_tokens=login_tokens,
                contract=contract,
            ),
        )
        if not (verify_tls and self._is_tls_failure(result)):
            return result
        if not self.discovery.bool_("tls_insecure_retry", True):
            result.add_hint("TLS verification failed; the insecure retry is disabled")
            return result

        retry = self._attempt_http(
            url,
            headers=headers,
            timeout=timeout,
            verify_tls=False,
            login=login,
            login_tokens=login_tokens,
            contract=contract,
        )
        retry.insecure_tls = True
        if retry.ok or retry.requires_auth:
            retry.add_hint("answered only with TLS certificate verification disabled")
            return retry
        result.add_hint("retried with TLS verification disabled; still no MCP answer")
        return result

    def _settle_inconclusive(self, result: Handshake, again: Any) -> Handshake:
        """Ask once more when a 2xx reply was neither a refusal nor JSON-RPC.

        That reply proves nothing about the URL -- a gateway warming up, a
        cache in front of the endpoint, a proxy that answered for it -- and it
        used to be recorded as "does not speak MCP", which discarded the one
        endpoint openqa.opensuse.org has. ``inconclusive_retries`` asks again,
        ``inconclusive_retry_delay_seconds`` apart; a second answer that is
        anything else replaces the first.
        """
        retries = max(0, self.discovery.int_("inconclusive_retries", 1))
        delay = max(0.0, self.discovery.float_("inconclusive_retry_delay_seconds", 1))
        for _ in range(retries):
            if not result.inconclusive:
                break
            if delay:
                time.sleep(delay)
            retry = again()
            if not retry.inconclusive:
                return retry
            result.add_hint("answered the same way when asked again")
        return result

    def reply_excerpt(self, response: Any) -> str:
        """``HTTP 200 text/html: <first words>``, redacted -- what a rejection actually saw."""
        limit = max(0, self.discovery.int_("reply_excerpt_chars", 160))
        status = getattr(response, "status", 0)
        content_type = (getattr(response, "content_type", "") or "no content type").strip()
        body = " ".join(str(getattr(response, "text", "") or "").split())
        text = f"HTTP {status} {content_type}"
        if body and limit:
            clipped = body[:limit] + ("..." if len(body) > limit else "")
            text += f": {redact.redact(clipped)}"
        elif not body:
            text += ", empty body"
        return text

    def _scanned(self, response: Any) -> str:
        """The part of a reply's body read for markers (``mcp.discovery.response_scan_chars``).

        Bounded so a large error page costs nothing to search; 0 reads it all.
        One setting for every marker scan, so a marker found by one is found by
        all of them.
        """
        body = str(getattr(response, "text", "") or "")
        limit = self.discovery.int_("response_scan_chars", 4000)
        return body[:limit] if limit > 0 else body

    def credential_refusal(self, response: Any) -> str:
        """The ``auth.credential_refusal_markers`` entry a non-401 reply's body carries, or ``""``.

        Not every endpoint refuses with 401 or 403. Some answer 200 or 400 with
        ``{"error":"no api key"}`` or "invalid personal access token", and that
        is a credential refusal in everything but its status. Only statuses in
        ``auth.credential_refusal_statuses`` are read, and the reply still has
        to pass :meth:`_note_auth_evidence` -- the control probe and the
        login-page markers included -- before it counts as an MCP endpoint.
        """
        statuses = {
            int(s)
            for s in self.discovery.list_(
                "auth.credential_refusal_statuses",
                [
                    200,
                    201,
                    202,
                    400,
                    406,
                    422,
                ],
            )
            if str(s).strip().lstrip("-").isdigit()
        }
        if getattr(response, "status", 0) not in statuses:
            return ""
        body = self._scanned(response).lower()
        if not body:
            return ""
        for marker in self.discovery.str_list("auth.credential_refusal_markers", []):
            if marker and marker.lower() in body:
                return marker
        return ""

    def _attempt_http(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        timeout: float | None = None,
        verify_tls: bool = True,
        login: bool = False,
        login_tokens: Mapping[str, str] | None = None,
        contract: Mapping[str, Any] | None = None,
    ) -> Handshake:
        """One pass of the HTTP handshake, at one TLS setting."""
        result = Handshake(transport=Transport.STREAMABLE_HTTP, url=url)
        effective = timeout or self.timeout
        started = time.monotonic()
        waited: list[float] = []
        base_headers = {
            "content-type": "application/json",
            "accept": self.accept,
            **self.safe_headers(url, headers),
        }

        with self.ctx.http(timeout=effective, verify_tls=verify_tls) as client:
            for version in self.versions:
                response = client.post(
                    url,
                    headers=base_headers,
                    json_body=self.rpc("initialize", self.initialize_params(version)),
                    timeout=effective,
                )
                result.status = response.status
                result.content_type = (
                    str(
                        getattr(response, "content_type", "")
                        or (getattr(response, "headers", None) or {}).get("content-type", "")
                    )
                    .split(";")[0]
                    .strip()
                    .lower()
                )
                result.elapsed = time.monotonic() - started
                # Step C: read what came back before deciding it was a no. A
                # 400 carrying a JSON-RPC envelope is an MCP endpoint objecting
                # to the request; a 404 is not.
                self._note_hints(result, response)

                if response.status in (401, 403):
                    result.requires_auth = True
                    result.error = f"authentication required (HTTP {response.status})"
                    result.auth_hint = response.headers.get("www-authenticate", "")
                    self._note_auth_evidence(result, response)
                    return result
                if response.status == 0:
                    result.error = response.error or "connection failed"
                    return result
                if response.status >= 500:
                    result.error = f"server error HTTP {response.status}"
                    return result
                refusal = self.credential_refusal(response)
                if refusal and not self._initialize_result(response):
                    result.requires_auth = True
                    result.error = f"authentication required (HTTP {response.status}: the reply says {refusal!r})"
                    result.auth_hint = response.headers.get("www-authenticate", "")
                    self._note_auth_evidence(result, response)
                    return result
                if not response.ok:
                    # 404/405/406 are the normal "this is not an MCP endpoint"
                    # answers; no point cycling protocol versions against them.
                    result.error = f"HTTP {response.status}"
                    return result

                payload = response.json()
                if not isinstance(payload, dict) or not (
                    "jsonrpc" in payload or "result" in payload or "error" in payload
                ):
                    # Neither a refusal nor an answer: say what it was, and do
                    # not let it stand as proof of anything.
                    result.inconclusive = True
                    result.error = f"response was not a JSON-RPC message ({self.reply_excerpt(response)})"
                    return result

                if "error" in payload:
                    code = (payload.get("error") or {}).get("code")
                    message = str((payload.get("error") or {}).get("message", ""))
                    if code == _INVALID_PARAMS and version != self.versions[-1]:
                        log.debug("%s rejected protocol %s: %s", url, version, message)
                        continue  # negotiate down
                    result.error = f"server error: {message or code}"
                    return result

                inner = payload.get("result")
                if not isinstance(inner, dict) or "protocolVersion" not in inner:
                    result.error = "reply was JSON-RPC but not an initialize result"
                    return result

                self._absorb_initialize(result, inner)
                result.session_id = response.headers.get(self.session_header.lower(), "")
                result.ok = True
                break
            else:
                result.error = "no offered protocol version was accepted"
                return result

        # Handshake complete: confirm, then look at what the server exposes.
        follow_up = dict(base_headers)
        if result.session_id:
            follow_up[self.session_header.lower()] = result.session_id
        follow_up[self.version_header.lower()] = result.protocol_version

        with self.ctx.http(timeout=effective, verify_tls=verify_tls) as client:
            client.post(
                url,
                headers=follow_up,
                json_body=self.rpc("notifications/initialized", request_id=None),
                timeout=effective,
            )

            def call(method: str, rid: int, params: Any = None) -> Any:
                return client.post(
                    url,
                    headers=follow_up,
                    json_body=self.rpc(method, params or {}, request_id=rid),
                    timeout=effective,
                ).json()

            call = self.patient(call, _signature([url], headers or {}, {}), _hosts_of([url]), waited)
            self._list_capabilities(result, call)
            self._prove_round_trip(result, call)
            result.credential_sent = login_check.credential_in(headers)
            if login:
                self._check_login(result, call, login_tokens)
            self._run_contract(result, call, contract)

        # Time spent waiting on the pacing or a "try again later" is not the
        # server's: the ranking's latency must read as it did without them.
        result.elapsed = time.monotonic() - started - sum(waited)
        return result

    # -- deprecated HTTP+SSE ---------------------------------------------------

    def probe_sse(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        timeout: float | None = None,
        verify_tls: bool = True,
        login: bool = False,
    ) -> Handshake:
        """Try the pre-2025-03-26 HTTP+SSE transport at ``url`` (``login`` as for :meth:`probe_http`)."""
        result = self._attempt_sse(url, headers=headers, timeout=timeout, verify_tls=verify_tls, login=login)
        if not (verify_tls and self._is_tls_failure(result)):
            return result
        if not self.discovery.bool_("tls_insecure_retry", True):
            return result
        retry = self._attempt_sse(url, headers=headers, timeout=timeout, verify_tls=False, login=login)
        retry.insecure_tls = True
        if retry.ok or retry.requires_auth:
            retry.add_hint("answered only with TLS certificate verification disabled")
            return retry
        result.add_hint("retried with TLS verification disabled; still no MCP answer")
        return result

    def _attempt_sse(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        timeout: float | None = None,
        verify_tls: bool = True,
        login: bool = False,
    ) -> Handshake:
        result = Handshake(transport=Transport.SSE, url=url)
        effective = timeout or self.timeout
        started = time.monotonic()

        with self.ctx.http(timeout=effective, verify_tls=verify_tls) as client:
            meta, events = client.stream_sse(
                url,
                headers={
                    "accept": self.config.str_("sse_accept_header", "text/event-stream"),
                    **self.safe_headers(url, headers),
                },
                max_events=2,
                timeout=effective,
            )
            result.status = meta.status
            self._note_hints(result, meta)
            if meta.status in (401, 403):
                result.requires_auth = True
                result.error = f"authentication required (HTTP {meta.status})"
                result.auth_hint = (meta.headers or {}).get("www-authenticate", "")
                self._note_auth_evidence(result, meta)
                return result
            if not meta.ok:
                result.error = meta.error or f"HTTP {meta.status}"
                return result
            if "text/event-stream" not in (meta.content_type or ""):
                result.error = f"not an SSE stream (content-type {meta.content_type or 'unknown'})"
                return result

            endpoint = _endpoint_from_events(events)
            if not endpoint:
                result.error = "SSE stream carried no 'endpoint' event"
                return result

        # The endpoint event may be absolute or relative to the SSE URL.
        post_url = urljoin(url, endpoint)
        result.url = post_url
        # From here the message exchange is ordinary JSON-RPC over POST; reuse
        # the HTTP path but keep the transport label honest.
        inner = self.probe_http(post_url, headers=headers, timeout=effective, verify_tls=verify_tls, login=login)
        inner.transport = Transport.SSE
        inner.url = url
        inner.elapsed = time.monotonic() - started
        inner.error = inner.error or ""
        inner.add_hint(*result.protocol_hints)
        return inner

    # -- WebSocket detection ---------------------------------------------------

    def probe_websocket(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        timeout: float | None = None,
        verify_tls: bool = True,
    ) -> Handshake:
        """Ask whether ``url`` would upgrade to a WebSocket that carries MCP.

        Deliberately a *detection*, never an acceptance: Claude Code has no
        WebSocket MCP transport, so a server found here cannot simply be
        registered. But "this host upgrades /ws to MCP" is the opposite
        conclusion from "this host does not speak MCP" -- it is a native
        endpoint that needs a bridge -- and step A of the checklist requires
        ``/ws`` to be checked, so the answer has to be obtained, not assumed.

        An upgrade on its own is not that answer. Plenty of services keep a
        WebSocket for their own pages: openqa.opensuse.org's ``/api/v1/ws/``
        answers ``404`` with an ``Upgrade: websocket`` header, and was recorded
        as a native MCP endpoint that closed the package tier and asked for a
        relay bridge to a socket that has never spoken JSON-RPC. So the upgrade
        counts only with a sign of MCP (``mcp.discovery.websocket_*``): the
        server accepting one of the MCP subprotocols offered, or the route
        itself being named for MCP. A refusal status is never an upgrade,
        whatever headers ride along with it.
        """
        result = Handshake(transport=Transport.WEBSOCKET, url=url)
        effective = timeout or self.timeout
        started = time.monotonic()
        offered = [p.strip() for p in self.discovery.str_list("websocket_mcp_subprotocols", ["mcp"]) if p.strip()]
        probe_headers = {
            "connection": "Upgrade",
            "upgrade": "websocket",
            "sec-websocket-version": "13",
            # A fixed, meaningless key: this is a protocol probe, and the server
            # is being asked whether it would upgrade, not actually upgraded.
            "sec-websocket-key": "AAAAAAAAAAAAAAAAAAAAAA==",
            **({"sec-websocket-protocol": ", ".join(offered)} if offered else {}),
            **self.safe_headers(url, headers),
        }
        with self.ctx.http(timeout=effective, verify_tls=verify_tls) as client:
            response = client.get(url, headers=probe_headers, timeout=effective)

        result.status = response.status
        result.elapsed = time.monotonic() - started
        self._note_hints(result, response)
        upgrade = (response.headers.get("upgrade", "") or "").lower()
        upgraded = response.status == 101 or ("websocket" in upgrade and 0 < response.status < 400)
        if not upgraded and response.status != 426:
            result.error = f"no WebSocket upgrade (HTTP {response.status})"
            return result

        evidence = self._websocket_mcp_evidence(url, response, offered)
        if response.status == 426:
            result.add_hint("HTTP 426 Upgrade Required: this route is WebSocket-only")
        if not evidence and self.discovery.bool_("websocket_requires_mcp_evidence", True):
            result.add_hint(WEBSOCKET_UNPROVEN_HINT)
            result.error = (
                "a WebSocket answered here, but nothing says it carries MCP -- it accepted none "
                f"of the MCP subprotocols offered ({', '.join(offered) or 'none configured'}) and "
                "the route is not named for MCP -- so it is the service's own socket, not an MCP "
                "endpoint, and it closes no tier"
            )
            return result
        result.add_hint(WEBSOCKET_HINT, *evidence)
        result.error = (
            "a WebSocket endpoint answered here. Claude Code cannot connect to an MCP "
            "server over WebSocket, so this is a native endpoint that needs a stdio "
            "bridge in front of it"
            if upgraded
            else "the route exists but requires a WebSocket upgrade"
        )
        return result

    def _websocket_mcp_evidence(self, url: str, response: Any, offered: Sequence[str]) -> list[str]:
        """What says a WebSocket that answered at ``url`` carries MCP; empty if nothing does."""
        out: list[str] = []
        headers = {str(k).lower(): str(v) for k, v in (getattr(response, "headers", None) or {}).items()}
        accepted = [p.strip().lower() for p in headers.get("sec-websocket-protocol", "").split(",") if p.strip()]
        chosen = [p for p in offered if p.lower() in accepted]
        if chosen:
            out.append(f"the server accepted the {chosen[0]!r} WebSocket subprotocol")
        path = (urlsplit(url).path or "").lower()
        for pattern in self.discovery.str_list("websocket_mcp_path_patterns", ["(^|/)mcp(/|$)"]):
            try:
                if pattern and re.search(pattern, path):
                    out.append(f"the route {path!r} is named for MCP")
                    break
            except re.error as exc:
                log.warning("ignoring invalid websocket_mcp_path_patterns entry %r: %s", pattern, exc)
        return out

    # -- stdio -----------------------------------------------------------------

    def client_env(self, env: Mapping[str, str] | None) -> dict[str, str]:
        """The environment an MCP client gives a stdio server, and no more.

        ``build_env`` starts from ``os.environ``, which is right for trialling a
        *candidate* -- we are finding out what the software can do, with every
        advantage. It is wrong for testing a finished **entry**, because Claude
        Code does not launch servers that way: it passes the entry's own ``env``
        plus a narrow passthrough, and nothing else.

        That difference is a whole class of bug. An entry can pass our launch on
        a variable inherited from the operator's shell, be written, and then fail
        the client's launch for want of the same variable -- which is exactly the
        ``starts: yes`` beside ``claude: no`` that shipped a broken openQA entry.
        The check was more permissive than the thing it was predicting, so it
        could not predict it.

        The passthrough list is configuration (``mcp.verification.client_env``):
        a server needs ``PATH`` to find its interpreter and ``HOME`` to find its
        caches, and which names those are is a property of the platform.
        """
        passthrough = self.verification.str_list("client_env.passthrough", [])
        out = {name: os.environ[name] for name in passthrough if name in os.environ}
        out.update({str(k): str(v) for k, v in (env or {}).items()})
        return out

    def probe_stdio(self, command: Sequence[str], **kwargs: Any) -> Handshake:
        """:meth:`_probe_stdio_once`, launched again when the launcher -- not the server -- failed for a moment.

        Two ``npx -y mcp-graphql@2.0.4`` starting together on a fresh npm cache:
        one read the package before the other finished installing it ("npm
        error enoent ... _npx/.../package.json"), and gitlab-example-net's existing
        entry was judged "does not start" and replaced. A launcher's own
        transient failure (``launcher_transient_patterns``) is no verdict on the
        entry: it is launched again after ``launcher_retry_pause_seconds``.
        (``mcp.verification.launcher_retries``.)
        """
        config = self.ctx.settings.section("mcp.verification")
        retries = config.int_("launcher_retries", LAUNCHER_RETRIES)
        patterns = config.str_list("launcher_transient_patterns", list(LAUNCHER_TRANSIENT_PATTERNS))
        result = self._probe_stdio_once(command, **kwargs)
        for _ in range(max(0, retries)):
            said = f"{result.error or ''}"
            if result.ok or not any(re.search(p, said, re.IGNORECASE) for p in patterns if p):
                break
            log.info(
                "%s: the launcher failed for a moment (%s) -- launched again",
                " ".join(map(str, command[:3])),
                said[:160],
            )
            time.sleep(config.float_("launcher_retry_pause_seconds", 3.0))
            result = self._probe_stdio_once(command, **kwargs)
        warming = config.str_list("launcher_warming_patterns", list(LAUNCHER_WARMING_PATTERNS))
        said = f"{result.error or ''}"
        if not result.ok and not result.credential_pending and any(re.search(p, said) for p in warming if p):
            # Still fetching what it needs: launched again with room to finish,
            # and judged by its answer, never by the download.
            wait = max(
                float(kwargs.get("timeout") or 0.0),
                config.float_("launcher_warm_timeout_seconds", LAUNCHER_WARM_TIMEOUT_SECONDS),
            )
            log.info(
                "%s: the launcher was still fetching what the server needs (%s) -- launched again with %.0fs to finish",
                " ".join(map(str, command[:3])),
                said[:120],
                wait,
            )
            result = self._probe_stdio_once(command, **{**kwargs, "timeout": wait})
            if result.ok or result.credential_pending:
                result.add_hint(
                    "its first start fetched what it needs (a cold launcher cache): a first start in "
                    "Claude Code can be as slow"
                )
        return result

    def _probe_stdio_once(
        self,
        command: Sequence[str],
        *,
        env: Mapping[str, str] | None = None,
        secret_env: Mapping[str, Any] | None = None,
        cwd: str | None = None,
        timeout: float | None = None,
        inherit: bool = True,
        login: bool = False,
        login_tokens: Mapping[str, str] | None = None,
        contract: Mapping[str, Any] | None = None,
        public_calls: Sequence[Mapping[str, Any]] = (),
        withhold: Sequence[str] = (),
    ) -> Handshake:
        """Launch a local server and speak newline-delimited JSON-RPC to it.

        ``inherit`` decides whose environment the child gets: the whole of ours
        (trialling a candidate) or only what a client would pass (testing an
        entry). See :meth:`client_env`. ``login`` asks for the credential the
        server was given to be checked with one read-only call -- only ever
        when that credential is real (see :meth:`_check_login`).

        ``withhold`` names variables this launch claims *not* to have -- a
        credential still pending. Inherited from this run's environment they
        would reach the child anyway, so each is removed unless ``env`` or
        ``secret_env`` sets it on purpose (a stand-in, a real login). Without
        this, github.com's "no credential" trial authenticated with a token the
        child inherited, and the run concluded the token was optional.
        """
        result = Handshake(transport=Transport.STDIO, url=" ".join(command))
        effective = timeout or self.verification.float_("stdio_startup_timeout_seconds", 25)
        argv = [str(c) for c in command]
        started = time.monotonic()
        waited: list[float] = []

        if not argv:
            result.error = "no command to launch"
            return result

        from . import noninteractive

        # This launch's own record of URLs it asked to open (noninteractive):
        # targets are worked side by side, and one shared record would blame
        # the wrong candidate.
        requests = noninteractive.record_for_launch(self.ctx)
        launch_env = self.ctx.runner.build_env(env, secret_env) if inherit else self.client_env(env)
        supplied = {*(env or {}), *(secret_env or {})}
        for name in withhold:
            if name and name not in supplied:
                launch_env.pop(str(name), None)
        if requests is not None:
            launch_env[noninteractive.variable(self.ctx)] = str(requests)
        try:
            process = subprocess.Popen(  # noqa: S603 - argv list, never a shell string
                argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                cwd=cwd,
                env=launch_env,
                # A group of its own, so the server *and everything it started*
                # can be stopped. `npx`, `uvx` and `docker run` are launchers:
                # the process holding our stdout is often a grandchild, and one
                # that ignored stdin closing once held jira.example.com's package
                # stage -- and the whole run -- open indefinitely.
                start_new_session=os.name == "posix",
            )
        except (OSError, ValueError) as exc:
            result.error = f"cannot launch: {exc}"
            return result

        session = _StdioSession(
            process,
            effective,
            ceiling=max(effective, self.verification.float_("stdio_session_timeout_seconds", 240.0)),
            shutdown_grace=self.verification.float_("stdio_shutdown_grace_seconds", 2.0),
            stop_signals=self._stop_signals(),
            stop_grace=float(getattr(self.ctx.runner, "stop_grace_seconds", 5.0) or 0.0),
            label=argv[0],
        )
        try:
            for version in self.versions:
                reply = session.call(self.rpc("initialize", self.initialize_params(version)))
                if reply is None:
                    result.error = session.failure() or "no response to initialize"
                    return result
                if "error" in reply:
                    code = (reply.get("error") or {}).get("code")
                    if code == _INVALID_PARAMS and version != self.versions[-1]:
                        continue
                    result.error = f"server error: {(reply.get('error') or {}).get('message', code)}"
                    return result
                inner = reply.get("result")
                if not isinstance(inner, dict) or "protocolVersion" not in inner:
                    result.error = "reply was not an initialize result"
                    return result
                self._absorb_initialize(result, inner)
                result.start_seconds = time.monotonic() - started
                starts = self.ctx.state.setdefault("mcp_start_seconds", {})
                key = start_key(argv, env)
                starts[key] = max(float(starts.get(key, 0.0)), result.start_seconds)
                result.ok = True
                break
            else:
                result.error = "no offered protocol version was accepted"
                return result

            session.notify(self.rpc("notifications/initialized", request_id=None))

            def call(method: str, rid: int, params: Any = None) -> Any:
                return session.call(self.rpc(method, params or {}, request_id=rid))

            # What was withheld is part of the launch: an answer given with an
            # inherited credential is never reused for one launched without it.
            withheld = sorted(str(n) for n in withhold if n and n not in supplied)
            signature = _signature(argv, env or {}, secret_env or {}) + (json.dumps(withheld) if withheld else "")
            call = self.patient(call, signature, _hosts_of([*argv, *(env or {}).values()]), waited)
            self._list_capabilities(result, call)
            self._prove_round_trip(result, call)
            self._check_public(result, call, public_calls)
            result.credential_sent = login_check.credential_in({**(env or {}), **(secret_env or {})})
            if not result.credential_sent:
                # A call made with no credential to a server that waits for one
                # may never be answered: github-mcp-server, asked get_me with no
                # token, held its address for four minutes. Such calls get a
                # short wait of their own; no answer is "unproven", nothing more.
                quick = self.verification.float_("no_credential_call_timeout_seconds", 20.0)
                if quick > 0:
                    session.timeout = min(float(session.timeout or quick), quick)
                session.give_up_after_silence = self.verification.bool_("no_credential_give_up_after_silence", True)
            if login:
                self._check_login(result, call, login_tokens)
            self._run_contract(result, call, contract)
        finally:
            # Every way a launch can fall short -- calls failed, nothing
            # advertised, no handshake -- has its server's own word read.
            try:
                self._explain_from_stderr(result, session)
            except Exception:  # noqa: BLE001 - an explanation never costs the trial
                pass
            session.close()
            result.elapsed = time.monotonic() - started - sum(waited)
            if requests is not None:
                result.browser_login = noninteractive.take(requests)
        return result

    def _stop_signals(self) -> list[int]:
        """The run's stop ladder (run.stop_signals) -- one policy for every child."""
        ladder = getattr(self.ctx.runner, "stop_ladder", None)
        if callable(ladder):
            return list(ladder())
        return [int(signal.SIGTERM), int(getattr(signal, "SIGKILL", signal.SIGTERM))]

    # -- shared ----------------------------------------------------------------

    def safe_headers(self, url: str, headers: Mapping[str, str] | None) -> dict[str, str]:
        """Lower-cased probe headers, with credentials kept off the wire.

        Step B has this program trying ``http://`` as well as ``https://``, which
        means a bearer token the operator configured for the target would
        otherwise be sent in clear over a network that may not be theirs. The
        probe still happens -- a plaintext endpoint is a finding -- it just
        happens anonymously, and the 401 that results is the honest answer.
        """
        out = {str(k).lower(): str(v) for k, v in (headers or {}).items()}
        if not out or not self.discovery.bool_("strip_auth_on_cleartext", True):
            return out
        parts = urlsplit(url)
        if parts.scheme != "http" or is_local(parts.hostname or ""):
            return out
        stripped = [
            name.lower()
            for name in self.discovery.str_list("cleartext_stripped_headers", list(CLEARTEXT_STRIPPED_HEADERS))
        ]
        for name in stripped:
            if out.pop(name, None) is not None:
                log.debug("withheld the %s header from cleartext probe of %s", name, url)
        return out

    @staticmethod
    def _initialize_result(response: Any) -> bool:
        """Is this reply a JSON-RPC ``initialize`` result? Then no marker in it is a refusal."""
        try:
            payload = response.json()
        except Exception:  # noqa: BLE001 - any unreadable body is simply not one
            return False
        return isinstance(payload, dict) and isinstance(payload.get("result"), dict)

    def _note_hints(self, result: Handshake, response: Any) -> None:
        """Record what a response says about its own protocol (checklist step C).

        Cheap, and it changes conclusions: an endpoint that 400s with a JSON-RPC
        envelope, or 401s with an OAuth challenge, is an MCP endpoint that was
        asked the wrong question -- which is a different report, and a different
        remediation, from a path that is simply not there.
        """
        headers = getattr(response, "headers", None) or {}
        for name in self.discovery.str_list(
            "protocol_hint_headers",
            [
                "content-type",
                "upgrade",
                "connection",
                "www-authenticate",
                "mcp-session-id",
                "mcp-protocol-version",
                "x-mcp-version",
            ],
        ):
            value = str(headers.get(name.lower(), "") or "").strip()
            if value:
                result.add_hint(f"{name}: {value[:120]}")
        body = self._scanned(response).lower()
        for marker in self.discovery.str_list(
            "protocol_hint_markers",
            [
                "jsonrpc",
                "protocolversion",
                "mcp-session-id",
                "notifications/initialized",
                "text/event-stream",
            ],
        ):
            if marker and marker.lower() in body:
                result.add_hint(f"the response body mentions {marker!r}")

    def control_answer(self, url: str) -> tuple[int, str] | None:
        """How this host answers a path that cannot possibly exist.

        The negative control, and the one check here that needs to know nothing
        about the product on the other end. A 401 means "this endpoint wants a
        credential" only on a host where *not everything* answers 401. Ask an
        invented path first and the question answers itself: if the nonsense
        path is refused exactly the way ``/mcp`` was, the refusal was the site's
        habit and carries no information about MCP.

        This is what generalises. A list of products whose 401s are meaningless
        would need a new entry for every Jira, Confluence, service desk and
        intranet ever deployed; a control probe costs one request per origin,
        is cached for the run, and works on hosts nobody has heard of.

        Returns ``(status, content_type)``, or ``None`` when the control is
        disabled or could not be run -- in which case the caller carries on
        without it rather than refusing to decide.
        """
        if not self.discovery.bool_("auth.control_probe", True):
            return None
        split = urlsplit(url)
        if not split.scheme or not split.netloc:
            return None
        origin = f"{split.scheme}://{split.netloc}"
        with self._control_guard:
            if origin in self._controls:
                return self._controls[origin]
            pending = self._control_pending.get(origin)
            asking = pending is None
            if asking:
                pending = self._control_pending[origin] = threading.Event()
        if not asking:
            # Bounded by the owner's own request timeout, which always ends in
            # the `finally` below.
            pending.wait()
            return self._controls.get(origin)

        answer: tuple[int, str] | None = None
        try:
            answer = self._control_probe(origin)
        finally:
            # Recorded even when the probe failed: never probe an origin twice.
            with self._control_guard:
                self._controls[origin] = answer
                self._control_pending.pop(origin, None)
            pending.set()
        return answer

    def _control_probe(self, origin: str) -> tuple[int, str] | None:
        """One request to an invented path on ``origin``. See :meth:`control_answer`."""
        template = self.discovery.str_("auth.control_path_template", "/initialize-claude-ai-control-{nonce}")
        path = template.replace("{nonce}", uuid.uuid4().hex[:12])
        try:
            with self.ctx.http(timeout=self.timeout) as client:
                response = client.post(
                    urljoin(origin, path),
                    headers={"content-type": "application/json", "accept": self.accept},
                    json_body=self.rpc("initialize", self.initialize_params(self.versions[0])),
                    timeout=self.timeout,
                )
        except Exception as exc:  # noqa: BLE001 - a control that fails is just absent
            log.debug("control probe of %s failed: %s", origin, exc)
            return None
        if not response.status:
            return None
        answer = (response.status, (response.content_type or "").lower())
        log.debug("control: %s answers %s %s to a path that does not exist", origin, *answer)
        return answer

    def _note_auth_evidence(self, result: Handshake, response: Any) -> None:
        """Decide whether a 401/403 was an *MCP* endpoint refusing us.

        It usually is not. A Jira, a Confluence, a service desk or any intranet
        answers 401 or 403 on every URL it owns -- including paths that do not
        exist -- so on those hosts the status says nothing whatever about MCP,
        and believing it registers the website as a server. No credential can
        ever make that entry work, which is the worst kind of wrong answer: one
        that looks like progress and sends the operator hunting for a token.

        The test is deliberately generous, because the opposite mistake --
        discarding a real endpoint -- is worse than carrying a doubtful one. Any
        *one* of these is enough: a JSON-RPC envelope in the body, an MCP header
        on the rejection, a pointer to protected-resource metadata, or a content
        type that simply is not a web page. Only an HTML login page with nothing
        else to say fails it.
        """
        if not self.discovery.bool_("auth.require_mcp_evidence", True):
            result.auth_evidence.append("not required by configuration")
            return

        headers = {str(k).lower(): str(v) for k, v in (getattr(response, "headers", None) or {}).items()}
        for name in self.discovery.str_list("auth.evidence_headers", ["Mcp-Session-Id", "MCP-Protocol-Version"]):
            if headers.get(name.lower(), "").strip():
                result.auth_evidence.append(f"the rejection carried {name}")

        challenge = headers.get("www-authenticate", "")
        for marker in self.discovery.str_list("auth.evidence_challenge_markers", ["resource_metadata", "mcp"]):
            if marker and marker.lower() in challenge.lower():
                result.auth_evidence.append(f"the challenge names {marker}")

        body = self._scanned(response)
        for marker in self.discovery.str_list(
            "auth.evidence_body_markers",
            [
                "jsonrpc",
                '"error":{"code"',
                "protocolVersion",
            ],
        ):
            if marker and marker.lower() in body.lower():
                result.auth_evidence.append(f"the body carried {marker!r}")

        content_type = (getattr(response, "content_type", "") or headers.get("content-type", "")).lower()
        disqualifying = [
            c.lower()
            for c in self.discovery.str_list(
                "auth.disqualifying_content_types",
                [
                    "text/html",
                    "application/xhtml+xml",
                ],
            )
        ]
        if content_type and not any(c and c in content_type for c in disqualifying):
            result.auth_evidence.append(f"it answered {content_type.split(';')[0]}, not a web page")

        # The control has the last word. Everything above asks "does this answer
        # look like MCP?"; the control asks the better question, "does this host
        # answer *anything* differently?" -- and on a host that refuses every
        # URL identically, even a JSON-shaped refusal proves nothing.
        control = self.control_answer(result.url)
        if control and result.auth_evidence:
            content_type = (
                (getattr(response, "content_type", "") or headers.get("content-type", "")).split(";")[0].strip().lower()
            )
            if control == (result.status, content_type):
                result.auth_evidence.clear()
                result.add_hint(
                    f"a path that does not exist is refused identically (HTTP {control[0]}"
                    f"{', ' + control[1] if control[1] else ''}), so this host's refusals say "
                    "nothing about MCP"
                )

        # A login page served as JSON still passes everything above, because the
        # only clause that fired was "it answered application/json, not a web
        # page" -- true of every JSON API and therefore of every JSON error.
        # confluence.example.com answers an anonymous POST with
        #     {"errorMessage":"You are not logged in. <a href=\"/login.action\">Log in</a>"}
        # which is a web page wearing a content type, and it was registered as an
        # MCP endpoint on the strength of it. Markers that mean "a human should
        # log in here" therefore overrule the evidence rather than adding to it:
        # no MCP endpoint has any reason to say them.
        if result.auth_evidence:
            found = self._login_marker(body)
            if found:
                result.auth_evidence.clear()
                result.add_hint(
                    f"the rejection says {found!r}: this is a login page telling a person to sign "
                    "in, whatever content type it arrived as, and no token will turn it into an "
                    "MCP endpoint"
                )

        if not result.auth_evidence:
            # Said plainly, because this sentence is what stops somebody going
            # to look for a token that would not have helped.
            result.add_hint("the rejection was a web page, not a protocol answer: no sign this URL speaks MCP")

    def _login_marker(self, body: str) -> str:
        """The first "log in here" marker in a refusal body, or ``""``.

        Kept as configuration rather than a regex so that a product nobody here
        has met can be added without touching code.
        """
        text = (body or "").lower()
        for marker in self.discovery.str_list("auth.login_page_markers", []):
            if marker and marker.lower() in text:
                return marker
        return ""

    def _is_tls_failure(self, result: Handshake) -> bool:
        """Did this probe fail on the certificate rather than on the server?"""
        if result.status != 0 or not result.error:
            return False
        text = result.error.lower()
        markers = self.discovery.str_list("tls_error_markers", list(TLS_ERROR_MARKERS))
        return any(marker.lower() in text for marker in markers if marker)

    def _absorb_initialize(self, result: Handshake, inner: Mapping[str, Any]) -> None:
        result.protocol_version = str(inner.get("protocolVersion", ""))
        info = inner.get("serverInfo") or {}
        if isinstance(info, Mapping):
            result.server_name = str(info.get("name", ""))
            result.server_version = str(info.get("version", ""))
        caps = inner.get("capabilities")
        result.capabilities = dict(caps) if isinstance(caps, Mapping) else {}
        width = self.verification.int_("advertised.instruction_chars", 2000)
        result.instructions = str(inner.get("instructions", ""))[:width]
        # The server's own words about itself, kept where grounding can read
        # them: "this server queries https://build.opensuse.org" in the
        # instructions is the server answering the only question that matters.
        for text in (result.instructions, result.server_name):
            if text.strip():
                result.advertised.append(redact.redact(text.strip()[:width]))

    def _list_capabilities(self, result: Handshake, call) -> None:
        """Enumerate tools/resources/prompts, but only where advertised.

        Calling ``tools/list`` on a server that never declared a ``tools``
        capability is a protocol violation, and some servers close the
        connection over it -- so the capability map decides what we ask for.
        """
        wanted = [
            ("tools", "tools/list", "tools", result.tools),
            ("resources", "resources/list", "resources", result.resources),
            ("prompts", "prompts/list", "prompts", result.prompts),
        ]
        request_id = 2
        for capability, method, key, sink in wanted:
            if capability not in result.capabilities:
                continue
            rid = request_id
            request_id += 1
            try:
                reply = call(method, rid)
            except Exception as exc:  # noqa: BLE001 - listing must never fail a probe
                log.debug("%s failed on %s: %s", method, result.url, exc)
                continue
            if not isinstance(reply, Mapping) or "result" not in reply:
                continue
            if reply.get("id") == rid and not result.round_trip:
                # A reply to a request sent after the handshake, carrying the id
                # that request chose: the session is real and it answers.
                result.round_trip = True
                result.round_trip_method = method
            entries = (reply.get("result") or {}).get(key, [])
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if isinstance(entry, Mapping):
                    name = entry.get("name") or entry.get("uri") or ""
                    if name:
                        sink.append(str(name))
                    self._absorb_self_description(result, entry)
                    if capability == "tools":
                        self._check_tool_schema(result, entry)
                        result.tool_specs.append(login_check.tool_spec(entry))

    def _check_tool_schema(self, result: Handshake, entry: Mapping[str, Any]) -> None:
        """Record a tool name or input property key the client will not accept.

        The client is the judge, so the rule is its, in configuration:
        ``mcp.verification.client_tool_name_pattern`` and
        ``client_name_pattern``. openSUSE's osc-mcp declares a property named
        ``path_repository jsonschema:`` on ``search_packages``; once that tool
        was loaded, every request the Claude Code session made was refused with
        "Property keys should match pattern". Nested schemas are walked too.
        """
        if self.verification.str_("schema_problems", "refuse").strip().lower() == "off":
            return
        tool = str(entry.get("name") or "")
        tool_rule = self.verification.str_("client_tool_name_pattern", "")
        key_rule = self.verification.str_("client_name_pattern", "^[a-zA-Z0-9_.-]{1,64}$")
        if tool_rule and tool and not re.fullmatch(tool_rule, tool):
            result.schema_problems.append({"tool": tool, "key": ""})
        if not key_rule:
            return
        schema = entry.get("inputSchema")
        for key in _property_keys(schema):
            if not re.fullmatch(key_rule, key):
                result.schema_problems.append({"tool": tool, "key": key})

    def schema_refusal(self, handshake: Handshake | None) -> str:
        """Why a server must not be registered for what its tools declare; "" if it may.

        ``mcp.verification.schema_problems``: ``refuse`` (the default) refuses,
        ``warn`` registers it with the problem reported, ``off`` ignores it.
        """
        if handshake is None or not handshake.schema_problems:
            return ""
        if self.verification.str_("schema_problems", "refuse").strip().lower() != "refuse":
            return ""
        return describe_schema_problems(handshake.schema_problems)

    def _absorb_self_description(self, result: Handshake, entry: Mapping[str, Any]) -> None:
        """Keep the address-bearing text a listing entry carries.

        ``_list_capabilities`` took ``name or uri`` and discarded the rest --
        so for a resource *with* a name the URI was lost, which is exactly the
        field that says which host the server talks to.

        Input schemas are included because a generic server is usually
        configured through one: a ``base_url`` parameter whose ``default`` is
        the target is the server telling us what it serves.
        """
        advertised = self.verification.section("advertised")
        if len(result.advertised) >= advertised.int_("entries", 200):
            return
        width = advertised.int_("value_chars", 400)
        for key in advertised.str_list("describing_keys", ["uri", "uriTemplate", "description", "title"]):
            value = entry.get(key)
            if isinstance(value, str) and value.strip():
                result.advertised.append(redact.redact(value.strip()[:width]))
        for key in advertised.str_list("schema_containers", ["inputSchema", "arguments"]):
            schema = entry.get(key)
            if schema is None:
                continue
            strings = _schema_strings(
                schema,
                advertised.int_("schema_depth", 4),
                advertised.str_list("schema_keys", ["description", "default", "const", "title", "pattern", "format"]),
                advertised.int_("schema_list_items", 40),
            )
            for value in strings:
                result.advertised.append(redact.redact(value[:width]))

    def _prove_round_trip(self, result: Handshake, call) -> None:
        """Make sure something was asked *after* the handshake, and answered.

        ``initialize`` alone proves very little: a stale gateway, a cached
        response or a recording can produce one. What cannot be faked cheaply is
        a second request, chosen by this client, on the session the handshake
        opened, answered with the id this client picked. ``tools/list`` usually
        does it already; a server that advertises no listable capability gets
        ``ping``, the method the specification defines for exactly this.
        """
        if result.round_trip or not result.ok:
            return
        method = self.config.str_("ping_method", "ping")
        if not method:
            return
        try:
            reply = call(method, 99)
        except Exception as exc:  # noqa: BLE001 - a failed ping is a verdict, not a crash
            log.debug("%s failed on %s: %s", method, result.url, exc)
            return
        if isinstance(reply, Mapping) and reply.get("id") == 99 and "result" in reply:
            result.round_trip = True
            result.round_trip_method = method

    def _explain_from_stderr(self, result: Handshake, session: Any) -> None:
        """When calls failed, the server's own stderr line naming why, kept on the result and on each failed row.

        mcp-bugzilla answered every call "Failed to resolve dependency 'bz'";
        its stderr said ``Ignoring invalid SSL_CERT_FILE. File does not exist``
        and ``FileNotFoundError``: a trust store it was given did not exist. The
        reply named no cause, so the server was blamed. The last stderr line
        matching a class of ``[mcp.verification.stderr_causes]`` (first class
        that matches wins) is the cause; classes ``trust`` and ``transport``
        are repaired, never blamed on the server.
        """
        failed = [
            r
            for r in result.functional
            if r.get("verdict") == login_check.UNCHECKED and str(r.get("detail") or "").startswith("the call failed")
        ]
        failed += [r for r in result.contract if r.get("passed") is False]
        # graphql-mcp-server listed nothing: the CA bundle its entry named, from
        # another machine, was not here, and its schema never loaded.
        empty = not result.ok or not (result.tools or result.resources or result.prompts)
        if not (failed or empty) or not self.verification.section("stderr_causes").bool_("enabled", True):
            return
        lines = []
        lock = getattr(session, "_stderr_lock", None)
        try:
            if lock is not None:
                with lock:
                    lines = [str(line).strip() for line in list(getattr(session, "_stderr", [])) if str(line).strip()]
        except Exception:  # noqa: BLE001 - an explanation, never a reason to fail
            return
        cause = stderr_cause(lines, self.ctx.settings)
        if not cause:
            return
        result.stderr_cause = cause
        said = f"the server's own log says: {cause['line']} ({cause['class']})"
        for row in failed:
            row["detail"] = f"{row.get('detail') or 'failed'} -- {said}"
        if empty and result.error and said not in result.error:
            result.error = f"{result.error} -- {said}"
        log.info("%s: %s; %s", result.url, "calls failed" if failed else "it offered nothing", said)

    def _check_login(self, result: Handshake, call, tokens: Mapping[str, str] | None = None) -> None:
        """Call read-only tools and record what they show: the login, and the settings.

        A listing proves the server is there; only a call proves the service
        behind it takes the credential -- sd.example.com's ``mcp-atlassian`` listed
        49 tools and failed every call. And one accepted call proves only its
        own code path: osc-mcp's API tools took ``https://api.opensuse.org``,
        ``search_packages`` answered "unexpected api address format". So up to
        ``max_calls`` tools are asked (:func:`.login_check.pick`, read-only
        only), each answer is kept in :attr:`Handshake.functional`, the login
        verdict is the first decisive one, and a "misconfigured" answer from any
        is kept beside it. Asked only by a caller whose credential is real --
        never with the stand-in.
        """
        if not result.ok or login_check.mode(self.ctx.settings) == "off":
            return
        chosen = login_check.pick(result.tool_specs, self.ctx.settings, tokens)
        result.unexercised = login_check.unexercised(result.tool_specs, self.ctx.settings, tokens)
        if not chosen:
            result.login = login_check.UNCHECKED
            result.login_detail = "no tool is both read-only and callable without arguments nobody can supply"
            return
        verdicts: list[tuple[str, str, str]] = []
        for offset, (name, arguments) in enumerate(chosen):
            rid = 90 + offset
            try:
                reply = call("tools/call", rid, {"name": name, "arguments": arguments})
            except Exception as exc:  # noqa: BLE001 - a failed check is a verdict, not a crash
                reply = None
                log.debug("tools/call %s failed on %s: %s", name, result.url, exc)
            verdict, detail = login_check.judge(reply, self.ctx.settings)
            row = {"tool": name, "verdict": verdict, "detail": detail}
            if login_check.account_missing(name, verdict, detail, self.ctx.settings):
                # The account's own record does not exist: the login was
                # recognised and the service has no account behind it.
                verdict = row["verdict"] = login_check.NO_ACCESS
                detail = row["detail"] = f"{name}, which reads this account's own record, answered 404 ({detail})"
                row["reason"] = login_check.NO_ACCOUNT
            result.functional.append(row)
            verdicts.append((verdict, name, detail))
            if verdict == login_check.MISCONFIGURED and not result.misconfigured:
                result.misconfigured = f"{name}: {detail}"
            if verdict in login_check.REFUSED:
                break  # a refused credential is refused for every tool
        # A refusal from any call outranks acceptance from another, and
        # acceptance needs content: an empty answer proves nothing.
        result.login, result.login_tool, result.login_detail = login_check.summarise(verdicts, self.ctx.settings)
        result.login_reason = next(
            (
                str(r.get("reason"))
                for r in result.functional
                if r.get("reason") and r.get("tool") == result.login_tool and r.get("verdict") == result.login
            ),
            "",
        )
        self._probe_settings(result, call, {n for n, _ in chosen})
        log.info(
            "%s: checked with %s -- %s%s",
            result.url,
            ", ".join(n for n, _ in chosen),
            login_check.in_words(result.login, sent=result.credential_sent),
            f"; {login_check.in_words(login_check.MISCONFIGURED)}" if result.misconfigured else "",
        )

    def _probe_settings(self, result: Handshake, call, asked: set[str]) -> None:
        """Ask each read tool nobody could call, once, with stand-in arguments -- only whether its settings are accepted.

        A setting is read by the tools that use it, and checked before their
        arguments: osc-mcp's ``search_packages`` alone read ``OSC_MCP_API``,
        needed three arguments nobody could fill, and was never called -- so
        the address form it rejected was never found, and the entry shipped
        with it. The answer is read for a setting error
        (``misconfiguration_markers``) and nothing else: never a login verdict,
        never a usage task. Read tools only, as for the login check
        (``mcp.verification.login_check.probe_settings``, ``setting_probe_max``).
        """
        config = self.ctx.settings.section("mcp.verification.login_check")
        if not config.bool_("probe_settings", True) or result.login in login_check.REFUSED:
            return  # a refused credential is never sent again: not for this, not for anything
        never = config.str_list("never_patterns", list(login_check.DEFAULT_NEVER_PATTERNS))
        verbs = config.str_list("read_verbs", list(login_check.DEFAULT_READ_VERBS))
        budget = max(0, config.int_("setting_probe_max", 12))
        for spec in result.tool_specs:
            name = str(spec.get("name") or "")
            if budget <= 0 or not name or name in asked or not spec.get("required"):
                continue
            if login_check._matches(never, name) or spec.get("destructive") is True or spec.get("read_only") is False:
                continue
            if spec.get("read_only") is not True and not login_check._reads(name, verbs):
                continue
            arguments = login_check.setting_probe_arguments(spec, self.ctx.settings)
            if arguments is None:
                continue
            budget -= 1
            try:
                reply = call("tools/call", 120 + budget, {"name": name, "arguments": arguments})
            except Exception as exc:  # noqa: BLE001 - a failed probe is a verdict, not a crash
                log.debug("setting probe %s failed on %s: %s", name, result.url, exc)
                continue
            verdict, detail = login_check.judge(reply, self.ctx.settings)
            rejected = verdict == login_check.MISCONFIGURED
            result.functional.append(
                {
                    "tool": name,
                    "verdict": login_check.MISCONFIGURED if rejected else SETTINGS_ACCEPTED,
                    "detail": detail if rejected else "its settings were accepted (stand-in arguments)",
                    "probe": True,
                }
            )
            if rejected and not result.misconfigured:
                result.misconfigured = f"{name}: {detail}"

    def patient(self, call, signature: str, host: str, waited: list[float] | None = None):
        """``call``, asking each ``tools/call`` once per run, paced per host, and once more if told "later".

        * The same call on the same launch -- command, settings, credential --
          is answered once per run and reused: the login check, the usage tasks
          and a slow-start retry often want the same read.
        * Calls to one host are paced (``[mcp.verification.pacing]``): the
          run's own checks were what got bugzilla.example.com to answer "429 Too
          Many Requests".
        * An answer that says "try again later" (:func:`.login_check.is_temporary`)
          is asked once more after ``temporary_retry_seconds``; a second one is
          left as it is, and judged temporary -- inconclusive, never a failure.
        """
        settings = self.ctx.settings
        memo = self.ctx.state.setdefault("mcp_call_memo", {})
        config = settings.section("mcp.verification.login_check")
        pause = min(config.float_("temporary_retry_seconds", 5.0), config.float_("temporary_retry_max_seconds", 30.0))

        def temporary(reply: Any) -> bool:
            result = reply.get("result") if isinstance(reply, Mapping) else None
            if not isinstance(result, Mapping) or not result.get("isError"):
                return False
            text = " ".join(str(p.get("text") or "") for p in result.get("content") or [] if isinstance(p, Mapping))
            return login_check.is_temporary(text, settings)

        def ask(method: str, rid: int, params: Any = None) -> Any:
            if method != "tools/call":
                return call(method, rid, params)
            key = signature + json.dumps(params or {}, sort_keys=True, default=str)
            fresh = getattr(_FRESH, "depth", 0) > 0
            if key in memo and not fresh:
                return memo[key]
            slept = self._pace(host)
            reply = call(method, rid, params)
            if temporary(reply) and pause >= 0:
                log.info("%s: asked to try again later; once more in %.0fs", host or "the server", pause)
                time.sleep(pause)
                slept += pause + self._pace(host)
                reply = call(method, rid, params)
            if waited is not None:
                waited.append(slept)
            if not temporary(reply) and not fresh:
                memo[key] = reply
            return reply

        return ask

    def _pace(self, host: str) -> float:
        """Wait until another call to ``host`` keeps within ``[mcp.verification.pacing]``; the seconds waited."""
        pacing = self.ctx.settings.section("mcp.verification.pacing")
        if not host or not pacing.bool_("enabled", True):
            return 0.0
        gap = max(0.0, pacing.float_("min_gap_seconds", 0.2))
        per_minute = max(0, pacing.int_("max_per_minute", 40))
        with _PACE_LOCK:
            seen = self.ctx.state.setdefault("mcp_call_times", {}).setdefault(host, [])
            now = time.monotonic()
            seen[:] = [t for t in seen if now - t < 60.0]
            wait = 0.0
            if seen:
                wait = max(wait, gap - (now - seen[-1]))
            if per_minute and len(seen) >= per_minute:
                wait = max(wait, 60.0 - (now - seen[0]))
            seen.append(now + max(0.0, wait))
        if wait > 0:
            time.sleep(wait)
        return max(0.0, wait)

    def _run_contract(self, result: Handshake, call, contract: Mapping[str, Any] | None) -> None:
        """Ask the usage tasks that apply to this server (:mod:`.contract`); record each verdict.

        Run only when the caller says the server has what it needs -- a real
        credential, or none required -- since a task cannot succeed without it.
        The answers are judged and dropped: only pass/fail and why are kept.
        """
        from . import contract as usage  # contract imports login_check, as this module does

        if not result.ok or not contract or not contract.get("tasks"):
            return
        calls, skipped = usage.resolve(
            contract["tasks"],
            result.tool_specs,
            host=str(contract.get("host") or ""),
            limit=self.ctx.settings.int_("mcp.contract.max_tasks", usage.DEFAULT_MAX_TASKS),
            settings=self.ctx.settings,
        )
        result.contract_skipped = skipped
        for offset, planned in enumerate(calls):
            try:
                reply = call("tools/call", 70 + offset, {"name": planned["tool"], "arguments": planned["arguments"]})
            except Exception as exc:  # noqa: BLE001 - a failed task is a verdict, not a crash
                reply = None
                log.debug("usage task %s failed on %s: %s", planned["task"], result.url, exc)
            passed, detail = usage.judge(
                reply, planned, self.ctx.settings, contract.get("truth"), sent=result.credential_sent
            )
            result.contract.append(
                {
                    "task": planned["task"],
                    "tool": planned["tool"],
                    "source": planned["source"],
                    "arguments": dict(planned["arguments"]),
                    "passed": passed,
                    "detail": detail,
                    "needs_login": bool(planned.get("needs_login")),
                }
            )
            log.info("%s: usage task %r with %s -- %s", result.url, planned["task"], planned["tool"], detail)
        # A task no tool was found for by name or description is done by a read
        # tool whose answer carries the kind's own words: smelt's agent-written
        # bridge answered introspection under a name no pattern knew.
        offset = 90
        for task in usage.unmatched(contract["tasks"], calls):
            for tool in usage.by_answer(task, result.tool_specs, self.ctx.settings):
                offset += 1
                try:
                    reply = call("tools/call", offset, {"name": tool, "arguments": {}})
                except Exception:  # noqa: BLE001 - a tool that cannot answer does not do the task
                    continue
                marker = usage.carries(reply, task.answer_markers)
                if not marker:
                    continue
                passed, detail = usage.judge(
                    reply, {"expect": list(task.expect)}, self.ctx.settings, sent=result.credential_sent
                )
                if passed:
                    detail = f"passed: its answer carries the kind's own {marker!r}"
                result.contract.append(
                    {
                        "task": task.name,
                        "tool": tool,
                        "source": task.source,
                        "arguments": {},
                        "passed": passed,
                        "detail": detail,
                        "found_by": "answer",
                        "needs_login": task.needs_login,
                    }
                )
                log.info("%s: usage task %r found in %s by its answer -- %s", result.url, task.name, tool, detail)
                break

    def _check_public(self, result: Handshake, call, public_calls: Sequence[Mapping[str, Any]]) -> None:
        """Call the first listed tool that needs no login; record whether it answered with real content.

        A bridge generated from an API kind's operations (:func:`.api_kinds.public_calls`)
        proves it reaches that API this way, with no credential at all. Without
        it, smelt.example.net's bridge -- built for an API smelt does not serve --
        listed its tools and was recorded as waiting for a credential.
        """
        from .api_kinds import public_answer  # api_kinds imports the types this module does

        if not result.ok:
            return
        listed = set(result.tools)
        for entry in public_calls:
            name = str(entry.get("tool") or "")
            if name not in listed:
                continue
            try:
                reply = call("tools/call", 80, {"name": name, "arguments": {}})
            except Exception as exc:  # noqa: BLE001 - a failed call is a verdict, not a crash
                reply = None
                log.debug("tools/call %s failed on %s: %s", name, result.url, exc)
            answered, detail = public_answer(reply, list(entry.get("required_keys") or []))
            result.public_check = {"tool": name, "answered": answered, "detail": detail}
            log.info(
                "%s: %s, which needs no login, %s",
                result.url,
                name,
                "answered" if answered else f"did not answer with real content ({detail})",
            )
            return

    # -- policy ----------------------------------------------------------------

    def accepts(self, handshake: Handshake | None) -> tuple[bool, str]:
        """Apply ``[mcp.verification]`` to a handshake. Returns (accepted, why)."""
        if handshake is None:
            return False, "not probed"
        if self.verification.bool_("require_initialize", True) and not handshake.ok:
            return False, handshake.error or "initialize did not complete"
        if self.verification.bool_("require_tool_listing", True):
            has_any = bool(handshake.tools or handshake.resources or handshake.prompts)
            if not has_any and not self.verification.bool_("allow_empty_tools", False):
                return False, "server exposes no tools, resources or prompts"
        if self.verification.bool_("require_round_trip", True) and not handshake.round_trip:
            return False, (
                "the handshake completed but no request after it was answered, so the "
                "connection was never shown to work in both directions"
            )
        refusal = self.schema_refusal(handshake)
        if refusal:
            return False, refusal
        return True, handshake.summary()


# ----------------------------------------------------------------------------
# stdio plumbing
# ----------------------------------------------------------------------------


class _StdioSession:
    """A line-oriented JSON-RPC session over a child process' stdio.

    Reads run on a daemon thread so a server that never answers cannot wedge the
    run -- every call is bounded by the configured timeout, the whole session by
    ``ceiling``, and :meth:`close` by the stop ladder. All three bounds matter:
    bounding only the calls left the shutdown unbounded, and the shutdown is
    where jira.example.com's run stalled with nothing in the log.
    """

    def __init__(
        self,
        process: subprocess.Popen,
        timeout: float,
        *,
        ceiling: float = 0.0,
        shutdown_grace: float = 2.0,
        stop_signals: Sequence[int] = (),
        stop_grace: float = 5.0,
        label: str = "",
    ) -> None:
        self.process = process
        self.timeout = timeout
        self.deadline = time.monotonic() + ceiling if ceiling and ceiling > 0 else 0.0
        self.shutdown_grace = max(float(shutdown_grace or 0.0), 0.0)
        self.stop_signals = list(stop_signals) or [
            int(signal.SIGTERM),
            int(getattr(signal, "SIGKILL", signal.SIGTERM)),
        ]
        self.stop_grace = max(float(stop_grace or 0.0), 0.0)
        self.label = label
        # Remembered now: once the leader is reaped its group id can no longer
        # be looked up, and a grandchild still holding the pipe is exactly the
        # process that has to be reached after that.
        self.group: int | None = None
        if os.name == "posix":
            with suppress(OSError):
                group = os.getpgid(process.pid)
                if group != os.getpgid(0):
                    self.group = group
        self._lines: queue.Queue[str | None] = queue.Queue()
        # The LAST lines, not the first. This was a plain list capped at 40 by
        # refusing to append past it, which keeps the head -- and a traceback
        # puts the one line that names a cause at the very end. A server whose
        # startup chain ran past 40 lines therefore had its exception dropped
        # before anything could read it, and ``failure()`` then reported a frame
        # from the middle. gitlab.example.net's bridge died on
        # ``ValueError: GitLab token is required. Set one of: GITLAB_TOKEN, ...``
        # -- a message that names the withheld variable outright -- and none of
        # it survived to be read.
        self._stderr: deque[str] = deque(maxlen=_STDERR_LINES)
        #: The reader thread appends while failure() reads; iterating a deque
        #: that is being appended to raises, and a server still writing to
        #: stderr when the handshake timed out turned the stage into an
        #: "unexpected error".
        self._stderr_lock = threading.Lock()
        self._reader = threading.Thread(target=self._pump_stdout, daemon=True)
        self._errors = threading.Thread(target=self._pump_stderr, daemon=True)
        self._reader.start()
        self._errors.start()

    def _pump_stdout(self) -> None:
        try:
            for line in self.process.stdout or ():
                self._lines.put(line)
        except Exception:  # noqa: BLE001 - the pipe closing is normal
            pass
        finally:
            self._lines.put(None)

    def _pump_stderr(self) -> None:
        try:
            for line in self.process.stderr or ():
                with self._stderr_lock:
                    self._stderr.append(line.rstrip())
        except Exception:  # noqa: BLE001
            pass

    def notify(self, payload: Mapping[str, Any]) -> None:
        self._send(payload)

    #: Set by a caller whose calls carry no credential: once one call goes
    #: unanswered, the rest of the session answers None at once -- a server
    #: waiting for a token answers none of them (github-mcp-server: a dozen
    #: unanswered calls filled the whole four-minute session).
    give_up_after_silence: bool = False
    silent: bool = False

    def call(self, payload: Mapping[str, Any]) -> dict[str, Any] | None:
        """Send a request and wait for the matching response id."""
        if self.give_up_after_silence and self.silent and payload.get("id") is not None:
            return None
        reply = self._call(payload)
        if reply is None and payload.get("id") is not None and self.give_up_after_silence:
            self.silent = True
        return reply

    def _call(self, payload: Mapping[str, Any]) -> dict[str, Any] | None:
        if not self._send(payload):
            return None
        wanted = payload.get("id")
        deadline = time.monotonic() + self.timeout
        if self.deadline:
            deadline = min(deadline, self.deadline)
        while time.monotonic() < deadline:
            try:
                line = self._lines.get(timeout=max(0.1, deadline - time.monotonic()))
            except queue.Empty:
                return None
            if line is None:  # process closed stdout
                return None
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                # Servers commonly print banners to stdout before behaving.
                continue
            if isinstance(message, dict) and message.get("id") == wanted:
                return message
        return None

    def _send(self, payload: Mapping[str, Any]) -> bool:
        if self.process.stdin is None or self.process.poll() is not None:
            return False
        try:
            self.process.stdin.write(json.dumps(payload) + "\n")
            self.process.stdin.flush()
        except (BrokenPipeError, OSError, ValueError):
            return False
        return True

    def failure(self) -> str:
        """Why the server is not answering, with the informative line first.

        This joined the last three stderr lines oldest-first, which for a Python
        traceback means a code frame leads and the exception -- the only line
        that says what went wrong -- trails. Anything that then shortens the
        text for a console keeps the frame and drops the exception, so a real
        bridge reported::

            process exited with rc=1: user_lifespan_result = await stack.enter...

        which names no cause at all. The downstream rule that decides whether a
        failed trial was a credential wall was reading the same string, so the
        wrong end of the traceback cost the target its server.

        So the last line that looks like ``SomeError: message`` is promoted to
        the front, the frame that raised it follows, and the surrounding lines
        come last as context -- see :func:`_failure_lines`.
        """
        with self._stderr_lock:
            captured = list(self._stderr)
        lines = [line.strip() for line in captured if line.strip()]
        detail = " | ".join(_failure_lines(lines))
        if self.process.poll() not in (None, 0):
            return f"process exited with rc={self.process.returncode}: {detail}".strip()
        return detail

    def close(self) -> None:
        """End the session in bounded time, whatever the server does.

        The order is the fix. This used to close stdout and stderr *first* --
        while the reader threads were still blocked reading them. Closing a
        buffered stream waits for a read in progress on another thread, and that
        read ends only when every process holding the pipe exits; so a server
        that ignores stdin closing (or a launcher's grandchild that inherited
        the pipe) blocked ``close()`` forever, and the terminate/kill after it
        was never reached. jira.example.com's package stage stalled there with
        nothing in the log.

        Now: stdin is closed (the polite request), the server gets
        ``shutdown_grace`` seconds, then its whole process group is taken down
        the run's stop ladder (``run.stop_signals``, ``run.stop_grace_seconds``
        each), and only once the readers have finished are their pipes closed.
        A pipe still held by something outside the group is abandoned -- left
        to a daemon thread -- rather than waited on.
        """
        with suppress(Exception):
            if self.process.stdin is not None:
                self.process.stdin.close()
        if not self._settled(self.shutdown_grace):
            for sig in self.stop_signals:
                self._signal(sig)
                if self._settled(self.stop_grace):
                    break
        if self._readers_done():
            for stream in (self.process.stdout, self.process.stderr):
                with suppress(Exception):
                    if stream is not None:
                        stream.close()
            return
        log.warning(
            "%s: still holding its output pipe after every stop signal; abandoned rather than waited on (pid %s)",
            self.label or "stdio server",
            self.process.pid,
        )

    def _settled(self, seconds: float) -> bool:
        """Wait up to ``seconds`` for the leader to exit *and* the pipes to drain."""
        end = time.monotonic() + seconds
        with suppress(subprocess.TimeoutExpired):
            self.process.wait(timeout=max(end - time.monotonic(), 0.0))
        for thread in (self._reader, self._errors):
            thread.join(timeout=max(end - time.monotonic(), 0.0))
        return self.process.poll() is not None and self._readers_done()

    def _readers_done(self) -> bool:
        return not (self._reader.is_alive() or self._errors.is_alive())

    def _signal(self, sig: int) -> None:
        """Signal the server's whole group, or the server alone if it has none."""
        with suppress(OSError, ValueError):
            if self.group is not None:
                os.killpg(self.group, sig)
                return
            if self.process.poll() is None:
                self.process.send_signal(sig)


def _signature(argv: Sequence[str], env: Mapping[str, Any], secret_env: Mapping[str, Any]) -> str:
    """One launch's identity: its command, settings and credentials -- the credentials by fingerprint only."""
    from .secret_policy import credential_fingerprint

    plain = sorted((str(k), str(v)) for k, v in env.items() if not hasattr(v, "reveal"))
    secrets = sorted(
        (str(k), credential_fingerprint([v]))
        for k, v in {**secret_env, **{k: v for k, v in env.items() if hasattr(v, "reveal")}}.items()
    )
    return json.dumps([list(map(str, argv)), plain, secrets], sort_keys=True)


def _hosts_of(texts: Iterable[Any]) -> str:
    from .grounding import hosts_named

    found = hosts_named(texts)
    return found[0] if found else ""


def _endpoint_from_events(events: Iterable[Mapping[str, str]]) -> str:
    """Extract the POST endpoint the legacy SSE transport announces."""
    for event in events:
        if (event.get("event") or "").strip() == "endpoint":
            data = (event.get("data") or "").strip()
            if data:
                return data
    # Some implementations omit the event name and just send the URL.
    for event in events:
        data = (event.get("data") or "").strip()
        if data.startswith(("/", "http://", "https://")):
            return data
    return ""


__all__ = ["McpProber", "WEBSOCKET_HINT", "WEBSOCKET_UNPROVEN_HINT", "TLS_ERROR_MARKERS", "CLEARTEXT_STRIPPED_HEADERS"]
