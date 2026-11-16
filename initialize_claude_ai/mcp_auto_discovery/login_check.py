"""Is the credential *accepted*? One read-only call, judged the way a client sees it.

A server that starts, lists its tools and answers ``ping`` has proved that it
is there. It has not proved that its credential works. ``mcp-atlassian`` lists
every tool without contacting Jira, so sd.example.com's entry passed every check
this program had -- and every tool call then failed with ``500 Server Error ...
/servicedesk/customer/portals``: the instance sent the request to its customer
portal because it did not accept the caller as a Jira user. The same entry
pointed at jira.example.com, with that instance's token, worked.

So whenever every credential an entry needs holds a real value, the prober
calls one tool on the session it already opened and this module judges the
answer (:func:`judge`). Which tool is decided here too (:func:`pick`): only one
the server says is read-only, or whose name says so, and never one whose name
says it changes something. Everything is configuration --
``[mcp.verification.login_check]`` -- and the verdict travels on the handshake
(:attr:`.Handshake.login`), so both pipelines, the stage gate, the publish-time
check and the report read the same answer.
"""

from __future__ import annotations

import json
import re
from typing import Any, Mapping, Sequence

from ..core import redact

#: The verdicts. ``""`` on a handshake means the check was not attempted -- no
#: real credential to check, or the mode is off.
ACCEPTED, REJECTED, NO_ACCESS, UNCHECKED = "accepted", "rejected", "no_access", "unchecked"

#: A call failed because a *setting* is written in a form the server rejects --
#: an address with a scheme where it wants a bare host. osc-mcp's API tools add
#: ``https://`` themselves and accepted either form; only ``search_packages``
#: answered "unexpected api address format". Not a credential verdict: it is
#: recorded beside the login one (:attr:`.Handshake.misconfigured`).
MISCONFIGURED = "misconfigured"

#: A call that succeeded and returned *nothing*: an empty list, an object of
#: empty lists, a zero total, blank text. It proves nothing about access -- for
#: a restricted account "nothing to show you" and "you may not see anything"
#: are the same answer. sd.example.com's customer account got ``[]`` from
#: ``jira_get_all_projects`` while every issue lookup went to the portal; read
#: as "accepted", that would have settled the address on a server the account
#: cannot use.
EMPTY = "empty"

#: Verdicts that leave access unproved -- nothing decisive came back.
UNPROVED = (UNCHECKED, EMPTY)

#: Verdicts that say the credential does not work, as opposed to "not proved".
REFUSED = (REJECTED, NO_ACCESS)

#: "Try again later": a rate limit (429), a gateway or a service that is down for
#: now (502/503/504), a timeout. Never a verdict about the server, the entry or
#: the credential -- bugzilla.example.com's working entry got "429 Too Many
#: Requests" and was judged "failed what it is used for".
TEMPORARY = "temporary"

