"""Getting a Claude agent to run, whatever this machine happens to have.

Phase 4 is the agentic phase, but it must still work on a host where the agent
SDK was never installed, or where the CLI is a different version from the one
this code was written against.  So the runtime is negotiated, not assumed:

``sdk``   ``claude_agent_sdk`` in-process -- richest: typed subagents, tool
          permissions and structured messages.
``cli``   shell out to ``claude -p --output-format json`` -- always available
          once phase 1 has run.
``none``  no runtime; phase 4 falls back to :mod:`initialize_claude_ai.mcp_auto_discovery.traditional`.

The CLI runtime introspects ``claude --help`` and only passes flags the installed
binary actually advertises.  That is why a missing ``--max-turns`` on an older or
newer build degrades to "no turn cap" instead of "unrecognised option, run
aborted".
"""

from __future__ import annotations

import functools
import json
import os
import re
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from ..core import redact
from ..core.context import RunContext
from ..core.logging_setup import get_logger
from ..core.report import format_duration

log = get_logger("mcp.runtime")

#: Called while an agent is still working, with one short line each time.
#: A stage can take many minutes and produce nothing until it ends, which is
#: indistinguishable from a hang; this is how the caller says otherwise.
ProgressHook = Callable[[str], None]

#: The wordings of each failure class, as regular expressions matched
#: case-insensitively. Packaged as ``[mcp.agent.failure_patterns]``, which is
#: what the agentic pipeline passes to :func:`failure_kind`; this is its
#: fallback and equals it. Keyed by class:
#:
#: ``refusal`` -- the request was rejected before the agent ever saw it, so
#: sending the same words again changes nothing. Only a differently worded
#: prompt, or a different model, can get through -- which is what the refusal
#: itself advises. Deliberately narrow about the word "refused": "connection
#: refused" is the network saying no, which is transient, and a bare
#: ``refus(ed|al)`` would catch it and send the wrong remedy.
#:
#: ``timeout`` -- the agent was still working when the clock ran out. Distinct
#: from a transient failure even though it looks like one: nothing was
#: overloaded and nothing said no, the brief was simply larger than the time
#: allowed. Sending the same brief again buys the same wall-clock spend and the
#: same ending, so this class earns its own remedy -- a narrower brief and a
#: smaller turn budget -- rather than a straight repeat.
#:
#: ``transient`` -- a failure about this moment rather than this request: the
#: same prompt sent again stands a real chance.
#:
#: ``unusable`` -- the runtime itself cannot serve *any* request: the model is
#: not enabled on this deployment, the credential is rejected, the quota is
#: gone. Facts about the installation, identical for every stage and every
#: target -- so unlike every other class here, the remedy is not a different
#: attempt. It is to stop attempting.
#:
#: A whole run was lost to the absence of that last class. ``claude-opus-5-5``
#: was not enabled on the operator's Vertex project, so every agent call
#: returned ``404`` with the fix in its own text -- "Try --model to switch to
#: claude-opus-5". It matched nothing here (note "not available" rather than
#: "unavailable"), so it was classed ``other``, retried three times a stage,
#: and silently degraded thirteen targets to the deterministic pipeline. The
#: sentence naming the remedy appeared twenty-nine times in the log and never
#: once as a conclusion.
FAILURE_PATTERNS: dict[str, tuple[str, ...]] = {
    "unsupported_model": (
        r"does not support (?:this|the) model",
        r"model .{0,60}(?:is not supported|unsupported)",
        r"unrecognized_model",
    ),
    "unusable": (
        r"not available on your",
        r"is not available\b.*\bdeployment",
        r"model .{0,60}(?:not found|does not exist|is not enabled)",
        r"\b404\b.*model",
        r"model.*\b404\b",
        r"invalid[_ ]api[_ ]key",
        r"authentication[_ ]error",
        r"not authenticated",
        r"\b40[13]\b.*(?:credential|api key|permission)",
        r"quota (?:exceeded|exhausted)",
        r"billing",
    ),
    "refusal": (
        r"safeguards",
        r"flagged this (?:message|request)",
        r"legal/aup",
        r"can(?:no|')t respond to this",
        r"refused this (?:message|request)",
        r"request was refused",
        r"declined to (?:respond|answer|help)",
    ),
    "timeout": (
        r"exceeded mcp\.agent\.timeout_seconds",
        r"timed out",
        r"timeout",
        r"deadline exceeded",
    ),
    "transient": (
        r"overloaded",
        r"rate.?limit",
        r"too many requests",
        r"temporar",
        r"unavailable",
        r"connection (?:reset|refused|error)",
        r"\b50[234]\b",
    ),
}


@functools.lru_cache(maxsize=32)
def _compiled(patterns: tuple[str, ...]) -> re.Pattern[str] | None:
    """One case-insensitive pattern matching any of ``patterns``; None for none."""
    kept = [f"(?:{p})" for p in patterns if p]
    return re.compile("|".join(kept), re.IGNORECASE) if kept else None


