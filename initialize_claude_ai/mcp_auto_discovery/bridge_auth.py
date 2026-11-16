"""How a generated bridge authenticates: decided from evidence, named for people.

build.example.net's bridge was registered with ``MCP_BRIDGE_AUTH_HEADER`` and
``MCP_BRIDGE_AUTH_VALUE``. Neither name said what to supply. The server had said
it plainly -- ``WWW-Authenticate: basic realm="API login"`` -- so the answer was
"your user name and password", but the operator was left to work out that the
header is ``Authorization`` and the value ``Basic`` plus the base64 of
``user:password``, by hand, in a shell. The value that was there had no
``Basic`` in it and could never have logged anybody in.

So the method is taken from evidence and the parts are named for what they are:

1. **The API description** -- OpenAPI 3 ``components.securitySchemes`` or
   Swagger 2 ``securityDefinitions``, preferring one the description's own
   ``security`` requirement names.
2. **The server's challenge** -- the ``WWW-Authenticate`` scheme of a 401 from a
   documented operation, asked without credentials.

Each method has parts (``[mcp.bridge.generated.auth.parts]``: basic is a user
name and a password, bearer a token, an API key a key). Each part is exported
under a variable named after the target (``variable_template``:
``BUILD_EXAMPLE_NET_USERNAME``) and described in words (``descriptions``). The
bridge reads it under that same name -- the manifest tells it which -- so the
entry, the report and the shell agree, and the bridge builds the header
itself, so nobody types base64 into a shell. Nothing here is a list in code: a method nobody has met is
a few lines of TOML.
"""

from __future__ import annotations

import re
from typing import Any, Mapping

from ..core.config import Settings
from ..core.logging_setup import get_logger

log = get_logger("mcp.bridge.auth")

#: OpenAPI ``http`` schemes, and Swagger 2 ``type`` values, to the method names
#: ``[mcp.bridge.generated.auth.parts]`` is keyed by.
_SPEC_SCHEMES = {"basic": "basic", "bearer": "bearer", "oauth2": "bearer", "openidconnect": "bearer"}


def method_from_spec(spec: Mapping[str, Any]) -> dict[str, str]:
    """The authentication method an API description declares; {} if it declares none."""
    components = spec.get("components") if isinstance(spec.get("components"), Mapping) else {}
    schemes = components.get("securitySchemes") if isinstance(components, Mapping) else None
    if not isinstance(schemes, Mapping):
        schemes = spec.get("securityDefinitions")
    if not isinstance(schemes, Mapping) or not schemes:
        return {}
    preferred: list[str] = []
    for requirement in spec.get("security") or []:
        if isinstance(requirement, Mapping):
            preferred.extend(str(name) for name in requirement)
    ordered = [n for n in preferred if n in schemes] + [n for n in schemes if n not in preferred]
    for name in ordered:
        scheme = schemes.get(name)
        if not isinstance(scheme, Mapping):
            continue
        kind = str(scheme.get("type", "")).lower()
        if kind == "http":
            method = _SPEC_SCHEMES.get(str(scheme.get("scheme", "")).lower())
            if method:
                return {"scheme": method, "evidence": f"the API description's {name!r} security scheme"}
        elif kind == "apikey" and scheme.get("name"):
            where = str(scheme.get("in", "header")).lower()
            if where in ("header", "query"):
                return {
                    "scheme": "apikey",
                    "in": where,
                    "name": str(scheme["name"]),
                    "evidence": f"the API description's {name!r} security scheme",
                }
        elif kind in _SPEC_SCHEMES:
            return {"scheme": _SPEC_SCHEMES[kind], "evidence": f"the API description's {name!r} security scheme"}
    return {}


def method_from_plan(plan: Mapping[str, Any] | None) -> dict[str, str]:
    """The method the address's login plan chose (:mod:`.login_methods`), as a bridge sends it; {} for none or a client.

    The plan weighs the catalogue, every challenge, what a stand-in showed the
    service understands and what the operator already holds; a generated bridge
    for an API kind follows it rather than the kind's configured default --
    sd.example.com's customer-API bridge sent a bearer token because the kind said
    so, while the address takes the operator's user name and password.
    """
    chosen = dict((plan or {}).get("chosen") or {})
    method = str(chosen.get("method") or "")
    evidence = f"the login plan for the address ({chosen.get('label') or method})"
    if method == "basic":
        return {"scheme": "basic", "evidence": evidence}
    sent = str(chosen.get("sent_as") or "")
    name, _, value = sent.partition(":")
    word = value.strip().split(" ", 1)[0] if value.strip() else ""
    if method in ("token", "apikey") and name.strip():
        if name.strip().lower() == "authorization" and word and not word.startswith("<"):
            return {"scheme": "bearer" if word.lower() == "bearer" else "token", "prefix": word, "evidence": evidence}
        return {"scheme": "apikey", "in": "header", "name": name.strip(), "evidence": evidence}
    return {}


