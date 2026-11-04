"""A tiny HTTP client that works before the environment is provisioned.

``httpx`` is used when present (better timeouts, HTTP/2, real streaming); the
stdlib ``urllib`` backend keeps phases 1-2 working on a bare machine where no
third-party package is installed yet.  The surface is deliberately narrow: the
MCP probes need request/response plus a bounded server-sent-events read.
"""

from __future__ import annotations

import gzip
import json
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from dataclasses import dataclass, field
from typing import Any, Iterator, Mapping

from . import redact
from .logging_setup import get_logger

log = get_logger("http")

try:  # optional, installed by phase 1
    import httpx  # type: ignore[import-not-found]
except ModuleNotFoundError:  # pragma: no cover
    httpx = None  # type: ignore[assignment]


@dataclass
class Response:
    url: str
    status: int
    headers: dict[str, str] = field(default_factory=dict)
    text: str = ""
    elapsed: float = 0.0
    error: str = ""
    #: Populated when a redirect chain was followed.
    final_url: str = ""

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    @property
    def content_type(self) -> str:
        return (self.headers.get("content-type") or "").split(";")[0].strip().lower()

    def json(self) -> Any:
        """Parse the body as JSON, tolerating a single SSE ``data:`` frame."""
        body = self.text.strip()
        if not body:
            return None
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            pass
        for line in body.splitlines():
            line = line.strip()
            if line.startswith("data:"):
                payload = line[5:].strip()
                if payload and payload != "[DONE]":
                    try:
                        return json.loads(payload)
                    except json.JSONDecodeError:
                        continue
        return None

    def summary(self) -> str:
        if self.error:
            return f"{self.status or 0} {self.error}"
        return f"{self.status} {self.content_type or 'unknown'} {len(self.text)}B"

    @property
    def problem(self) -> str:
        """Why this response is unusable, in words a reader can act on.

        ``status`` is 0 when the request never produced a response at all --
        DNS, TLS, a refused connection, a timeout. Reporting that as "HTTP 0"
        reads as a server that answered strangely, and sends whoever is
        debugging to the wrong end of the wire; the transport error says which
        end actually failed.
        """
        if self.status == 0:
            return self.error or "no response (transport error)"
        return f"HTTP {self.status}" + (f" ({self.error})" if self.error else "")

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "final_url": self.final_url or self.url,
            "status": self.status,
            "content_type": self.content_type,
            "elapsed_seconds": round(self.elapsed, 3),
            "error": redact.redact(self.error),
            "body_preview": redact.redact(self.text[:800]),
        }