#: Failure classes, in the order they are tested. ``UNUSABLE`` is first: it is
#: the only one that says something about the run rather than the request, and
#: several of its wordings would otherwise be caught by ``TRANSIENT``.
UNUSABLE, REFUSAL, TIMEOUT, TRANSIENT, OTHER = (
    "unusable",
    "refusal",
    "timeout",
    "transient",
    "other",
)
#: The runtime itself is too old for the model: "Claude Code 2.1.259 does not
#: support this model". Not the deployment's fault and not the request's -- the
#: other runtime (another Claude Code) may serve it, else the runtime's own
#: default model does. Tested before ``UNUSABLE``.
UNSUPPORTED_MODEL = "unsupported_model"


#: Keys a runtime puts its human-readable failure under in the JSON it prints.
#: Tried in order; the first non-empty string wins.
FAILURE_MESSAGE_KEYS = ("result", "error", "message", "detail")


def _failure_message(raw: str, limit: int) -> str:
    """The sentence a person needs out of a failed run's output.

    ``claude -p --output-format json`` prints a whole result object even when it
    fails, and the reason lives in one field of it. This used to take the *last*
    600 characters, which for a JSON document is guaranteed to start mid-key --
    so what reached the report, twenty-nine times in one run, was::

        ackground":0,"foreground":0,"unset":0},"started_in_background":0,...

    while the field that mattered read "The model claude-opus-5-5 is not
    available on your vertex deployment. Try --model to switch to
    claude-opus-5." The fix was in the run's own output and never once legible,
    and the classifier that should have caught it was reading the same slice.

    Falls back to the tail of the raw text, which is the right answer for a
    stack trace, where the last lines are the informative ones.
    """
    text = str(raw or "").strip()
    if not text:
        return ""
    found = json_message(text)
    return found[1][:limit] if found else text[-limit:]


def json_message(text: str) -> tuple[str, str] | None:
    """``(what precedes it, its message)`` for a JSON object embedded in ``text``.

    The message is the first of :data:`FAILURE_MESSAGE_KEYS` holding a
    non-empty string. ``None`` when there is no object, it does not parse, or
    it names no message -- the caller keeps the text as it was.
    """
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        payload = json.loads(text[start : end + 1])
    except (ValueError, TypeError):
        return None
    if not isinstance(payload, Mapping):
        return None
    for key in FAILURE_MESSAGE_KEYS:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return text[:start].strip(), value.strip()
    return None


def failure_kind(
    error: str,
    unusable_markers: Sequence[str] = (),
    patterns: Mapping[str, Sequence[str]] | None = None,
) -> str:
    """What sort of failure this is, which decides how to try again.

    A stage that gives up on the first error is not autonomous, but neither is
    one that retries identically forever: the classes get different second
    attempts -- reword, shrink, repeat, change approach -- except
    :data:`UNUSABLE`, whose answer is to stop and say so.

    ``patterns`` are the class wordings (``mcp.agent.failure_patterns``; a
    class it leaves out keeps :data:`FAILURE_PATTERNS`). ``unusable_markers``
    adds plain installation-level wordings (``mcp.agent.unusable_markers`` and
    the credential-failure markers) on top, because the phrasing comes from
    whichever runtime is installed and not from anything this program controls.
    """
    text = str(error or "")
    if not text:
        return OTHER
    for marker in unusable_markers:
        if marker and str(marker).lower() in text.lower():
            return UNUSABLE
    configured = patterns or {}
    for kind in (UNSUPPORTED_MODEL, UNUSABLE, REFUSAL, TIMEOUT, TRANSIENT):
        wordings = configured.get(kind, FAILURE_PATTERNS[kind])
        wordings = [wordings] if isinstance(wordings, str) else wordings
        compiled = _compiled(tuple(str(w) for w in wordings or ()))
        if compiled is not None and compiled.search(text):
            return kind
    return OTHER


@dataclass
class AgentDefinitionSpec:
    """One subagent, in the shape both runtimes need."""

    name: str
    description: str
    prompt: str
    tools: list[str] = field(default_factory=list)
    model: str = ""
    #: Turn budget for this stage alone, overriding ``mcp.agent.max_turns``.
    #: Discovery is meant to be decisive -- batch the probes, do not wander --
    #: and a per-stage cap is how that stops being advice. 0 means "no override".
    max_turns: int = 0

    def turn_budget(self, default: int, override: int = 0) -> int:
        """How many turns this agent gets: ``--agent-max-turns``, then its own, then the default.

        ``override`` has to exist as a third input rather than being folded into
        ``default``, because ``default`` loses. Every agent shipped here defines
        its own ``max_turns``, so ``mcp.agent.max_turns`` is a fallback that
        nothing currently reaches -- raising it changes nothing, which is the
        sort of setting that wastes an afternoon. A flag that only moved *that*
        number would be equally inert, so ``--agent-max-turns`` is a separate
        value that beats the per-agent ones outright.
        """
        return override or self.max_turns or default

    def to_cli_json(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"description": self.description, "prompt": self.prompt}
        if self.tools:
            payload["tools"] = self.tools
        if self.model:
            payload["model"] = self.model
        return payload