def method_from_challenge(header: str, settings: Settings | None = None) -> dict[str, str]:
    """The method a ``WWW-Authenticate`` header asks for; {} if it names none this knows.

    ``basic realm="API login"`` -> basic. ``Bearer`` -> bearer. Any other single
    word scheme (``Token``) is a token sent under that word.
    """
    text = str(header or "").strip()
    match = re.match(r"([A-Za-z][A-Za-z0-9_-]*)", text)
    if not match:
        return {}
    word = match.group(1)
    evidence = f"its 401 challenge ({text[:80]})"
    lowered = word.lower()
    if settings is not None:
        from .login_methods import CLIENT, classify

        if classify(word, settings)["kind"] == CLIENT:
            # Signed requests (api.example.net's "Signature"), Kerberos, NTLM:
            # performed by a client, never carried by a header variable.
            return {}
    if lowered == "basic":
        return {"scheme": "basic", "evidence": evidence}
    if lowered == "bearer":
        return {"scheme": "bearer", "evidence": evidence}
    if lowered in ("negotiate", "ntlm", "digest"):
        return {}  # nothing a bridge can do with a header variable
    return {"scheme": "token", "prefix": word, "evidence": evidence}


def parts_of(method: Mapping[str, str], settings: Settings) -> list[str]:
    """The parts of a credential for ``method`` (``["USERNAME", "PASSWORD"]``)."""
    table = settings.dict_("mcp.bridge.generated.auth.parts", {})
    raw = table.get(str(method.get("scheme", "")), [])
    values = [raw] if isinstance(raw, str) else list(raw or [])
    return [str(v).strip().upper() for v in values if str(v).strip()]


def exported_name(slug: str, part: str, settings: Settings) -> str:
    """The variable an operator exports for one part: ``BUILD_EXAMPLE_NET_USERNAME``."""
    template = settings.str_("mcp.bridge.generated.auth.variable_template", "{SLUG}_{PART}")
    upper = re.sub(r"[^A-Za-z0-9]+", "_", slug).strip("_").upper()
    return template.replace("{SLUG}", upper).replace("{PART}", part)


#: Fallback for ``mcp.bridge.generated.auth.account``.
DEFAULT_ACCOUNT = (
    "the account {organisation} issues you -- the same user name and password as another service's is fine "
    "when this service accepts them"
)


def account_of(host: str, settings: Settings) -> str:
    """Which account a login for ``host`` is, in words -- its organisation's (``mcp.bridge.generated.auth.account``).

    "your user name on build.example.net" did not say whose account: the operator
    supplied their build.opensuse.org login, and build.example.net refused it.
    """
    from .grounding import registrable_domain  # imports this module's neighbours

    template = settings.str_("mcp.bridge.generated.auth.account", DEFAULT_ACCOUNT)
    return template.replace("{organisation}", registrable_domain(host) or host).replace("{host}", host)


def describe_part(part: str, host: str, settings: Settings) -> str:
    """What one part holds, in words (``your user name on build.example.net -- the account example.net issues you``)."""
    table = settings.dict_("mcp.bridge.generated.auth.descriptions", {})
    text = str(table.get(part) or part.lower().replace("_", " ")).replace("{host}", host)
    return text.replace("{account}", account_of(host, settings)) if "{account}" in text else text


def request_credentials(method: Mapping[str, str], values: Mapping[str, str]) -> tuple[dict[str, str], dict[str, str]]:
    """``(headers, query)`` carrying ``values`` the way the generated bridge sends them.

    Mirrors the bridge's own ``auth_headers`` / ``auth_query`` -- the login
    check asks exactly what the bridge will ask, or it proves nothing.
    """
    import base64

    scheme = method.get("scheme", "")
    if scheme == "basic" and values.get("USERNAME") and values.get("PASSWORD"):
        pair = base64.b64encode(f"{values['USERNAME']}:{values['PASSWORD']}".encode()).decode("ascii")
        return {"authorization": "Basic " + pair}, {}
    if scheme in ("bearer", "token") and values.get("TOKEN"):
        return {"authorization": f"{method.get('prefix') or scheme.capitalize()} {values['TOKEN']}"}, {}
    if scheme == "apikey" and values.get("API_KEY") and method.get("name"):
        if method.get("in") == "query":
            return {}, {method["name"]: values["API_KEY"]}
        return {method["name"]: values["API_KEY"]}, {}
    return {}, {}


def method_in_words(method: Mapping[str, str]) -> str:
    """``HTTP Basic``, ``a bearer token``, ``an API key in the X-Api-Key header``."""
    scheme = method.get("scheme", "")
    if scheme == "basic":
        return "HTTP Basic (a user name and a password)"
    if scheme == "bearer":
        return "a bearer token"
    if scheme == "token":
        return f'a token sent as "{method.get("prefix", "Token")} <token>"'
    if scheme == "apikey":
        return f"an API key in the {method.get('name')} {method.get('in', 'header')}"
    return scheme


__all__ = [
    "describe_part",
    "exported_name",
    "method_from_challenge",
    "method_from_spec",
    "method_in_words",
    "parts_of",
    "request_credentials",
]
