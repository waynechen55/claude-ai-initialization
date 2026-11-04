"""Diagnose, repair, prove again -- until it works, or only the operator can act.

A check that fails used to be where a candidate's story ended: it was dropped,
and the address went on to the next tier or ended empty. But most failures this
week were not dead ends, they were *correctable*: a credential under a name the
server never reads (build-example-net's ``BUILD_EXAMPLE_NET_BASE64_OF_USER_PASSWORD``
beside mcp-openapi-proxy's ``EXTRA_HEADERS``), a header sent under the scheme
word as its name (``MCP_BRIDGE_AUTH_HEADER=Bearer``), a service whose login only
its client performs. Each has a fix the program can make itself.

So every candidate that did not do the job is **classified** (:func:`classify`,
one closed list of classes, read from the evidence its checks left), and each
class's **repairs** are tried in the order ``[mcp.repair.classes.<class>]``
gives. A repair produces a corrected *variant*; the variant is proved again by
every check -- handshake, login, usage contract -- and kept only if it passes.
A class whose only remedy is the operator's (an account to enable, a key to
register) is said so, precisely, and left for the checklist.

Repairs are code (each a small, named action); which applies to what, in what
order, is configuration.
"""

from __future__ import annotations

import copy
import re
from typing import Any, Callable, Mapping, Sequence

from ..core.logging_setup import get_logger
from . import contract, login_check
from .types import Candidate

log = get_logger("mcp.repair")

#: Fallbacks for ``[mcp.repair]``, equal to the packaged values.
DEFAULT_MAX_VARIANTS = 4
DEFAULT_CLASSES = {
    "unread_credential": ["move_to_read_variable"],
    "header_shape": ["fix_header_name"],
    "credential_not_sent": ["move_to_read_variable", "fix_header_name"],
    "client_only": ["client_bridge"],
    "no_account": ["operator"],
    "rejected": ["operator"],
    "trust_file": ["use_system_bundle"],
}


def classify(candidate: Candidate, plan: Mapping[str, Any] | None = None) -> list[str]:
    """The failure classes a candidate's checks show, most specific first."""
    shake = candidate.handshake
    if shake is None:
        return []
    if (
        shake.login == login_check.TEMPORARY
        or any(r.get("verdict") == login_check.TEMPORARY for r in shake.functional)
        or any(r.get("passed") is None and r.get("task") and contract.fits(r) for r in shake.contract)
    ):
        # "Try again later" is not a fault to repair.
        return []
    found: list[str] = []
    problems = list(shake.shape_problems or [])
    if any("is not a variable" in str(p.get("remedy", "")) for p in problems):
        found.append("unread_credential")
    if any(
        "header name" in str(p.get("remedy", "")) or "Authorization (value:" in str(p.get("remedy", ""))
        for p in problems
    ):
        found.append("header_shape")
    if shake.login_reason == login_check.SAME_AS_ANONYMOUS:
        found.append("credential_not_sent")
    if shake.login_reason == login_check.NO_ACCOUNT:
        found.append("no_account")
    if (plan or {}).get("client_only") and "MCP_CLIENT_MANIFEST" not in candidate.env:
        found.append("client_only")
    if (shake.stderr_cause or {}).get("class") == "trust" or _broken_trust_file(candidate):
        # Its own log said the trust store it was given is missing or wrong --
        # or its entry names one that is not here (copied from another machine):
        # a transport fault to repair, never the server's or the login's.
        found.insert(0, "trust_file")
    if shake.login == login_check.REJECTED and not found:
        found.append("rejected")
    return found


def _broken_trust_file(candidate: Candidate) -> str:
    """The trust variable a candidate's own env sets to a file that cannot work; "" when none does."""
    from .consumer_trust import TRUST_VARIABLES, file_problem

    for key in TRUST_VARIABLES:
        value = str(candidate.env.get(key) or "")
        found = re.fullmatch(r"\$\{[A-Za-z_][A-Za-z0-9_]*(?::-(.*))?\}", value)
        path = (found.group(1) or "") if found else value
        if path.startswith(("/", "~")) and file_problem(path):
            return key
    return ""


def repairs_for(settings: Any, classes: Sequence[str]) -> list[tuple[str, str]]:
    """``[(class, repair)]`` in the configured order, each repair once."""
    table = settings.dict_("mcp.repair.classes", DEFAULT_CLASSES)
    out: list[tuple[str, str]] = []
    for name in classes:
        for repair in table.get(name) or []:
            if all(repair != r for _, r in out):
                out.append((name, str(repair)))
    return out


