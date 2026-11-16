"""What the operator already holds for an address: credentials by name and shape, never by value.

build-example-net was written as ``"EXTRA_HEADERS": "${BUILD_EXAMPLE_NET_EXTRA_HEADERS:-}"``
-- a new variable, with nothing to say what goes in it -- while the shell had
exported ``BUILD_EXAMPLE_NET_USERNAME`` and ``BUILD_EXAMPLE_NET_BASE64_OF_USER_PASSWORD``:
exactly ``Authorization: Basic <that value>``. sd.example.com was offered nothing
but a token issued by jira.example.com, while the operator's login works on every
server of the organisation's single sign-on.

So the environment is read for each address (``[mcp.discovery.auth.inventory]``):

* a variable named for the address -- its slug, ``BUILD_EXAMPLE_NET_`` -- is the
  operator's for it; what it holds is told by its name's last words
  (``parts``: ``USERNAME``, ``PASSWORD``, ``TOKEN``, ...);
* a variable named like an encoded login (``composite_patterns``) counts as the
  basic login *only* when its value decodes to ``user:password`` -- the value
  is looked at in memory for its shape and dropped;
* the result is ``{method: {part: variable}}``: ``basic`` with ``USERNAME`` and
  ``PASSWORD`` (or ``ENCODED``), ``token`` with ``TOKEN``, ``apikey`` with
  ``API_KEY``.

Read by the login plan (a method the operator holds comes before one they
would have to create), by the writer (an entry slot needing an encoded login is
filled from an encoded variable they hold, or composed from the parts), and by
the same-login trial (a user name and password held for another address of the
same organisation is tried once, read-only, where none is held -- single
sign-on, nothing more). Values are never logged, reported or written.
"""

from __future__ import annotations

import base64
import binascii
import os
import re
from typing import Any, Mapping, Sequence

from .types import slugify

#: Fallbacks for ``[mcp.discovery.auth.inventory]``, equal to the packaged values.
DEFAULT_PARTS = {
    "USERNAME": ["USERNAME", "USER", "LOGIN"],
    "PASSWORD": ["PASSWORD", "PASS", "PASSWD"],
    "TOKEN": ["TOKEN", "PAT", "PERSONAL_TOKEN", "ACCESS_TOKEN", "PERSONAL_ACCESS_TOKEN"],
    "API_KEY": ["API_KEY", "APIKEY"],
}
DEFAULT_COMPOSITE_PATTERNS = ("BASE64", "BASIC", "ENCODED")
DEFAULT_METHODS = {"basic": [["USERNAME", "PASSWORD"], ["ENCODED"]], "token": [["TOKEN"]], "apikey": [["API_KEY"]]}


def prefix_of(host: str) -> str:
    """``build.example.net`` -> ``BUILD_EXAMPLE_NET_``: how the program names an address's variables."""
    return re.sub(r"[^A-Za-z0-9]+", "_", slugify(host)).strip("_").upper() + "_"


