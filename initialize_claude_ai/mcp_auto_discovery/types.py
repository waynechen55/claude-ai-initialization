"""Value types shared by every phase-4 stage, in both modes.

The agentic and traditional pipelines produce exactly the same objects, which is
what lets them be swapped per-stage: an agent can find a candidate that the
deterministic verifier then proves, and a deterministic probe can hand a
candidate to an agent for repair.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from ..core import redact
from ..core.report import Status
from . import login_check


class Transport:
    """MCP transports this tool can speak."""

    #: Current HTTP transport: POST JSON-RPC, response is JSON or an SSE stream.
    STREAMABLE_HTTP = "streamable-http"
    #: Deprecated (spec 2024-11-05): GET /sse announces a POST endpoint.
    SSE = "sse"
    #: Local subprocess speaking newline-delimited JSON-RPC on stdio.
    STDIO = "stdio"
    #: Not a transport this tool can register -- Claude Code does not connect to
    #: an MCP server over WebSocket -- but one worth *detecting*: a host that
    #: upgrades /ws is a host with a native MCP endpoint, and the answer is a
    #: bridge rather than "this target does not do MCP".
    WEBSOCKET = "websocket"


#: Fallbacks for ``mcp.registration.transport_aliases``: other words for a
#: transport (an agent's, a registry's) mapped to the one this program speaks.
TRANSPORT_ALIASES = {
    "http": Transport.STREAMABLE_HTTP,
    "https": Transport.STREAMABLE_HTTP,
    "streamable-http": Transport.STREAMABLE_HTTP,
    "streamablehttp": Transport.STREAMABLE_HTTP,
    "streamable": Transport.STREAMABLE_HTTP,
    "remote": Transport.STREAMABLE_HTTP,
    "url": Transport.STREAMABLE_HTTP,
    "sse": Transport.SSE,
    "server-sent-events": Transport.SSE,
    "event-stream": Transport.SSE,
    "stdio": Transport.STDIO,
    "local": Transport.STDIO,
    "process": Transport.STDIO,
    "command": Transport.STDIO,
}
#: The transports an entry of each kind can be registered with.
KIND_TRANSPORTS = {"stdio": (Transport.STDIO,), "url": (Transport.STREAMABLE_HTTP, Transport.SSE)}


def normalize_transport(value: str, kind: str, aliases: dict[str, str] | None = None) -> str:
    """The transport an entry of ``kind`` is registered with, from whatever word ``value`` is.

    An unknown word, or one that contradicts the kind -- "stdio" for a URL --
    never decides: a URL entry is HTTP (``streamable-http`` unless SSE was
    named), a command entry is stdio. openqa.example.net's and
    developers.google.com's endpoints were written ``"type": "stdio"`` beside a
    ``url``, and Claude Code dropped both (``command: expected string``).
    """
    table = {**TRANSPORT_ALIASES, **{str(k).lower(): str(v) for k, v in (aliases or {}).items()}}
    word = str(value or "").strip().lower().replace("_", "-").replace(" ", "-")
    word = table.get(word, word)
    allowed = KIND_TRANSPORTS.get("stdio" if kind == "stdio" else "url")
    return word if word in allowed else allowed[0]


class Stage:
    """The four stages of discovery, in the order they run.

    These strings are the stage's identity everywhere: the key in
    ``mcp.stages``, in ``[mcp.agent.stage_agents]`` and in
    ``[mcp.policy.tiers]``, the name in the report, and the label on the
    terminal line.
    """

    ENDPOINT = "endpoint"
    PACKAGE = "package"
    BRIDGE = "bridge"
    STRATEGY = "strategy"


class Mode:
    """How a stage was actually carried out.

    Normally a result rather than a setting: discovery starts agentic and each
    stage records which of these it ended up in. ``FALLBACK`` is the honest
    middle case -- an agent ran and contributed findings, but the deterministic
    pipeline is what finished the stage. ``--no-agent`` (``mcp.agentic``) is the
    one way to fix the answer in advance, and it can only fix it at
    :data:`TRADITIONAL`.
    """

    #: A Claude subagent drove the stage and its candidates were verified.
    AGENTIC = "agentic"
    #: Deterministic probing and rule-based reasoning, start to finish.
    TRADITIONAL = "traditional"
    #: Agent first, deterministic pipeline after it came back short.
    FALLBACK = "agentic+traditional"


#: How each mode is announced on the terminal: the word, and the colour it wears.
#:
#: The mode belongs in its own column, in its own colour, because it answers a
#: different question from the verdict beside it: not "did this work" but "who
#: did it". Magenta for the agent, blue for the deterministic path, and amber for
#: a stage that needed both -- amber being what this program already uses for
#: "went through, but not the way it was meant to".
MODE_TAG: dict[str, tuple[str, tuple[str, ...]]] = {
    Mode.AGENTIC: ("agentic", ("magenta", "bold")),
    Mode.TRADITIONAL: ("traditional", ("blue",)),
    Mode.FALLBACK: ("fallback", ("yellow",)),
}


def mode_tag(mode: str) -> tuple[str, tuple[str, ...]]:
    """``(label, styles)`` for a mode, including one this table has never seen."""
    return MODE_TAG.get(mode, (mode or "unknown", ("dim",)))


@dataclass
class Handshake:
    """The outcome of a real MCP ``initialize`` exchange.

    ``ok`` is the only thing that lets a candidate be reported as working: no
    stage may claim success on the basis of a plausible-looking URL.
    """

    ok: bool = False
    transport: str = ""
    url: str = ""
    protocol_version: str = ""
    server_name: str = ""
    server_version: str = ""
    instructions: str = ""
    capabilities: dict[str, Any] = field(default_factory=dict)
    session_id: str = ""
    #: A request sent *after* the handshake came back with its own id matched --
    #: the proof that the channel carries traffic both ways, not just the one
    #: exchange every dead-but-listening endpoint can manage.
    round_trip: bool = False
    #: Which method proved it (``tools/list``, ``ping``, ...).
    round_trip_method: str = ""
    tools: list[str] = field(default_factory=list)
    resources: list[str] = field(default_factory=list)
    prompts: list[str] = field(default_factory=list)
    #: Tool names and input property keys the client cannot accept, as
    #: ``{"tool": ..., "key": ...}`` (``key`` empty when the tool's own name is
    #: the problem). One is enough to break every request a Claude Code session
    #: makes once that tool is loaded -- see ``mcp.verification.client_name_pattern``.
    schema_problems: list[dict[str, str]] = field(default_factory=list)
    #: Whether the credential the server was given is *accepted*: one read-only
    #: tool called on this session and its answer judged
    #: (:mod:`.login_check`). ``""`` when not attempted -- no real credential to
    #: check, or the check is off. ``login_tool`` names the tool called and
    #: ``login_detail`` what the answer said, redacted.
    login: str = ""
    login_tool: str = ""
    login_detail: str = ""
    #: The verdict counts: the credential was refused or has no access, and
    #: ``mcp.verification.login_check.mode = "pending"`` -- so the server is
    #: waiting for a working credential rather than working. See
    #: :attr:`Candidate.verified`.
    login_refused: bool = False
    #: Where the credential the login check used came from: "" for the
    #: exported variable, or a description such as "the value written in the
    #: existing entry 'sd-example-com'" when it was the credential in use.
    login_source: str = ""
    #: The trial with a stand-in credential failed exactly as the one without
    #: it (volatile parts removed): the credential is proved not to be what it
    #: is missing. Internal -- read by the verifier's next decision.
    standin_unchanged: bool = field(default=False, repr=False)
    #: Variables pointed at a configuration scope of the server's own because
    #: the operator's configuration broke it (``[mcp.healing.isolation]``), and
    #: what showed it. Empty when the server's own environment was enough.
    config_isolated: list[str] = field(default_factory=list)
    config_interference: str = ""
    #: Every tool the check called and what it answered: ``{tool, verdict,
    #: detail}``. One accepted call proves only its own code path.
    functional: list[dict[str, str]] = field(default_factory=list)
    #: ``{class, line}`` when calls failed and the server's own stderr named why
    #: (``[mcp.verification.stderr_causes]``): mcp-bugzilla answered every call
    #: "Failed to resolve dependency 'bz'" while its log said the CA file it
    #: was given did not exist. Empty when nothing failed or nothing was said.
    stderr_cause: dict[str, str] = field(default_factory=dict)
    #: "tool: detail" when a call answered that a setting is written in a form
    #: the server rejects (``login_check.MISCONFIGURED``); "" otherwise.
    misconfigured: str = ""
    #: Read tools that could not be called -- a required argument nobody could
    #: fill -- so an accepted login is not read as "every tool works".
    unexercised: list[str] = field(default_factory=list)
    #: A tool that needs no login (``mcp.discovery.api_kinds.<name>.operations``
    #: with ``public = true``), called on a bridge generated from that kind:
    #: ``{"tool", "answered", "detail"}``; empty when there was none to call.
    #: ``answered`` false means the bridge does not reach the API it was built
    #: for -- smelt.example.net's answered with an HTML 404 -- not that it waits.
    public_check: dict[str, Any] = field(default_factory=dict)
    #: A fingerprint of the credential the login check used
    #: (:func:`.secret_policy.credential_fingerprint`), never the value and never
    #: reported -- so the same credential accepted by one candidate and refused
    #: by another shows the refusing one is broken (:meth:`TargetOutcome.absorb`).
    login_credential: str = field(default="", repr=False)
    #: Whether the launch or request the login check called carried any
    #: credential (:func:`.login_check.credential_in`); None when no check ran.
    #: A refusal with nothing sent refuses nothing -- the slot is missing --
    #: and a success with nothing sent proves only that the service is public.
    credential_sent: bool | None = None
    #: "tool: detail" when the service refused the *query* a tool sent -- too
    #: large, too complex, too deep (``login_check.job_refused_markers``): the
    #: tool cannot do its job with any credential. "" otherwise.
    job_refused: str = ""
    #: URLs the launch asked to open while discovery ran (:mod:`.noninteractive`):
    #: it wants a browser login -- a step for the operator, never a wait.
    browser_login: list[str] = field(default_factory=list)
    #: Fingerprints of the secret parts of that credential, one each
    #: (:func:`.secret_policy.secret_part_prints`): the same password under
    #: another user name, or beside other settings, is still the same password.
    login_parts: list[str] = field(default_factory=list, repr=False)
    #: Why, beside the verdict, when there is more to say: ``login_check.NO_ACCOUNT``
    #: -- the service recognised the login and has no account behind it.
    login_reason: str = ""
    #: Credentials it does not need: its calls succeeded with none (a public
    #: service). Written as optional slots, never counted as missing.
    credential_optional: list[str] = field(default_factory=list)
    #: The usage tasks asked (:mod:`.contract`): ``{task, tool, source, passed, detail}``.
    contract: list[dict[str, Any]] = field(default_factory=list)
    #: The operator's own tasks this server could not be asked, with why.
    contract_skipped: list[dict[str, str]] = field(default_factory=list)
    #: ``{variable: form}`` proved by trying an address in other forms
    #: (``[mcp.verification.address_forms]``); empty when nothing was changed.
    address_forms: dict[str, str] = field(default_factory=dict)
    #: ``{variable: value}`` proved by trying the form of the *request* -- an
    #: Accept header a gateway rejected for a logged-in call
    #: (``[mcp.verification.request_forms]``); empty when nothing was changed.
    request_forms: dict[str, str] = field(default_factory=dict)
    #: Values that do not fit their slot (``[mcp.discovery.auth.value_shapes]``):
    #: ``{name, remedy}`` -- names and the shape they lack, never the value.
    shape_problems: list[dict[str, str]] = field(default_factory=list)
    #: Name, required arguments and annotations of each listed tool, for
    #: choosing one to check the login with. Never reported.
    tool_specs: list[dict[str, Any]] = field(default_factory=list, repr=False)
    #: HTTP status of the initialize response, 0 for a transport-level failure.
    status: int = 0
    error: str = ""
    #: True when the server answered 401/403 or advertised OAuth metadata.
    requires_auth: bool = False
    auth_hint: str = ""
    #: Variables the server needs before it can do its job, which were not
    #: available when it was trialled. A stdio server usually reads its
    #: credential when a *tool is called*, not at start-up, so one can complete
    #: a full handshake and advertise every tool it has while still being
    #: unusable -- see :attr:`Candidate.verified`.
    #:
    #: Read the other way round, this is also the list of variables the proven
    #: launch did **without**, which is what decides whether a ``${NAME}`` slot
    #: may be written for them -- see :attr:`Candidate.proven_without`.
    credential_pending: list[str] = field(default_factory=list)
    #: What, in that rejection, was evidence of MCP rather than of a login page.
    #: Filled in by :meth:`McpProber._note_auth_evidence`; empty means the server
    #: refused us and gave no sign of speaking the protocol -- see
    #: :attr:`Candidate.needs_credential`.
    auth_evidence: list[str] = field(default_factory=list)
    #: What the second trial -- the one with each missing credential set to a
    #: stand-in value -- showed, when the first was inconclusive. Filled in by
    #: :meth:`CandidateVerifier._credential_test`; empty when no such trial ran.
    #: The stand-in itself never appears here or anywhere else.
    credential_test: str = ""
    #: What the server said about itself once it was talking: its
    #: ``instructions``, its tool and prompt descriptions, its resource URIs,
    #: the defaults and examples in its input schemas.
    #:
    #: This is the strongest evidence there is that a server serves a given
    #: target, and it was being thrown away. Reading a launcher tells you what
    #: somebody *configured*; this is what the running server *says it does*,
    #: after a real handshake. An Open Build Service server lists resource URIs
    #: under ``https://build.opensuse.org/…``; a package that merely matched a
    #: keyword search lists nothing of the kind.
    #:
    #: Bounded and redacted at collection -- see ``mcp.verification.advertised``.
    advertised: list[str] = field(default_factory=list)
    elapsed: float = 0.0
    #: Seconds from launch to the ``initialize`` answer -- the wait a client
    #: sees, an install by the launcher included (stdio only).
    start_seconds: float = 0.0
    #: The endpoint only answered once certificate verification was turned off
    #: (step B of the tier-1 checklist). It is a real endpoint; it is also one
    #: whose peer was never authenticated, so this is carried everywhere the
    #: handshake goes -- the report, the ranking penalty, the remediation.
    insecure_tls: bool = False
    #: What the response said about itself even when the handshake failed: an
    #: SSE content type, a WebSocket upgrade, a JSON-RPC envelope, an OAuth
    #: challenge. This is step C's evidence, and it is often the difference
    #: between "nothing here" and "something here, spoken differently".
    protocol_hints: list[str] = field(default_factory=list)
    #: The server answered 2xx with something that was not JSON-RPC: not a
    #: refusal, not an answer. It proves nothing either way, so it is retried
    #: once and never, on its own, counted as "this URL does not speak MCP" --
    #: see :meth:`~.verifier.CandidateVerifier.credit_auth_claim`.
    inconclusive: bool = False
    #: Content type of the initialize response ("" when none) -- compared with a
    #: made-up path's answer before an inconclusive reply is read as a refusal.
    content_type: str = ""
    #: Why this URL is known to be an MCP endpoint -- a handshake, an MCP-shaped
    #: refusal, an answer unlike a made-up path's -- or "" when nothing showed it.
    endpoint_proof: str = ""
    #: Why nothing was launched at all -- a dry run, trials switched off -- or
    #: "". Not an answer and not a failure: an entry this run did not launch is
    #: never reported as one that "does not start".
    not_launched: str = ""

    def add_hint(self, *hints: str) -> None:
        for hint in hints:
            if hint and hint not in self.protocol_hints:
                self.protocol_hints.append(hint)

    @property
    def server_label(self) -> str:
        if self.server_name and self.server_version:
            return f"{self.server_name} {self.server_version}"
        return self.server_name or "(unnamed server)"

    @property
    def auth_scheme(self) -> str:
        """The scheme named in the ``WWW-Authenticate`` challenge, if any.

        ``Bearer realm="api"`` -> ``Bearer``. The scheme decides how a
        credential is presented, so it is read off the challenge rather than
        assumed: a server asking for ``Basic`` or a vendor scheme does not want
        a bearer token, and sending one produces a second 401 the operator then
        has to diagnose.
        """
        first = self.auth_hint.strip().split(" ", 1)[0].strip()
        return first if first.isalpha() else ""

    def summary(self) -> str:
        if not self.ok:
            return self.error or f"HTTP {self.status}"
        counts = []
        if self.tools:
            counts.append(f"{len(self.tools)} tool(s)")
        if self.resources:
            counts.append(f"{len(self.resources)} resource(s)")
        if self.prompts:
            counts.append(f"{len(self.prompts)} prompt(s)")
        detail = ", ".join(counts) or "no tools advertised"
        warning = " -- TLS certificate NOT verified" if self.insecure_tls else ""
        return f"{self.server_label} via {self.transport}, protocol {self.protocol_version}, {detail}{warning}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "transport": self.transport,
            "url": self.url,
            "protocol_version": self.protocol_version,
            "server": self.server_label,
            "capabilities": sorted(self.capabilities),
            "session": bool(self.session_id),
            "round_trip": self.round_trip,
            "round_trip_method": self.round_trip_method,
            "tools": self.tools[:50],
            "schema_problems": list(self.schema_problems),
            "login": self.login,
            "login_tool": self.login_tool,
            "login_detail": self.login_detail,
            "login_refused": self.login_refused,
            "login_source": self.login_source,
            "config_isolated": list(self.config_isolated),
            "config_interference": self.config_interference,
            "functional": [dict(row) for row in self.functional],
            "misconfigured": self.misconfigured,
            "unexercised": list(self.unexercised),
            "public_check": dict(self.public_check),
            "login_reason": self.login_reason,
            "credential_optional": list(self.credential_optional),
            "contract": [dict(row) for row in self.contract],
            "contract_skipped": [dict(row) for row in self.contract_skipped],
            "address_forms": dict(self.address_forms),
            "request_forms": dict(self.request_forms),
            "shape_problems": [dict(row) for row in self.shape_problems],
            "resources": self.resources[:50],
            "prompts": self.prompts[:50],
            "status": self.status,
            "requires_auth": self.requires_auth,
            "credential_pending": self.credential_pending,
            "credential_test": redact.redact(self.credential_test),
            "insecure_tls": self.insecure_tls,
            "inconclusive": self.inconclusive,
            "not_launched": self.not_launched,
            "auth_evidence": [redact.redact(e) for e in self.auth_evidence[:10]],
            "protocol_hints": self.protocol_hints[:20],
            "error": redact.redact(self.error),
            "elapsed_seconds": round(self.elapsed, 3),
        }


@dataclass
class Candidate:
    """Something that might be an MCP server, plus how to reach it.

    ``headers`` and ``env`` hold *names and non-secret values only*; a credential
    is carried as a reference in ``secret_env`` and resolved at launch time.
    """

    kind: str = "url"  # "url" | "stdio"
    name: str = ""
    url: str = ""
    command: list[str] = field(default_factory=list)
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    #: variable name -> secret *reference* (env:/file:/keyring:...), never a value
    secret_env: dict[str, str] = field(default_factory=dict)
    #: variable name -> the *shape* of the value, as learned during discovery:
    #: ``"username:key:secret"``, ``"email:api_token"``. Never a value, and
    #: never a guess -- absent means nobody established what the product
    #: issues, which is reported as unknown rather than papered over.
    #:
    #: This exists because a name derived without it under-describes the value:
    #: openQA's credential is three parts joined by colons, the agent named the
    #: variable ``..._API_KEY`` after one of them, and an operator who set just
    #: the key got a 403 with nothing to explain it.
    #: What happened when this candidate's entry was built, launched as a
    #: client would launch it, and repaired if it did not start. Set by
    #: :meth:`~.traditional.TraditionalPipeline.heal_or_discard`; ``None`` until
    #: the stage asks. See :mod:`.healing`.
    healing: Any = None
    secret_formats: dict[str, str] = field(default_factory=dict)
    #: ``secret_env`` key -> the variable its entry's ``${...}`` points at, set
    #: by the writer (``JIRA_PERSONAL_TOKEN`` -> ``SD_EXAMPLE_COM_JIRA_PERSONAL_TOKEN``
    #: when another service's entry already claims the plain name). Every
    #: instruction names the exported variable, never the key.
    exported_names: dict[str, str] = field(default_factory=dict)
    #: Why its entry was discarded by the start gate, when it was: the healing
    #: verdict in words. A candidate discarded there is still a lead -- the
    #: closest thing an address got -- and the report names it with this.
    start_failure: str = ""
    #: The API kind this candidate was bridged from
    #: (``mcp.discovery.api_kinds.<name>.operations``), "" for anything else --
    #: so a server is not told it has an alternative in itself.
    api_kind: str = ""
    #: Where the way it sends its credential comes from, in words -- the API
    #: description's security scheme, the service's 401 challenge, a kind's
    #: configured ``security``, or a published server's own code; "" when
    #: nobody checked it (a bridge an agent wrote). A refusal from a candidate
    #: without it is not yet a verdict on the credential (see unconfirmed_refusal).
    auth_method_evidence: str = ""
    #: Credential variables its launched script reads that it did not declare,
    #: as the variables the entry expands -- found by the writer
    #: (:meth:`.McpWriter.declare_reads`) and named to the operator as needed.
    #: sd.example.com's agent-written bridge read SD_EXAMPLE_COM_USERNAME and
    #: SD_EXAMPLE_COM_PASSWORD and declared neither.
    undeclared_credentials: list[str] = field(default_factory=list)
    #: ``secret_env`` key -> the variable to read for it instead of a new one:
    #: the one an existing entry for the same host reads for the credential
    #: this candidate was proved with (:meth:`.CandidateVerifier.check_in_use`).
    #: One instance's token, one export.
    reuse_names: dict[str, str] = field(default_factory=dict)
    #: ``env`` key -> the ``${...}`` reference an existing entry wrote for it,
    #: when the value in :attr:`env` is that reference expanded for the trial.
    #: The writer writes the reference back, never the expansion: an existing
    #: build-opensuse-org entry's ``${BUILD_OPENSUSE_ORG_USERNAME}`` was expanded
    #: to be launched, and the operator's user name was written into the file.
    references: dict[str, str] = field(default_factory=dict)
    #: ``secret_env`` key -> what the operator already exports for it, as found
    #: by :meth:`.CandidateVerifier._borrow_held`: a variable name, or a form of
    #: ``${...}`` references (``Authorization:Basic ${BUILD_EXAMPLE_NET_BASE64_OF_USER_PASSWORD}``).
    #: Applied once the login check accepts it.
    held_names: dict[str, str] = field(default_factory=dict)
    #: ``secret_env`` key -> the value written for it: a form of ``${...}``
    #: references to variables the operator already exports, proved by a login
    #: check -- in place of a new variable nobody exports.
    composed: dict[str, str] = field(default_factory=dict)
    #: Settings whose ``${NAME}`` nothing sets -- not credentials: launched
    #: without, and never written as a bare ``${NAME}`` (BUGZILLA_AUTH_MODE).
    unset_settings: list[str] = field(default_factory=list)
    #: The name of the existing registration it was built from, "" for a new
    #: candidate -- between two copies of one server, the existing one wins a tie.
    existing_name: str = ""
    #: Why this entry is known to have worked on this machine -- it is the
    #: entry that last passed here (:mod:`.known_good`), or Claude Code shows
    #: it connected -- "" for neither. Between two candidates that proved
    #: equally little, this one keeps its place (``mcp.discovery.reuse.worked_before_wins``).
    worked_before: str = ""
    #: The login scheme the candidate performs (``basic``, ``bearer``,
    #: ``signature``...), "" when unknown -- for :mod:`.login_history`.
    login_scheme: str = ""
    #: What the run knows about it while nothing is proved -- (history, API
    #: host, values still to supply), lower is better -- and why, in words
    #: (:mod:`.evidence`). Empty when unmarked.
    evidence: tuple = ()
    evidence_why: str = ""
    #: Why a verified candidate still settled nothing (it could not be asked
    #: what the address is for, or its access was never proved) -- for the
    #: agent's retry brief and the report. "" when it settled, or never verified.
    unsettled: str = ""
    #: ``secret_env`` key -> the credential an existing entry *uses right now*
    #: (a literal it holds, or the default of its ``${NAME:-…}`` while ``NAME``
    #: is unset), as a :class:`~..core.secrets.Secret`. Read from the entry,
    #: never written anywhere, never reported, never logged: its one use is the
    #: read-only login call (:meth:`.CandidateVerifier.check_in_use`), because
    #: it is the credential Claude Code sends every time it loads the file.
    #: sd.example.com's entry held one; the run tested only the exported variable,
    #: which was unset, and stayed silent about the token that was failing.
    in_use: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)
    #: exported variable -> a value kept out of ``.mcp.json`` only because it
    #: *looks* like a secret (:meth:`.McpWriter.credential_evidence` ``SUSPECT``),
    #: as a :class:`~..core.secrets.Secret`. The entry reads ``${NAME}``; the
    #: value goes where Claude Code reads it when the entry is written, and the
    #: start check launches with it, so the launch tested is the one shipped.
    #: Never a credential the operator is asked for: nothing proved it is one.
    #: gitlab.example.net's bridge User-Agent was made a bare ``${…}`` that nothing
    #: set, and reported as "waiting for a credential".
    kept_out: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)
    #: Runtime settings this machine needs (``NODE_OPTIONS``…), by the per-machine
    #: variable the entry reads them from -- kept in this machine's Claude Code
    #: settings env, never in the file that travels (McpWriter.machine_settings).
    machine_values: dict[str, str] = field(default_factory=dict, repr=False, compare=False)
    #: Runtime-tuning settings dropped because the job was done as well without
    #: them (``{key: value}``); restored per machine when a later check fails
    #: the way a network does (``runtime_tuning.restore_on``).
    dropped_tuning: dict[str, str] = field(default_factory=dict, repr=False, compare=False)
    #: What the address was known to be (product and API kinds) when this
    #: candidate's usage tasks were chosen -- ``None`` until it was judged. A
    #: verdict reached under a job that has since changed is judged again
    #: before anything is chosen (:meth:`.CandidateVerifier.stale_verdicts`):
    #: gitlab.example.net's mcp-graphql was verified before the address was known
    #: to be a GitLab, then dropped by a re-check that knew.
    judged_job: tuple[str, ...] | None = field(default=None, compare=False)
    #: When set, the only usage tasks asked (by name) -- the final re-check asks
    #: what proved the candidate, nothing new (:meth:`.CandidateVerifier.recheck`).
    recheck_tasks: list[str] | None = field(default=None, compare=False)
    #: variable name -> where to obtain one, in words. Printed, never followed.
    secret_sources: dict[str, str] = field(default_factory=dict)
    headers: dict[str, str] = field(default_factory=dict)
    #: ``env`` names whose value this program filled in from the target's
    #: address and that do not, on their own, show the server is *for* the
    #: target -- a generic ``BASE_URL`` that was simply the only address the
    #: entry declared. They are launched with, and written, like any other
    #: variable; :mod:`.grounding` just does not read them as evidence, because
    #: an address we supplied cannot vouch for the package we supplied it to.
    #: See ``mcp.packages.address_fill.sole_fill_grounds``.
    unvouched_env: set[str] = field(default_factory=set)
    transport: str = ""
    #: Which stage produced this candidate.
    origin: str = ""
    #: Where the idea came from, in words ("MCP registry", "path template /mcp").
    source: str = ""
    #: 0.0-1.0 ordering hint; verification is what actually decides.
    confidence: float = 0.0
    #: Commands that must run before this candidate can be launched.
    install: list[list[str]] = field(default_factory=list)
    #: Programs the server runs at call time, beside its launch command -- a
    #: client bridge drives ``osc`` -- each with the ways to install it, tried
    #: in order until one puts it on the PATH (``[mcp.discovery.clients.<name>]
    #: install``). Checked before the trial: one whose program is missing and
    #: cannot be installed is broken, never "waiting for a credential".
    requires: dict[str, list[list[str]]] = field(default_factory=dict)
    #: Credential slots the server reads that its entry did not pass, given
    #: back by this run (:meth:`.McpWriter.complete_slots`): written as
    #: ``${NAME:-}`` -- empty until supplied, never a bare reference.
    completed_slots: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    #: Why somebody believes this endpoint wants a credential, in words: an
    #: agent that read the product's documentation, or the checklist that
    #: recorded it behind an auth wall. Empty for most candidates. Never a
    #: credential -- see :meth:`~.verifier.CandidateVerifier.credit_auth_claim`.
    auth_claim: str = ""
    handshake: Handshake | None = None

    @property
    def verified(self) -> bool:
        """Did this server prove it works *and* could it be used right now?

        Both halves are required, and the second is why this is not simply
        ``handshake.ok``. A stdio server reads its credential when a tool is
        called, so one trialled without that credential can complete the
        handshake, advertise a hundred tools, and fail every one of them. The
        handshake is a fact and is recorded as such; "verified" is a promise to
        the operator that the entry works as written, and that promise cannot
        be made with an empty credential slot -- see
        :attr:`Handshake.credential_pending`.
        """
        return bool(
            self.handshake
            and self.handshake.ok
            and not self.handshake.credential_pending
            and not self.handshake.login_refused
        )

    @property
    def launch_values(self) -> dict[str, Any]:
        """The values the entry reads from where Claude Code keeps them -- what launching it as written needs here."""
        return {**self.kept_out, **self.machine_values}

    @property
    def proven_without(self) -> set[str]:
        """Variables the launch that actually handshaked did not have.

        This is a fact about the trial, not a judgement: the verifier launches a
        stdio candidate with every unresolvable secret left out, and when that
        launch completes a handshake the missing names are recorded on the
        handshake. They are exactly the variables the server demonstrably does
        **not** need in order to start.

        The writer consults this before putting a ``${VAR}`` slot in ``env``.
        Two real entries were broken by writing one anyway. ``mcp-bugzilla`` was
        proven to start without ``BUGZILLA_AUTH_MODE``, and was then written
        with ``"BUGZILLA_AUTH_MODE": "${BUGZILLA_AUTH_MODE}"``; the variable was
        unset, so the launch that shipped was not the launch that was tested,
        and the process exited during start-up. ``@ivotoby/openapi-mcp-server``
        went the same way with ``API_HEADERS``. Adding an unset variable is not
        a no-op -- it is a different command.

        The slot is not lost, only deferred: once the operator exports the
        variable, the next run resolves it, trials the server *with* it, and the
        entry is written with the placeholder because by then the launch
        carrying it is the launch that was proven.
        """
        shake = self.handshake
        if shake is None or not shake.ok:
            return set()
        return {str(name) for name in [*shake.credential_pending, *shake.credential_optional]}

    @property
    def needs_credential(self) -> bool:
        """An endpoint that is there, speaks MCP, and wants to know who you are.

        A 401 or 403 is the server answering: it exists, it is listening, and it
        declined to talk to an anonymous client. That is a *positive* finding
        about the target and a different thing from ``verified`` -- the
        handshake did not complete and nothing here pretends it did -- so such a
        candidate travels its own route: registered with its credential named
        and left empty, reported separately, never counted as working.

        **The status alone is not enough.** A Jira, a Confluence, a service desk
        or any intranet answers 401 or 403 on every URL it owns, including ones
        that do not exist, so on such a host the status carries no information
        about MCP at all -- and taking it at face value registers the website as
        a server, which no credential can ever make work. So the rejection also
        has to have carried something protocol-shaped:
        :attr:`Handshake.auth_evidence`, which the prober fills in from a
        JSON-RPC body, an MCP header, a protected-resource document or simply a
        content type that is not a web page. ``mcp.discovery.auth`` decides what
        counts; requiring nothing restores the older, credulous behaviour.
        """
        if self.handshake and self.handshake.credential_pending:
            # It answered every question we could ask without a credential --
            # stronger evidence than a 401, and the same conclusion: real, and
            # waiting on a variable.
            return True
        if self.handshake and (self.handshake.ok or self.handshake.requires_auth) and self.handshake.login_refused:
            # It answered everything, with the credential it was given -- and
            # its service refused that credential. Real, and waiting on a
            # working value: the same route, with a different remedy.
            return True
        return bool(
            self.handshake and not self.handshake.ok and self.handshake.requires_auth and self.handshake.auth_evidence
        )

    @property
    def label(self) -> str:
        return self.name or self.url or " ".join(self.command) or "(unnamed)"

    def full_command(self) -> list[str]:
        return [*self.command, *self.args]

    def add_note(self, *lines: str) -> None:
        for line in lines:
            if line and line not in self.notes:
                self.notes.append(line)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "name": self.name,
            "label": self.label,
            "url": self.url,
            "command": redact.redact_argv(self.full_command()) if self.command else [],
            "env": sorted(self.env),
            "secret_env": {k: redact.redact(v) for k, v in self.secret_env.items()},
            "secret_formats": dict(self.secret_formats),
            "headers": sorted(self.headers),
            "unvouched_env": sorted(self.unvouched_env),
            "transport": self.transport,
            "origin": self.origin,
            "source": self.source,
            "confidence": round(self.confidence, 2),
            "install": [redact.redact_argv(c) for c in self.install],
            "notes": [redact.redact(n) for n in self.notes],
            "auth_claim": redact.redact(self.auth_claim),
            # Whether this candidate's entry actually starts, and what was
            # changed to get it there. Collected by the stage loop and, until
            # now, never reported -- which is the habit this file's own history
            # is full of. See :mod:`.healing`.
            "healing": self.healing.to_dict() if self.healing is not None else None,
            "verified": self.verified,
            "handshake": self.handshake.to_dict() if self.handshake else None,
        }

    def identity(self) -> str:
        """Deduplication key."""
        if self.kind == "stdio":
            return "stdio:" + " ".join(self.full_command())
        return f"url:{self.url.rstrip('/')}"


@dataclass
class StageOutcome:
    """What one stage did to one target, and what it has to show for it.

    Three lists rather than one, because they mean different things and are
    counted separately to the very end: ``candidates`` is everything proposed,
    ``verified`` is the subset that completed a handshake, and ``provisional``
    is the subset that answered by demanding a credential -- real endpoints
    that are not yet usable.
    """

    stage: str
    mode: str = Mode.TRADITIONAL
    status: str = Status.SKIPPED
    detail: str = ""
    candidates: list[Candidate] = field(default_factory=list)
    verified: list[Candidate] = field(default_factory=list)
    #: Endpoints that answered but demanded a credential. Real MCP endpoints,
    #: not yet usable ones -- see :attr:`Candidate.needs_credential`.
    provisional: list[Candidate] = field(default_factory=list)
    #: Anything the stage learned that a later stage can use (OpenAPI specs,
    #: OAuth metadata, package names, reachable ports...).
    findings: dict[str, Any] = field(default_factory=dict)
    remediation: list[str] = field(default_factory=list)
    artifacts: list[str] = field(default_factory=list)
    duration: float = 0.0
    error: str = ""

    def add_remediation(self, *lines: str) -> None:
        for line in lines:
            if line and line not in self.remediation:
                self.remediation.append(line)

    def note_provisional(self, limit: int = 1, *, prefer_detected_api: bool = True) -> list[Candidate]:
        """Single out the candidates that answered by demanding a credential.

        ``limit`` caps how many are carried forward, because a host that puts
        its whole API behind a login answers 401 on every path probed, and
        registering a dozen entries for one service would be worse than
        registering none. The most promising one is the one a probe ranked
        highest, so that is the one kept; ``0`` means keep them all, which is
        what ``--exhaustive-mcp`` asks for everywhere else too.
        """
        # By proof first, then by the probe's ranking: an agent's bridge that
        # arrived first must not outrank one whose login a call accepted. One
        # already proven broken -- it does not start, or its entry disagrees
        # with what it launches -- waits for nothing: build-example-net's CLI copy,
        # whose manifest read a variable the entry never passed, was written
        # as "waiting for a credential" over two bridges that could work.
        # Then, among the equally proved, the one built for an API this host was
        # seen to serve: sd.example.com's bridge stage kept an agent's bridge (more
        # confident, no API kind recorded) over the program's own customer-API
        # bridge, so the cross-stage preference in TargetOutcome.absorb never saw
        # it, and mcp-atlassian -- the server a customer account cannot use --
        # kept the slot. (mcp.discovery.auth.prefer_detected_api)
        walled = sorted(
            (c for c in self.candidates if c.needs_credential and c not in self.verified and not c.start_failure),
            # ...then the one known to have worked here, then confidence, then
            # the faster start (Claude Code gives a server 30 s to start).
            # Then, with nothing proved, what the run knows: a scheme this host
            # refused before goes last, the one its API host asks for and the
            # one needing fewer values come first (:mod:`.evidence`).
            key=lambda c: choice_key(c, prefer_detected_api=prefer_detected_api),
        )
        chosen: list[Candidate] = []
        seen: set[str] = set()
        for candidate in walled:
            key = candidate.url or candidate.label
            if key in seen:
                continue
            seen.add(key)
            chosen.append(candidate)
            if limit and len(chosen) >= limit:
                break
        self.provisional = chosen
        return chosen

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "mode": self.mode,
            "status": self.status,
            "detail": redact.redact(self.detail),
            "candidates": [c.to_dict() for c in self.candidates],
            "verified": [c.label for c in self.verified],
            "provisional": [c.label for c in self.provisional],
            "findings": _scrub(self.findings),
            "remediation": self.remediation,
            "artifacts": self.artifacts,
            "duration_seconds": round(self.duration, 2),
            "error": redact.redact(self.error),
        }


def waits_for_credential(candidate: Candidate) -> str:
    """What a candidate waiting for a credential is, in words that fit it.

    A remote endpoint *refused an anonymous client*; a local package did no
    such thing -- it started, or failed to, on this machine, and needs a value
    before it offers its tools. confluence.example.com's npm package was reported
    as "a real MCP endpoint that refused an anonymous client" and then as "the
    target's own MCP endpoint", neither of which it was. One wording, shared by
    both pipelines and the phase, so the same candidate is described the same
    way everywhere.
    """
    shake = candidate.handshake
    if shake is not None and shake.login_refused:
        # It was given a credential -- exported, or the one written in its
        # existing entry -- and its service refused it. "Needs a credential"
        # would send the operator looking for one they already supplied.
        return login_check.waiting_words(shake.login, shake.login_source, started=shake.ok)
    if candidate.kind == "stdio":
        return "is a local server that needs a credential before it offers its tools"
    return "is an MCP endpoint that refused an anonymous client"


def _unproved(candidate: Candidate) -> bool:
    """Its access was checked and never proved: every call empty or unchecked."""
    shake = candidate.handshake
    return bool(shake is not None and shake.login in login_check.UNPROVED and not candidate.api_kind)


def _login_accepted(candidate: Candidate) -> bool:
    """A call accepted this candidate's login."""
    shake = candidate.handshake
    return bool(shake is not None and shake.login == login_check.ACCEPTED)


