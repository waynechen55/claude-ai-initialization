"""The one answer to "which certificate authorities does this run trust?".

Before this existed the program had three answers and none of them agreed.
``httpx`` trusted certifi's bundle and nothing else; the ``urllib`` fallback
trusted the host's store; the MCP servers it registered were told about the
host's bundle through :mod:`..mcp_auto_discovery.trust`. On a machine whose
store lacked an internal authority, every one of them failed the same target
in a different way, and the report blamed the network: build.example.net was
recorded as "did not respond at all" when it had answered every request with a
certificate from ``Example CA 2023.1``.

Everything here is **additive**. The store is the host's own, plus certifi,
plus what the operator names (``--mcp-trust-ca``), plus what this run learned
about a target the operator named (``mcp.trust.pin_presented_chain``). Nothing
removes an authority, nothing turns verification off, and a host that
verified before verifies exactly the same way now.

Learning a chain is trust on first use, and it is kept deliberately narrow:

* only for an address given with ``--mcp-server`` (or ``mcp.servers``) --
  never for a host something else merely links to;
* only when verification failed because the issuer is unknown
  (``pin_verify_codes``), never for a wrong hostname or an expired certificate;
* only the certificates *above* the server's own, never the leaf, and nothing
  when the server sends its leaf alone;
* only if the host then verifies, hostname included, with them added.

Every chain learned is reported with its subjects and fingerprints, written to
``<store_dir>/anchors`` where the operator can read or delete it, and
``--mcp-trust-pin never`` turns learning off.
"""

from __future__ import annotations

import glob
import hashlib
import os
import re
import socket
import ssl
import tempfile
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence
from urllib.parse import urlsplit

from .config import Settings
from .locks import KeyedLocks
from .logging_setup import get_logger
from .paths_home import artifacts_home

log = get_logger("trust")

#: ``mcp.trust.pin_presented_chain`` / ``--mcp-trust-pin``.
PIN_MODES = ("targets", "never")

#: What became of one host.
TRUSTED = "trusted"  # verified with the host's own store, as before
ANCHORED = "anchored"  # verified only with what this run adds
UNTRUSTED = "untrusted"  # answered, with a certificate nothing here vouches for
UNREACHABLE = "unreachable"  # no TLS conversation happened at all

_STATE_KEY = "trust_store"
_CREATE = threading.Lock()
_PEM = re.compile(r"-----BEGIN CERTIFICATE-----.+?-----END CERTIFICATE-----", re.DOTALL)


@dataclass
class TrustFinding:
    """What one host's certificate said about this machine's trust store."""

    host: str
    port: int
    status: str
    detail: str
    #: The authority the server's certificate chains to, in words.
    issuer: str = ""
    #: The certificates this run added for it: subject, issuer, sha256.
    anchors: list[dict[str, Any]] = field(default_factory=list)
    #: True when the anchors were learned in this run rather than given or kept.
    learned: bool = False
    #: The OpenSSL verify code of the failure, when there was one.
    verify_code: int = 0

    @property
    def key(self) -> str:
        return f"{self.host}:{self.port}"

    def needs_root(self) -> bool:
        """True when no anchor is self-signed: a runtime without partial-chain support still fails."""
        return bool(self.anchors) and not any(a.get("self_signed") for a in self.anchors)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def pin_mode(settings: Settings) -> str:
    """The pinning mode in force; an unknown value learns nothing, loudly."""
    raw = settings.str_("mcp.trust.pin_presented_chain", "targets").strip().lower()
    if raw in PIN_MODES:
        return raw
    log.warning(
        "mcp.trust.pin_presented_chain = %r is not one of %s; learning nothing",
        raw,
        ", ".join(PIN_MODES),
    )
    return "never"


def system_bundle(settings: Settings) -> str:
    """The host's CA bundle, or ``""`` when none of the usual places has one.

    ``mcp.trust.bundle`` pins a file; otherwise the first of
    ``mcp.trust.bundle_candidates`` that exists wins. An empty candidate list
    turns detection off, which is the switch for a host where naming the bundle
    is the wrong thing to do.
    """
    configured = settings.str_("mcp.trust.bundle", "").strip()
    if configured:
        if Path(os.path.expanduser(configured)).is_file():
            return os.path.expanduser(configured)
        log.warning("mcp.trust.bundle names %r, which does not exist", configured)
        return ""
    for candidate in settings.str_list(
        "mcp.trust.bundle_candidates",
        [
            "/etc/ssl/ca-bundle.pem",
            "/var/lib/ca-certificates/ca-bundle.pem",
            "/etc/ssl/certs/ca-certificates.crt",
            "/etc/pki/tls/certs/ca-bundle.crt",
            "/etc/ssl/cert.pem",
        ],
    ):
        path = Path(os.path.expanduser(str(candidate)))
        if path.is_file():
            return str(path)
    return ""