@dataclass
class AgentResult:
    """One agent turn, as the pipeline needs to see it.

    Both runtimes normalise to this, so nothing downstream has to know whether
    the SDK or the CLI produced it. ``ok`` means the turn completed, not that
    the answer was any good -- what the agent claims is still only a hypothesis
    until the verifier has been through ``payload``.
    """

    ok: bool = False
    runtime: str = ""
    text: str = ""
    #: The JSON object the agent was asked to emit, if it emitted one.
    payload: dict[str, Any] | None = None
    turns: int = 0
    cost_usd: float = 0.0
    duration: float = 0.0
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "runtime": self.runtime,
            "turns": self.turns,
            "cost_usd": round(self.cost_usd, 4),
            "duration_seconds": round(self.duration, 2),
            "error": redact.redact(self.error),
            "text_preview": redact.redact(self.text[:600]),
        }


#: Guards the lazy creation of each runtime's per-thread state.
_THREAD_STATE_GUARD = threading.Lock()


class AgentRuntime(ABC):
    """How a subagent is actually launched: in process, or as a subprocess.

    Two implementations, chosen at run time by ``mcp.agent.runtime`` and by
    what is installed -- :class:`SdkRuntime` and :class:`CliRuntime`. They are
    interchangeable on purpose: a defect in one is not a defect in agent mode,
    and a retry may swap runtimes mid-stage. Everything they differ about
    (flags, transport, streaming) stays behind this interface; everything they
    must agree on (the brief, the tools, the turn budget, the shape of the
    answer) is settings and :class:`AgentResult`.
    """

    name = ""

    def __init__(self, ctx: RunContext) -> None:
        self.ctx = ctx
        self.config = ctx.settings.section("mcp.agent")
        #: Set by the caller to retry a stage on a different model. A refusal
        #: names changing the model as one of its two remedies, and the other
        #: one -- rewording -- is the caller's job, not the runtime's.
        self.model_override = ""

    @property
    def model_override(self) -> str:
        """The retry model *for the calling thread*.

        Per thread because under ``--concurrency`` one runtime object drives
        several addresses at once, each on its own thread. When this was a
        plain attribute, one address's refusal re-pointed every other
        address's next request at a model chosen for somebody else's brief --
        and three refusals in quick succession rotated the run straight back to
        the model that had refused. One address at a time, there is one thread
        and nothing differs.
        """
        return getattr(self._per_thread(), "model", "")

    @model_override.setter
    def model_override(self, value: str) -> None:
        self._per_thread().model = str(value or "")

    def _per_thread(self) -> threading.local:
        # Created on first use rather than in __init__, so a subclass that does
        # not call super().__init__ still gets one.
        local = self.__dict__.get("_thread_state")
        if local is None:
            with _THREAD_STATE_GUARD:
                local = self.__dict__.setdefault("_thread_state", threading.local())
        return local

    @abstractmethod
    def available(self) -> tuple[bool, str]:
        """Return ``(usable, reason_if_not)``."""

    @abstractmethod
    def run(
        self,
        prompt: str,
        *,
        agent: AgentDefinitionSpec,
        workspace: Path,
        system_prompt: str = "",
        on_progress: ProgressHook | None = None,
    ) -> AgentResult:
        """Run one agent to completion and return what it produced.

        ``on_progress`` is optional and advisory: a runtime that cannot see
        inside a running agent simply never calls it.
        """

    # -- shared -----------------------------------------------------------------

    def time_limit(self) -> float:
        """Wall-clock ceiling for one agent, in seconds. 0 means no ceiling.

        Both runtimes read the same setting. A turn cap is not a time cap -- one
        turn can spend ten minutes in a web search -- so without this an agent
        stage has no upper bound at all, and an operator watching a silent
        terminal cannot tell a slow answer from a wedged one.
        """
        return max(0.0, self.config.float_("timeout_seconds", 900.0))

    def progress_interval(self) -> float:
        """How often to say "still working" while an agent is silent. 0 = never."""
        return max(0.0, self.config.float_("progress_interval_seconds", 30.0))

    def verbose(self) -> bool:
        """Report each thing the agent does, not just that it is still alive."""
        return self.config.bool_("verbose", False)

    def model(self) -> str:
        """The model to drive the agent with, retry override first; "" -- the runtime's default -- once it said it cannot serve the configured one."""
        if getattr(self, "configured_model_unsupported", False):
            return self.model_override
        return self.model_override or self.config.str_("model")

    def allowed_tools(self, agent: AgentDefinitionSpec) -> list[str]:
        configured = self.config.str_list("allowed_tools")
        if not agent.tools:
            return configured
        # Never widen beyond the phase-level allow list.
        return [t for t in agent.tools if not configured or t in configured] or configured