def _proven_alternative(candidate: Candidate) -> bool:
    """A bridge for an alternative API kind whose access a call proved."""
    shake = candidate.handshake
    return bool(candidate.api_kind and shake is not None and shake.login == login_check.ACCEPTED)


def proves_access(candidate: Candidate) -> bool:
    """A call proved this candidate can be used: its login accepted, or it works and nothing was left unproved."""
    shake = candidate.handshake
    if shake is None:
        return False
    if shake.login == login_check.ACCEPTED:
        return True
    if candidate.verified and any(r.get("passed") and r.get("needs_login") for r in shake.contract):
        # A usage task only the logged-in account can pass (the current user,
        # its own record) passed: the login is proved by doing the job, even
        # where no tool could be asked as a login check. gitlab-example-net read
        # its own user through mcp-graphql and was still "not working".
        return True
    return candidate.verified and shake.login not in (*login_check.UNPROVED, *login_check.REFUSED)


def unconfirmed_refusal(candidate: Candidate) -> bool:
    """Its service refused the credential -- but nothing checked how this candidate sends it.

    A bridge an agent wrote picks its own authentication; a 401 from it is
    evidence about the bridge *or* the credential. sd.example.com's agent-written
    bridge was refused, and the report blamed "the credential this machine
    already uses" while the run's own bridge, which sends it the way the
    service documents, was never tried.
    """
    shake = candidate.handshake
    return bool(
        shake is not None
        and shake.login == login_check.REJECTED
        and not candidate.auth_method_evidence
        and candidate.origin in (Stage.BRIDGE, Stage.STRATEGY)
    )


