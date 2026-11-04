"""What the operator typed, turned into a URL the rest of phase 4 can trust.

``--mcp-server`` takes addresses from a human, and humans write addresses the way
they say them: ``example.com``, ``www.example.com/mcp``, ``HTTPS://Example.com/``,
``localhost:3000``, ``[::1]:8080``, occasionally ``https:/example.com`` because the
second slash was lost on the way through a chat window.  Every one of those names
the same thing to the person typing it, and every one of them has to become a
single, exact base URL before stage 1 can start appending ``/mcp`` to it --
otherwise the candidate list is built on a guess and the whole search inherits it.

This module is that step, in two halves:

``parse``    pure text.  Fills in the scheme, lowercases and punycodes the host,
             drops the default port, drops the query, the fragment and anything
             that looks like a credential, and refuses what it cannot make sense
             of instead of passing it on.
``resolve``  one cheap request.  Text alone cannot tell you that ``example.com``
             is really ``https://www.example.com``, or that a host serves plain
             HTTP and nothing else -- only the server can, and it answers in a
             redirect.  So the canonical URL is *confirmed*, not assumed.

Resolution is an improvement, never a requirement: a host that does not answer at
the root is still discovered against exactly what was typed, because an MCP server
on ``:8080`` is perfectly normal on a machine whose port 443 is closed.

One asymmetry is deliberate.  When the address as given does not answer at all,
the other scheme is tried -- but http is only ever *added* for a host that is
plausibly local.  Upgrading http to https is free; quietly downgrading a public
address to cleartext because TLS happened to fail is not, so it does not happen.
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass, replace
from typing import Iterable
from urllib.parse import urlsplit

from ..core.config import Settings
from ..core.context import RunContext
from ..core.logging_setup import get_logger
from ..core.trust_store import UNTRUSTED, TrustStore

log = get_logger("mcp.address")

#: The transports discovery can probe over. Anything else is refused by name.
SUPPORTED_SCHEMES = ("https", "http")
#: Ports that are implied by the scheme, and so are noise in a canonical URL.
DEFAULT_PORTS = {"http": 80, "https": 443}
#: Commas, as documented; whitespace too, because a URL cannot contain one.
_SEPARATORS = re.compile(r"[,\s]+")
#: ``scheme://`` written correctly.
_SCHEME = re.compile(r"^(?P<scheme>[A-Za-z][A-Za-z0-9+.\-]*)://")
#: ``https:/host`` or ``https:host`` -- restricted to the two schemes we accept so
#: that ``localhost:3000`` is read as a port and not as a scheme called localhost.
_SLOPPY_SCHEME = re.compile(r"^(?P<scheme>https?):/{0,2}(?=[^/])", re.IGNORECASE)
#: A hostname once it is ASCII: letters, digits, hyphens and dots.
_HOSTNAME = re.compile(r"^(?!-)[A-Za-z0-9.\-]+$")
#: Wrappers a pasted address arrives inside.
_LEADING = "<\"'`"
_TRAILING = ">\"'`,;."

#: What a credential written into an address is replaced with before it is
#: echoed. Short and obviously not a value, so nobody copies it back out.
USERINFO_MASK = "***"
#: The ``user:password@`` of an authority, scheme optional because an address
#: arrives as often without one. Anchored at the start and applied once: a ``@``
#: later in the path or query is part of the path, not a credential. The
#: userinfo must be non-empty, so the scoped npm name ``@acme/mcp-server`` --
#: a perfectly ordinary ``--mcp-server`` value for the package stage -- is left
#: as the operator wrote it.
_USERINFO = re.compile(r"\A(?P<scheme>[A-Za-z][A-Za-z0-9+.\-]*://)?(?P<userinfo>[^/?#@]+)@")

#: Names that mean "this machine" no matter whose network it is.
DEFAULT_LOOPBACK_HOSTS: tuple[str, ...] = ("localhost", "localhost.localdomain", "::1")
#: Wildcard bind addresses. Typed as a target they mean the local machine.
DEFAULT_ANY_HOSTS: tuple[str, ...] = ("0.0.0.0", "::")
#: DNS zones that are local-only by standard or by overwhelming convention:
#: ``.local`` (mDNS, RFC 6762), ``.home.arpa`` (RFC 8375), and ``.internal`` and
#: ``.lan``, which are not reserved but are what everyone actually uses.
#:
#: This list is a *default*, not the definition. ``--mcp-server`` takes any
#: address, and a great many organisations run their internal zone under a name
#: no standard has ever heard of -- ``.corp``, ``.intra``, ``.svc.cluster.local``,
#: a real registered domain served split-horizon. Discovering an MCP server on
#: one of those and then marking it down for being "a public host on cleartext"
#: would be this program inventing a security finding out of its own ignorance,
#: so the operator can say what their local zones are.
DEFAULT_LOCAL_SUFFIXES: tuple[str, ...] = (".local", ".internal", ".lan", ".home.arpa")


@dataclass(frozen=True)
class LocalZones:
    """What counts as "our own network" for the host in front of us."""

    loopback_hosts: frozenset[str] = frozenset(DEFAULT_LOOPBACK_HOSTS)
    any_hosts: frozenset[str] = frozenset(DEFAULT_ANY_HOSTS)
    #: A tuple, because :meth:`str.endswith` takes one directly.
    suffixes: tuple[str, ...] = DEFAULT_LOCAL_SUFFIXES


#: Process-wide, and deliberately so.
#:
#: :attr:`ServerAddress.is_local` is a property on a frozen dataclass that four
#: unrelated modules parse for themselves, from call sites that have no
#: ``Settings`` in reach -- the ranker scoring a candidate, the prober deciding
#: whether a token may go on the wire. Threading configuration through all of
#: them to answer one yes/no question about the network would put the answer in
#: four places again, which is the bug this replaced. So it is configured once,
#: at startup, exactly like :func:`initialize_claude_ai.core.redact.configure`.
_ZONES = LocalZones()


def configure_local_zones(
    suffixes: Iterable[str] = (),
    *,
    loopback_hosts: Iterable[str] = (),
    any_hosts: Iterable[str] = (),
    replace_defaults: bool = False,
) -> LocalZones:
    """Teach this process which zones are on the operator's own network.

    Additive by default: an operator naming ``.corp`` wants ``.corp`` *as well
    as* loopback, not instead of it. ``replace_defaults`` is there for the rarer
    case of a host where one of the built-in conventions is wrong -- a ``.lan``
    that really is somebody else's public domain. It drops the suffix
    conventions only; loopback is this machine on anybody's network, and no
    configuration makes that untrue.
    """
    global _ZONES
    base = replace(_ZONES, suffixes=()) if replace_defaults else _ZONES
    extra = tuple(
        s if s.startswith(".") else f".{s}" for s in (str(x).strip().lower() for x in suffixes) if s and s != "."
    )
    _ZONES = LocalZones(
        loopback_hosts=base.loopback_hosts.union(str(h).strip().lower() for h in loopback_hosts if str(h).strip()),
        any_hosts=base.any_hosts.union(str(h).strip().lower() for h in any_hosts if str(h).strip()),
        # Deduplicated, because endswith() walks the tuple and a config file
        # merged from four layers can easily name the same zone twice.
        suffixes=tuple(dict.fromkeys(base.suffixes + extra)),
    )
    return _ZONES


def local_zones() -> LocalZones:
    """The zones in force, for the report and for tests."""
    return _ZONES


@dataclass(frozen=True)
class ServerAddress:
    """One ``--mcp-server`` value, before and after being made canonical."""

    #: Exactly what was typed, kept for the report so the operator can see the
    #: input their output came from.
    raw: str
    url: str = ""
    scheme: str = ""
    host: str = ""
    port: int | None = None
    path: str = ""
    #: Every correction made, in the order made, in words an operator can check.
    notes: tuple[str, ...] = ()
    #: Non-empty when the address could not be understood; ``url`` is then empty.
    error: str = ""
    #: True once a live response confirmed (or corrected) the origin.
    resolved: bool = False

    @property
    def ok(self) -> bool:
        return bool(self.url) and not self.error

    @property
    def netloc(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"{host}:{self.port}" if self.port else host

    @property
    def origin(self) -> str:
        return f"{self.scheme}://{self.netloc}" if self.host else ""

    @property
    def is_loopback(self) -> bool:
        return _is_loopback(self.host)

    @property
    def is_local(self) -> bool:
        return _is_local(self.host)

    def with_origin(self, scheme: str, netloc: str) -> "ServerAddress":
        """A copy pointing at another origin, keeping the supplied path."""
        rebuilt = _finalise(self.raw, scheme, netloc, self.path, list(self.notes))
        return replace(rebuilt, resolved=self.resolved)

    def describe(self) -> str:
        """One line for the report: the URL, and what had to change to get it."""
        if self.error:
            return f"{self.raw}: {self.error}"
        if self.url == self.raw:
            return self.url
        return f"{self.raw} -> {self.url}"

    def to_dict(self) -> dict[str, object]:
        return {
            "raw": self.raw,
            "url": self.url,
            "scheme": self.scheme,
            "host": self.host,
            "port": self.port,
            "path": self.path,
            "notes": list(self.notes),
            "resolved": self.resolved,
            "error": self.error,
        }


def split_servers(values: Iterable[str]) -> list[str]:
    """Flatten ``--mcp-server a,b --mcp-server c`` into ``[a, b, c]``.

    Repeats and separators are both accepted because both get used: a script
    builds one comma-separated string, a person repeats the flag. Duplicates are
    dropped with the order preserved, so the first mention decides the position.
    """
    out: list[str] = []
    for value in values or []:
        for part in _SEPARATORS.split(str(value or "")):
            cleaned = part.strip()
            if cleaned and cleaned not in out:
                out.append(cleaned)
    return out


def parse_all(values: Iterable[str]) -> list[ServerAddress]:
    """Split, parse, and drop addresses that turn out to be the same server."""
    addresses: list[ServerAddress] = []
    seen: set[str] = set()
    for raw in split_servers(values):
        address = parse(raw)
        # Two spellings of one server ("example.com", "https://example.com/")
        # would otherwise be discovered twice and registered twice.
        key = address.url or f"!{address.raw}"
        if key in seen:
            continue
        seen.add(key)
        addresses.append(address)
    return addresses


def parse(raw: str, *, default_scheme: str = "https") -> ServerAddress:
    """Turn one written address into a canonical base URL, or say why not."""
    text = _mask_userinfo(_unwrap(str(raw or "")))
    if not text:
        return ServerAddress(raw=str(raw or ""), error="empty address")

    notes: list[str] = []
    scheme, rest = _split_scheme(text)
    if scheme and scheme not in SUPPORTED_SCHEMES:
        return ServerAddress(
            raw=text,
            error=f"{scheme}:// is not something discovery can probe; use http or https",
        )
    if not scheme:
        # No scheme: assume the secure one, except where TLS is the exception
        # rather than the rule. A developer's server on localhost is http.
        scheme = "http" if _is_local(_host_hint(rest)) else default_scheme
        notes.append(f"no scheme given; assuming {scheme}://")
    elif text.lower().startswith(scheme) and not text[len(scheme) :].startswith("://"):
        notes.append(f"read as {scheme}://")

    rest = _bracket_ipv6(rest)
    try:
        parts = urlsplit(f"{scheme}://{rest}")
        port = parts.port
    except ValueError as exc:
        return ServerAddress(raw=text, error=f"not a usable address ({exc})")

    if parts.username or parts.password:
        # Never echoed, never carried forward: a credential in an address ends up
        # in the report, the log and the registered server entry. There is a
        # setting for this (mcp.discovery.auth.bearer_token_ref) that does not.
        notes.append("credentials in the address were dropped; use mcp.discovery.auth")

    host, error = _canonical_host(parts.hostname or "", notes)
    if error:
        return ServerAddress(raw=text, error=error)

    if port is not None and not 1 <= port <= 65535:
        return ServerAddress(raw=text, error=f"port {port} is outside 1-65535")
    if port is not None and port == DEFAULT_PORTS.get(scheme):
        notes.append(f"dropped the default port :{port}")
        port = None

    if parts.query:
        notes.append("dropped the query string; discovery appends its own paths")
    if parts.fragment:
        notes.append("dropped the fragment")

    netloc = f"[{host}]" if ":" in host else host
    if port:
        netloc = f"{netloc}:{port}"
    return _finalise(text, scheme, netloc, _canonical_path(parts.path, notes), notes)


class AddressResolver:
    """Confirms each address against the server before discovery builds on it.

    One request per address, at the address itself. A redirect is the server
    telling us its own canonical origin -- ``example.com`` to ``www.example.com``,
    http to https -- and taking it means stage 1 probes the real host instead of
    probing a redirect two hundred times.
    """

    def __init__(self, ctx: RunContext, config: Settings | None = None) -> None:
        self.ctx = ctx
        self.config = config if config is not None else ctx.settings.section("mcp.address")

    @property
    def enabled(self) -> bool:
        return self.config.bool_("resolve", True)

    def resolve(self, address: ServerAddress) -> ServerAddress:
        """The address, corrected by whatever the server says about itself."""
        if not address.ok or not self.enabled:
            return address

        timeout = self.config.float_("timeout_seconds", 6.0)
        attempts = [address.url, *self._alternates(address)]
        errors: list[str] = []
        with self.ctx.http(timeout=timeout) as client:
            for attempt in attempts:
                response = client.head(attempt)
                if response.status == 0 or response.status >= 400:
                    # A server may refuse HEAD and answer the same GET; and a GET
                    # is what a browser would have sent, so it is the fairer test.
                    response = client.get(attempt)
                if response.status == 0:
                    errors.append(f"{attempt} ({response.error or 'no response'})")
                    continue
                return self._adopt(address, response.final_url or response.url)

        certificate = TrustStore.for_run(self.ctx).finding(address.url)
        if certificate is not None and certificate.status == UNTRUSTED:
            # It did answer -- with a certificate nothing here vouches for. Saying
            # "no response" would send the operator to check the network.
            note = (
                f"{address.origin} presented a certificate not trusted here "
                f"({certificate.detail}); using the address as given"
            )
        else:
            note = "no response from " + ", ".join(errors) + "; using the address as given"
        log.debug("address %s not confirmed: %s", address.raw, "; ".join(errors))
        return replace(address, notes=(*address.notes, note))

    # -- helpers ---------------------------------------------------------------

    def _alternates(self, address: ServerAddress) -> list[str]:
        """The other scheme, when trying it is an upgrade or a local certainty."""
        if not self.config.bool_("try_alternate_scheme", True):
            return []
        if address.scheme == "http":
            return [f"https://{address.netloc}{address.path}"]
        if address.is_local:
            return [f"http://{address.netloc}{address.path}"]
        # A public https address that does not answer stays https. Falling back to
        # cleartext because TLS failed is how a probe ends up somewhere it should
        # not be, and an unreachable host is the likelier explanation anyway.
        return []

    def _adopt(self, address: ServerAddress, final_url: str) -> ServerAddress:
        parts = urlsplit(final_url)
        if not parts.hostname:
            return replace(address, resolved=True)

        moved = parse(f"{parts.scheme}://{parts.netloc}", default_scheme=address.scheme)
        if not moved.ok:
            return replace(address, resolved=True)

        scheme = moved.scheme
        if address.scheme == "https" and scheme == "http" and not moved.is_local:
            # Following a public redirect off TLS would send every subsequent
            # probe -- and any configured bearer token -- in cleartext. Take the
            # host it names, keep the transport it was reached on.
            scheme = "https"
        if scheme == address.scheme and moved.netloc == address.netloc:
            return replace(address, resolved=True)

        # The origin only. The path the operator supplied is theirs; a landing
        # page's own path is not a better base to append "/mcp" to.
        note = f"{address.origin} redirects to {scheme}://{moved.netloc}"
        confirmed = address.with_origin(scheme, moved.netloc)
        return replace(confirmed, notes=(*address.notes, note), resolved=True)


# ----------------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------------


def _mask_userinfo(text: str) -> str:
    """Hide a credential written into the address, before anything keeps it.

    ``--mcp-server`` takes any address, and an appliance behind basic auth is
    routinely handed round as ``https://admin:hunter2@box.corp/mcp``. The
    credential is dropped from the URL further down, but :attr:`ServerAddress.raw`
    is kept for the report so the operator can see what they typed -- and the
    report, the transcript and the log are all places a password must not reach.
    Masking here means no return path of :func:`parse`, including the early
    error ones, can carry it. The username stays: it is not the secret, and it
    is what makes the line recognisable as the address that was typed.
    """

    def hide(match: re.Match[str]) -> str:
        scheme, userinfo = match.group("scheme") or "", match.group("userinfo")
        name = userinfo.split(":", 1)[0] if ":" in userinfo else ""
        return f"{scheme}{name}:{USERINFO_MASK}@" if name else f"{scheme}{USERINFO_MASK}@"

    return _USERINFO.sub(hide, text, count=1)


def _unwrap(text: str) -> str:
    """Strip the brackets, quotes and trailing punctuation of a pasted address."""
    text = text.strip()
    while text and text[0] in _LEADING:
        text = text[1:].strip()
    while text and text[-1] in _TRAILING:
        text = text[:-1].strip()
    if text.startswith("//"):  # protocol-relative, as copied out of HTML
        text = text[2:]
    return text


def _split_scheme(text: str) -> tuple[str, str]:
    match = _SCHEME.match(text)
    if match:
        return match.group("scheme").lower(), text[match.end() :]
    match = _SLOPPY_SCHEME.match(text)
    if match:
        return match.group("scheme").lower(), text[match.end() :]
    return "", text


def _host_hint(rest: str) -> str:
    """The host part of a scheme-less address, enough to classify it."""
    authority = rest.split("/", 1)[0].split("@")[-1]
    if authority.startswith("["):
        return authority[1:].split("]", 1)[0]
    if authority.count(":") > 1:  # a bare IPv6 literal, not host:port
        return authority
    return authority.split(":", 1)[0].lower()


def _bracket_ipv6(rest: str) -> str:
    """``::1`` is a host; ``::1:8080`` is ambiguous, so only the plain form."""
    if rest.startswith("[") or "/" in rest or rest.count(":") < 2:
        return rest
    try:
        ipaddress.IPv6Address(rest)
    except ValueError:
        return rest
    return f"[{rest}]"


def _canonical_host(host: str, notes: list[str]) -> tuple[str, str]:
    """Lowercase, absolute-dot-free, ASCII. Returns ``(host, error)``."""
    host = host.strip().lower()
    if not host:
        return "", "no host in the address"
    if host.endswith(".") and len(host) > 1:
        host = host.rstrip(".")
        notes.append("dropped the trailing dot")
    if any(ord(char) > 127 for char in host):
        try:
            encoded = host.encode("idna").decode("ascii")
        except UnicodeError:
            return "", f"{host} is not a usable hostname"
        notes.append(f"encoded as {encoded}")
        host = encoded
    try:
        ipaddress.ip_address(host)
    except ValueError:
        if not _HOSTNAME.match(host) or ".." in host:
            return "", f"{host} is not a usable hostname"
    return host, ""


def _canonical_path(path: str, notes: list[str]) -> str:
    """Collapse repeated slashes, drop the trailing one, keep the rest."""
    collapsed = re.sub(r"/{2,}", "/", path or "").rstrip("/")
    if collapsed and collapsed != (path or "").rstrip("/"):
        notes.append(f"tidied the path to {collapsed}")
    return collapsed


def _finalise(raw: str, scheme: str, netloc: str, path: str, notes: list[str]) -> ServerAddress:
    parts = urlsplit(f"{scheme}://{netloc}")
    host = (parts.hostname or "").lower()
    return ServerAddress(
        raw=raw,
        url=f"{scheme}://{netloc}{path}",
        scheme=scheme,
        host=host,
        port=parts.port,
        path=path,
        notes=tuple(notes),
    )


def is_loopback(host: str) -> bool:
    """This machine, by name or by address."""
    if host.lower() in _ZONES.loopback_hosts:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def is_local(host: str) -> bool:
    """Loopback, link-local, private ranges, or a name on a local-only zone.

    Four decisions hang off this answer, and all four go the wrong way for a
    host wrongly called public: the bearer token is withheld from a cleartext
    probe, the cleartext and third-party-host penalties are applied to a
    perfectly ordinary internal endpoint, the developer ports are not probed,
    and a bare name defaults to ``https``.
    """
    if not host:
        return False
    host = host.lower()
    if is_loopback(host) or host in _ZONES.any_hosts:
        return True
    if host.endswith(_ZONES.suffixes):
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        # A single label with no domain -- "mcpbox", "buildserver" -- resolves
        # through a search domain or a hosts file, both of which are on-site by
        # definition. No public name looks like this.
        return "." not in host
    return address.is_private or address.is_link_local


# Backwards-compatible private aliases, still used inside this module.
_is_loopback = is_loopback
_is_local = is_local


__all__ = [
    "AddressResolver",
    "LocalZones",
    "ServerAddress",
    "SUPPORTED_SCHEMES",
    "configure_local_zones",
    "is_local",
    "is_loopback",
    "local_zones",
    "parse",
    "parse_all",
    "split_servers",
]