# ----------------------------------------------------------------------------
# In-process SDK
# ----------------------------------------------------------------------------


def add_venv_to_path(ctx: RunContext) -> str:
    """Make a virtualenv built by phase 1 importable from this process.

    Where the system interpreter is externally managed -- PEP 668, which is most
    distributions now -- phase 1 installs the Python SDKs into a virtualenv
    instead. That venv is not on this process's ``sys.path``, so without this the
    SDK that was just installed reads as missing and phase 4 quietly drops to the
    CLI runtime. Returns the directory added, or "".
    """
    import site
    import sys

    venv = ctx.env.get("PYTHON_VENV") or str(ctx.settings.path("install.venv_path") or (ctx.state_dir / "venv"))
    if not venv:
        return ""
    # This interpreter's version only. A venv built by a different python3 holds
    # wheels with compiled extensions -- rpds, under jsonschema, under the SDK --
    # that raise ModuleNotFoundError on import here. Better to report the SDK
    # missing and use the CLI runtime than to import half of it and fail later.
    version = f"python{sys.version_info.major}.{sys.version_info.minor}"
    packages = Path(venv) / "lib" / version / "site-packages"
    location = str(packages)
    if location in sys.path:
        return ""
    if not packages.is_dir():
        log.debug("no %s site-packages in %s; leaving sys.path alone", version, venv)
        return ""
    # addsitedir rather than sys.path.append, so .pth files in the venv are
    # processed too -- editable installs rely on them.
    site.addsitedir(location)
    log.debug("added phase-1 virtualenv to sys.path: %s", location)
    return location


#: ``mcp.agent.sdk_cli``: which Claude Code the SDK runtime drives.
SDK_CLI_MODES = ("auto", "bundled", "installed")
#: Asked of an interpreter: where the SDK keeps the Claude Code it ships with
#: (the SDK's own lookup, ``_find_bundled_cli``); "" when it ships none.
_BUNDLED_CLI_PROBE = (
    "import pathlib, platform, claude_agent_sdk\n"
    "p = pathlib.Path(claude_agent_sdk.__file__).parent / '_bundled' / "
    "('claude.exe' if platform.system() == 'Windows' else 'claude')\n"
    "print(p if p.is_file() else '')\n"
)


@dataclass
class SdkCli:
    """The Claude Code the SDK runtime drives, and the copies it was chosen from."""

    #: Passed as ``cli_path``; "" leaves the SDK to its own (bundled) copy.
    path: str = ""
    version: str = ""
    bundled: str = ""
    bundled_version: str = ""
    installed: str = ""
    installed_version: str = ""
    mode: str = "auto"
    why: str = ""

    @property
    def runs(self) -> str:
        """The binary that will actually run."""
        return self.path or self.bundled

    @property
    def runs_version(self) -> str:
        return self.version if self.path else self.bundled_version

    @property
    def behind(self) -> str:
        """Why the copy that runs is older than one this machine has; "" when it is not."""
        from ..install.verify import version_at_least, version_tuple

        newest = max((v for v in (self.bundled_version, self.installed_version) if v), key=version_tuple, default="")
        running = self.runs_version
        if not newest or not running or version_at_least(running, newest):
            return ""
        return f"the SDK runs Claude Code {running} ({self.runs}) while {newest} is installed here"