def proof_rank(candidate: Candidate) -> int:
    """Lower is better: accepted, not yet checked, unproved, refused without checking how, refused."""
    shake = candidate.handshake
    login = shake.login if shake is not None else ""
    if login == login_check.ACCEPTED:
        return 0
    if not login:
        return 1
    if shake is not None and shake.login_reason == login_check.ACCOUNT_NO_ACCESS:
        # The account may not use the product (its own record said 403): that
        # is the account's verdict, not this server's -- ranked like a login
        # never checked, so an unproved server with an empty slot does not
        # outrank the entry carrying the operator's credential.
        return 1
    if login in login_check.UNPROVED:
        return 2
    if (
        shake is not None
        and login in login_check.REFUSED
        and (login_check.nothing_sent(shake) or shake.login_reason == login_check.SAME_AS_ANONYMOUS)
    ):
        # No credential was sent, so none was refused: ranked like a login
        # never checked. sd.example.com's two service-desk bridges were asked with
        # no token, got 401, were ranked "REJECTED" -- below a mcp-atlassian
        # nobody had checked -- and lost the slot to it.
        return 1
    if shake is not None and login == login_check.REJECTED and login_check.credential_missing(shake.login_detail, None):
        # Nothing was sent -- "not set", "no SSH key" -- so nothing was refused:
        # ranked like a login never checked, not like one the service turned down.
        return 1
    return 3 if unconfirmed_refusal(candidate) else 4