def pem_blocks(text: str) -> list[str]:
    """Every PEM certificate in ``text``, normalised to end in one newline."""
    return [block.strip() + "\n" for block in _PEM.findall(text or "")]


def describe(pem: str) -> dict[str, Any]:
    """Subject, issuer and fingerprint of one PEM certificate, as far as this Python can tell.

    ``cryptography`` is used when it is installed; otherwise the standard
    library's decoder. Neither is required: the fingerprint is always there, and
    the names fall back to "" rather than failing the run.
    """
    der = ssl.PEM_cert_to_DER_cert(pem)
    out: dict[str, Any] = {"sha256": hashlib.sha256(der).hexdigest(), "subject": "", "issuer": ""}
    try:  # optional
        from cryptography import x509  # type: ignore[import-not-found]

        cert = x509.load_der_x509_certificate(der)
        out["subject"] = cert.subject.rfc4514_string()
        out["issuer"] = cert.issuer.rfc4514_string()
    except Exception:  # noqa: BLE001 - absent or unable; try the stdlib
        decoded = _stdlib_decode(pem)
        out["subject"] = _flatten(decoded.get("subject"))
        out["issuer"] = _flatten(decoded.get("issuer"))
    out["self_signed"] = bool(out["subject"]) and out["subject"] == out["issuer"]
    return out


def common_name(name: str) -> str:
    """``CN=...`` out of a distinguished name in either spelling; the whole name otherwise."""
    for part in re.split(r"(?<!\\),", name or ""):
        key, _, value = part.strip().partition("=")
        if key.strip().lower() in ("cn", "commonname"):
            return value.strip()
    return (name or "").strip()