#: Fallbacks for ``[mcp.verification.login_check]``, equal to the packaged values.
DEFAULT_MODE = "pending"
DEFAULT_TOOL_PATTERNS = (
    r"(^|_)(get_)?(me|myself|whoami|current_user|my_profile|my_user)$",
    r"(^|_)(get|list)_my(_|$)",
    r"(^|_)(get_)?(server_)?(info|about|version|status)$",
    r"(^|_)(list|get_all|get)_(projects|spaces|repositories|repos|groups|products|boards|job_groups|workers"
    r"|architectures)$",
)
DEFAULT_NEVER_PATTERNS = (
    r"(^|_)(create|add|update|edit|set|put|post|patch|delete|remove|drop|merge|approve|assign|transition|move|copy"
    r"|upload|run|trigger|restart|cancel|retry|commit|branch|publish|write|send|mutat|execute|invoke)",
)
DEFAULT_MAX_CALLS = 3
DEFAULT_EMPTY_TOTAL_FIELDS = ("total", "size", "count", "totalCount", "total_count", "totalSize")
DEFAULT_EMPTY_LIST_FIELDS = ("values", "items", "results", "issues", "projects", "data", "nodes", "edges", "records")
#: Names that begin with a read verb are eligible (beside the patterns above
#: and anything annotated read-only), as long as never_patterns does not match.
DEFAULT_READ_VERBS = ("get", "list", "search", "show", "describe", "read", "info", "find", "query", "fetch", "view")
#: Values for required arguments, by argument-name pattern. ``{product}`` is
#: the target's product word, ``{host}`` its host.
DEFAULT_ARGUMENTS = {"^(pattern|query|q|search|search_?term|term|keyword|text)$": "{product}"}
DEFAULT_MISCONFIGURATION_MARKERS = (
    "unexpected api address format",
    "address format",
    "invalid url",
    "invalid uri",
    "missing protocol scheme",
    "unsupported protocol scheme",
    "no such host",
    "malformed",
    "invalid address",
    "invalid host",
    "failed to parse url",
)
DEFAULT_FAILURE_FIELDS = ("error", "errors", "success=false", "ok=false")
#: Fallback for ``failure_text_patterns``, equal to the packaged value: an
#: answer whose first line states an HTTP failure is a failure, flagged or not.
#: mcp-openapi-proxy answers "401 Client Error: Unauthorized for url: …" as an
#: ordinary result; it was taken as the account's own record, and an anonymous
#: proxy replaced build-opensuse-org's working bridge.
DEFAULT_FAILURE_TEXT_PATTERNS = (
    r"^\W*([45]\d\d) (?:client|server) error\b",
    r"^\W*(?:error:?\s*)*(?:api request failed:?\s*)?request failed with status code ([45]\d\d)\b",
    r"^\W*(?:error:?\s*)*http(?:/[\d.]+)?\s+([45]\d\d)\b",
    r"^\W*(?:error:?\s*)*status(?: code)?:?\s+([45]\d\d)\b",
)
#: Fallbacks for the temporary verdict, equal to the packaged values.
DEFAULT_TEMPORARY_MARKERS = (
    "too many requests",
    "rate limit",
    "ratelimit",
    "temporarily unavailable",
    "service unavailable",
    "bad gateway",
    "gateway timeout",
    "timed out",
    "try again later",
    # The connection dropped or stalled on the way: the service was never asked.
    # github.com's get_me met "TLS handshake timeout" and "unexpected EOF" on a
    # slow first connection, and the run took the token for refused.
    "unexpected eof",
    "tls handshake timeout",
    "i/o timeout",
    "context deadline exceeded",
    "connection reset",
    "econnreset",
    "socket hang up",
    "etimedout",
    "eai_again",
    "temporary failure in name resolution",
    # The service answered, but not in its API's form -- a page from whatever
    # stands in front of it: bugzilla.opensuse.org's /rest/time once came back
    # HTTP 200 "application/x-cgi", and a known-good entry was judged broken.
    "non-json response",
    "unexpected token <",
)
DEFAULT_TEMPORARY_STATUSES = (429, 502, 503, 504)
#: A ``login_reason`` beside NO_ACCESS: the service recognises the login but
#: has no account behind it for this service (see account_missing).
NO_ACCOUNT = "no_account"
#: Every call that succeeded answers the same with no credential: proves nothing.
PUBLIC_ONLY = "public_only"
#: Refused exactly as an anonymous caller is: the credential may never have been sent.
SAME_AS_ANONYMOUS = "same_as_anonymous"
#: Its calls succeed with no credential at all: it works without a login (a public service).
WITHOUT_LOGIN = "works_without_login"
#: Refused while no credential was sent at all: nothing was refused -- the
#: service asks for a login. Ranked and reported as a credential still missing.
NOTHING_SENT = "nothing_sent"
#: The launch asked to open a sign-in page (:mod:`.noninteractive`): it needs a
#: browser login, which discovery never waits for -- a step for the operator.
BROWSER_LOGIN = "needs_browser_login"
#: The service's own record of the account answered 403 with this credential
#: (:meth:`.CandidateVerifier._confirm_by_own_record`): the account may not use
#: the product at all, so no other server can do better with it.
ACCOUNT_NO_ACCESS = "account_no_access"
#: Asked with a credential issued for another instance (a token other hosts'
#: entries send): its refusal or empty answer says nothing about this address.
FOREIGN = "foreign_credential"
#: Tools that read the logged-in account's *own* record. A 404 from one is not
#: "proves nothing": the service knows no account for this login.
DEFAULT_IDENTITY_PATTERNS = (
    r"(^|_)(get_)?(me|myself|whoami|current_user)$",
    r"(^|_)(get_)?my_(account|profile|user|person)$",
)
#: A failure answered as a web page is the gateway in front of the API talking,
#: not the API (an XML or JSON API never answers in HTML).
DEFAULT_GATEWAY_PAGE_PATTERNS = (r"<!doctype html", r"<html[\s>]", r"\ban html page\b", r"not the api'?s (own )?form")
#: How :func:`judge` says a failure was a gateway's web page -- matched by
#: :func:`account_missing` and :func:`request_form_rejected`, so the wording
#: and the rules that read it cannot drift apart.
GATEWAY_PAGE_WORDS = "as a web page, not in the API's own form"
#: Fallbacks for ``[mcp.verification.request_forms]``, equal to the packaged values.
DEFAULT_REQUEST_FORM_STATUSES = (404, 406, 415)
#: Fewest calls that must all fail before a server counts as not working.
DEFAULT_FAILED_CALLS_MIN = 2
#: Fallbacks for ``status_outranks_wording``/``status_verdicts``/``status_pattern``.
DEFAULT_STATUS_OUTRANKS_WORDING = True
DEFAULT_STATUS_VERDICTS = {"401": "rejected", "403": "no_access"}
DEFAULT_STATUS_PATTERN = r"(?<![\d.:/])([1-5]\d\d)(?![\d.:/])"

DEFAULT_REJECTED_MARKERS = (
    "401",
    "unauthorized",
    "unauthorised",
    "authentication failed",
    "authentication required",
    "invalid token",
    "invalid api key",
    "invalid personal access token",
    "token is invalid",
    "token expired",
    "bad credentials",
    "invalid credentials",
    "invalid_token",
)
#: Fallback for ``missing_credential_markers``: a refusal that says no
#: credential reached the server (the variable unset, no key file) -- not that
#: one was refused.
DEFAULT_MISSING_CREDENTIAL_MARKERS = (
    "not set",
    "is unset",
    "no ssh key",
    "no key to sign with",
    "no credential",
    "no password",
    "no token",
    "missing credential",
    "does not pass",
)
#: Fallback for ``account_wide_markers``: a NO_ACCESS answer carrying one of
#: these is about the *account* (sent to a customer portal, a login or sign-on
#: page), not about one operation -- it outranks an accepted call in
#: :func:`summarise`. sd.example.com redirected a customer account to its portal.
DEFAULT_ACCOUNT_WIDE_MARKERS = ("/servicedesk/customer", "/login", "/sso/")
DEFAULT_NO_ACCESS_MARKERS = (
    "403",
    "forbidden",
    "/servicedesk/customer",
    "/login",
    "/sso/",
    "permission denied",
    "not permitted",
    "insufficient permission",
    "does not have permission",
)
DEFAULT_REMEDIES = {
    REJECTED: "{host} refused the credential in {variables} ({detail}). Create a new one on {host} and export it, "
    "then re-run phase 4.",
    NO_ACCESS: "{host} took the request but sent this account away from its API ({detail}) -- the account in "
    "{variables} is not allowed to use it. Use an account with API access to {host} (for a service desk: an agent "
    "rather than a portal customer), or a server built for what this account can reach, then re-run phase 4.",
    UNCHECKED: "the credential was not proved: {detail}.",
    MISCONFIGURED: "a setting is written in a form the server rejects ({detail}); the forms tried are listed above, "
    "and the server's own documentation or default says which it wants.",
    "alternative": "{host} also serves {kinds}; if the account in {variables} cannot use the API this server speaks "
    "(a service-desk customer, for one), export it and re-run phase 4 -- a bridge for that API will be built and "
    "checked.",
    SAME_AS_ANONYMOUS: "{host} refused the call exactly as it refuses a caller with no credential ({detail}). "
    "Before replacing the credential in {variables}, check that the server sends it at all: that the variable is "
    "one the server reads, that the auth type is one it implements, and that a header is sent under its own name "
    "(Authorization, with the scheme word at the start of the value). If all of that holds, the credential is "
    "wrong: create a new one on {host}.",
    NO_ACCOUNT: "{host} recognises the login in {variables}, but has no account behind it for this service "
    "({detail}). The credential itself is fine: sign in once to {host}'s web interface with that account, which "
    "creates its account there, or ask {host}'s administrators to enable it; then reconnect the server.",
}

