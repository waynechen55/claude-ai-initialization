"""How a service can be logged in to -- every way it offers, and which to use.

build.example.net answers ``Basic realm="API login"``; the same build service's API
host, api.example.net, answers ``Signature realm="Use your developer account"`` --
requests signed with the operator's SSH key, which ``osc`` does and no header
variable can. The program read only the first challenge of the first host and
turned an unknown scheme word into "a token sent under that word", so it asked
for a token nobody can issue and wrote username/password entries no credential
could fix.

So, per address:

* **every challenge is read** -- each ``WWW-Authenticate`` scheme, on the target
  and on its API hosts (``api_host_patterns``, e.g. ``api.{domain}``), at the
  product's protected paths or the generic ``challenge_paths``
  (:func:`challenges`);
* **each scheme is classified** from ``[mcp.discovery.auth.methods]``: a header
  can carry it (basic, bearer, token, API key), a client must do it
  (signature, negotiate/Kerberos, NTLM, digest), or it is unknown -- never
  silently "a token";
* **the product is recognised** from ``[mcp.discovery.products.<name>]``
  (``recognise`` = a path read anonymously and markers its answer carries), and
  its catalogue says how it issues credentials: where a token is created, how
  it is sent, the scope reading needs, what a token type cannot do, which client
  speaks it;
* **every feasible method is a candidate, and none is preferred for its kind**
  (``mcp.discovery.auth.methods_equal``): an SSH-key signature, a token, a key
  and secret, a user name and password count the same. Proof decides between
  the servers built for them (a call that needs the login accepted, the usage
  checks passed); before anything is proved, the plan orders them by evidence
  only -- what this host accepted or refused before, which logins its API host
  itself offers, what this machine holds -- and names the others as
  alternatives (:func:`plan`). ``method_order`` is a neutral listing order, and
  decides again only with ``methods_equal`` off.

Nothing names a product or a scheme in code; each is a table.
"""

from __future__ import annotations

import re
from typing import Any, Callable, Mapping, Sequence

from ..core.config import fill
from ..core.logging_setup import get_logger

log = get_logger("mcp.login_methods")

HEADER = "header"
CLIENT = "client"
UNKNOWN = "unknown"

#: Fallbacks, equal to the packaged values.
DEFAULT_METHODS = {
    "basic": {"kind": HEADER, "method": "basic"},
    "bearer": {"kind": HEADER, "method": "token"},
    "token": {"kind": HEADER, "method": "token"},
    "signature": {"kind": CLIENT, "method": "client"},
    "negotiate": {"kind": CLIENT, "method": "client"},
    "kerberos": {"kind": CLIENT, "method": "client"},
    "ntlm": {"kind": CLIENT, "method": "client"},
    "digest": {"kind": CLIENT, "method": "client"},
}
DEFAULT_METHOD_ORDER = ("token", "apikey", "basic", "client")
DEFAULT_API_HOST_PATTERNS = ("api.{domain}",)
#: Methods chosen only when a challenge offered them (a password login is
#: advertised in its 401; a token or key usually is not).
DEFAULT_OFFERED_ONLY = ("basic",)
DEFAULT_CHALLENGE_PATHS = ("/",)


def schemes(header: str) -> list[dict[str, str]]:
    """Every challenge in one ``WWW-Authenticate`` value: ``[{"scheme", "text"}]``.

    A value may carry several (``Basic realm="x", Signature realm="y"``); a
    comma inside a quoted parameter does not start a new one.
    """
    out: list[dict[str, str]] = []
    pieces, current, quoted = [], "", False
    for char in str(header or ""):
        if char == '"':
            quoted = not quoted
        if char == "," and not quoted:
            pieces.append(current)
            current = ""
        else:
            current += char
    pieces.append(current)
    for piece in (p.strip() for p in pieces):
        match = re.match(r"([A-Za-z][A-Za-z0-9_-]*)(\s|$)", piece)
        if match and "=" not in match.group(1):
            out.append({"scheme": match.group(1), "text": piece})
        elif out and piece:
            out[-1]["text"] += ", " + piece  # another parameter of the same challenge
    return out