def sdk_cli(ctx: RunContext, *, python: str = "") -> SdkCli:
    """Which Claude Code the SDK runtime runs (``mcp.agent.sdk_cli``); asked once per interpreter per run.

    claude-agent-sdk 0.2.152 ships Claude Code 2.1.259, which answered "does not
    support this model" while the installed 2.1.309 -- the one phase 2 logs in
    and the CLI runtime uses -- served it: every SDK attempt failed first.

    * ``auto`` -- the installed CLI when it is at least as new as the bundled
      one (or nothing is bundled), else the bundled one; versions unknown,
      the SDK keeps its own;
    * ``installed`` -- the installed CLI whenever there is one;
    * ``bundled`` -- the SDK's own copy, always.

    ``python`` names the interpreter the SDK is installed in (phase 1 asks the
    one it installed into); "" asks this process.
    """
    from ..install.verify import parse_version, version_at_least

    cache = ctx.state.setdefault("mcp_sdk_cli", {})
    if python in cache:
        return cache[python]
    mode = ctx.settings.str_("mcp.agent.sdk_cli", "auto").strip().lower()
    if mode not in SDK_CLI_MODES:
        log.warning("mcp.agent.sdk_cli = %r is not one of %s; using auto", mode, ", ".join(SDK_CLI_MODES))
        mode = "auto"
    timeout = ctx.settings.float_("install.version_timeout_seconds", 25)
    bundled = ""
    if python:
        probe = ctx.runner.probe([python, "-c", _BUNDLED_CLI_PROBE], timeout=timeout)
        lines = (probe.stdout or "").strip().splitlines() if probe.ok else []
        bundled = lines[-1].strip() if lines else ""
    else:
        try:
            import claude_agent_sdk

            candidate = (
                Path(claude_agent_sdk.__file__).parent / "_bundled" / ("claude.exe" if os.name == "nt" else "claude")
            )
            bundled = str(candidate) if candidate.is_file() else ""
        except ImportError:
            bundled = ""
    installed = ctx.tool("claude", config_key="paths.claude") or ""

    def version_of(binary: str) -> str:
        if not binary:
            return ""
        return parse_version(ctx.runner.probe([binary, "--version"], timeout=timeout).output)

    choice = SdkCli(
        bundled=bundled,
        bundled_version=version_of(bundled),
        installed=installed,
        installed_version=version_of(installed),
        mode=mode,
    )
    if mode == "installed" and installed:
        choice.path, choice.version, choice.why = installed, choice.installed_version, "mcp.agent.sdk_cli = installed"
    elif (
        mode == "auto"
        and installed
        and (
            not bundled
            or (
                choice.installed_version
                and choice.bundled_version
                and version_at_least(choice.installed_version, choice.bundled_version)
            )
        )
    ):
        choice.path, choice.version = installed, choice.installed_version
        choice.why = (
            "the SDK ships no Claude Code of its own"
            if not bundled
            else f"the installed Claude Code {choice.installed_version} is at least as new as the bundled "
            f"{choice.bundled_version}"
        )
    else:
        choice.why = (
            "mcp.agent.sdk_cli = bundled"
            if mode == "bundled"
            else "the bundled Claude Code is newer, or a version could not be read"
            if bundled
            else "no Claude Code is installed for the SDK to run"
        )
    cache[python] = choice
    return choice