#: How much of a server's answer is kept as the verdict's detail.
_DETAIL_CHARS = 200


def mode(settings: Any) -> str:
    """``pending``, ``warn`` or ``off`` (``mcp.verification.login_check.mode``)."""
    value = settings.str_("mcp.verification.login_check.mode", DEFAULT_MODE).strip().lower()
    return value if value in ("pending", "warn", "off") else DEFAULT_MODE


def enforced(settings: Any) -> bool:
    """Does a refused verdict stop a server counting as working?"""
    return mode(settings) == "pending"


def keeps_searching(candidate: Any, settings: Any) -> bool:
    """Is this a server whose account has no API access -- one that must not end the search?

    Its credential is valid; the account may not use the API this server
    speaks. Another server, for an API the account *can* use, may be found a
    tier down, so the descent goes on (``no_access_settles = false``) and the
    slot it held is given up to whatever is found (:meth:`.TargetOutcome.absorb`).
    """
    shake = getattr(candidate, "handshake", None)
    if shake is None:
        return False
    if shake.login == REJECTED and shake.login_refused:
        # Refused -- by a candidate that chose for itself how to send the
        # credential (an agent's bridge). That is evidence about the candidate
        # as much as the credential, so it ends nothing while a candidate that
        # sends it the documented way may be found (unconfirmed_rejection_settles).
        from .types import unconfirmed_refusal  # types imports this module

        return unconfirmed_refusal(candidate) and not settings.bool_(
            "mcp.verification.login_check.unconfirmed_rejection_settles", False
        )
    if shake.login != NO_ACCESS or not shake.login_refused:
        return False
    return not settings.bool_("mcp.verification.login_check.no_access_settles", False)


def tool_spec(entry: Mapping[str, Any]) -> dict[str, Any]:
    """What :func:`pick` needs to know about one listed tool.

    :func:`pick` never reads a description. ``description`` (trimmed) is kept
    for one use only: a usage task finding its tool by what the tool says it
    does (:func:`.contract.resolve`), and then only among tools that are safe
    to read with (:func:`callable_read`) -- the answer is judged either way.

    ``defaults`` holds, for each required argument, a value the tool's own
    schema offers -- its ``default``, ``const``, first ``examples`` entry or
    first ``enum`` value -- so a read-only tool with arguments can still be
    exercised with values the server itself suggested.
    """
    schema = entry.get("inputSchema") if isinstance(entry.get("inputSchema"), Mapping) else {}
    required = schema.get("required") if isinstance(schema.get("required"), list) else []
    annotations = entry.get("annotations") if isinstance(entry.get("annotations"), Mapping) else {}
    properties = schema.get("properties") if isinstance(schema.get("properties"), Mapping) else {}
    defaults: dict[str, Any] = {}
    for argument in required:
        prop = properties.get(argument) if isinstance(properties.get(argument), Mapping) else {}
        for key in ("default", "const"):
            if key in prop and prop[key] not in (None, ""):
                defaults[str(argument)] = prop[key]
                break
        else:
            for key in ("examples", "enum"):
                values = prop.get(key)
                if isinstance(values, list) and values and values[0] not in (None, ""):
                    defaults[str(argument)] = values[0]
                    break
    return {
        "name": str(entry.get("name") or ""),
        "required": [str(r) for r in required],
        "properties": [str(name) for name in properties],
        "defaults": defaults,
        # Each required argument's JSON type, for a stand-in of the right kind
        # (setting_probe_arguments): the schema's word, never a description.
        "types": {
            str(a): str((properties.get(a) or {}).get("type") or "string")
            if isinstance(properties.get(a), Mapping)
            else "string"
            for a in required
        },
        "read_only": annotations.get("readOnlyHint"),
        "destructive": annotations.get("destructiveHint"),
        "description": " ".join(str(entry.get("description") or "").split())[:300],
    }


def callable_read(spec: Mapping[str, Any], settings: Any) -> bool:
    """May this tool be called as a read: marked read-only, or named with a read verb -- never destructive or never_*?"""
    config = settings.section("mcp.verification.login_check")
    name = str(spec.get("name") or "")
    if not name or _matches(config.str_list("never_patterns", list(DEFAULT_NEVER_PATTERNS)), name):
        return False
    if spec.get("destructive") is True or spec.get("read_only") is False:
        return False
    if spec.get("read_only") is True:
        return True
    verbs = config.str_list("read_verbs", list(DEFAULT_READ_VERBS))
    return any(re.match(rf"(?i){re.escape(v)}", name) for v in verbs if v)


def _matches(patterns: Sequence[str], name: str) -> bool:
    for pattern in patterns:
        try:
            if pattern and re.search(str(pattern), name, re.IGNORECASE):
                return True
        except re.error:
            continue
    return False


def _arguments(
    spec: Mapping[str, Any], table: Mapping[str, Any], tokens: Mapping[str, str] | None = None
) -> dict[str, Any] | None:
    """Values for every required argument of ``spec``; None when one cannot be filled.

    The ``arguments`` table first (with ``{product}`` / ``{host}`` filled from
    ``tokens``), then the value the tool's own schema offers. A template whose
    token is unknown fills nothing.
    """
    filled: dict[str, Any] = {}
    defaults = spec.get("defaults") if isinstance(spec.get("defaults"), Mapping) else {}
    for argument in spec.get("required") or []:
        value: Any = None
        for pattern, template in table.items():
            if not _matches([str(pattern)], argument) or not str(template):
                continue
            text = str(template)
            for key, token in (tokens or {}).items():
                text = text.replace("{" + key + "}", str(token))
            if "{" not in text and text:
                value = text
                break
        if value is None and argument in defaults:
            value = defaults[argument]
        if value is None:
            return None
        filled[argument] = value
    return filled