def best_of(candidates: list[Candidate]) -> Candidate:
    """The copy of one server that is kept: working first, then most of the usage contract, proof, the existing entry, order."""
    from . import contract

    order = {id(c): i for i, c in enumerate(candidates)}
    return min(
        candidates,
        key=lambda c: (
            0 if c.verified else 1,
            -contract.score(c),
            proof_rank(c),
            0 if c.existing_name else 1,
            order[id(c)],
        ),
    )


def evidence_key(candidate: Candidate, *, prefer_detected_api: bool = True) -> tuple:
    """What is *known* about a candidate, lower is better -- proof, then evidence; never order or confidence.

    proof (:func:`proof_rank`), the host's history with its login scheme, being
    built for an API the address was seen to serve, having worked on this
    machine, the scheme the address's API host asks for (a scheme it offers
    nowhere last), the values still to supply. Shared by every chooser -- the
    per-stage pick, the cross-stage slot, an existing entry keeping its place --
    so no branch decides by a rule the others do not apply.
    """
    found = _evidence(candidate)
    return (
        proof_rank(candidate),
        found[0],
        0 if (prefer_detected_api and built_for_api(candidate)) else 1,
        0 if candidate.worked_before else 1,
        *found[1:],
    )


#: Fallback for ``mcp.discovery.auth.evidence.client_bridge_is_built_for_api`` /
#: ``client_bridge_markers``: the env keys that say a server drives the product's
#: own client (``client_bridge.ENV_MANIFEST``), read back from any entry.
CLIENT_BRIDGE_COUNTS = True
CLIENT_BRIDGE_MARKERS = ("MCP_CLIENT_MANIFEST",)
_CLIENT_BRIDGE: tuple[bool, tuple[str, ...]] = (CLIENT_BRIDGE_COUNTS, CLIENT_BRIDGE_MARKERS)