class SdkRuntime(AgentRuntime):
    """In-process agent via ``claude_agent_sdk``.

    Preferred when the package is importable: no subprocess, and the message
    stream arrives as it is produced, which is what lets a long stage report
    progress and what makes a timeout leave a partial transcript rather than
    nothing.
    """

    name = "sdk"

    def available(self) -> tuple[bool, str]:
        try:
            import claude_agent_sdk  # noqa: F401
        except ImportError:
            add_venv_to_path(self.ctx)
        try:
            import claude_agent_sdk  # noqa: F401
        except ImportError as exc:
            return False, f"claude-agent-sdk is not importable ({exc})"
        if not self.ctx.tool("claude", config_key="paths.claude"):
            # The SDK drives the CLI underneath; without it there is nothing to drive.
            return False, "the claude CLI is not installed"
        return True, ""

    def run(
        self,
        prompt: str,
        *,
        agent: AgentDefinitionSpec,
        workspace: Path,
        system_prompt: str = "",
        on_progress: ProgressHook | None = None,
    ) -> AgentResult:
        import asyncio

        started = time.monotonic()
        # What the CLI underneath writes to stderr: the SDK's own error only
        # says "Check stderr output for details", four times in one run, and
        # the reason was never seen. (mcp.agent.capture_stderr)
        self._stderr_lines: list[str] = []
        try:
            result = asyncio.run(self._run_async(prompt, agent, workspace, system_prompt, on_progress))
        except Exception as exc:  # noqa: BLE001 - surface, never crash the phase
            said = _failure_message("\n".join(self._stderr_lines[-40:]), 600) if self._stderr_lines else ""
            log.debug("SDK runtime failed%s", f" -- its CLI said: {said}" if said else "", exc_info=True)
            return AgentResult(
                runtime=self.name,
                error=f"{type(exc).__name__}: {exc}" + (f" -- the CLI said: {said}" if said else ""),
                duration=time.monotonic() - started,
            )
        result.duration = time.monotonic() - started
        return result

    async def _run_async(
        self,
        prompt: str,
        agent: AgentDefinitionSpec,
        workspace: Path,
        system_prompt: str,
        on_progress: ProgressHook | None = None,
    ) -> AgentResult:
        from claude_agent_sdk import AgentDefinition, ClaudeAgentOptions, query

        options_kwargs: dict[str, Any] = {
            "cwd": str(workspace),
            "allowed_tools": self.allowed_tools(agent),
            "permission_mode": self.config.str_("permission_mode", "acceptEdits"),
            "setting_sources": self.config.str_list("setting_sources", ["project"]),
            "agents": {
                agent.name: AgentDefinition(
                    description=agent.description,
                    prompt=agent.prompt,
                    tools=agent.tools or None,
                    model=agent.model or None,
                )
            },
        }
        if self.model():
            options_kwargs["model"] = self.model()
        budget = agent.turn_budget(
            self.config.int_("max_turns", 24),
            override=self.config.int_("max_turns_override", 0),
        )
        if budget:
            options_kwargs["max_turns"] = budget
        if self.config.str_list("disallowed_tools"):
            options_kwargs["disallowed_tools"] = self.config.str_list("disallowed_tools")
        if system_prompt:
            options_kwargs["system_prompt"] = system_prompt
        if self.config.bool_("capture_stderr", True):
            lines = getattr(self, "_stderr_lines", None)
            if lines is None:
                lines = self._stderr_lines = []
            keep = max(1, self.config.int_("stderr_lines", 200))

            def heard(line: str) -> None:
                lines.append(redact.redact(str(line).rstrip()))
                del lines[:-keep]

            options_kwargs["stderr"] = heard

        cli = sdk_cli(self.ctx)
        if cli.path:
            # The Claude Code phase 2 logged in and the CLI runtime runs -- not
            # an older copy the SDK ships, which can lack the configured model.
            options_kwargs["cli_path"] = cli.path
            log.debug("SDK runtime drives %s (%s)", cli.path, cli.why)

        options = ClaudeAgentOptions(**_supported_kwargs(ClaudeAgentOptions, options_kwargs))

        result = AgentResult(runtime=self.name)
        chunks: list[str] = []
        await self._consume(query(prompt=prompt, options=options), result, chunks, on_progress)

        result.text = "\n".join(chunks).strip()
        result.payload = extract_json(result.text)
        result.ok = bool(result.text) and not result.error
        return result

    async def _consume(
        self,
        stream: Any,
        result: AgentResult,
        chunks: list[str],
        on_progress: ProgressHook | None,
    ) -> None:
        """Read the agent's messages under a deadline, keeping what arrived.

        The deadline is enforced per message rather than around the whole
        iteration so that the remaining budget shrinks as the agent works, and
        so that a timeout leaves the partial transcript intact: an agent that
        found the answer and then ran long is worth more than nothing, and the
        caller falls back to the deterministic pipeline either way.
        """
        import asyncio

        limit = self.time_limit()
        started = time.monotonic()
        ticker = self._start_ticker(started, on_progress)
        try:
            while True:
                remaining = (limit - (time.monotonic() - started)) if limit else None
                if remaining is not None and remaining <= 0:
                    raise asyncio.TimeoutError
                try:
                    message = await asyncio.wait_for(stream.__anext__(), timeout=remaining)
                except StopAsyncIteration:
                    break
                self._absorb(message, result, chunks, on_progress)
        except (asyncio.TimeoutError, TimeoutError):
            result.error = f"the agent exceeded mcp.agent.timeout_seconds ({limit:g}s) and was stopped"
            log.debug("SDK agent timed out after %.1fs", time.monotonic() - started)
            if on_progress:
                on_progress(f"stopped after {format_duration(time.monotonic() - started)} (timeout)")
        finally:
            if ticker is not None:
                ticker.cancel()
            # The generator owns the CLI subprocess underneath; a cancelled
            # __anext__ leaves it running until the interpreter exits.
            aclose = getattr(stream, "aclose", None)
            if aclose is not None:
                try:
                    await aclose()
                except Exception:  # noqa: BLE001 - cleanup must not mask the result
                    log.debug("closing the SDK stream failed", exc_info=True)

    def _start_ticker(self, started: float, on_progress: ProgressHook | None) -> Any:
        """A heartbeat, so a long turn does not look like a hung process."""
        import asyncio

        interval = self.progress_interval()
        if on_progress is None or interval <= 0:
            return None

        async def tick() -> None:
            while True:
                await asyncio.sleep(interval)
                on_progress(f"still working ({format_duration(time.monotonic() - started)})")

        return asyncio.ensure_future(tick())

    def _absorb(
        self,
        message: Any,
        result: AgentResult,
        chunks: list[str],
        on_progress: ProgressHook | None,
    ) -> None:
        text = _text_of(message)
        if text:
            chunks.append(text)
        usage = getattr(message, "total_cost_usd", None)
        if usage is not None:
            result.cost_usd = float(usage or 0.0)
        turns = getattr(message, "num_turns", None)
        if turns is not None:
            result.turns = int(turns or 0)
        if getattr(message, "is_error", False):
            result.error = text or "the agent reported an error"
        if on_progress is not None and self.verbose():
            activity = _activity_of(message)
            if activity:
                on_progress(activity)


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------