def _reads(name: str, verbs: Sequence[str]) -> bool:
    """Does the tool's name begin with a read verb (``get_issue``, ``jira_get_issue``, ``list_requests``)?"""
    alternatives = "|".join(re.escape(v) for v in verbs if v)
    return bool(alternatives) and re.search(rf"(^|_)({alternatives})(_|$)", name, re.IGNORECASE) is not None


def pick(
    specs: Sequence[Mapping[str, Any]], settings: Any, tokens: Mapping[str, str] | None = None
) -> list[tuple[str, dict[str, Any]]]:
    """The tools that may be called, best first, at most ``max_calls``.

    Identity tools before listings, in the order of ``tool_patterns``; then any
    tool the server itself marks read-only; then any whose name begins with a
    read verb (``read_verbs``) -- those needing no argument before those whose
    arguments could be filled. One accepted call proves only that call's code
    path, so more than one is asked. A tool the server marks destructive or not
    read-only, or whose name matches ``never_patterns``, is never called.
    """
    config = settings.section("mcp.verification.login_check")
    patterns = config.str_list("tool_patterns", list(DEFAULT_TOOL_PATTERNS))
    never = config.str_list("never_patterns", list(DEFAULT_NEVER_PATTERNS))
    verbs = config.str_list("read_verbs", list(DEFAULT_READ_VERBS))
    table = config.dict_("arguments", dict(DEFAULT_ARGUMENTS))
    limit = max(0, config.int_("max_calls", DEFAULT_MAX_CALLS))

    def allowed(spec: Mapping[str, Any]) -> dict[str, Any] | None:
        name = str(spec.get("name") or "")
        if not name or _matches(never, name):
            return None
        if spec.get("destructive") is True or spec.get("read_only") is False:
            return None
        return _arguments(spec, table, tokens)

    chosen: list[tuple[str, dict[str, Any]]] = []
    seen: set[str] = set()
    for pattern in patterns:
        for spec in specs:
            name = str(spec.get("name") or "")
            if name in seen or not _matches([pattern], name):
                continue
            arguments = allowed(spec)
            if arguments is not None:
                chosen.append((name, arguments))
                seen.add(name)
    for spec in specs:
        name = str(spec.get("name") or "")
        if name in seen or spec.get("read_only") is not True:
            continue
        arguments = allowed(spec)
        if arguments is not None:
            chosen.append((name, arguments))
            seen.add(name)
    for needs_arguments in (False, True):
        for spec in specs:
            name = str(spec.get("name") or "")
            if name in seen or not _reads(name, verbs) or bool(spec.get("required")) != needs_arguments:
                continue
            arguments = allowed(spec)
            if arguments is not None:
                chosen.append((name, arguments))
                seen.add(name)
    return chosen[:limit]


#: Fallback for ``mcp.verification.login_check.setting_probe_values``: a
#: stand-in of each JSON type, for a call that only asks whether a setting is
#: accepted (a value nobody's data matches).
DEFAULT_SETTING_PROBE_VALUES = {"string": "initialize-claude-ai-probe", "integer": 1, "number": 1, "boolean": False}


def setting_probe_arguments(spec: Mapping[str, Any], settings: Any) -> dict[str, Any] | None:
    """Stand-in values for every required argument of ``spec``, by type; None for a type with no stand-in.

    For :meth:`.McpProber._probe_settings`: a call whose answer is read only
    for a setting error. osc-mcp's ``search_packages`` checks its address
    setting before it uses ``path``, so any value shows whether the setting's
    form is accepted -- the one tool that reads it had never been called.
    """
    table = settings.section("mcp.verification.login_check").dict_("setting_probe_values", DEFAULT_SETTING_PROBE_VALUES)
    defaults = spec.get("defaults") if isinstance(spec.get("defaults"), Mapping) else {}
    types = spec.get("types") if isinstance(spec.get("types"), Mapping) else {}
    out: dict[str, Any] = {}
    for argument in spec.get("required") or []:
        if argument in defaults:
            out[argument] = defaults[argument]
            continue
        kind = str(types.get(argument) or "string")
        if kind not in table:
            return None
        out[argument] = table[kind]
    return out


def unexercised(
    specs: Sequence[Mapping[str, Any]], settings: Any, tokens: Mapping[str, str] | None = None
) -> list[str]:
    """Read tools that could not be called -- a required argument nobody could fill.

    Reported, so "the credential was accepted" is not read as "every tool
    works": osc-mcp's ``search_packages`` needs ``path`` and
    ``path_repository``, and only that tool rejected the address form.
    """
    config = settings.section("mcp.verification.login_check")
    verbs = config.str_list("read_verbs", list(DEFAULT_READ_VERBS))
    never = config.str_list("never_patterns", list(DEFAULT_NEVER_PATTERNS))
    table = config.dict_("arguments", dict(DEFAULT_ARGUMENTS))
    out = []
    for spec in specs:
        name = str(spec.get("name") or "")
        if not name or _matches(never, name) or not _reads(name, verbs) or not spec.get("required"):
            continue
        if _arguments(spec, table, tokens) is None:
            missing = [a for a in spec["required"] if a not in (spec.get("defaults") or {})]
            out.append(f"{name} (needs {', '.join(missing)})")
    return out


def _text_of(reply: Mapping[str, Any]) -> tuple[str, bool]:
    """``(what the answer says, whether it is flagged as a failure)``."""
    if "error" in reply:
        error = reply.get("error")
        message = error.get("message", "") if isinstance(error, Mapping) else str(error or "")
        return str(message), True
    result = reply.get("result")
    if not isinstance(result, Mapping):
        return "", False
    parts = []
    for item in result.get("content") or []:
        if isinstance(item, Mapping) and isinstance(item.get("text"), str):
            parts.append(item["text"])
    structured = result.get("structuredContent")
    if not parts and structured is not None:
        parts.append(json.dumps(structured, default=str))
    return "\n".join(parts), bool(result.get("isError"))


