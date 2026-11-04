"""Trust follows the runtime that opens the connection -- Claude Code itself, for a URL entry.

openqa-example-net's entry was a URL: Claude Code opens that connection itself, on
Node, which trusts its own roots, the system store and ``NODE_EXTRA_CA_CERTS``
-- not this program's bundle. The run verified openqa.example.net with a store
that held the internal "Example Trust Root" and wrote the entry; on a machine
whose system store lacked that root Claude Code showed it "Failed to connect --
UNABLE_TO_GET_ISSUER_CERT_LOCALLY". A stdio entry carries its trust in its own
``env``; a URL entry has none, so its trust must be in Claude Code's.

:func:`ensure` finds every https URL entry whose chain the consumer cannot
complete on its own (:func:`consumer_trusts`), keeps a bundle the consumer is
already given when it completes them, else writes one beside ``.mcp.json``
holding that bundle's certificates plus each chain up to its self-signed root,
and hands it to :class:`..core.machine_env.MachineEnv`, which brings every
place a new terminal's Claude Code reads to it and logs what it did. A value
naming a missing file, or a bundle without the root, is "wrong" there
(:func:`problem`) and corrected. Nothing of it goes into ``.mcp.json``.
(``[mcp.trust.consumer]``.)
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Iterable, Mapping
from urllib.parse import urlsplit

from ..core.logging_setup import get_logger

log = get_logger("mcp.consumer_trust")

#: Fallbacks for ``[mcp.trust.consumer]``, equal to the packaged values.
VARIABLE = "NODE_EXTRA_CA_CERTS"
TRUST_VARIABLES = ("NODE_EXTRA_CA_CERTS", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE")
#: Empty: the command [mcp.trust.runtime_stores] gives for ``variable``.
STORE_COMMAND: tuple[str, ...] = ()
BUNDLE_DIR = "trust"
BUNDLE_FILENAME = "claude-code-ca.pem"


def config(settings: Any) -> Any:
    return settings.section("mcp.trust.consumer")


def url_hosts(entries: Mapping[str, Mapping[str, Any]]) -> list[tuple[str, str, int]]:
    """``(name, host, port)`` for every entry Claude Code connects to itself over https."""
    out: list[tuple[str, str, int]] = []
    for name, entry in entries.items():
        url = str((entry or {}).get("url") or "")
        if not url or (entry or {}).get("command"):
            continue
        parts = urlsplit(url)
        if parts.scheme.lower() == "https" and parts.hostname:
            out.append((str(name), parts.hostname.lower(), parts.port or 443))
    return out


def consumer_store(ctx: Any) -> list[str]:
    """The PEM files standing for what Claude Code trusts on its own: its runtime's roots, and the system store.

    Asked of the runtime where it can be (``store_command``), else the public
    roots this program carries; the system store too when Claude Code reads it
    (``reads_system_store``: changing ``SSL_CERT_FILE`` changes its verdict).
    """
    from ..core.trust_store import TrustStore, _certifi, system_bundle

    cfg = config(ctx.settings)
    store = TrustStore.for_run(ctx)
    command = [str(a) for a in cfg.list_("store_command", list(STORE_COMMAND)) if str(a)]
    if not command:
        stores = ctx.settings.dict_("mcp.trust.runtime_stores", {}) or {}
        command = [str(a) for a in stores.get(cfg.str_("variable", VARIABLE)) or [] if str(a)]
    files = [store.runtime_store(command)] if command else []
    if not any(files):
        files = [_certifi()]
    if cfg.bool_("reads_system_store", True):
        files.append(system_bundle(ctx.settings))
    return [f for f in files if f]


def consumer_trusts(ctx: Any, host: str, port: int, extra: str = "") -> bool | None:
    """Can Claude Code complete ``host``'s chain with its own stores (plus ``extra``)? None when unknown."""
    from ..core.trust_store import TrustStore

    files = consumer_store(ctx)
    if not files and not extra:
        return None
    first, rest = (extra, files) if extra else (files[0], files[1:])
    return TrustStore.for_run(ctx).chain_complete(host, port, first, extra=rest)