def classify(scheme: str, settings: Any) -> dict[str, str]:
    """``{"kind", "method"}`` for a scheme word: header-capable, client-only, or unknown."""
    table = settings.dict_("mcp.discovery.auth.methods", DEFAULT_METHODS)
    entry = table.get(str(scheme).lower())
    if isinstance(entry, Mapping):
        return {"kind": str(entry.get("kind") or UNKNOWN), "method": str(entry.get("method") or "")}
    return {"kind": UNKNOWN, "method": ""}


def api_hosts(host: str, settings: Any) -> list[str]:
    """The hosts that may serve ``host``'s API besides itself: ``api.{domain}`` and the like."""
    from .grounding import registrable_domain

    host = str(host or "").lower()
    domain = registrable_domain(host) or host
    out: list[str] = []
    for pattern in settings.str_list("mcp.discovery.auth.api_host_patterns", list(DEFAULT_API_HOST_PATTERNS)):
        candidate = fill(pattern, {"domain": domain, "host": host}).lower()
        if candidate and candidate != host and candidate not in out:
            out.append(candidate)
    return out


def products(settings: Any) -> dict[str, Mapping[str, Any]]:
    return {str(k): v for k, v in settings.dict_("mcp.discovery.products", {}).items() if isinstance(v, Mapping)}


def recognise(host: str, settings: Any, get: Callable[[str], Any]) -> str:
    """The catalogue product ``host`` is, from an anonymous read of each product's ``recognise.path``; "" if none."""
    for name, product in products(settings).items():
        rule = product.get("recognise") if isinstance(product.get("recognise"), Mapping) else {}
        if str(host).lower() in [str(h).lower() for h in rule.get("hosts") or []]:
            return name
    behind: list[str] = []
    for name, product in products(settings).items():
        rule = product.get("recognise") if isinstance(product.get("recognise"), Mapping) else {}
        path, markers = str(rule.get("path") or ""), [str(m) for m in rule.get("markers") or []]
        if not path.startswith("/") or not markers:
            continue
        response = get(f"https://{host}{path}")
        text = str(getattr(response, "text", "") or "") if response is not None else ""
        if (
            response is not None
            and 200 <= int(getattr(response, "status", 0) or 0) < 300
            and all(m in text for m in markers)
        ):
            return name
        if _behind_login(host, path, response, settings, get):
            behind.append(name)
            continue
        # Its recognition page may send a browser to single sign-on
        # (confluence.example.com's /login.action answers 302); its own API paths
        # still answer in the API's form.
        for protected in [str(x) for x in product.get("protected_paths") or [] if str(x).startswith("/")]:
            if _behind_login(host, protected.split("?", 1)[0], get(f"https://{host}{protected}"), settings, get):
                behind.append(name)
                break
    # Nothing answered openly; a product whose own recognition path asks for a
    # login -- in its API's form, while a sibling that cannot exist is answered
    # otherwise -- is that product behind a login (sd.example.com's serverInfo).
    return behind[0] if len(behind) == 1 else ""


def _behind_login(host: str, path: str, response: Any, settings: Any, get: Callable[[str], Any]) -> bool:
    """Does ``path`` exist on ``host`` behind a login? 401/403 not as a web page, a control sibling answered otherwise."""
    if not settings.bool_("mcp.discovery.auth.recognise_behind_login", True) or response is None:
        return False
    status = int(getattr(response, "status", 0) or 0)
    headers = getattr(response, "headers", {}) or {}
    served = str(headers.get("content-type") or "").split(";")[0].strip().lower()
    if status not in (401, 403) or served in ("text/html", "application/xhtml+xml", ""):
        return False
    control = settings.str_("mcp.discovery.api_kind_control_name", "initialize-claude-ai-control")
    sibling = path.rsplit("/", 1)[0] + "/" + control
    other = get(f"https://{host}{sibling}")
    other_status = int(getattr(other, "status", 0) or 0) if other is not None else 0
    return bool(other_status) and other_status != status