def states_http_failure(text: str, patterns: Sequence[str]) -> bool:
    """Does the answer's first line state an HTTP failure (``failure_text_patterns``) -- a 4xx or 5xx, said in words?

    Only the first line: a listing that mentions "status 404" somewhere in an
    issue's text is not a failed call.
    """
    first = next((line.strip() for line in (text or "").splitlines() if line.strip()), "")
    for pattern in patterns:
        try:
            if re.search(pattern, first, re.IGNORECASE):
                return True
        except re.error:
            continue
    return False


def _flags_failure(text: str, fields: Sequence[str]) -> bool:
    """Does a JSON answer carry one of ``failure_fields``?"""
    try:
        payload = json.loads(text)
    except (TypeError, ValueError):
        return False
    if not isinstance(payload, Mapping):
        return False
    for field in fields:
        name, _, wanted = str(field).partition("=")
        name = name.strip()
        if not name or name not in payload:
            continue
        value = payload[name]
        if not wanted:
            if value not in (None, "", [], {}, False):
                return True
        elif str(value).strip().lower() == wanted.strip().lower():
            return True
    return False


def _empty(text: str, total_fields: Sequence[str], list_fields: Sequence[str]) -> bool:
    """Is a successful answer structurally empty -- nothing that proves the account could see anything?"""
    stripped = str(text or "").strip()
    if not stripped:
        return True
    try:
        payload = json.loads(stripped)
    except (TypeError, ValueError):
        return False
    if payload in ([], {}, None, ""):
        return True
    if isinstance(payload, list):
        return all(item in ([], {}, None, "") for item in payload)
    if not isinstance(payload, Mapping):
        return False
    totals = [payload[k] for k in total_fields if k in payload]
    lists = [payload[k] for k in list_fields if isinstance(payload.get(k), list)]
    if lists and all(not items for items in lists):
        return True
    if totals and all(str(v).strip() in ("0", "0.0") for v in totals) and not any(lists):
        return True
    return all(value in ([], {}, None, "") for value in payload.values())


def summarise(verdicts: Sequence[tuple[str, str, str]], settings: Any = None) -> tuple[str, str, str]:
    """``(verdict, tool, detail)`` for several calls: a refusal from any outranks acceptance from another.

    Acceptance needs a call that returned content (an EMPTY answer is not one);
    with neither, the first answer stands as "not proved". Order of the calls
    does not decide: sd.example.com's empty project list came first, its portal
    redirect later, and the refusal is the truth.

    With ``settings``, one refinement (``content_outranks_scoped_refusal``): a
    call *accepted with content* proves the credential, and a NO_ACCESS on
    another operation that is no account-wide sign -- a stated 403 for that one
    operation, not a redirect to a portal, login or sign-on page
    (``account_wide_markers``, ``mcp.discovery.auth.login_page_markers``) -- is
    a permission limit on that operation, said in the detail. Whether the server
    does its job stays the usage contract's verdict. confluence.example.com's
    ``confluence_search`` returned the operator's page and its usage task
    passed, ``confluence_search_user`` answered 403, and the whole credential
    was called "NO API ACCESS". A refused credential (401) still outranks all.
    """
    accepted = next((row for row in verdicts if row[0] == ACCEPTED), None)
    if settings is not None and accepted is not None:
        config = settings.section("mcp.verification.login_check")
        if config.bool_("content_outranks_scoped_refusal", True) and not any(row[0] == REJECTED for row in verdicts):
            wide = [
                *config.str_list("account_wide_markers", list(DEFAULT_ACCOUNT_WIDE_MARKERS)),
                *settings.str_list("mcp.discovery.auth.login_page_markers", []),
            ]
            scoped = [row for row in verdicts if row[0] == NO_ACCESS]
            if scoped and not any(_marker_in(wide, row[2] or "") for row in scoped):
                limits = "; ".join(
                    f"{tool} refused this account ({detail or 'no access'})" for _v, tool, detail in scoped
                )
                detail = f"{accepted[2]}; " if accepted[2] else ""
                return ACCEPTED, accepted[1], f"{detail}a permission limit, not the login: {limits}"
    for wanted in (REFUSED, (ACCEPTED,)):
        for verdict, tool, detail in verdicts:
            if verdict in wanted:
                return verdict, tool, detail
    if not verdicts:
        return "", "", ""
    if all(row[0] == TEMPORARY for row in verdicts):
        # Nothing was decided: busy, or never reached -- try again later.
        return TEMPORARY, verdicts[0][1], verdicts[0][2]
    empty = next((row for row in verdicts if row[0] == EMPTY), None)
    if empty is not None and all(row[0] in UNPROVED for row in verdicts):
        return UNCHECKED, empty[1], "answered with nothing"
    _verdict, tool, detail = verdicts[0]
    return UNCHECKED, tool, detail


def _marker_in(markers: Sequence[str], text: str) -> str:
    """The first marker ``text`` carries -- as a whole word where it starts or ends with one."""
    for marker in markers:
        marker = str(marker or "").strip()
        if not marker:
            continue
        start = r"\b" if marker[0].isalnum() else ""
        end = r"\b" if marker[-1].isalnum() else ""
        if re.search(f"{start}{re.escape(marker)}{end}", text, re.IGNORECASE):
            return marker
    return ""


def credential_missing(detail: str, settings: Any) -> bool:
    """Does a refusal's ``detail`` say no credential reached the server (unset, no key) rather than a refused one?"""
    markers = (
        settings.str_list(
            "mcp.verification.login_check.missing_credential_markers", list(DEFAULT_MISSING_CREDENTIAL_MARKERS)
        )
        if settings is not None
        else list(DEFAULT_MISSING_CREDENTIAL_MARKERS)
    )
    return bool(_marker_in(markers, str(detail or "")))