def problem(ctx: Any, value: str, needs: Iterable[tuple[str, str, int]]) -> str:
    """Why ``value`` (a CA file for Claude Code) does not do its job, in words; "" when it does."""
    from ..core.trust_store import TrustStore

    path = os.path.expanduser(str(value or "").strip())
    if not path:
        return "empty"
    if not os.path.isfile(path):
        return "a file that does not exist (Node ignores it with a warning)"
    store = TrustStore.for_run(ctx)
    lacking = [
        host
        for _name, host, port in needs
        if store.chain_complete(host, port, path, extra=consumer_store(ctx)) is False
    ]
    return f"a bundle without the root {', '.join(sorted(set(lacking)))} needs" if lacking else ""


def current_value(ctx: Any, variable: str) -> str:
    """What a new terminal's Claude Code is given now: its settings env, else what a fresh shell exports."""
    from .claude_check import _claude_settings_env, shell_values

    return _claude_settings_env(ctx).get(variable) or shell_values(ctx, [variable]).get(variable, "")


def bundle_path(ctx: Any) -> Path | None:
    """Where the consumer bundle lives: beside ``.mcp.json``, in the entries' own folder."""
    from ..core.paths_home import artifacts_home
    from ..core.trust_store import TrustStore

    cfg = config(ctx.settings)
    home = artifacts_home(ctx.settings) or TrustStore.for_run(ctx).store_dir()
    if home is None:
        return None
    return Path(home) / cfg.str_("bundle_dir", BUNDLE_DIR) / cfg.str_("bundle_filename", BUNDLE_FILENAME)


def build_bundle(ctx: Any, needs: Iterable[tuple[str, str, int]], keep: str = "") -> tuple[str, list[str]]:
    """``(path, roots)``: a bundle holding ``keep``'s certificates and each host's chain up to its root."""
    from ..core.fileio import write_private
    from ..core.trust_store import TrustStore, common_name, describe, pem_blocks

    store = TrustStore.for_run(ctx)
    target = bundle_path(ctx)
    if target is None:
        return "", []
    pems: dict[str, str] = {}
    if keep and os.path.isfile(os.path.expanduser(keep)):
        for pem in pem_blocks(Path(os.path.expanduser(keep)).read_text(encoding="utf-8", errors="replace")):
            pems.setdefault(describe(pem)["sha256"], pem)
    roots: list[str] = []
    for _name, host, port in needs:
        path, _added = store.chain_to_root(host, port)
        for pem in path[1:]:  # never the server's own certificate
            pems.setdefault(describe(pem)["sha256"], pem)
        if path and describe(path[-1]).get("self_signed"):
            roots.append(common_name(describe(path[-1])["subject"]))
    if not pems:
        return "", []
    if ctx.dry_run:
        return str(target), sorted(set(roots))
    target.parent.mkdir(parents=True, exist_ok=True)
    write_private(target, "".join(p.strip() + "\n" for p in pems.values()), mode=0o644)
    return str(target), sorted(set(roots))


#: Fallback for ``[mcp.trust.consumer] replacing_variables``: variables that
#: *replace* a runtime's whole store -- one naming a missing file leaves that
#: runtime trusting nothing. ~/.profile and /etc/profile.d set SSL_CERT_FILE and
#: REQUESTS_CA_BUNDLE to a deleted /tmp bundle.
REPLACING_VARIABLES = ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE")


def file_problem(value: str) -> str:
    """Why a CA file setting cannot work at all, in words; "" when the file holds certificates."""
    from ..core.trust_store import pem_blocks

    path = os.path.expanduser(str(value or "").strip())
    if not path:
        return ""
    if not os.path.isfile(path):
        return "a file that does not exist"
    try:
        if not pem_blocks(Path(path).read_text(encoding="utf-8", errors="replace")):
            return "a file holding no certificate"
    except OSError as exc:
        return f"a file that cannot be read ({exc.strerror or exc})"
    return ""