class TrustStore:
    """The run's trust store. Get it with :meth:`for_run`."""

    def __init__(self, settings: Settings, *, runner: Any = None, dry_run: bool = False) -> None:
        self.settings = settings
        self.runner = runner
        self.dry_run = dry_run
        self._guard = threading.RLock()
        self._hosts = KeyedLocks()
        self._findings: dict[str, TrustFinding] = {}
        #: sha256 -> PEM, every anchor this run holds (kept on disk and learned).
        self._anchors: dict[str, str] = {}
        self._generation = 0
        self._contexts: dict[tuple[bool, int], ssl.SSLContext] = {}
        self._bundle_written: tuple[int, str] | None = None
        #: (host, port, file, mtime, extra files) -> verdict of a complete-chain check.
        self._complete: dict[tuple[Any, ...], bool | None] = {}
        self._hosts_complete = KeyedLocks()
        #: command -> the PEM file holding the store that runtime reports.
        self._runtime_stores: dict[tuple[str, ...], str] = {}
        #: host:port -> (its chain continued to a root, the certificates added above it).
        self._paths: dict[str, tuple[list[str], list[str]]] = {}
        self._load_kept()

    @classmethod
    def for_run(cls, ctx: Any) -> "TrustStore":
        """The one trust store for this run, created on first use."""
        with _CREATE:
            found = ctx.state.get(_STATE_KEY)
            if not isinstance(found, cls):
                found = ctx.state[_STATE_KEY] = cls(
                    ctx.settings, runner=getattr(ctx, "runner", None), dry_run=bool(ctx.dry_run)
                )
            return found

    # -- where things live ------------------------------------------------------

    @property
    def enabled(self) -> bool:
        return self.settings.bool_("mcp.trust.enabled", True)

    def store_dir(self) -> Path | None:
        raw = self.settings.str_("mcp.trust.store_dir", "").strip()
        return Path(os.path.expanduser(raw)) if raw else None

    def anchor_dir(self) -> Path | None:
        base = self.store_dir()
        sub = self.settings.str_("mcp.trust.anchor_subdir", "anchors").strip()
        return (base / sub if sub else base) if base else None

    def bundle_file(self) -> Path | None:
        base = self.store_dir()
        name = self.settings.str_("mcp.trust.bundle_filename", "ca-bundle.pem").strip()
        return base / name if base and name else None

    # -- what is trusted --------------------------------------------------------

    def extra_files(self) -> list[str]:
        """The operator's own PEM files (``--mcp-trust-ca``), the ones that exist."""
        found: list[str] = []
        for raw in self.settings.str_list("mcp.trust.extra_ca", []):
            for part in str(raw).split(","):
                path = os.path.expanduser(part.strip())
                if not path:
                    continue
                if Path(path).is_file():
                    found.append(path)
                else:
                    log.warning("mcp.trust.extra_ca names %r, which does not exist", path)
        return found

    def anchors(self) -> list[str]:
        with self._guard:
            return list(self._anchors.values())

    def adds_anything(self) -> bool:
        """True when this run trusts more than the host does."""
        return bool(self.enabled and (self.extra_files() or self.anchors()))

    def context(self) -> ssl.SSLContext:
        """The context this program's own requests verify with: the host's store and everything added."""
        return self._context(include_run=True)

    def _context(self, *, include_run: bool, extra_pems: Sequence[str] = ()) -> ssl.SSLContext:
        with self._guard:
            key = (include_run, self._generation)
            cached = None if extra_pems else self._contexts.get(key)
            if cached is not None:
                return cached
            context = ssl.create_default_context()
            if self.enabled:
                bundle = system_bundle(self.settings)
                if bundle:
                    _load(context, cafile=bundle)
                if self.settings.bool_("mcp.trust.include_certifi", True):
                    certifi_bundle = _certifi()
                    if certifi_bundle:
                        _load(context, cafile=certifi_bundle)
                if include_run:
                    for path in self.extra_files():
                        _load(context, cafile=path)
                    pems = list(self._anchors.values())
                    if pems:
                        _load(context, cadata="".join(pems))
                if extra_pems:
                    _load(context, cadata="".join(extra_pems))
                if self.settings.bool_("mcp.trust.partial_chain", True):
                    flag = getattr(ssl, "VERIFY_X509_PARTIAL_CHAIN", 0)
                    if flag:
                        context.verify_flags |= flag
            if not extra_pems:
                self._contexts[key] = context
            return context

    def bundle(self) -> str:
        """A PEM file holding everything :meth:`context` trusts, for a server to read; "" if none is needed.

        Written only when the run trusts more than the host does, and never in
        a dry run. One file serves every runtime: it holds the host's own
        bundle too, so a variable that *replaces* a store (``SSL_CERT_FILE``)
        narrows nothing, and one that adds to it (``NODE_EXTRA_CA_CERTS``)
        merely repeats what it had.
        """
        if self.dry_run or not self.adds_anything():
            return ""
        target = self.bundle_file()
        if target is None:
            return ""
        with self._guard:
            if self._bundle_written == (self._generation, str(target)) and target.is_file():
                return str(target)
            parts: list[str] = []
            base = system_bundle(self.settings) or (
                _certifi() if self.settings.bool_("mcp.trust.include_certifi", True) else ""
            )
            if base:
                parts.append(_read(base))
            for path in self.extra_files():
                parts.append(_read(path))
            parts.extend(self._anchors.values())
            text = "\n".join(p.strip() + "\n" for p in parts if p.strip())
            try:
                _write_atomically(target, text)
            except OSError as exc:
                log.warning("could not write the run's CA bundle to %s: %s", target, exc)
                return ""
            self._bundle_written = (self._generation, str(target))
            return str(target)

    # -- a bundle as a strict runtime reads it -----------------------------------

    def chain_complete(self, host: str, port: int, cafile: str, *, extra: Sequence[str] = ()) -> bool | None:
        """Does ``cafile`` carry ``host``'s chain all the way to a self-signed root?

        Asked the way the strictest runtime asks: partial chains off, so a
        trusted *intermediate* does not end the chain. Node verifies like that,
        and this program's own checks did not -- Python 3.13 and curl accept a
        chain that stops at any trusted certificate, so smelt.example.net's entry,
        whose ``NODE_EXTRA_CA_CERTS`` held "Example CA 2023.1" and "Example CA
        Root" but not the "Example Trust Root" above them, passed every check here
        and failed its first request with ``fetch failed``.

        ``extra`` adds files the runtime also trusts -- its own store, for a
        variable that adds to it (see :meth:`runtime_store`). ``None`` when no
        verdict was reached: the host did not answer, the file is not there, or
        the failure was not about the chain.
        """
        try:
            stat = os.stat(cafile)
        except OSError:
            return None
        extras = tuple(e for e in extra if e)
        key = (host, port, cafile, stat.st_mtime_ns, extras)
        with self._hosts_complete(repr(key)):
            with self._guard:
                if key in self._complete:
                    return self._complete[key]
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            for path in (cafile, *extras):
                _load(context, cafile=path)
            for name in ("VERIFY_X509_PARTIAL_CHAIN", "VERIFY_X509_STRICT"):
                flag = getattr(ssl, name, 0)
                if flag:
                    context.verify_flags &= ~flag
            timeout = self.settings.float_("mcp.trust.probe_timeout_seconds", 8)
            outcome, _code, _message = _handshake(host, port, context, timeout)
            verdict = True if outcome == "ok" else False if outcome == "tls-issuer" else None
            with self._guard:
                self._complete[key] = verdict
            return verdict

    def runtime_store(self, command: Sequence[str]) -> str:
        """A PEM file holding the store a runtime reports through ``command``; "" if it cannot.

        Asked of the runtime itself, because nothing else knows it. Node ships
        its own roots and ignores the host's; Python's ``certifi`` is, on some
        distributions, the host's store under another name -- so standing in
        for Node's store with certifi's called a bundle complete that Node
        rejects. Once per command for the run, in a temporary file.
        """
        argv = tuple(str(a) for a in command if str(a))
        if not argv:
            return ""
        with self._guard:
            if argv in self._runtime_stores:
                return self._runtime_stores[argv]
        path = ""
        if self.runner is not None and self.runner.which(argv[0]):
            result = self.runner.probe(list(argv), timeout=self.settings.float_("mcp.trust.probe_timeout_seconds", 8))
            pems = pem_blocks(result.stdout or "") if getattr(result, "ok", False) else []
            if pems:
                handle, name = tempfile.mkstemp(prefix="runtime-store-", suffix=".pem")
                with os.fdopen(handle, "w", encoding="utf-8") as out:
                    out.write("\n".join(pems) + "\n")
                path = name
        with self._guard:
            self._runtime_stores[argv] = path
        return path

    # -- completing a chain to its root -------------------------------------------

    def issuer_pool(self) -> list[str]:
        """Every certificate this machine offers as an issuer: the host's store, what this run adds,
        the anchor folders (``mcp.trust.issuer_sources``) and the CA files MCP entries here already name."""
        texts: list[str] = []
        base = system_bundle(self.settings)
        if base:
            texts.append(_read(base))
        texts += [_read(p) for p in self.extra_files()]
        with self._guard:
            texts += list(self._anchors.values())
        patterns = list(self.settings.str_list("mcp.trust.issuer_sources", list(ISSUER_SOURCES)))
        if self.settings.bool_("mcp.trust.issuers_from_program_dirs", True):
            # The copies this program keeps of what entries used -- a lost CA
            # file's root survives there: smelt.example.net's "Example Trust Root" was
            # only in a bundle deleted with the scratch folder.
            state = self.settings.str_("paths.state_dir", "").strip()
            for folder in (
                os.path.join(state, self.settings.str_("mcp.known_good.files_dir", "mcp-known-good-files"))
                if state
                else "",
                str(artifacts_home(self.settings) or ""),
            ):
                if folder:
                    patterns += [os.path.join(folder, "**", "*.pem"), os.path.join(folder, "**", "*.crt")]
        for pattern in patterns:
            for path in sorted(glob.glob(os.path.expanduser(pattern), recursive=True)):
                if os.path.isfile(path):
                    texts.append(_read(path))
        if self.settings.bool_("mcp.trust.issuers_from_entries", True):
            texts += [_read(p) for p in _entry_ca_files()]
        pool: dict[str, str] = {}
        for text in texts:
            for pem in pem_blocks(text):
                try:
                    pool.setdefault(hashlib.sha256(ssl.PEM_cert_to_DER_cert(pem)).hexdigest(), pem)
                except ValueError:
                    continue
        return list(pool.values())

    def chain_to_root(self, host: str, port: int) -> tuple[list[str], list[str]]:
        """``(path, added)``: the chain ``host`` presents, continued by issuer up to a self-signed root
        where this machine has one; ``added`` are the certificates above what it presents."""
        key = f"{host}:{port}"
        with self._guard:
            known = self._paths.get(key)
        if known is not None:
            return known
        timeout = self.settings.float_("mcp.trust.probe_timeout_seconds", 8)
        chain = self._presented_chain(host, port, timeout)
        path, added = list(chain), []
        if chain:
            pool = [(pem, describe(pem)) for pem in self.issuer_pool()]
            seen = {describe(pem)["sha256"] for pem in chain}
            current = describe(chain[-1])
            for _ in range(max(1, self.settings.int_("mcp.trust.max_chain_depth", 6))):
                if current.get("self_signed") or not current.get("issuer"):
                    break
                found = [(pem, d) for pem, d in pool if d["subject"] == current["issuer"] and d["sha256"] not in seen]
                if not found:
                    break
                found.sort(key=lambda pair: (not pair[1].get("self_signed"),))
                pem, current = found[0]
                seen.add(current["sha256"])
                path.append(pem)
                added.append(pem)
        with self._guard:
            self._paths[key] = (path, added)
        return path, added

    def complete_to_root(self, host: str, port: int) -> list[str]:
        """Trust, for this run, the issuers above what ``host`` presents up to a self-signed root; their names.

        The run's bundle carried smelt.example.net's chain only to a cross-signed
        "Example CA Root": Python accepts that partial chain, Node does not, and a
        Node server failed every request. The root it needed was in a CA file an
        MCP entry on this machine already named. Kept only when the chain then
        verifies strictly -- the way the strictest runtime reads it.
        (``mcp.trust.complete_to_root``.)
        """
        if not self.enabled or not self.settings.bool_("mcp.trust.complete_to_root", True):
            return []
        path, added = self.chain_to_root(host, port)
        if not added or not describe(path[-1]).get("self_signed"):
            return []
        with self._guard:
            fresh = [
                pem for pem in added if hashlib.sha256(ssl.PEM_cert_to_DER_cert(pem)).hexdigest() not in self._anchors
            ]
        if not fresh:
            return []
        self._keep(fresh)
        names = [common_name(describe(pem)["subject"]) for pem in fresh]
        log.info("%s:%s: completed its chain to a self-signed root with %s", host, port, ", ".join(names))
        return names

    def expiring(self, host: str, port: int, days: float) -> list[str]:
        """The certificates on ``host``'s chain to its root that expire within ``days``, as "name (date)"."""
        if days <= 0:
            return []
        path, _ = self.chain_to_root(host, port)
        limit = time.time() + days * 86400
        out: list[str] = []
        for pem in path[1:]:
            after = str(_stdlib_decode(pem).get("notAfter") or "")
            try:
                when = ssl.cert_time_to_seconds(after)
            except ValueError:
                continue
            if when < limit:
                out.append(f"{common_name(describe(pem)['subject'])} ({after})")
        return list(dict.fromkeys(out))

    def missing_root(self, host: str, port: int) -> str:
        """The authority above the last certificate ``host`` sends, in words; "" if unknown."""
        timeout = self.settings.float_("mcp.trust.probe_timeout_seconds", 8)
        chain = self._presented_chain(host, port, timeout)
        if not chain:
            return ""
        top = describe(chain[-1])
        if top.get("self_signed"):
            return ""
        return common_name(top.get("issuer", "")) or str(top.get("issuer", ""))

    # -- what one host said ------------------------------------------------------

    def finding(self, url_or_host: str) -> TrustFinding | None:
        host, port = _host_port(url_or_host)
        with self._guard:
            if port:
                return self._findings.get(f"{host}:{port}")
            return next((f for f in self._findings.values() if f.host == host), None)

    def findings(self) -> list[TrustFinding]:
        with self._guard:
            return list(self._findings.values())

    def anchored_hosts(self) -> list[str]:
        return sorted({f.host for f in self.findings() if f.status == ANCHORED})

    def bundle_for(self, texts: Iterable[str]) -> str:
        """The run bundle if any of ``texts`` names a host that needed it; "" otherwise.

        An entry reaches its target through an address somewhere in its
        command line or environment. Naming the run's bundle only where that
        address is a host that needed it keeps every other entry exactly as it
        was written before.
        """
        hosts = self.anchored_hosts()
        if not hosts:
            return ""
        blob = " ".join(str(t) for t in texts if t).lower()
        for host in hosts:
            if re.search(rf"(?<![\w.-]){re.escape(host)}(?![\w-])", blob):
                return self.bundle()
        return ""

    def examine(self, url: str, *, target: bool = True) -> TrustFinding | None:
        """Find out whether this run can verify ``url``'s certificate, learning a chain if allowed.

        Once per host and port for the run; a second caller waits for the first
        and takes its answer. Returns ``None`` for anything that is not https.
        """
        parts = urlsplit(str(url))
        if parts.scheme.lower() != "https" or not parts.hostname or not self.enabled:
            return None
        host = parts.hostname.lower()
        port = parts.port or 443
        key = f"{host}:{port}"
        with self._hosts(key):
            with self._guard:
                known = self._findings.get(key)
            if known is not None:
                return known
            found = self._examine(host, port, target=target)
            with self._guard:
                self._findings[key] = found
            self._announce(found)
            return found

    def _examine(self, host: str, port: int, *, target: bool) -> TrustFinding:
        timeout = self.settings.float_("mcp.trust.probe_timeout_seconds", 8)
        outcome, code, message = _handshake(host, port, self._context(include_run=False), timeout)
        if outcome == "ok":
            return TrustFinding(host, port, TRUSTED, "verified with this host's own trust store")
        if outcome == "net":
            return TrustFinding(host, port, UNREACHABLE, message)
        if outcome == "tls-other":
            return TrustFinding(host, port, UNTRUSTED, message)

        chain = self._presented_chain(host, port, timeout)
        issuer = describe(chain[0])["issuer"] if chain else ""
        if self.adds_anything():
            again, _, _ = _handshake(host, port, self._context(include_run=True), timeout)
            if again == "ok":
                return TrustFinding(
                    host,
                    port,
                    ANCHORED,
                    "verified with the authorities this run adds (--mcp-trust-ca or a chain kept from an earlier run)",
                    issuer=issuer,
                    anchors=self._matching(chain),
                    verify_code=code,
                )
        untrusted = TrustFinding(host, port, UNTRUSTED, message, issuer=issuer, verify_code=code)
        why_not = self._why_not_pin(target, code, chain)
        if why_not:
            untrusted.detail = f"{message}; not learned: {why_not}"
            return untrusted

        offered = chain[1:]
        again, _, after = _handshake(host, port, self._context(include_run=True, extra_pems=offered), timeout)
        if again != "ok":
            untrusted.detail = f"{message}; the chain it presents does not verify it either ({after})"
            return untrusted
        described = [describe(pem) for pem in offered]
        self._keep(offered)
        return TrustFinding(
            host,
            port,
            ANCHORED,
            "trusted on first use: the chain this target presents was added for this run",
            issuer=issuer,
            anchors=described,
            learned=True,
            verify_code=code,
        )

    def _why_not_pin(self, target: bool, code: int, chain: Sequence[str]) -> str:
        if pin_mode(self.settings) == "never":
            return "mcp.trust.pin_presented_chain = never"
        if not target:
            return "only an address given with --mcp-server is learned"
        codes = {
            int(c)
            for c in self.settings.str_list(
                "mcp.trust.pin_verify_codes",
                [
                    "2",
                    "19",
                    "20",
                ],
            )
            if c.strip().isdigit()
        }
        if code not in codes:
            return f"verify code {code} is not an unknown-issuer failure (mcp.trust.pin_verify_codes)"
        if len(chain) < 2:
            return "the server presents only its own certificate, and that is never trusted as an anchor"
        return ""

    def _matching(self, chain: Sequence[str]) -> list[dict[str, Any]]:
        with self._guard:
            kept = set(self._anchors)
        described = [describe(pem) for pem in chain[1:]]
        return [d for d in described if d["sha256"] in kept] or described

    # -- the chain a server presents ---------------------------------------------

    def _presented_chain(self, host: str, port: int, timeout: float) -> list[str]:
        """The certificates ``host`` sends, leaf first, as PEM; [] if they cannot be read."""
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        try:
            with socket.create_connection((host, port), timeout=timeout) as raw:
                with context.wrap_socket(raw, server_hostname=host) as tls:
                    getter = getattr(tls, "get_unverified_chain", None)
                    if getter is not None:
                        return [_as_pem(item) for item in getter() or ()]
        except (OSError, ValueError) as exc:
            log.debug("reading the chain %s:%s presents failed: %s", host, port, exc)
            return []
        return self._chain_by_command(host, port, timeout)

    def _chain_by_command(self, host: str, port: int, timeout: float) -> list[str]:
        """Before Python 3.13 the chain is read with ``mcp.trust.chain_command``, if it is installed."""
        template = self.settings.str_list(
            "mcp.trust.chain_command",
            [
                "openssl",
                "s_client",
                "-showcerts",
                "-connect",
                "{host}:{port}",
                "-servername",
                "{host}",
            ],
        )
        if not template or self.runner is None:
            return []
        argv = [str(p).replace("{host}", host).replace("{port}", str(port)) for p in template]
        if not self.runner.which(argv[0]):
            return []
        result = self.runner.probe(argv, timeout=timeout, stdin_text="")
        return pem_blocks(result.stdout or "")

    # -- keeping what was learned ---------------------------------------------------

    def _load_kept(self) -> None:
        directory = self.anchor_dir()
        if directory is None or not directory.is_dir():
            return
        for path in sorted(directory.glob("*.pem")):
            for pem in pem_blocks(_read(str(path))):
                try:
                    digest = hashlib.sha256(ssl.PEM_cert_to_DER_cert(pem)).hexdigest()
                except ValueError:
                    continue
                self._anchors[digest] = pem
        if self._anchors:
            log.debug("trusting %d certificate(s) kept in %s", len(self._anchors), directory)

    def _keep(self, pems: Sequence[str]) -> None:
        directory = self.anchor_dir()
        with self._guard:
            for pem in pems:
                digest = hashlib.sha256(ssl.PEM_cert_to_DER_cert(pem)).hexdigest()
                if digest in self._anchors:
                    continue
                self._anchors[digest] = pem
                if directory is not None and not self.dry_run:
                    try:
                        _write_atomically(directory / f"{digest}.pem", pem)
                    except OSError as exc:
                        log.warning("could not keep %s in %s: %s", digest, directory, exc)
            self._generation += 1

    def _announce(self, found: TrustFinding) -> None:
        if found.status == ANCHORED and found.learned:
            log.warning(
                "%s: trusting the chain it presents (%s) -- review %s",
                found.key,
                "; ".join(f"{common_name(a['subject']) or '?'} sha256={a['sha256']}" for a in found.anchors),
                self.anchor_dir() or "the report",
            )
        elif found.status in (ANCHORED, UNTRUSTED):
            log.info("%s: %s (%s)", found.key, found.status, found.detail)