def judge(reply: Mapping[str, Any] | None, settings: Any) -> tuple[str, str]:
    """``(verdict, detail)`` for one ``tools/call`` answer. The detail is redacted."""
    if not isinstance(reply, Mapping):
        return UNCHECKED, "the call was not answered"
    config = settings.section("mcp.verification.login_check")
    text, failed = _text_of(reply)
    failed = failed or _flags_failure(text, config.str_list("failure_fields", list(DEFAULT_FAILURE_FIELDS)))
    failed = failed or states_http_failure(
        text, config.str_list("failure_text_patterns", list(DEFAULT_FAILURE_TEXT_PATTERNS))
    )
    detail = redact.redact(" ".join(text.split())[:_DETAIL_CHARS])
    if failed and is_temporary(text, settings):
        return TEMPORARY, f"try again later -- the service was busy or never reached: {detail or 'no message'}"
    if not failed:
        if config.bool_("empty_answers_prove_nothing", True) and _empty(
            text,
            config.str_list("empty_total_fields", list(DEFAULT_EMPTY_TOTAL_FIELDS)),
            config.str_list("empty_list_fields", list(DEFAULT_EMPTY_LIST_FIELDS)),
        ):
            return EMPTY, "answered with nothing"
        nobody = answered_as_nobody(text, settings)
        if nobody:
            # gitlab.example.net answered {"currentUser": null}: a success that names
            # no account is an anonymous answer, never a login accepted.
            return EMPTY, f"answered as nobody -- no account behind the call ({nobody})"
        return ACCEPTED, ""
    leading = _leading_status(text)
    if leading and leading in _request_form_statuses(settings) and is_gateway_page(text, settings):
        # A gateway's web page that leads with 404/406/415: that status is the
        # answer, whatever words surround it. build.opensuse.org's bridge
        # explained its 404 page with "... refused with 401 ..." and the
        # refusal markers fired on the explanation -- the login was blamed for
        # a request the gateway rejected (see request_form_rejected).
        return UNCHECKED, (
            f"the call failed for another reason: answered HTTP {leading} {GATEWAY_PAGE_WORDS} "
            "-- the gateway in front of it, not the API"
        )
    stated = stated_status_verdict(text, settings)
    if stated:
        # The status the server stated outranks a client's wording of it:
        # mcp-atlassian says "Authentication failed … (403). Token may be expired
        # or invalid", and 403 means the token was taken and its account may not
        # do this -- not a refused token.
        return stated, detail
    if _marker_in(config.str_list("rejected_markers", list(DEFAULT_REJECTED_MARKERS)), text):
        return REJECTED, detail
    no_access = [
        *config.str_list("no_access_markers", list(DEFAULT_NO_ACCESS_MARKERS)),
        *settings.str_list("mcp.discovery.auth.login_page_markers", []),
    ]
    if _marker_in(no_access, text):
        return NO_ACCESS, detail
    if _marker_in(config.str_list("misconfiguration_markers", list(DEFAULT_MISCONFIGURATION_MARKERS)), text):
        return MISCONFIGURED, detail
    if is_gateway_page(text, settings):
        # Read for the markers above like any failure, and never taken as the
        # API's own answer: an XML or JSON API does not speak HTML.
        status = re.search(r"\b([1-5]\d\d)\b", text)
        return UNCHECKED, (
            f"the call failed for another reason: answered {('HTTP ' + status.group(1)) if status else 'a failure'} "
            f"{GATEWAY_PAGE_WORDS} -- the gateway in front of it, not the API"
        )
    return UNCHECKED, f"the call failed for another reason: {detail or 'no message'}"


#: Fallback for ``[mcp.verification.login_check] no_identity_patterns``, equal
#: to the packaged list: a successful answer saying *who* is calling is nobody.
#: gitlab.example.net's GraphQL answered ``{"currentUser": null}`` while its server
#: sent no token; every other call answered too -- public projects only.
DEFAULT_NO_IDENTITY_PATTERNS = (
    r'"(currentUser|current_user|viewer|me|whoami|myself|authenticatedUser)"\s*:\s*null',
    r'"(authenticated|logged_in|loggedIn|is_authenticated)"\s*:\s*false',
)


def answered_as_nobody(text: str, settings: Any) -> str:
    """The part of a successful answer that says no account is calling; "" when none does."""
    patterns = (
        settings.str_list("mcp.verification.login_check.no_identity_patterns", list(DEFAULT_NO_IDENTITY_PATTERNS))
        if settings is not None
        else list(DEFAULT_NO_IDENTITY_PATTERNS)
    )
    for pattern in patterns:
        try:
            found = re.search(pattern, text or "", re.IGNORECASE)
        except re.error:
            continue
        if found:
            return " ".join(found.group(0).split())[:80]
    return ""


def stated_status_verdict(text: str, settings: Any) -> str:
    """The verdict an HTTP status stated in a failure means (``status_verdicts``); "" when none is stated.

    One status only: an answer naming both 401 and 403 is left to the wordings.
    """
    config = settings.section("mcp.verification.login_check") if settings is not None else None
    if config is not None and not config.bool_("status_outranks_wording", DEFAULT_STATUS_OUTRANKS_WORDING):
        return ""
    table = (
        config.dict_("status_verdicts", DEFAULT_STATUS_VERDICTS) if config is not None else None
    ) or DEFAULT_STATUS_VERDICTS
    pattern = config.str_("status_pattern", DEFAULT_STATUS_PATTERN) if config is not None else DEFAULT_STATUS_PATTERN
    verdicts = {REJECTED: REJECTED, NO_ACCESS: NO_ACCESS, "rejected": REJECTED, "no_access": NO_ACCESS}
    try:
        found = {code for code in re.findall(pattern, text or "") if str(code) in table}
    except re.error:
        return ""
    if len(found) != 1:
        return ""
    return verdicts.get(str(table[found.pop()]).lower(), "")


def is_temporary(text: str, settings: Any) -> bool:
    """Does a failure say "try again later" (``temporary_markers`` or a ``temporary_statuses`` code)?"""
    config = settings.section("mcp.verification.login_check")
    lowered = (text or "").lower()
    if any(m and m in lowered for m in config.str_list("temporary_markers", list(DEFAULT_TEMPORARY_MARKERS))):
        return True
    codes = [str(int(c)) for c in config.list_("temporary_statuses", list(DEFAULT_TEMPORARY_STATUSES))]
    return any(
        re.search(rf"(^|\D){code}(\D|$)", lowered)
        and re.search(rf"\b(http|status|error|code)\W{{0,3}}{code}\b|\b{code}\s+\w", lowered)
        for code in codes
    )