# -- the repairs ---------------------------------------------------------------------------------------


def move_to_read_variable(candidate: Candidate, context: Mapping[str, Any]) -> list[Candidate]:
    """Variants with the credential moved to a variable the server reads.

    The reader's own source names what it reads (:meth:`.EnvContract.all_reads`):
    a token-like variable (``API_KEY``) takes the value as it is; a header list
    (``EXTRA_HEADERS``) takes ``Authorization: <scheme> ${VAR}``, the scheme from
    what the service asked for (its challenge, :mod:`.login_methods`).
    """
    from .env_contract import EnvContract
    from .secret_policy import carries_credentials, is_secret_name

    settings = context["settings"]
    reader, reads, _files = EnvContract(settings).all_reads(candidate.full_command())
    unread = [
        str(p["name"]) for p in candidate.handshake.shape_problems if "is not a variable" in str(p.get("remedy", ""))
    ]
    if not reads or not unread:
        return []
    scheme = _scheme(context.get("plan") or {})
    variants: list[Candidate] = []
    for holder in unread:
        reference = _reference(candidate, holder)
        for target in sorted(r for r in reads if is_secret_name(r)):
            variant = _copy(candidate, f"moved {holder} to {target}, which {reader} reads")
            _drop(variant, holder)
            if carries_credentials(target):
                # A header list: the whole header, the value expanded as Claude Code will.
                variant.env[target] = f"Authorization: {scheme} {reference}" if scheme else reference
                variant.secret_env.pop(target, None)
            else:
                variant.secret_env[target] = f"env:{reference[2:-1]}" if reference.startswith("${") else reference
                variant.env.pop(target, None)
            variants.append(variant)
    return variants


def fix_header_name(candidate: Candidate, context: Mapping[str, Any]) -> list[Candidate]:
    """A variant with the header named ``Authorization`` and the scheme word moved into its value."""
    from . import value_shapes

    settings = context["settings"]
    fixed = _copy(candidate, "")
    changed = []
    for key, value in list(candidate.env.items()):
        problem = value_shapes.mismatch(key, str(value), settings)
        if problem is None or "header name" not in str(problem.get("expected", "")):
            continue
        word = str(value).strip()
        fixed.env[key] = settings.str_("mcp.repair.header_name", "Authorization")
        partner = re.sub(r"(HEADER|HEADER_NAME)$", "VALUE", key)
        if partner in fixed.env and not str(fixed.env[partner]).lower().startswith(word.lower() + " "):
            fixed.env[partner] = f"{word} {fixed.env[partner]}"
        changed.append(f"{key} now names the Authorization header, with '{word}' at the start of its value")
    if not changed:
        return []
    fixed.add_note("repaired: " + "; ".join(changed))
    return [fixed]


def client_bridge(candidate: Candidate, context: Mapping[str, Any]) -> list[Candidate]:
    """The bridge that drives the service's own client, for a login only a client performs."""
    from . import client_bridge as builder

    plan = context.get("plan") or {}
    made: list[Candidate] = []
    for candidate, _artifacts, detail in builder.generate_all(context["ctx"], context["target"], context["host"], plan):
        log.info("%s: %s", context["target"], detail)
        if candidate is not None:
            made.append(candidate)
    return made


def use_system_bundle(candidate: Candidate, context: Mapping[str, Any]) -> list[Candidate]:
    """A variant whose trust variables name this machine's own CA bundle, where they named a file that cannot work.

    mcp-bugzilla's every call failed: SSL_CERT_FILE named a deleted /tmp bundle
    a login shell exported. Each trust variable the candidate sets to a missing
    or certificate-less file -- and each one its stderr named -- is given the
    system bundle; the writer reads it through the per-machine trust name.
    """
    from ..core.trust_store import system_bundle
    from . import trust
    from .consumer_trust import TRUST_VARIABLES, file_problem

    settings = context["settings"]
    base = system_bundle(settings)
    if not base or file_problem(base):
        return []
    names = sorted(set(trust.trust_variables(settings)) | set(TRUST_VARIABLES))
    said = str((candidate.handshake.stderr_cause or {}).get("line") or "") if candidate.handshake else ""
    variant = _copy(candidate, "")
    changed = []
    for key in names:
        value = str(candidate.env.get(key) or "")
        found = re.fullmatch(r"\$\{[A-Za-z_][A-Za-z0-9_]*(?::-(.*))?\}", value)
        path = (found.group(1) or "") if found else value
        named = bool(said) and re.search(rf"\b{re.escape(key)}\b", said) is not None
        if (path and file_problem(path)) or (named and key not in candidate.env) or (named and not path):
            variant.env[key] = base
            variant.references.pop(key, None)
            changed.append(key)
    if not changed:
        return []
    variant.add_note(
        f"repaired: {', '.join(changed)} now name this machine's own CA bundle -- the file "
        "given before could not be read"
    )
    return [variant]