def brief(found: TrustFinding | None, bundle: str, variables: Iterable[str] = ()) -> str:
    """One sentence about a host's certificate for someone about to write an entry; "" if unaffected.

    ``variables`` are the environment variables runtimes read a trust store
    from (``mcp.trust.runtimes``), named so the agent uses the right one.
    """
    if found is None or found.status not in (ANCHORED, UNTRUSTED):
        return ""
    issuer = common_name(found.issuer) or "an authority"
    if found.status == ANCHORED and bundle:
        names = list(dict.fromkeys(v for v in variables if v))
        text = (
            f"{found.host} presents a certificate from {issuer}, which this host's trust store does "
            f"not hold; initialize-claude-ai verifies it with {bundle}. Name that file in the entry's "
            "env under the variable its runtime reads"
            + (f" ({', '.join(names)})" if names else "")
            + " rather than extracting a chain of your own."
        )
        if found.needs_root():
            text += (
                f" It holds the chain above {found.host}'s certificate but not a self-signed root, "
                "which Python accepts and Node does not. Every bundle an entry names is checked "
                "the way Node reads it and replaced if it falls short, and refused if nothing on "
                "this machine carries the root -- so if you name a bundle, say which root it ends at."
            )
        return text
    return (
        f"{found.host} presents a certificate from {issuer} that nothing on this machine vouches for "
        f"({found.detail}). A server launched here will fail TLS against it until the operator passes "
        "the authority with --mcp-trust-ca; report that rather than disabling verification."
    )