def is_gateway_page(text: str, settings: Any) -> bool:
    """Is ``text`` a web page (``gateway_page_patterns``) rather than an API's answer?"""
    patterns = settings.str_list(
        "mcp.verification.login_check.gateway_page_patterns", list(DEFAULT_GATEWAY_PAGE_PATTERNS)
    )
    return any(_search(p, text or "") for p in patterns)


def account_missing(tool: str, verdict: str, detail: str, settings: Any) -> bool:
    """Did a tool reading the account's *own* record get a 404 -- no account for this login?

    build.opensuse.org's ``get_my_account`` (``GET /person/<user>``) answered
    404 while a wrong login got 401: the login was recognised, and the service
    had no account behind it. That is a verdict about the account, not "the
    call failed for another reason".
    """
    if verdict != UNCHECKED or not re.search(r"\b404\b", detail or ""):
        return False
    if GATEWAY_PAGE_WORDS in (detail or "") or is_gateway_page(detail or "", settings):
        # A web page is the gateway's answer, not the API's: the same 404 came
        # back for an account that exists when the request's *form* was
        # rejected (a multi-value Accept header). Not an account verdict --
        # request_form_rejected() names it, and the request forms are tried.
        return False
    patterns = settings.str_list("mcp.verification.login_check.identity_tool_patterns", list(DEFAULT_IDENTITY_PATTERNS))
    return any(_search(p, tool or "") for p in patterns)


def _leading_status(text: str) -> str:
    """The HTTP status a failure leads with -- the first one on its first line; "" when it states none."""
    first = next((line.strip() for line in (text or "").splitlines() if line.strip()), "")
    found = re.search(r"\b([1-5]\d\d)\b", first)
    return found.group(1) if found else ""


def _request_form_statuses(settings: Any) -> set[str]:
    statuses = (
        settings.list_("mcp.verification.request_forms.statuses", list(DEFAULT_REQUEST_FORM_STATUSES))
        if settings is not None
        else DEFAULT_REQUEST_FORM_STATUSES
    )
    return {str(s) for s in statuses}


def request_form_rejected(rows: Sequence[Mapping[str, Any]], settings: Any) -> str:
    """The first call whose *request* the gateway rejected, as ``"tool: detail"``; "" when none was.

    A call answered with a gateway's web page and one of
    ``[mcp.verification.request_forms] statuses`` (404/406/415) proved nothing
    about the credential or the account: build.opensuse.org's bridge got that
    page for every logged-in call because of its Accept header, and the data
    with ``Accept: */*``. Only rows the login check judged inconclusive
    count -- a refusal or an acceptance already said something.
    """
    wanted = _request_form_statuses(settings)
    for row in rows or []:
        detail = str(row.get("detail") or "")
        if row.get("verdict") != UNCHECKED:
            continue
        if not (GATEWAY_PAGE_WORDS in detail or (settings is not None and is_gateway_page(detail, settings))):
            continue
        found = {code for code in re.findall(r"\b([1-5]\d\d)\b", detail)}
        if found & wanted:
            return f"{row.get('tool') or 'a call'}: {detail[:160]}"
    return ""


def same_answer(one: str, other: str) -> bool:
    """Are two failure details the same answer, apart from what changes call to call?"""

    def norm(text: str) -> str:
        text = re.sub(r"https?://\S+", "<url>", text or "")
        text = re.sub(r"\b[0-9a-f]{8,}\b|\d{4}-\d\d-\d\d[T ][\d:.]+Z?", "<id>", text, flags=re.IGNORECASE)
        return " ".join(text.lower().split())

    return norm(one) == norm(other)


def uniform_failure(rows: Sequence[Mapping[str, Any]]) -> str:
    """The HTTP status every call failed with, when they all failed alike; "" otherwise."""
    codes = set()
    for row in rows:
        if row.get("verdict") != UNCHECKED:
            return ""
        found = re.search(r"\b([1-5]\d\d)\b", str(row.get("detail") or ""))
        if not found:
            return ""
        codes.add(found.group(1))
    return codes.pop() if len(codes) == 1 else ""


def calls_all_failed(rows: Sequence[Mapping[str, Any]], settings: Any) -> bool:
    """Did every call the check made fail -- at least ``failed_calls_min`` of them?"""
    least = max(1, settings.int_("mcp.verification.login_check.failed_calls_min", DEFAULT_FAILED_CALLS_MIN))
    failures = ("the call failed", "the call was not answered")
    return len(rows) >= least and all(
        row.get("verdict") == UNCHECKED and str(row.get("detail") or "").startswith(failures) for row in rows
    )


def _search(pattern: str, text: str) -> bool:
    try:
        return re.search(pattern, text, re.IGNORECASE) is not None
    except re.error:
        return False


def classify_failure(text: str, settings: Any) -> str:
    """The verdict a *failure* message carries -- REJECTED, NO_ACCESS -- or "".

    For a failure that is not a tool call's answer: a server given a real
    credential that died at start-up saying "401 Unauthorized", or a healing
    attempt that ended at a portal. Read only because it already is a failure,
    so a marker in it means what it says.
    """
    config = settings.section("mcp.verification.login_check")
    if _marker_in(config.str_list("rejected_markers", list(DEFAULT_REJECTED_MARKERS)), text or ""):
        return REJECTED
    no_access = [
        *config.str_list("no_access_markers", list(DEFAULT_NO_ACCESS_MARKERS)),
        *settings.str_list("mcp.discovery.auth.login_page_markers", []),
    ]
    if _marker_in(no_access, text or ""):
        return NO_ACCESS
    if _marker_in(config.str_list("misconfiguration_markers", list(DEFAULT_MISCONFIGURATION_MARKERS)), text or ""):
        return MISCONFIGURED
    return ""


