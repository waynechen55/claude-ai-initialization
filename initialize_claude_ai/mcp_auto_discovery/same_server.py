"""One server, one entry: telling copies of one server apart from different servers.

bugzilla.example.com ended a run with two entries, ``bugzilla-example-com`` and
``bugzilla-example-com-mcp-bugzilla``: the existing ``uvx mcp-bugzilla
--bugzilla-server https://bugzilla.example.com`` and an agent's proposal of the
same package with ``--read-only`` added. Every check compared whole command
lines, so one extra flag made them two servers -- and an ordinary run registers
every server that works.

A *copy* is the same thing launched with other options: the same package (read
with the launcher families of ``mcp.env_contract.package_sources``, versions
aside) reaching the same hosts, or the same URL on the same transport. Copies
are folded into the best of them in every mode; different servers -- another
package, another host, another path or transport -- are never folded, so how
many servers an address keeps stays what ``--exhaustive-mcp`` and
``--the-best-mcp`` decide.
"""

from __future__ import annotations

from typing import Any, Sequence

from .address import parse as parse_address
from .env_contract import EnvContract
from .grounding import hosts_named
from .types import Candidate, best_of


def enabled(settings: Any) -> bool:
    return settings.bool_("mcp.registration.fold_copies", True)


#: Fallback for ``mcp.registration.fold_script_extensions``.
SCRIPT_EXTENSIONS = (".py", ".js", ".mjs", ".cjs", ".ts", ".rb", ".sh")


def script_of(argv: Sequence[str], settings: Any) -> str:
    """The file name of the script an interpreter runs (``mcp_client_bridge_x.py``), or ``""``.

    A generated bridge is the same server wherever its file lives:
    build-opensuse-org's client bridge was registered twice in one ordinary
    run -- once from the project's ``bridges/`` directory, once from the run's
    own -- because the two paths made two command lines.
    """
    extensions = tuple(
        str(e).lower() for e in settings.str_list("mcp.registration.fold_script_extensions", list(SCRIPT_EXTENSIONS))
    )
    for token in list(argv)[1:]:
        name = str(token).replace("\\", "/").rsplit("/", 1)[-1].rstrip("}")
        if "/" in str(token) and name.lower().endswith(extensions):
            return name
    return ""


def key(candidate: Candidate, settings: Any) -> tuple:
    """What makes two candidates the same server; equal keys are copies."""
    if candidate.kind != "stdio":
        address = parse_address(candidate.url)
        url = f"{(address.scheme or '').lower()}://{(address.host or '').lower()}"
        url += f":{address.port}" if address.port else ""
        url += (address.path or "").rstrip("/")
        return ("url", url, candidate.transport)
    argv = candidate.full_command()
    hosts = frozenset(hosts_named([*argv, *candidate.env.values()]))
    package = EnvContract(settings).package_of(argv)
    if package:
        return ("package", package.lower(), hosts)
    script = script_of(argv, settings)
    if script and hosts:
        return ("script", script, hosts)
    return ("command", tuple(argv), hosts)


def fold(candidates: Sequence[Candidate], settings: Any) -> tuple[list[Candidate], list[tuple[Candidate, Candidate]]]:
    """``(one candidate per server, in the given order; [(copy, the one kept)])``."""
    if not enabled(settings) or len(candidates) < 2:
        return list(candidates), []
    groups: dict[tuple, list[Candidate]] = {}
    for candidate in candidates:
        groups.setdefault(key(candidate, settings), []).append(candidate)
    chosen = {id(best_of(group)): group for group in groups.values()}
    kept = [c for c in candidates if id(c) in chosen]
    folded = [(c, k) for k in kept for c in chosen[id(k)] if c is not k]
    for copy_, keeper in folded:
        inherit_credential(keeper, copy_, settings)
        carry_provenance(keeper, copy_)
    return kept, folded


def carry_provenance(keeper: Candidate, copy_: Candidate) -> None:
    """What tied a copy to the address -- how it was found -- is evidence about the server, so the kept copy carries it.

    developers-google-com (an existing registration: a claim) and the agent's
    copy of the same URL (named in developers.google.com's documentation) are
    one server; the copy kept is the working one, and every gate after the
    fold reads the documentation's word on it too.
    """
    from .grounding import VENDOR_PROVENANCE

    if copy_.source and copy_.source != keeper.source:
        keeper.add_note(f"the same server as {copy_.label}, which was found as: {copy_.source}")
    for note in copy_.notes:
        if any(marker in note.lower() for marker in VENDOR_PROVENANCE):
            keeper.add_note(note)


def inherit_credential(keeper: Candidate, copy_: Candidate, settings: Any) -> bool:
    """Give ``keeper`` the credential slots of a copy of the same server, when it has none of its own.

    developers-google-com's existing entry sent ``Authorization`` and worked;
    the agent's rediscovered copy of the same URL sent nothing and got 401.
    The kept copy carries the slots (references, never values) of the one
    that has them, so what is written is what worked.
    (``mcp.registration.inherit_credential_slots``.)
    """
    if not settings.bool_("mcp.registration.inherit_credential_slots", True):
        return False
    if keeper.verified or keeper.secret_env or any("${" in str(v) for v in keeper.headers.values()):
        return False
    if not (copy_.secret_env or any("${" in str(v) for v in copy_.headers.values())):
        return False
    keeper.headers = {**copy_.headers, **keeper.headers}
    keeper.secret_env = dict(copy_.secret_env)
    keeper.secret_formats = {**copy_.secret_formats, **keeper.secret_formats}
    keeper.in_use = {**copy_.in_use, **keeper.in_use}
    keeper.add_note(f"carries the credential slots of {copy_.label}, a copy of the same server")
    return True


__all__ = ["carry_provenance", "enabled", "fold", "inherit_credential", "key"]