class HttpClient:
    """Backend-agnostic client. All methods return a :class:`Response`."""

    def __init__(
        self,
        *,
        timeout: float = 10.0,
        connect_timeout: float = 5.0,
        user_agent: str = "initialize-claude-ai",
        verify_tls: bool = True,
        max_bytes: int = 1_000_000,
        follow_redirects: bool = True,
        default_headers: Mapping[str, str] | None = None,
        ssl_context: ssl.SSLContext | None = None,
    ) -> None:
        self.timeout = timeout
        self.connect_timeout = connect_timeout
        self.user_agent = user_agent
        self.verify_tls = verify_tls
        self.max_bytes = max_bytes
        self.follow_redirects = follow_redirects
        self.default_headers = dict(default_headers or {})
        #: What a verified connection trusts. ``None`` keeps each backend's own
        #: default -- certifi for httpx, the host's store for urllib -- which is
        #: two different answers; :meth:`RunContext.http` passes the run's one.
        self.ssl_context = ssl_context if verify_tls else None
        self._client: Any = None
        if not verify_tls:
            log.warning("TLS verification is disabled; only do this against trusted internal hosts")

    # -- lifecycle -------------------------------------------------------------

    def __enter__(self) -> "HttpClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        if self._client is not None:
            try:
                self._client.close()
            except Exception:  # pragma: no cover
                pass
            self._client = None

    def _httpx_client(self) -> Any:
        if self._client is None:
            self._client = httpx.Client(
                timeout=httpx.Timeout(self.timeout, connect=self.connect_timeout),
                verify=self.ssl_context if self.ssl_context is not None else self.verify_tls,
                follow_redirects=self.follow_redirects,
                headers={"user-agent": self.user_agent},
            )
        return self._client

    # -- requests --------------------------------------------------------------

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        json_body: Any = None,
        data: bytes | str | None = None,
        timeout: float | None = None,
    ) -> Response:
        merged = {"user-agent": self.user_agent, **self.default_headers}
        merged.update({k.lower(): v for k, v in (headers or {}).items()})
        body: bytes | None = None
        if json_body is not None:
            body = json.dumps(json_body).encode("utf-8")
            merged.setdefault("content-type", "application/json")
        elif data is not None:
            body = data.encode("utf-8") if isinstance(data, str) else data
        effective = self.timeout if timeout is None else timeout

        if httpx is not None:
            return self._request_httpx(method, url, merged, body, effective)
        return self._request_urllib(method, url, merged, body, effective)

    def get(self, url: str, **kwargs: Any) -> Response:
        return self.request("GET", url, **kwargs)

    def head(self, url: str, **kwargs: Any) -> Response:
        return self.request("HEAD", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> Response:
        return self.request("POST", url, **kwargs)

    # -- backends --------------------------------------------------------------

    def _budget(self, timeout: float) -> Any:
        """Per-request timeout that still respects the connect budget.

        Passing a bare float to httpx replaces *every* phase of the timeout,
        connect included -- so a 30 s read budget silently became a 30 s connect
        budget. On a host whose AAAA records blackhole, httpx applies that
        budget to each resolved address in turn, and a single call can stall for
        minutes. Keeping connect separate bounds that to something sane.
        """
        connect = min(self.connect_timeout, timeout) if self.connect_timeout else timeout
        return httpx.Timeout(timeout, connect=connect)

    def _request_httpx(
        self, method: str, url: str, headers: dict[str, str], body: bytes | None, timeout: float
    ) -> Response:
        started = time.monotonic()
        try:
            raw = self._httpx_client().request(
                method, url, headers=headers, content=body, timeout=self._budget(timeout)
            )
        except Exception as exc:  # noqa: BLE001 - surface as a Response, never raise
            return Response(url=url, status=0, error=_describe(exc), elapsed=time.monotonic() - started)
        text = raw.text
        if len(text) > self.max_bytes:
            text = text[: self.max_bytes]
        return Response(
            url=url,
            final_url=str(raw.url),
            status=raw.status_code,
            headers={k.lower(): v for k, v in raw.headers.items()},
            text=text,
            elapsed=time.monotonic() - started,
        )

    def _request_urllib(
        self, method: str, url: str, headers: dict[str, str], body: bytes | None, timeout: float
    ) -> Response:
        started = time.monotonic()
        request = urllib.request.Request(url, data=body, method=method.upper())
        for key, value in headers.items():
            request.add_header(key, value)
        request.add_header("accept-encoding", "gzip, deflate")
        context = self._urllib_context()
        opener_args: list[Any] = [urllib.request.HTTPSHandler(context=context)]
        if not self.follow_redirects:
            opener_args.append(_NoRedirect())
        opener = urllib.request.build_opener(*opener_args)
        try:
            with opener.open(request, timeout=timeout) as raw:  # noqa: S310 - scheme validated by caller
                payload = raw.read(self.max_bytes + 1)
                return Response(
                    url=url,
                    final_url=raw.geturl(),
                    status=raw.status,
                    headers={k.lower(): v for k, v in raw.headers.items()},
                    text=_decode_body(payload[: self.max_bytes], raw.headers.get("content-encoding", "")),
                    elapsed=time.monotonic() - started,
                )
        except urllib.error.HTTPError as exc:
            payload = b""
            try:
                payload = exc.read(self.max_bytes)
            except Exception:  # pragma: no cover
                pass
            return Response(
                url=url,
                status=exc.code,
                headers={k.lower(): v for k, v in (exc.headers or {}).items()},
                text=_decode_body(payload, (exc.headers or {}).get("content-encoding", "")),
                elapsed=time.monotonic() - started,
            )
        except Exception as exc:  # noqa: BLE001
            return Response(url=url, status=0, error=_describe(exc), elapsed=time.monotonic() - started)

    def _urllib_context(self) -> ssl.SSLContext:
        if not self.verify_tls:
            context = ssl.create_default_context()
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
            return context
        return self.ssl_context or ssl.create_default_context()

    # -- server-sent events ----------------------------------------------------

    def stream_sse(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        max_events: int = 3,
        timeout: float | None = None,
    ) -> tuple[Response, list[dict[str, str]]]:
        """Open an SSE stream and read at most ``max_events`` events.

        Returns the response metadata plus parsed events (``{"event", "data"}``).
        Used to detect the deprecated HTTP+SSE MCP transport, whose first event
        announces the POST endpoint to use.
        """
        merged = {
            "user-agent": self.user_agent,
            "accept": "text/event-stream",
            "cache-control": "no-cache",
            **{k.lower(): v for k, v in (headers or {}).items()},
        }
        effective = self.timeout if timeout is None else timeout
        events: list[dict[str, str]] = []
        started = time.monotonic()

        if httpx is not None:
            try:
                with self._httpx_client().stream("GET", url, headers=merged, timeout=self._budget(effective)) as raw:
                    meta = Response(
                        url=url,
                        final_url=str(raw.url),
                        status=raw.status_code,
                        headers={k.lower(): v for k, v in raw.headers.items()},
                        elapsed=time.monotonic() - started,
                    )
                    if raw.status_code < 400:
                        events = list(_parse_sse(raw.iter_lines(), max_events, started, effective))
                    return meta, events
            except Exception as exc:  # noqa: BLE001
                return Response(url=url, status=0, error=_describe(exc), elapsed=time.monotonic() - started), []

        request = urllib.request.Request(url, method="GET")
        for key, value in merged.items():
            request.add_header(key, value)
        context = self._urllib_context()
        try:
            with urllib.request.urlopen(request, timeout=effective, context=context) as raw:  # noqa: S310
                meta = Response(
                    url=url,
                    final_url=raw.geturl(),
                    status=raw.status,
                    headers={k.lower(): v for k, v in raw.headers.items()},
                    elapsed=time.monotonic() - started,
                )
                lines = (line.decode("utf-8", "replace").rstrip("\n") for line in raw)
                events = list(_parse_sse(lines, max_events, started, effective))
                return meta, events
        except urllib.error.HTTPError as exc:
            return Response(url=url, status=exc.code, elapsed=time.monotonic() - started), []
        except Exception as exc:  # noqa: BLE001
            return Response(url=url, status=0, error=_describe(exc), elapsed=time.monotonic() - started), []


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:  # type: ignore[override]
        return None


def _parse_sse(lines: Iterator[str], max_events: int, started: float, budget: float) -> Iterator[dict[str, str]]:
    event: dict[str, str] = {}
    data: list[str] = []
    count = 0
    for raw in lines:
        if time.monotonic() - started > budget:
            break
        line = raw.rstrip("\r")
        if not line:
            if data or event:
                event["data"] = "\n".join(data)
                yield event
                count += 1
                event, data = {}, []
                if count >= max_events:
                    return
            continue
        if line.startswith(":"):
            continue
        field_name, _, value = line.partition(":")
        value = value.lstrip()
        if field_name == "data":
            data.append(value)
        else:
            event[field_name] = value
    if data or event:
        event["data"] = "\n".join(data)
        yield event


def _decode_body(payload: bytes, encoding: str) -> str:
    encoding = (encoding or "").lower()
    try:
        if "gzip" in encoding:
            payload = gzip.decompress(payload)
        elif "deflate" in encoding:
            payload = zlib.decompress(payload, -zlib.MAX_WBITS)
    except Exception:  # pragma: no cover - truncated body, keep what we have
        pass
    return payload.decode("utf-8", "replace")


def _describe(exc: Exception) -> str:
    if isinstance(exc, socket.timeout):
        return "timeout"
    name = type(exc).__name__
    detail = str(exc).strip()
    if "timeout" in name.lower() or "timed out" in detail.lower():
        return "timeout"
    return f"{name}: {detail}" if detail else name