def built_for_api(candidate: Candidate) -> bool:
    """Was it made for the API this address serves -- a bridge from its kind, or one driving its product's own client?

    build-example-net: the password bridge, its manifest matching the OBS API kind,
    outranked the osc bridge that signs at api.example.net -- the only login that
    API accepts -- because only the first counted as "built for the API", and
    that came before the login's fit. Both were made for this product's API;
    the login decides between them.
    """
    if candidate.api_kind:
        return True
    counts, markers = _CLIENT_BRIDGE
    return counts and any(str(k) in markers for k in candidate.env)


def choice_key(candidate: Candidate, *, prefer_detected_api: bool = True) -> tuple:
    """:func:`evidence_key`, then confidence and the faster start -- for ordering, never for replacing."""
    return (
        *evidence_key(candidate, prefer_detected_api=prefer_detected_api),
        -candidate.confidence,
        float(candidate.handshake.start_seconds or 0.0) if candidate.handshake is not None else 0.0,
    )


def evidence_beats(candidate: Candidate, holder: Candidate, *, prefer_detected_api: bool = True) -> bool:
    """Does what is known make ``candidate`` strictly better than ``holder`` -- neither having proved access?"""
    if proves_access(holder):
        return False
    return evidence_key(candidate, prefer_detected_api=prefer_detected_api) < evidence_key(
        holder, prefer_detected_api=prefer_detected_api
    )