class CliRuntime(AgentRuntime):
    """Agent via ``claude -p`` as a subprocess.

    The route that works on a host where the SDK is not installed, and the
    fallback when the SDK fails. ``claude --help`` is parsed once so no unknown
    option is ever passed: the CLI's flags vary by version, and an unrecognised
    one turns a working stage into a usage error.
    """

    name = "cli"

    def __init__(self, ctx: RunContext) -> None:
        super().__init__(ctx)
        self._flags: set[str] | None = None
        #: How much of a failure message to keep. Enough for the sentence that
        #: names the remedy, which is what these messages are for.
        self.error_chars = ctx.settings.int_("mcp.agent.error_chars", 600)

    def available(self) -> tuple[bool, str]:
        if not self.ctx.tool("claude", config_key="paths.claude"):
            return False, "the claude CLI is not installed"
        return True, ""

    def supported_flags(self) -> set[str]:
        """Parse ``claude --help`` once so we never pass an unknown option."""
        if self._flags is not None:
            return self._flags
        claude = self.ctx.tool("claude", config_key="paths.claude")
        flags: set[str] = set()
        if claude:
            probe = self.ctx.runner.probe([claude, "--help"], timeout=self.config.float_("help_timeout_seconds", 45))
            if probe.ok:
                flags = set(re.findall(r"(--[a-zA-Z][a-zA-Z0-9-]*)", probe.stdout))
        self._flags = flags
        return flags

    def run(
        self,
        prompt: str,
        *,
        agent: AgentDefinitionSpec,
        workspace: Path,
        system_prompt: str = "",
        on_progress: ProgressHook | None = None,
    ) -> AgentResult:
        claude = self.ctx.tool("claude", config_key="paths.claude")
        if not claude:
            return AgentResult(runtime=self.name, error="the claude CLI is not installed")

        cli = self.config.section("cli")
        flags = self.supported_flags()
        argv: list[str] = [claude, "-p"]

        def add(flag: str, *values: str) -> None:
            if flag in flags:
                argv.append(flag)
                argv.extend(values)
            else:
                log.debug("installed claude does not support %s; continuing without it", flag)

        add("--output-format", cli.str_("output_format", "json"))
        if self.model():
            add("--model", self.model())
        budget = agent.turn_budget(
            self.config.int_("max_turns", 24),
            override=self.config.int_("max_turns_override", 0),
        )
        if budget:
            add("--max-turns", str(budget))
        add("--permission-mode", self.config.str_("permission_mode", "acceptEdits"))
        tools = self.allowed_tools(agent)
        if tools:
            add("--allowed-tools", ",".join(tools))
        if self.config.str_list("disallowed_tools"):
            add("--disallowed-tools", ",".join(self.config.str_list("disallowed_tools")))
        if self.config.str_list("setting_sources", ["project"]):
            add("--setting-sources", ",".join(self.config.str_list("setting_sources", ["project"])))
        add("--agents", json.dumps({agent.name: agent.to_cli_json()}))
        if system_prompt:
            add("--append-system-prompt", system_prompt)
        argv.extend(str(a) for a in cli.list_("extra_args", []))
        argv.append(prompt)

        started = time.monotonic()
        # mutating=True: the agent can write files, so a dry run must not run it.
        result = self.ctx.runner.run(
            argv,
            # 0, not None: the runner reads 0 as "no ceiling" and None as
            # "use the process-wide default", which is not what 0 means here.
            timeout=self.time_limit(),
            cwd=str(workspace),
            label=f"agent:{agent.name}",
        )
        outcome = AgentResult(runtime=self.name, duration=time.monotonic() - started)
        if result.simulated:
            outcome.error = "dry run: the agent was not invoked"
            return outcome
        if not result.ok:
            outcome.error = (
                _failure_message(result.stderr or result.stdout or "", self.error_chars) or f"rc={result.returncode}"
            )
            return outcome

        parsed = _parse_cli_output(result.stdout)
        outcome.text = parsed.get("result") or result.stdout.strip()
        outcome.turns = int(parsed.get("num_turns") or 0)
        outcome.cost_usd = float(parsed.get("total_cost_usd") or 0.0)
        outcome.payload = extract_json(outcome.text)
        outcome.ok = bool(outcome.text) and not parsed.get("is_error")
        if parsed.get("is_error"):
            outcome.error = _failure_message(outcome.text, self.error_chars)
        return outcome


# ----------------------------------------------------------------------------
# selection
# ----------------------------------------------------------------------------


def available_runtimes(ctx: RunContext) -> tuple[list[AgentRuntime], str]:
    """Every runtime that is usable right now, best first.

    ``select_runtime`` answers "what should run the agent"; this answers "what
    else could run it if that one turns out to be broken". A caller that only
    knows the first answer has to give up the moment the SDK misbehaves, which
    turns a defect in one runtime into a defect in agent mode as a whole.

    Returns ``(runtimes, reason)``: the reason names the chosen runtime when
    there is one, and explains every rejection when there is not.
    """
    preference = ctx.settings.str_("mcp.agent.runtime", "auto").strip().lower()
    candidates: list[type[AgentRuntime]] = {
        "sdk": [SdkRuntime],
        "cli": [CliRuntime],
        "auto": [SdkRuntime, CliRuntime],
    }.get(preference, [SdkRuntime, CliRuntime])

    usable_runtimes: list[AgentRuntime] = []
    reasons: list[str] = []
    for factory in candidates:
        runtime = factory(ctx)
        usable, why = runtime.available()
        if usable:
            usable_runtimes.append(runtime)
        else:
            reasons.append(f"{runtime.name}: {why}")
    if usable_runtimes:
        return usable_runtimes, f"{usable_runtimes[0].name} runtime"
    return [], "; ".join(reasons) or "no agent runtime available"