# ----------------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------------


#: Fallback for ``mcp.trust.issuer_sources``: the folders distributions keep local anchors in.
ISSUER_SOURCES = (
    "/usr/share/pki/trust/anchors/*",
    "/etc/pki/trust/anchors/*",
    "/etc/pki/ca-trust/source/anchors/*",
    "/usr/local/share/ca-certificates/*",
)

_CA_FILE = re.compile(r"(/[^\s\"'$}]+\.(?:pem|crt|cer))")
_DEFAULTED = re.compile(r"\$\{[A-Za-z_][A-Za-z0-9_]*:-([^}]*)\}")


def _entry_ca_files() -> list[str]:
    """CA files named by the MCP entries this machine has (the project's .mcp.json, Claude Code's config)."""
    import json as _json

    sources = [Path.cwd() / ".mcp.json"]
    config = Path(os.environ.get("CLAUDE_CONFIG_DIR") or str(Path.home())) / ".claude.json"
    sources.append(config)
    try:
        projects = _json.loads(config.read_text(encoding="utf-8")).get("projects") or {}
        sources += [Path(str(p)) / ".mcp.json" for p in projects]
    except (OSError, ValueError, AttributeError):
        pass
    found: list[str] = []
    for source in dict.fromkeys(sources):
        try:
            text = source.read_text(encoding="utf-8")
        except OSError:
            continue
        text = _DEFAULTED.sub(lambda m: m.group(1), text)
        for match in _CA_FILE.finditer(text):
            path = os.path.expandvars(match.group(1))
            if os.path.isfile(path) and path not in found:
                found.append(path)
    return found