#: Fallback for ``mcp.discovery.auth.evidence.one_comparison``.
ONE_COMPARISON = True
_ONE_COMPARISON = ONE_COMPARISON


def configure_choice(settings: Any) -> None:
    """Read ``mcp.discovery.auth.evidence.one_comparison`` (the shared chooser) once per run."""
    global _ONE_COMPARISON, _CLIENT_BRIDGE
    _ONE_COMPARISON = settings.bool_("mcp.discovery.auth.evidence.one_comparison", ONE_COMPARISON)
    _CLIENT_BRIDGE = (
        settings.bool_("mcp.discovery.auth.evidence.client_bridge_is_built_for_api", CLIENT_BRIDGE_COUNTS),
        tuple(settings.str_list("mcp.discovery.auth.evidence.client_bridge_markers", list(CLIENT_BRIDGE_MARKERS))),
    )


def _evidence_everywhere() -> bool:
    return _ONE_COMPARISON


def _evidence(candidate: Candidate) -> tuple:
    from .evidence import order

    return order(candidate)


def _evidence_neutral() -> tuple:
    from .evidence import NEUTRAL

    return NEUTRAL


def _replaceable(holder: Candidate, by: Candidate) -> bool:
    """May ``by`` take ``holder``'s slot for its account's lack of access?

    Not when the service's own record said the *account* may not use it
    (confluence.example.com answered 403 on /rest/api/user/current): no other
    server does better with that account, and an unproved package with an empty
    slot took the place of the entry that carried the operator's token. Only
    one whose access a call proved may.
    """
    shake = holder.handshake
    if shake is not None and shake.login_reason == login_check.ACCOUNT_NO_ACCESS:
        return proves_access(by)
    return True


def _no_access(candidate: Candidate) -> bool:
    """Waiting only because its service gives the account no API access (see login_check)."""
    shake = candidate.handshake
    return bool(shake is not None and shake.login_refused and shake.login == login_check.NO_ACCESS)


def waiting_remedy(candidate: Candidate) -> str:
    """The stage's remediation line for a candidate waiting on a credential -- both pipelines.

    One that was given a credential its service refused needs a different
    credential or account, and saying "supplying a token is an export" would
    send the operator to do what they already did.
    """
    shake = candidate.handshake
    if shake is not None and shake.login_refused:
        return (
            f"{candidate.label} {waits_for_credential(candidate)}"
            + (f" ({shake.login_detail})" if shake.login_detail else "")
            + ". It is written out with its credential slot, so a working credential -- or an account "
            "with API access -- is an export rather than another discovery run."
        )
    return (
        f"{candidate.label} {waits_for_credential(candidate)}. If it passes the registration "
        "checks it is written out with its credential slot empty, so supplying a token "
        "is an export rather than another discovery run."
    )