def decodes_to_login(value: str) -> bool:
    """Does ``value`` decode (base64) to ``user:password``? Looked at, never kept."""
    text = str(value or "").strip()
    if not text or len(text) % 4 or not re.fullmatch(r"[A-Za-z0-9+/=]+", text):
        return False
    try:
        decoded = base64.b64decode(text, validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return False
    user, sep, password = decoded.partition(":")
    return bool(sep and user.strip() and password)


def known_environ(ctx: Any) -> dict[str, str]:
    """What the operator holds: Claude Code's settings env (what a new terminal's Claude Code has) under this shell's.

    The operator keeps credentials in Claude Code's settings.json env; a held
    login looked up in this run's shell alone was not seen there, and
    gitlab.example.net's mcp-graphql was judged without the GITLAB_EXAMPLE_NET_TOKEN
    Claude Code would have given it. (``mcp.discovery.auth.inventory.include_claude_settings``.)
    """
    env: dict[str, str] = {}
    if ctx.settings.bool_("mcp.discovery.auth.inventory.include_claude_settings", True):
        try:
            from . import claude_check

            env.update(claude_check._claude_settings_env(ctx))
        except Exception:  # noqa: BLE001 - unreadable settings hold nothing
            pass
    env.update({k: v for k, v in os.environ.items() if str(v or "").strip()})
    return env


def held(host: str, settings: Any, environ: Mapping[str, str] | None = None) -> dict[str, dict[str, str]]:
    """``{method: {part: variable}}`` the operator exported for ``host``, complete methods only."""
    config = settings.section("mcp.discovery.auth.inventory")
    if not config.bool_("enabled", True):
        return {}
    env = os.environ if environ is None else environ
    prefix = prefix_of(host)
    parts_table = config.dict_("parts", DEFAULT_PARTS)
    composite = [str(p).upper() for p in config.str_list("composite_patterns", list(DEFAULT_COMPOSITE_PATTERNS))]
    found: dict[str, str] = {}
    for name in sorted(env):
        if not name.startswith(prefix) or not str(env.get(name) or "").strip():
            continue
        rest = name[len(prefix) :]
        if any(word in rest for word in composite):
            # Named like an encoded login: one only if the value is one --
            # never a plain password because its name ends in _PASSWORD.
            if decodes_to_login(env[name]):
                found.setdefault("ENCODED", name)
            continue
        for part, words in parts_table.items():
            if any(rest == str(w).upper() or rest.endswith("_" + str(w).upper()) for w in words or []):
                found.setdefault(str(part), name)
                break
    out: dict[str, dict[str, str]] = {}
    for method, alternatives in config.dict_("methods", DEFAULT_METHODS).items():
        for needed in alternatives or []:
            if needed and all(p in found for p in needed):
                out[str(method)] = {p: found[p] for p in needed}
                break
    return out


#: The text in a value shape's ``examples.basic`` that stands for an encoded login.
_ENCODED_PLACEHOLDER = "<base64 of user:password>"


def slot_part(slot: str, settings: Any) -> str:
    """What a credential slot of an entry holds: a part (``USERNAME``, ``TOKEN``...), or ``FORM:<template>``; "" unknown.

    A slot whose value shape (``mcp.discovery.auth.value_shapes``) expects an
    encoded login -- ``EXTRA_HEADERS``: ``Authorization:Basic <base64 of
    user:password>`` -- gets that example as its template, ``{ENCODED}`` where
    the encoded login goes, ``{USERNAME}``/``{PASSWORD}`` for a ``user:password``
    form.
    """
    name = str(slot).upper()
    for pattern, shape in settings.dict_("mcp.discovery.auth.value_shapes", {}).items():
        try:
            if not re.search(str(pattern), name):
                continue
        except re.error:
            continue
        example = str(((shape or {}).get("examples") or {}).get("basic") or "") if isinstance(shape, Mapping) else ""
        if _ENCODED_PLACEHOLDER in example:
            return "FORM:" + example.replace(_ENCODED_PLACEHOLDER, "{ENCODED}")
        if example == "<user>:<password>":
            return "FORM:{USERNAME}:{PASSWORD}"
    parts_table = settings.section("mcp.discovery.auth.inventory").dict_("parts", DEFAULT_PARTS)
    for part, words in parts_table.items():
        if any(name == str(w).upper() or name.endswith("_" + str(w).upper()) for w in words or []):
            return str(part)
    return ""


def values_for(login: Mapping[str, str], environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """A held basic login's parts, read now: ``USERNAME``, ``PASSWORD`` and ``ENCODED``, each derived if need be."""
    env = os.environ if environ is None else environ
    out = {part: str(env.get(variable) or "") for part, variable in login.items() if part != "HOST"}
    if out.get("ENCODED") and not out.get("USERNAME"):
        user, _, password = base64.b64decode(out["ENCODED"]).decode("utf-8").partition(":")
        out.update({"USERNAME": user, "PASSWORD": password})
    if out.get("USERNAME") and out.get("PASSWORD") and not out.get("ENCODED"):
        out["ENCODED"] = base64.b64encode(f"{out['USERNAME']}:{out['PASSWORD']}".encode()).decode("ascii")
    return out


def fill_slot(part: str, values: Mapping[str, str]) -> str:
    """The value a slot of :func:`slot_part` ``part`` takes from ``values``; "" when they do not cover it."""
    if part.startswith("FORM:"):
        template = part[len("FORM:") :]
        needed = re.findall(r"\{([A-Z_]+)\}", template)
        if not all(values.get(n) for n in needed):
            return ""
        return re.sub(r"\{([A-Z_]+)\}", lambda m: values[m.group(1)], template)
    return str(values.get(part) or "")


def run_hosts(ctx: Any) -> list[str]:
    """The hosts of this run's addresses (``--mcp-server`` / ``mcp.servers``)."""
    from .address import parse as parse_address

    raw = ctx.state.get("mcp_servers") or ctx.settings.str_list("mcp.servers")
    hosts: list[str] = []
    # --mcp-server takes a comma-separated list, kept as given.
    items = [part for value in raw or [] for part in re.split(r"[,\s]+", str(value)) if part]
    for item in items:
        host = (parse_address(str(item)).host or "").lower()
        if host and host not in hosts:
            hosts.append(host)
    return hosts


def same_login_hosts(host: str, hosts: list[str], settings: Any) -> list[str]:
    """Of ``hosts``, those of ``host``'s organisation -- where its single sign-on login may also work."""
    from .grounding import registrable_domain

    if not settings.bool_("mcp.discovery.auth.inventory.same_login", True):
        return []
    mine = _organisation(registrable_domain(host) or host)
    return [h for h in hosts if h != host and _organisation(registrable_domain(h) or h) == mine]


def _organisation(domain: str) -> str:
    """``example.com`` and ``example.net`` are one organisation: the registrable domain without its suffix."""
    return str(domain or "").lower().split(".", 1)[0]


__all__ = [
    "decodes_to_login",
    "fill_slot",
    "held",
    "prefix_of",
    "run_hosts",
    "same_login_hosts",
    "slot_part",
    "values_for",
]


def methods_of_slots(slots: Sequence[str], settings: Any) -> dict[str, dict[str, str]]:
    """``{method: {part: slot}}`` the credential slots ``slots`` make up -- complete methods only.

    The credential an existing entry already sends a host, named by what it
    holds (``parts``, ``methods``): ``JIRA_PERSONAL_TOKEN`` is a token, a
    ``…_USERNAME`` with a ``…_PASSWORD`` a basic login.
    """
    config = settings.section("mcp.discovery.auth.inventory")
    found: dict[str, str] = {}
    for slot in slots:
        part = slot_part(str(slot), settings)
        if part and not part.startswith("FORM:"):
            found.setdefault(part, str(slot))
    out: dict[str, dict[str, str]] = {}
    for method, alternatives in config.dict_("methods", DEFAULT_METHODS).items():
        for needed in alternatives or []:
            if needed and all(p in found for p in needed):
                out[str(method)] = {p: found[p] for p in needed}
                break
    return out
