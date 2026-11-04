"""The usage contract: a server works when it does the job, not when it answers.

Every failure that reached the operator had passed the program's checks:
smelt.example.net got a bridge for an API it does not serve, sd.example.com's token
was valid for an API its account could not use, build-example-net "worked" on a
``get_about`` anybody may call, build.opensuse.org's login had nothing behind
it. Each check was a stand-in for one question -- *can Claude Code do with this
server what the operator will do with it?* -- and this module asks it directly.

A **task** is one real read: ``name``, ``tools`` (patterns, the first listed
tool that matches is called), ``arguments`` (argument-name pattern -> value,
``{host}`` filled), ``expect`` (text the answer must contain, compared without
case and never printed) and an optional ``truth`` -- a ``path`` on the target
read directly, whose JSON ``fields`` must appear in the answer, so the answer
is shown to come from *this* instance. Tasks come from two places, both
configuration, neither code:

* ``[mcp.contract.products.<name>]`` -- what a kind of server is for. A task
  that sends nothing of its own (``describe the API``) is asked of any server
  listing its tool; one that sends a product's own words -- GitLab's
  ``{ currentUser { username } }`` -- belongs to that product and is asked only
  where the address *is* it: recognised as the product or serving the API kind
  (``scope``, :func:`scope_of`). smelt.example.net's GraphQL bridge was asked
  GitLab's query because it listed ``graphql_query``, failed it, and was left
  out of the file although it did its own job;
* ``[mcp.contract.targets."<host>"].checks`` -- the operator's own, for one address,
  kept in their configuration (``~/.config/initialize-claude-ai/config.toml``
  or ``./initialize-claude-ai.toml``).

The handshake, the login check and the empty-answer rules stay as diagnostics;
the contract decides (:func:`verdict`), and a replacement must pass more of it
than what it replaces (:func:`score`).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

from . import login_check

#: Fallbacks for ``[mcp.contract]``, equal to the packaged values.
DEFAULT_ENABLED = True
DEFAULT_MAX_TASKS = 4
DEFAULT_ANSWER_PROBES = 3


@dataclass
class Task:
    name: str
    tools: list[str] = field(default_factory=list)
    #: Patterns for what a tool says it does (its description), for a server
    #: that names its tool otherwise -- smelt's agent-written bridge did.
    describes: list[str] = field(default_factory=list)
    #: Words the kind's own answer carries ("__schema"): with no tool found by
    #: name or description, a read tool whose answer carries one does the task.
    answer_markers: list[str] = field(default_factory=list)
    arguments: dict[str, Any] = field(default_factory=dict)
    expect: list[str] = field(default_factory=list)
    truth: dict[str, Any] = field(default_factory=dict)
    source: str = ""
    #: Only the logged-in account can pass it (its own record, the current
    #: user): passed, it proves the login as much as an accepted login check.
    needs_login: bool = False


def enabled(settings: Any) -> bool:
    return settings.bool_("mcp.contract.enabled", DEFAULT_ENABLED)


#: The start of a task's detail when the server said the request itself does not fit it (:func:`fits`).
NOT_FITTING = "does not fit this server"
#: Fallback for ``mcp.contract.not_fitting_markers``, equal to the packaged value.
DEFAULT_NOT_FITTING_MARKERS = (
    "cannot query field",
    "unknown argument",
    "unknown field",
    "unknown type",
    "doesn't exist on type",
    "does not exist on type",
    "graphql_validation_failed",
    "validation error",
    "400 bad request",
    "400: bad request",
    "http 400",
    "status 400",
    "status code 400",
    "error 400",
)

#: Fallbacks for ``mcp.contract.scope_product_tasks``/``default_scope``/``scope_by_arguments``.
DEFAULT_SCOPE_PRODUCT_TASKS = True
DEFAULT_SCOPE = "tools"
DEFAULT_SCOPE_BY_ARGUMENTS = True
SCOPES = ("address", "tools")


def scope_of(settings: Any, product: Mapping[str, Any], raw: Mapping[str, Any]) -> str:
    """``"address"`` -- asked only where the address is this product -- or ``"tools"`` -- of any server listing its tool.

    The task's own ``scope``, else its product's, else: a task that sends
    arguments carries the product's own words and belongs to it
    (``mcp.contract.scope_by_arguments``); otherwise ``mcp.contract.default_scope``.
    """
    for explicit in (raw.get("scope"), product.get("scope")):
        if str(explicit or "").lower() in SCOPES:
            return str(explicit).lower()
    if raw.get("arguments") and settings.bool_("mcp.contract.scope_by_arguments", DEFAULT_SCOPE_BY_ARGUMENTS):
        return "address"
    fallback = settings.str_("mcp.contract.default_scope", DEFAULT_SCOPE).lower()
    return fallback if fallback in SCOPES else DEFAULT_SCOPE


def tasks_for(settings: Any, host: str, job: Sequence[str] | None = None) -> list[Task]:
    """The operator's checks for ``host``, then the products' tasks that may be asked of it.

    ``job`` is what the address is -- its recognised product and detected API
    kinds -- or None when nothing is known yet. A task scoped to its product
    (:func:`scope_of`) is included only when that product is in ``job``; the
    rest are applied later, by the tools a server lists.
    """
    if not enabled(settings) or login_check.mode(settings) == "off":
        return []  # "off" means no tool is called at all
    out: list[Task] = []
    targets = settings.dict_("mcp.contract.targets", {})
    for key, table in targets.items():
        if isinstance(table, Mapping) and _same_host(str(key), host):
            out += [_task(raw, "you") for raw in table.get("checks") or [] if isinstance(raw, Mapping)]
    scoped = settings.bool_("mcp.contract.scope_product_tasks", DEFAULT_SCOPE_PRODUCT_TASKS)
    known = {str(n).lower() for n in job or []}
    for name, product in settings.dict_("mcp.contract.products", {}).items():
        if not isinstance(product, Mapping):
            continue
        for raw in product.get("tasks") or []:
            if not isinstance(raw, Mapping):
                continue
            if scoped and scope_of(settings, product, raw) == "address" and str(name).lower() not in known:
                continue  # another product's words: never asked of a server for an address that is not it
            out.append(_task(raw, f"product {name}"))
    return [t for t in out if t.name and t.tools]


def product_tasks(settings: Any, names: Sequence[str]) -> list[Task]:
    """The tasks of the catalogue products and API kinds ``names`` (``[mcp.contract.products.<name>]``)."""
    if not enabled(settings) or login_check.mode(settings) == "off":
        return []
    table = settings.dict_("mcp.contract.products", {})
    out: list[Task] = []
    for name in names:
        product = table.get(name)
        if isinstance(product, Mapping):
            out += [_task(raw, f"product {name}") for raw in product.get("tasks") or [] if isinstance(raw, Mapping)]
    return [t for t in out if t.name and t.tools]


def askable(
    tasks: Sequence[Task], tools: Sequence[str], specs: Sequence[Mapping[str, Any]] = (), settings: Any = None
) -> bool:
    """Can any of ``tools`` be asked one of ``tasks`` -- by name, or (``specs``) by what it says it does?"""
    if any(_search(p, str(tool)) for task in tasks for p in task.tools for tool in tools):
        return True
    return any(_by_description(task, specs, settings) for task in tasks) if specs else False


def _by_description(task: Task, specs: Sequence[Mapping[str, Any]], settings: Any) -> str:
    """The first safe read tool whose description matches one of ``task.describes``; ""."""
    if not task.describes or settings is None or not settings.bool_("mcp.contract.match_descriptions", True):
        return ""
    for spec in specs:
        text = str(spec.get("description") or "")
        if text and any(_search(p, text) for p in task.describes) and login_check.callable_read(spec, settings):
            return str(spec.get("name") or "")
    return ""


def unmatched(tasks: Sequence[Task], calls: Sequence[Mapping[str, Any]]) -> list[Task]:
    """The tasks with answer markers that no call was planned for -- to be found by their answers."""
    planned = {str(c.get("task")) for c in calls}
    return [t for t in tasks if t.answer_markers and t.name not in planned]


def by_answer(task: Task, specs: Sequence[Mapping[str, Any]], settings: Any) -> list[str]:
    """Safe read tools needing no argument, to ask for an answer carrying ``task.answer_markers``."""
    if settings is None or not settings.bool_("mcp.contract.match_answers", True):
        return []
    limit = max(0, settings.int_("mcp.contract.answer_probe_max_tools", DEFAULT_ANSWER_PROBES))
    out = [
        str(s.get("name"))
        for s in specs
        if not [r for r in s.get("required") or [] if r not in (s.get("defaults") or {})]
        and login_check.callable_read(s, settings)
    ]
    return out[:limit]


def carries(reply: Any, markers: Sequence[str]) -> str:
    """The first of ``markers`` the reply's text carries; ""."""
    text = _text(reply)
    return next((m for m in markers if m and m in text), "")