def same_product_api_hosts(host: str, product_name: str, settings: Any, get: Callable[[str], Any]) -> list[str]:
    """The :func:`api_hosts` of ``host`` that are recognised as the same product -- evidence, not spelling.

    ``api.{domain}`` is a guess from the name: for build.example.net it is
    api.example.net, the build service's API; for gitlab.example.net, openqa.example.net
    and smelt.example.net it is the same api.example.net, and for sd.example.com it is
    api.example.com -- neither theirs. Credited to them, its ``Signature`` and
    ``Basic`` challenges became their logins. A host counts only when it answers
    the product's own recognition (``recognise``) as ``host`` did
    (``mcp.discovery.auth.api_host_same_product``); an unrecognised target has none.
    """
    if not settings.bool_("mcp.discovery.auth.api_host_same_product", True):
        return api_hosts(host, settings)
    if not product_name:
        return []
    return [h for h in api_hosts(host, settings) if recognise(h, settings, get) == product_name]


def challenges(
    host: str, product: Mapping[str, Any] | None, settings: Any, get: Callable[[str], Any], product_name: str = ""
) -> list[dict[str, str]]:
    """Every login challenge the target and its API hosts answer: ``[{host, scheme, kind, method, evidence}]``."""
    paths = [str(p) for p in ((product or {}).get("protected_paths") or [])] or settings.str_list(
        "mcp.discovery.auth.challenge_paths", list(DEFAULT_CHALLENGE_PATHS)
    )
    found: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for where in [host, *same_product_api_hosts(host, product_name, settings, get)]:
        for path in paths:
            response = get(f"https://{where}{path}")
            if response is None or int(getattr(response, "status", 0) or 0) != 401:
                continue
            headers = getattr(response, "headers", {}) or {}
            for challenge in schemes(str(headers.get("www-authenticate") or "")):
                key = (where, challenge["scheme"].lower())
                if key in seen:
                    continue
                seen.add(key)
                found.append(
                    {
                        "host": where,
                        "scheme": challenge["scheme"],
                        **classify(challenge["scheme"], settings),
                        "evidence": challenge["text"][:120],
                    }
                )
            break  # one 401 per host says how that host is logged in to
    return found


#: Fallback for ``mcp.discovery.auth.remembered_challenges_days``.
REMEMBERED_CHALLENGES_DAYS = 30


def with_remembered(ctx: Any, host: str, offered: list[dict[str, str]]) -> list[dict[str, str]]:
    """``offered`` plus the challenges another run saw from a host that gave none this time; this run's are recorded.

    A temporary failure is not a verdict: build.opensuse.org's API host,
    api.opensuse.org, asks for ``Basic`` -- seen in earlier runs -- but was not
    recognised in one (its public page answered 503), its challenge went
    missing, and the login plan could not tell the client bridge's Basic login
    from a proxy's unknown one. A host's remembered challenges are used only
    when it gave none in this run, and only for ``remembered_challenges_days``.
    (``mcp.discovery.auth.remember_challenges``.)
    """
    import json
    import time
    from pathlib import Path

    settings = ctx.settings
    if not settings.bool_("mcp.discovery.auth.remember_challenges", True) or not host:
        return offered
    store = Path(ctx.state_dir) / settings.str_("mcp.discovery.auth.challenges_file", "mcp-login-challenges.json")
    try:
        data = json.loads(store.read_text(encoding="utf-8")) if store.is_file() else {}
    except (OSError, ValueError):
        data = {}
    now = time.time()
    keep = settings.int_("mcp.discovery.auth.remembered_challenges_days", REMEMBERED_CHALLENGES_DAYS) * 86400
    rows = data.get(host.lower(), {}) if isinstance(data, dict) else {}
    seen_now = {str(o.get("host") or "").lower() for o in offered}
    merged = list(offered)
    for where, row in rows.items():
        if where in seen_now or not isinstance(row, dict) or now - float(row.get("when", 0)) > keep:
            continue
        for challenge in row.get("challenges") or []:
            merged.append(
                {
                    **challenge,
                    "evidence": f"seen in an earlier run: {challenge.get('evidence', '')}"[:160],
                    "remembered": "yes",
                }
            )
    if offered and not ctx.dry_run:
        for where in seen_now:
            rows[where] = {
                "when": now,
                "challenges": [
                    o for o in offered if str(o.get("host") or "").lower() == where and not o.get("remembered")
                ],
            }
        data[host.lower()] = rows
        try:
            store.parent.mkdir(parents=True, exist_ok=True)
            store.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
        except OSError:
            pass
    return merged