def _handshake(host: str, port: int, context: ssl.SSLContext, timeout: float) -> tuple[str, int, str]:
    """``("ok"|"tls-issuer"|"tls-other"|"net", verify_code, message)`` for one TLS handshake."""
    try:
        with socket.create_connection((host, port), timeout=timeout) as raw:
            with context.wrap_socket(raw, server_hostname=host):
                return "ok", 0, ""
    except ssl.SSLCertVerificationError as exc:
        code = int(getattr(exc, "verify_code", 0) or 0)
        message = str(getattr(exc, "verify_message", "") or exc)
        return "tls-issuer", code, f"certificate verify failed: {message}"
    except ssl.SSLError as exc:
        return "tls-other", 0, f"TLS failed: {exc}"
    except (OSError, ValueError) as exc:
        return "net", 0, f"{type(exc).__name__}: {exc}"


def _as_pem(item: Any) -> str:
    if isinstance(item, (bytes, bytearray)):
        return ssl.DER_cert_to_PEM_cert(bytes(item))
    public = getattr(item, "public_bytes", None)
    if public is not None:
        data = public(ssl.ENCODING_PEM)
        return data.decode("ascii") if isinstance(data, bytes) else str(data)
    return str(item)


def _load(context: ssl.SSLContext, **kwargs: Any) -> None:
    try:
        context.load_verify_locations(**kwargs)
    except (OSError, ssl.SSLError, ValueError) as exc:
        log.warning("could not load %s: %s", kwargs.get("cafile") or "certificates", exc)