def select_runtime(ctx: RunContext) -> tuple[AgentRuntime | None, str]:
    """Pick a runtime per ``mcp.agent.runtime``. Returns ``(runtime, reason)``."""
    runtimes, reason = available_runtimes(ctx)
    return (runtimes[0] if runtimes else None), reason


# ----------------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------------


def extract_json(text: str) -> dict[str, Any] | None:
    """Pull the JSON object an agent was asked to emit out of its prose.

    Agents wrap results in fences, add a sentence before them, or emit nothing
    but JSON. All three are handled; anything else returns None and the caller
    falls back to the free text.
    """
    if not text:
        return None
    fenced = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    for block in reversed(fenced):
        parsed = _try_json(block)
        if parsed is not None:
            return parsed
    stripped = text.strip()
    parsed = _try_json(stripped)
    if parsed is not None:
        return parsed
    # Last resort: the outermost braces in the message.
    start, end = stripped.find("{"), stripped.rfind("}")
    if 0 <= start < end:
        return _try_json(stripped[start : end + 1])
    return None


def _try_json(text: str) -> dict[str, Any] | None:
    try:
        value = json.loads(text)
    except (ValueError, TypeError):
        return None
    return value if isinstance(value, dict) else None


def _parse_cli_output(stdout: str) -> dict[str, Any]:
    payload = _try_json((stdout or "").strip())
    if payload is not None:
        return payload
    for line in reversed((stdout or "").splitlines()):
        parsed = _try_json(line.strip())
        if parsed is not None:
            return parsed
    return {}


def _activity_of(message: Any) -> str:
    """One short line naming what the agent just did, for the operator.

    Tool calls are the signal worth printing -- "using WebFetch: https://..."
    says the search is moving -- while the model's prose is the answer being
    drafted, and reprinting it would bury the report it arrives in.
    """
    content = getattr(message, "content", None)
    if content is None or isinstance(content, str):
        return ""
    for block in content:
        name = _field(block, "name")
        if not isinstance(name, str) or not name:
            continue
        hint = _tool_hint(_field(block, "input"))
        return redact.redact(f"using {name}: {hint}" if hint else f"using {name}")
    return ""


def _field(block: Any, name: str) -> Any:
    if isinstance(block, Mapping):
        return block.get(name)
    return getattr(block, name, None)


def _tool_hint(payload: Any) -> str:
    """The one argument of a tool call worth showing, clipped to one line."""
    if not isinstance(payload, Mapping):
        return ""
    for key in ("command", "url", "query", "pattern", "file_path", "path", "description"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            first = value.strip().splitlines()[0]
            return first[:100] + ("..." if len(first) > 100 else "")
    return ""


def _text_of(message: Any) -> str:
    """Extract human-readable text from an SDK message of any shape."""
    direct = getattr(message, "result", None)
    if isinstance(direct, str) and direct:
        return direct
    content = getattr(message, "content", None)
    if isinstance(content, str):
        return content
    chunks: list[str] = []
    for block in content or []:
        text = getattr(block, "text", None)
        if isinstance(text, str):
            chunks.append(text)
        elif isinstance(block, Mapping) and isinstance(block.get("text"), str):
            chunks.append(block["text"])
    return "\n".join(chunks)


def _supported_kwargs(factory: Any, kwargs: Mapping[str, Any]) -> dict[str, Any]:
    """Drop options this SDK version does not know about.

    The SDK's option set grows between releases; passing an unknown keyword would
    be a hard failure for what is only ever a refinement.
    """
    import inspect

    try:
        accepted = set(inspect.signature(factory).parameters)
    except (TypeError, ValueError):
        return dict(kwargs)
    dropped = [k for k in kwargs if k not in accepted]
    if dropped:
        log.debug("installed claude-agent-sdk ignores: %s", ", ".join(sorted(dropped)))
    return {k: v for k, v in kwargs.items() if k in accepted}


def workspace_for(ctx: RunContext) -> Path:
    path = ctx.settings.path("mcp.agent.workspace") or (ctx.work_dir / "agent-workspace")
    return ctx.ensure_dir(Path(os.path.expanduser(str(path))))


__all__ = [
    "SDK_CLI_MODES",
    "SdkCli",
    "sdk_cli",
    "AgentRuntime",
    "SdkRuntime",
    "CliRuntime",
    "AgentDefinitionSpec",
    "AgentResult",
    "ProgressHook",
    "available_runtimes",
    "select_runtime",
    "failure_kind",
    "REFUSAL",
    "TIMEOUT",
    "TRANSIENT",
    "OTHER",
    "extract_json",
    "workspace_for",
]