def repair_broken(ctx: Any, skip: Iterable[str] = ()) -> list[Any]:
    """Every CA variable a new terminal (or Claude Code's settings) sets to a file that cannot work, corrected.

    A store-replacing variable goes back to the system's own bundle -- what
    every runtime trusted before something broke it -- and an adding one
    (NODE_EXTRA_CA_CERTS) too when nothing needs more. Only variables set
    somewhere: none is added that nobody asked for.
    """
    from ..core.machine_env import MachineEnv
    from ..core.trust_store import system_bundle
    from .claude_check import _claude_settings_env, shell_values_each

    cfg = config(ctx.settings)
    if not cfg.bool_("repair_broken", True):
        return []
    names = [n for n in cfg.str_list("trust_variables", list(TRUST_VARIABLES)) if n and n not in set(skip)]
    # Every kind of new terminal (a login shell reads ~/.profile and
    # /etc/profile.d, a terminal window ~/.bashrc), and Claude Code's settings:
    # a value broken in any one of them is broken for a Claude Code started there.
    settings_env = {k: v for k, v in _claude_settings_env(ctx).items() if k in names}
    sources = [*shell_values_each(ctx, names).values(), settings_env]
    fixed = []
    base = system_bundle(ctx.settings)
    for name in names:
        value = next((src[name] for src in sources if src.get(name) and file_problem(src[name])), "")
        broken = file_problem(value)
        if not value or not broken or not base or file_problem(base):
            continue
        why = (
            f"{name} named {value}, {broken}: "
            + (
                "every runtime reading it trusted nothing"
                if name in cfg.str_list("replacing_variables", list(REPLACING_VARIABLES))
                else "Node ignores it with a warning"
            )
            + "; now the system's own bundle"
        )
        fixed.append(MachineEnv(ctx).apply(name, base, why, valid=file_problem))
    return fixed


def ensure(ctx: Any, entries: Mapping[str, Mapping[str, Any]], *, force: bool = False) -> Any:
    """Give Claude Code the trust its URL entries need, in every place it reads; the :class:`MachineValue`, or None.

    ``force``: Claude Code's own connection failed on trust, so its verdict
    stands whatever the stand-in stores said.
    """
    from ..core.machine_env import MachineEnv

    cfg = config(ctx.settings)
    if not cfg.bool_("enabled", True):
        return None
    hosts = url_hosts(entries)
    needs = [n for n in hosts if force or consumer_trusts(ctx, n[1], n[2]) is False]
    variable = cfg.str_("variable", VARIABLE)
    if not needs:
        repair_broken(ctx)
        return None
    repair_broken(ctx, skip=[variable])
    current = current_value(ctx, variable)
    named = ", ".join(sorted({f"{name} ({host})" for name, host, _port in needs}))
    if current and not problem(ctx, current, needs):
        value, roots = current, []
        why = f"lets Claude Code trust the certificate authority of {named}; the bundle it is given already holds it"
    else:
        value, roots = build_bundle(ctx, needs, keep=current)
        if not value or (not ctx.dry_run and problem(ctx, value, needs)):
            log.warning("%s: no certificate on this machine completes the chain Claude Code needs", named)
            console = getattr(ctx, "console", None)
            if console is not None:
                console.bullet(
                    f"{named}: Claude Code cannot verify its certificate, and no certificate on this "
                    "machine completes the chain -- give the issuing root with --mcp-trust-ca FILE and re-run "
                    "phase 4",
                    indent=2,
                )
            return None
        kept = f" plus what {current} held" if current and os.path.isfile(os.path.expanduser(current)) else ""
        why = (
            f"lets Claude Code trust the certificate authority of {named}"
            + (f" ({', '.join(roots)})" if roots else "")
            + kept
        )
    return MachineEnv(ctx).apply(variable, value, why, valid=lambda v: problem(ctx, v, needs))


__all__ = [
    "REPLACING_VARIABLES",
    "TRUST_VARIABLES",
    "VARIABLE",
    "build_bundle",
    "file_problem",
    "repair_broken",
    "consumer_store",
    "consumer_trusts",
    "current_value",
    "ensure",
    "problem",
    "url_hosts",
]