@dataclass
class TargetOutcome:
    """Everything phase 4 learned about one target."""

    target: str
    base_url: str = ""
    stages: list[StageOutcome] = field(default_factory=list)
    verified: list[Candidate] = field(default_factory=list)
    #: Endpoints that proved they exist by refusing an anonymous client. They
    #: are registered with their credential named and empty, so the operator
    #: exports one variable rather than re-running the whole search, and they
    #: are counted and reported apart from :attr:`verified` throughout.
    provisional: list[Candidate] = field(default_factory=list)
    #: Scored comparison of everything that answered, best first. Only filled in
    #: when --the-best-mcp asked for one winner; the losers live on here, since
    #: ``verified`` has been trimmed to the winner by then and a choice nobody
    #: can inspect afterwards is not a choice anyone can argue with.
    ranking: list[dict[str, Any]] = field(default_factory=list)
    registered: list[str] = field(default_factory=list)
    #: One row per endpoint that was written out: every check it had to pass
    #: (connected, tools, two-way traffic, and what Claude Code itself says
    #: about it afterwards). Filled in by phase 4 after publishing.
    verification: list[dict[str, Any]] = field(default_factory=list)
    playbook: list[str] = field(default_factory=list)
    #: The tier-1 checklist for this target (a
    #: :class:`~initialize_claude_ai.mcp_auto_discovery.policy.DueDiligence`).
    #: Untyped here so that :mod:`policy` can import this module and not the
    #: other way round; it is the one object both pipelines fill in and the
    #: tier gate reads.
    due_diligence: Any = None
    #: Entries already in ``.mcp.json`` that point at the same service as
    #: something this run wrote. Warned about rather than removed: an operator's
    #: own entry is theirs to keep.
    duplicates: list[dict[str, str]] = field(default_factory=list)
    #: Credential variables an entry written for this target shares with an
    #: entry for a *different* service: ``{"variable", "name", "other"}``. One
    #: value cannot be two instances' tokens, so one of them cannot work.
    shared_secrets: list[dict[str, str]] = field(default_factory=list)
    #: Candidates whose entries the start gate discarded, kept for the report:
    #: osc-mcp for build.opensuse.org was found three times and refused three
    #: times, and the "best lead" printed instead was a URL that answered 404.
    unstartable: list[Candidate] = field(default_factory=list)
    #: Identities (:meth:`Candidate.identity`) of verified candidates whose
    #: entry Claude Code itself could not use -- its own check failed, asked
    #: twice. Written still, never counted as working: github-com was reported
    #: "PASSED" beside "when Claude Code itself used it, get_me failed".
    consumer_failed: list[str] = field(default_factory=list)
    #: How many times a withdrawn settling server has handed the address over
    #: (``mcp.handover.max_rounds``): reopening is owed once, never in a loop.
    handover_rounds: int = 0
    #: The API kinds detected for this target (``mcp.discovery.api_kinds``),
    #: ``{name: {"label", "url"}}`` -- what an alternative to a server would be.
    api_kinds: dict[str, dict[str, str]] = field(default_factory=dict)
    #: How the address can be logged in to (:func:`.login_methods.plan`): the
    #: product, every challenge seen, the method chosen and the alternatives.
    auth_plan: dict[str, Any] = field(default_factory=dict)
    #: Registrations already on this machine that this run proved are not this
    #: target's: they answered, they connect only to somebody else's host, and
    #: the registration gate refused them. Name, where it was read, the reason,
    #: and -- once phase 4 has acted -- what was done. See
    #: ``mcp.registration.retire_refused_existing``.
    stale: list[dict[str, str]] = field(default_factory=list)
    #: The candidate behind each ``stale`` row, by name, so phase 4 can judge it
    #: again on everything the run found before removing it. Not reported: the
    #: row already says what it is.
    stale_candidates: dict[str, Candidate] = field(default_factory=dict, repr=False)
    #: Registrations already on this machine for this target that answered
    #: during this run (a handshake, or a demand for a credential), by name,
    #: with where each was read. If nothing new can be written for the target,
    #: these are what it still has -- see :attr:`kept_existing`.
    answered_existing: dict[str, str] = field(default_factory=dict)
    #: Of the registrations on this machine for this target, those Claude Code
    #: itself reports connected (``claude mcp list``), by name, with where each
    #: was read -- including ones this run could not launch on its own. A
    #: connected entry is never "does not start", and is replaced only by a
    #: server that proved it does the job (see McpWriter._holds_its_place).
    connected_existing: dict[str, str] = field(default_factory=dict)
    #: Why what the address *is* (product, logins, API kinds) is undecided: the
    #: server answered "try again later" while it was being identified, in
    #: words; "" when identification was not cut short. While set, nothing
    #: unproven settles the address (TraditionalPipeline.settled).
    identification_undecided: str = ""
    #: Proven servers refused *only* because nothing yet tied them to the
    #: address (grounding) -- kept aside, not dropped, and judged again when the
    #: address gains evidence (TraditionalPipeline.rejudge_identity).
    #: developers-google-com was refused at reuse, before the endpoint stage
    #: found developers.google.com documenting that very URL.
    identity_pending: list[Candidate] = field(default_factory=list, repr=False)
    #: Of those, the ones left as the target's only registration because every
    #: entry this run could write was refused: name, source and why. Reported,
    #: so a run never reads as having left an address empty when the machine
    #: still holds a server for it.
    kept_existing: list[dict[str, str]] = field(default_factory=list)
    #: Candidates found for this target whose entries a registration gate
    #: refused (or withdrew) at publish. Moved here out of ``verified`` and
    #: ``provisional``, which from then on hold only what was written -- so
    #: nothing refused is ever counted as a working server.
    refused: list[Candidate] = field(default_factory=list)
    #: Registrations already on this machine for this target that did NOT start
    #: during this run and were left in place: name, source and why. Claude Code
    #: still loads them and reports them failing, so the operator is told.
    broken_existing: list[dict[str, str]] = field(default_factory=list)
    #: When nothing was registered, the candidate that came closest -- what it
    #: is, what it needs and how it failed -- so the report can say where to go
    #: next instead of only "nothing was found".
    best_lead: dict[str, Any] = field(default_factory=dict)
    status: str = Status.SKIPPED
    detail: str = ""
    #: Verdicts already reached for this target, keyed by the launch that was
    #: trialled -- see :meth:`CandidateVerifier.launch_key`. Shared by every
    #: stage and every agent attempt on the target, and by both pipelines, so
    #: that a launch proposed a second time is answered from here instead of
    #: being downloaded and started again. Not reported: it is a cache of
    #: verdicts every candidate already carries.
    trials: dict[str, Handshake] = field(default_factory=dict, repr=False, compare=False)

    @property
    def succeeded(self) -> bool:
        """A verified server Claude Code itself did not fail (:attr:`consumer_failed`)."""
        return any(c.identity() not in self.consumer_failed for c in self.verified)

    def absorb(
        self,
        stage: StageOutcome,
        provisional_limit: int = 1,
        *,
        prefer_detected_api: bool = True,
        proven_replaces_unproved: bool = False,
    ) -> None:
        """Fold one stage's results into the target's -- both kinds of find.

        Shared by both pipelines so that an endpoint which answered 401 cannot
        be picked up by one of them and quietly dropped by the other; the two
        lists stay disjoint, since a candidate that later verified is no longer
        waiting on anything.

        ``provisional_limit`` is spent here, across the whole target, and that
        is the point of doing it here rather than in
        :meth:`StageOutcome.note_provisional` alone. A per-stage cap of one is
        not a cap: four stages keep four candidates, and ``jira.example.com`` came
        out of an ordinary run -- no ``--exhaustive-mcp`` -- with a package entry
        and a bridge entry side by side, only one of which worked. The stage
        still picks its own best; this decides how many of those best ones the
        target is willing to register, and the earliest stage wins, because
        stage order is tier order and a tier-1 lead outranks a tier-3 one.
        """
        self._judge_refusals([*[c for s in self.stages for c in s.candidates], *stage.candidates], stage)
        for candidate in stage.verified:
            if candidate not in self.verified:
                self.verified.append(candidate)
        # Disjoint in both directions: something registered provisionally by an
        # earlier stage and verified by a later one is no longer waiting on a
        # credential, and must not go on holding a slot in the budget below.
        self.provisional = [c for c in self.provisional if c not in self.verified]
        # A server whose account has no API access holds its slot only until
        # something this account *can* use turns up: verified, or waiting on a
        # credential that may work. Without this the earliest stage's
        # unusable answer kept the slot, and the bridge found below it for the
        # API the account can use was never registered.
        if self.verified:
            self.provisional = [c for c in self.provisional if not _no_access(c)]
        # A bridge for the alternative API whose access is *proved* replaces a
        # server whose access never was (every call empty or unchecked) -- the
        # descent only reached it because of that (see
        # TraditionalPipeline.unproved_with_alternative). sd.example.com's
        # mcp-atlassian got [] for a customer account; the customer-API bridge
        # got that account's requests.
        #
        # ...and so does any server whose login a call proved, with
        # ``proven_replaces_unproved``: the descent only continues past a
        # verified tier because nothing there proved access, so what proves it
        # below is the answer. gitlab.example.net's package tier verified
        # efficient-gitlab (every call public -- nothing proved), the bridge tier
        # proved gitlab-example-net's login, and both were written for one address.
        if any(_proven_alternative(c) for c in [*stage.verified, *stage.candidates]) or (
            proven_replaces_unproved and any(_login_accepted(c) for c in stage.verified)
        ):
            self.verified = [c for c in self.verified if not _unproved(c)]
            self.provisional = [c for c in self.provisional if not _unproved(c)]
        # A refusal nobody can attribute -- the candidate chose how to send the
        # credential -- gives its slot up to a candidate whose access a call proved.
        if any(proves_access(c) for c in [*stage.verified, *stage.candidates]):
            self.provisional = [c for c in self.provisional if not unconfirmed_refusal(c)]
        self.provisional = [c for c in self.provisional if not c.start_failure]
        for candidate in stage.note_provisional(provisional_limit, prefer_detected_api=prefer_detected_api):
            if candidate in self.provisional or candidate in self.verified:
                continue
            if provisional_limit and len(self.provisional) >= provisional_limit:
                unusable = next((c for c in self.provisional if _no_access(c) and _replaceable(c, candidate)), None)
                if unusable is None and _proven_alternative(candidate):
                    unusable = next((c for c in self.provisional if _unproved(c)), None)
                if unusable is None and proof_rank(candidate) == 0:
                    # Proof across stages, not only within one: an accepted
                    # login takes the slot of the least-proved candidate an
                    # earlier tier left there. sd.example.com's package stage kept
                    # @atlassian-dc-mcp/jira, never login-checked, while the
                    # bridge a tier down had its login accepted.
                    worst = max(self.provisional, key=proof_rank)
                    unusable = worst if proof_rank(worst) > 0 else None
                if (
                    unusable is None
                    and prefer_detected_api
                    and built_for_api(candidate)
                    and not proves_access(candidate)
                ):
                    # Neither proved: the server built for the API this host
                    # was *seen* to serve outranks one that was not. sd.example.com
                    # serves a Service Management customer API (jira.example.com
                    # does not); with no credential of its own on the machine,
                    # mcp-atlassian -- whose calls a customer account gets sent
                    # to the portal for -- kept the slot over the bridge for it.
                    # (mcp.discovery.auth.prefer_detected_api)
                    unusable = next(
                        (
                            c
                            for c in self.provisional
                            if not built_for_api(c) and not proves_access(c) and proof_rank(c) >= proof_rank(candidate)
                        ),
                        None,
                    )
                if unusable is None and candidate.worked_before and not proves_access(candidate):
                    # Neither proved: the entry known to have worked on this
                    # machine keeps its place over one nothing ever proved.
                    # gitlab.example.net's connected bridge lost to an mcp-graphql
                    # that only waited for a token, then took 70 s to start.
                    # (mcp.discovery.reuse.worked_before_wins)
                    unusable = next(
                        (
                            c
                            for c in self.provisional
                            if not c.worked_before and not proves_access(c) and proof_rank(c) >= proof_rank(candidate)
                        ),
                        None,
                    )
                if unusable is None and _evidence(candidate) != _evidence_neutral() and not proves_access(candidate):
                    # Neither proved, and the run knows more for this one: the
                    # login its address's API host asks for, fewer values to
                    # supply, a scheme the host never refused (:mod:`.evidence`).
                    unusable = next(
                        (
                            c
                            for c in self.provisional
                            if not proves_access(c)
                            and proof_rank(c) >= proof_rank(candidate)
                            and not c.worked_before
                            and not (c.api_kind and not candidate.api_kind)
                            and _evidence(c) > _evidence(candidate)
                        ),
                        None,
                    )
                    if unusable is not None:
                        candidate.add_note(
                            f"registered in place of {unusable.label}: neither proved access, "
                            f"and the run knows more for this one ({candidate.evidence_why})"
                        )
                        self.provisional.remove(unusable)
                        self.provisional.append(candidate)
                        continue
                if unusable is None and not proves_access(candidate) and _evidence_everywhere():
                    # Whatever rule above did not decide, the one comparison
                    # every chooser shares does (evidence_key): the tier order
                    # alone never keeps a slot for what is known to be worse.
                    # sd.example.com's package stage kept @atlassian-dc-mcp/jira
                    # over the bridge for the customer API it was seen to serve.
                    beaten = [
                        c
                        for c in self.provisional
                        if evidence_beats(candidate, c, prefer_detected_api=prefer_detected_api)
                    ]
                    if beaten:
                        worst = max(beaten, key=lambda c: choice_key(c, prefer_detected_api=prefer_detected_api))
                        candidate.add_note(
                            f"registered in place of {worst.label}: neither proved access, and what is "
                            "known ranks this one first (proof, history, the API it is built for, the "
                            "login the address asks for)"
                        )
                        self.provisional.remove(worst)
                        self.provisional.append(candidate)
                        continue
                if unusable is None or _no_access(candidate):
                    break
                self.provisional.remove(unusable)
                candidate.add_note(
                    f"registered in place of {unusable.label}, whose account has no API access"
                    if _no_access(unusable)
                    else f"registered in place of {unusable.label}: neither proved access, and this one is known "
                    f"to work here ({candidate.worked_before})"
                    if candidate.worked_before
                    and not unusable.worked_before
                    and proof_rank(candidate) <= proof_rank(unusable)
                    and not proves_access(candidate)
                    else f"registered in place of {unusable.label}: neither proved access, and this one is "
                    f"built for the {candidate.api_kind or 'product'} API this host was seen to serve"
                    if built_for_api(candidate)
                    and not built_for_api(unusable)
                    and proof_rank(candidate) == proof_rank(unusable)
                    else f"registered in place of {unusable.label}, which proved less (its login was "
                    f"{login_check.in_words(unusable.handshake.login) if unusable.handshake and unusable.handshake.login else 'never checked'})"
                )
            self.provisional.append(candidate)

    def _judge_refusals(self, pool: list[Candidate], stage: StageOutcome) -> None:
        """A credential one candidate's service accepted and another's refused: the refusing one is broken.

        Compared by fingerprint (:attr:`Handshake.login_credential`), never by
        value. The refusing candidate is taken out of the running -- not
        verified, not waiting -- with the reason, so the report does not tell
        the operator their working credential was rejected.
        """
        accepted = {
            c.handshake.login_credential: c
            for c in pool
            if c.handshake is not None and c.handshake.login_credential and c.handshake.login == login_check.ACCEPTED
        }
        for candidate in pool:
            shake = candidate.handshake
            if shake is None or shake.login != login_check.REJECTED or not shake.login_credential:
                continue
            other = accepted.get(shake.login_credential)
            if other is None or other is candidate:
                continue
            # Not waiting for anything either: no credential makes it work.
            shake.ok = shake.login_refused = shake.requires_auth = False
            shake.credential_pending = []
            shake.error = (
                f"the credential it was refused with was accepted by {other.label}'s service, so the "
                "credential is fine and this candidate sends it wrongly"
            )
            candidate.add_note(shake.error)
            self.provisional = [c for c in self.provisional if c is not candidate]
            self.verified = [c for c in self.verified if c is not candidate]
            stage.provisional = [c for c in stage.provisional if c is not candidate]
            stage.verified = [c for c in stage.verified if c is not candidate]

    def stage(self, name: str) -> StageOutcome | None:
        for outcome in self.stages:
            if outcome.stage == name:
                return outcome
        return None

    def all_findings(self) -> dict[str, Any]:
        merged: dict[str, Any] = {}
        for outcome in self.stages:
            merged.update(outcome.findings)
        return merged

    def to_dict(self) -> dict[str, Any]:
        return {
            "target": self.target,
            "base_url": self.base_url,
            "status": self.status,
            # Derived, but emitted anyway: "did this target end up with a working
            # server" is the one question every consumer of the report asks, and
            # re-deriving it from `verified` invites each of them to get it wrong.
            "succeeded": self.succeeded,
            "consumer_failed": list(self.consumer_failed),
            "detail": redact.redact(self.detail),
            "stages": [s.to_dict() for s in self.stages],
            "verified": [c.to_dict() for c in self.verified],
            "provisional": [c.to_dict() for c in self.provisional],
            "ranking": _scrub(self.ranking),
            "registered": self.registered,
            "verification": _scrub(self.verification),
            "playbook": self.playbook,
            "due_diligence": self.due_diligence.to_dict() if self.due_diligence else None,
            "duplicates": self.duplicates,
            "shared_secrets": self.shared_secrets,
            "api_kinds": dict(self.api_kinds),
            "auth_plan": dict(self.auth_plan),
            # Waiting, but not for a missing value: the one supplied was refused
            # by the service, or its account has no API access.
            "credential_refused": [
                {
                    "name": c.label,
                    "verdict": c.handshake.login,
                    "tool": c.handshake.login_tool,
                    "detail": c.handshake.login_detail,
                }
                for c in self.provisional
                if c.handshake is not None and c.handshake.login_refused
            ],
            "stale_registrations": _scrub(self.stale),
            "kept_existing": _scrub(self.kept_existing),
            "answered_existing": dict(self.answered_existing),
            "connected_existing": dict(self.connected_existing),
            "identification_undecided": self.identification_undecided,
            "refused": [c.to_dict() for c in self.refused],
            "broken_existing": _scrub(self.broken_existing),
            "best_lead": _scrub(self.best_lead),
        }