#: Fallbacks for ``mcp.packages.credential_probe``'s stand-ins, equal to the packaged values.
DEFAULT_STANDIN_USER = "initialize-claude-ai-stand-in-user"
DEFAULT_STANDIN_SECRET = "initialize-claude-ai-stand-in-not-a-real-credential"


def _standin_headers(method: str, login: Mapping[str, Any], settings: Any) -> dict[str, str]:
    """The headers a stand-in login of ``method`` is sent with; {} for a method with no header form."""
    import base64

    probe = settings.section("mcp.packages.credential_probe")
    user = probe.str_("placeholder_user", DEFAULT_STANDIN_USER)
    secret = probe.str_("placeholder", DEFAULT_STANDIN_SECRET)
    if method == "basic":
        return {"Authorization": "Basic " + base64.b64encode(f"{user}:{secret}".encode()).decode("ascii")}
    sent = str(login.get("sent_as") or "")
    name, _, value = sent.partition(":")
    if name.strip() and "<" in value:
        return {name.strip(): re.sub(r"<[^>]+>", secret, value.strip())}
    return {}


def _answer_differs(one: Any, other: Any) -> bool:
    """Do two answers differ beyond what changes on every request (digits, case, spacing)?"""

    def norm(response: Any) -> tuple[int, str, str]:
        text = re.sub(r"\s+", " ", re.sub(r"[0-9a-f]{8,}|\d+", "", str(getattr(response, "text", "") or "").lower()))
        headers = getattr(response, "headers", {}) or {}
        return int(getattr(response, "status", 0) or 0), text[:600], str(headers.get("www-authenticate") or "").lower()

    return norm(one) != norm(other)


def understood_methods(
    host: str,
    product_name: str,
    offered: Sequence[Mapping[str, str]],
    settings: Any,
    get: Callable[[str], Any],
    ask: Callable[[str, Mapping[str, str]], Any],
) -> list[dict[str, str]]:
    """Logins the catalogue lists for ``host`` that no challenge named -- tried with a stand-in, kept if understood.

    A challenge names what a service *advertises*, not all it accepts:
    sd.example.com's 401 says ``OAuth``, and its API takes a user name and
    password. Read as a limit (``offered_only_methods``), that ruled the
    password out and left a token the customer account could not use. So each
    method in ``offered_only_methods`` the challenge left out is sent once, as a
    stand-in nobody owns (``mcp.packages.credential_probe``), to a read that
    needs a login: an answer that differs from the anonymous one shows the
    service processed it -- the method is understood, recorded with that
    evidence. Nothing real is sent; no account is at risk.
    (``mcp.discovery.auth.try_unoffered_methods``.)
    """
    config = settings.section("mcp.discovery.auth")
    if not config.bool_("try_unoffered_methods", True):
        return []
    product = products(settings).get(product_name) or {}
    named = {str(o.get("method")) for o in offered if str(o.get("host")) == host}
    trial = set(config.str_list("offered_only_methods", list(DEFAULT_OFFERED_ONLY)))
    paths = [str(p) for p in (product.get("protected_paths") or [])] or config.str_list(
        "challenge_paths", list(DEFAULT_CHALLENGE_PATHS)
    )
    found: list[dict[str, str]] = []
    for login in [x for x in product.get("logins") or [] if isinstance(x, Mapping)]:
        method = str(login.get("method") or "")
        if method in named or method not in trial:
            continue
        headers = _standin_headers(method, login, settings)
        if not headers:
            continue
        for path in paths:
            url = f"https://{host}{path}"
            anonymous = get(url)
            if anonymous is None or int(getattr(anonymous, "status", 0) or 0) not in (401, 403):
                continue
            answer = ask(url, headers)
            if answer is not None and _answer_differs(anonymous, answer):
                found.append(
                    {
                        "host": host,
                        "scheme": method,
                        "kind": HEADER,
                        "method": method,
                        "evidence": f"a stand-in {method} login changed the answer of {path} "
                        f"({getattr(anonymous, 'status', '?')} -> {getattr(answer, 'status', '?')})",
                    }
                )
            break
    return found