def judge_http(status: int, text: str, settings: Any) -> tuple[str, str]:
    """``(verdict, detail)`` for one authenticated HTTP request made directly to the API.

    The generated bridge's own check (it knows the API, so it can ask it
    without a tool) speaks the same verdicts as :func:`judge`: 401 is a refused
    credential, 403 an account without access, a 2xx is accepted, and anything
    else proves nothing. A body is read for the same markers only on failure.
    """
    detail = f"HTTP {status}"
    if 200 <= status < 300:
        return ACCEPTED, detail
    temporary = settings.section("mcp.verification.login_check").list_(
        "temporary_statuses", list(DEFAULT_TEMPORARY_STATUSES)
    )
    if status in [int(s) for s in temporary] or is_temporary(text, settings):
        return TEMPORARY, f"{detail}: the server asked to try again later"
    config = settings.section("mcp.verification.login_check")
    if status == 401 or _marker_in(config.str_list("rejected_markers", list(DEFAULT_REJECTED_MARKERS)), text or ""):
        return REJECTED, detail
    no_access = [
        *config.str_list("no_access_markers", list(DEFAULT_NO_ACCESS_MARKERS)),
        *settings.str_list("mcp.discovery.auth.login_page_markers", []),
    ]
    if status == 403 or _marker_in(no_access, text or ""):
        return NO_ACCESS, detail
    return UNCHECKED, f"the request answered {detail}"


def remedy(
    verdict: str,
    *,
    host: str,
    variables: Sequence[str],
    detail: str,
    settings: Any,
    kinds: str = "",
    reason: str = "",
) -> str:
    """What the operator is told for ``verdict`` -- or for its ``reason``, when one has its own remedy."""
    table = settings.dict_("mcp.verification.login_check.remedies", dict(DEFAULT_REMEDIES))
    template = str(
        (table.get(reason) or DEFAULT_REMEDIES.get(reason) if reason else "")
        or table.get(verdict)
        or DEFAULT_REMEDIES.get(verdict, "")
    )
    return (
        template.replace("{host}", host or "the service")
        .replace("{variables}", ", ".join(variables) or "its credential variables")
        .replace("{detail}", detail or "no detail")
        .replace("{kinds}", kinds or "another API")
    )


def in_words(verdict: str, *, sent: bool | None = None) -> str:
    """The verdict as a phrase for a report line -- about the credential only when one was sent (``sent``)."""
    if sent is False:
        # Nothing was sent: "the credential was REJECTED" described a token
        # nobody supplied, and "accepted" a login nobody made.
        if verdict in REFUSED:
            return "no credential was sent, and the service asks for one"
        if verdict == ACCEPTED:
            return "answered with no credential at all (public)"
    return {
        ACCEPTED: "the credential was accepted",
        REJECTED: "the credential was REJECTED",
        NO_ACCESS: "the account has NO API ACCESS",
        UNCHECKED: "the credential was not proved",
        MISCONFIGURED: "a setting is written in a form the server rejects",
        EMPTY: "the call answered with nothing",
        TEMPORARY: "the service was busy or never reached -- try again later (inconclusive)",
    }.get(verdict, "")


def waiting_words(verdict: str, source: str = "", *, started: bool = True) -> str:
    """What a server whose credential failed this check *is*, after its name.

    ``source`` names where the credential came from when it was not the
    exported variable -- "the value written in the existing entry ..." -- so
    the operator knows which value to replace.
    """
    head = "started, but its service" if started else "answered, but its service"
    given = source or "the credential it was given"
    if verdict == NO_ACCESS:
        return f"{head} gives the account behind {given} no API access"
    return f"{head} rejected {given}"


__all__ = [
    "ACCEPTED",
    "EMPTY",
    "MISCONFIGURED",
    "NO_ACCESS",
    "REFUSED",
    "REJECTED",
    "UNPROVED",
    "UNCHECKED",
    "classify_failure",
    "enforced",
    "FOREIGN",
    "in_words",
    "setting_probe_arguments",
    "judge",
    "judge_http",
    "keeps_searching",
    "account_missing",
    "is_temporary",
    "TEMPORARY",
    "calls_all_failed",
    "is_gateway_page",
    "uniform_failure",
    "NO_ACCOUNT",
    "PUBLIC_ONLY",
    "SAME_AS_ANONYMOUS",
    "answered_as_nobody",
    "WITHOUT_LOGIN",
    "same_answer",
    "mode",
    "pick",
    "remedy",
    "summarise",
    "tool_spec",
    "unexercised",
    "waiting_words",
]


def credential_in(values: Mapping[str, Any] | None) -> bool:
    """Does a launch's environment or a request's headers carry a non-empty credential?

    The fact a verdict is about: a refusal of a call that carried none refuses
    nothing (the service asks for a login), and a success with none proves only
    that the service answers anybody. Decided by the names and values the
    secret policy calls credentials -- never by what the reply said.
    """
    from .secret_policy import carries_inline_credential, holds_a_credential, is_secret_name

    for key, value in (values or {}).items():
        text = value.reveal() if hasattr(value, "reveal") else str(value if value is not None else "")
        if not text.strip() or "${" in text:
            continue
        if is_secret_name(str(key)) or holds_a_credential(str(key), text) or carries_inline_credential(text):
            return True
    return False


def nothing_sent(shake: Any) -> bool:
    """Was the call this verdict came from made with no credential at all (``Handshake.credential_sent`` false)?"""
    return shake is not None and getattr(shake, "credential_sent", None) is False


#: Fallback for ``mcp.verification.login_check.job_refused_markers``.
DEFAULT_JOB_REFUSED_MARKERS = (
    "query too large",
    "query is too large",
    "query has complexity",
    "max complexity",
    "maximum complexity",
    "query depth",
    "max depth",
    "too complex",
    "complexity limit",
    "query size",
)


def job_refused(rows: Sequence[Mapping[str, Any]], settings: Any) -> str:
    """ "tool: detail" for the first call whose failure says the service refused the query itself; "" for none.

    gitlab.example.net answered mcp-graphql's ``currentUser`` with "Query too
    large" -- with a valid token and without one: the generated query is
    what is refused, so no credential can make the tool work.
    """
    markers = (
        settings.str_list("mcp.verification.login_check.job_refused_markers", list(DEFAULT_JOB_REFUSED_MARKERS))
        if settings is not None
        else list(DEFAULT_JOB_REFUSED_MARKERS)
    )
    for row in rows or []:
        if row.get("passed") is True or row.get("verdict") == ACCEPTED:
            continue
        detail = str(row.get("detail") or "")
        if _marker_in(markers, detail):
            return f"{row.get('tool') or row.get('task') or 'a tool'}: {detail[:160]}"
    return ""