def slugify(value: str, *, fallback: str = "target") -> str:
    """A filesystem- and MCP-name-safe token derived from a URL or host."""
    text = re.sub(r"^[a-z]+://", "", str(value).strip().lower())
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return text or fallback


def clip(payload: Any, limit: int) -> Any:
    """``payload`` itself if its JSON fits in ``limit`` characters, else the
    first ``limit`` of them, marked. Findings are free-form and some are whole
    documents; a report and a prompt each have room for a summary. ``limit`` 0
    keeps everything."""
    try:
        text = json.dumps(payload, default=str)
    except (TypeError, ValueError):
        return str(payload)[:limit] if limit > 0 else str(payload)
    if limit <= 0 or len(text) <= limit:
        return payload
    return text[:limit] + "...(truncated)"


def _scrub(node: Any) -> Any:
    """Redact free-form finding payloads before they reach a report."""
    if isinstance(node, dict):
        return {k: _scrub(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_scrub(v) for v in node]
    if isinstance(node, str):
        return redact.redact(node)
    return node


__all__ = [
    "clip",
    "Transport",
    "Stage",
    "Mode",
    "MODE_TAG",
    "mode_tag",
    "Handshake",
    "Candidate",
    "StageOutcome",
    "TargetOutcome",
    "slugify",
    "waits_for_credential",
]
