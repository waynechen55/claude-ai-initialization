"""The last check: ask Claude Code itself to use the entry, the way the operator will.

The program trials a server by launching it itself, and Claude Code launches it
differently: ``${NAME}`` expands only from the environment Claude Code started
with, its own working directory and trust stores apply, and so on. Several
failures surfaced only there -- a token exported inside a session never reached
the server; build-example-net's ``${BUILD_EXAMPLE_NET_BASE64_OF_USER_PASSWORD}`` held
nothing until Claude Code was restarted.

So, once an entry is written, a short non-interactive Claude Code session
(``claude -p``) is given *exactly that entry* (``--strict-mcp-config``) and
asked to call one usage task's tool. The program reads the tool's own answer
from the session's event stream and judges it with the usage contract's rules
-- the model's summary is never the verdict.

Before any request, the variables the entry reads are looked for in the
environment the check runs in (the shell phase 4 runs from -- the one a Claude
Code started from it would get). A missing one is reported as such, with no
model request spent. A check that cannot run at all (no ``claude``, not logged
in, out of quota, a timeout) is *inconclusive*: it never fails an entry.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from ..core import diskspace
from ..core.config import fill
from ..core.context import RunContext
from ..core.fileio import write_private
from ..core.logging_setup import get_logger
from . import contract

log = get_logger("mcp.claude_check")

PASSED = "passed"
FAILED = "failed"
MISSING_VARIABLE = "missing_variable"
COULD_NOT_RUN = "could_not_run"

#: ``${NAME}`` without a default -- the variables an entry needs from the environment.
_REFERENCE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


@dataclass
class Outcome:
    outcome: str
    detail: str
    tool: str = ""
    #: Why a connection failed, as Claude Code said it, and its class (:func:`classify`).
    reason: str = ""
    reason_class: str = ""

    def to_dict(self) -> dict[str, str]:
        out = {"outcome": self.outcome, "detail": self.detail, "tool": self.tool}
        if self.reason_class:
            out.update(reason=self.reason, reason_class=self.reason_class)
        return out


#: Fallback for ``[mcp.registration.claude_check.failure_classes]``: what a
#: connection failure Claude Code reports is about, by its words -- so the
#: repair fits it. openqa-example-net was "could not connect" in the log, while
#: ``claude mcp list`` said UNABLE_TO_GET_ISSUER_CERT_LOCALLY: a trust failure.
#: First match wins; anything else is "server".
FAILURE_CLASSES = {
    "trust": [
        r"UNABLE_TO_GET_ISSUER_CERT",
        r"UNABLE_TO_VERIFY_LEAF_SIGNATURE",
        r"SELF_SIGNED_CERT",
        r"CERT_(UNTRUSTED|HAS_EXPIRED|NOT_YET_VALID|SIGNATURE_FAILURE)",
        r"unable to verify the first certificate",
        r"(?i)certificate (verify|has expired|is not trusted)",
        r"(?i)\bissuer certificate\b",
    ],
    # Reached, and what answered is not an MCP server: a web page, a login
    # form. confluence.example.com's /mcp gave Claude Code "Unexpected content
    # type: text/html" -- no credential turns a web page into an MCP server.
    "not_mcp": [
        r"CLIENT_HTTP_UNEXPECTED_CONTENT",
        r"(?i)unexpected content type",
        r"(?i)\btext/html\b",
        r"(?i)not (a )?json-?rpc",
    ],
    "credential": [
        r"\b401\b",
        r"\b403\b",
        r"(?i)unauthori[sz]ed",
        r"(?i)authorization header",
        r"(?i)needs authentication",
        r"(?i)forbidden",
    ],
    "temporary": [r"(?i)timed? ?out", r"ECONNRESET", r"ETIMEDOUT", r"EAI_AGAIN", r"\b429\b", r"\b50[234]\b"],
}

#: Fallback for ``[mcp.registration.fresh_env] shell_variables``: variables a new
#: terminal takes from the shell profiles, which a check through Claude Code
#: must see as that terminal does -- the profiles exported a NODE_EXTRA_CA_CERTS
#: naming a deleted file, and nothing here looked.
SHELL_VARIABLES = ("NODE_EXTRA_CA_CERTS", "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE")
SHELL_COMMAND = ("bash", "-lic", "{probe}")
_SHELL_MARKER = "__ICA_SHELL__"


def classify(reason: str, settings: Any) -> str:
    """The class of a connection failure (``trust``, ``credential``, ``temporary``, ``server``); "" with no reason."""
    text = str(reason or "")
    if not text.strip():
        return ""
    classes = settings.dict_("mcp.registration.claude_check.failure_classes", FAILURE_CLASSES) or FAILURE_CLASSES
    for name, patterns in classes.items():
        if any(p and re.search(str(p), text) for p in (patterns or [])):
            return str(name)
    return "server"


def shell_values_each(ctx: Any, names: list[str] | tuple[str, ...]) -> dict[str, dict[str, str]]:
    """``{shell kind: {name: value}}`` -- what each kind of new terminal ends with, after every profile it reads.

    The kinds are ``[machine_env.effective_shells]`` -- the one definition of a
    new terminal, shared with the per-machine writer: a login shell (SSH, a
    console, ``su -``) reads ``~/.profile`` and ``/etc/profile.d``, an ordinary
    terminal window reads ``~/.bashrc``. On one machine the first exported
    ``SSL_CERT_FILE`` as a deleted /tmp bundle and the second the system's; a
    check that asked only one of them saw half the machine. With
    ``[mcp.registration.fresh_env] use_effective_shells = false``, the single
    ``shell_command`` is asked instead. Asked once per run per name.
    """
    config = ctx.settings.section("mcp.registration.fresh_env")
    if not names or not config.bool_("include_shell_profiles", True):
        return {}
    if config.bool_("use_effective_shells", True):
        from ..core.machine_env import EFFECTIVE_SHELLS

        shells = ctx.settings.dict_("machine_env.effective_shells", EFFECTIVE_SHELLS) or EFFECTIVE_SHELLS
    else:
        shells = {"a new terminal": config.list_("shell_command", list(SHELL_COMMAND))}
    cache = ctx.state.setdefault("fresh_shell_values_each", {})
    keep = {k: v for k, v in os.environ.items() if k in ("HOME", "USER", "LOGNAME", "PATH", "LANG", "TERM", "SHELL")}
    out: dict[str, dict[str, str]] = {}
    for kind, argv in shells.items():
        known = cache.setdefault(str(kind), {})
        wanted = [n for n in names if n not in known]
        if wanted:
            probe = "; ".join(f"printf '\\n{_SHELL_MARKER}{n}=%s' \"${{{n}}}\"" for n in wanted)
            command = [str(a).replace("{probe}", probe) for a in (argv or [])]
            got: dict[str, str] = {}
            if command:
                try:
                    done = subprocess.run(
                        command,
                        capture_output=True,
                        text=True,
                        stdin=subprocess.DEVNULL,  # noqa: S603
                        timeout=config.float_("shell_timeout_seconds", 20.0),
                        env=keep,
                    )
                    for line in (done.stdout or "").splitlines():
                        if line.startswith(_SHELL_MARKER) and "=" in line:
                            key, _, value = line[len(_SHELL_MARKER) :].partition("=")
                            got[key] = value
                except (OSError, subprocess.TimeoutExpired):
                    pass
            for n in wanted:
                known[n] = got.get(n, "")
        out[str(kind)] = {n: known[n] for n in names if known.get(n)}
    return out


def shell_values(ctx: Any, names: list[str] | tuple[str, ...]) -> dict[str, str]:
    """What a new terminal gives Claude Code for ``names`` -- every kind of new terminal (:func:`shell_values_each`).

    Where the kinds agree, that value. Where they disagree, the one that cannot
    work (a file that is not there, or holds no certificate) when one cannot --
    a check must see the terminal that fails, not the one that happens to
    work -- else the first kind's; the disagreement is logged once, naming both.
    """
    from .consumer_trust import file_problem

    each = shell_values_each(ctx, names)
    out: dict[str, str] = {}
    said = ctx.state.setdefault("fresh_shell_disagreements", set())
    for n in names:
        seen = {kind: values[n] for kind, values in each.items() if values.get(n)}
        if not seen:
            continue
        distinct = list(dict.fromkeys(seen.values()))
        if len(distinct) == 1:
            out[n] = distinct[0]
            continue
        broken = [v for v in distinct if v.startswith(("/", "~")) and file_problem(v)]
        out[n] = broken[0] if broken else distinct[0]
        if n not in said:
            said.add(n)
            log.info(
                "new terminals disagree on %s: %s -- judged with %s",
                n,
                "; ".join(f"{kind}: {value}" for kind, value in seen.items()),
                out[n],
            )
    return out


#: Fallbacks for ``[mcp.registration] failed_markers`` / ``connected_markers`` /
#: ``degraded_patterns``, equal to the packaged values. A status Claude Code marks
#: with a warning -- ``! Connected · tools fetch failed — Request timed out`` --
#: is not a working server: its tools could not even be listed. build-opensuse-org's
#: proxy was "connected" that way and kept its place over a bridge that works.
FAILED_MARKERS = (
    "failed to connect",
    "connection failed",
    "not connected",
    "needs authentication",
    "tools fetch failed",
    "timed out",
)
CONNECTED_MARKERS = ("connected",)
DEGRADED_PATTERNS = (r"(^|[\s\-:])!\s*connected",)


def status_verdict(text: str, settings: Any) -> str:
    """What one Claude Code status says: ``"failed"``, ``"connected"``, or ``""`` when it says neither.

    The one reading every caller uses: failures and warnings first, so "failed
    to connect" and "! Connected · tools fetch failed" are never "connected".
    """
    marks = settings.section("mcp.registration")
    lowered = (text or "").lower()
    if any(m and m.lower() in lowered for m in marks.str_list("failed_markers", list(FAILED_MARKERS))):
        return "failed"
    for pattern in marks.str_list("degraded_patterns", list(DEGRADED_PATTERNS)):
        try:
            if re.search(pattern, lowered):
                return "failed"
        except re.error:
            continue
    if any(m and m.lower() in lowered for m in marks.str_list("connected_markers", list(CONNECTED_MARKERS))):
        return "connected"
    return ""


def enabled(settings: Any) -> bool:
    return settings.bool_("mcp.registration.check_through_claude", True)


def missing_variables(entry: Mapping[str, Any], environ: Mapping[str, str]) -> list[str]:
    """The ``${NAME}`` (no default) an entry reads that ``environ`` does not set."""
    texts = [
        str(entry.get("command") or ""),
        str(entry.get("url") or ""),
        *map(str, entry.get("args") or []),
        *map(str, (entry.get("env") or {}).values()),
        *map(str, (entry.get("headers") or {}).values()),
    ]
    names: list[str] = []
    for text in texts:
        for name in _REFERENCE.findall(text):
            if not environ.get(name) and name not in names:
                names.append(name)
    return names


#: Fallback for ``mcp.registration.fresh_env.keep``: the variables a terminal
#: has before anything the operator exported -- patterns, matched whole.
FRESH_ENV_KEEP = (
    "PATH",
    "HOME",
    "USER",
    "LOGNAME",
    "SHELL",
    "LANG",
    "LANGUAGE",
    "LC_.*",
    "TERM",
    "TZ",
    "TMPDIR",
    "XDG_.*",
    "https?_proxy",
    "HTTPS?_PROXY",
    "no_proxy",
    "NO_PROXY",
    "all_proxy",
    "ALL_PROXY",
)


def fresh_environ(ctx: RunContext) -> dict[str, str]:
    """The environment a Claude Code started in a new terminal has: no more than that.

    The system's own variables (``mcp.registration.fresh_env.keep``), what this
    program's environment file exports (a new shell reads it), and Claude
    Code's ``settings.json`` env (applied by Claude Code itself) -- never the
    rest of the shell this run happens to be in. build-example-net "passed"
    through Claude Code because this run's shell exported
    BUILD_EXAMPLE_NET_USERNAME; a new terminal had nothing, and it failed.
    """
    config = ctx.settings.section("mcp.registration.fresh_env")
    if not config.bool_("enabled", True):
        return dict(os.environ)
    patterns = [re.compile(f"(?:{p})\\Z") for p in config.str_list("keep", list(FRESH_ENV_KEEP)) if p]
    env = {k: v for k, v in os.environ.items() if any(p.match(k) for p in patterns)}
    if config.bool_("include_program_env_file", True):
        exported = getattr(getattr(ctx, "env", None), "values", None) or {}
        env.update({str(k): str(v) for k, v in exported.items() if v is not None and str(k) not in env})
    # What the shell profiles export for the variables a new terminal takes from them.
    env.update(shell_values(ctx, config.str_list("shell_variables", list(SHELL_VARIABLES))))
    env.update(_claude_settings_env(ctx))
    # Discovery runs unattended (noninteractive): the URL recorder goes along,
    # so a check through Claude Code cannot open a browser either.
    from . import noninteractive

    for name in ("BROWSER", noninteractive.variable(ctx)):
        if os.environ.get(name):
            env[name] = os.environ[name]
    return env


def _claude_settings_env(ctx: RunContext) -> dict[str, str]:
    """Claude Code's own settings ``env``: it expands an entry's ``${NAME}`` from it too.

    Every file this program writes an ``env`` into (:func:`.settings_writer.env_settings_paths`):
    a credential kept out of a shared project file sits in the private one,
    and a check that read only the shared file would call it missing.
    """
    from ..provision.settings_writer import SettingsReadError, SettingsWriter, env_settings_paths

    merged: dict[str, str] = {}
    for path in env_settings_paths(ctx):
        try:
            env = SettingsWriter(ctx, path).read().get("env") or {}
        except (SettingsReadError, OSError):
            continue
        if isinstance(env, Mapping):
            merged.update({str(k): str(v) for k, v in env.items()})
    return merged


def _session_env(ctx: RunContext) -> dict[str, str]:
    """What a ``claude -p`` session needs to reach its model from this shell (``mcp.registration.fresh_env.session_keep``).

    The model login is not the entry's business: a session through Claude Code
    must start, so the variables naming the model backend are carried; the
    entry's own variables are not.
    """
    config = ctx.settings.section("mcp.registration.fresh_env")
    patterns = [re.compile(f"(?:{p})\\Z") for p in config.str_list("session_keep", list(SESSION_KEEP)) if p]
    return {k: v for k, v in os.environ.items() if any(p.match(k) for p in patterns)}


#: Fallback for ``mcp.registration.fresh_env.session_keep``.
SESSION_KEEP = (
    "ANTHROPIC_.*",
    "CLAUDE_CODE_.*",
    "CLAUDE_CONFIG_DIR",
    "CLOUD_ML_REGION",
    "GOOGLE_.*",
    "AWS_.*",
    "VERTEX_.*",
    "GCLOUD_.*",
    "CLOUDSDK_.*",
)


def task_for(candidate: Any) -> tuple[str, dict[str, Any]]:
    """The tool and arguments to ask: a passed usage task, else the accepted login tool; ("", {}) if neither."""
    shake = getattr(candidate, "handshake", None)
    if shake is None:
        return "", {}
    for row in shake.contract:
        if row.get("passed") and row.get("tool"):
            return str(row["tool"]), dict(row.get("arguments") or {})
    if shake.login_tool and shake.login == "accepted":
        return shake.login_tool, {}
    return "", {}


def check(ctx: RunContext, name: str, entry: Mapping[str, Any], candidate: Any) -> Outcome:
    """Ask Claude Code to call one task's tool through ``entry``; judge the tool's own answer.

    A failure is asked again (``mcp.registration.claude_check.retries_on_failure``)
    before it is believed. Claude Code words a tool's error result its own way --
    github-com's ``get_me`` failed once on a slow first connection and came back
    as "has an output schema but did not return structured content", which no
    marker of a transient failure matches -- and one answer from a session that
    costs seconds is not a verdict on the entry.
    """
    retries = max(0, ctx.settings.section("mcp.registration.claude_check").int_("retries_on_failure", 1))
    outcome = _check_once(ctx, name, entry, candidate)
    while outcome.outcome == FAILED and retries > 0:
        retries -= 1
        log.info("%s: Claude Code check failed (%s); asking once more", name, outcome.detail)
        outcome = _check_once(ctx, name, entry, candidate)
    return outcome


def _check_once(ctx: RunContext, name: str, entry: Mapping[str, Any], candidate: Any) -> Outcome:
    """One :func:`check`: one Claude Code session, one tool call, judged."""
    settings = ctx.settings
    config = settings.section("mcp.registration.claude_check")
    fresh = fresh_environ(ctx)
    missing = missing_variables(entry, fresh)
    if missing:
        return Outcome(
            MISSING_VARIABLE,
            (
                f"a Claude Code started in a new terminal would not have {', '.join(missing)}"
                + (
                    f" (only this run's shell sets {', '.join(n for n in missing if os.environ.get(n))})"
                    if any(os.environ.get(n) for n in missing)
                    else ""
                )
                + f": set {'it' if len(missing) == 1 else 'them'} in Claude Code's settings.json env, or export "
                f"{'it' if len(missing) == 1 else 'them'} in the shell profile you start Claude Code from"
            ),
        )
    tool, arguments = task_for(candidate)
    if not tool:
        return Outcome(COULD_NOT_RUN, "no usage task or accepted login gave a tool to ask")
    claude = ctx.tool("claude", config_key="paths.claude")
    if not claude:
        return Outcome(COULD_NOT_RUN, "the claude CLI is not installed", tool)
    qualified = f"mcp__{name}__{tool}"
    with diskspace.temporary_directory("ica-claude-check-") as scratch:
        config_file = Path(scratch) / "mcp.json"
        write_private(config_file, json.dumps({"mcpServers": {name: dict(entry)}}))
        tokens = {
            "claude": claude,
            "config": str(config_file),
            "tool": qualified,
            "prompt": fill(
                config.str_("prompt", DEFAULT_PROMPT), {"tool": qualified, "arguments": json.dumps(arguments)}
            ),
            "max_turns": str(config.int_("max_turns", 4)),
        }
        argv = [fill(str(part), tokens) for part in config.list_("command", list(DEFAULT_COMMAND))]
        model = config.str_("model", "").strip()
        if model:
            argv += ["--model", model]
        try:
            run = subprocess.run(  # noqa: S603 - argv list, never a shell
                argv,
                capture_output=True,
                text=True,
                stdin=subprocess.DEVNULL,
                timeout=config.float_("timeout_seconds", 180.0),
                cwd=os.getcwd(),
                env={**fresh, **_session_env(ctx)},
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return Outcome(COULD_NOT_RUN, f"Claude Code did not finish ({type(exc).__name__})", tool)
    return judge_stream(run.stdout, name, qualified, run.returncode, settings, tool)


#: Fallback for ``mcp.registration.claude_check.waiting_markers``: statuses that
#: mean the server is there and asks for the credential its slot waits for.
WAITING_MARKERS = ("needs authentication", "401", "unauthorized")
#: Fallback for ``mcp.registration.claude_check.status_pattern``: the line of
#: ``claude mcp get`` that carries the connection status.
STATUS_PATTERN = r"(?im)^\s*status:\s*(?P<status>.+)$"


def connect_check(ctx: RunContext, name: str, entry: Mapping[str, Any], *, waiting: bool = False) -> Outcome:
    """Does Claude Code itself connect to ``entry``? No model request, no credential needed.

    The entry is added to a throw-away configuration directory -- holding a
    copy of the operator's ``settings.json``, so its ``env`` (``MCP_TIMEOUT``,
    a trust store) applies as it will -- and ``claude mcp get`` asked for its
    status: Claude Code starts it with its own loader, environment and
    start-up wait. gitlab-example-net's mcp-graphql started in the program's trial
    and timed out after Claude Code's 30 s, and the only check through Claude
    Code was skipped because no tool could be called without a token.
    (``mcp.registration.claude_check.connect``.)
    """
    settings = ctx.settings
    config = settings.section("mcp.registration.claude_check")
    if not config.bool_("connect", True):
        return Outcome(COULD_NOT_RUN, "the connection check is off")
    claude = ctx.tool("claude", config_key="paths.claude")
    if not claude:
        return Outcome(COULD_NOT_RUN, "the claude CLI is not installed")
    registration = settings.section("mcp.registration")
    variable = registration.str_("config_dir_variable", "CLAUDE_CONFIG_DIR")
    payload = json.dumps(dict(entry), separators=(",", ":"))
    tokens = {"claude": claude, "name": name, "json": payload, "scope": "user"}
    add = [
        fill(str(t), tokens)
        for t in registration.list_("add_command", ["{claude}", "mcp", "add-json", "{name}", "{json}"])
    ]
    add += [fill(str(t), tokens) for t in registration.list_("scope_flag", ["--scope", "{scope}"])]
    get = [fill(str(t), tokens) for t in registration.list_("get_command", ["{claude}", "mcp", "get", "{name}"])]
    real_settings = Path(getattr(ctx, "claude_home", None) or Path.home() / ".claude") / "settings.json"
    with diskspace.temporary_directory("ica-claude-connect-") as scratch:
        if real_settings.is_file():
            try:
                write_private(Path(scratch) / "settings.json", real_settings.read_text(encoding="utf-8"))
            except OSError:
                pass
        environ = {**fresh_environ(ctx), variable: scratch}
        try:
            added = subprocess.run(
                add, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=60, cwd=os.getcwd(), env=environ
            )  # noqa: S603 - argv list
            if added.returncode != 0:
                said = " ".join((added.stderr or added.stdout or "").split())[-200:]
                return Outcome(FAILED, f"Claude Code refuses the entry ({said or 'no reason given'})")
            asked = subprocess.run(
                get,
                capture_output=True,
                text=True,
                stdin=subprocess.DEVNULL,
                timeout=config.float_("connect_timeout_seconds", 300.0),
                cwd=os.getcwd(),
                env=environ,
            )  # noqa: S603 - argv list
        except (OSError, subprocess.TimeoutExpired) as exc:
            return Outcome(COULD_NOT_RUN, f"Claude Code did not answer ({type(exc).__name__})")
        match = re.search(config.str_("status_pattern", STATUS_PATTERN), asked.stdout or "")
        said = " ".join((match.group("status") if match else "").split())
        reason = _failure_reason(ctx, name, environ) if status_verdict(said, settings) == "failed" else ""
    match = re.search(config.str_("status_pattern", STATUS_PATTERN), asked.stdout or "")
    status = " ".join((match.group("status") if match else "").split())
    lowered = status.lower()
    if waiting and any(m and m in lowered for m in config.str_list("waiting_markers", list(WAITING_MARKERS))):
        # Registered with its credential slot empty: asking for it is the answer expected.
        return Outcome(MISSING_VARIABLE, f"Claude Code reaches it and it asks for its credential ({status})")
    verdict = status_verdict(status, settings)
    kind = classify(reason, settings) if verdict == "failed" else ""
    if waiting and kind == "credential":
        # Reached it, and it turned the empty credential away: what a server
        # waiting for its credential is expected to say.
        return Outcome(
            MISSING_VARIABLE,
            f"Claude Code reaches it and it asks for its credential ({reason})",
            reason=reason,
            reason_class=kind,
        )
    if verdict == "failed":
        because = f": {reason} -- a {kind} failure" if reason else ""
        return Outcome(
            FAILED, f"Claude Code could not connect to it ({status}{because})", reason=reason, reason_class=kind
        )
    if verdict == "connected":
        return Outcome(PASSED, f"Claude Code connects to it ({status})")
    return Outcome(COULD_NOT_RUN, f"Claude Code gave no status ({status or 'none'})")


#: Fallback for ``mcp.registration.claude_check.list_command``: the listing that
#: says *why* a server failed (``claude mcp get`` gives only "Failed to connect").
LIST_COMMAND = ("{claude}", "mcp", "list")


def _failure_reason(ctx: RunContext, name: str, environ: Mapping[str, str]) -> str:
    """Why Claude Code could not connect to ``name``, in its own words from ``claude mcp list``; "" if unsaid."""
    config = ctx.settings.section("mcp.registration.claude_check")
    if not config.bool_("read_failure_reason", True):
        return ""
    claude = ctx.tool("claude", config_key="paths.claude")
    command = [fill(str(t), {"claude": claude, "name": name}) for t in config.list_("list_command", list(LIST_COMMAND))]
    try:
        listed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,  # noqa: S603
            timeout=config.float_("connect_timeout_seconds", 300.0),
            cwd=os.getcwd(),
            env=dict(environ),
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    for line in (listed.stdout or "").splitlines():
        if line.strip().startswith(f"{name}:"):
            _, sep, why = line.partition(" — ")
            return " ".join(why.split()) if sep else ""
    return ""


def judge_stream(stdout: str, name: str, qualified: str, returncode: int, settings: Any, tool: str = "") -> Outcome:
    """Read a ``stream-json`` session: the server's status, then the tool's own answer."""
    status = ""
    asked: set[str] = set()
    answer: Any = None
    result_error = ""
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("type") == "system" and event.get("subtype") == "init":
            for server in event.get("mcp_servers") or []:
                if server.get("name") == name:
                    status = str(server.get("status") or "")
        message = event.get("message") if isinstance(event.get("message"), Mapping) else {}
        for part in message.get("content") or [] if isinstance(message.get("content"), list) else []:
            if not isinstance(part, Mapping):
                continue
            if part.get("type") == "tool_use" and part.get("name") == qualified:
                asked.add(str(part.get("id")))
            if part.get("type") == "tool_result" and str(part.get("tool_use_id")) in asked:
                answer = part
        if event.get("type") == "result" and event.get("is_error"):
            result_error = str(event.get("result") or event.get("subtype") or "error")[:200]
    if status and status != "connected":
        return Outcome(FAILED, f"Claude Code could not connect to it ({status})", tool)
    if answer is None:
        why = result_error or (f"exit {returncode}" if returncode else "the tool was never called")
        return Outcome(COULD_NOT_RUN, f"Claude Code did not reach the tool ({why})", tool)
    content = answer.get("content")
    parts = content if isinstance(content, list) else [{"type": "text", "text": str(content or "")}]
    reply = {"result": {"isError": bool(answer.get("is_error")), "content": parts}}
    passed, detail = contract.judge(reply, {"expect": []}, settings)
    if passed is None:
        return Outcome(COULD_NOT_RUN, f"through Claude Code: {detail}", tool)
    return Outcome(PASSED if passed else FAILED, f"through Claude Code: {detail}", tool)


#: Fallbacks for ``[mcp.registration.claude_check]``, equal to the packaged values.
DEFAULT_COMMAND = (
    "{claude}",
    "-p",
    "--output-format",
    "stream-json",
    "--verbose",
    "--strict-mcp-config",
    "--mcp-config",
    "{config}",
    "--allowedTools",
    "{tool}",
    "ToolSearch",
    "--max-turns",
    "{max_turns}",
    "{prompt}",
)
DEFAULT_PROMPT = (
    "Call the tool {tool} exactly once with these arguments: {arguments}. Do not call any other tool, "
    "except ToolSearch to load it. Then reply DONE."
)

__all__ = [
    "COULD_NOT_RUN",
    "FAILED",
    "MISSING_VARIABLE",
    "PASSED",
    "Outcome",
    "check",
    "connect_check",
    "judge_stream",
    "missing_variables",
    "task_for",
]