def plan(
    host: str,
    product_name: str,
    offered: Sequence[Mapping[str, str]],
    settings: Any,
    held: Mapping[str, Mapping[str, str]] | None = None,
    same_login: Mapping[str, Mapping[str, str]] | None = None,
    in_use: Mapping[str, Mapping[str, str]] | None = None,
    history: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """The method to use and the alternatives, by ``method_order`` among the feasible ones.

    Feasible: the product's catalogue lists it and, for a token, says it can
    read -- or the service offered it in a challenge. A token type the
    catalogue marks as unable to read (``reads = false``) is never chosen for a
    read-only server, and is said so.
    """
    product = products(settings).get(product_name) or {}
    logins = [dict(x) for x in product.get("logins") or [] if isinstance(x, Mapping)]
    order = settings.str_list("mcp.discovery.auth.method_order", list(DEFAULT_METHOD_ORDER))
    offered_methods = {str(o.get("method")) for o in offered if o.get("method")}
    feasible: list[dict[str, Any]] = []
    excluded: list[str] = []
    for login in logins:
        method = str(login.get("method") or "")
        if method == "token" and login.get("reads") is False:
            excluded.append(
                f"its {login.get('label') or 'tokens'} cannot read ({login.get('limits') or 'operation-only'})"
            )
            continue
        if (
            offered
            and method in settings.str_list("mcp.discovery.auth.offered_only_methods", list(DEFAULT_OFFERED_ONLY))
            and method not in offered_methods
        ):
            # A password login the service did not offer anywhere it was asked
            # (api.example.net only signs): not a choice, however the catalogue lists it.
            continue
        feasible.append(login)
    for method in sorted(offered_methods - {str(x.get("method")) for x in feasible}):
        if method in order or settings.bool_("mcp.discovery.auth.methods_equal", True):
            feasible.append({"method": method, "label": f"what {host} offers ({method})"})
    # The schemes this host accepted or refused before (login_history), as
    # methods: a method accepted last time first, one refused and never
    # accepted last -- before what is held, before the fixed order.
    seen: dict[str, str] = {}
    for scheme, verdict in (history or {}).items():
        method_of = classify(str(scheme), settings).get("method") or str(scheme)
        if verdict == "accepted" or seen.get(method_of) != "accepted":
            seen[method_of] = verdict

    equal = settings.bool_("mcp.discovery.auth.methods_equal", True)
    # Which methods the API host itself offers: a client of the API logs in
    # there (build.example.net's API host, api.example.net, offers only a signature;
    # build.opensuse.org's, api.opensuse.org, a password).
    api = set(api_hosts(host, settings))
    at_api = {str(o.get("method")) for o in offered if o.get("method") and str(o.get("host") or "") in api}
    neutral = {id(login): i for i, login in enumerate(feasible)}

    def rank(login: Mapping[str, Any]) -> tuple[int, ...]:
        method = str(login.get("method"))
        place = order.index(method) if method in order else len(order)
        before = (
            {"accepted": 0, "refused": 2}.get(seen.get(method, ""), 1)
            if settings.bool_("mcp.discovery.auth.history.enabled", True)
            else 1
        )
        held_rank = (
            (0 if method in (held or {}) or method in (in_use or {}) else 1 if method in (same_login or {}) else 2)
            if settings.bool_("mcp.discovery.auth.prefer_held_methods", True)
            else 0
        )
        if equal:
            # Evidence only, never the kind of method; ties keep the order found.
            return before, (0 if method in at_api else 1) if at_api else 0, held_rank, neutral.get(id(login), 0)
        if not settings.bool_("mcp.discovery.auth.prefer_held_methods", True):
            return before, 0, place
        # What the operator already holds -- exported, or sent to this host by an
        # entry here now -- before another address's login, before what they
        # would have to create.
        return (
            before,
            (0 if method in (held or {}) or method in (in_use or {}) else 1 if method in (same_login or {}) else 2),
            place,
        )

    feasible.sort(key=rank)
    client_only = bool(offered) and all(o.get("kind") == CLIENT for o in offered)
    chosen = feasible[0] if feasible else {}
    return {
        "product": product_name,
        "offered": [dict(o) for o in offered],
        "chosen": chosen,
        "alternatives": feasible[1:],
        "excluded": excluded,
        "client_only": client_only,
        # Variable *names* only: what the operator holds, here and (a login) for
        # another address of the same organisation.
        "held": {k: dict(v) for k, v in (held or {}).items()},
        "same_login": {k: dict(v) for k, v in (same_login or {}).items()},
        "in_use": {k: dict(v) for k, v in (in_use or {}).items()},
        "history": dict(history or {}),
    }


def describe(plan_: Mapping[str, Any], host: str) -> list[str]:
    """What the operator is told about logging in to ``host``, in words -- never a value."""
    lines: list[str] = []
    origin = f"https://{host}"
    chosen = plan_.get("chosen") or {}
    client_host = next((str(o["host"]) for o in plan_.get("offered") or [] if o.get("kind") == CLIENT), host)
    if plan_.get("client_only"):
        hosts = sorted({str(o["host"]) for o in plan_.get("offered") or []})
        words = sorted({str(o["scheme"]) for o in plan_.get("offered") or []})
        lines.append(
            f"{', '.join(hosts)} only take a login a client performs ({', '.join(words)}): no variable can carry it"
            + (
                f"; use {chosen.get('client')} -- "
                f"{fill(str(chosen.get('setup') or ''), {'origin': f'https://{client_host}', 'host': client_host})}"
                if chosen.get("client")
                else ""
            )
        )
    elif chosen.get("method") == "client" and chosen.get("client"):
        signing = sorted(
            {f"{o['scheme']} at {o['host']}" for o in plan_.get("offered") or [] if o.get("kind") == CLIENT}
        )
        lines.append(
            f"log in to {host} through {chosen.get('label') or chosen['client']}"
            + (f" (the service asks for {', '.join(signing)}, which only a client performs)" if signing else "")
            + f": {fill(str(chosen.get('setup') or ''), {'origin': f'https://{client_host}', 'host': client_host})}"
        )
    elif chosen.get("method") == "token" and chosen.get("create"):
        lines.append(
            f"log in to {host} with {chosen.get('label') or 'a token'}: create one at "
            f"{fill(str(chosen['create']), {'origin': origin, 'host': host})}"
            + (f" with the {chosen['scope']} scope" if chosen.get("scope") else "")
            + (f", sent as {chosen['sent_as']}" if chosen.get("sent_as") else "")
        )
    elif chosen.get("method") in ("basic", "apikey", "token"):
        held = (plan_.get("held") or {}).get(str(chosen.get("method"))) or {}
        same = (plan_.get("same_login") or {}).get(str(chosen.get("method"))) or {}
        if held:
            lines.append(
                f"log in to {host} with {chosen.get('label') or chosen['method']}: you already export "
                f"{', '.join(sorted(held.values()))}, which the entry reads"
            )
        elif same:
            names = ", ".join(sorted(v for k, v in same.items() if k != "HOST"))
            lines.append(
                f"log in to {host} with {chosen.get('label') or chosen['method']}: the same login as "
                f"{same.get('HOST')} ({names}) is tried, single sign-on; export it under {host}'s own names "
                "if it works there too"
            )
    for offered in plan_.get("offered") or []:
        if "stand-in" in str(offered.get("evidence") or ""):
            lines.append(
                f"{host} also takes {offered.get('method')} although its challenge does not name it: "
                f"{offered['evidence']}"
            )
    for reason in plan_.get("excluded") or []:
        lines.append(f"{host}: {reason}, so it is not offered for a read-only server")
    return lines


__all__ = [
    "CLIENT",
    "HEADER",
    "UNKNOWN",
    "api_hosts",
    "challenges",
    "classify",
    "describe",
    "plan",
    "products",
    "recognise",
    "same_product_api_hosts",
    "schemes",
    "understood_methods",
]