def resolve(
    tasks: Sequence[Task],
    specs: Sequence[Mapping[str, Any]],
    *,
    host: str = "",
    limit: int = DEFAULT_MAX_TASKS,
    settings: Any = None,
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """``(calls, skipped)``: the tasks this server can be asked, with the tool and arguments for each.

    A product's task is skipped silently when the server lists none of its
    tools -- it is another kind of server. The operator's own task is
    reported as skipped, with why: they asked for it.
    """
    calls: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    by_name = {str(s.get("name") or ""): s for s in specs}
    for task in tasks:
        tool = next((n for n in by_name if any(_search(p, n) for p in task.tools)), "") or _by_description(
            task, specs, settings
        )
        if not tool:
            if task.source == "you":
                skipped.append({"task": task.name, "why": "the server lists no tool for it"})
            continue
        spec = by_name[tool]
        arguments: dict[str, Any] = {}
        for prop in [*spec.get("properties", []), *spec.get("required", [])]:
            for pattern, value in task.arguments.items():
                if _search(pattern, str(prop)) and prop not in arguments:
                    arguments[str(prop)] = _fill(value, host)
        missing = [r for r in spec.get("required", []) if r not in arguments]
        for name in missing:
            if name in (spec.get("defaults") or {}):
                arguments[name] = spec["defaults"][name]
        missing = [r for r in spec.get("required", []) if r not in arguments]
        if missing:
            skipped.append(
                {"task": task.name, "why": f"{tool} needs {', '.join(missing)}, which the task does not give"}
            )
            continue
        calls.append(
            {
                "task": task.name,
                "tool": tool,
                "arguments": arguments,
                "source": task.source,
                "expect": list(task.expect),
                "truth": dict(task.truth),
                "needs_login": task.needs_login,
            }
        )
        if len(calls) >= limit:
            break
    return calls, skipped


def judge(
    reply: Any,
    call: Mapping[str, Any],
    settings: Any,
    truth: Callable[[Mapping[str, Any]], tuple[list[str], str]] | None = None,
    *,
    sent: bool | None = None,
) -> tuple[bool | None, str]:
    """Did one task's call do the job? ``(passed, detail)`` -- None when the server said "try again later".

    The detail never quotes the answer's content.
    """
    verdict, detail = login_check.judge(reply, settings)
    if verdict == login_check.TEMPORARY:
        return None, f"inconclusive -- {detail}"
    if verdict == login_check.UNCHECKED and _misfit(reply, settings):
        # A client error that blames the request -- a field or argument this
        # API does not have -- says the task was written for another server:
        # neither a failure nor a pass. A refused login (401/403) is no misfit.
        return None, f"{NOT_FITTING}: it refused the request itself, not the caller ({detail})"
    if verdict == login_check.EMPTY:
        return False, "answered with nothing"
    if verdict != login_check.ACCEPTED:
        return False, f"{login_check.in_words(verdict, sent=sent) or 'failed'} ({detail})"
    text = _text(reply).lower()
    if login_check.is_gateway_page(text, settings):
        return False, "answered with a web page, not the API's own answer"
    missing = [e for e in call.get("expect") or [] if str(e).lower() not in text]
    if missing:
        return False, f"the answer lacks {len(missing)} of the {len(call['expect'])} expected value(s)"
    if call.get("truth") and truth is not None:
        values, why = truth(call["truth"])
        if why:
            return True, f"passed; the direct read for comparison did not answer ({why})"
        absent = [v for v in values if v.lower() not in text]
        if absent:
            return False, f"the answer does not match the target read directly ({len(absent)} value(s) differ)"
        return True, f"passed, and matches the target read directly ({len(values)} value(s))"
    return True, "passed"


def truth_values(text: str, fields: Sequence[str]) -> list[str]:
    """The values at dotted ``fields`` in a JSON answer read directly from the target."""
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return []
    out: list[str] = []
    for dotted in fields:
        value: Any = data
        for part in [p for p in str(dotted).split(".") if p]:
            if isinstance(value, list):
                value = value[int(part)] if part.isdigit() and int(part) < len(value) else None
            else:
                value = value.get(part) if isinstance(value, Mapping) else None
        if value not in (None, "", [], {}):
            out.append(str(value))
    return out


def verdict(results: Sequence[Mapping[str, Any]]) -> tuple[bool | None, str]:
    """``(True, …)`` every task asked passed; ``(False, why)`` one failed; ``(None, …)`` nothing was asked."""
    if not results:
        return None, "no usage task applies to this server"
    misfits = [r for r in results if not fits(r)]
    results = [r for r in results if fits(r)]
    if not results:
        return None, f"no usage task fits this server ({len(misfits)} {NOT_FITTING})"
    decided = [r for r in results if r.get("passed") is not None]
    if not decided:
        return None, "every usage task was inconclusive: the service was busy or never reached -- try again later"
    failed = [r for r in decided if r.get("passed") is False]
    if failed:
        first = failed[0]
        return False, (
            f"usage task {first.get('task', '?')!r} failed: {first.get('tool') or 'its tool'} "
            f"{first.get('detail') or 'did not do the job'}"
        )
    return True, f"{len(decided)} usage task(s) passed"


def fits(row: Mapping[str, Any]) -> bool:
    """Was a task's call one the server could be asked -- not refused as a request it does not have?"""
    return not str(row.get("detail") or "").startswith(NOT_FITTING)


def _misfit(reply: Any, settings: Any) -> bool:
    if settings is not None and not settings.bool_("mcp.contract.misfit_is_not_a_failure", True):
        return False
    markers = (
        settings.str_list("mcp.contract.not_fitting_markers", list(DEFAULT_NOT_FITTING_MARKERS))
        if settings is not None
        else list(DEFAULT_NOT_FITTING_MARKERS)
    )
    text = _text(reply).lower()
    if isinstance(reply, Mapping) and isinstance(reply.get("error"), Mapping):
        text += " " + str(reply["error"].get("message") or "").lower()
    return any(str(m).lower() in text for m in markers if str(m).strip())


def score(candidate: Any) -> int:
    """How much of the contract a candidate passed: the tasks, then an accepted login as a tie-breaker."""
    shake = getattr(candidate, "handshake", None)
    if shake is None:
        return 0
    passed = sum(2 for r in shake.contract if r.get("passed"))
    return passed + (1 if shake.login == login_check.ACCEPTED else 0)


def _task(raw: Mapping[str, Any], source: str) -> Task:
    tools = raw.get("tools") if isinstance(raw.get("tools"), list) else [raw.get("tool") or ""]
    return Task(
        name=str(raw.get("name") or ""),
        tools=[str(t) for t in tools if str(t).strip()],
        describes=[str(t) for t in raw.get("describes") or [] if str(t).strip()],
        answer_markers=[str(t) for t in raw.get("answer_markers") or [] if str(t).strip()],
        arguments={str(k): v for k, v in (raw.get("arguments") or {}).items()},
        expect=[str(e) for e in raw.get("expect") or [] if str(e).strip()],
        truth=dict(raw.get("truth") or {}),
        source=source,
        needs_login=bool(raw.get("needs_login", False)),
    )


def _same_host(key: str, host: str) -> bool:
    key = key.strip().lower()
    key = re.sub(r"^[a-z]+://", "", key).split("/", 1)[0]
    return bool(host) and key == host.strip().lower()


def _fill(value: Any, host: str) -> Any:
    return value.replace("{host}", host) if isinstance(value, str) else value


def _search(pattern: str, text: str) -> bool:
    try:
        return re.search(pattern, text, re.IGNORECASE) is not None
    except re.error:
        return False


def _text(reply: Any) -> str:
    result = reply.get("result") if isinstance(reply, Mapping) else None
    if not isinstance(result, Mapping):
        return ""
    return " ".join(str(p.get("text") or "") for p in result.get("content") or [] if isinstance(p, Mapping))


__all__ = [
    "NOT_FITTING",
    "Task",
    "askable",
    "by_answer",
    "carries",
    "enabled",
    "fits",
    "judge",
    "product_tasks",
    "resolve",
    "scope_of",
    "score",
    "tasks_for",
    "truth_values",
    "unmatched",
    "verdict",
]
