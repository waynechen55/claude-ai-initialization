"""What this host trusts, and how to tell a server about it.

A target whose certificate is signed by an internal authority is the ordinary
case on a corporate network, and it breaks MCP servers in a way that looks like
nothing to do with TLS.

``build.example.net`` is the worked example. Its certificate comes from
``CN=Example CA 2023.1``, which the host trusts -- ``curl`` fetches the
OpenAPI spec with ``HTTP 200``. Node ships **its own** certificate store and
does not, so ``@ivotoby/openapi-mcp-server`` failed to load the spec at
start-up, exited, and Claude Code reported::

    build-example-net . failed . Connection closed

Nothing in that mentions a certificate. The entry looked perfect, the token in
it was valid, and the variable everyone suspected -- ``API_HEADERS`` -- was not
involved at all.

The fix is one environment variable, and the program already knew the argument
for it: :meth:`BridgeBuilder.ca_bundle` has named the system bundle for its own
generated bridges from the beginning, reasoning that doing so "never weakens
verification: it is the set of authorities this host has been told to trust,
which is a superset of a vendored one". That was right, and it was wired to
exactly one thing this program writes. Every *other* server it registers --
every published package, which is most of them -- got nothing.

So the knowledge moves here, where both pipelines and both kinds of entry can
reach it, and the missing half is added: which variable a given runtime reads.

Three things follow from the evidence, all measured rather than assumed:

* ``NODE_EXTRA_CA_CERTS`` is *additive*. It cannot remove trust, only add, so
  naming the host's bundle is safe on any machine.
* An **empty** value is inert -- Node behaves exactly as if it were unset.
* An **unexpanded** ``${NODE_EXTRA_CA_CERTS}`` prints one warning to stderr and
  carries on. It does not kill the server, and stderr is not the protocol
  channel.

That last point is why the variable is written even where no bundle was found:
the operator gets a lever they can see in the file, and the worst case is a
warning. Whatever is written, the entry is then launched exactly as written by
:meth:`.CandidateVerifier.verify_as_written`, so the claim is checked rather
than argued.

What this is *not* is a way to turn verification off. Variables that do that
are stripped from every entry -- see ``mcp.registration.tls_weakening_env``.
Pointing a server at the host's trust store and telling it to stop checking are
opposite acts, and only one of them is here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import urlsplit

from ..core.config import Settings
from ..core.logging_setup import get_logger
from ..core.trust_store import system_bundle

log = get_logger("mcp.trust")

#: ``${VAR}`` / ``${VAR:-default}`` wrapping a command, so the runtime can be
#: recognised through the portability rewriting that has already happened.
_WRAPPED = ("${", "}")


def runtime_of(command: str) -> str:
    """The launcher's bare name, seen through any ``${VAR:-/path}`` wrapping.

    By the time an entry is written its command may be
    ``${ICA_MCP_UVX:-/home/linuxbrew/.linuxbrew/bin/uvx}``. The runtime is
    still ``uvx``, and a lookup that could not see that would quietly apply to
    nothing -- the same class of miss as matching a pattern instead of asking
    the rule.
    """
    text = str(command or "").strip()
    if text.startswith(_WRAPPED[0]) and text.endswith(_WRAPPED[1]):
        inner = text[2:-1]
        default = inner.partition(":-")[2]
        if default:
            text = default
        else:
            # No default to read a path out of, so the variable's own name is
            # what is left. This program chose that name from
            # `launcher_variable_template`, so `ICA_MCP_NPX` still says `npx` --
            # and a lookup that gave up here would silently apply to nothing.
            text = inner.rpartition("_")[2] or inner
    return PurePosixPath(text).name.lower()


def variables_for(command: str, settings: Settings) -> list[str]:
    """Trust-store variables the runtime behind ``command`` reads.

    Configuration, not a list in code: ``[mcp.trust.runtimes]`` maps a launcher
    name to the variables it honours, so a runtime nobody has met yet is a line
    of TOML rather than a patch.
    """
    table = settings.dict_("mcp.trust.runtimes", {})
    raw = table.get(runtime_of(command))
    if isinstance(raw, str):
        return [raw] if raw.strip() else []
    if isinstance(raw, Sequence):
        return [str(v).strip() for v in raw if str(v).strip()]
    return []


def env_for(
    command: str,
    settings: Settings,
    existing: Mapping[str, Any] | None = None,
    *,
    bundle: str = "",
) -> dict[str, str]:
    """``{VARIABLE: "${VARIABLE:-<bundle>}"}`` for one entry's command.

    Anything already present in ``existing`` is left alone: discovery may have
    found the right answer for this product, and it is better informed than a
    table keyed on the launcher's name.

    With no bundle found the variable is still written, as a bare ``${VAR}``.
    That is deliberate. Silence here is what cost three days on one server: the
    operator could not see that a trust store was the missing piece, because
    nothing in the entry mentioned one. An unexpanded value costs a warning on
    stderr and nothing else -- measured, not assumed.

    ``bundle`` names a different file -- the run's own, for an entry that
    reaches a host whose certificate the host bundle does not cover (see
    :meth:`.TrustStore.bundle_for`). The form is the same, so the operator's
    own value still wins at launch.
    """
    if not settings.bool_("mcp.trust.enabled", True):
        return {}
    wanted = variables_for(command, settings)
    if not wanted:
        return {}
    bundle = bundle or system_bundle(settings)
    have = {str(k) for k in (existing or {})}
    out: dict[str, str] = {}
    for name in wanted:
        if name in have:
            continue
        out[name] = f"${{{name}:-{bundle}}}" if bundle else f"${{{name}}}"
    return out


def launch_env_for(
    command: str, settings: Settings, bundle: str, existing: Mapping[str, Any] | None = None
) -> dict[str, str]:
    """The same variables with the value itself, for a trial launched here.

    A trial is only worth anything if it is the launch that ships, and the
    entry will name ``bundle``; so the trial names it too.
    """
    if not bundle or not settings.bool_("mcp.trust.enabled", True):
        return {}
    have = {str(k) for k in (existing or {})}
    return {name: bundle for name in variables_for(command, settings) if name not in have}


#: An https URL anywhere in a command line or an environment value.
_HTTPS_URL = re.compile(r"https://[^\s'\"<>|,;]+", re.IGNORECASE)


def trust_variables(settings: Settings) -> set[str]:
    """Every trust-store variable any configured runtime reads (``[mcp.trust.runtimes]``)."""
    names: set[str] = set()
    for raw in settings.dict_("mcp.trust.runtimes", {}).values():
        values = [raw] if isinstance(raw, str) else raw if isinstance(raw, Sequence) else []
        names.update(str(v).strip() for v in values if str(v).strip())
    return names


def https_hosts(texts: Iterable[str]) -> list[tuple[str, int]]:
    """``(host, port)`` for every https URL in ``texts``, in order, once each.

    Only https: a certificate is only checked by a server that speaks TLS, and
    the address it speaks it to is a URL in its launch -- ``ENDPOINT``,
    ``--bugzilla-server``, ``JIRA_URL``.
    """
    out: list[tuple[str, int]] = []
    for text in texts:
        for match in _HTTPS_URL.finditer(str(text or "")):
            try:
                parts = urlsplit(match.group(0))
                host, port = (parts.hostname or "").lower(), parts.port or 443
            except ValueError:
                continue
            if host and "$" not in host and (host, port) not in out:
                out.append((host, port))
    return out


@dataclass
class BundleCheck:
    """What checking the trust-store files an entry names found."""

    #: Variable -> the bundle that carries the chain, for one that did not.
    replaced: dict[str, str] = field(default_factory=dict)
    #: One sentence per variable that was replaced, or could not be.
    notes: list[str] = field(default_factory=list)
    #: Why the entry cannot reach its host, for a variable whose runtime needs a
    #: self-signed root (``mcp.trust.root_required_variables``) and no bundle
    #: here carries one. Empty when nothing is wrong.
    unfixable: list[str] = field(default_factory=list)
    #: Certificates on a reached host's chain that expire soon (``mcp.trust.expiry_warning_days``).
    expiring: list[str] = field(default_factory=list)


def check_named_bundles(
    env: Mapping[str, Any],
    hosts: Sequence[tuple[str, int]],
    settings: Settings,
    store: Any,
) -> BundleCheck:
    """Check every trust-store file ``env`` names against the hosts it must reach.

    The file is the one the runtime will read, so it is judged the way the
    strictest runtime reads it (:meth:`.TrustStore.chain_complete`): the chain
    must end at a self-signed root inside it. An entry carried such a file in
    from somewhere this program never checked -- a reused registration, an
    agent's proposal, ``--mcp-trust-ca`` -- and :func:`env_for` deliberately
    leaves a value it finds alone. smelt.example.net's ``NODE_EXTRA_CA_CERTS`` named
    a bundle without the root of its chain; the server started, handshook and
    listed its tools, and failed every request.

    A file that falls short is replaced by the first of the host's bundle and
    this run's combined bundle that carries the chain -- both hold the host's
    store, which is where such a root usually lives. When neither does, a
    variable in ``mcp.trust.root_required_variables`` makes the entry
    unusable and the reason is returned; any other is noted and left, since
    its runtime may accept the shorter chain.
    """
    out = BundleCheck()
    if not settings.bool_("mcp.trust.enabled", True) or not settings.bool_("mcp.trust.check_named_bundles", True):
        return out
    if not hosts or store is None:
        return out
    from .portability import expand

    days = settings.float_("mcp.trust.expiry_warning_days", 60)
    if days > 0 and hasattr(store, "expiring"):
        for host, port in hosts:
            soon = store.expiring(host, port, days)
            if soon:
                out.expiring.append(
                    f"{host}'s certificate chain expires soon: {', '.join(soon)} -- within "
                    f"{days:g} days every entry reaching it stops working until the new chain is trusted"
                )
    stores = settings.dict_("mcp.trust.runtime_stores", {})
    required = set(settings.str_list("mcp.trust.root_required_variables", ["NODE_EXTRA_CA_CERTS"]))
    known = trust_variables(settings)
    for name in sorted(str(k) for k in env):
        if name not in known:
            continue
        path = expand(str(env[name]))
        if not path or "${" in path:
            continue
        # A variable that adds to the runtime's own store is judged on the file
        # plus that store, asked of the runtime; one that replaces it, on the
        # file alone.
        command = stores.get(name)
        extra = [store.runtime_store(command)] if isinstance(command, Sequence) and not isinstance(command, str) else []
        failing = [(host, port) for host, port in hosts if store.chain_complete(host, port, path, extra=extra) is False]
        if failing and hasattr(store, "complete_to_root"):
            # The run's own bundle is completed up to a self-signed root first,
            # from issuers this machine already has -- then it can stand in.
            for host, port in failing:
                store.complete_to_root(host, port)
        if not failing:
            continue
        host, port = failing[0]
        alternatives = [b for b in dict.fromkeys([system_bundle(settings), store.bundle()]) if b and b != path]
        # A replacement has to serve every host the entry reaches, not only the
        # one that failed -- the file it replaces may be what another needs.
        replacement = next(
            (b for b in alternatives if all(store.chain_complete(h, p, b, extra=extra) is not False for h, p in hosts)),
            "",
        )
        root = store.missing_root(host, port)
        missing = f" (it lacks {root})" if root else ""
        if replacement:
            out.replaced[name] = replacement
            out.notes.append(
                f"{name} named {path}, which does not carry {host}'s certificate chain to a "
                f"self-signed root{missing}; a runtime that needs one -- Node does -- fails every "
                f"request with it. The entry names {replacement} instead, which does."
            )
            log.info("%s: %s replaced by %s for %s%s", name, path, replacement, host, missing)
        elif name in required:
            out.unfixable.append(
                f"{name} names {path}, which does not carry {host}'s certificate chain to a "
                f"self-signed root{missing}, and no bundle on this machine does -- the server "
                "would start and then fail every request. Pass the root with --mcp-trust-ca"
            )
        else:
            out.notes.append(
                f"{name} names {path}, which does not carry {host}'s certificate chain to a "
                f"self-signed root{missing}; a runtime that accepts a shorter chain still works, "
                "one that does not will fail. Pass the root with --mcp-trust-ca"
            )
    return out


__all__ = [
    "BundleCheck",
    "check_named_bundles",
    "env_for",
    "https_hosts",
    "launch_env_for",
    "runtime_of",
    "system_bundle",
    "trust_variables",
    "variables_for",
]