REPAIRS: dict[str, Callable[[Candidate, Mapping[str, Any]], list[Candidate]]] = {
    "use_system_bundle": use_system_bundle,
    "move_to_read_variable": move_to_read_variable,
    "fix_header_name": fix_header_name,
    "client_bridge": client_bridge,
}


def run(
    candidate: Candidate, context: Mapping[str, Any], verify: Callable[[Candidate], Candidate]
) -> tuple[Candidate | None, list[str]]:
    """Repair ``candidate`` until a variant passes every check; ``(the variant or None, what was tried)``."""
    settings = context["settings"]
    classes = classify(candidate, context.get("plan"))
    tried: list[str] = []
    budget = max(0, settings.int_("mcp.repair.max_variants", DEFAULT_MAX_VARIANTS))
    for name, repair in repairs_for(settings, classes):
        action = REPAIRS.get(repair)
        if action is None:
            continue  # "operator": nothing the program can do -- the checklist says it
        for variant in action(candidate, context):
            if budget <= 0:
                return None, tried
            budget -= 1
            proved = verify(variant)
            shake = proved.handshake
            ok = proved.verified or (
                shake is not None and shake.ok and proved.needs_credential and shake.login not in login_check.REFUSED
            )
            tried.append(
                f"{repair} for {name}: {'worked' if ok else (shake.error if shake else 'no answer') or 'did not help'}"
            )
            log.info("%s: repair %s (%s) -- %s", proved.label, repair, name, "worked" if ok else "did not help")
            if ok:
                # The repair has to be what made the difference: asked again
                # now, the original must still fail. bugzilla.example.com's old
                # entry "was repaired" only because a rate limit had ended.
                again = verify(_copy(candidate, ""))
                if _as_good(again, proved, context.get("plan")):
                    tried[-1] = f"{repair} for {name}: not needed -- the original works when asked again"
                    again.add_note("works when asked again; its earlier failure did not last")
                    return again, tried
                proved.add_note(f"repaired ({repair}) after {name}")
                return proved, tried
    return None, tried


def _as_good(original: Candidate, repaired: Candidate, plan: Mapping[str, Any] | None) -> bool:
    """Does the original, asked again, meet the repaired variant's standard?

    Working is not enough: its checks must show nothing left to repair, its
    login be proved at least as well, and it must pass as much of its usage
    contract. A call that answers anybody proves nothing about an entry whose
    credential the server never reads.
    """
    from . import contract
    from .types import proof_rank

    if not original.verified or classify(original, plan):
        return False
    return proof_rank(original) <= proof_rank(repaired) and contract.score(original) >= contract.score(repaired)


def _scheme(plan: Mapping[str, Any]) -> str:
    for offered in plan.get("offered") or []:
        if offered.get("kind") == "header":
            scheme = str(offered.get("scheme") or "")
            return scheme[:1].upper() + scheme[1:].lower() if scheme else ""
    return ""


def _reference(candidate: Candidate, holder: str) -> str:
    """How the moved value is written: the ``${VAR}`` the holder already read."""
    ref = str(candidate.secret_env.get(holder) or "")
    variable = ref.partition("env:")[2] if ref.startswith("env:") else ""
    if variable:
        return f"${{{variable}}}"
    value = str(candidate.env.get(holder) or "")
    return value if value.startswith("${") else f"${{{holder}}}"


def _copy(candidate: Candidate, note: str) -> Candidate:
    variant = copy.deepcopy(candidate)
    variant.handshake = None
    variant.notes = list(candidate.notes)
    if note:
        variant.add_note(f"repaired: {note}")
    return variant


def _drop(candidate: Candidate, key: str) -> None:
    candidate.env.pop(key, None)
    candidate.secret_env.pop(key, None)


__all__ = ["DEFAULT_CLASSES", "REPAIRS", "classify", "repairs_for", "run"]