def _certifi() -> str:
    try:  # optional
        import certifi  # type: ignore[import-not-found]

        return str(certifi.where())
    except Exception:  # noqa: BLE001
        return ""


def _read(path: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _write_atomically(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as out:
            out.write(text)
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _stdlib_decode(pem: str) -> dict[str, Any]:
    decoder = getattr(getattr(ssl, "_ssl", None), "_test_decode_cert", None)
    if decoder is None:
        return {}
    handle, temporary = tempfile.mkstemp(suffix=".pem")
    try:
        with os.fdopen(handle, "w", encoding="ascii") as out:
            out.write(pem)
        return dict(decoder(temporary) or {})
    except Exception:  # noqa: BLE001 - a private helper; its absence costs names only
        return {}
    finally:
        try:
            os.unlink(temporary)
        except OSError:
            pass


def _flatten(name: Any) -> str:
    """The stdlib's nested tuples as ``CN=...,O=...``."""
    short = {
        "commonName": "CN",
        "organizationName": "O",
        "organizationalUnitName": "OU",
        "countryName": "C",
        "localityName": "L",
        "stateOrProvinceName": "ST",
    }
    parts: list[str] = []
    for rdn in name or ():
        for key, value in rdn:
            parts.append(f"{short.get(key, key)}={value}")
    return ",".join(reversed(parts))


def _host_port(text: str) -> tuple[str, int]:
    raw = str(text or "").strip()
    if "://" in raw:
        parts = urlsplit(raw)
        return (parts.hostname or "").lower(), parts.port or (443 if parts.scheme == "https" else 0)
    return raw.lower(), 0


__all__ = [
    "ANCHORED",
    "PIN_MODES",
    "TRUSTED",
    "UNREACHABLE",
    "UNTRUSTED",
    "TrustFinding",
    "TrustStore",
    "brief",
    "common_name",
    "describe",
    "pem_blocks",
    "pin_mode",
    "system_bundle",
]
