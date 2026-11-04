"""Handing a verified server to Claude Code.

Every verified server ends up in **both** places, always -- there is no flag for
half of it:

* ``.mcp.json`` in the directory the command was run from. This is the file
  Claude Code reads for project-scoped servers, so it is both the record of the
  run and a working configuration; it can be reviewed, committed, or copied to
  another machine. It is written first, because it is the part that does not
  depend on the CLI being installed.
* ``claude mcp add-json <name> <json> --scope <scope>`` -- registers with the CLI
  so the server is usable in the next session without moving anything.

Both are idempotent, and :meth:`McpWriter.usage_instructions` is the paragraph
the operator gets telling them what to do with the file.

``.mcp.json`` holds ``mcpServers`` and nothing else.  The file exists to be
copied -- into another project, into a commit, onto a machine that has not been
set up yet -- and the copy is usually done by selecting the whole thing, so
anything in it that is not configuration is something the reader has to notice
and delete first.  Prose in a configuration file is a trap even when the parser
tolerates it.

Naming follows one rule, because these names are read side by side: the base
name is the target's full FQDN with dashes for dots (``mcp.deepwiki.com`` ->
``mcp-deepwiki-com``), and when one address yields several endpoints -- which is
what ``--exhaustive-mcp`` is for -- every one of them carries a suffix saying
which endpoint it is, rather than the first taking the bare name.

A credential never appears in either destination.  A candidate that needs one
carries the *variable name*, written as ``${NAME}``: the value is supplied by the
operator's environment when the server is launched, and nothing
initialize-claude-ai writes to disk or puts on a command line ever contains it.

That is also how an endpoint that answered **401 or 403** gets registered rather
than discarded.  Such an endpoint is a positive finding -- it exists, it is
listening, and it declined an anonymous client -- and throwing it away because
this run had no token means the operator obtains one and then has to re-run the
entire search to get a configuration out of it.  So it is written out
*provisionally*: the credential slot is there, named after the service and left
empty, and the entry becomes usable the moment the variable is exported.  The
form is ``"Authorization": "Bearer ${GITLAB_EXAMPLE_NET_TOKEN}"`` rather than an
empty ``env`` entry because that is the one Claude Code acts on -- it expands
``${NAME}`` in ``url`` and ``headers``, and when the variable is unset it says
so by name ("Missing environment variables: GITLAB_EXAMPLE_NET_TOKEN"), whereas an
``env`` block on an http server is ignored without comment.  Nothing about this
is allowed to read as success: a provisional registration is counted apart from
the verified ones everywhere it appears.

**A local server is not a remote one, and the slot is not written the same
way.**  A remote server reads its credential from a header, so the placeholder
is the only route it has.  A stdio server is a *process*, and one launched with
an extra unset variable is running a different command from the one that was
trialled.  Two were: ``mcp-bugzilla`` was proven to start without
``BUGZILLA_AUTH_MODE``, written with ``"BUGZILLA_AUTH_MODE": "${BUGZILLA_AUTH_MODE}"``,
and exited during start-up on the operator's machine.  So a stdio entry carries
a slot only for a variable the proven launch actually carried; the rest are
named in :meth:`McpWriter.briefing` with what each should hold, and the next
run -- with them exported -- trials the server carrying them and writes the slot
it proved.  See :attr:`.Candidate.proven_without`.

Whatever is written, :meth:`McpWriter._check_it_starts` then launches the
finished entry exactly as written before anything claims it works.  Every other
gate here judges the candidate; this one judges the artefact, and the two are
not the same object.

It is the *last* word rather than the only one.  The stage that produced the
candidate asked the same question first, through the same
:class:`~.healing.EntryDoctor` -- there, an entry that will not start can still
be repaired by the agent, and a candidate nothing can repair is dropped while
the tiers below it are still open.  By the time one reaches here it has usually
been healed already; this re-asks because the file is written from *this*
answer, and one implementation means the stage and the file cannot disagree.

What the servers cannot say about themselves -- where a generated bridge lives
and what has to be installed for it to run, every environment variable each one
needs by name, a warning when an entry already in the file points at the same
service -- is :meth:`McpWriter.briefing`.  The phase prints it in its own framed
block and it is captured in the log and in the JSON report, so it survives the
terminal without riding along inside a file that has to stay copy-pastable.

An earlier version of this program did write that briefing into the file, under
a top-level ``_instructions`` key, so :meth:`McpWriter.tidy` takes it back out
of any file that still has it.  It runs at the start of every phase-4 run and
not only when something is published: a run that discovers nothing leaves the
file exactly as it found it, and "exactly as it found it" is the one case where
the prose would survive forever.
"""

from __future__ import annotations

import copy
import json
import os
import re
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import urlsplit

from ..core import diskspace, redact
from ..core.config import fill
from ..core.context import RunContext
from ..core.fileio import write_generated, write_private
from ..core.logging_setup import get_logger
from ..core.secrets import Secret
from ..core.trust_store import TrustStore
from . import contract, known_good, login_check, trust
from .address import parse as parse_address
from .auth_profiles import profile_for
from .env_contract import EnvContract, Read
from .grounding import (
    GROUNDED,
    SERVED_ELSEWHERE,
    Grounding,
    endpoint_hosts,
    host_of,
    hosting_namespaces_of,
    hosts_named,
    identity_rule_of,
    same_organisation,
    same_service,
)
from .grounding import assess as ground
from .portability import ENTRY_VARIABLE, HOST_BOUND, Portability, assess, search_path
from .registry_client import product_tokens
from .secret_policy import (
    OPAQUE_MIN_CHARS,
    OPAQUE_MIN_ENTROPY,
    OPAQUE_MIN_RATIO,
    holds_a_credential,
    is_secret_name,
    looks_opaque,
    names_a_selector,
)
from .types import (
    KIND_TRANSPORTS,
    TRANSPORT_ALIASES,
    Candidate,
    Handshake,
    TargetOutcome,
    Transport,
    best_of,
    normalize_transport,
    proves_access,
    slugify,
    waits_for_credential,
)
from .verifier import CandidateVerifier, Check

log = get_logger("mcp.writer")

#: Fallback for ``mcp.registration.name_by_role_parts``: the login parts named
#: and described by what they hold. A token or key keeps the name its server
#: gives it -- its kind is not known from a name.
ROLE_PARTS = ("USERNAME", "PASSWORD")

#: How an environment variable is referenced from an MCP server entry. Claude
#: Code expands this form when it launches the server; the value itself stays in
#: the operator's environment.
ENV_PLACEHOLDER = "${{{name}}}"
#: A variable the server may read but need not: empty when unset. Claude Code
#: passes an unset bare ``${NAME}`` through as those literal characters, so a
#: slot that is allowed to be empty has to say so. See :mod:`.env_contract`.
OPTIONAL_PLACEHOLDER = "${{{name}:-}}"

#: Marks a name derived from an IPv6 literal, whose colons do not survive
#: slugging. Without it ``[::1]:8080`` is named ``1-8080``.
IPV6_NAME_PREFIX = "ipv6-"

#: How the credential variable of an authenticated endpoint is named when
#: nothing else named one. ``{SLUG}`` is the server's own name in the shape
#: environment variables take, so ``gitlab-example-net`` asks for
#: ``GITLAB_EXAMPLE_NET_TOKEN`` -- recognisably that service's, and not a generic
#: ``MCP_TOKEN`` that the second authenticated target would collide with.
DEFAULT_TOKEN_ENV_TEMPLATE = "{SLUG}_TOKEN"

#: MCP entry ``type`` for each transport, as Claude Code spells it.
#: Fallbacks for ``mcp.verification.login_check.user_steps``: refusals that
#: mean a step only the operator can take, and how each is said.
USER_STEP_PATTERNS = {
    "key_not_registered": ["not registered with your account", "every SSH key was tried"],
    "no_api_access": ["has NO API ACCESS", "no API access"],
}
USER_STEP_WORDING = {
    "key_not_registered": (
        "{host} {only}accepts the {scheme} login{said}, and none of this machine's keys ({tried}) is accepted for "
        "your account: register this machine's public key with that account, then re-run phase 4 for {target}"
    ),
    "no_api_access": (
        "{host} recognises the login {server} used but gives that account no API access: ask the service for API "
        "access for it, then re-run phase 4 for {target}"
    ),
    "needs_browser_login": (
        "{server} asked to open {hosts} to log in through a browser, which discovery never waits for: sign in once "
        "yourself{alternative}, then re-run phase 4 for {target}"
    ),
}

#: Fallback for ``mcp.registration.load_check.settings_files``: every settings
#: file Claude Code merges, as templates ({project}, {claude_home}).
LOAD_SETTINGS_FILES = (
    "{project}/.claude/settings.local.json",
    "{project}/.claude/settings.json",
    "{claude_home}/settings.json",
)

#: Fallback for ``mcp.registration.entry_shapes``: what an entry of each type
#: must carry and may carry, as Claude Code reads ``.mcp.json``.
ENTRY_SHAPES = {
    "stdio": {"required": ["command"], "allowed": ["type", "command", "args", "env"]},
    "http": {"required": ["url"], "allowed": ["type", "url", "headers", "oauth"]},
    "sse": {"required": ["url"], "allowed": ["type", "url", "headers", "oauth"]},
}

ENTRY_TYPES = {
    Transport.STDIO: "stdio",
    Transport.STREAMABLE_HTTP: "http",
    Transport.SSE: "sse",
}


@dataclass
class Registration:
    """What happened to one verified server."""

    name: str
    candidate: Candidate
    entry: dict[str, Any] = field(default_factory=dict)
    scope: str = ""
    registered: bool = False
    written: bool = False
    #: The registration was fully prepared but withheld because this is a dry run.
    simulated: bool = False
    #: Written and registered, but the endpoint never completed a handshake: it
    #: answered 401/403 and the credential slot in the entry is empty. Usable
    #: the moment :attr:`needs_env` is exported, and not before.
    provisional: bool = False
    #: Variables that must hold a value before this entry does anything, in the
    #: order they appear in the entry.
    needs_env: list[str] = field(default_factory=list)
    #: Whether this entry survives being copied to another machine, and what
    #: ties it down if not. See :mod:`.portability`.
    portability: Any = None
    #: Whether the entry says which service it talks to. See :mod:`.grounding`.
    grounding: Any = None
    #: Repairs that were applied and then re-proved, in the order they worked.
    #: An entry that needed one is still a good entry -- the point is that the
    #: change was verified, not that nothing was changed.
    repairs: list[str] = field(default_factory=list)
    #: Every repair that was *tried* and what came of it, including the ones
    #: that changed nothing. "We tried and failed" is a different report from
    #: "we did not try", and an operator deciding what to do next needs the
    #: difference.
    attempted_repairs: list[str] = field(default_factory=list)
    #: The entry of this name that was already in the file when the run started,
    #: when this run declined to replace it. It is left alone -- this run has
    #: nothing better to offer -- but the operator has to be told it is there
    #: and that nothing verified it today.
    superseded_nothing: Mapping[str, Any] | None = None
    #: The handshake from launching **this entry**, as written, rather than the
    #: candidate it came from. ``None`` when there was nothing to launch (a URL
    #: entry) or nothing may be launched (dry run). See
    #: :meth:`.CandidateVerifier.verify_as_written`.
    start_check: Any = None
    #: The entry before a dropped runtime setting was restored (:meth:`McpWriter._restore_runtime_tuning`).
    restored_from: Any = None
    #: Refused only so that an existing entry could keep its place (:meth:`McpWriter._holds_its_place`) --
    #: never by a gate -- and so the one written if that place stays empty (:meth:`McpWriter._write_refused_best`).
    held_back: bool = False
    #: What the value should look like and where it comes from, when the product
    #: is one whose credential is not a single opaque string. Empty otherwise.
    credential_hint: str = ""
    #: Why this entry was **not written at all** -- it failed a registration
    #: gate (grounding, portability). Distinct from :attr:`skipped`, which is
    #: about the CLI step and says nothing about the file. They were one field
    #: once, and the consequence was severe: a refusal set it, ``_register``
    #: then overwrote it with "the claude CLI is not installed", and the entry
    #: was written to ``.mcp.json`` regardless -- so both gates announced a
    #: decision they did not carry out, and the operator was told neither.
    refused: str = ""
    #: Why the *claude CLI* registration step did not happen. The entry is in
    #: the file either way; this is only about the CLI's own copy.
    skipped: str = ""
    #: The claude CLI already holds this name for a registration this run
    #: proved cannot work for this account (no API access), and this entry is
    #: its replacement: the CLI copy is removed and this one added. Left in
    #: place it would shadow the file's entry -- the CLI never re-adds a name
    #: it knows -- and every session would keep loading the unusable one.
    replaces_unusable: str = ""
    #: The optional login variables this entry reads that nothing sets yet: it
    #: answers what anybody may ask, and nothing that needs the account.
    anonymous_until: list[str] = field(default_factory=list)
    #: What Claude Code itself made of the entry, using it (:mod:`.claude_check`):
    #: ``{outcome, detail, tool}``. Not :attr:`claude_check`, which is ``claude mcp get``'s row.
    claude_usage: dict[str, str] = field(default_factory=dict)
    #: Whether Claude Code itself connects to the entry (:func:`.claude_check.connect_check`).
    claude_connect: dict[str, str] = field(default_factory=dict)
    #: What the claude CLI said when adding the entry failed; "" when it did not.
    cli_failed: str = ""
    #: The tools the registration it replaces offered, for the briefing: one
    #: looking for ``jira_get_issue`` should be told it is now ``get_request``.
    replaced_tools: list[str] = field(default_factory=list)
    detail: str = ""
    remediation: list[str] = field(default_factory=list)
    #: What Claude Code itself says about the server once it is registered, as
    #: the fourth line of the verification report. ``None`` until asked.
    claude_check: Any = None
    #: Entries already in ``.mcp.json`` that point at the same service as this
    #: one: ``{"name": ..., "target": ..., "why": ...}``.
    duplicates: list[dict[str, str]] = field(default_factory=list)
    #: Credential variables this entry shares with an entry for a different
    #: service: ``{"variable": ..., "name": ..., "other": ...}``.
    shared_secrets: list[dict[str, str]] = field(default_factory=list)
    #: The address this entry was found for.
    target: str = ""
    #: This run's own ``claude mcp add`` put it in the CLI -- the only case in
    #: which withdrawing it may take it back out. A name that was already known
    #: to the CLI belongs to whoever registered it.
    added_by_run: bool = False
    #: What ``.mcp.json`` held under this name before this run wrote it, so a
    #: withdrawal restores the operator's entry rather than deleting it.
    had_previous: bool = False
    previous_entry: Mapping[str, Any] | None = None
    #: Written, then taken back out again because the same claim turned up for
    #: an unrelated address. See :meth:`McpWriter._shared_claim`.
    withdrawn: bool = False
    #: The candidate tried in this one's place after it was refused for not
    #: starting (``mcp.registration.start_fallbacks``), so the refusal and the
    #: registration that follows under the same name read as one event.
    replaced_by: str = ""

    def note(self, text: str) -> None:
        """Add a clause to the detail line.

        Two things happen to every registration -- the file and the CLI -- and
        the operator wants both on the one line, so this accumulates rather than
        letting whichever ran last speak for the other.
        """
        if text and text not in self.detail:
            self.detail = f"{self.detail}; {text}" if self.detail else text

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "scope": self.scope,
            "registered": self.registered,
            "written_to_file": self.written,
            "simulated": self.simulated,
            "provisional": self.provisional,
            "claude_usage": dict(self.claude_usage),
            "claude_connect": dict(self.claude_connect),
            "needs_environment": self.needs_env,
            "credential_hint": self.credential_hint,
            **(self.portability.to_dict() if self.portability else {}),
            **(self.grounding.to_dict() if self.grounding else {}),
            "starts_as_written": None if self.start_check is None else self.start_check.ok,
            # What healing did, in the machine-readable record as well as on
            # screen. "We tried and failed" and "we did not try" are different
            # facts, and a tool reading this report needs both.
            "repairs_applied": list(self.repairs),
            "repairs_attempted": list(self.attempted_repairs),
            "left_an_earlier_entry_in_place": self.superseded_nothing is not None,
            "refused": self.refused,
            "skipped": self.skipped,
            "detail": redact.redact(self.detail),
            "entry": redact.redact_mapping(dict(self.entry)),
            "remediation": self.remediation,
            "claude_check": self.claude_check.to_dict() if self.claude_check else None,
            "duplicates": self.duplicates,
            "target": self.target,
            "withdrawn": self.withdrawn,
            "replaced_by": self.replaced_by,
        }


class McpWriter:
    """Publishes verified candidates to the CLI and to ``.mcp.json``."""

    def __init__(self, ctx: RunContext) -> None:
        self.ctx = ctx
        self.settings = ctx.settings
        self.config = ctx.settings.section("mcp")
        self.registration = ctx.settings.section("mcp.registration")
        #: Credential shapes learned this run, by package and kind of variable
        #: (see :func:`_shape_family`), for a sibling target of the same package.
        self._learned_shapes: dict[tuple[str, str], tuple[str, str]] = {}
        #: Addresses whose entry stopped working, with what changed since it last
        #: did (:meth:`_look_back`), for the phase's report.
        self.lookbacks: list[dict[str, Any]] = []
        #: How each address can be logged in to, in words (:func:`.login_methods.describe`).
        self.login_advice: list[dict[str, Any]] = []
        #: What only the operator can do, one step per line, each with how to
        #: confirm it (:meth:`_operator_steps`), for the phase's checklist.
        self.operator_steps: list[dict[str, str]] = []
        #: Used to launch finished entries, not candidates. See
        #: :meth:`_check_it_starts`.
        self.verifier = CandidateVerifier(ctx)
        #: What the launched script reads, so the entry can declare all of it.
        self.env_contract = EnvContract(ctx.settings)
        self._known: set[str] | None = None
        #: Everything published so far, across every target. The briefing is
        #: shown once at the end of the phase, so it has to outlive the call
        #: that produced each entry.
        self.published: list[Registration] = []
        #: Entry name -> the address it was written for in this run: two servers
        #: for one address are one instance, whatever hosts each reaches.
        self.address_of: dict[str, str] = {}
        #: ``(candidate, variable)`` whose credential default this run removed
        #: from the file, so the report says it plainly and what to do instead.
        self.removed_defaults: list[tuple[str, str]] = []
        #: Address -> its login plan (every route it offered), for a remedy to
        #: name the routes a refused login did not try.
        self.login_plans: dict[str, Mapping[str, Any]] = {}
        #: Why the project file could not be written, each reason once.
        self.write_failures: list[str] = []
        #: Every weakly-founded claim made so far, by launch identity, so the
        #: same claim for an unrelated address can be recognised. See
        #: :meth:`_shared_claim`.
        self._claims: dict[str, list[Registration]] = {}
        #: Entries published for an earlier address and withdrawn since, not yet
        #: reported. See :meth:`take_withdrawn`.
        self._withdrawn: list[Registration] = []

    # -- where the file goes -----------------------------------------------------

    @property
    def json_path(self) -> Path:
        """The MCP configuration file this run writes.

        Relative by default (``.mcp.json``), which puts it in the directory the
        command was run from -- the same place Claude Code looks for a project's
        MCP servers, so the file is usable where it lands instead of being a
        record of a run buried under ``~/.local/state``. An absolute
        ``mcp.mcp_json_path`` pins it elsewhere.
        """
        configured = self.config.path("mcp_json_path")
        if configured is None:
            configured = Path(".mcp.json")
        return configured if configured.is_absolute() else Path.cwd() / configured

    @property
    def display_path(self) -> str:
        """The file as the operator would type it, if they are standing in it."""
        path = self.json_path
        try:
            return f"./{path.relative_to(Path.cwd())}"
        except ValueError:
            return str(path)

    def usage_instructions(self) -> list[str]:
        """How to use the file that was just written -- here, or anywhere else.

        Three routes, cheapest first, because which one is right depends on
        something this program cannot know: whether these servers belong to this
        project, to this operator, or to a machine that has not been set up yet.
        """
        path = self.display_path
        scope = self.config.str_("register_scope", "local")
        claude_home = self.settings.path("paths.claude_home")
        home = str(claude_home) if claude_home else "~/.claude"
        return [
            f"Start Claude Code from this directory: it reads {path} on startup and "
            "asks you once to approve the servers in it.",
            f"For another project, copy the file into that project's root: cp {path} /path/to/project/.mcp.json",
            f"To have them in every project instead of this one, register them for your "
            f"user: claude mcp add-json <name> '<entry from {path}>' --scope user  "
            f"(Claude Code keeps user-scope servers with its own configuration, "
            f"alongside {home}).",
            *self._undo_instructions(path, scope),
        ]

    def _undo_instructions(self, path: str, scope: str) -> list[str]:
        """How to undo what this run registered, said as it is: the CLI copies it added, by scope; the rest is the file.

        This once said "registered by this run at --scope local; `claude mcp
        remove <name>` undoes it" for entries that lived only in the project
        file -- and a remove without a scope takes a project entry out of
        .mcp.json. Every remove is :meth:`remove_by_hand`'s, which names the scope.
        """
        added = sorted({r.name for r in self.published if r.registered and getattr(r, "added_by_run", False)})
        lines = []
        if added:
            lines.append(
                f"Added to the claude CLI by this run (--scope {scope}): "
                + "; ".join(f"{n}: `{self.remove_by_hand(n)}` undoes it" for n in added)
            )
        lines.append(
            f"The rest live in {path} (project scope): to undo one, delete its entry there -- never "
            "`claude mcp remove` without --scope, which removes the project file's entry."
        )
        return lines

    # -- naming ----------------------------------------------------------------

    def base_name(self, target: str) -> str:
        """The name a server gets when it is the only one found for an address.

        The full FQDN, dots turned into dashes -- ``api.example.com`` becomes
        ``api-example-com`` and not ``example`` or ``api``. Shortening it would
        make two addresses on one domain indistinguishable in ``claude mcp list``,
        which is the place these names are actually read.

        An IPv6 literal is marked as one. ``--mcp-server`` takes any address,
        including ``[::1]:8080``, and slugging that on its punctuation alone
        leaves ``1-8080``: unreadable, and indistinguishable from a host
        genuinely called ``1``. The whole point of these names is that somebody
        reading ``claude mcp list`` next month knows what they are looking at.
        """
        address = parse_address(target)
        host = address.host or ""
        if ":" in host:
            host = f"{IPV6_NAME_PREFIX}{host}"
        if host and address.port:
            host = f"{host}-{address.port}"
        prefix = self.config.str_("name_prefix", "")
        return slugify(f"{prefix}{host or target}" if prefix else (host or target))

    def name_all(self, candidates: Sequence[Candidate], target: str) -> list[str]:
        """Names for every endpoint being written out for one address.

        One endpoint gets the bare FQDN. Several -- what ``--exhaustive-mcp``
        asks for -- each get a suffix saying which endpoint they are, including
        the first: ``mcp-deepwiki-com-mcp`` and ``mcp-deepwiki-com-sse`` are a
        pair someone can choose between, whereas ``mcp-deepwiki-com`` beside
        ``mcp-deepwiki-com-sse`` reads as though the second were an afterthought.
        """
        base = self.base_name(target)
        if len(candidates) < 2:
            return [base] * len(candidates)
        from .existing import ExistingRegistrations  # imports this module

        # What is registered now claims its own name first: build-opensuse-org's
        # osc-mcp entry became build-opensuse-org-stdio because the remembered
        # client bridge, tried first, took the bare name -- and every tool name
        # the operator knew changed with it. (mcp.registration.current_keeps_name)
        existing = ExistingRegistrations.for_run(self.ctx)
        order = list(range(len(candidates)))
        if self.registration.bool_("current_keeps_name", True):
            order.sort(key=lambda i: 0 if existing.registration_for(candidates[i]) is not None else 1)
        names: list[str] = [""] * len(candidates)
        used: set[str] = set()
        for i in order:
            suffix = self._distinguisher(candidates[i], target)
            names[i] = self._unique(self._compose(base, suffix), candidates[i], used)
        return names

    def server_name(self, candidate: Candidate, target: str, taken: Iterable[str] = ()) -> str:
        """One stable, readable, collision-free name for a discovered server."""
        base = self.base_name(target)
        used = set(taken)
        if base not in used:
            return base
        # More than one server on the same target: distinguish by what it is.
        return self._unique(self._compose(base, self._distinguisher(candidate, target)), candidate, used)

    @staticmethod
    def _compose(base: str, suffix: str) -> str:
        """Join the address name and the distinguisher without saying it twice.

        The distinguisher for a local server is the name its discoverer gave it,
        and a discoverer that has just been asked about ``jira.example.com`` tends
        to call what it found ``jira-example-com-atlassian-dc-mcp``. Prefixing the
        address to that produced
        ``jira-example-com-jira-example-com-atlassian-dc-mcp-b1ff``, which is not
        wrong so much as unreadable -- and it is not specific to one host or one
        agent, so trimming that one string would leave the next one to be found
        by whoever read the next ``.mcp.json``.

        So: drop the base from the front of the suffix when the suffix repeats
        it *whole*, however many times. Nothing less than the whole base, which
        is the restraint that matters -- trimming merely the labels the two have
        in common turns the second host of a pair into
        ``mcp-deepwiki-com-devin-ai``, because ``mcp.devin.ai`` and
        ``mcp.deepwiki.com`` both begin with ``mcp``, and the name no longer
        names the host it belongs to. A suffix that merely overlaps, or an
        unrelated one such as ``bridge`` or ``sse``, is left alone: a little
        repetition is cheaper than a name that is wrong.
        """
        if not suffix:
            return base
        base_labels = [label for label in base.split("-") if label]
        labels = [label for label in suffix.split("-") if label]
        while labels[: len(base_labels)] == base_labels:
            labels = labels[len(base_labels) :]
        return "-".join([*base_labels, *labels]) if labels else base

    def _unique(self, proposed: str, candidate: Candidate, used: set[str]) -> str:
        """Trim, de-duplicate and reserve a name."""
        name = proposed[:64].strip("-") or slugify(candidate.label, fallback="mcp-server")
        if name in used:
            # Same path on two transports: say which, before resorting to a number.
            transport = slugify(candidate.transport, fallback="")
            if transport and f"{name}-{transport}" not in used:
                name = f"{name}-{transport}"
        while name in used:
            for index in range(2, 100):
                if f"{name}-{index}" not in used:
                    name = f"{name}-{index}"
                    break
            else:  # pragma: no cover - a hundred endpoints on one address
                name = f"{name}-{os.getpid()}"
        used.add(name)
        return name

    @staticmethod
    def _distinguisher(candidate: Candidate, target: str) -> str:
        """What tells this server apart from the others found for one address.

        Registering several servers for one target is ordinary now that
        --exhaustive-mcp exists, so these names are read side by side. Slugging
        the whole URL is unique but unreadable -- a second endpoint on another
        host came out as ``mcp-deepwiki-com-mcp-devin-ai-mcp`` -- so use only the
        part that actually differs: another host is named by its host, another
        endpoint on the same host by its path.
        """
        if candidate.kind == "stdio":
            return slugify(candidate.name or " ".join(candidate.full_command()), fallback="local")
        address = parse_address(candidate.url)
        wanted = parse_address(target)
        if address.host and address.host != wanted.host:
            host = f"{IPV6_NAME_PREFIX}{address.host}" if ":" in address.host else address.host
            return slugify(host, fallback="alt")
        return slugify(address.path or candidate.name, fallback="alt")

    # -- credentials for an endpoint that would not talk anonymously ------------

    def auth_profile(self, candidate: Candidate) -> dict[str, str]:
        """What this particular product calls its credential, and its shape.

        "Set ``OPENQA_OPENSUSE_ORG_TOKEN``" is only useful advice where the
        product actually issues something called a token. openQA does not: its
        MCP endpoint wants ``username:key:secret``, three values joined by
        colons, and an operator told to supply a token will go looking for one
        that does not exist. The generic name is right for most targets and
        wrong for some, so the exceptions are data:
        ``[mcp.discovery.auth.profiles.<name>]`` matches a host pattern and
        says what the variable should be called, what the value looks like and
        where to get it. Adding a product is a TOML entry.

        Returns ``{}`` when no profile matches, which is the ordinary case.
        """
        return profile_for(self.settings, candidate.url)

    def credential_shape(self, candidate: Candidate, variable: str = "") -> tuple[str, str]:
        """``(shape, where_to_get_one)`` for one credential variable.

        Discovery first, then the variable's own name, then configuration. A
        shape learned while looking at the product beats a table somebody wrote
        in advance, and it is the only thing that works for a product nobody has
        written a table entry for -- which is most of them.

        ``variable`` matters. This used to answer for the *candidate*, returning
        the first shape it found and letting one sentence stand for every
        variable the entry needed. ``mcp-bugzilla`` needs two, and
        ``@ivotoby/openapi-mcp-server`` needs ``API_HEADERS`` -- a
        ``Name:value`` list, not a token -- and both were described with the
        same words: "check its own documentation ... may call it an API token".
        The operator exported a bare token into a header list, the parser
        rejected it, and the server exited. One variable, one answer.
        """
        if variable and variable not in candidate.secret_env:
            # Asked by the exported name (SD_EXAMPLE_COM_JIRA_PERSONAL_TOKEN); the
            # shape was learned under the key the server reads.
            variable = next((k for k, e in candidate.exported_names.items() if e == variable), variable)
        wanted = [variable] if variable else list(candidate.secret_env)
        for name in wanted:
            shape = candidate.secret_formats.get(name, "").strip()
            if shape:
                family = _shape_family(candidate, name)
                if family[0]:
                    self._learned_shapes.setdefault(family, (shape, candidate.secret_sources.get(name, "").strip()))
                return shape, candidate.secret_sources.get(name, "").strip()
        for name in wanted:
            # Learned earlier in this run for the same package and the same kind
            # of variable (CONFLUENCE_PERSONAL_TOKEN -> JIRA_PERSONAL_TOKEN): the
            # same product issues the same kind of credential.
            family = _shape_family(candidate, name)
            learned = self._learned_shapes.get(family) if family[0] else None
            if learned:
                return learned
        for name in wanted:
            shape = self.shape_from_name(name)
            if shape:
                return shape, candidate.secret_sources.get(name, "").strip()
        for name in wanted:
            described = self.manifest_description(candidate, name)
            if described[0]:
                return described
        profile = self.auth_profile(candidate)
        if not profile.get("shape", "").strip() and self.registration.bool_("name_by_role", True):
            # What the slot holds, when its name says so (a user name, a
            # password): never "shape unknown" for a password.
            from . import bridge_auth
            from .inventory import slot_part

            for name in wanted:
                part = slot_part(name, self.settings)
                if part in self.registration.str_list("name_by_role_parts", list(ROLE_PARTS)):
                    host = candidate.secret_sources.get(name, "").strip() or self.service_host(candidate)
                    if host:
                        return bridge_auth.describe_part(part, host, self.settings), host
        return profile.get("shape", "").strip(), profile.get("obtain", "").strip()

    def manifest_description(self, candidate: Candidate, variable: str) -> tuple[str, str]:
        """``(what the variable holds, where to get it)`` from the generated bridge's own manifest; ``("", "")`` without one.

        A kept entry is rebuilt from the file and arrives knowing nothing the
        generated candidate knew: sd-example-com's hint fell back to "shape unknown"
        while its manifest said ``auth: bearer``, variable ``SD_EXAMPLE_COM_TOKEN``.
        What this program wrote into the manifest is evidence, so it is read
        back. (``mcp.registration.describe_from_manifest``.)
        """
        import json as _json

        from . import bridge_auth
        from .portability import expand

        if not self.registration.bool_("describe_from_manifest", True):
            return "", ""
        path = next((expand(str(v)) for k, v in candidate.env.items() if str(k).endswith("_MANIFEST") and v), "")
        if not path or not os.path.isfile(path):
            return "", ""
        try:
            manifest = _json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return "", ""
        host = parse_address(str(manifest.get("base_url") or "")).host or self.service_host(candidate)
        auth = manifest.get("auth") or {}
        for part, name in (auth.get("variables") or {}).items():
            if name == variable and host:
                text = bridge_auth.describe_part(str(part).upper(), host, self.settings)
                return f"{text}, sent as {bridge_auth.method_in_words(auth)}", host
        return "", ""

    def shape_from_name(self, variable: str) -> str:
        """What the variable's own name says its value looks like, or ``""``.

        The weakest of the three sources and still worth asking, because it is
        the one that is always available: nothing discovered ``API_HEADERS``'s
        format, no profile matched ``build.example.net``, and the name was saying
        "headers" the whole time. ``[mcp.discovery.auth.name_shapes]`` maps a
        pattern to a sentence, so adding a kind of value is a TOML entry and
        no product is named in code.
        """
        text = str(variable or "")
        if not text:
            return ""
        for pattern, shape in self.settings.dict_("mcp.discovery.auth.name_shapes", {}).items():
            try:
                if re.search(str(pattern), text, re.IGNORECASE):
                    return str(shape).strip()
            except re.error as exc:
                log.warning("auth name_shapes pattern %s is unusable: %s", pattern, exc)
        return ""

    def credential_placeholder(self, candidate: Candidate, variable: str) -> str:
        """What to show after ``VARIABLE=`` in an example the operator copies.

        ``<your real token>`` for everything was wrong for every variable that
        is not a token, and it was wrong in the worst way: it is a sentence the
        operator can act on, so they did. When the shape is known it goes here;
        when it is not, the placeholder says so rather than inventing a kind.
        """
        shape, _ = self.credential_shape(candidate, variable)
        if not shape:
            return "<value -- shape unknown, check the product's documentation>"
        return f"<{shape}>"

    def service_host(self, candidate: Candidate) -> str:
        """The host a candidate serves: its URL's, or the first its launch line or ``env`` names.

        A local server has no URL, so "check its documentation ( may call it a
        token ...)" was printed with the host missing. Its configuration names
        the instance it talks to.
        """
        if candidate.url:
            return parse_address(candidate.url).host or ""
        named = hosts_named([*candidate.full_command(), *candidate.env.values()])
        return named[0] if named else ""

    def alternative_hint(self, candidate: Candidate, variables: Sequence[str], outcome: TargetOutcome) -> str:
        """The alternative API to try if this server's access cannot be checked; "" when there is none.

        For a server registered without its access proved (no credential to
        check with, or every call empty) on a target that serves a bridgeable
        API kind it does not speak (``login_check.remedies.alternative``).
        sd.example.com's first run would otherwise register mcp-atlassian and
        say nothing about the customer API it had already found.
        """
        from . import api_kinds

        shake = candidate.handshake
        if shake is not None and shake.login == login_check.ACCEPTED:
            return ""
        if not variables and not (shake is not None and shake.login in login_check.UNPROVED):
            return ""  # needs nothing and was not found wanting: a working server, no hint
        others = [
            kind.label
            for kind, _ in api_kinds.bridgeable(self.settings, outcome.api_kinds or {})
            if kind.name != candidate.api_kind
        ]
        if not others:
            return ""
        return login_check.remedy(
            "alternative",
            host=host_of(outcome.target) or self.service_host(candidate),
            variables=list(variables) or self.exported_variables(candidate),
            detail="",
            settings=self.settings,
            kinds=", ".join(f"a {label} API" for label in others),
        )

    @staticmethod
    def variable_read_for(entry: Mapping[str, Any], key: str, candidate: Candidate) -> str:
        """The variable the written ``entry`` reads for ``key`` -- the one an operator must set.

        An instruction must name what the entry reads, whoever named it: build-example-net's
        report said "needs OBS_USERNAME" while its entry read
        ``${BUILD_EXAMPLE_NET_OBS_USERNAME:-}``, because the slot was named by the
        source reader and ``exported_names`` never heard of it.
        """
        for block in ("env", "headers"):
            value = (entry.get(block) or {}).get(key)
            if value is not None:
                found = ENTRY_VARIABLE.search(str(value))
                if found:
                    return found.group(1)
        named = candidate.exported_names.get(key, key)
        text = json.dumps(entry)
        if entry.get("url") and key not in (entry.get("env") or {}) and "${" + named not in text:
            # The slot's name is read nowhere in the entry: a URL entry's
            # credential header already spoke for it (openqa-example-net's
            # `Bearer ${OPENQA_EXAMPLE_NET_AUTHORIZATION}` while the report asked for
            # OPENQA_EXAMPLE_NET_USERNAME_KEY_SECRET). Name what the header reads.
            for value in (entry.get("headers") or {}).values():
                found = ENTRY_VARIABLE.search(str(value))
                if found:
                    return found.group(1)
        return named

    def exported_variables(self, candidate: Candidate) -> list[str]:
        """The variables an operator exports for ``candidate``, as its entry reads them."""
        return sorted(
            {
                candidate.exported_names.get(key, key)
                for key in candidate.secret_env
                if not self.env_contract.session(key)
            }
        )

    def login_remedy(self, candidate: Candidate, variables: Sequence[str], target: str = "") -> str:
        """What to do about a credential the service refused; "" when none was refused."""
        shake = candidate.handshake
        if shake is None or not shake.login_refused:
            return ""
        host = host_of(candidate.url or target) or host_of(target) or self.service_host(candidate)
        named = list(variables) or self.exported_variables(candidate)
        detail = shake.login_detail or shake.login_tool
        if shake.login_source:
            # The value tested was the one written in the existing entry, not an
            # exported one: say so, and where the replacement goes.
            detail = f"tested with {shake.login_source}: {detail}"
        remedy = login_check.remedy(
            shake.login,
            host=host,
            variables=named,
            detail=detail,
            settings=self.settings,
            reason=shake.login_reason,
        )
        if shake.login == login_check.REJECTED and not login_check.credential_missing(
            shake.login_detail, self.settings
        ):
            other = self.other_routes(candidate, target)
            if other:
                remedy = f"{remedy} {other}".strip()
        return remedy

    def other_routes(self, candidate: Candidate, target: str) -> str:
        """The login routes ``target`` offers besides the one ``candidate`` was refused on, with how to set each up; "".

        build.example.net refused a Example login over basic auth again and again,
        and every remedy said "create a new one and export it" -- while its API
        host answered ``Signature realm="Use your developer account"``: an SSH
        key on the account, a route that was never presented. A credential
        refused on one route may be the right account on a route that is closed
        to it; the other routes the service offered, with each one's own setup
        step, are named beside the refusal.
        (``mcp.verification.login_check.name_other_routes``.)
        """
        from . import client_bridge
        from .inventory import slot_part

        if not self.settings.bool_("mcp.verification.login_check.name_other_routes", True):
            return ""
        plan = self.login_plans.get(target) or {}
        offered = plan.get("offered") or []
        if not offered:
            return ""
        parts = {slot_part(k, self.settings) for k in candidate.secret_env}
        used = "basic" if "PASSWORD" in parts else str((plan.get("chosen") or {}).get("method") or "").lower()
        origin = f"https://{host_of(target) or target}"
        routes: list[str] = []
        for route in offered:
            scheme = str(route.get("scheme") or "")
            if not scheme or scheme.lower() == used:
                continue
            realm = re.search(r'realm="([^"]*)"', str(route.get("evidence") or ""))
            setup = ""
            for name, client in client_bridge.clients(self.settings).items():
                if scheme.lower() in {str(k).lower() for k in (client.get("logins") or {})}:
                    hint = str(client.get("key_register_hint") or "").replace("{origin}", origin)
                    setup = f" -- through {name}" + (f": {hint}" if hint else "")
                    break
            routes.append(f"{scheme} at {route.get('host')}" + (f" ('{realm.group(1)}')" if realm else "") + setup)
        if not routes:
            return ""
        return (
            f"If this is the right account, the route it was tried on may be closed to it: {host_of(target) or target} "
            f"also offers {'; '.join(routes)}. Re-run phase 4 once that route is set up."
        )

    def credential_instruction(self, candidate: Candidate, variable: str) -> str:
        """``NAME=<shape> -- where to get one``, for one variable.

        Both halves or neither. The shape without the source sends an operator
        looking in the wrong place; the source without the shape is what told
        somebody to paste a bare token into a header list.
        """
        line = f"{variable}={self.credential_placeholder(candidate, variable)}"
        _, where = self.credential_shape(candidate, variable)
        if not where:
            return line
        host = self.service_host(candidate)
        return f"{line} -- get it from {where.replace('{host}', host)}"

    def name_from_shape(self, slug: str, shape: str) -> str:
        """``username:key:secret`` -> ``<SLUG>_USERNAME_KEY_SECRET``, or ``""``.

        A variable named after one part of a multi-part value is worse than a
        vague one: ``OPENQA_..._API_KEY`` reads as an instruction to set the
        key, and an operator who does exactly that gets a 403 with nothing to
        explain it. Naming it after every part makes the name the instruction.

        Only a shape that is plainly composite produces a name -- a single
        opaque token keeps the ordinary naming, because ``..._TOKEN`` is
        accurate there and shorter.
        """
        separators = self.settings.str_list("mcp.discovery.auth.shape_separators", [":", "/", "|"])
        parts: list[str] = [shape]
        for separator in separators:
            if separator and separator in shape:
                parts = shape.split(separator)
                break
        else:
            return ""
        cleaned = []
        for part in parts:
            token = re.sub(r"[^A-Za-z0-9]+", "_", part.strip()).strip("_").upper()
            # A part that is a sentence rather than a field name means the
            # shape was prose; naming a variable after prose helps nobody.
            if not token or len(token) > 24 or token.count("_") > 2:
                return ""
            cleaned.append(token)
        limit = self.settings.int_("mcp.discovery.auth.shape_max_parts", 4)
        if not 2 <= len(cleaned) <= limit:
            return ""
        return f"{slug}_{'_'.join(cleaned)}"

    def credential_env_name(self, candidate: Candidate, name: str) -> str:
        """The variable an authenticated endpoint's token should come from.

        Whatever named one first wins, because it is better informed than any
        rule here: an agent that read the vendor's documentation knows the
        product calls it ``GITLAB_TOKEN``, and the operator who set
        ``mcp.discovery.auth.bearer_token_ref`` has already said where theirs
        lives. Next comes a matching :meth:`auth_profile`, which knows what the
        product calls it. Only when none of those spoke is a name derived, from
        the server's own name through
        ``mcp.discovery.auth.token_env_template`` -- per service, so two
        authenticated targets in one run do not both claim one variable.
        """
        slug = (name or self.base_name(candidate.url)).upper().replace("-", "_")

        # The operator's own reference first, always. Someone who said where
        # their credential lives has answered the question, and no derivation
        # of ours gets to rename their variable.
        reference = self.settings.str_("mcp.discovery.auth.bearer_token_ref")
        if reference.startswith("env:") and reference[4:].strip():
            return reference[4:].strip()

        # Then a shape, which beats a *name* -- including a name the agent
        # proposed, because the shape usually comes from the agent too and is
        # the better of its two statements. Told "the variable is
        # OPENQA_..._API_KEY" and "the value is username:key:secret", the
        # second is what the operator needs the name to say.
        shape, _ = self.credential_shape(candidate)
        if shape:
            derived = self.name_from_shape(slug, shape)
            if derived:
                return derived

        for existing in candidate.secret_env:
            return existing
        template = self.auth_profile(candidate).get("env") or self.settings.str_(
            "mcp.discovery.auth.token_env_template", DEFAULT_TOKEN_ENV_TEMPLATE
        )
        return template.replace("{SLUG}", slug).replace("{slug}", slug.lower())

    def credential_hint(self, candidate: Candidate, variable: str = "") -> str:
        """One sentence telling the operator what to put in one variable.

        An empty slot is only half an instruction. The other half is the shape
        of the value and where it comes from, and for anything whose credential
        is not a single opaque string that is the difference between a
        five-second export and a support question.
        """
        shape, where = self.credential_shape(candidate, variable)
        if not shape:
            profile = self.auth_profile(candidate)
            shape, where = profile.get("format", ""), where or profile.get("obtain", "")
        host = self.service_host(candidate)
        if not shape and not where:
            # No profile matched, so we do not know what this product issues --
            # and saying nothing implies we do, because the variable is called
            # ..._TOKEN. openQA taught this one: its credential is three values
            # joined by colons, and an operator told only the name went looking
            # for a token that does not exist. Admitting the gap costs a line
            # and is true of every product nobody has written a profile for.
            return (
                "the shape of this value is whatever the product issues -- check its own "
                f"documentation ({host} may call it an API token, a personal access token, "
                "or a key and secret that have to be combined)"
            )
        parts = []
        if shape and ":" in shape:
            # A composite -- username:key:secret -- is what the old wording was for.
            parts.append(f"the value is {shape}, not a single opaque token")
        elif shape:
            # One part of a credential in parts, described: "your user name on X".
            parts.append(f"the value is {shape}")
        if where:
            parts.append(f"get it from {where.replace('{host}', host)}")
        return "; ".join(parts)

    def prepare_provisional(self, candidate: Candidate, name: str) -> list[str]:
        """Give an authenticated endpoint the empty credential slot it needs.

        Returns the variable names the operator has to fill in. The reference is
        recorded on the candidate, not just in the entry, so that everything
        downstream which already knows how to say "this server needs a variable
        exported" -- the briefing, the final verdict, the ranking -- picks this
        up without being taught about authentication separately.
        """
        # A credential in parts -- a user name and a password -- keeps every
        # part under the name the server reads it by. Reducing it to the one
        # "token" variable below would drop the other part, and with it the
        # login. See verifier._companion_test and bridge_auth.
        if len(candidate.secret_env) > 1:
            return sorted(candidate.secret_env)
        carried = sorted(
            {
                m.group(1)
                for value in candidate.env.values()
                for m in ENTRY_VARIABLE.finditer(str(value))
                if is_secret_name(m.group(1))
            }
        )
        if candidate.kind == "stdio" and carried and not candidate.secret_env:
            # The credential already travels inside a setting the server reads
            # (HEADERS = '{"Authorization": "Bearer ${GITLAB_EXAMPLE_NET_TOKEN}"}'):
            # that variable is the slot; a second, unread one is not added.
            return carried
        if (
            candidate.kind == "stdio"
            and candidate.secret_env
            and self.registration.bool_("keep_server_slot_keys", True)
        ):
            # A local server reads its credential under its own key; renaming
            # the key gives it a variable it never reads. osc-mcp's slot became
            # BUILD_OPENSUSE_ORG_OSC_MCP_USER, which osc-mcp does not read and
            # nobody exports. The variable the key points at is named by
            # _exported_name; the key stays.
            return sorted(candidate.secret_env)
        variable = self.credential_env_name(candidate, name)
        if variable in candidate.secret_env:
            return sorted(candidate.secret_env)

        # Renamed, not added alongside. Leaving the superseded name in place
        # would put both in the entry and let the *worse* one win the header --
        # ${OPENQA_..._API_KEY} sorts before ${OPENQA_..._USERNAME_KEY_SECRET},
        # so the operator would be told to set the accurate variable and the
        # file would read the misleading one. One credential, one name.
        superseded = [
            existing
            for existing in candidate.secret_env
            if existing != variable and not candidate.headers.get(existing)
        ]
        for old in superseded:
            candidate.secret_env.pop(old, None)
            shape = candidate.secret_formats.pop(old, "")
            if shape:
                candidate.secret_formats.setdefault(variable, shape)
            where = candidate.secret_sources.pop(old, "")
            if where:
                candidate.secret_sources.setdefault(variable, where)
            candidate.add_note(
                f"the credential variable is {variable}, not {old}: the value is "
                f"{shape or 'not a single opaque token'}, and a name that says so is the "
                "instruction. Export the new name."
            )
        candidate.secret_env[variable] = f"env:{variable}"
        return sorted(candidate.secret_env)

    def _credential_headers(self, candidate: Candidate, name: str = "") -> dict[str, str]:
        """Headers for a remote candidate, with every secret it needs carried.

        A remote MCP server takes its credential in a header; ``env`` does
        nothing for one, because Claude Code launches no process for it. So any
        variable in ``secret_env`` that no header already references is given
        one here, in the scheme the server itself asked for.
        """
        headers = dict(candidate.headers)
        auth = self.settings.section("mcp.discovery.auth")
        header_name = auth.str_("header_name", "Authorization")
        for variable in sorted(candidate.secret_env):
            placeholder = ENV_PLACEHOLDER.format(name=variable)
            if any(placeholder in str(value) for value in headers.values()):
                continue
            if any(k.lower() == header_name.lower() for k in headers):
                # The credential header is already spoken for by something
                # better informed; naming the variable in the briefing is all
                # that is left to do about this one.
                continue
            scheme = candidate.handshake.auth_scheme if candidate.handshake else ""
            template = auth.str_("header_template", "Bearer {token}")
            if scheme and not template.lower().startswith(scheme.lower()):
                allowed = [s.lower() for s in auth.str_list("header_schemes", ["Bearer", "Basic", "Token", "ApiKey"])]
                if scheme.lower() in allowed:
                    # The server said which scheme it wants; honour that over
                    # the configured default rather than sending a bearer token
                    # to a server that asked for something else.
                    template = f"{scheme} {{token}}"
                else:
                    # ...but not every scheme in a challenge is one a token can
                    # be presented under. Jira answers `OAuth realm="..."`,
                    # meaning OAuth 1.0a request signing -- a personal access
                    # token is still a Bearer token, and `Authorization: OAuth
                    # <PAT>` is simply a second 401 to diagnose.
                    candidate.add_note(
                        f"the server asked for the {scheme} scheme, which a single credential "
                        f"cannot satisfy; the entry uses {template.split()[0]} instead"
                    )
            headers[header_name] = template.replace("{token}", placeholder)
        # A variable another service's entry already reads is renamed for this
        # one -- in every header that names it -- so one value is never asked to
        # be two instances' credential. See _shared_name.
        for variable in sorted(candidate.secret_env):
            exported = self._shared_name(variable, name, candidate)
            candidate.exported_names[variable] = exported
            if exported == variable:
                continue
            old, new = ENV_PLACEHOLDER.format(name=variable), ENV_PLACEHOLDER.format(name=exported)
            headers = {k: str(v).replace(old, new) for k, v in headers.items()}
        return headers

    # -- entries ---------------------------------------------------------------

    def portability_of(self, candidate: Candidate) -> Portability:
        """Grade an entry on whether it survives this machine. See :mod:`.portability`."""
        return assess(
            candidate,
            sandbox_dirs=[
                str(self.settings.path("mcp.packages.sandbox_dir") or ""),
                str(self.ctx.work_dir / "mcp-trials"),
            ],
            durable_dirs=self.durable_venv_roots(),
            must_exist=self.settings.bool_("mcp.registration.require_paths_exist", True),
            credential_files=self.credential_files_of(candidate),
        )

    def credential_files_of(self, candidate: Candidate) -> list[str]:
        """Paths in ``candidate``'s credential slots, which may be absent here (``credential_files_may_be_absent``)."""
        from .portability import credential_files

        if not self.registration.bool_("credential_files_may_be_absent", True):
            return []
        patterns = self.registration.str_list("credential_file_slots", list(CREDENTIAL_FILE_SLOTS))
        return credential_files(
            candidate,
            lambda name: bool(
                name in candidate.secret_env
                or is_secret_name(name)
                or any(re.search(p, name, re.IGNORECASE) for p in patterns if p)
            ),
        )

    def durable_venv_roots(self) -> list[str]:
        """Where a virtualenv is kept on purpose rather than built to trial something.

        The configured roots, phase 1's own venv even when ``install.venv_path``
        is unset (the same fallback phase 1 uses), and -- unless switched off --
        the venv this program is running from, whose interpreter is the one a
        generated bridge is proved with.
        """
        roots = self.settings.str_list("mcp.registration.durable_venv_roots", [])
        roots.append(str(self.settings.path("install.venv_path") or self.ctx.state_dir / "venv"))
        if self.settings.bool_("mcp.registration.trust_running_venv", True) and (sys.prefix != sys.base_prefix):
            roots.append(sys.prefix)
        return [r for r in roots if r.strip()]

    def grounding_of(self, candidate: Candidate, target: str, served_by: Sequence[str] = ()) -> Grounding:
        """Does this entry name the service it serves? See :mod:`.grounding`.

        ``served_by`` is what this run found answering for the target -- pass
        :func:`~.grounding.endpoint_hosts` of the outcome. Without it a bridge
        that relays to the vendor's endpoint host looks like a launcher pointed
        somewhere unrelated.
        """
        if not self.settings.bool_("mcp.registration.require_address_in_entry", True):
            return Grounding(GROUNDED, "not required by configuration")
        return ground(
            candidate,
            target,
            allow_front_door=self.settings.bool_("mcp.registration.allow_bare_origin", False),
            served_by=served_by,
            hosting_namespaces=hosting_namespaces_of(self.settings),
            identity=identity_rule_of(self.settings),
        )

    def registered_url(self, candidate: Candidate) -> str:
        """The URL to write out: the one that was probed, exactly as probed.

        This used to strip our own ``?format=mcp`` before writing, on the
        reasoning that the query is a string *this program invented* -- one of
        ``mcp.discovery.query_templates``, tried against every origin on the
        chance some product uses it -- so recording it as the server's address
        is circular.

        The reasoning was right and the remedy was worse than the disease.
        Removing the query does not undo the probe; it invents a *second* URL,
        one that was never requested, and writes that down instead.
        ``https://confluence.example.com?format=mcp`` answered ``401`` and became
        the entry ``https://confluence.example.com`` -- a URL nothing had ever
        tested, which turns out to be a login page, and which no credential can
        ever make work. Registering what you verified means the URL too.

        So the circularity is dealt with where it belongs: a bare origin is
        refused by :mod:`.grounding` unless it completed a real handshake, and
        the note below says what the query was, so nobody mistakes it for the
        vendor's.
        """
        url = candidate.url
        if not url or candidate.verified:
            return url
        split = urlsplit(url)
        templates = self.settings.str_list(
            "mcp.discovery.query_templates",
            [
                "?format=mcp",
                "?transport=mcp",
                "?protocol=mcp",
            ],
        )
        ours = {t.lstrip("?").split("=")[0].strip() for t in templates if "=" in t}
        present = {pair.split("=")[0].strip() for pair in split.query.split("&") if pair}
        if split.query and present <= ours and split.path in ("", "/"):
            candidate.add_note(
                f"the only thing marking this URL as MCP was '?{split.query}', a query string "
                "this program added while probing -- the host never acknowledged it"
            )
        return url

    def reshape_for_claude(self, entry: Mapping[str, Any]) -> tuple[dict[str, Any], str] | None:
        """``(entry, what changed)`` when Claude Code would skip ``entry`` for its type alone; None otherwise.

        The type follows the entry's own fields (``url`` -> http/sse,
        ``command`` -> stdio, :func:`.types.normalize_transport`), and a key
        that type does not take (``env`` beside a url) is dropped. Only when the
        result passes the shape rules is it offered.
        """
        if not self.registration.bool_("reshape_entries", True):
            return None
        if not self._shape_problem(entry):
            return None  # Claude Code takes it as it is
        shapes = self.registration.dict_("entry_shapes", dict(ENTRY_SHAPES))
        kind = (
            "url"
            if str(entry.get("url") or "").strip() and not str(entry.get("command") or "").strip()
            else ("stdio" if str(entry.get("command") or "").strip() else "")
        )
        if not kind:
            return None
        before = str(entry.get("type") or "stdio")
        wanted = ENTRY_TYPES[
            normalize_transport(before, kind, self.registration.dict_("transport_aliases", dict(TRANSPORT_ALIASES)))
        ]
        shape = shapes.get(wanted) or {}
        allowed = list(shape.get("allowed") or entry)
        fixed = {k: v for k, v in entry.items() if k in allowed}
        fixed["type"] = wanted
        dropped = sorted(k for k in entry if k not in allowed)
        if fixed == dict(entry) or self.claude_rejects("reshape", fixed):
            return None
        said = f'type "{before}" -> "{wanted}"' if before != wanted else ""
        if dropped:
            said = ", ".join(p for p in (said, f"dropped {', '.join(dropped)}") if p)
        return fixed, f"reshaped for Claude Code ({said})"

    def claude_rejects(self, name: str, entry: Mapping[str, Any]) -> str:
        """Why Claude Code would refuse ``entry`` -- "" when it takes it. Asked of Claude Code itself.

        Its own shape rules first (``mcp.registration.entry_shapes``: what each
        type must and may carry), then Claude Code's own validation: the
        entry is added to a throw-away configuration directory with
        ``add_command`` (``validate_with_claude``), so nothing real changes
        and the judge is the program that will load the file. openqa.example.net's
        ``"type": "stdio"`` beside a ``url`` was written, Claude Code
        answered "Invalid configuration", and the run reported it registered.
        """
        if not self.registration.bool_("validate_entries", True):
            return ""
        problem = self._shape_problem(entry)
        return problem or self._claude_validates(name, entry)

    def _shape_problem(self, entry: Mapping[str, Any]) -> str:
        """What in ``entry`` breaks Claude Code's shape rules (``entry_shapes``); "" for nothing."""
        shapes = self.registration.dict_("entry_shapes", dict(ENTRY_SHAPES))
        kind = str(entry.get("type") or "stdio")
        shape = shapes.get(kind)
        if not isinstance(shape, Mapping):
            return f"its type {kind!r} is not one Claude Code registers ({', '.join(sorted(shapes))})"
        missing = [k for k in shape.get("required") or [] if not str(entry.get(k) or "").strip()]
        if missing:
            return f"a {kind} entry needs {', '.join(missing)}, and this one has none"
        foreign = [k for k in entry if k not in (shape.get("allowed") or entry)]
        if foreign:
            return f"a {kind} entry does not take {', '.join(sorted(foreign))}"
        return ""

    def _claude_validates(self, name: str, entry: Mapping[str, Any]) -> str:
        """Claude Code's own verdict on ``entry``, from a throw-away configuration; "" when it accepts it or cannot be asked."""
        if self.ctx.dry_run or not self.registration.bool_("validate_with_claude", True):
            return ""
        claude = self.ctx.tool("claude", config_key="paths.claude")
        if not claude:
            return ""
        payload = json.dumps(entry, separators=(",", ":"), sort_keys=True)
        cache = self.ctx.state.setdefault("mcp_claude_validated", {})
        if payload in cache:
            return cache[payload]

        argv = [
            self._expand(token, claude=claude, name=name, json_payload=payload)
            for token in self.registration.list_("add_command", ["{claude}", "mcp", "add-json", "{name}", "{json}"])
        ]
        argv += [self._expand(t, scope="user") for t in self.registration.list_("scope_flag", ["--scope", "{scope}"])]
        variable = self.registration.str_("config_dir_variable", "CLAUDE_CONFIG_DIR")
        with diskspace.temporary_directory("ica-validate-") as scratch:
            result = self.ctx.runner.run(
                argv,
                timeout=self.registration.float_("timeout_seconds", 90),
                label=f"validate {name}",
                env={variable: scratch},
                mutating=False,
            )
        if getattr(result, "simulated", False):
            return ""
        said = " ".join(((result.stderr or "") + " " + (result.stdout or "")).split())
        verdict = "" if result.ok else f"Claude Code refuses it ({said[-200:] or 'no reason given'})"
        cache[payload] = verdict
        return verdict

    def entry_transport(self, candidate: Candidate) -> str:
        """The transport the entry is written with: the one its handshake was proved over, else its own, normalised.

        Never a word taken on trust: the agent's ``transport`` for
        openqa.example.net's endpoint fell through a table lookup to "stdio", and
        the entry -- a ``url`` with ``"type": "stdio"`` -- was dropped by Claude
        Code. Always one the entry's kind can carry (:func:`.types.normalize_transport`).
        """
        kind = "stdio" if candidate.kind == "stdio" else "url"
        aliases = self.registration.dict_("transport_aliases", dict(TRANSPORT_ALIASES))
        shake = candidate.handshake
        if shake is not None and (shake.ok or shake.requires_auth) and shake.transport:
            proved = normalize_transport(str(shake.transport), kind, aliases)
            if str(shake.transport) in KIND_TRANSPORTS[kind]:
                return proved
        return normalize_transport(str(candidate.transport or ""), kind, aliases)

    def _keep_alternatives(self, outcome: TargetOutcome, written: list[Registration]) -> None:
        """Record the other servers this address waits with, for the next run to prove (:mod:`.alternatives`).

        Only while what was written still waits itself: a working entry leaves
        nothing to wait for, and clears the record.
        """
        from . import alternatives

        if not written or self.ctx.dry_run:
            return
        if any(r.candidate is not None and r.candidate.verified for r in written):
            alternatives.record(self.ctx, outcome.target, [])
            return
        chosen = {id(r.candidate) for r in written if r.candidate is not None}
        launches = {json.dumps(r.entry, sort_keys=True) for r in written}
        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        for stage in outcome.stages:
            for candidate in stage.candidates:
                if id(candidate) in chosen or candidate.start_failure or not candidate.needs_credential:
                    continue
                name = candidate.name or candidate.label
                self.own_files(candidate, name)
                entry = self.entry_for(candidate, name)
                key = json.dumps(entry, sort_keys=True)
                if key in launches or key in seen:
                    continue
                seen.add(key)
                rows.append(
                    {
                        "name": name,
                        "entry": entry,
                        "why": candidate.evidence_why or (candidate.handshake.error if candidate.handshake else ""),
                    }
                )
        alternatives.record(self.ctx, outcome.target, rows)

    def own_files(self, candidate: Candidate, name: str) -> list[str]:
        """Move the files a server launches or reads out of this run's scratch area, into its own folder.

        Bridges, manifests and CA bundles were registered from
        ``work/agent-workspace/artifacts`` and ``work/bridges``: they lived only
        as long as cleanup made an exception for them, and smelt.example.net's
        bundle path pointed nowhere once the project moved. Each file under the
        work directory that ``candidate`` names is copied to
        ``mcp.registration.artifacts_home/<name>/`` -- with the folder it sits
        in when that folder is the server's own (its name carries the server's
        name) -- and the candidate is pointed there before its entry is made.
        Returns what moved. Never in a dry run. (``mcp.registration.own_files``.)
        """
        if self.ctx.dry_run or not self.registration.bool_("own_files", True):
            return []
        from ..core.paths_home import artifacts_home

        work = Path(str(self.ctx.work_dir)).resolve()
        home = artifacts_home(self.settings)
        if home is None:
            return []
        if home == work or work in home.parents:
            return []  # a home inside the scratch area is no home
        slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
        moved: dict[str, str] = {}
        # Files are moved from the scratch area -- and from a home an earlier
        # version kept them in (the state folder), which a copied project leaves
        # behind. (mcp.registration.legacy_artifacts_homes)
        roots = [
            work,
            *[
                r
                for r in (
                    Path(os.path.expanduser(x)).resolve()
                    for x in self.registration.str_list("legacy_artifacts_homes", [])
                    if str(x).strip()
                )
                if r != home and home not in r.parents and r not in home.parents
            ],
        ]

        def root_of(path: Path) -> Path | None:
            return next((r for r in roots if path == r or r in path.parents), None)

        def unit(path: Path) -> Path:
            best = path
            root = root_of(path)
            for parent in path.parents:
                if root is None or parent == root or root not in parent.parents:
                    break
                if slug and slug in parent.name.lower():
                    best = parent
            return best

        from ..core.paths_home import collapsed, server_destination

        def destination(source: Path) -> Path:
            root = root_of(source)
            if root is not None and root != work:
                # From an earlier home: its layout is kept, never deepened.
                return home.joinpath(*collapsed(source.relative_to(root).parts))
            # From the scratch area: the one rule every mover shares.
            return server_destination(home, slug, source)

        def place(text: str) -> str:
            if not text.startswith("/"):
                return text
            for old, new in moved.items():
                if text == old or text.startswith(old + "/"):
                    return new + text[len(old) :]
            try:
                real = Path(text).resolve()
            except (OSError, ValueError):
                return text
            if real.exists() and (real == home or home in real.parents):
                # Already home: never moved again -- only a path an earlier
                # version nested in itself is put back where it belongs.
                fixed = home.joinpath(*collapsed(real.relative_to(home).parts))
                if fixed == real:
                    return text
                try:
                    fixed.parent.mkdir(parents=True, exist_ok=True)
                    if real.is_dir():
                        shutil.copytree(real, fixed, dirs_exist_ok=True)
                    else:
                        shutil.copy2(real, fixed)
                except OSError as exc:
                    log.warning("%s: could not un-nest %s (%s)", name, real, exc)
                    return text
                moved[str(real)] = str(fixed)
                return str(fixed)
            if not real.exists() or root_of(real) is None or real in roots:
                return text
            source = unit(real)
            target = destination(source)
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                if source.is_dir():
                    shutil.copytree(source, target, dirs_exist_ok=True)
                else:
                    shutil.copy2(source, target)
            except OSError as exc:
                log.warning("%s: could not move %s out of the scratch area (%s)", name, source, exc)
                return text
            moved[str(source)] = str(target)
            return str(target) + str(real)[len(str(source)) :]

        candidate.command = [place(str(t)) for t in candidate.command]
        candidate.args = [place(str(t)) for t in candidate.args]
        candidate.env = {k: place(str(v)) if isinstance(v, str) else v for k, v in candidate.env.items()}
        if moved:
            # A manifest a moved bridge reads may name another moved file.
            for new in moved.values():
                for file in [Path(new)] if Path(new).is_file() else Path(new).rglob("*.json"):
                    if file.suffix != ".json" or not file.is_file():
                        continue
                    try:
                        text = file.read_text(encoding="utf-8")
                    except (OSError, UnicodeDecodeError):
                        continue
                    changed = text
                    for old, fresh in moved.items():
                        changed = changed.replace(old, fresh)
                    if changed != text:
                        file.write_text(changed, encoding="utf-8")
            candidate.add_note(f"its files were moved out of the run's scratch area into {home / slug}")
            log.info(
                "%s: files moved out of the scratch area: %s",
                name,
                ", ".join(f"{Path(o).name} -> {n}" for o, n in moved.items()),
            )
        return list(moved.values())

    def entry_for(self, candidate: Candidate, name: str = "") -> dict[str, Any]:
        """The MCP server entry, with credentials left as references."""
        entry: dict[str, Any] = {"type": ENTRY_TYPES[self.entry_transport(candidate)]}

        # Paths this run owns are written as ${VARIABLE:-<absolute>}, so the
        # entry means the same thing here and can be relocated elsewhere by
        # exporting one variable. See `portable`.
        roots = self.path_roots()
        env = {
            # The env key is passed so a stray path can be named after the
            # variable that holds it: MCP_BRIDGE_CA_BUNDLE is a better name for
            # a PEM than anything derived from the filename. See `portable`.
            name: self.portable(value, roots, key=name)
            for name, value in candidate.env.items()
            if value != ""
        }
        # What an existing entry wrote as a reference is written as that
        # reference, never as the value it was expanded to for the trial.
        for key, reference in candidate.references.items():
            # Written whenever the entry read it -- including one whose trial
            # value was empty, which the filter above leaves out.
            if (key in env or key in candidate.env) and not self.env_contract.session(key):
                env[key] = reference
        # A variable the login session sets is passed under its own name and
        # nothing else -- also when an earlier entry renamed it, pinned this
        # host's value, or listed it as a credential (see SESSION_VARIABLES).
        for key in [*candidate.env, *candidate.secret_env]:
            if self.env_contract.session(key):
                env[key] = OPTIONAL_PLACEHOLDER.format(name=key)
        if candidate.kind == "stdio":
            command = [self.portable(token, roots) for token in candidate.full_command()]
            entry["command"] = command[0] if command else ""
            if len(command) > 1:
                entry["args"] = command[1:]
            # Write the launch that was verified, not a variation on it. A
            # variable the proven launch did not carry is left out -- see
            # Candidate.proven_without for the two entries this broke.
            deferred = candidate.proven_without
            for key in candidate.secret_env:
                if self.env_contract.session(key):
                    continue  # written above, under its own name
                if candidate.composed.get(key):
                    # What the operator already exports, in the form the slot
                    # takes -- proved by the login check (CandidateVerifier._borrow_held).
                    env[key] = candidate.composed[key]
                    candidate.exported_names[key] = (
                        ", ".join(re.findall(r"\$\{([A-Za-z_][A-Za-z0-9_]*)", env[key])) or key
                    )
                    continue
                exported = (
                    candidate.reuse_names.get(key)
                    or self._trial_source(candidate, key)
                    or self._exported_name(key, name, candidate)
                )
                candidate.exported_names[key] = exported
                if key in deferred:
                    # Proven to start without it, so the slot is written in
                    # the one form that expands to nothing when unset. Leaving
                    # it out -- the earlier rule -- kept the launch identical
                    # but hid the variable from everybody who copies the file,
                    # which is the failure `declare_reads` exists to prevent.
                    env[key] = OPTIONAL_PLACEHOLDER.format(name=exported)
                    candidate.add_note(
                        f"{key} is written as {env[key]}: this server was proven to start "
                        "without it, and that form expands to an empty string while it is "
                        "unset, so the launch is the one that was tested. Export "
                        f"{exported} to supply it."
                    )
                    continue
                # The reference scheme is initialize-claude-ai's business; what
                # Claude Code needs is the variable, and only as a placeholder.
                # The *key* is whatever the server reads and must not change;
                # only the variable it points at is ours to name.
                env[key] = ENV_PLACEHOLDER.format(name=exported)
            for key in candidate.completed_slots:
                if key in candidate.secret_env and not candidate.composed.get(key):
                    env[key] = OPTIONAL_PLACEHOLDER.format(name=candidate.exported_names.get(key, key))
            # A slot that carries a credential in a form (a JSON header object)
            # is written in that form around the credential itself, never as
            # one variable the operator would have to fill with the whole form.
            from . import api_kinds

            api_kinds.credential_in_taken_form(self.ctx, candidate, env, name, CandidateVerifier._host_of(candidate))
            self.carry_supplied_credentials(candidate, name, env, CandidateVerifier._host_of(candidate))
            self.machine_settings(candidate, name, env)
            self.declare_reads(candidate, name, env, roots)
        else:
            entry["url"] = self.registered_url(candidate)
            headers = self._credential_headers(candidate, name)
            if headers:
                entry["headers"] = headers
        env = self._without_tls_weakening(env, candidate)
        env = self._without_secret_defaults(env, candidate)
        env = self._without_literal_credentials(env, candidate, name)
        env = self._without_unset_settings(env, candidate)
        if candidate.kind == "stdio":
            # Tell the runtime what this host trusts. A target behind an
            # internal CA is ordinary, and a runtime with its own certificate
            # store -- Node's, certifi's -- cannot reach it: the server exits at
            # start-up and the operator is shown `Connection closed`, which
            # mentions no certificate. See :mod:`.trust`.
            #
            # An entry that reaches a host this host's store could not verify
            # names the run's own bundle instead, which holds that store too.
            run_bundle = self.run_bundle_for(candidate)
            added = trust.env_for(str(entry.get("command", "")), self.settings, env, bundle=run_bundle)
            env.update(added)
            if added:
                candidate.add_note(
                    f"{', '.join(sorted(added))} points the runtime at "
                    + (
                        "this run's trust store (the host's, plus the authorities its target needed)"
                        if run_bundle
                        else "this host's trust store"
                    )
                    + ", so a target behind an internal certificate authority is reachable. "
                    "It adds authorities and never removes any; unset, the entry behaves as "
                    "though it were absent."
                )
            # ...and every trust-store file the entry names -- ours, or one it
            # brought with it -- must carry its hosts' chains to a self-signed
            # root, the way the strictest runtime reads it. One that does not is
            # replaced by a bundle that does. See trust.check_named_bundles.
            check = self.bundle_check(candidate, env)
            for variable, bundle in check.replaced.items():
                env[variable] = f"${{{variable}:-{bundle}}}"
            candidate.add_note(*check.notes, *check.expiring)
        if candidate.kind == "stdio":
            entry, env = self._compose_launch(entry, env, candidate)
        elif env:
            # Nothing is launched for a URL entry, so nothing reads an env
            # block: openqa.example.net's NODE_EXTRA_CA_CERTS sat in one and Claude
            # Code's own client never saw it. A trust store for such a host is
            # Claude Code's to hold, in every place it reads (see consumer_trust).
            candidate.add_note(
                f"{', '.join(sorted(env))} left out of the entry: a {entry['type']} entry launches nothing, so "
                "nothing would read it"
            )
            env = {}
        # Every ${NAME} the entry reads is a name only this program, or the
        # user for this entry, sets -- applied here, where every path's entry
        # passes (new, existing, carried, repaired). See own_references.
        env = self.own_references(entry, env, name, candidate)
        if candidate.kind == "stdio":
            # Whatever path produced the command, an installer's private
            # launcher is never what the file says (mcp.launch_form).
            moved = self.launcher_repair(entry)
            if moved:
                candidate.add_note(moved)
        if env:
            entry["env"] = dict(sorted(env.items()))
        if candidate.kind == "stdio":
            # What this package reads a login from, for the same package's entry
            # for another address (verifier.sibling_credential).
            from .verifier import record_package_credentials

            record_package_credentials(self.ctx, entry)
        return entry

    #: ``${NAME}`` / ``${NAME:-default}`` as Claude Code expands it.
    _REFERENCE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(:-([^}]*))?\}")

    def owned_reference(self, variable: str, server: str, default: str = "", host: str = "") -> str:
        """The name an entry reads ``variable`` through: one nobody but this program, or the user for this entry, sets.

        bugzilla-example-com read ``${BUGZILLA_API_KEY:-}``: the operator's shell
        exported a revoked ``BUGZILLA_API_KEY`` for another purpose, and it won
        over the new key written as the default. Its ``SSL_CERT_FILE`` read
        ``${SSL_CERT_FILE:-/etc/ssl/ca-bundle.pem}``, and a login shell exported
        ``SSL_CERT_FILE`` as a deleted /tmp bundle another installer wrote --
        every tool failed. A generic name is set by anybody; what the file says
        is then not what runs. So:

        * a session variable (``SSH_AUTH_SOCK``) keeps its name -- the session sets it;
        * a name of this program's (``path_variable_prefix``, ``owned_prefixes``)
          or already naming this entry (``{SLUG}_…``) is kept;
        * a trust bundle that defaults to this machine's own bundle reads
          ``trust_reference`` (``ICA_MCP_CA_BUNDLE``), set per machine;
        * anything else reads ``owned_reference_template`` (``{SLUG}_{NAME}``).

        The key -- what the server reads -- never changes; only the variable
        the value comes from. (``mcp.registration.own_reference_names``.)
        """
        variable = str(variable or "").strip()
        if not variable or not self.registration.bool_("own_reference_names", True):
            return variable
        if self.env_contract.session(variable) or variable in self._own_names():
            return variable
        prefix = self.settings.str_("mcp.registration.path_variable_prefix", "ICA_MCP")
        owned = [p for p in [f"{prefix}_" if prefix else "", *self.registration.str_list("owned_prefixes", [])] if p]
        if any(variable.startswith(p) for p in owned):
            return variable
        trust_name = self.registration.str_("trust_reference", "ICA_MCP_CA_BUNDLE")
        if trust_name and (
            (default and self._is_system_bundle(default))
            or (not default and variable in trust.trust_variables(self.settings))
        ):
            # This machine's own bundle -- or none found, the lever still
            # written: one per-machine name, set by phase 1 wherever it lives.
            return trust_name
        slug = re.sub(r"[^A-Za-z0-9]+", "_", str(server or "")).strip("_").upper()
        if not slug or variable.upper() == slug or variable.upper().startswith(slug + "_"):
            return variable
        # A name that already names this instance -- its domain's own label as
        # a whole word: BUILD_EXAMPLE_… for build.example.de, GITHUB_… for
        # github.com -- is its own. A product's name is not: BUGZILLA_API_KEY
        # is every Bugzilla's, bugzilla.example.com's and bugzilla.opensuse.org's.
        label = self._instance_label(host or server.replace("-", "."))
        if label and label in variable.upper().split("_"):
            return variable
        template = self.registration.str_("owned_reference_template", "{SLUG}_{NAME}")
        return template.replace("{SLUG}", slug).replace("{NAME}", variable.upper())

    @staticmethod
    def _instance_label(host: str) -> str:
        """The label that names one instance's owner: ``Example`` for bugzilla.example.com, ``OPENSUSE`` for bugzilla.opensuse.org."""
        labels = [x for x in str(host or "").lower().strip(".").split(".") if x]
        if len(labels) < 2:
            return ""
        return re.sub(r"[^A-Za-z0-9]+", "_", labels[-2]).upper()

    def _own_names(self) -> set[str]:
        """Names this program or the operator chose on purpose: configured path roots, configured credential references."""
        names = {variable for variable, _root in self.path_roots()}
        names.update(recorded_path_variables(self.ctx))
        for key in self.registration.str_list("operator_reference_settings", ["mcp.discovery.auth.bearer_token_ref"]):
            scheme, _, name = self.settings.str_(key, "").partition(":")
            if scheme.strip().lower() == "env" and name.strip():
                names.add(name.strip())
        return names

    def _is_system_bundle(self, value: str) -> bool:
        """Is ``value`` this machine's own CA bundle (the file :func:`.trust.system_bundle` names)?"""
        from ..core.trust_store import system_bundle

        bundle = system_bundle(self.settings)
        if not bundle or not str(value).startswith("/"):
            return False
        try:
            return os.path.realpath(str(value)) == os.path.realpath(bundle)
        except OSError:
            return False

    def own_references(
        self, entry: dict[str, Any], env: dict[str, Any], server: str, candidate: Candidate | None = None
    ) -> dict[str, Any]:
        """``env`` -- and ``entry``'s ``args`` and ``headers`` -- with every reference through :meth:`owned_reference`.

        What Claude Code's settings hold under the old name for a credential is
        kept under the new one (:attr:`.Candidate.kept_out`, written where
        Claude Code reads it), so a login the operator gave is never lost by
        the rename; a value only a shell exports is not carried -- that is the
        export the rename is protecting the entry from. Returns the new env.
        """
        if not self.registration.bool_("own_reference_names", True):
            return env
        renamed: dict[str, str] = {}
        # Names the operator gave this entry, proved by a login check: what
        # they already export for it (held, composed), the variable an existing
        # entry for the same host reads for it, and what the trial read. Those
        # are the user's for this entry, never renamed out from under them.
        theirs: set[str] = set()
        if candidate is not None:
            for text in [
                *candidate.composed.values(),
                *candidate.held_names.values(),
                *candidate.reuse_names.values(),
                *(self._trial_source(candidate, k) for k in candidate.secret_env),
            ]:
                # A ${NAME}, a bare name, or a name inside a composed form
                # ({b64:USER:PASSWORD}): every variable it is built from.
                theirs.update(
                    re.findall(r"(?<![A-Za-z0-9_])([A-Z][A-Z0-9_]*[A-Z0-9])(?![A-Za-z0-9_])", str(text or ""))
                )
        host = CandidateVerifier._host_of(candidate) if candidate is not None else ""

        def rewrite(text: Any, key: str = "") -> Any:
            if not isinstance(text, str) or "${" not in text:
                return text

            def one(match: re.Match[str]) -> str:
                old, default = match.group(1), match.group(3)
                new = old if old in theirs else self.owned_reference(old, server, default or "", host)
                if new == old:
                    return match.group(0)
                renamed[old] = new
                if key:
                    renamed_keys.setdefault(old, set()).add(key)
                return "${" + new + (match.group(2) or "") + "}"

            return self._REFERENCE.sub(one, text)

        renamed_keys: dict[str, set[str]] = {}
        out = {k: rewrite(v, str(k)) for k, v in env.items()}
        if isinstance(entry.get("args"), list):
            entry["args"] = [rewrite(a) for a in entry["args"]]
        if isinstance(entry.get("headers"), dict):
            entry["headers"] = {k: rewrite(v, str(k)) for k, v in entry["headers"].items()}
        if not renamed or candidate is None:
            return out
        current = self._claude_settings_env() if self.registration.bool_("carry_settings_value", True) else {}
        for old, new in renamed.items():
            for slot, exported in list(candidate.exported_names.items()):
                if exported == old:
                    candidate.exported_names[slot] = new
            for store in (candidate.kept_out, candidate.machine_values):
                if old in store and new not in store:
                    store[new] = store.pop(old)
            keys = renamed_keys.get(old, set())
            credential = is_secret_name(old) or any(is_secret_name(k) or k in candidate.secret_env for k in keys)
            value = current.get(old, "")
            if credential and value and not self._is_set(new) and new not in candidate.kept_out:
                candidate.kept_out[new] = Secret(value, source=f"{old} in Claude Code's settings")
                redact.register(value)
            candidate.add_note(
                f"{', '.join(sorted(keys)) or old} reads ${{{new}}}, not ${{{old}}}: {old} is a name other software "
                f"and shells set too, and an export of it would win over what this entry was given"
                + (
                    f"; the value Claude Code's settings hold under {old} is kept under {new}"
                    if credential and value
                    else ""
                )
            )
        log.info(
            "%s: references renamed to names only this entry reads: %s",
            server or candidate.name,
            ", ".join(f"{o} -> {n}" for o, n in sorted(renamed.items())),
        )
        return out

    def _compose_launch(
        self, entry: dict[str, Any], env: dict[str, Any], candidate: Candidate
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Build a slot from the parts the operator exports, at start-up, when nothing holds it ready-made.

        ``EXTRA_HEADERS`` takes ``Authorization:Basic <base64 of user:password>``;
        Claude Code expands ``${NAME}`` but cannot encode. When the operator
        exports the user name and password (proved by the login check), the
        entry starts the program's small composer (``mcp.registration.compose``,
        written beside the generated bridges) which sets the slot from them and
        runs the server -- nobody types base64 into a shell.
        """
        wanted = {k: v[len("COMPOSE:") :] for k, v in candidate.composed.items() if str(v).startswith("COMPOSE:")}
        if not wanted or not self.registration.section("compose").bool_("enabled", True):
            return entry, env
        script = self.composer_script()
        if script is None:
            return entry, env
        roots = self.path_roots()
        interpreter = self.portable(self.settings.str_("mcp.bridge.generated.interpreter", "") or sys.executable, roots)
        argv = [str(entry.get("command") or ""), *map(str, entry.get("args") or [])]
        sets: list[str] = []
        for key, template in sorted(wanted.items()):
            env.pop(key, None)
            sets += ["--set", f"{key}={template}"]
            for variable in re.findall(
                r"\{(?:b64:)?([A-Za-z_][A-Za-z0-9_]*)(?::([A-Za-z_][A-Za-z0-9_]*))?\}", template
            ):
                for name in variable:
                    if name:
                        env[name] = ENV_PLACEHOLDER.format(name=name)
        entry["command"] = interpreter
        entry["args"] = [self.portable(str(script), roots), *sets, "--", *argv]
        candidate.add_note(
            f"{', '.join(sorted(wanted))} is built at start-up from the user name and password you already export "
            f"({', '.join(sorted({n for t in wanted.values() for n in re.findall(r'[A-Z][A-Z0-9_]{2,}', t)}))})"
        )
        return entry, env

    def composer_script(self) -> Path | None:
        """The composer (``mcp.registration.compose.filename``) beside the generated bridges; written once. None in a dry run."""
        out_dir = Path(self.settings.str_("mcp.bridge.output_dir", "") or (self.ctx.work_dir / "bridges"))
        path = out_dir / self.registration.section("compose").str_("filename", "mcp_compose.py")
        if self.ctx.dry_run:
            return path
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
            if not path.exists() or path.read_text(encoding="utf-8") != COMPOSER_SOURCE:
                write_generated(path, COMPOSER_SOURCE, executable=True)
        except OSError as exc:
            log.warning("could not write the composer %s: %s", path, exc)
            return None
        return path

    def bundle_check(self, candidate: Candidate, env: Mapping[str, Any]) -> trust.BundleCheck:
        """The trust-store files ``env`` names, checked against the hosts the entry reaches."""
        hosts = trust.https_hosts([candidate.url, *candidate.full_command(), *env.values()])
        return trust.check_named_bundles(env, hosts, self.settings, TrustStore.for_run(self.ctx))

    def trust_refusal(self, candidate: Candidate, entry: Mapping[str, Any] | None = None) -> str:
        """Why the entry cannot reach its host over TLS at all; "" when it can, or nobody knows.

        Only for a variable whose runtime needs a self-signed root
        (``mcp.trust.root_required_variables``) when no bundle on this machine
        carries one -- a server that starts, lists its tools and then fails
        every request is not a configuration. ``mcp.trust.refuse_incomplete_chain
        = false`` writes it anyway, with the reason in its notes.
        """
        if candidate.kind != "stdio":
            return ""
        entry = entry if entry is not None else self.entry_for(candidate)
        check = self.bundle_check(candidate, entry.get("env") or {})
        if not check.unfixable:
            return ""
        if not self.settings.bool_("mcp.trust.refuse_incomplete_chain", True):
            candidate.add_note(*check.unfixable)
            return ""
        return "; ".join(check.unfixable)

    def run_bundle_for(self, candidate: Candidate) -> str:
        """The run's CA bundle if ``candidate`` reaches a host that needed it; "" otherwise."""
        texts = [candidate.url, *candidate.full_command(), *candidate.env.values()]
        return TrustStore.for_run(self.ctx).bundle_for(texts)

    def machine_settings(self, candidate: Candidate, name: str, env: dict[str, str]) -> list[str]:
        """Write each runtime-tuning value as a reference to a per-machine variable, the value kept on this machine.

        smelt-example-net's mcp-graphql needed NODE_OPTIONS's long connection wait
        on the machine that ran the program -- every real call failed without
        it -- and the same literal, copied in .mcp.json, held its start past
        Claude Code's 30 s on the next machine. A literal cannot serve both: the
        entry reads ``${<variable>:-}`` (``machine_variable_template``), the
        value goes into this machine's Claude Code settings env (with the
        values kept out of the file), and another machine leaves it unset and
        gets the runtime's default. (``mcp.verification.runtime_tuning.per_machine``.)
        """
        from .verifier import RUNTIME_OPTION_VARIABLES

        config = self.settings.section("mcp.verification.runtime_tuning")
        if candidate.kind != "stdio" or not config.bool_("per_machine", True):
            return []
        names = [p for p in config.str_list("runtime_option_variables", list(RUNTIME_OPTION_VARIABLES)) if p]
        template = config.str_("machine_variable_template", MACHINE_VARIABLE_TEMPLATE)
        moved = []
        for key, value in list(env.items()):
            text = str(value)
            if not text.strip() or ENTRY_VARIABLE.search(text) or not any(re.fullmatch(p, str(key)) for p in names):
                continue
            variable = fill(
                template,
                {
                    "SLUG": re.sub(r"[^A-Z0-9]+", "_", (name or candidate.name).upper()).strip("_"),
                    "KEY": re.sub(r"[^A-Z0-9]+", "_", str(key).upper()).strip("_"),
                },
            )
            env[key] = OPTIONAL_PLACEHOLDER.format(name=variable)
            candidate.machine_values[variable] = text
            moved.append(key)
        if moved:
            candidate.add_note(
                f"{', '.join(moved)} kept for this machine only: the entry reads a per-machine variable "
                "whose value is in this machine's Claude Code settings; another machine leaves it unset"
            )
        return moved

    def carry_supplied_credentials(self, candidate: Candidate, name: str, env: dict[str, str], host: str) -> list[str]:
        """Keep, in ``env``, each credential slot an existing entry for ``host`` supplies that this server reads too.

        bugzilla-example-com's new entry worked as anybody -- public bugs only --
        and the claude CLI copy that carried the operator's API key was then
        replaced by it: a server that works without a login is not a reason to
        drop the login that reaches more. A key carried by any existing entry
        for the address, that what this entry launches reads (its source names
        it) or that the same launch carried, and that ``env`` leaves unset, is
        written optional (``${NAME:-}``): the variable it read, or -- a literal
        -- one named for this entry, its value kept where Claude Code reads it,
        never in the file. (``mcp.registration.carry_supplied_credentials``.)
        Returns the keys carried.
        """
        if candidate.kind != "stdio" or not host or not self.registration.bool_("carry_supplied_credentials", True):
            return []
        from .existing import ExistingRegistrations  # imports this module

        try:
            existing = ExistingRegistrations.for_run(self.ctx)
            theirs = [r for r in existing.registrations() if existing._about(r, host)]
        except Exception:  # noqa: BLE001 - a lookup must never cost the entry
            return []
        if not theirs:
            return []
        command = candidate.full_command()
        ours = {"command": command[0] if command else "", "args": command[1:]}
        carried = self._carry_from([r.entry for r in theirs], ours, env, name, candidate)
        if carried:
            candidate.add_note(
                f"{', '.join(carried)} kept from the existing entry for {host}: a login you supplied is never dropped, "
                "even where the server also works without one"
            )
        return carried

    def _carry_from(
        self,
        sources: Iterable[Mapping[str, Any]],
        ours: Mapping[str, Any],
        env: dict[str, str],
        name: str,
        candidate: Candidate | None = None,
    ) -> list[str]:
        """The credential keys of ``sources`` that ``ours`` reads and ``env`` lacks, added to ``env`` as optional slots."""
        argv = [str(ours.get("command") or ""), *[str(a) for a in ours.get("args") or []]]
        try:
            _reader, reads, files = self.env_contract.all_reads(argv)
        except Exception:  # noqa: BLE001 - an unreadable source changes nothing
            reads, files = set(), []
        mine = _launch_signature(ours)
        carried: list[str] = []
        for entry in sources:
            same = _launch_signature(entry) == mine
            for key, raw in dict(entry.get("env") or {}).items():
                key, text = str(key), str(raw or "")
                if not text or (env.get(key) or "").strip() or self.env_contract.session(key):
                    continue
                if not (is_secret_name(key) or holds_a_credential(key, text)) or names_a_selector(key):
                    continue
                if not (same or key in reads or EnvContract.mentions(key, files)):
                    continue
                match = ENTRY_VARIABLE.fullmatch(text.strip())
                if match:
                    variable = match.group(1)
                elif ENTRY_VARIABLE.search(text):
                    continue  # a form around a reference: carried by the form's own rules, not guessed here
                else:
                    variable = self._exported_name(key, name, candidate)
                    if candidate is not None:
                        candidate.kept_out.setdefault(variable, Secret(text, source="an existing entry"))
                    redact.register(text)
                env[key] = OPTIONAL_PLACEHOLDER.format(name=variable)
                carried.append(key)
        return sorted(set(carried))

    def declare_reads(
        self,
        candidate: Candidate,
        server: str,
        env: dict[str, str],
        roots: Sequence[tuple[str, str]],
    ) -> list[Read]:
        """Name in ``env`` every variable the launched script reads.

        A candidate declares what its author remembered; the script reads what
        it reads. openqa.example.net's bridge read six variables and declared one,
        so its manifest and CA bundle were chosen by a fallback nobody could
        see -- and on the next machine the fallback found a stale manifest.
        See :mod:`.env_contract`.

        Each read is written with the value the script would have used anyway,
        so the entry behaves identically here and says on every other machine
        what it needs. The key is what the script reads; the variable it
        expands is namespaced by :meth:`_exported_name`, because two bridges
        that both read ``MCP_BRIDGE_MANIFEST`` must not share one export.
        """
        if not self.env_contract.enabled:
            return []
        added: list[Read] = []
        for read in self.env_contract.reads_of(candidate.full_command()):
            if read.name in env:
                continue
            env[read.name] = self.declared_value(read, server, roots, candidate)
            added.append(read)
            if self.env_contract.session(read.name):
                continue  # the session's own variable: never a credential to ask for
            if is_secret_name(read.name) and read.name not in candidate.secret_env:
                # A credential nobody declared is a credential nobody will be
                # told to export: it is named as needed (see publish).
                exported = self._exported_name(read.name, server)
                if exported not in candidate.undeclared_credentials:
                    candidate.undeclared_credentials.append(exported)
        if added:
            candidate.add_note(
                "declared in env because the launched script reads them: "
                + ", ".join(f"{r.name} ({PurePosixPath(r.source).name}:{r.line})" for r in added)
                + ". Each carries the default the script already used, so nothing changes "
                "here; on another machine each one is a setting that can be found and exported."
            )
            log.info(
                "%s: the launched script reads %s, which the candidate did not declare",
                server or candidate.name,
                ", ".join(r.name for r in added),
            )
        return added

    def declared_value(
        self, read: Read, server: str, roots: Sequence[tuple[str, str]], candidate: Candidate | None = None
    ) -> str:
        """The ``env`` value for one undeclared read. Never a nested default.

        * a path is written through :meth:`portable`, so it relocates with a
          configured root or by exporting its own variable;
        * a literal default is kept as the default;
        * no default, or a secret, is ``${NAME:-}`` -- empty when unset. A bare
          ``${NAME}`` would reach the server as those literal characters.
        """
        if self.env_contract.session(read.name):
            # Whatever the session sets, under the name it sets it by.
            return OPTIONAL_PLACEHOLDER.format(name=read.name)
        exported = self._exported_name(read.name, server, candidate)
        default = read.default or ""
        # A default that is, or looks like, a credential is never copied out
        # of the source into a file that is meant to be committed.
        if (
            not default
            or is_secret_name(read.name)
            or holds_a_credential(read.name, default)
            or self.env_contract.embeds_a_credential(default)
        ):
            return OPTIONAL_PLACEHOLDER.format(name=exported)
        if read.is_path:
            return self.portable(default, roots, key=exported)
        if any(ch in default for ch in "}$"):
            # Cannot sit inside ${...:-...} without breaking the expansion.
            return default
        return f"${{{exported}:-{default}}}"

    def retrial_rewritten(self, outcome: TargetOutcome) -> list[str]:
        """Judge again an existing entry that failed, when this run rewrote a file it names; the names re-judged.

        sd-example-com's customer-API bridge was judged "does not start" -- its
        operations manifest was empty, left so by a full disk -- and later in the
        same run the bridge stage regenerated that very manifest. The entry
        would have worked; the verdict had been taken before the file changed,
        and the writer replaced it with mcp-atlassian. So before anything is
        written, every existing entry that failed and names a file this process
        has since written (:data:`.fileio.REWRITTEN`) is trialled again, and
        what it proves now is what counts: working, waiting for a credential,
        or still broken. (``mcp.registration.retrial_rewritten``.)
        """
        from ..core.fileio import REWRITTEN
        from .existing import ExistingRegistrations
        from .portability import expand
        from .types import StageOutcome

        if not REWRITTEN or not self.registration.bool_("retrial_rewritten", True):
            return []
        existing = ExistingRegistrations.for_run(self.ctx)
        seen: dict[int, Candidate] = {}
        for candidate in [*(c for st in outcome.stages for c in st.candidates), *outcome.unstartable]:
            seen.setdefault(id(candidate), candidate)
        again: list[str] = []
        limit = (
            0
            if self.settings.bool_("mcp.exhaustive", False)
            else self.settings.int_("mcp.discovery.auth.max_provisional", 1)
        )
        for candidate in seen.values():
            registration = existing.registration_for(candidate)
            if registration is None and not candidate.existing_name:
                continue
            shake = candidate.handshake
            failed = bool(candidate.start_failure) or (
                shake is not None and not shake.ok and not candidate.needs_credential
            )
            if not failed:
                continue
            named: set[str] = set()
            for token in [*candidate.full_command(), *candidate.env.values()]:
                text = expand(str(token))
                if "/" in text:
                    try:
                        named.add(str(Path(text).resolve()))
                    except (OSError, ValueError):
                        continue
            changed = named & REWRITTEN
            if not changed:
                continue
            candidate.handshake = None
            candidate.start_failure = ""
            self.verifier.verify_all([candidate], stop_at_first=True)
            name = registration.name if registration is not None else candidate.existing_name
            again.append(name)
            if candidate.verified or candidate.needs_credential:
                outcome.broken_existing = [b for b in outcome.broken_existing if b.get("name") != name]
                outcome.unstartable = [c for c in outcome.unstartable if c is not candidate]
                if candidate.verified and candidate not in outcome.verified:
                    outcome.verified.append(candidate)
                elif candidate.needs_credential:
                    from . import evidence

                    evidence.mark(
                        self.ctx,
                        [candidate],
                        (urlsplit(outcome.target).hostname or "").lower(),
                        outcome.auth_plan or {},
                    )
                    outcome.absorb(
                        StageOutcome(stage=candidate.origin or "package", candidates=[candidate]),
                        limit,
                        prefer_detected_api=self.settings.bool_("mcp.discovery.auth.prefer_detected_api", True),
                    )
            candidate.add_note(
                f"judged again after this run rewrote {', '.join(sorted(Path(c).name for c in changed))}, "
                "a file it names: "
                + (
                    "it works"
                    if candidate.verified
                    else "it waits for its credential"
                    if candidate.needs_credential
                    else "it still fails"
                )
            )
            log.info(
                "%s: %s judged again after %s was rewritten -- %s",
                outcome.target,
                name,
                ", ".join(sorted(changed)),
                "works"
                if candidate.verified
                else "waits for a credential"
                if candidate.needs_credential
                else "still fails",
            )
        return again

    def complete_slots(self, candidate: Candidate) -> list[str]:
        """Give an entry every credential slot what it launches reads; the names added.

        Checked on every entry before it is written -- new, kept or repaired --
        against the generated bridge's manifest (the variables its login reads,
        :func:`.traditional.manifest_missing`). An entry was written without
        ``BUILD_EXAMPLE_NET_USERNAME`` while its own report said it needed it, and
        nothing compared the two; an entry is never written short again,
        whatever path dropped the slot. (``mcp.registration.complete_slots``.)
        """
        from .traditional import manifest_missing

        if candidate.kind != "stdio" or not self.registration.bool_("complete_slots", True):
            return []
        # What will be written, not what the candidate holds: entry_for leaves
        # out an empty value with no reference behind it, so such a key is not
        # "present" -- counting it so let BUILD_EXAMPLE_NET_USERNAME="" pass this
        # check and vanish from the file.
        written = {k: v for k, v in candidate.env.items() if v != "" or k in candidate.references}
        _path, missing = manifest_missing({"env": {**written, **candidate.secret_env}}, self.settings)
        for variable in missing:
            candidate.secret_env[variable] = f"env:{variable}"
            if variable not in candidate.completed_slots:
                candidate.completed_slots.append(variable)
        if missing:
            candidate.add_note(
                f"{', '.join(missing)} added to the entry: what it launches reads them, and the entry did not pass them"
            )
            log.info(
                "%s: %s added -- read by what it launches, missing from the entry", candidate.label, ", ".join(missing)
            )
        return missing

    def prefer_installed(self, candidate: Candidate) -> str:
        """Run the installed program, not a copy an agent built in its workspace; what changed, or "".

        build-opensuse-org was written to run
        ``…/agent-workspace/artifacts/build-opensuse-org/bin/osc-mcp`` -- a
        build in this program's scratch space -- while the same run had
        installed ``/usr/local/bin/osc-mcp``. Copied to another machine, the
        entry named a file that is not there. When the launch command lies in
        the agent workspace and a program of the same name is on the PATH
        outside it, that program is trialled the same way; if it answers alike
        (the same server, the same tools, the same wait for a credential), the
        entry runs it. (``mcp.registration.prefer_installed_program``.)
        """
        import copy

        from .portability import expand
        from .runtime import workspace_for

        if candidate.kind != "stdio" or not self.registration.bool_("prefer_installed_program", True):
            return ""
        command = candidate.full_command()
        if not command:
            return ""
        built = Path(expand(str(command[0])))
        workspace = workspace_for(self.ctx)
        try:
            built.resolve().relative_to(workspace.resolve())
        except (ValueError, OSError):
            return ""
        installed = self.ctx.runner.which(built.name)
        if not installed:
            return ""
        try:
            Path(installed).resolve().relative_to(workspace.resolve())
            return ""  # the PATH finds the same scratch copy
        except (ValueError, OSError):
            pass
        shake = candidate.handshake
        if shake is None:
            return ""
        variant = copy.deepcopy(candidate)
        variant.command = [installed, *candidate.command[1:]] if candidate.command else [installed]
        variant.handshake = None
        self.verifier.verify_all([variant], stop_at_first=True)
        alike = self._answers_alike(candidate, variant)
        if not alike:
            log.info(
                "%s: %s on the PATH does not answer like the workspace build; the build is kept",
                candidate.label,
                installed,
            )
            return ""
        candidate.command = list(variant.command)
        note = (
            f"runs {installed}, the installed {built.name}, instead of the copy built in the agent workspace "
            f"({built}) -- it answered the same, and outlives the run"
        )
        candidate.add_note(note)
        log.info("%s: %s", candidate.label, note)
        return note

    @staticmethod
    def _answers_alike(build: Candidate, installed: Candidate) -> bool:
        """Does ``installed`` reach the outcome ``build`` reached -- the same job, not the same bytes?

        Compared by outcome: a build that works needs an installed program that
        works and offers at least its tools; a build waiting for a credential
        needs one waiting too (or working). The server's name counts only when
        both gave one. Exact equality refused /usr/local/bin/osc-mcp for a
        workspace build of the same server: both refused to start without a
        login and named it -- the build's trial had merely carried an extra slot.
        """
        mine, theirs = build.handshake, installed.handshake
        if mine is None or theirs is None:
            return False
        if mine.server_name and theirs.server_name and mine.server_name != theirs.server_name:
            return False
        if build.verified:
            return installed.verified and set(mine.tools) <= set(theirs.tools)
        if build.needs_credential:
            if not (installed.needs_credential or installed.verified):
                return False
            return not (mine.tools and theirs.tools) or set(mine.tools) <= set(theirs.tools)
        return False

    def path_roots(self) -> list[tuple[str, str]]:
        """``(variable, absolute root)`` pairs, most specific first.

        Read from ``[mcp.registration.path_variables]``; an empty table turns
        the rewriting off and leaves plain absolute paths behind.
        """
        table = self.settings.dict_("mcp.registration.path_variables", {})
        roots: list[tuple[str, str]] = []
        for variable, raw in table.items():
            root = str(raw or "").strip().rstrip("/") or str(raw or "").strip()
            if root and not root.startswith("/") and not root.startswith("$"):
                # A relative root (artifacts_home, "mcp-servers", "." for the
                # folder itself) lies beside the file the entries are written to.
                root = str((self.json_path.resolve().parent / root).resolve())
            if variable.strip() and root.startswith("/") and not self._too_broad_root(root):
                roots.append((variable.strip(), root))
        # Longest first, so a file under the artifacts directory is named by the
        # artifacts variable rather than by the state directory above it.
        roots.sort(key=lambda pair: len(pair[1]), reverse=True)

        # The interpreter, last, because it matches whole rather than as a
        # prefix. It is a system binary and would otherwise be left alone like
        # /etc/ssl/ca-bundle.pem -- but unlike that it is a *choice this program
        # made*: it is pinned absolute precisely because a bare `python3`
        # resolved to a different interpreter with a different trust store on
        # the machine that launched it. Pinned as the default and overridable by
        # the variable keeps that fix and makes it movable.
        variable = self.settings.str_("mcp.registration.interpreter_variable", "ICA_MCP_PYTHON")
        interpreter = self.settings.str_("mcp.bridge.generated.interpreter", "") or sys.executable
        if variable and str(interpreter).startswith("/"):
            roots.append((variable, str(interpreter)))

        roots.extend(self.launcher_roots())
        return roots

    def _too_broad_root(self, root: str) -> bool:
        """Is ``root`` too broad to stand for one program's files -- ``/``, the home folder, or one above it?

        The project folder (``ICA_MCP_PROJECT = "."``) is the folder holding
        ``.mcp.json``. When that is the home folder, an SSH key or
        ``~/.local/bin/uvx`` would be written as "a file of this project".
        (``mcp.registration.path_root_never``.)
        """
        try:
            path = Path(root).resolve()
            home = Path.home().resolve()
        except (OSError, RuntimeError):
            return False
        never = [
            Path(os.path.expanduser(str(p))).resolve()
            for p in self.registration.str_list("path_root_never", ["/", "~"])
            if str(p).strip()
        ]
        return path in never or path == home or path in home.parents

    def program_variables(self) -> dict[str, str]:
        """This program's own path variables and what they stand for here.

        ``ICA_MCP_PYTHON``, ``ICA_MCP_ARTIFACTS`` and the rest are written by
        this program, as ``${NAME:-<path>}``. An entry that names one *without*
        its default -- an older version, a hand edit, a copy -- cannot be
        resolved by anybody else, and reuse used to call it "``${ICA_MCP_PYTHON}``
        is not installed". This program knows what each one means.
        """
        found = {variable: root for variable, root in self.path_roots()}
        for variable, root in recorded_path_variables(self.ctx).items():
            found.setdefault(variable, root)
        # The trust bundle entries read through trust_reference is this
        # machine's own (own_references): phase 1 sets it wherever it lives here.
        trust_name = self.registration.str_("trust_reference", "ICA_MCP_CA_BUNDLE")
        if trust_name and self.registration.bool_("own_reference_names", True):
            from ..core.trust_store import system_bundle

            bundle = system_bundle(self.settings)
            if bundle:
                found.setdefault(trust_name, bundle)
        return found

    def resolve_program_variable(self, name: str) -> str:
        """What a variable this program names stands for here; "" when it is not one of ours.

        Known ones first (:meth:`program_variables`, including every per-file
        name it recorded when writing). Otherwise a name derived from a file
        (:meth:`path_variable_for`: ``/usr/bin/python3.13`` became
        ``ICA_MCP_PYTHON3_13``) is spelled back -- ``_`` as ``.``, ``-`` or
        itself -- and looked up on ``PATH``.
        """
        known = self.program_variables()
        if name in known:
            return known[name]
        return self.program_variable_on_path(name)

    def program_variable_on_path(self, name: str) -> str:
        """The binary on ``PATH`` a variable this program derived from a file's name spells; "" otherwise."""
        prefix = self.settings.str_("mcp.registration.path_variable_prefix", "ICA_MCP")
        if not prefix or not name.startswith(prefix + "_"):
            return ""
        token = name[len(prefix) + 1 :].lower()
        for spelled in dict.fromkeys([token.replace("_", "."), token.replace("_", "-"), token]):
            found = self.ctx.runner.which(spelled)
            if found and str(found).startswith("/"):
                return str(found)
        return ""

    def launcher_roots(self) -> list[tuple[str, str]]:
        """``(variable, absolute path)`` for each launcher binary we can find.

        The interpreter above is pinned and made overridable for a good reason;
        a *launcher* -- ``uvx``, ``npx``, ``node``, ``docker`` -- was left out,
        and it is the commonest absolute path in a real ``.mcp.json``. Agents
        are told to report the binary they tested by absolute path, so an entry
        arrives naming ``/home/linuxbrew/.linuxbrew/bin/uvx``. That works, and
        on the next machine -- where ``uvx`` is at ``/usr/bin/uvx``, or under
        ``~/.local/bin`` -- it is ``ENOENT``, reported as
        ``failed · Connection closed`` with nothing to say why. It is the first
        failure this program was ever shown, and it is still in the file.

        Whole path rather than the containing directory, matching the
        interpreter: one variable means one binary, so
        ``export ICA_MCP_UVX=/usr/bin/uvx`` says exactly what it does. A
        directory variable would be shorter where the toolchain shares a
        ``bin``, and wrong the moment it does not.

        A launcher not installed here is simply not named -- there is no path
        to write a default from, and a bare ``uvx`` resolved through ``PATH``
        is already portable.
        """
        template = self.settings.str_("mcp.registration.launcher_variable_template", "ICA_MCP_{LAUNCHER}")
        if not template:
            return []
        found: list[tuple[str, str]] = []
        for name in self.launcher_names():
            resolved = self.ctx.runner.which(name)
            if not resolved or not str(resolved).startswith("/"):
                continue
            variable = template.replace("{LAUNCHER}", re.sub(r"[^A-Za-z0-9]+", "_", name).upper())
            found.append((variable, str(resolved)))
        return found

    def launcher_names(self) -> list[str]:
        """The binaries ``[mcp.packages.launchers]`` actually launches things with.

        Derived from the launch templates rather than listed again, so adding a
        launcher to that table is enough and the two cannot drift. ``{python}``
        and friends are placeholders, not binaries, and are skipped.
        """
        names: list[str] = []
        for template in self.settings.dict_("mcp.packages.launchers", {}).values():
            first = str(template[0]).strip() if isinstance(template, Sequence) and template else ""
            if first and not first.startswith("{") and first not in names:
                names.append(first)
        return names

    def portable(self, value: str, roots: Sequence[tuple[str, str]], *, key: str = "") -> str:
        """Rewrite an absolute path as ``${VARIABLE:-<that path>}``.

        ``.mcp.json`` is meant to be copied, and **every absolute path in it is
        a fact about one machine**. That is the whole rule, and it took four
        separate reports to arrive at it, each one a path nobody had thought to
        cover:

        * the bridge script, its manifest and its CA chain, under this
          program's state directory -- covered by a configured root;
        * ``/usr/bin/python3``, because a bare ``python3`` resolved to a
          different interpreter with a different trust store;
        * ``/root/.local/bin/uvx``, which was ``ENOENT`` on the next machine and
          surfaced as ``failed - Connection closed``;
        * ``OPENQA_VERIFY=/home/…/chain.pem``, a PEM belonging to a third-party
          package, under no root, matching no binary.

        Each fix named one more kind of path. The fourth is the one that shows
        the shape of the mistake: enumerating kinds of path is a losing game,
        because the next entry will hold a kind nobody enumerated. So the
        default is now *every* absolute path, and the question is only what to
        call the variable.

        Naming, in order:

        1. **A configured root** it lives under -- ``${ICA_MCP_ARTIFACTS:-…}/rest``.
           First, so a bundle of files moves by exporting one variable rather
           than one each.
        2. **A binary we know** -- the interpreter, or a launcher from
           ``[mcp.packages.launchers]``. Whole path, named after the binary.
        3. **The environment variable it is the value of.** A PEM in
           ``MCP_BRIDGE_CA_BUNDLE`` is written
           ``"${MCP_BRIDGE_CA_BUNDLE:-/etc/ssl/ca-bundle.pem}"`` -- the operator
           overrides the name the product already documents, and no name has to
           be invented.
        4. **Its own basename**, prefixed, for a path in ``command`` or
           ``args`` that is none of the above.

        The default keeps the entry byte-identical in effect on the machine that
        wrote it: Claude Code expands ``${VAR:-default}`` in ``command``,
        ``args`` and ``env``, applying the default when the variable is unset.
        That is also what makes this safe to apply broadly -- an unset variable
        yields exactly the original string, so even a value that merely *looks*
        like a path is unchanged in effect.

        The earlier version of this method stopped at rule 1 and argued that a
        variable for every system file would be noise. The evidence went the
        other way: an entry with four variables is one an operator can move,
        and an entry with four absolute paths is one they cannot, without first
        working out which files matter.
        """
        text = str(value)
        parts = search_path(text)
        if parts is not None:
            # A search path: each directory under a configured root moves with
            # it; nothing else is renamed, because the list as a whole is the
            # value of one variable and its parts are not files.
            return os.pathsep.join(
                self.portable(part, roots) if any(part == r or part.startswith(r + "/") for _, r in roots) else part
                for part in parts
            )
        if not text.startswith("/"):
            return text
        for variable, root in roots:
            if text == root:
                return f"${{{variable}:-{root}}}"
            if text.startswith(root + "/"):
                return f"${{{variable}:-{root}}}{text[len(root) :]}"
        variable = self.path_variable_for(text, key=key)
        if variable and not key.strip():
            # Remembered, so an entry read back without its default -- the
            # claude CLI prints ${NAME:-x} as ${NAME} -- still resolves.
            record_path_variable(self.ctx, variable, text)
        return f"${{{variable}:-{text}}}" if variable else text

    @staticmethod
    def _path_variables_in(entry: Mapping[str, Any]) -> dict[str, str]:
        """``{variable: the path it defaults to}`` for one written entry.

        Only variables that carry a default, which is what separates a
        relocatable path from an empty credential slot: ``${ICA_MCP_UVX:-/…}``
        is something to move, ``${GITLAB_TOKEN}`` is something to obtain, and
        the briefing says different things about them.
        """
        found: dict[str, str] = {}
        texts = [str(entry.get("command", "")), *[str(a) for a in entry.get("args") or []]]
        texts += [str(v) for v in (entry.get("env") or {}).values()]
        for text in texts:
            for match in ENTRY_VARIABLE.finditer(text):
                default = match.group(2)
                if default and default.startswith("/"):
                    found.setdefault(match.group(1), default)
        return found

    @staticmethod
    def _settings_in(entry: Mapping[str, Any]) -> list[str]:
        """One line per ``env`` key: what it is set to and how to change it.

        Read back from what was *written*, like :meth:`_path_variables_in`, so
        the briefing cannot describe an entry other than the one in the file.
        """
        lines: list[str] = []
        for key, value in sorted((entry.get("env") or {}).items()):
            text = str(value)
            match = ENTRY_VARIABLE.search(text)
            if not match:
                lines.append(f"{key} = {redact.redact(text)} (fixed)")
                continue
            variable, default = match.group(1), match.group(2)
            rest = text[match.end() :]
            if default is None:
                lines.append(f"{key}: export {variable} -- required, no default")
            elif default == "" and not rest:
                lines.append(f"{key}: export {variable} to set it -- empty until you do")
            elif rest:
                lines.append(f"{key}: {default}{rest} here -- export {variable} to move the directory it lives in")
            else:
                lines.append(f"{key}: {default} here -- export {variable} to change it")
        return lines

    def path_variable_for(self, path: str, *, key: str = "") -> str:
        """What to call the variable for an absolute path nothing else named.

        ``key`` -- the environment variable the path is the value of -- wins,
        because the product already chose that name and the operator has
        probably read it in the product's own documentation. Otherwise the
        basename, sanitised and prefixed, which is at least recognisable in a
        list of exports.

        Returns ``""`` when the naming is switched off
        (``mcp.registration.name_every_absolute_path``), which restores the
        older behaviour of leaving unrecognised paths alone.
        """
        if not self.settings.bool_("mcp.registration.name_every_absolute_path", True):
            return ""
        if key.strip():
            return re.sub(r"[^A-Za-z0-9]+", "_", key.strip()).strip("_").upper()
        prefix = self.settings.str_("mcp.registration.path_variable_prefix", "ICA_MCP")
        stem = PurePosixPath(str(path)).name or str(path)
        token = re.sub(r"[^A-Za-z0-9]+", "_", stem).strip("_").upper()
        if not token:
            return ""
        return f"{prefix}_{token}" if prefix else token

    def _exported_name(self, key: str, server: str, candidate: Candidate | None = None) -> str:
        """The variable an operator exports for ``key``: namespaced if generic or already claimed.

        :meth:`_plain_name` decides for a generic key; :meth:`_shared_name` then
        renames one that another service's entry already reads.
        """
        if self.env_contract.session(key):
            return str(key).strip()  # the session's variable keeps the session's name
        plain = self._plain_name(key, server)
        if candidate is not None and plain != str(key).strip():
            role = self._role_name(key, server, candidate)
            if role and role != plain and self._is_set(plain) and not self._is_set(role):
                # The operator already exports the name an earlier run gave this
                # slot: it is kept, never renamed out from under them.
                role = ""
            plain = role or plain
        return self._shared_name(plain, server, candidate) if candidate is not None else plain

    def _trial_source(self, candidate: Candidate, key: str) -> str:
        """The variable the trial read ``key``'s value from, when it is another name and holds a value; "" otherwise.

        gitlab.example.net's agent declared ``GITLAB_PERSONAL_ACCESS_TOKEN`` as
        ``env:GITLAB_EXAMPLE_NET_TOKEN`` -- the variable the operator keeps in
        Claude Code's settings. The trial read that one and the login was
        accepted, and the entry was written reading ``${GITLAB_PERSONAL_ACCESS_TOKEN}``,
        which nothing sets: a new terminal's Claude Code started the server
        with no token. The entry reads what the trial read. Only a reference
        to another variable that is set -- a name the slot only proposes is
        still named here (:meth:`_exported_name`).
        (``mcp.registration.read_trial_source``.)
        """
        if not self.registration.bool_("read_trial_source", True):
            return ""
        scheme, _, variable = str(candidate.secret_env.get(key) or "").partition(":")
        variable = variable.strip()
        if (
            scheme.strip().lower() != "env"
            or variable == str(key).strip()
            or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", variable)
        ):
            return ""
        return variable if self._is_set(variable) else ""

    def _is_set(self, variable: str) -> bool:
        """Does a new terminal's Claude Code have ``variable`` -- exported, or in its settings env?"""
        return bool(os.environ.get(variable) or self._claude_settings_env().get(variable))

    def _role_name(self, key: str, server: str, candidate: Candidate) -> str:
        """``{SLUG}_{PART}`` for a generic login slot (``name_by_role_parts``: a user name, a password); "" otherwise.

        osc-mcp reads ``OSC_MCP_USER`` and ``OSC_MCP_PASSWORD``; namespaced,
        they became ``BUILD_OPENSUSE_ORG_OSC_MCP_USER`` -- names nobody
        exports, for a login the operator already exports as
        ``BUILD_OPENSUSE_ORG_USERNAME`` / ``_PASSWORD``, the names the client
        bridge for the same service reads. A slot is named for what it holds
        (:func:`.inventory.slot_part`) when no other slot of the entry holds the
        same, with
        ``mcp.bridge.generated.auth.variable_template``. Only a key that would
        be namespaced anyway: a vendor's own name (``GITHUB_PERSONAL_ACCESS_TOKEN``)
        is kept. (``mcp.registration.name_by_role``.)
        """
        from . import bridge_auth
        from .inventory import slot_part

        if not self.registration.bool_("name_by_role", True):
            return ""
        if key not in candidate.secret_env and not self._login_part(key, candidate):
            # Both halves of a login are named alike: build-example-net's OBS_USERNAME,
            # beside its OBS_SSH_KEY slot, became BUILD_EXAMPLE_NET_OBS_USERNAME while
            # the operator exported BUILD_EXAMPLE_NET_USERNAME.
            return ""
        part = slot_part(key, self.settings)
        if part not in self.registration.str_list("name_by_role_parts", list(ROLE_PARTS)):
            return ""
        if any(other != key and slot_part(other, self.settings) == part for other in candidate.secret_env):
            return ""
        slug = re.sub(r"[^A-Za-z0-9]+", "_", server).strip("_").upper()
        if not slug:
            return ""
        if not candidate.secret_formats.get(key):
            host = candidate.secret_sources.get(key, "").strip() or self.service_host(candidate) or server
            candidate.secret_formats[key] = bridge_auth.describe_part(part, host, self.settings)
        return bridge_auth.exported_name(slug, part, self.settings)

    def _shared_name(self, variable: str, server: str, candidate: Candidate | None) -> str:
        """``variable``, or a per-service name for it if another service's entry reads it.

        A credential belongs to one instance. jira.example.com and sd.example.com run
        the same package, which reads ``JIRA_PERSONAL_TOKEN``; written through
        for both, one exported value had to be two instances' tokens, and at
        most one of the two entries could ever work -- so the operator pasted a
        literal token into each, which is the one thing the file must never
        hold. The key stays what the server reads; the ``${...}`` it points at
        becomes ``mcp.registration.shared_secret_template`` (``{SLUG}_{NAME}``)
        when an entry already in the file, or written earlier in this run,
        reads the same variable for a different host.
        """
        if candidate is None or not server or not variable:
            return variable
        slug = re.sub(r"[^A-Za-z0-9]+", "_", server).strip("_").upper()
        if not slug or variable.upper().startswith(slug):
            return variable
        mine = set(hosts_named([candidate.url, *candidate.full_command(), *candidate.env.values()]))
        if not mine:
            return variable  # nothing says which instance this is, so nothing to tell apart
        for other, entry in self._other_entries(server).items():
            if variable not in _credential_variables(entry):
                continue
            if (
                self.registration.bool_("same_address_one_instance", True)
                and self.address_of.get(other)
                and self.address_of.get(other) == self.address_of.get(server)
            ):
                # Written for the same address in this run: one instance, one
                # credential. osc-mcp (api.opensuse.org) and the client bridge
                # (build.opensuse.org) both serve build.opensuse.org, and the
                # second was told to read BUILD_OPENSUSE_ORG_STDIO_BUILD_OPENSUSE_ORG_PASSWORD.
                continue
            theirs = _entry_hosts(entry)
            if theirs and theirs != mine:
                template = self.registration.str_("shared_secret_template", "{SLUG}_{NAME}")
                renamed = template.replace("{SLUG}", slug).replace("{NAME}", variable.upper())
                candidate.add_note(
                    f"{variable} is already read by {other} for {', '.join(sorted(theirs))}; a credential "
                    f"belongs to one instance, so this entry reads {renamed} instead -- export that."
                )
                return renamed
        return variable

    def shared_secret_findings(self) -> list[dict[str, Any]]:
        """Credential variables read by entries for *different* services, across the whole file.

        :meth:`_shared_name` keeps this run's entries apart; this finds the
        pairs it could not -- two entries already in the file, or one with no
        host to tell it apart by. Names and hosts only, never a value.
        """
        entries = self._other_entries("")
        readers: dict[str, list[tuple[str, set[str]]]] = {}
        for name, entry in entries.items():
            for variable in _credential_variables(entry):
                readers.setdefault(variable, []).append((name, _entry_hosts(entry)))
        template = self.registration.str_("shared_secret_template", "{SLUG}_{NAME}")
        findings: list[dict[str, Any]] = []
        for variable, servers in sorted(readers.items()):
            hosts = {frozenset(h) for _, h in servers if h}
            if len(servers) < 2 or len(hosts) < 2:
                continue
            findings.append(
                {
                    "variable": variable,
                    "servers": [name for name, _ in servers],
                    "hosts": sorted({host for _, h in servers for host in h}),
                    "suggested": [
                        template.replace("{SLUG}", re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_").upper()).replace(
                            "{NAME}", variable.upper()
                        )
                        for name, _ in servers
                    ],
                }
            )
        # The same credential *value* sent to different instances, under any
        # names (compared by fingerprint, parts of a Basic credential included):
        # build-example-net sent build.opensuse.org's login, under a name of its own.
        if self.registration.bool_("compare_credential_values", True):
            from .existing import ExistingRegistrations  # imports this module

            for row in ExistingRegistrations.for_run(self.ctx).shared_credentials():
                findings.append({"variable": "", "same_value": True, "suggested": [], **row})
        return findings

    def _other_entries(self, server: str) -> dict[str, Mapping[str, Any]]:
        """Entries in the file, and written earlier in this run, other than ``server``'s."""
        entries = dict(self._existing_entries())
        entries.update({r.name: r.entry for r in self.published if not r.refused and r.entry})
        entries.pop(server, None)
        return entries

    def _plain_name(self, key: str, server: str) -> str:
        """The variable an operator exports for ``key``, namespaced if it is generic.

        ``mcp-graphql`` reads its auth header from a variable called plain
        ``HEADERS``. Written through as ``"HEADERS": "${HEADERS}"`` that is
        unusable: it collides with every other server in the file, and it tells
        the reader nothing about which service wants it. So the key stays
        ``HEADERS`` -- the server would not find it under any other name -- and
        the placeholder points at ``${SMELT_EXAMPLE_NET_HEADERS}``.

        Only *generic* keys are renamed. ``GITHUB_PERSONAL_ACCESS_TOKEN``
        already names its product, is what GitHub's own documentation tells
        people to export, and renaming it would surprise somebody who has had it
        in their shell for a year. The test is whether the key already carries
        one of the target's product tokens.
        """
        key = str(key).strip()
        if not server or not key:
            return key
        slug = re.sub(r"[^A-Za-z0-9]+", "_", server).strip("_").upper()
        if not slug or key.upper().startswith(slug):
            return key
        # The slug is the hostname with dashes for dots, so put them back:
        # product_tokens wants a host, and "github-com" has no labels to read.
        tokens = [t for t in product_tokens(server.replace("-", ".")) if len(t) > 2]
        if any(token.upper() in key.upper() for token in tokens):
            return key
        return f"{slug}_{key.upper()}"

    def _without_unset_settings(self, env: Mapping[str, Any], candidate: Candidate) -> dict[str, Any]:
        """A setting's bare ``${NAME}`` that nothing sets becomes ``${NAME:-}`` -- or goes, when it was proven unneeded.

        Claude Code passes an unset ``${NAME}`` as those literal characters:
        bugzilla-opensuse-org's ``BUGZILLA_AUTH_MODE: "${BUGZILLA_AUTH_MODE}"``
        reached the server as ``'${BUGZILLA_AUTH_MODE}'`` and it exited. A
        credential keeps its ``${NAME}`` slot -- that is what tells the operator
        to export it; a setting (a mode, a selector) never needs one.
        """
        if not self.registration.bool_("optional_unset_settings", True):
            return dict(env)
        out: dict[str, Any] = {}
        for key, value in env.items():
            text = str(value)
            if key in candidate.secret_env or is_secret_name(str(key)) or "${" not in text:
                out[key] = value
                continue

            def optional(match: re.Match[str]) -> str:
                variable, default = match.group(1), match.group(2)
                if default is not None or os.environ.get(variable) or is_secret_name(variable):
                    return match.group(0)
                if variable in candidate.kept_out:
                    # Supplied by the writer itself (Candidate.kept_out): set, not unset.
                    return match.group(0)
                return f"${{{variable}:-}}"

            rewritten = ENTRY_VARIABLE.sub(optional, text)
            if self._login_part(str(key), candidate):
                # Half of a login (a user name beside a password slot) is needed
                # for every call, not to start: kept, empty while unset. The kept
                # build-example-net lost BUILD_EXAMPLE_NET_USERNAME here, and with it any
                # way to pass the user name to its bridge.
                out[key] = rewritten
                continue
            if rewritten != text and key in candidate.unset_settings and not ENTRY_VARIABLE.sub("", rewritten).strip():
                candidate.add_note(f"{key} is left out of the entry: nothing sets it, and the server starts without it")
                continue
            out[key] = rewritten
        return out

    def _login_part(self, key: str, candidate: Candidate) -> bool:
        """Is ``key`` one part of a login whose other part is a credential slot of the entry (``name_by_role_parts``)?"""
        from .inventory import slot_part

        parts = self.registration.str_list("name_by_role_parts", list(ROLE_PARTS))
        if slot_part(key, self.settings) not in parts:
            return False
        return any(
            other != key and (slot_part(other, self.settings) in parts or is_secret_name(other))
            for other in candidate.secret_env
        )

    #: :meth:`credential_evidence` verdicts.
    PROVEN, SUSPECT = "proven", "suspect"

    def server_defaults(self, candidate: Candidate) -> dict[str, str]:
        """What the launched source falls back to for each variable it reads (:mod:`.env_contract`); {} if unknown."""
        if candidate.kind != "stdio" or not self.env_contract.enabled:
            return {}
        cache = self.__dict__.setdefault("_server_defaults_cache", {})
        key = tuple(str(part) for part in candidate.full_command())
        if key not in cache:
            try:
                cache[key] = {
                    r.name: str(r.default) for r in self.env_contract.reads_of(list(key)) if r.default is not None
                }
            except Exception:  # noqa: BLE001 - reading a source is evidence, never a reason to fail a write
                cache[key] = {}
        return cache[key]

    def credential_evidence(self, key: str, value: str, candidate: Candidate) -> str:
        """Is ``value``, under ``key``, a credential? ``PROVEN``, ``SUSPECT`` or ``""`` (configuration).

        One rule for every guard that keeps a value out of the file, so they can
        no longer disagree:

        * ``PROVEN`` -- the name says credential, the value carries one inline,
          or it is a credential already in use here (:func:`.secret_policy.holds_a_credential`);
        * ``""`` -- the server's own source uses exactly this value when the
          variable is unset (:meth:`server_defaults`): configuration, whatever it
          looks like. A server does not ship its operator's secret as a default;
        * ``SUSPECT`` -- it only *looks* random (:func:`.secret_policy.looks_opaque`).
          That alone kept a token under ``SMELT_EXAMPLE_NET_HEADERS`` out of the file,
          and it also called ``mcp-bridge-gitlab-example-net/1`` a credential. So a
          suspect value is kept out of the file and kept working (``kept_out``),
          and is never reported as a credential the operator must supply.
        """
        text = str(value or "")
        if not text.strip():
            return ""
        if holds_a_credential(str(key), text):
            return self.PROVEN
        if self.server_defaults(candidate).get(str(key)) == text:
            return ""
        cfg = self.settings.section("mcp.discovery.reuse")
        if looks_opaque(
            text,
            cfg.int_("opaque_value_min_chars", OPAQUE_MIN_CHARS),
            cfg.float_("opaque_value_min_entropy", OPAQUE_MIN_ENTROPY),
            cfg.float_("opaque_value_min_entropy_ratio", OPAQUE_MIN_RATIO),
        ) and not names_a_selector(str(key)):
            return self.SUSPECT
        return ""

    def _keep_out(self, key: str, value: str, variable: str, candidate: Candidate) -> str:
        """``${variable}`` for a ``SUSPECT`` value, which is kept where Claude Code reads it once written."""
        redact.register(value)
        candidate.kept_out[variable] = Secret(value, source=f"{key}, kept out of {self.display_path}")
        candidate.add_note(
            f"{key}'s value looks like a secret although nothing shows it is one; the entry reads "
            f"${{{variable}}} and the value is kept in Claude Code's settings env when the entry is written, "
            "so the file never holds it and the server gets exactly what it was tested with. Nothing to export."
        )
        log.info("%s: %s looks like a secret; kept out of the file as %s", candidate.label, key, variable)
        return ENV_PLACEHOLDER.format(name=variable)

    def _without_secret_defaults(self, env: Mapping[str, Any], candidate: Candidate) -> dict[str, Any]:
        """Strip a credential hiding as the default of a ``${NAME:-…}`` reference.

        ``${NAME:-value}`` reads like a reference and is a literal: the value is
        in the file. build.example.net's entry carried a 24-character token as the
        default of its auth variable, where every check that looks for a bare
        literal secret walked past it. Whatever produced such a value -- an
        agent, a repair, an older entry carried forward -- the slot is written
        as ``${NAME}`` and the value stays out of the file. A path default is
        never touched: a file's location is not its contents.

        Judged by :meth:`credential_evidence`, like every guard: a default that
        only *looks* random is kept out of the file and kept working
        (:meth:`_keep_out`) -- never left as a bare ``${NAME}`` nothing sets --
        and one equal to the server's own default is configuration.
        """
        out: dict[str, Any] = {}
        stripped: list[str] = []
        for key, value in env.items():
            text = str(value)

            def clean(match: re.Match[str], key: str = str(key)) -> str:
                variable, default = match.group(1), match.group(2)
                if not default or default.startswith("/") or "$" in default:
                    return match.group(0)
                evidence = self.credential_evidence(key, default, candidate)
                if evidence == self.PROVEN:
                    stripped.append(str(key))
                    return f"${{{variable}}}"
                if evidence == self.SUSPECT:
                    return self._keep_out(key, default, variable, candidate)
                return match.group(0)

            out[key] = ENTRY_VARIABLE.sub(clean, text)
        for key in stripped:
            candidate.add_note(
                f"{key} carried a credential as the default of its ${{…}} reference; the default "
                "was removed so the value never reaches the file. Export the variable it names."
            )
            variable = next((m.group(1) for m in ENTRY_VARIABLE.finditer(str(out.get(key, "")))), key)
            if (candidate.name, variable) not in self.removed_defaults:
                self.removed_defaults.append((candidate.name, variable))
            log.info("removed a credential-shaped default from %s for %s", key, candidate.label)
        return out

    def _without_literal_credentials(self, env: Mapping[str, Any], candidate: Candidate, server: str) -> dict[str, Any]:
        """Turn any literal credential left in ``env`` into a ``${…}`` reference -- the last guard.

        Whatever produced the value -- an agent that copied it from an old
        entry, a registry, a repair -- a credential never reaches the file:
        build.example.net's token arrived under ``API_HEADERS``, a name no
        credential word matched, and was written as a setting. Recognised by
        name, by what the name carries (secret_policy.carries_credentials), by
        being a credential already in use here, or by looking random. The key
        stays what the server reads; the reference is named like any secret's,
        and the operator is told to export it.

        Only a *proven* credential (:meth:`credential_evidence`) is told to be
        exported. One that merely looks random is kept out of the file and kept
        working (:meth:`_keep_out`): written as ``${NAME}`` with the value held
        where Claude Code reads it. Making it a bare reference nothing set, and a
        credential slot, turned gitlab.example.net's bridge User-Agent into
        "waiting for a credential" and a Claude Code warning.
        """
        out: dict[str, Any] = {}
        for key, value in env.items():
            text = str(value)
            literal = text and "${" not in text and not text.startswith("/")
            evidence = self.credential_evidence(str(key), text, candidate) if literal else ""
            if evidence == self.SUSPECT:
                out[key] = self._keep_out(str(key), text, self._exported_name(str(key), server, candidate), candidate)
                continue
            if evidence == self.PROVEN:
                exported = self._exported_name(str(key), server, candidate)
                out[key] = ENV_PLACEHOLDER.format(name=exported)
                candidate.secret_env.setdefault(str(key), f"env:{exported}")
                candidate.exported_names[str(key)] = exported
                candidate.add_note(
                    f"{key} held a credential as a literal value; the entry reads ${{{exported}}} instead -- "
                    "export it. The value is never written into the file."
                )
                log.info("%s: a literal credential in %s became a reference", candidate.label, key)
                continue
            out[key] = value
        return out

    def _without_tls_weakening(self, env: Mapping[str, str], candidate: Candidate) -> dict[str, str]:
        """Drop variables that switch certificate verification off.

        ``NODE_TLS_REJECT_UNAUTHORIZED`` turns off certificate checking for a
        whole Node process, and it reached a registered entry twice over: once
        because an agent set it to reach a host with an internal CA, and once
        because ``auth`` matched inside ``UNAUTHORIZED``, so it was filed as a
        credential and written as ``"${NODE_TLS_REJECT_UNAUTHORIZED}"`` -- a
        placeholder for something that is not a secret.

        Either way it has no business in ``.mcp.json``. A file that is meant to
        be copied should not carry a switch that silently disables TLS on
        whichever machine it lands on, and one left as an unset ``${…}`` is
        worse than useless: it does nothing until the day somebody exports that
        name for an unrelated reason. If a server genuinely cannot be reached
        without it, that is a trust-store problem to report, not a flag to
        smuggle.
        """
        blocked = {
            n.strip().upper()
            for n in self.settings.str_list(
                "mcp.registration.tls_weakening_env",
                [
                    "NODE_TLS_REJECT_UNAUTHORIZED",
                    "PYTHONHTTPSVERIFY",
                    "GIT_SSL_NO_VERIFY",
                    "SSL_VERIFY",
                    "SSL_NO_VERIFY",
                    "CURL_INSECURE",
                    "REQUESTS_SKIP_VERIFY",
                    "REJECT_UNAUTHORIZED",
                ],
            )
            if n.strip()
        }
        # A list of exact names always lags reality: `REJECT_UNAUTHORIZED`,
        # which `@ivotoby/openapi-mcp-server` uses, reached a real entry as
        # "false" because the list only knew `NODE_TLS_REJECT_UNAUTHORIZED`.
        # The patterns catch the next spelling nobody has met yet; the list
        # stays because it documents the ones we have.
        patterns = [
            p.strip().upper()
            for p in self.settings.str_list(
                "mcp.registration.tls_weakening_patterns",
                [
                    "NO_VERIFY",
                    "SKIP_VERIFY",
                    "REJECT_UNAUTHORIZED",
                    "INSECURE",
                ],
            )
            if p.strip()
        ]
        if not blocked and not patterns:
            return dict(env)
        kept: dict[str, str] = {}
        for name, value in env.items():
            upper = name.strip().upper()
            if upper not in blocked and not any(p in upper for p in patterns):
                kept[name] = value
                continue
            candidate.add_note(
                f"{name} was dropped from the entry: it disables TLS certificate verification, "
                "and .mcp.json is copied between machines. If this server needs a private CA, "
                "install the certificate in the system trust store instead"
            )
            log.info("dropped %s from the entry for %s: it weakens TLS", name, candidate.label)
        return kept

    # -- publishing ------------------------------------------------------------

    def client_timeout_seconds(self) -> float:
        """How long Claude Code waits for a server to start: ``MCP_TIMEOUT`` (ms) -- this shell's, else
        Claude Code's own ``settings.json`` env -- else ``client_timeout_seconds``."""
        config = self.registration.section("client_timeout")
        variable = config.str_("variable", "MCP_TIMEOUT")
        for value in (os.environ.get(variable, "") if variable else "", self._claude_settings_env().get(variable, "")):
            try:
                if str(value).strip():
                    return float(value) / 1000.0
            except ValueError:
                continue
        return config.float_("seconds", 30.0)

    def _claude_settings_env(self) -> dict[str, str]:
        """The ``env`` Claude Code reads from the settings this program writes -- shared and private file alike."""
        from . import claude_check

        return claude_check._claude_settings_env(self.ctx)

    def _keep_held_credentials(self, registration: Registration) -> None:
        """A credential the entry used as a literal is kept where Claude Code reads it, never in ``.mcp.json``.

        The project file holds references only, so a token or password an
        existing entry carried as a literal (or as a ``${NAME:-default}``) is
        taken out of it -- and the entry then had nothing to send:
        sd-example-com, openqa-example-net and gitlab-example-net came out "waiting for a
        credential" although the operator had supplied one. The value goes into
        Claude Code's own ``settings.json`` ``env`` under the variable the entry
        now reads (Claude Code expands an entry's ``${NAME}`` from it -- proved
        with ``claude mcp get``), mode 0600, never over a different value
        already there, never one this run saw refused, and only for a variable
        the shell does not already export. (``mcp.registration.keep_credentials_in``.)
        """
        candidate = registration.candidate
        where = self.registration.str_("keep_credentials_in", "settings").strip().lower()
        if candidate is None or where != "settings" or self.ctx.dry_run or registration.refused:
            return
        if not candidate.in_use and not candidate.kept_out and not candidate.machine_values:
            return
        shake = candidate.handshake
        refused = shake is not None and shake.login in login_check.REFUSED and not login_check.nothing_sent(shake)
        text = json.dumps(registration.entry)
        current = self._claude_settings_env()
        updates: dict[str, str] = {}
        pairs: list[tuple[str, Any]] = []
        if not refused:
            # A credential this run saw refused is never kept.
            for slot, secret in candidate.in_use.items():
                if slot in candidate.held_names:
                    continue  # read from what the operator exports for it already
                pairs.append((candidate.exported_names.get(slot) or candidate.reuse_names.get(slot) or slot, secret))
        # A value kept out only because it looks like a secret is part of the
        # launch that was tested (see Candidate.kept_out), refused login or not.
        pairs.extend(candidate.kept_out.items())
        # ...and this machine's runtime settings, which the file reads by variable:
        # through the one per-machine writer, into every place a new terminal's
        # Claude Code reads (core/machine_env.py), never only settings.json.
        machine = dict(candidate.machine_values)
        from ..core.machine_env import MachineEnv

        if machine and MachineEnv(self.ctx).enabled:
            for variable, value in machine.items():
                if f"${{{variable}" in text and str(value or "").strip():
                    MachineEnv(self.ctx).apply(
                        variable,
                        str(value),
                        f"{registration.name}: a runtime setting this "
                        "machine needs, read by the entry through this variable",
                    )
            machine = {}
        pairs.extend(machine.items())
        for variable, secret in pairs:
            if f"${{{variable}}}" not in text and f"${{{variable}:-" not in text:
                continue
            value = secret.reveal() if hasattr(secret, "reveal") else str(secret or "")
            if not value.strip() or os.environ.get(variable):
                continue
            if current.get(variable) and current[variable] != value:
                registration.note(f"{variable} in Claude Code's settings holds another value; left as it is")
                continue
            if current.get(variable) == value:
                continue
            redact.register(value)
            updates[variable] = value
        if not updates:
            return
        from ..provision.settings_writer import SettingsWriter, secret_settings_path

        path = secret_settings_path(self.ctx)
        result = SettingsWriter(self.ctx, path).apply({"env": updates})
        if getattr(result, "error", ""):
            log.warning("could not keep %s in %s: %s", ", ".join(sorted(updates)), path, result.error)
            return
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        names = ", ".join(sorted(updates))
        registration.needs_env = [n for n in registration.needs_env if n not in updates]
        if not registration.needs_env:
            # Its slot is filled where Claude Code reads it: no longer an entry
            # "registered with an empty credential".
            registration.provisional = False
        registration.note(f"{names} kept in {path} (env), where Claude Code reads it -- not in {self.display_path}")
        registration.remediation.append(
            f"{registration.name}: what its entry carried is now kept as {names} in {path} (env), which "
            f"Claude Code reads; {self.display_path} holds only the reference. Restart Claude Code."
        )
        log.info("%s: %s kept in %s", registration.name, names, path)

    def _keep_supplied_values(self, registration: Registration, target: str) -> None:
        """Every value an existing entry supplied that the new entry still needs is kept where Claude Code reads it.

        Not only credentials: build-example-net's old entry carried the user name
        as ``${BUILD_EXAMPLE_NET_USERNAME:-<name>}``; the new entry reads
        ``${BUILD_EXAMPLE_NET_USERNAME}``, the value was dropped with the old entry,
        and only the run's own shell still had it -- a new terminal failed. A
        ``${NAME}`` the written entry reads with no value of its own, that a new
        terminal's Claude Code would not have (:func:`.claude_check.fresh_environ`),
        and that an existing entry for the address supplied, goes into Claude
        Code's settings.json env under that name (mode 0600), never over another
        value there, and the operator is told. (``mcp.registration.keep_supplied_values``.)
        """
        from . import claude_check
        from .existing import ExistingRegistrations  # imports this module

        if (
            registration.refused
            or self.ctx.dry_run
            or not registration.written
            or not self.registration.bool_("keep_supplied_values", True)
        ):
            return
        # ${NAME} and ${NAME:-} alike: both are empty in a new terminal that lacks NAME
        # (build-example-net's user name was written as ${BUILD_EXAMPLE_NET_USERNAME:-}).
        needed = sorted(set(re.findall(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-)?\}", json.dumps(registration.entry))))
        if not needed:
            return
        fresh = claude_check.fresh_environ(self.ctx)
        missing = [n for n in needed if not fresh.get(n)]
        if not missing:
            return
        supplied = ExistingRegistrations.for_run(self.ctx).supplied_values(host_of(target) or target)
        updates = {n: supplied[n][0] for n in missing if n in supplied and str(supplied[n][0]).strip()}
        if not updates:
            return
        from ..provision.settings_writer import SettingsWriter, secret_settings_path

        path = secret_settings_path(self.ctx)
        for value in updates.values():
            if holds_a_credential("", value) or looks_opaque(value):
                redact.register(value)
        result = SettingsWriter(self.ctx, path).apply({"env": updates})
        if getattr(result, "error", ""):
            log.warning("could not keep %s in %s: %s", ", ".join(sorted(updates)), path, result.error)
            return
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        names = ", ".join(sorted(updates))
        registration.needs_env = [n for n in registration.needs_env if n not in updates]
        if (
            not registration.needs_env
            and registration.provisional
            and registration.candidate is not None
            and registration.candidate.verified
        ):
            registration.provisional = False
        registration.note(
            f"{names} kept in {path} (env) from the entry it replaces, where a new terminal's Claude Code reads it"
        )
        registration.remediation.append(
            f"{registration.name}: {names}, which the earlier entry supplied, is now kept in {path} (env) -- the new "
            f"entry reads it and a new terminal would not have had it. Restart Claude Code."
        )
        log.info(
            "%s: %s kept in %s (supplied by %s)",
            registration.name,
            names,
            path,
            ", ".join(sorted({supplied[n][1] for n in updates})),
        )

    def move_literal_secrets(self) -> tuple[list[str], list[str]]:
        """Credentials the project file holds as plain text -- or as the default of ``${NAME:-…}`` -- moved to settings env.

        An operator who pastes a token into ``.mcp.json`` has a file that is
        copied between machines and sits beside a repository; the program never
        writes a value there, and an entry it keeps in place should not carry
        one either. Each such value goes into Claude Code's settings.json env
        (mode 0600) under the variable the entry reads -- the env key itself, or
        ``<SERVER>_<HEADER>`` for a header -- and the entry reads ``${NAME}``.
        Never over a different value already there: that one is left, and named.
        Returns ``(moved, left)`` as "server: NAME" lines -- never a value.
        (``mcp.registration.move_literal_secrets``.)
        """
        if self.ctx.dry_run or not self.registration.bool_("move_literal_secrets", True):
            return [], []
        path = self.json_path
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return [], []
        servers = document.get("mcpServers") if isinstance(document, Mapping) else None
        if not isinstance(servers, Mapping):
            return [], []
        from ..provision.settings_writer import SettingsWriter, secret_settings_path
        from . import claude_check

        target = secret_settings_path(self.ctx)
        current = claude_check._claude_settings_env(self.ctx)
        defaulted = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*):-(.+)\}$")
        updates: dict[str, str] = {}
        moved: list[str] = []
        left: list[str] = []
        changed = False
        for name, entry in servers.items():
            if not isinstance(entry, dict):
                continue
            slug = re.sub(r"[^A-Z0-9]+", "_", str(name).upper()).strip("_")
            for block in ("env", "headers"):
                values = entry.get(block)
                if not isinstance(values, dict):
                    continue
                for key, raw in list(values.items()):
                    text = str(raw)
                    match = defaulted.match(text)
                    variable, value = (match.group(1), match.group(2)) if match else ("", text)
                    if not value or "${" in value or not holds_a_credential(str(key), value):
                        continue
                    if not variable:
                        variable = (
                            str(key) if block == "env" else f"{slug}_{re.sub(r'[^A-Z0-9]+', '_', str(key).upper())}"
                        )
                    have = updates.get(variable, current.get(variable, ""))
                    if have and have != value:
                        left.append(f"{name}: {variable} (settings env already holds another value)")
                        continue
                    updates[variable] = value
                    values[key] = f"${{{variable}}}"
                    redact.register(value)
                    moved.append(f"{name}: {variable}")
                    changed = True
        if not changed:
            return [], left
        result = SettingsWriter(self.ctx, target).apply({"env": updates})
        if getattr(result, "error", ""):
            log.warning("could not move literal credentials into %s: %s", target, result.error)
            return [], [*left, *[f"{m} (could not write {target})" for m in moved]]
        try:
            os.chmod(target, 0o600)
        except OSError:
            pass
        write_private(path, json.dumps(document, indent=2) + "\n")
        log.info("moved %d literal credential(s) out of %s into %s (env)", len(moved), path, target)
        return moved, left

    def persist_client_env(self, variable: str, value: str, why: str) -> str:
        """Put ``variable=value`` where Claude Code reads it at start-up; where it went, or "".

        ``mcp.registration.client_timeout.persist_to``: ``settings`` -- Claude
        Code's own ``settings.json`` ``env`` (the place a new terminal's Claude
        Code reads; this program's environment file is read by nobody unless
        the operator sources it), ``env_file`` -- that file only, or ``both``.
        """
        where = self.registration.section("client_timeout").str_("persist_to", "both").strip().lower()
        if self.ctx.dry_run or not variable:
            return ""
        places: list[str] = []
        if where in ("env_file", "both"):
            self.ctx.env.set(variable, value, passthrough=True)
            places.append("this program's environment file")
        from ..core.machine_env import MachineEnv

        if where in ("settings", "both") and MachineEnv(self.ctx).enabled:
            # Every place a new terminal's Claude Code reads, logged place by place.
            record = MachineEnv(self.ctx).apply(variable, value, why)
            places.extend(p.place for p in record.places)
            return ", ".join(places)
        if where in ("settings", "both"):
            from ..provision.settings_writer import SettingsWriter, settings_path

            path = settings_path(self.ctx)
            result = SettingsWriter(self.ctx, path).apply({"env": {variable: value}})
            if getattr(result, "error", ""):
                log.warning("could not set %s in %s: %s", variable, path, result.error)
            else:
                places.append(str(path))
                log.info("%s=%s set in %s (%s)", variable, value, path, why)
        return ", ".join(places)

    def _start_samples(self, registration: Registration, candidate: Candidate, took: float, limit: float) -> float:
        """The slowest of several starts of the entry as written, when one start came near Claude Code's wait.

        One measurement is not a start-up time for a server whose start does
        network work: gitlab.example.net's mcp-graphql downloads the whole schema
        on every start and took 12 s, 19 s, 71 s and 73 s on one machine. So
        when a start passes ``sample_from`` of the wait, the entry is started
        until ``samples`` starts are known, and the slowest counts.
        """
        config = self.registration.section("client_timeout")
        samples = config.int_("samples", 3)
        if samples <= 1 or took <= limit * config.float_("sample_from", 0.3) or candidate.kind != "stdio":
            return took
        from .probes import start_key

        counts = self.ctx.state.setdefault("mcp_start_counts", {})
        key = start_key(candidate.full_command(), candidate.env)
        while int(counts.get(key, 1)) < samples:
            counts[key] = int(counts.get(key, 1)) + 1
            shake = self.verifier.verify_as_written(registration.entry, registration.name, kept=candidate.launch_values)
            if shake is None:
                break
            took = max(took, float(shake.start_seconds or 0.0) if shake.ok else limit)
        return took

    def _within_client_timeout(self, registration: Registration, candidate: Candidate) -> None:
        """Does the server start inside Claude Code's start-up wait -- every time, not once?

        build-example-net's ``uvx mcp-openapi-proxy`` took 57 s on a machine that had
        not installed it yet; Claude Code gives up after 30 and showed it
        disconnected. gitlab-example-net's mcp-graphql passed its trial and took up
        to 73 s on later starts. The slowest of several starts
        (:meth:`_start_samples`) is compared with the client's wait; longer,
        the operator is told, and with ``raise`` on a wait that fits is put
        where Claude Code reads it (:meth:`persist_client_env`, never lowered).
        """
        config = self.registration.section("client_timeout")
        shake = candidate.handshake
        if not config.bool_("enabled", True) or shake is None or candidate.kind != "stdio":
            return
        limit = self.client_timeout_seconds()
        from .probes import start_key

        took = max(
            float(shake.start_seconds or 0.0),
            float(
                self.ctx.state.get("mcp_start_seconds", {}).get(start_key(candidate.full_command(), candidate.env), 0.0)
            ),
        )
        took = self._start_samples(registration, candidate, took, limit)
        if took <= limit * config.float_("margin", 0.8):
            return
        launch = " ".join(candidate.full_command()[:3])
        needed = int(took * config.float_("raise_factor", 1.5) + 0.999)
        variable = config.str_("variable", "MCP_TIMEOUT")
        registration.note(f"its slowest start took {took:.0f}s, near or past Claude Code's {limit:.0f}s start-up wait")
        if config.bool_("raise", True) and variable and needed > limit:
            where = self.persist_client_env(
                variable, str(needed * 1000), f"{registration.name} starts in up to {took:.0f}s"
            )
            if where:
                registration.note(f"{variable}={needed * 1000} set in {where}, so Claude Code waits long enough")
                registration.remediation.append(
                    f"{registration.name}: its slowest start took {took:.0f}s and Claude Code waited {limit:.0f}s; "
                    f"{variable}={needed * 1000} is now set in {where}. Restart Claude Code for it to apply."
                )
                return
        registration.remediation.append(
            f"{registration.name}: its slowest start (installing it included) took {took:.0f}s, and Claude Code waits "
            f"{limit:.0f}s before calling it disconnected. Start Claude Code with {variable}={needed * 1000}, or run "
            f"`{launch}` once on a new machine before starting Claude Code."
        )

    def _raise_client_wait_if_slow(self, registration: Registration) -> None:
        """Claude Code could not connect: when the server is slow to start, give it the wait it needs and ask again."""
        from . import claude_check

        candidate = registration.candidate
        if candidate is None or candidate.kind != "stdio" or self.ctx.dry_run:
            return
        before = self.client_timeout_seconds()
        self._within_client_timeout(registration, candidate)
        if self.client_timeout_seconds() <= before:
            return
        again = claude_check.connect_check(
            self.ctx, registration.name, registration.entry, waiting=not candidate.verified
        )
        registration.claude_connect = again.to_dict()
        if again.outcome != claude_check.FAILED:
            registration.claude_usage = again.to_dict()
            registration.remediation = [r for r in registration.remediation if "could not connect" not in r]
            registration.note(f"after the longer wait: {again.detail}")

    def _check_it_starts(self, registration: Registration, has_backup: bool = False) -> None:
        """Launch the finished entry and record whether it came up.

        The last gate, and the only one that tests the artefact rather than the
        thing it was derived from. Grounding asks whether the entry names its
        service; portability asks whether its paths exist; this asks the
        question the operator will ask -- *does it start?* -- of the exact text
        about to be written.

        Three entries in one run passed every earlier gate and died on
        start-up. Each had been proven as a candidate and then written with one
        extra unset variable, so nothing that ran had ever run what shipped.

        A failure refuses the entry (``mcp.registration.require_entry_starts``,
        on by default), and the remediation says what happened; with that off
        it is written anyway and reported, loudly. What must never happen is
        reporting it as working. ``has_backup`` says another proven candidate
        is about to be tried under this name, so nothing is said about an
        earlier entry being left in place -- it is about to be replaced.

        The stage asked this same question of the same candidate. When the
        entry it launched is the entry about to be written, its verdict is the
        answer and nothing is launched again.
        """
        if not self.config.bool_("registration.verify_as_written", True):
            return

        # One call, not three. Examining launches the entry; repairing launches
        # each attempt; asking again afterwards for a verdict launched a third
        # time. The doctor already knows the answer it arrived at, so the answer
        # is read from it -- every one of those launches is a subprocess and a
        # handshake against somebody's software.
        from .healing import EntryDoctor

        earlier = getattr(registration.candidate, "healing", None)
        if (
            earlier is not None
            and earlier.tested
            and earlier.ok
            and not earlier.applied
            and earlier.entry == registration.entry
        ):
            healing = earlier
        else:
            healing = EntryDoctor(self.ctx, self, self.verifier).examine(
                registration.candidate, registration.name, announce=False
            )
        registration.attempted_repairs = list(healing.attempts)
        if not healing.tested:
            # Nothing was launchable here -- a remote entry, a dry run. Not a
            # verdict, so none is recorded.
            return
        if healing.applied:
            registration.entry = healing.entry
            registration.repairs = list(healing.applied)
            registration.note(
                "the entry did not start and was repaired (" + ", ".join(healing.applied) + "), then proved again"
            )
        registration.start_check = Handshake(ok=healing.ok, transport=Transport.STDIO, error=healing.error)
        if healing.ok:
            return
        detail = (healing.error or "it exited without completing a handshake").strip()
        registration.note(f"the entry as written did not start: {detail}")

        if self.config.bool_("registration.require_entry_starts", True):
            # Refused, not reported. An entry that cannot start is not a
            # configuration -- it is a line that will fail in front of the
            # operator as "Connection closed", with no reason attached. Two
            # openQA entries shipped exactly like that, and the run called
            # itself PARTIAL rather than saying the thing had not worked.
            #
            # Refusing also reopens the lower tiers: an unregistrable candidate
            # must not settle its target, which is what lets tier 3 build a
            # bridge for a target whose tier-2 package will not run here.
            attempted = "; ".join(registration.attempted_repairs) or "none available"
            registration.refused = (
                f"it did not start when launched exactly as written: {detail} (repairs tried -- {attempted})"
            )
            registration.remediation.append(
                f"{registration.name} was NOT written: the entry does not start. {detail}. "
                f"Repairs attempted: {attempted}. The server answered during discovery, so this "
                "is about the entry rather than the service -- most often a variable it needs "
                "that the entry does not declare, since a client passes only what the entry "
                "names. Add it to the server's `env` and re-run phase 4, or set "
                "mcp.registration.require_entry_starts = false to write it anyway."
            )
            registration.remediation.extend(
                f"{registration.name}: {line}" for line in getattr(healing, "differences", [])
            )

            # Nothing was written -- but something of this name may already be
            # in the file from an earlier run. It is left alone, because this
            # run has nothing better to put there, and *that fact has to be
            # said*: silence would read as "the file is as this run left it",
            # and the operator would be looking at an entry nothing verified
            # today while believing otherwise.
            existing = None if has_backup else self._existing_entries().get(registration.name)
            if has_backup:
                self.ctx.console.bullet(
                    f"{registration.name}: not written -- the entry does not start; trying the "
                    "next candidate this run proved",
                    indent=4,
                )
            elif existing is not None:
                registration.superseded_nothing = existing
                registration.note(
                    f"an earlier {registration.name} entry is still in {self.display_path} and was left as it was"
                )
                registration.remediation.append(
                    f"{registration.name} already existed in {self.display_path} before this "
                    "run and has been left untouched -- this run had nothing better to replace "
                    "it with. It was NOT re-verified today, so if that server is failing, the "
                    "entry you are looking at is the old one and this run did not write it."
                )
                self.ctx.console.bullet(
                    f"{registration.name}: not written; the existing entry in "
                    f"{self.display_path} is left as it was and was not verified today",
                    indent=4,
                )
                log.warning(
                    "%s: refused a new entry and left the pre-existing one in place",
                    registration.name,
                )
            else:
                self.ctx.console.bullet(f"{registration.name}: not written -- the entry does not start", indent=4)
            return

        registration.remediation.append(
            f"{registration.name} is written to {self.display_path} but did not start when "
            f"launched exactly as written: {detail}. The server itself answered during "
            "discovery, so this is about the entry rather than the service -- check the "
            "variables in its `env` block against the product's own documentation, and see "
            "the note on this server in the briefing."
        )

    def publish(self, outcome: TargetOutcome) -> list[Registration]:
        """Record *and* register every server found for one target.

        Verified servers and ones that answered 401/403 are named in a single
        pass, so the two groups cannot collide over a name, and written to the
        same file: the difference between them is not where the entry goes but
        whether its credential slot has been filled, and that is said in the
        briefing and the verdict rather than by leaving the entry out.
        """
        registrations: list[Registration] = []
        if outcome.auth_plan:
            self.login_plans[outcome.target] = outcome.auth_plan
        # Which login this host accepted or refused, for the next run's choice.
        from . import login_history

        login_history.record(
            self.ctx,
            host_of(outcome.target) or outcome.target,
            {id(c): c for st in outcome.stages for c in st.candidates}.values(),
        )
        self.retrial_rewritten(outcome)
        existing = self._existing_entries()
        provisional = (
            # Never one already proven broken: it waits for nothing (see
            # StageOutcome.note_provisional).
            [c for c in outcome.provisional if c not in outcome.verified and not c.start_failure]
            if self.settings.bool_("mcp.discovery.auth.register_authenticated", True)
            else []
        )
        found = [*outcome.verified, *provisional]
        if not found and self.registration.bool_("never_empty_when_verified", True):
            # An address is never left with nothing while a server answered and
            # was verified here: smelt.example.net's agent-written bridge worked,
            # was "unable to be asked the job", and the address got no entry. It
            # is written, and the verdict says what it never proved.
            spare = [
                c for st in outcome.stages for c in st.candidates if c.verified and not c.start_failure and c.unsettled
            ]
            if spare:
                chosen = best_of(spare)
                chosen.add_note(
                    f"written although it settled nothing ({chosen.unsettled}): the address had no other "
                    "server, and this one answered and was verified"
                )
                provisional = [chosen]
                found = [chosen]
        # One server, one entry, in every mode: copies of one server -- the same
        # package or URL reaching the same hosts, launched with other flags --
        # are folded into the best of them (see same_server). The copies stay
        # proven candidates, so they remain start-up fallbacks below.
        from . import same_server

        found, folded = same_server.fold(found, self.settings)
        for copy_, kept in folded:
            log.info("%s: %s is a copy of %s -- one entry for one server", outcome.target, copy_.label, kept.label)
        provisional = [c for c in provisional if c in found]
        # What this run found answering for the target, so an entry naming one of
        # those hosts is judged on our own evidence rather than on spelling.
        known_hosts = endpoint_hosts(outcome)
        # Proven candidates that were not chosen, for when a chosen one turns
        # out not to start: the next takes its name and its place.
        backups = self.start_backups(outcome, found)
        fallbacks = max(0, self.registration.int_("start_fallbacks", 2))
        # Asked before the loop as well as after: an unproven candidate may not
        # take the name of an existing entry that answered and was not proved unusable.
        unusable = self._unusable_existing(outcome)

        queue = list(zip(found, self.name_all(found, outcome.target)))
        while queue:
            candidate, name = queue.pop(0)
            needs_keys: list[str] = []
            awaiting_note = ""
            self.address_of[name] = outcome.target
            self.prefer_installed(candidate)
            self.complete_slots(candidate)
            if candidate in provisional or not candidate.verified:
                needs_keys = self.prepare_provisional(candidate, name)
            self.own_files(candidate, name)
            entry = self.entry_for(candidate, name)
            # What the operator exports is what the entry's ${...} reads -- which
            # is not always the key the server reads (see _shared_name). Every
            # instruction below names the exported variable.
            needs_env = [
                self.variable_read_for(entry, key, candidate)
                for key in needs_keys
                if not self.env_contract.session(key)
            ]
            # Read by its script and never declared: needed all the same by one
            # that is not proved to work without it, unless already exported
            # (mcp.registration.name_undeclared_credentials).
            if self.registration.bool_("name_undeclared_credentials", True) and not candidate.verified:
                needs_env += [
                    name
                    for name in candidate.undeclared_credentials
                    if name not in needs_env and not os.environ.get(name)
                ]
            registration = Registration(
                name=name,
                candidate=candidate,
                entry=entry,
                scope=self.config.str_("register_scope", "local"),
                provisional=bool(needs_env),
                needs_env=needs_env,
                target=outcome.target,
            )
            if candidate.handshake is not None and candidate.handshake.credential_optional:
                optional = self.exported_variables(candidate)
                # Anonymous until one is given: what it does now is what anybody
                # may do. gitlab-example-net and bugzilla-opensuse-org were reported
                # as working, and every private project and restricted bug was
                # out of reach. Said as such, never as "passed" for the account.
                registration.anonymous_until = [v for v in optional if not self._is_set(v)]
                registration.note(
                    (
                        f"public answers only -- not logged in: set {', '.join(registration.anonymous_until)} "
                        "for what needs a login (optional)"
                    )
                    if registration.anonymous_until
                    else f"works without a login; {', '.join(optional)} optional"
                )
            refused_login = self.login_remedy(candidate, needs_env, outcome.target)
            alternative = self.alternative_hint(candidate, needs_env, outcome)
            if refused_login:
                # It was given a credential and its service refused it: the
                # remedy is a different credential or account, not "export a
                # value" -- the operator already did.
                awaiting_note = f"{name} {waits_for_credential(candidate)}. {refused_login}"
            elif needs_env:
                registration.credential_hint = self.credential_hint(candidate)
                # The shape of the value belongs in the same sentence as the
                # name of the variable. Told only the name, an operator whose
                # product does not issue anything called a token goes looking
                # for one -- see McpWriter.auth_profile.
                wanted = "; ".join(f"{variable}: {self.credential_hint(candidate, variable)}" for variable in needs_env)
                # Said only of an entry that is written: added at the end, once
                # every gate below has let it through. A refused one was being
                # described as "registered with an empty credential".
                awaiting_note = (
                    f"{name} {waits_for_credential(candidate)}, so it is registered with an empty "
                    f"credential. Export {wanted}. Then re-run phase 4, which trials the server "
                    "with the variable set and writes the entry it proved."
                )
                shake = candidate.handshake
                if shake is not None and shake.login == login_check.ACCEPTED and shake.login_source:
                    # The value the old entry carried works -- the operator only
                    # has to move it into the variable the entry now reads.
                    awaiting_note += (
                        f" The credential written in its existing entry was accepted by its service "
                        f"({shake.login_source}): that value is the one to export."
                    )
            elif candidate.secret_env:
                registration.remediation.append(
                    f"{name} reads {', '.join(self.exported_variables(candidate))} from the environment; "
                    "export the variable(s) in the shell you start Claude Code from."
                )
            if refused_login:
                registration.credential_hint = refused_login
            if alternative:
                # Nothing could be checked, and the target serves another API
                # this server does not speak: said now, not after a second run.
                registration.remediation.append(f"{name}: {alternative}")
                if awaiting_note:
                    awaiting_note += f" {alternative}"
            registration.grounding = self.grounding_of(candidate, outcome.target, known_hosts)
            if not registration.grounding.registrable:
                # An entry nobody can believe is worse than no entry: it reads
                # as though the address were covered, and the operator stops
                # looking. See :mod:`.grounding` for the three real examples.
                registration.refused = registration.grounding.reason
                registration.remediation.append(
                    f"{name} was not registered: {registration.grounding.reason}. An entry has to "
                    f"name the service it talks to -- as the URL's host, an argument, or an "
                    f"environment value such as the server's own base URL."
                )
                registrations.append(registration)
                continue

            shared = self._shared_claim(registration)
            if shared:
                # One endpoint that names none of them, vouched for only by a
                # claim, turning up for several unrelated addresses is not
                # evidence it serves each -- it is evidence the claim is wrong.
                # https://api.mcp.ai/p_examplep_administradores was registered for
                # six Example hosts and github.com that way. This one is refused,
                # and the ones already published for the others are withdrawn.
                others = sorted({r.target for r in shared})
                registration.refused = (
                    f"{candidate.label} was also claimed for {', '.join(others)}, an unrelated "
                    f"address -- and nothing in the entry or the server's answer names "
                    f"{outcome.target}, so the same claim for several services belongs to none "
                    "of them"
                )
                registration.remediation.append(
                    f"{name} was not registered: {registration.refused}. Set "
                    "mcp.registration.refuse_shared_claims = false only if one endpoint really "
                    "does serve all of these addresses."
                )
                registrations.append(registration)
                for other in shared:
                    self._withdraw(other, outcome.target)
                continue

            rejected = self.claude_rejects(name, registration.entry)
            if rejected:
                # Claude Code would skip it at load: written, it reads as a
                # server on the list and is none. The entry already here stays.
                registration.refused = rejected
                registration.remediation.append(
                    f"{name} was not registered: {rejected}. The entry already in place, if any, is left as it was."
                )
                registrations.append(registration)
                continue

            refusal = self.trust_refusal(candidate, registration.entry)
            if refusal:
                registration.refused = refusal
                registration.remediation.append(f"{name} was not registered: {refusal}.")
                registrations.append(registration)
                continue

            registration.portability = self.portability_of(candidate)
            if not registration.portability.registrable:
                # Better nothing than an entry that cannot start. It would fail
                # with ENOENT, which says nothing about why, and .mcp.json is a
                # file people copy -- a broken entry travels with it.
                registration.refused = registration.portability.reason
                registration.remediation.append(
                    f"{name} was not registered: {registration.portability.reason}. Prefer a "
                    "launcher that fetches what it needs at start-up -- `npx -y <package>`, "
                    "`uvx <package>`, `uv tool run <package>` -- which works here and on any "
                    "machine this file is copied to."
                )
                registrations.append(registration)
                continue

            # Two copies of one name: the one that proved more is the one
            # written, never a refusal that leaves the file with the lesser
            # copy -- gitlab-example-net's file copy was refused for its CLI
            # name-mate, and the address was reported "not proved today".
            best = (
                self._existing_candidate(outcome, name)
                if self.registration.bool_("best_copy_speaks_for_name", True)
                else None
            )
            if (
                best is not None
                and best is not candidate
                and best.verified
                and contract.score(best) > contract.score(candidate)
                and self.portability_of(best).registrable
            ):
                log.info(
                    "%s: %s is written from its better proven copy (%s, usage score %d against %d)",
                    outcome.target,
                    name,
                    best.label,
                    contract.score(best),
                    contract.score(candidate),
                )
                candidate = best
                registration.candidate = best
                registration.entry = self.entry_for(best, name)
                registration.portability = self.portability_of(best)
                # The copy written is the copy counted (TargetOutcome.succeeded).
                if best not in outcome.verified:
                    outcome.verified.append(best)
            held = self._holds_its_place(registration, outcome, unusable)
            if held and not self._keepable(outcome, name):
                # What would keep its place cannot be written to the file (a
                # launch that will not travel): the replacement goes in instead,
                # so the address never ends with no entry.
                log.info(
                    "%s: the existing %s cannot be recorded in %s, so it does not hold its place: %s",
                    outcome.target,
                    name,
                    self.display_path,
                    held,
                )
                held = ""
            if held:
                # The choice to keep it is the condition for writing it: it is
                # recorded in the file too (_record_kept), whatever it proved.
                # github-com kept its place because it answered, and was never
                # written because it was neither verified nor connected.
                self._held_names.setdefault(outcome.target, set()).add(name)
                registration.refused = held
                registration.held_back = True
                registration.remediation.append(
                    f"{candidate.label} was not written over {name}: {held}. Prove it -- export its "
                    "credential and re-run phase 4 -- or set mcp.registration.keep_answering_existing "
                    "= false to let an unproven candidate replace it."
                )
                registrations.append(registration)
                continue

            self._check_it_starts(registration, has_backup=bool(backups and fallbacks))
            if registration.refused and backups and fallbacks:
                # Refused because it does not start. One failure must not leave
                # the address with nothing while another proven candidate is in
                # hand -- confluence.example.com ended empty that way.
                fallbacks -= 1
                replacement = backups.pop(0)
                registration.replaced_by = replacement.label
                registration.note(f"{replacement.label} was tried in its place")
                registration.remediation.append(
                    f"{replacement.label}, the next candidate this run proved, was tried in its place."
                )
                log.info(
                    "%s: %s does not start; trying %s in its place",
                    outcome.target,
                    name,
                    replacement.label,
                )
                group = outcome.verified if replacement.verified else outcome.provisional
                if replacement not in group:
                    group.append(replacement)
                queue.insert(0, (replacement, name))
                registrations.append(registration)
                continue
            if awaiting_note and not registration.refused:
                registration.remediation.insert(0, awaiting_note)
            if not registration.refused:
                self._within_client_timeout(registration, candidate)
            shake = candidate.handshake
            if (
                not registration.refused
                and shake is not None
                and shake.schema_problems
                and self.settings.str_("mcp.verification.schema_problems", "refuse").strip().lower() == "warn"
            ):
                # Registered on request (mcp.verification.schema_problems = warn),
                # and said as loudly as a refusal would have been.
                from .probes import describe_schema_problems

                warning = describe_schema_problems(shake.schema_problems)
                registration.note(f"WARNING: {warning}")
                registration.remediation.insert(
                    0,
                    f"{name}: {warning}. Avoid those tools, or remove the server, until its maintainers fix the names.",
                )
            if not registration.refused and shake is not None and shake.shape_problems:
                # A value that cannot work in its slot -- a bare token where a
                # header list is read. The remedy carries the exact form.
                for problem in reversed(shake.shape_problems):
                    registration.remediation.insert(0, f"{name}: {problem['remedy']}")
                registration.credential_hint = "; ".join(p["remedy"] for p in shake.shape_problems)
            if not registration.refused and shake is not None and shake.misconfigured:
                # Some tools work and at least one rejects how a setting is
                # written, and no other form cleared it: registered, and said.
                registration.remediation.insert(
                    0,
                    f"{name}: "
                    + login_check.remedy(
                        login_check.MISCONFIGURED,
                        host=self.service_host(candidate),
                        variables=sorted(candidate.env),
                        detail=shake.misconfigured,
                        settings=self.settings,
                    ),
                )
            registration.duplicates = self._duplicates_of(registration, existing)
            for duplicate in registration.duplicates:
                registration.remediation.append(
                    f"{duplicate['name']} in {self.display_path} already points at "
                    f"{duplicate['target']}. {duplicate['why']} Two entries for one service means "
                    "Claude Code loads the same tools twice under different names; keep one: "
                    f"`{self.remove_by_hand(duplicate['name'])}`, or delete it from "
                    f"{self.display_path} if it is only there."
                )
            registrations.append(registration)
        outcome.duplicates = [d for r in registrations for d in r.duplicates]

        # A refused entry is refused everywhere: not into the file, not into the
        # CLI. This used to be left implicit, and it was not true -- `_write_json`
        # looped over every registration it was handed, so an entry the gates had
        # just rejected was written out anyway and then registered, and the
        # reason was overwritten by whatever `_register` had to say. Both gates
        # announced decisions they never carried out.
        written = [r for r in registrations if not r.refused]
        unusable = self._unusable_existing(outcome)
        for registration in written:
            previous, why = unusable.get(registration.name, (None, ""))
            if previous is not None and previous is not registration.candidate:
                registration.replaces_unusable = registration.name
                registration.replaced_tools = list(previous.handshake.tools) if previous.handshake else []
                registration.note(f"replaces the existing {registration.name}, which {why}")

        # The file first: it is the deliverable, and it does not need the CLI to
        # exist. Registering is what makes the server usable without moving the
        # file anywhere, and it is allowed to fail on a machine without `claude`.
        if written:
            self._write_json(written)
            self._trust_for_claude_code(written)
        for registration in written:
            self._register(registration)
            self._keep_held_credentials(registration)
            self._keep_supplied_values(registration, outcome.target)
            self._check_through_claude(registration, outcome)
            if registration.claude_usage.get("outcome") == "failed" and registration.candidate is not None:
                # Claude Code itself could not use it, asked twice: written still,
                # never counted as working (TargetOutcome.consumer_failed).
                identity = registration.candidate.identity()
                if identity not in outcome.consumer_failed:
                    outcome.consumer_failed.append(identity)
            if registration.claude_usage.get("outcome") not in ("failed", "missing_variable"):
                known_good.record(
                    self.ctx, outcome.target, registration.name, registration.entry, registration.candidate
                )
            if known_good.never_proved(registration.candidate):
                registration.remediation.append(
                    f"{registration.name} never proved its login: every call that succeeded is one anybody can make "
                    "without a credential. It is not counted as working."
                )
        self._keep_alternatives(outcome, written)
        self._look_back(outcome, written)
        for registration in written:
            self._operator_steps(registration, outcome)
        if outcome.auth_plan:
            from .login_methods import describe

            lines = describe(outcome.auth_plan, host_of(outcome.target) or outcome.target)
            if lines:
                self.login_advice.append({"target": outcome.target, "lines": lines})

        outcome.registered = [r.name for r in written if r.registered or r.written]
        if not written and (registrations or outcome.answered_existing):
            # Nothing written -- refused, or nothing found -- yet the machine may
            # still hold an entry that answered; it is said, and recorded in the
            # file when it works (_record_kept).
            self._keep_existing(outcome, registrations)
        self.published.extend(registrations)
        return registrations

    def _report_shadowing(self, registration: Registration) -> None:
        """Say so when the CLI holds a *different* entry under this name -- it wins over the file.

        A CLI (local-scope) registration takes precedence over the project's
        ``.mcp.json``. build.example.net's corrected entry was written to the file
        and the CLI kept loading the broken one, with nothing in the report to
        say the two differed.
        """
        from .existing import ExistingRegistrations  # imports this module

        existing = ExistingRegistrations.for_run(self.ctx)
        theirs = existing.cli_entry(registration.name)
        if not theirs or _same_launch(theirs, registration.entry):
            return
        candidate = registration.candidate
        if candidate is not None and not candidate.verified and existing.cli_connected(registration.name):
            # Nothing proved the written entry, and the CLI's copy -- the one
            # Claude Code loads -- is connected: telling the operator to remove
            # it trades a server that answers for one that may not.
            registration.remediation.append(
                f"The claude CLI holds a different {registration.name}, which Claude Code loads in place of the "
                f"one written to {self.display_path} and shows connected. The written entry is not proved yet "
                "(it waits for a credential), so keep the CLI's until the written one is proved -- re-run phase 4 "
                f"once its credential is set, then `{self.remove_by_hand(registration.name)}` if it still differs."
            )
            return
        registration.remediation.append(
            f"The claude CLI holds a different {registration.name} from the one written to "
            f"{self.display_path}, and the CLI's takes precedence. Remove it -- "
            f"`{self.remove_by_hand(registration.name)}` -- so the entry this run proved is the one that loads."
        )

    def _unusable_existing(self, outcome: TargetOutcome) -> dict[str, tuple[Candidate, str]]:
        """Existing registrations this run proved cannot work, by name -- with why, in words.

        Four ways: it does not start (build.example.net's CLI copy died in
        ``ToolsManager.initialize``, and "already known to the CLI" left it
        loading in place of the corrected entry); its entry was discarded by
        the start gate; its account has no API access; or its access stayed
        unproved -- every call empty -- while a bridge for the target's other
        API proved access and took its place (sd.example.com's mcp-atlassian
        answered ``[]`` to everything, with a working token and with a dead
        one alike; on a machine where it lives in the claude CLI, the CLI copy
        would have gone on loading in place of the bridge). Waiting for a
        credential is not one of them -- that one may work once exported.
        """
        from .existing import ExistingRegistrations  # imports this module

        existing = ExistingRegistrations.for_run(self.ctx)
        found: dict[str, tuple[Candidate, str]] = {}
        proven = next((c for c in [*outcome.verified, *outcome.provisional] if c.api_kind and proves_access(c)), None)
        pool = [c for stage in outcome.stages for c in stage.candidates]
        pool += [c for c in outcome.unstartable if all(c is not p for p in pool)]
        for candidate in pool:
            if existing.registration_for(candidate) is None:
                continue
            shake = candidate.handshake
            why = ""
            if (
                shake is not None
                and shake.login_reason == login_check.ACCOUNT_NO_ACCESS
                and not any(proves_access(c) for c in [*outcome.verified, *outcome.provisional])
            ):
                # The account may not use the product: no other server does
                # better with it, so nothing replaces the entry that carries it.
                continue
            if shake is not None and shake.login_refused and shake.login == login_check.NO_ACCESS:
                why = f"has no API access for its account ({shake.login_detail})"
            elif shake is not None and contract.verdict(shake.contract)[0] is False:
                # It started and answered -- and failed what it is for.
                why = f"failed what it is used for ({contract.verdict(shake.contract)[1]})"
            elif candidate.start_failure:
                why = f"does not start ({candidate.start_failure})"
            elif (
                shake is not None
                and not shake.ok
                and not shake.credential_pending
                and not shake.requires_auth
                and not (
                    self.verifier.looks_transient(shake)
                    and (
                        existing.cli_connected(candidate.name)
                        or candidate.worked_before
                        or self.verifier.answered_try_later(shake)
                    )
                )
            ):
                # A timeout alone never takes away an entry Claude Code reports
                # connected: slow is not broken (see CandidateVerifier._retry_slow_start).
                why = f"does not start ({self.verifier.excerpt(shake.error) or 'it exited'})"
            elif (
                proven is not None
                and shake is not None
                and shake.login in login_check.UNPROVED
                and not candidate.api_kind
                and self.settings.bool_("mcp.registration.replace_unproved_existing", True)
            ):
                why = (
                    f"never proved access ({shake.login_detail or login_check.in_words(shake.login)}), while "
                    f"{proven.label} proved it"
                )
            if why:
                found[candidate.name] = (candidate, why)
        return found

    def _is_existing_server(self, registration: Registration) -> bool:
        """Is ``registration`` the existing entry of its name -- that entry itself, or a copy of the same server?

        A copy is the same server launched another way (:mod:`.same_server`):
        the agent's ``uvx mcp-atlassian@0.23.1`` and the CLI's ``uvx
        mcp-atlassian`` for confluence.example.com. A rule that keeps an existing
        entry in place must never hold it against itself -- confluence-example-com
        was "held" that way and nothing was written. A copy that lacks the
        existing entry's credential slots takes them.
        """
        from . import same_server
        from .existing import ExistingRegistrations  # imports this module

        name, candidate = registration.name, registration.candidate
        if candidate.existing_name == name:
            return True
        existing = ExistingRegistrations.for_run(self.ctx)
        entry = self._existing_entries().get(name)
        held = next((r for r in existing.registrations() if r.name == name), None)
        entry = entry or (held.entry if held else None)
        if not entry:
            return False
        if _same_launch(entry, registration.entry):
            return True
        if held is None:
            return False
        theirs = existing.candidate(held, existing.tier_of(held.entry), record_origin=False)
        if same_server.key(theirs, self.settings) != same_server.key(candidate, self.settings):
            return False
        if same_server.inherit_credential(candidate, theirs, self.settings):
            registration.entry = self.entry_for(candidate, name)
        return True

    def _holds_its_place(self, registration: Registration, outcome: TargetOutcome, unusable: Mapping[str, Any]) -> str:
        """Why the existing entry of ``registration``'s name stays, in words; "" when it may be replaced.

        It stays when it answered during this run (``outcome.answered_existing``),
        nothing proved it unusable, it is not the same launch, and the candidate
        is unproven: not verified and its login never accepted. smelt.example.net's
        working ``mcp-graphql`` entry was overwritten by a bridge that only
        waited for a token -- built for an API smelt does not serve.
        """
        if not self.registration.bool_("keep_answering_existing", True):
            return ""
        name = registration.name
        candidate = registration.candidate
        shake = candidate.handshake
        # A place is kept by proof, never by status. An existing entry that
        # proved nothing this run -- no login accepted, no task done -- holds
        # it only while nothing known ranks above it (types.evidence_key):
        # being registered, answering, or Claude Code calling it "connected"
        # is no evidence. build-opensuse-org's proxy, written by an earlier
        # version, "connected" with its tools unlisted, kept its place over
        # the bridge that logs in the way the address asks.
        # (mcp.registration.keeping_needs_proof)
        from .types import evidence_beats

        # Both ranked by the same evidence before they are compared: an existing
        # copy nobody marked carried the neutral key, "nothing to supply".
        unmarked = [
            c
            for c in (self._existing_candidate(outcome, name), candidate)
            if c is not None and not getattr(c, "evidence", None) and c.needs_credential
        ]
        if unmarked:
            from . import evidence as _evidence

            _evidence.mark(
                self.ctx, unmarked, (urlsplit(outcome.target).hostname or "").lower(), outcome.auth_plan or {}
            )
        if self.registration.bool_("keeping_needs_proof", True):
            incumbent = self._existing_candidate(outcome, name)
            if (
                incumbent is not None
                and incumbent is not candidate
                and contract.score(incumbent) == 0
                and evidence_beats(candidate, incumbent)
            ):
                log.info(
                    "%s: the existing %s proved nothing this run and ranks below %s on what is known; "
                    "it does not hold its place",
                    outcome.target,
                    name,
                    candidate.label,
                )
                return ""
        # An entry Claude Code itself connects to is replaced only by a server
        # that proved it does the job -- an accepted login or a passed usage
        # task -- never by one that merely answered. build-example-net's connected
        # client bridge was replaced by an openapi-proxy that only "worked" on
        # a public /about.
        from .types import proof_rank

        previous_c = self._existing_candidate(outcome, name)
        if (
            previous_c is not None
            and previous_c is not candidate
            and self.registration.bool_("connected_is_not_working", True)
            and candidate.api_kind
            and not previous_c.api_kind
            and contract.score(previous_c) == 0
            and proof_rank(previous_c) >= proof_rank(candidate)
        ):
            # Neither proved anything, and the replacement is built for the API
            # this host was seen to serve: being connected in Claude Code holds
            # nothing by itself. sd.example.com's mcp-atlassian -- whose calls a
            # customer account is sent to the portal for -- was kept over the
            # bridge for its service-desk API because Claude Code connected to it.
            return ""
        # "Claude Code connects to it" holds no place: a status, not proof
        # (mcp.registration.connected_holds_place, off). gitlab.example.net's
        # bridge was refused for an old claude CLI copy that "connected" before
        # the run began -- a copy this run had itself discarded, since it named
        # a file not on the machine -- and the address ended with no entry in
        # .mcp.json. An existing entry keeps its place by what this run proved
        # of it, or by ranking above the candidate on what is known (above),
        # and only one judged this run can: nothing here holds a place for an
        # entry no copy of which was tried.
        if (
            self.registration.bool_("connected_holds_place", False)
            and name in outcome.connected_existing
            and name not in unusable
        ):
            if proof_rank(candidate) != 0 and contract.score(candidate) == 0:
                if not self._is_existing_server(registration):
                    return (
                        f"Claude Code connects to the existing {name}, and {candidate.label} proved less "
                        "(no accepted login, no usage task passed)"
                    )
        incumbent, ineligible = self._eligible_incumbent(registration, outcome, unusable)
        if ineligible:
            log.info(
                "%s: the existing %s holds no place over %s: %s", outcome.target, name, candidate.label, ineligible
            )
        if (
            incumbent is not None
            and self.registration.bool_("keeping_needs_proof", True)
            and contract.score(incumbent) == 0
            and self._ranks_above(incumbent, candidate)
        ):
            # Neither did any of the job: it keeps its place only by ranking
            # strictly above on what is known -- the comparison every chooser
            # shares -- once the values it supplies count for both.
            return (
                f"the existing {name} ranks above {candidate.label} on what is known (proof, history, "
                "built for the API, worked before, the login asked for, values to supply)"
            )
        # Shown short of its job is not shown worse than its replacement: an
        # existing entry that still passed part of the contract stays over one
        # that passed less and proved no login. build-example-net's openapi-proxy
        # still searched packages; it was replaced by a server that could do
        # nothing until a variable nobody exports was set.
        if name in unusable and self.registration.bool_("keep_more_proven_existing", True):
            from .types import proof_rank

            previous_u, why_u = unusable[name]
            shake_u = previous_u.handshake if previous_u is not None else None
            broken = (
                previous_u is None
                or bool(previous_u.start_failure)
                or (shake_u is not None and shake_u.login == login_check.NO_ACCESS)
            )
            theirs, mine = (contract.score(previous_u) if previous_u is not None else 0), contract.score(candidate)
            if previous_u is not candidate and not broken and theirs > mine and proof_rank(candidate) != 0:
                return (
                    f"the existing {name} {why_u}, yet it still did more than {candidate.label} "
                    f"(usage score {theirs} against {mine}); a replacement has to prove more than what it replaces"
                )
        # The usage contract decides first: what passed more of it stays.
        previous = self._existing_candidate(outcome, name)
        if previous is not None and previous is not candidate and name not in unusable:
            theirs, mine = contract.score(previous), contract.score(candidate)
            if theirs > mine:
                return (
                    f"the existing {name} passed more of what it is used for than {candidate.label} "
                    f"(usage score {theirs} against {mine})"
                )
            if mine > theirs and theirs == 0 and previous.handshake and previous.handshake.contract:
                return ""
        if candidate.verified or (shake is not None and shake.login == login_check.ACCEPTED):
            return ""
        if name not in outcome.answered_existing or name in unusable:
            return ""
        if not self._keepable(outcome, name) or self._eligible_incumbent(registration, outcome, unusable)[1]:
            # Answered earlier in the run, yet no copy of it may keep the place:
            # discarded or never judged (gitlab.example.net's CLI copy named a file
            # not on the machine), unusable, or the very server being written.
            return ""
        if (
            candidate.api_kind
            and self.settings.bool_("mcp.discovery.auth.prefer_detected_api", True)
            and previous is not None
            and not previous.api_kind
            and not previous.verified
            and not proves_access(previous)
        ):
            # Neither proved access; the replacement is built for the API this
            # host was seen to serve and the existing entry is not (see
            # TargetOutcome.absorb) -- answering proves no more than that.
            return ""
        from .existing import PROJECT_FILE, ExistingRegistrations  # imports this module

        source = outcome.answered_existing[name]
        theirs = (
            self._existing_entries().get(name)
            if source == PROJECT_FILE
            else ExistingRegistrations.for_run(self.ctx).cli_entry(name)
        )
        if (theirs and _same_launch(theirs, registration.entry)) or self._is_existing_server(registration):
            return ""
        where = self.display_path if source == PROJECT_FILE else "the claude CLI"
        if shake is not None and shake.credential_pending:
            unproven = f"it is only waiting for {', '.join(shake.credential_pending)}"
        else:
            unproven = f"nothing proved it works ({login_check.in_words(shake.login) if shake and shake.login else 'no call was accepted'})"
        log.info("%s: %s keeps its place; %s is not proven (%s)", outcome.target, name, candidate.label, unproven)
        return (
            f"the existing {name} in {where} answered during this run and nothing proved it unusable, while {unproven}"
        )

    def _operator_steps(self, registration: Registration, outcome: TargetOutcome) -> None:
        """The steps only the operator can take for an entry written today, each with how to confirm it.

        Everything the program can do it did: the entry is written for the
        route that will work. What is left is a step no program can take --
        exporting a value Claude Code must see, logging a client in, an account
        to enable -- and each ends with the recheck that proves it in seconds.
        """
        host = host_of(outcome.target) or outcome.target
        recheck = self.registration.str_("recheck_command", "initialize-claude-ai run --phases 4 --mcp-recheck {host}")
        confirm = recheck.replace("{host}", host)
        shake = registration.candidate.handshake
        steps: list[str] = []
        unoffered = self._unoffered_login(registration.candidate, outcome)
        if unoffered and registration.needs_env:
            # No value for these slots can make it log in: never ask for one as
            # though it could. build-opensuse-org's report said "export
            # BUILD_OPENSUSE_ORG_EXTRA_HEADERS"; the operator put a Bearer token
            # there, for an API that takes Basic. (mcp.registration.say_unoffered_login)
            steps.append(unoffered)
        elif registration.needs_env:
            steps.append(
                f"export {', '.join(registration.needs_env)} in the shell you start Claude Code from"
                + (f" ({registration.credential_hint})" if registration.credential_hint else "")
                + ", then restart Claude Code"
            )
        usage = registration.claude_usage or {}
        if usage.get("outcome") == "missing_variable" and not registration.needs_env:
            steps.append(str(usage.get("detail")))
        if shake is not None and shake.login_refused:
            steps.append(self.login_remedy(registration.candidate, registration.needs_env, outcome.target))
        for step in steps:
            if step:
                self.operator_steps.append(
                    {"target": outcome.target, "name": registration.name, "step": step, "confirm": confirm}
                )

    def _unoffered_login(self, candidate: Candidate, outcome: TargetOutcome) -> str:
        """What to say when ``candidate`` sends a kind of login its address offers nowhere; "" otherwise."""
        if candidate is None or not self.registration.bool_("say_unoffered_login", True):
            return ""
        from . import evidence

        plan = self.login_plans.get(outcome.target) or outcome.auth_plan or {}
        host = host_of(outcome.target) or outcome.target
        try:
            found = evidence.key(self.ctx, candidate, host, plan)
        except Exception:  # noqa: BLE001 - a remark must never cost the entry
            return ""
        if len(found) < 2 or found[1] != 3:
            return ""
        scheme = evidence.scheme_of(candidate, self.settings) or "its"
        offered = sorted(
            {
                f"{o.get('scheme')} at {o.get('host')}"
                for o in plan.get("offered") or []
                if isinstance(o, Mapping) and o.get("scheme")
            }
        )
        return (
            f"{candidate.label} sends a {scheme} login, which {host} offers nowhere (it asks for "
            f"{', '.join(offered) or 'another kind'}): no value for its slots can make it log in. "
            f"Re-run phase 4 for {host} -- a server that logs in the way it asks is preferred -- rather than "
            "exporting a value for this one"
        )

    def _look_back(self, outcome: TargetOutcome, written: Sequence[Registration]) -> None:
        """When an address has an entry that last worked and nothing written now did the job, say what changed.

        build.opensuse.org's osc-mcp worked on Oct 14; the next day the
        address had a bridge that failed every call. The differences --
        launch, package and version, hosts, variables, settings -- are what
        is really missing, and the entry that worked is named so it can be
        restored. Never a value: names, and plain settings only.
        """
        if not known_good.enabled(self.ctx):
            return
        row = known_good.last(self.ctx, outcome.target)
        if row is None or any(known_good.proved(r.candidate)[0] for r in written):
            return
        name = str(row.get("name") or "")
        current = next((r.entry for r in written if r.name == name), None) or self._existing_entries().get(name)
        if current is None:
            from .existing import ExistingRegistrations  # imports this module

            current = ExistingRegistrations.for_run(self.ctx).cli_entry(name) or None
        changes = known_good.differences(row.get("entry") or {}, current or {})
        self.lookbacks.append(
            {
                "target": outcome.target,
                "name": name,
                "recorded": str(row.get("recorded") or ""),
                "tasks": list(row.get("tasks") or []),
                "changes": changes or ["nothing in the entry itself"],
            }
        )

    def _check_through_claude(self, registration: Registration, outcome: TargetOutcome | None = None) -> None:
        """The last check: Claude Code itself uses the entry (:mod:`.claude_check`).

        A missing variable or a failure is said with its remedy, and a check
        that could not run is said to be inconclusive -- it never takes away an
        entry the other checks proved. Nothing else changes here: the entry is
        written already, and a re-run (or ``--recheck``) acts on the result.
        """
        from . import claude_check

        if not claude_check.enabled(self.settings) or self.ctx.dry_run:
            return
        candidate = registration.candidate
        connected = claude_check.connect_check(
            self.ctx,
            registration.name,
            registration.entry,
            waiting=bool(registration.provisional or (candidate is not None and not candidate.verified)),
        )
        if (
            connected.outcome == claude_check.FAILED
            and connected.reason_class == "trust"
            and registration.entry.get("url")
            and not registration.entry.get("command")
        ):
            # Claude Code's own verdict: it cannot verify the certificate. The
            # trust goes where it reads it, and it is asked again.
            from . import consumer_trust

            if consumer_trust.ensure(self.ctx, {registration.name: registration.entry}, force=True) is not None:
                connected = claude_check.connect_check(
                    self.ctx,
                    registration.name,
                    registration.entry,
                    waiting=bool(registration.provisional or (candidate is not None and not candidate.verified)),
                )
        registration.claude_connect = connected.to_dict()
        log.info("%s: Claude Code connection -- %s: %s", registration.name, connected.outcome, connected.detail)
        if connected.outcome == claude_check.FAILED and connected.reason_class == "not_mcp":
            # Claude Code reached it and what answered is not MCP: no credential
            # changes that. The entry goes, and the address is handed over.
            self._withdraw_not_mcp(registration, connected.reason, outcome)
        if connected.outcome == claude_check.FAILED:
            # Not reached by Claude Code at all: no tool call through it can say more.
            registration.claude_usage = connected.to_dict()
            registration.remediation.insert(
                0,
                (
                    f"{registration.name}: {connected.detail}. The entry was written; Claude Code will show it "
                    "disconnected until what the detail names is fixed."
                ),
            )
            self._raise_client_wait_if_slow(registration)
            return
        outcome = claude_check.check(self.ctx, registration.name, registration.entry, registration.candidate)
        if outcome.outcome == claude_check.FAILED and self._restore_runtime_tuning(registration, outcome.detail):
            outcome = claude_check.check(self.ctx, registration.name, registration.entry, registration.candidate)
            if outcome.outcome == claude_check.FAILED:
                self._undo_runtime_restore(registration)
        registration.claude_usage = outcome.to_dict()
        log.info("%s: Claude Code check -- %s: %s", registration.name, outcome.outcome, outcome.detail)
        if outcome.outcome == claude_check.MISSING_VARIABLE:
            registration.remediation.insert(0, f"{registration.name}: {outcome.detail}.")
        elif outcome.outcome == claude_check.FAILED:
            registration.remediation.insert(
                0,
                (
                    f"{registration.name}: when Claude Code itself used it, {outcome.tool} failed ({outcome.detail}). "
                    "The entry was written; fix what the detail names and re-run phase 4."
                ),
            )
        elif outcome.outcome == claude_check.COULD_NOT_RUN:
            if connected.outcome == claude_check.PASSED:
                registration.note(f"{connected.detail}; no tool could be asked through it ({outcome.detail})")
            else:
                registration.note(f"the Claude Code check could not run ({outcome.detail}); inconclusive")

    def _withdraw_not_mcp(self, registration: Registration, reason: str, outcome: TargetOutcome | None = None) -> None:
        """Take out an entry Claude Code found is not an MCP server, and let the address be handed over.

        confluence.example.com's /mcp -- a login redirect taken for an endpoint --
        was written "waiting for a credential"; Claude Code said "Unexpected
        content type: text/html", the run called it "a server failure" and kept
        it, while mcp-atlassian, which works, waited as an alternative. The
        entry is removed again (the one it replaced comes back), its URL is
        ruled out of the endpoint record so it closes no tier, and the address
        is counted as having nothing that works -- so it is handed over.
        (``mcp.registration.claude_check.withdraw_not_mcp``.)
        """
        if not self.config.section("registration.claude_check").bool_("withdraw_not_mcp", True) or self.ctx.dry_run:
            return
        why = f"Claude Code reached it and it is not an MCP server ({reason or 'not MCP'})"
        candidate = registration.candidate
        url = str(registration.entry.get("url") or "")
        record = getattr(outcome, "due_diligence", None) if outcome is not None else None
        if url and record is not None and hasattr(record, "retract"):
            record.retract(url, why)
        if candidate is not None and candidate.handshake is not None:
            candidate.handshake.requires_auth = False
            candidate.handshake.endpoint_proof = ""
            candidate.handshake.auth_evidence.clear()
            candidate.add_note(why)
        path = self.json_path
        try:
            document = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
        except (OSError, ValueError):
            document = {}
        servers = dict(document.get("mcpServers") or {})
        if servers.get(registration.name) == registration.entry:
            if registration.had_previous and registration.previous_entry is not None:
                servers[registration.name] = registration.previous_entry
            else:
                servers.pop(registration.name, None)
            document["mcpServers"] = dict(sorted(servers.items()))
            try:
                self._atomic_write(path, json.dumps(document, indent=2) + "\n")
            except OSError as exc:
                log.warning("could not take %s out of %s: %s", registration.name, path, exc)
                return
        if registration.registered:
            # The copy this run registered with the claude CLI goes too: Claude Code loads it first.
            claude = self.ctx.tool("claude", config_key="paths.claude")
            if claude and self._remove_from_cli(claude, registration.name, f"withdraw {registration.name}"):
                registration.registered = False
                self._known = None
        registration.written = False
        registration.refused = why
        if outcome is not None and candidate is not None and candidate.identity() not in outcome.consumer_failed:
            outcome.consumer_failed.append(candidate.identity())
        registration.remediation.insert(0, f"{registration.name} was taken out of {self.display_path} again: {why}.")
        log.info("%s: withdrawn -- %s", registration.name, why)

    def _trust_for_claude_code(self, written: Sequence[Registration]) -> None:
        """Give Claude Code the trust the URL entries just written need, before anything asks it to connect.

        A URL entry is opened by Claude Code itself: its trust lives in Claude
        Code's environment, never in the entry (:mod:`.consumer_trust`).
        """
        from . import consumer_trust

        if self.ctx.dry_run or not consumer_trust.config(self.settings).bool_("proactive", True):
            return
        entries = {r.name: r.entry for r in written if r.entry.get("url") and not r.entry.get("command")}
        try:
            consumer_trust.ensure(self.ctx, entries)
        except Exception as exc:  # noqa: BLE001 - the connection check still asks Claude Code itself
            log.warning("could not give Claude Code the trust its URL entries need: %s", exc)

    def _restore_runtime_tuning(self, registration: Registration, detail: str) -> bool:
        """Put back, per machine, a runtime setting dropped earlier, when Claude Code's use failed the way a network does.

        smelt-example-net's NODE_OPTIONS (a long connection wait) was dropped after
        a launch that did its job without it; Claude Code's own check moments
        later failed every call with "fetch failed". A setting dropped on
        proof is taken back on proof: the entry reads it through this
        machine's variable (:meth:`machine_settings`) -- never a literal in the
        file that travels -- the value goes into this machine's Claude Code
        settings, and the check is asked again. (``runtime_tuning.restore_on``.)
        """
        from .verifier import NETWORK_FAILURE_PATTERNS

        candidate = registration.candidate
        config = self.settings.section("mcp.verification.runtime_tuning")
        if candidate is None or not candidate.dropped_tuning or self.ctx.dry_run or not config.bool_("restore", True):
            return False
        patterns = [p for p in config.str_list("restore_on", list(NETWORK_FAILURE_PATTERNS)) if p]
        if not any(re.search(p, str(detail or ""), re.IGNORECASE) for p in patterns):
            return False
        entry = copy.deepcopy(dict(registration.entry))
        env = dict(entry.get("env") or {})
        env.update(candidate.dropped_tuning)
        self.machine_settings(candidate, registration.name, env)
        entry["env"] = env
        registration.restored_from = dict(registration.entry)
        registration.entry = entry
        restored = ", ".join(candidate.dropped_tuning)
        candidate.add_note(
            f"{restored} restored for this machine: Claude Code's use failed ({str(detail)[:120]}) after it was dropped"
        )
        log.info(
            "%s: %s restored for this machine -- Claude Code's use failed the way a network does",
            registration.name,
            restored,
        )
        self._write_json([registration])
        self._keep_held_credentials(registration)
        return True

    def _undo_runtime_restore(self, registration: Registration) -> None:
        """The restored setting did not help: the entry goes back as it was, the setting dropped."""
        previous = getattr(registration, "restored_from", None)
        if not previous:
            return
        registration.entry = previous
        registration.restored_from = None
        self._write_json([registration])
        registration.note("restoring the dropped runtime setting did not help either; left out")

    def _only_in_project_file(self, name: str) -> bool:
        """Is ``name`` held by the project file alone? ``claude mcp list`` shows it, but ``claude mcp remove`` cannot remove it.

        The project file is rewritten in place, so there is nothing to remove;
        asking the CLI failed with rc=1 for bugzilla-example-com.
        """
        from .existing import CLAUDE_CLI, ExistingRegistrations  # imports this module

        if name not in self._existing_entries():
            return False
        return not any(
            r.name == name and r.source == CLAUDE_CLI for r in ExistingRegistrations.for_run(self.ctx).registrations()
        )

    def _existing_candidate(self, outcome: TargetOutcome, name: str) -> Candidate | None:
        """The candidate this run made from the existing registration called ``name``, if any -- the best proven.

        Two copies may share a name: gitlab-example-net was a logged-in server in
        the claude CLI and an anonymous one in the project file. The one that
        proved most (verified, usage score, proof) speaks for the name, so the
        one that keeps its place is the one written.
        (``mcp.registration.best_copy_speaks_for_name``.)
        """
        found = self._existing_candidates(outcome, name)
        if len(found) > 1 and self.registration.bool_("best_copy_speaks_for_name", True):
            from .types import proof_rank

            # A copy that works or waits for its credential before one that
            # failed: confluence-example-com's file copy (a URL answering a login
            # page) tied with its CLI copy (mcp-atlassian, waiting for its token)
            # on everything else, spoke for the name as the first found, and kept
            # a place it had just been shown unable to fill.
            return max(
                found, key=lambda c: (bool(c.verified), bool(c.needs_credential), contract.score(c), -proof_rank(c))
            )
        return found[0] if found else None

    def _existing_candidates(self, outcome: TargetOutcome, name: str) -> list[Candidate]:
        """Every candidate this run made from an existing registration called ``name``, in stage order."""
        from .existing import ExistingRegistrations  # imports this module

        existing = ExistingRegistrations.for_run(self.ctx)
        found: list[Candidate] = []
        for candidate in [c for stage in outcome.stages for c in stage.candidates]:
            registration = existing.registration_for(candidate)
            if registration is not None and registration.name == name and candidate not in found:
                found.append(candidate)
        return found

    def _keep_existing(self, outcome: TargetOutcome, refused: Sequence[Registration]) -> None:
        """Say which registration the address still has when nothing could be written.

        Every entry this run could write was refused, but the machine may still
        hold one that answered during this run -- confluence.example.com's, in the
        claude CLI, which Claude Code reported as connected before the run
        began. It is left exactly as it was (nothing here removes it), and that
        is said, so the run does not read as having left the address empty, and
        the operator knows where the server they still have lives.
        """
        from .existing import PROJECT_FILE

        retired = {s.get("name") for s in outcome.stale}
        # An entry of a refused registration's own name, already reported as
        # "left as it was and not verified today", is not reported twice.
        said = {r.name for r in refused if r.superseded_nothing is not None}
        why = "; ".join(r.refused for r in refused if r.refused)
        for name, source in outcome.answered_existing.items():
            if name in retired or name in said or any(k.get("name") == name for k in outcome.kept_existing):
                continue
            where = self.display_path if source == PROJECT_FILE else "the claude CLI"
            # The copy that keeps its place is the one in the file: a better
            # proven copy of the same name elsewhere replaces the file's.
            replaced = self._record_kept(outcome, name, replace=True) if source == PROJECT_FILE else ""
            if replaced:
                where = self.display_path
            recorded = self._record_kept(outcome, name) if source != PROJECT_FILE else replaced
            remediation = f"{name} is still registered in {where}, as it was, and answered during this run. " + (
                "It was left untouched."
                if source == PROJECT_FILE
                else recorded
                or f"It is in the claude CLI only -- not in {self.display_path}, because no entry for it "
                f"could be written ({why}); `claude mcp get {name}` shows it."
            )
            outcome.kept_existing.append({"name": name, "source": source, "reason": why, "remediation": remediation})
            log.info("%s: nothing could be written; %s is kept as it was in %s", outcome.target, name, where)

    def user_steps(self, outcome: TargetOutcome) -> list[str]:
        """The steps only the operator can take for this address, in the server's own words, with what was tried.

        build.example.net's API host asks for a key signature ("Use your developer
        account"); every key on the machine was refused for the account; the
        report said "credential REJECTED" and pointed at the wrong place to
        register a key. Here a candidate's refusal is matched against
        ``mcp.verification.login_check.user_steps`` (pattern lists per kind of
        step); the step quotes the challenge the service itself sent for that
        login, names the host that sent it and lists what was tried -- never a
        location nobody published.
        """
        config = self.settings.section("mcp.verification.login_check.user_steps")
        if not config.bool_("enabled", True):
            return []
        kinds = config.dict_("patterns", dict(USER_STEP_PATTERNS))
        offered = list((outcome.auth_plan or {}).get("offered") or [])
        steps: list[str] = []
        seen: set[str] = set()
        candidates = {id(c): c for st in outcome.stages for c in st.candidates}.values()
        for candidate in candidates:
            shake = candidate.handshake
            if shake is None:
                continue
            if shake.browser_login and "needs_browser_login" not in seen:
                from . import noninteractive

                template = str(config.dict_("wording", dict(USER_STEP_WORDING)).get("needs_browser_login") or "")
                if template:
                    seen.add("needs_browser_login")
                    token = (outcome.auth_plan or {}).get("chosen") or {}
                    steps.append(
                        fill(
                            template,
                            {
                                "server": candidate.label,
                                "hosts": ", ".join(noninteractive.hosts_of(shake.browser_login)) or "a sign-in page",
                                "target": outcome.target,
                                "alternative": (
                                    f"; or use {host_of(outcome.target) or outcome.target}'s "
                                    f"{token.get('label') or token.get('method')} instead"
                                )
                                if token.get("method") in ("token", "apikey")
                                else "",
                            },
                        )
                    )
            text = " ".join([str(shake.login_detail or ""), str(shake.error or "")])
            kind = next((k for k, pats in kinds.items() if any(re.search(str(p), text) for p in pats or [])), "")
            if not kind or kind in seen:
                continue
            seen.add(kind)
            scheme = str(candidate.login_scheme or "").lower()
            challenge = next((o for o in offered if str(o.get("scheme") or "").lower() == scheme), None)
            where = str((challenge or {}).get("host") or host_of(outcome.target) or outcome.target)
            said = str((challenge or {}).get("evidence") or "")
            others = sorted(
                {str(o.get("scheme")) for o in offered if str(o.get("host")) == where}
                - {str((challenge or {}).get("scheme"))}
            )
            tried = re.findall(r"(?:^|\s)((?:~|/)[^\s:;,]+):", text)
            template = str(config.dict_("wording", dict(USER_STEP_WORDING)).get(kind) or "")
            if not template:
                continue
            steps.append(
                fill(
                    template,
                    {
                        "host": where,
                        "scheme": str((challenge or {}).get("scheme") or scheme or "its login"),
                        "only": "" if others else "only ",
                        "said": f" ({said})" if said else "",
                        "tried": ", ".join(dict.fromkeys(tried)) or "none found",
                        "target": outcome.target,
                        "server": candidate.label,
                    },
                )
            )
        return steps

    def claude_load_check(self, names: Iterable[str]) -> list[dict[str, Any]]:
        """Which written servers Claude Code will *not* load, and why -- one row each, naming the file to change.

        ``/mcp`` showed no servers at all on a machine where every entry was in
        ``.mcp.json``: eleven were listed in ``disabledMcpjsonServers`` in
        ``.claude/settings.local.json``, and two were skipped as invalid. The run
        had reported every one registered. So, after the last write, for every
        name this run wrote or kept (``mcp.registration.load_check``):

        * the entry in the file is checked as Claude Code reads it (:meth:`claude_rejects`);
        * every settings file Claude Code merges (``settings_files``, and this
          project's record in its configuration) is read for the lists that
          switch a project server off (``disabled_keys``);
        * a project-file server no CLI copy shadows and nothing approves
          (``approve_keys``, ``approve_all_keys``) waits for approval.

        Nothing is changed: these are the operator's choices; the row says
        which file and key decide.
        """
        from .existing import CLAUDE_CLI, ExistingRegistrations  # imports this module

        config = self.registration.section("load_check")
        wanted = {str(n) for n in names if n}
        if not wanted or not config.bool_("enabled", True):
            return []
        try:
            document = json.loads(self.json_path.read_text(encoding="utf-8")) if self.json_path.is_file() else {}
        except (OSError, ValueError):
            document = {}
        servers = dict(document.get("mcpServers") or {}) if isinstance(document, Mapping) else {}
        project = Path.cwd()
        tokens = {
            "project": str(project),
            "claude_home": str(getattr(self.ctx, "claude_home", Path.home() / ".claude")),
        }
        sources: list[tuple[str, Mapping[str, Any]]] = []
        for template in config.str_list("settings_files", list(LOAD_SETTINGS_FILES)):
            path = Path(fill(str(template), tokens)).expanduser()
            try:
                loaded = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None
            except (OSError, ValueError):
                loaded = None
            if isinstance(loaded, Mapping):
                sources.append((str(path), loaded))
        variable = self.settings.str_("mcp.discovery.reuse.claude_config_dir_variable", "CLAUDE_CONFIG_DIR")
        claude_json = Path(os.environ.get(variable) or Path.home()) / ".claude.json"
        try:
            record = (json.loads(claude_json.read_text(encoding="utf-8")).get("projects") or {}).get(str(project))
        except (OSError, ValueError, AttributeError):
            record = None
        if isinstance(record, Mapping):
            sources.append((f"{claude_json} (projects[{project}])", record))
        disabled_keys = config.str_list("disabled_keys", ["disabledMcpjsonServers"])
        approve_keys = config.str_list("approve_keys", ["enabledMcpjsonServers"])
        approve_all = config.str_list("approve_all_keys", ["enableAllProjectMcpServers"])
        cli_names = {r.name for r in ExistingRegistrations.for_run(self.ctx).registrations() if r.source == CLAUDE_CLI}
        rows: list[dict[str, Any]] = []
        for name in sorted(wanted):
            if name in cli_names:
                continue  # the CLI's copy loads; the project file's switches do not apply to it
            if name not in servers:
                continue  # said by missing_from_file
            problem = self.claude_rejects(name, dict(servers[name] or {}))
            if problem:
                rows.append(
                    {
                        "name": name,
                        "where": self.display_path,
                        "why": f"Claude Code skips it: {problem}",
                        "fix": f"correct or remove the entry in {self.display_path}, then re-run phase 4",
                    }
                )
                continue
            off = [
                (path, key)
                for path, data in sources
                for key in disabled_keys
                if name in [str(n) for n in (data.get(key) or [])]
            ]
            if off:
                rows.append(
                    {
                        "name": name,
                        "where": off[0][0],
                        "why": f"switched off by {off[0][1]} in {off[0][0]}",
                        "fix": f"remove {name} from {off[0][1]} in {', '.join(sorted({p for p, _ in off}))}, "
                        "then restart Claude Code",
                    }
                )
                continue
            approved = any(data.get(key) is True for _, data in sources for key in approve_all) or any(
                name in [str(n) for n in (data.get(key) or [])] for _, data in sources for key in approve_keys
            )
            if not approved and config.bool_("report_unapproved", True):
                rows.append(
                    {
                        "name": name,
                        "where": self.display_path,
                        "why": "waits for approval in Claude Code",
                        "fix": f"start `claude` in {project} and approve it (or add it to {approve_keys[0] if approve_keys else 'the approval list'})",
                    }
                )
        return rows

    def verify_written(self, names: Iterable[str]) -> list[dict[str, Any]]:
        """Check the final artifacts -- the file as written and the CLI copies that load before it -- and repair them.

        A check on one write path is not a check on the result: build-example-net's
        username slot was present when it was trialled, the publish loop had a
        check for exactly that, and the entry still reached the file without
        it -- by a path that check did not guard -- while the claude CLI held a
        different copy, which loads first and which nothing checked at all.
        So once everything is written, every entry named in ``names`` is read
        back and compared with what it launches reads (a generated bridge's
        manifest, :func:`.traditional.manifest_missing`):

        * in the project file, a missing slot is added as ``${NAME:-}`` in place;
        * in the claude CLI, a copy missing a slot, or differing from the entry
          this run wrote and proved, is replaced by the file's entry at the
          scope(s) it was held in -- Claude Code loads that copy first.

        Each change is returned as a row for the report. Never in a dry run.
        (``mcp.registration.verify_written``, ``…replace_divergent_cli_copies``.)
        """
        from .existing import ExistingRegistrations  # imports this module
        from .traditional import manifest_missing

        wanted = {str(n) for n in names if n}
        if not wanted or not self.registration.bool_("verify_written", True):
            return []
        rows: list[dict[str, Any]] = []
        path = self.json_path
        try:
            document = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
        except (OSError, ValueError):
            document = {}
        servers = dict(document.get("mcpServers") or {}) if isinstance(document, Mapping) else {}
        changed = False
        # Every entry's credential slots against what the script it launches
        # reads -- written, kept or hand-edited alike: gitlab-example-net passed
        # GITLAB_EXAMPLE_NET_TOKEN to a bridge reading GITLAB_TOKEN, connected, and
        # never logged in. (mcp.registration.align_slots_in_file)
        # Every adapter entry short of a login this run gets its credential
        # slot, whichever branch kept it (api_kinds.slot_entry).
        from . import api_kinds

        for name in sorted(servers):
            entry = dict(servers[name] or {})
            slotted, added = api_kinds.slot_entry(
                self.ctx,
                entry,
                name,
                CandidateVerifier._host_of(
                    Candidate(
                        kind="stdio",
                        command=[str(entry.get("command") or "")],
                        args=[str(a) for a in entry.get("args") or []],
                        env={k: str(v) for k, v in dict(entry.get("env") or {}).items()},
                    )
                ),
            )
            if slotted is not None:
                servers[name] = slotted
                changed = True
                rows.append(
                    {
                        "name": name,
                        "where": self.display_path,
                        "repaired": True,
                        "change": f"added the credential slot {', '.join(added)} (optional) -- it answered, "
                        "but a task that needs the account failed",
                    }
                )
        # Every entry of the file as this run would write it: owned names,
        # no literal credential, a login slot where its server reads one --
        # also the entries no write path touched this run (repair_file_entry).
        for name in sorted(servers):
            repaired_entry, notes = self.repair_file_entry(name, dict(servers[name] or {}))
            if repaired_entry is not None:
                servers[name] = repaired_entry
                changed = True
            rows.extend(
                {"name": name, "where": self.display_path, "repaired": repaired_entry is not None, "change": text}
                for text in notes
            )
        if self.registration.bool_("align_slots_in_file", True):
            for name in sorted(servers):
                aligned, change = self.align_entry_slots(dict(servers[name] or {}))
                if not change:
                    continue
                if aligned is not None:
                    servers[name] = aligned
                    changed = True
                rows.append(
                    {"name": name, "where": self.display_path, "change": change, "repaired": aligned is not None}
                )
        for name in sorted(wanted & set(servers)):
            reshaped = self.reshape_for_claude(dict(servers[name] or {}))
            if reshaped is not None:
                # An entry Claude Code would skip only for its shape -- "type":
                # "stdio" beside a url -- is given the shape its own fields
                # say, wherever it came from: developers-google-com's was
                # left in the file untouched by a run that did not replace it.
                servers[name] = reshaped[0]
                changed = True
                rows.append({"name": name, "where": self.display_path, "change": reshaped[1]})
            entry = dict(servers[name] or {})
            _manifest, missing = manifest_missing(entry, self.settings)
            if not missing:
                continue
            env = dict(entry.get("env") or {})
            env.update({variable: OPTIONAL_PLACEHOLDER.format(name=variable) for variable in missing})
            entry["env"] = dict(sorted(env.items()))
            servers[name] = entry
            changed = True
            rows.append({"name": name, "where": self.display_path, "change": f"added {', '.join(missing)}"})
        if changed and not self.ctx.dry_run:
            document["mcpServers"] = dict(sorted(servers.items()))
            try:
                self._atomic_write(path, json.dumps(document, indent=2, sort_keys=False) + "\n")
            except OSError as exc:
                log.warning("could not repair %s: %s", path, exc)
                rows = [dict(r, change=f"{r['change']} -- could not be written ({exc})") for r in rows]
        existing = ExistingRegistrations.for_run(self.ctx)
        existing._held = None  # the CLI's configuration as it is now, not as the run began
        held = existing.cli_config_entries()
        # Differing alone replaces a CLI copy only for an entry that verified:
        # one merely waiting for a credential proves nothing over what is there.
        proved = {r.name for r in self.published if r.written and not r.refused and r.candidate.verified}
        claude = self.ctx.tool("claude", config_key="paths.claude") if held else ""
        for name in sorted(set(held) - set(servers)):
            # A CLI copy with no file entry to replace it: said, never left silent.
            _aligned, change = self.align_entry_slots(dict(held[name][0] or {}))
            if change:
                rows.append(
                    {
                        "name": name,
                        "where": "the claude CLI",
                        "change": change + " -- fix it there, "
                        f"or remove it ({self.remove_by_hand(name)}) and re-run phase 4",
                        "repaired": False,
                    }
                )
        for name in sorted(wanted & set(held)):
            theirs, scopes = held[name]
            ours = servers.get(name)
            if not ours:
                continue
            _manifest, missing = manifest_missing(theirs, self.settings)
            # A CLI copy whose credential never reaches its script is replaced by
            # the file's, which was just aligned -- the CLI's copy loads first.
            unread = bool(self.align_entry_slots(dict(theirs or {}))[1]) and not _same_copy(theirs, ours)
            differs = (name in proved or unread) and not _same_copy(theirs, ours)
            if not (missing or differs) or not self.registration.bool_("replace_divergent_cli_copies", True):
                continue
            why = (
                f"it lacked {', '.join(missing)}"
                if missing
                else f"it differed from the entry proved in {self.display_path}"
            )
            # Never replaced by an entry that drops a login the copy supplies:
            # the file's entry gets the slot first (carry_supplied_credentials).
            if self.registration.bool_("carry_supplied_credentials", True):
                env = dict(ours.get("env") or {})
                carried = self._carry_from([theirs], ours, env, name)
                if carried and not self.ctx.dry_run:
                    ours = dict(ours, env=dict(sorted(env.items())))
                    servers[name] = ours
                    document["mcpServers"] = dict(sorted(servers.items()))
                    try:
                        self._atomic_write(path, json.dumps(document, indent=2, sort_keys=False) + "\n")
                        self._keep_carried_values(theirs, ours)
                        rows.append(
                            {
                                "name": name,
                                "where": self.display_path,
                                "repaired": True,
                                "change": f"kept {', '.join(carried)} from the claude CLI copy",
                            }
                        )
                    except OSError as exc:
                        log.warning("could not repair %s: %s", path, exc)
                        continue
            if not claude or self.ctx.dry_run:
                rows.append({"name": name, "where": "the claude CLI", "change": f"would be replaced: {why}"})
                continue
            if self._remove_from_cli(claude, name, f"replace {name} (divergent CLI copy)") is None:
                rows.append(
                    {
                        "name": name,
                        "where": "the claude CLI",
                        "change": f"not replaced ({why}); remove it: {self.remove_by_hand(name)}",
                    }
                )
                continue
            payload = json.dumps(ours, separators=(",", ":"))
            ok = True
            for scope in scopes or [self.config.str_("register_scope", "local")]:
                argv = [
                    self._expand(t, claude=claude, name=name, json_payload=payload)
                    for t in self.registration.list_("add_command", ["{claude}", "mcp", "add-json", "{name}", "{json}"])
                ]
                argv += [
                    self._expand(t, scope=scope) for t in self.registration.list_("scope_flag", ["--scope", "{scope}"])
                ]
                ok = (
                    self.ctx.runner.run(
                        argv, timeout=self.registration.float_("timeout_seconds", 90), label=f"register {name}"
                    ).ok
                    and ok
                )
            self._known = None
            rows.append(
                {
                    "name": name,
                    "where": "the claude CLI",
                    "change": f"replaced by the entry in {self.display_path} ({why})"
                    if ok
                    else f"removed ({why}), and re-registering it failed: claude mcp add-json {name} '<entry>'",
                }
            )
        # Last, every entry as Claude Code will run it on this machine: a
        # reference it cannot parse, a file it launches or names that is not
        # there. gitlab-example-net named a bridge three folders deep that no
        # longer existed, and was judged broken. (mcp.registration.check_as_run)
        if self.registration.bool_("check_as_run", True):
            repaired = False
            for name in sorted(servers):
                fixed, problems = self.check_as_run(dict(servers[name] or {}))
                if fixed is not None:
                    servers[name] = fixed
                    repaired = True
                rows.extend(
                    {"name": name, "where": self.display_path, "change": text, "repaired": done}
                    for text, done in problems
                )
            if repaired and not self.ctx.dry_run:
                document["mcpServers"] = dict(sorted(servers.items()))
                try:
                    self._atomic_write(path, json.dumps(document, indent=2, sort_keys=False) + "\n")
                except OSError as exc:
                    log.warning("could not repair %s: %s", path, exc)
        if self.registration.bool_("report_value_origins", True):
            # Where each value an entry reads comes from on this machine.
            for name in sorted(servers):
                origins = self.value_origins(dict(servers[name] or {}))
                if origins:
                    log.info(
                        "%s reads %s", name, "; ".join(f"{v} from {where}" for v, where in sorted(origins.items()))
                    )
        for row in rows:
            log.info("checked as written: %s in %s -- %s", row["name"], row["where"], row["change"])
        return rows

    def check_as_run(self, entry: Mapping[str, Any]) -> tuple[dict[str, Any] | None, list[tuple[str, bool]]]:
        """``(entry, [(problem, repaired)])``: an entry read as Claude Code will run it here.

        * a ``${`` that is no ``${NAME}`` / ``${NAME:-default}`` -- Claude Code
          passes it as literal text: reported, never guessed;
        * a launched file, or a file an env value names, that does not exist
          once its references are expanded the way a new terminal's Claude
          Code expands them (:func:`.claude_check.fresh_environ`): pointed at
          the one file of that name among this program's own folders (the
          servers' home, its bridges) when there is exactly one, else reported
          -- with the variable that relocates it, when one does.

        ``entry`` is None when nothing was repaired.
        """
        from . import claude_check

        fresh = claude_check.fresh_environ(self.ctx)
        problems: list[tuple[str, bool]] = []
        fixed = json.loads(json.dumps(dict(entry)))

        def resolved(text: str) -> str:
            return ENTRY_VARIABLE.sub(lambda m: str(fresh.get(m.group(1)) or m.group(2) or m.group(0)), text)

        def check(text: str, where: str, *, path_only: bool) -> str:
            stripped = ENTRY_VARIABLE.sub("", text)
            if "${" in stripped:
                problems.append(
                    (
                        f"{where} holds a reference Claude Code cannot read ({redact.redact(text)}): "
                        "write it as ${NAME} or ${NAME:-default}",
                        False,
                    )
                )
                return text
            value = resolved(text)
            if (
                not path_only
                or not value.startswith("/")
                or "${" in value
                or os.pathsep in value
                or Path(value).exists()
            ):
                return text
            found = self._find_own_file(Path(value))
            variables = [m.group(1) for m in ENTRY_VARIABLE.finditer(text)]
            if found is not None:
                rewritten = self.portable(str(found), self.path_roots())
                problems.append((f"{where} named {value}, which is not there; pointed at {found}", True))
                return rewritten
            hint = f" -- export {', '.join(variables)} for this machine, or run phase 1 here" if variables else ""
            problems.append((f"{where} names {value}, which is not there{hint}", False))
            return text

        command = str(fixed.get("command") or "")
        if fixed.get("url"):
            check(str(fixed["url"]), "url", path_only=False)
            for key, value in dict(fixed.get("headers") or {}).items():
                check(str(value), f"header {key}", path_only=False)
            return None, problems
        if command:
            fixed["command"] = check(command, "command", path_only=True)
            # There, and still not runnable: a launcher whose interpreter is
            # another machine's, or one that cannot import what it starts.
            from .launch_form import runnable

            launched = resolved(str(fixed["command"]))
            if launched.startswith("/") and "${" not in launched and Path(launched).exists():
                cannot = runnable(launched, self.settings, which=self.ctx.runner.which)
                if cannot:
                    problems.append(
                        (
                            f"command {launched} cannot run here: {cannot} -- copying a launcher does not "
                            "copy the program; launch the package by its launcher form (uvx/npx) or install "
                            "it here",
                            False,
                        )
                    )
        fixed_args = []
        for index, arg in enumerate(fixed.get("args") or []):
            fixed_args.append(check(str(arg), f"argument {index + 1}", path_only=not str(arg).startswith("-")))
        if "args" in fixed:
            fixed["args"] = fixed_args
        env = dict(fixed.get("env") or {})
        for key, value in env.items():
            text = str(value)
            names_file = bool(LOCATION_KEY.search(str(key))) or bool(
                re.search(r"\.(pem|crt|cer|json|py|js|ya?ml|toml)$", resolved(text))
            )
            env[key] = check(text, key, path_only=names_file)
        if env:
            fixed["env"] = env
        problems.extend((text, False) for text in self._start_hazards(env))
        for key, value in dict(fixed.get("headers") or {}).items():
            check(str(value), f"header {key}", path_only=False)
        return (fixed if _canonical(fixed) != _canonical(entry) else None), problems

    def _start_hazards(self, env: Mapping[str, Any]) -> list[str]:
        """Runtime settings holding a wait at or past Claude Code's start-up limit -- flagged, never guessed at.

        ``NODE_OPTIONS=--network-family-autoselection-attempt-timeout=30000``
        held smelt-example-net's start past Claude Code's 30 s on another machine.
        A number after a flag naming a timeout is read as milliseconds from
        1000 up, else seconds. (``mcp.verification.runtime_tuning.flag_start_hazards``.)
        """
        from .verifier import RUNTIME_OPTION_VARIABLES

        config = self.settings.section("mcp.verification.runtime_tuning")
        if not config.bool_("flag_start_hazards", True):
            return []
        names = [p for p in config.str_list("runtime_option_variables", list(RUNTIME_OPTION_VARIABLES)) if p]
        wait = self.settings.section("mcp.registration.client_timeout")
        budget = wait.float_("seconds", 30.0) * wait.float_("margin", 0.8)
        out = []
        for key, value in env.items():
            if not any(re.fullmatch(p, str(key)) for p in names):
                continue
            for flag, number in re.findall(r"(--?[\w.-]*timeout[\w.-]*)[=\s]+(\d+)", str(value), re.IGNORECASE):
                seconds = int(number) / 1000.0 if int(number) >= 1000 else float(number)
                if seconds >= budget:
                    out.append(
                        f"{key} sets {flag}={number} ({seconds:g}s), which can hold the server's start past "
                        f"Claude Code's {wait.float_('seconds', 30.0):g}s start-up wait on another machine -- "
                        "remove it unless the server needs it"
                    )
        return out

    def travelling_files(self) -> tuple[list[str], dict[str, list[str]]]:
        """``(beside, machine)`` for the file as written: what must be copied with it, and what each machine supplies.

        ``beside``: the top-level folders and files, relative to the file's own
        folder, that its entries name there (``mcp-servers/``, ``bridges/``, a
        trust bundle). ``machine``: each named path outside that folder (a
        system bundle, an interpreter), with the variables that relocate it.
        """
        try:
            document = json.loads(self.json_path.read_text(encoding="utf-8")) if self.json_path.is_file() else {}
        except (OSError, ValueError):
            return [], {}
        base = self.json_path.parent.resolve()
        beside: set[str] = set()
        machine: dict[str, list[str]] = {}
        for entry in dict(document.get("mcpServers") or {}).values():
            entry = dict(entry or {})
            texts = [
                str(entry.get("command") or ""),
                *map(str, entry.get("args") or []),
                *map(str, dict(entry.get("env") or {}).values()),
            ]
            for text in texts:
                value = ENTRY_VARIABLE.sub(lambda m: m.group(2) or "", text)
                if not value.startswith("/") or os.pathsep in value:
                    continue
                path = Path(value)
                try:
                    inside = path.resolve().relative_to(base)
                except (OSError, ValueError):
                    variables = sorted({m.group(1) for m in ENTRY_VARIABLE.finditer(text)})
                    if path.exists() and not variables and path.is_file() and path.parts[1:2] in (("usr",), ("bin",)):
                        continue  # a system command: every machine has its own
                    machine.setdefault(value, variables)
                    continue
                top = inside.parts[0] if inside.parts else ""
                if top:
                    beside.add(top + ("/" if (base / top).is_dir() else ""))
        return sorted(beside), dict(sorted(machine.items()))

    def own_entry_files(self, entry: Mapping[str, Any], name: str) -> dict[str, Any]:
        """``entry`` with every file it names in this program's state or scratch area copied beside the file it is written to.

        The run's trust bundle (in the state folder) and the configuration
        folder a repair gave a server (in the scratch area) were written as
        they were; a copied ``.mcp.json`` named paths that exist on no other
        machine, and cleanup may delete the scratch ones here. Only a value
        that is one path -- as written, or a reference's default -- is moved,
        file or folder, into the servers' home (:func:`.paths_home.artifacts_home`),
        and written portably. (``mcp.registration.entry_files_beside_file``.)
        """
        from ..core.paths_home import artifacts_home

        home = artifacts_home(self.settings)
        owned = []
        for root in (getattr(self.ctx, "state_dir", None), getattr(self.ctx, "work_dir", None)):
            try:
                if root:
                    owned.append(Path(root).resolve())
            except OSError:
                continue
        if not owned:
            return dict(entry)
        # Installed programs are phase 1's on every machine (a venv, a
        # launcher): never copied, only data an entry reads.
        installed = [Path(str(self.settings.path("install.venv_path") or Path(self.ctx.state_dir) / "venv"))]
        limit = self.registration.int_("entry_files_max_bytes", ENTRY_FILES_MAX_BYTES)
        roots = self.path_roots()
        moved: dict[str, str] = {}

        def size_of(path: Path) -> int:
            if path.is_file():
                return path.stat().st_size
            total = 0
            for item in path.rglob("*"):
                if item.is_file() and not item.is_symlink():
                    total += item.stat().st_size
                    if total > limit:
                        break
            return total

        def own(value: Any) -> Any:
            if not isinstance(value, str):
                return value
            text = value.strip()
            path_text = ENTRY_VARIABLE.sub(lambda m: m.group(2) or "", text) if ENTRY_VARIABLE.search(text) else text
            if ENTRY_VARIABLE.search(text) and ENTRY_VARIABLE.fullmatch(text) is None and not path_text.startswith("/"):
                return value
            if not path_text.startswith("/") or os.pathsep in path_text or any(c.isspace() for c in path_text):
                return value
            source = Path(path_text)
            try:
                resolved = source.resolve()
            except OSError:
                return value
            if (
                not resolved.exists()
                or resolved == home
                or home in resolved.parents
                or resolved in owned
                or not any(r in resolved.parents for r in owned)
                or any(i == resolved or i in resolved.parents for i in installed)
                or (resolved.is_file() and os.access(resolved, os.X_OK) and resolved.suffix not in DATA_SUFFIXES)
            ):
                return value
            try:
                if size_of(resolved) > limit:
                    log.info("%s: %s is larger than %d bytes and is left where it is", name, resolved, limit)
                    return value
            except OSError:
                return value
            target = home / slugify(name) / resolved.name
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                if resolved.is_dir():
                    shutil.copytree(resolved, target, dirs_exist_ok=True)
                else:
                    shutil.copy2(resolved, target)
            except OSError as exc:
                log.warning("%s: could not copy %s beside %s: %s", name, resolved, self.display_path, exc)
                return value
            moved[str(resolved)] = str(target)
            return self.portable(str(target), roots)

        fixed = json.loads(json.dumps(dict(entry)))
        if fixed.get("args"):
            fixed["args"] = [own(a) for a in fixed["args"]]
        if fixed.get("env"):
            fixed["env"] = {k: own(v) for k, v in fixed["env"].items()}
        if moved:
            log.info(
                "%s: files it names copied beside %s: %s",
                name,
                self.display_path,
                ", ".join(f"{Path(o).name} -> {n}" for o, n in moved.items()),
            )
        return fixed

    def repoint_missing(self, candidate: Candidate) -> list[str]:
        """Point each absolute path ``candidate`` launches or names that is not there at the one file of that name here.

        The same search as the final file check (:meth:`check_as_run`), done
        where an existing entry is judged -- before its trial, so a file that
        only moved is never the reason it is called broken. Returns what moved.
        """
        moved: list[str] = []

        def fix(value: str) -> str:
            text = str(value)
            if not text.startswith("/") or os.pathsep in text or Path(text).exists():
                return text
            found = self._find_own_file(Path(text))
            if found is None:
                return text
            moved.append(f"{text} -> {found}")
            return str(found)

        if candidate.kind != "stdio":
            return []
        candidate.command = [fix(t) for t in candidate.command]
        candidate.args = [fix(a) for a in candidate.args]
        candidate.env = {k: fix(v) if isinstance(v, str) else v for k, v in candidate.env.items()}
        for key, reference in list(candidate.references.items()):
            # A reference whose default named the old path names the new one.
            for old_new in moved:
                old, new = old_new.split(" -> ", 1)
                if old in reference:
                    candidate.references[key] = reference.replace(old, new)
        if moved:
            candidate.handshake = None
            candidate.add_note("files it names had moved and were found again: " + "; ".join(moved))
            log.info("%s: files it names had moved and were found again: %s", candidate.label, "; ".join(moved))
        return moved

    def repointed_entry(self, entry: Mapping[str, Any]) -> dict[str, Any]:
        """``entry`` with every missing absolute path re-pointed (:meth:`_find_own_file`) -- bare or a ``${X:-path}`` default.

        The same repair :meth:`repoint_missing` gives a candidate, for every
        check that judges the entry as written: gitlab-example-net's bridge was
        re-pointed for its trial, then judged "does not start as written" from
        the file's old path, and a package nobody had tried replaced it.
        """

        def fix(text: str) -> str:
            def one(path: str) -> str:
                if not path.startswith("/") or os.pathsep in path or Path(path).exists():
                    return path
                found = self._find_own_file(Path(path))
                return str(found) if found is not None else path

            if ENTRY_VARIABLE.search(text):
                return ENTRY_VARIABLE.sub(
                    lambda m: m.group(0) if m.group(2) is None else m.group(0).replace(m.group(2), one(m.group(2)), 1),
                    text,
                )
            head, sep, rest = text.partition(" ")
            return one(head) + sep + rest if sep and head.startswith("/") else one(text)

        fixed = json.loads(json.dumps(dict(entry)))
        if isinstance(fixed.get("command"), str):
            fixed["command"] = fix(fixed["command"])
        if fixed.get("args"):
            fixed["args"] = [fix(a) if isinstance(a, str) else a for a in fixed["args"]]
        if fixed.get("env"):
            fixed["env"] = {k: fix(v) if isinstance(v, str) else v for k, v in fixed["env"].items()}
        return fixed

    def _find_own_file(self, missing: Path) -> Path | None:
        """The one file named like ``missing`` (same name, same parent folder name if any) in this program's folders.

        First by the path's own tail under each of this program's roots: an
        entry naming ``/home/u/claude_ai_initialization/bridges/x.py``, used
        from a folder cloned as ``claude-ai-initialization`` (or moved
        anywhere), finds ``<here>/bridges/x.py`` -- whatever the old folder was
        called, longest tail first, at least a folder and a file.
        (``mcp.registration.repoint_by_relative_path``.) Then by name.
        """
        from ..core.paths_home import artifacts_home

        tail = self._found_by_tail(missing)
        if tail is not None:
            return tail
        roots = [
            artifacts_home(self.settings),
            self.json_path.parent / "bridges",
            *[
                Path(os.path.expanduser(str(p)))
                for p in self.registration.str_list("legacy_artifacts_homes", [])
                if str(p).strip()
            ],
        ]
        matches: list[Path] = []
        for root in roots:
            try:
                if root.is_dir():
                    matches += [p for p in root.rglob(missing.name) if p.is_file()]
            except OSError:
                continue
        if len(matches) > 1:
            matches = [p for p in matches if p.parent.name == missing.parent.name] or matches
        unique = {p.resolve() for p in matches}
        return next(iter(unique)) if len(unique) == 1 else None

    def _program_roots(self) -> list[Path]:
        """Where this program's files live here: the folder of the file written to, the servers' home, its own checkout."""
        from ..core.paths_home import artifacts_home

        found: list[Path] = []
        for root in (
            self.json_path.resolve().parent,
            artifacts_home(self.settings),
            Path(__file__).resolve().parents[2],
        ):
            try:
                if root and Path(root).is_dir() and not self._too_broad_root(str(root)) and Path(root) not in found:
                    found.append(Path(root))
            except OSError:
                continue
        return found

    def _found_by_tail(self, missing: Path) -> Path | None:
        """The file under one of :meth:`_program_roots` at the longest tail of ``missing`` (folder + file at least)."""
        if not self.registration.bool_("repoint_by_relative_path", True):
            return None
        parts = [p for p in missing.parts if p not in ("/", "")]
        for length in range(len(parts) - 1, 1, -1):
            tail = Path(*parts[-length:])
            hits = {(root / tail).resolve() for root in self._program_roots() if (root / tail).exists()}
            if len(hits) == 1:
                return next(iter(hits))
            if len(hits) > 1:
                return None  # ambiguous: never guessed
        return None

    def _keep_carried_values(self, theirs: Mapping[str, Any], ours: Mapping[str, Any]) -> None:
        """The values ``theirs`` supplies for variables ``ours`` now reads, kept in Claude Code's settings env (0600)."""
        from . import claude_check

        fresh = claude_check.fresh_environ(self.ctx)
        updates: dict[str, str] = {}
        theirs_env = dict(theirs.get("env") or {})
        for key, value in dict(ours.get("env") or {}).items():
            match = ENTRY_VARIABLE.fullmatch(str(value).strip())
            if not match or fresh.get(match.group(1)):
                continue
            source = str(theirs_env.get(key) or "")
            inner = ENTRY_VARIABLE.fullmatch(source.strip())
            supplied = (inner.group(2) or "") if inner else ("" if ENTRY_VARIABLE.search(source) else source)
            if supplied.strip():
                updates[match.group(1)] = supplied
        if not updates:
            return
        from ..provision.settings_writer import SettingsWriter, secret_settings_path

        for value in updates.values():
            redact.register(value)
        current = self._claude_settings_env()
        updates = {k: v for k, v in updates.items() if not current.get(k)}
        if not updates:
            return
        path = secret_settings_path(self.ctx)
        result = SettingsWriter(self.ctx, path).apply({"env": updates})
        if getattr(result, "error", ""):
            log.warning("could not keep %s in %s: %s", ", ".join(sorted(updates)), path, result.error)
            return
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        log.info("kept %s in %s (env), where Claude Code reads it", ", ".join(sorted(updates)), path)

    def repair_file_entry(self, name: str, entry: Mapping[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        """``(entry or None, changes)``: one entry of the file, as every entry this run writes would be.

        The final check reaches entries no write path touched this run -- kept,
        hand-edited, from an older version -- and they travel all the same:

        * a credential written as a literal goes to Claude Code's settings env
          under a name of the entry's own, and the entry reads it
          (``move_literal_credentials_in_file``) -- never over another value;
        * every reference goes through :meth:`owned_reference`; a credential
          Claude Code's settings hold under the old name is kept under the new
          one (``own_references_in_file``);
        * a stdio entry with no way to send a login gets the slot its own
          server reads one from -- its source, else the same package's other
          entries (:meth:`.CandidateVerifier.server_credential`), written
          optional (``slot_missing_logins_in_file``).

        The values go where Claude Code reads them (0600), never into the file.
        """
        from .verifier import sibling_credential

        fixed = copy.deepcopy(dict(entry))
        changes: list[str] = []
        moved = self.launcher_repair(fixed)
        if moved:
            changes.append(moved)
        env = {str(k): v for k, v in dict(fixed.get("env") or {}).items()}
        temp = Candidate(
            kind="stdio" if fixed.get("command") else "url",
            name=name,
            command=[str(fixed.get("command") or "")] if fixed.get("command") else [],
            args=[str(a) for a in fixed.get("args") or []],
            url=str(fixed.get("url") or ""),
            env={k: str(v) for k, v in env.items()},
        )
        if self.registration.bool_("move_literal_credentials_in_file", True):
            current = self._claude_settings_env()
            for key, value in list(env.items()):
                text = str(value or "")
                if (
                    not text
                    or "${" in text
                    or text.startswith("/")
                    or not is_secret_name(key)
                    or names_a_selector(key)
                    or self.env_contract.session(key)
                ):
                    continue
                variable = (
                    self.owned_reference(key, name, host=CandidateVerifier._host_of(temp))
                    if self.registration.bool_("own_reference_names", True)
                    else self._exported_name(key, name)
                )
                if current.get(variable) and current[variable] != text:
                    changes.append(
                        f"{key} holds a literal credential, and Claude Code's settings hold another value "
                        f"for {variable}: left as it is -- move it there by hand"
                    )
                    continue
                temp.kept_out[variable] = Secret(text, source=f"{key}, a literal in {self.display_path}")
                redact.register(text)
                env[key] = ENV_PLACEHOLDER.format(name=variable)
                changes.append(
                    f"{key}'s literal credential moved to Claude Code's settings as {variable}; the entry "
                    "reads it from there"
                )
        if self.registration.bool_("own_references_in_file", True):
            before = dict(env)
            env = self.own_references(fixed, env, name, temp)
            moved = sorted(k for k in env if env[k] != before.get(k))
            if moved:
                changes.append(
                    f"{', '.join(moved)} now read names only this entry reads -- a shell's export of the "
                    "generic name can no longer win over what this entry was given"
                )
        if fixed.get("command") and self.registration.bool_("slot_missing_logins_in_file", True):
            verifier = CandidateVerifier(self.ctx)
            temp.env = {k: str(v) for k, v in env.items()}
            if not verifier.sends_a_login(temp):
                variable, _offered = verifier.server_credential(temp)
                why = "its server's own code reads its login from it"
                if not variable:
                    variable = sibling_credential(self.ctx, temp.full_command())
                    why = "the same package's other entries read their login from it"
                if variable and variable not in env:
                    exported = self.owned_reference(variable, name, host=CandidateVerifier._host_of(temp))
                    env[variable] = OPTIONAL_PLACEHOLDER.format(name=exported)
                    changes.append(
                        f"added the credential slot {variable} (optional, reads {exported}): {why} -- "
                        "it was written with no way to send a login"
                    )
        if not changes:
            return None, []
        if env:
            fixed["env"] = dict(sorted(env.items()))
        if temp.kept_out and not self.ctx.dry_run:
            self._keep_in_settings(
                {k: (v.reveal() if hasattr(v, "reveal") else str(v)) for k, v in temp.kept_out.items()}
            )
        return fixed, changes

    def value_origins(self, entry: Mapping[str, Any]) -> dict[str, str]:
        """``{variable: where a new terminal's Claude Code gets it}`` for every reference ``entry`` reads.

        Claude Code's settings env, a startup file's line (``~/.bashrc:23``),
        the entry's own default, or unset -- names and places only, never a
        value. bugzilla-example-com's key came from a revoked export in
        ~/.bashrc, and nothing said so. (``mcp.registration.report_value_origins``.)
        """
        from ..core.machine_env import MachineEnv

        text = json.dumps(dict(entry))
        settings_env = self._claude_settings_env()
        out: dict[str, str] = {}
        for match in self._REFERENCE.finditer(text):
            variable, default = match.group(1), match.group(3)
            if variable in out:
                continue
            if settings_env.get(variable):
                out[variable] = "Claude Code's settings"
                continue
            lines = MachineEnv(self.ctx).exported_at(variable)
            if lines:
                out[variable] = ", ".join(lines)
            elif default:
                out[variable] = "the entry's default"
            else:
                out[variable] = "unset" + (" (optional)" if match.group(2) else "")
        return out

    def launcher_repair(self, entry: dict[str, Any]) -> str:
        """Rewrite, in place, a command that is an installer's private launcher to its portable form; what changed, or "".

        The command is read as Claude Code expands it here (an ICA_MCP_* default
        included), so an entry copied from another machine -- naming that
        machine's ``.../uv/tools/<pkg>/bin/<pkg>``, or a copy of that launcher
        whose interpreter is not here -- is recognised whether or not the file
        exists. Only when the portable launcher is on this machine.
        (``mcp.launch_form.repair_in_file``.)
        """
        from . import claude_check, launch_form

        if (
            entry.get("url")
            or not entry.get("command")
            or not self.settings.bool_("mcp.launch_form.repair_in_file", True)
        ):
            return ""
        fresh = claude_check.fresh_environ(self.ctx)
        command = ENTRY_VARIABLE.sub(
            lambda m: str(fresh.get(m.group(1)) or m.group(2) or m.group(0)), str(entry.get("command"))
        )
        portable, found = launch_form.launcher_form(
            [command, *[str(a) for a in entry.get("args") or []]], self.settings
        )
        if not portable or not self.ctx.runner.which(portable[0]):
            return ""
        entry["command"], entry["args"] = portable[0], portable[1:]
        return (
            f"command {command} was {found['package']}'s private launcher ({found['family']}), which runs only "
            f"beside its own install; now {' '.join(portable[:4])}, which starts the same package anywhere"
        )

    def _keep_in_settings(self, values: Mapping[str, str]) -> None:
        """``values`` into Claude Code's private settings env (0600), never over a different value already there."""
        from ..provision.settings_writer import SettingsWriter, secret_settings_path

        current = self._claude_settings_env()
        updates = {k: v for k, v in values.items() if str(v).strip() and not current.get(k)}
        if not updates:
            return
        for value in updates.values():
            redact.register(value)
        path = secret_settings_path(self.ctx)
        result = SettingsWriter(self.ctx, path).apply({"env": updates})
        if getattr(result, "error", ""):
            log.warning("could not keep %s in %s: %s", ", ".join(sorted(updates)), path, result.error)
            return
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        log.info("kept %s in %s (env), where Claude Code reads it", ", ".join(sorted(updates)), path)

    def align_entry_slots(self, entry: Mapping[str, Any]) -> tuple[dict[str, Any] | None, str]:
        """``(entry, change)``: an entry whose credential slot its launched script never reads, renamed to the one it does.

        A slot (an ``env`` key named like a credential) the script's source
        never reads, beside exactly one credential variable it reads that the
        entry does not set, is renamed to that variable -- its value, a
        ``${NAME…}`` reference or not, goes with it, so what the operator
        exports is unchanged. Any other unread slot is reported, entry None:
        the credential in it never reaches the server. ``(None, "")`` when
        nothing is wrong or the source cannot be read.
        """
        from .env_contract import EnvContract
        from .portability import expand

        if entry.get("url") or not entry.get("command"):
            return None, ""
        env = dict(entry.get("env") or {})
        slots = [k for k in env if is_secret_name(str(k)) and not self.env_contract.session(str(k))]
        if not slots:
            return None, ""
        argv = [expand(str(entry.get("command") or "")), *[expand(str(a)) for a in entry.get("args") or []]]
        try:
            contract = EnvContract(self.settings)
            if not contract.scripts_in(argv):
                return None, ""  # an installed package keeps the keys its README names
            reader, reads, files = contract.all_reads(argv)
        except Exception:  # noqa: BLE001 - an unreadable source changes nothing
            return None, ""
        if not reads:
            return None, ""
        # Reported only when the name appears nowhere the server could take it
        # from: its source (read through a constant, a list, a loop) or a file
        # the entry names (a manifest).
        # A key the API kind's adapter declares (credential_env) is what that
        # server reads, whatever a source scan says.
        from . import api_kinds

        declared: set[str] = set()
        adapter = api_kinds.adapter_of(self.ctx, Candidate(kind="stdio", command=argv[:1], args=argv[1:]))
        if adapter is not None:
            declared = set(adapter.credential_env)
        unread = [
            k
            for k in slots
            if k not in reads
            and k not in declared
            and not EnvContract.mentions(k, files)
            and not self._named_in_entry_files(k, env)
        ]
        if not unread:
            return None, ""
        # A rename target looks like an environment variable, never a source
        # identifier (`key`, `name`) a scan picked up.
        wanted = sorted(
            n
            for n in reads
            if is_secret_name(n)
            and n not in env
            and not self.env_contract.session(n)
            and re.fullmatch(r"[A-Z][A-Z0-9_]*", n)
        )
        if len(unread) == 1 and len(wanted) == 1:
            fixed = dict(entry)
            new_env = {(wanted[0] if k == unread[0] else k): v for k, v in env.items()}
            fixed["env"] = dict(sorted(new_env.items()))
            return fixed, (
                f"{unread[0]} renamed to {wanted[0]}: {reader} reads {wanted[0]}, never {unread[0]}, so "
                "the credential never reached it"
            )
        reads_words = ", ".join(wanted) if wanted else "no credential variable this entry could fill"
        return None, (f"the credential in {', '.join(unread)} never reaches the server: {reader} reads {reads_words}")

    def _named_in_entry_files(self, variable: str, env: Mapping[str, Any]) -> bool:
        """Does a file the entry names (a manifest) name ``variable``? Then the script reads it by that name.

        A generated bridge and a client bridge read the variables their
        manifest lists -- the script holds no literal name -- so the source
        scan alone called build-opensuse-org's password "never read".
        """
        from .portability import expand

        limit = self.settings.int_("mcp.env_contract.max_bytes", 2_000_000)
        for value in env.values():
            path = Path(expand(str(value)))
            try:
                if not path.is_absolute() or not path.is_file() or path.stat().st_size > limit:
                    continue
                if variable in path.read_text(encoding="utf-8", errors="replace"):
                    return True
            except OSError:
                continue
        return False

    def missing_from_file(self, outcome: TargetOutcome) -> list[str]:
        """Why the address has a server but ``.mcp.json`` has no entry for it -- after trying to put one there; [] when fine.

        Three addresses went missing from the file in three ways --
        bugzilla-opensuse-org, developers-google-com, confluence-example-com --
        while each still had a server. This is the check on the result, whatever
        the cause: every entry written must be in the file (re-written from
        what was published if not); a kept entry that works is recorded
        (:meth:`_record_kept`); and an address that had a server yet ends with no
        entry is said, with every refusal. (``mcp.registration.check_in_file``.)
        """
        if not self.registration.bool_("check_in_file", True) or self.ctx.dry_run:
            return []
        in_file = self._existing_entries()
        problems: list[str] = []
        published = {r.name: r for r in self.published if r.target == outcome.target and r.written and not r.refused}
        lost = [r for name, r in published.items() if name not in in_file]
        if lost:
            self._write_json(lost)
            in_file = self._existing_entries()
            problems += [
                f"{r.name} was written but had gone from {self.display_path}; written again"
                for r in lost
                if r.name in in_file
            ]
            problems += [
                f"{r.name} was written but is not in {self.display_path}, and writing it again failed"
                for r in lost
                if r.name not in in_file
            ]
        if any(name in in_file for name in published):
            return problems
        for name in list(outcome.answered_existing):
            if name not in in_file:
                self._record_kept(outcome, name)
        in_file = self._existing_entries()
        if not any(name in in_file for name in outcome.answered_existing) and self.registration.bool_(
            "repair_missing_entries", True
        ):
            # Still nothing for an address that has a server: the check repairs
            # instead of only reporting. The existing entry that answered is
            # recorded in its portable form, whatever it proved (credential
            # slots as ${VAR}); the report then says what it still needs.
            for name in list(outcome.answered_existing):
                if (
                    name not in in_file
                    and self._keepable(outcome, name)
                    and self._record_kept(outcome, name, force=True)
                ):
                    problems.append(
                        f"{name} had no entry in {self.display_path}; its existing entry is recorded "
                        "there now, in its portable form (it still has to prove itself)"
                    )
                    break
            in_file = self._existing_entries()
        if not any(name in in_file for name in outcome.answered_existing) and self.registration.bool_(
            "repair_from_untried_existing", True
        ):
            # Nothing this run tried answered -- but a registration for the
            # address that this run never tried may: gitlab-example-net's bridge sat
            # one tier below a server the re-check withdrew, and was never asked.
            for name in self._try_untried_existing(outcome):
                if (
                    name not in in_file
                    and self._keepable(outcome, name)
                    and self._record_kept(outcome, name, force=True)
                ):
                    problems.append(
                        f"{name} had no entry in {self.display_path}; the existing registration, never "
                        "tried this run, was tried now, answered, and is recorded there"
                    )
                    break
            in_file = self._existing_entries()
        if any(name in in_file for name in outcome.answered_existing):
            return problems
        written = self._write_refused_best(outcome)
        if written:
            problems.append(written)
            return problems
        had = [*outcome.verified, *outcome.provisional, *outcome.refused]
        if not had and not outcome.answered_existing:
            return problems  # it never had a server: reported as such elsewhere
        refusals = [f"{r.name}: {r.refused}" for r in self.published if r.target == outcome.target and r.refused]
        held = [k["name"] for k in outcome.kept_existing]
        problems.append(
            f"{outcome.target} has a server but {self.display_path} has no entry for it"
            + (f" (kept in the claude CLI only: {', '.join(held)})" if held else "")
            + (f" -- refused: {'; '.join(refusals)}" if refusals else "")
        )
        return problems

    def _write_refused_best(self, outcome: TargetOutcome) -> str:
        """Never an empty address: write the best candidate this run refused, when it works or waits; what was done.

        Two rounds running an address ended with "has a server but .mcp.json
        has no entry" -- gitlab-example-net, then build-opensuse-org -- each time
        after a working or waiting candidate had been refused so that an
        existing entry could keep a place it then never got. Said, not
        repaired. Now, when nothing for the address is in the file, the best
        refused candidate (shared evidence comparison) that works or waits for
        its credential is written, and that is said.
        (``mcp.registration.never_an_empty_address``.)
        """
        if not self.registration.bool_("never_an_empty_address", True):
            return ""
        from .types import choice_key

        # Only one held back for an existing entry's sake: a gate's refusal
        # (no address, does not start, not MCP) stands.
        refused = [
            r
            for r in self.published
            if r.target == outcome.target
            and r.refused
            and r.held_back
            and r.candidate is not None
            and not r.written
            and (r.candidate.verified or r.candidate.needs_credential)
        ]
        if not refused:
            return ""
        best = min(refused, key=lambda r: (not r.candidate.verified, choice_key(r.candidate)))
        why = best.refused
        best.refused = ""
        self._write_json([best])
        if not best.written:
            best.refused = why
            return ""
        best.note(f"written after all: the address would otherwise have no entry ({why})")
        log.info(
            "%s: %s written -- the address would otherwise have no entry in %s (it was refused: %s)",
            outcome.target,
            best.name,
            self.display_path,
            why,
        )
        return (
            f"{best.name} had been refused ({why}), and nothing else reached {self.display_path}: it is "
            "written now, so the address is not left empty"
        )

    def _try_untried_existing(self, outcome: TargetOutcome) -> list[str]:
        """Try, as written, every registration about the address this run never tried; the names that answered.

        Each is attached to its tier's stage record (so the rest of the writer
        finds it as it finds any reused registration) and judged as Claude Code
        would run it. One that works, or waits for a credential, joins
        ``answered_existing`` -- what the file check repairs from.
        """
        from ..core.report import Status
        from .existing import ExistingRegistrations
        from .types import Stage, StageOutcome

        existing = ExistingRegistrations.for_run(self.ctx)
        judged = set(outcome.answered_existing) | {str(b.get("name") or "") for b in outcome.broken_existing}
        for stage in outcome.stages:
            for candidate in stage.candidates:
                registration = existing.registration_for(candidate)
                if registration is not None:
                    judged.add(registration.name)
        answered: list[str] = []
        for tier in (Stage.ENDPOINT, Stage.PACKAGE, Stage.BRIDGE):
            for candidate in existing.for_target(outcome.target, tier):
                registration = existing.registration_for(candidate)
                name = registration.name if registration is not None else candidate.name
                if not name or name in judged:
                    continue
                judged.add(name)
                try:
                    self.verifier.verify(candidate)
                except Exception:  # noqa: BLE001 - a repair attempt must never cost the check
                    log.exception("%s: trying the existing %s failed", outcome.target, name)
                    continue
                record = outcome.stage(tier)
                if record is None:
                    record = StageOutcome(
                        stage=tier,
                        status=Status.SKIPPED,
                        detail="not run; its existing registrations were tried by the file check",
                    )
                    outcome.stages.append(record)
                record.candidates.append(candidate)
                if candidate.verified or candidate.needs_credential:
                    outcome.answered_existing.setdefault(name, registration.source if registration is not None else "")
                    answered.append(name)
                    log.info("%s: the existing %s, never tried this run, answers", outcome.target, name)
        return answered

    @property
    def _held_names(self) -> dict[str, set[str]]:
        """``{target: {names}}`` of existing entries this run kept in place of a refused replacement."""
        if not hasattr(self, "_held_store"):
            self._held_store: dict[str, set[str]] = {}
        return self._held_store

    def _eligible_incumbent(
        self, registration: Registration, outcome: TargetOutcome, unusable: Mapping[str, Any]
    ) -> tuple[Candidate | None, str]:
        """``(existing copy, "")`` when an existing entry of this name may hold its place at all; ``(None, why)`` if not.

        The one gate every keep rule passes, so no rule can bypass a guard the
        others have. build-opensuse-org's fresh client bridge was refused
        because "the existing build-opensuse-org ranks above it on what is
        known" -- a rule added without the guards the older ones had: that
        existing copy was unusable (Claude Code reported it fails to connect),
        it was the very server being written (the same client bridge, same
        manifest), and it ranked above only on a default user name its slot
        carried. Nothing was written, and .mcp.json lost the address.
        An existing copy is eligible only when it exists and is not the
        candidate, nothing proved it unusable, it is a different server
        (:mod:`.same_server`, across every copy of the name), and it can be
        written (:meth:`_keepable`). (``mcp.registration.one_keep_gate``.)
        """
        name, candidate = registration.name, registration.candidate
        incumbent = self._existing_candidate(outcome, name)
        if incumbent is None or incumbent is candidate:
            return None, ""
        if not self.registration.bool_("one_keep_gate", True):
            return incumbent, ""
        if name in unusable:
            return None, f"it is unusable ({unusable[name][1]})"
        if not self._keepable(outcome, name):
            return None, "no copy of it judged this run can be written to " + self.display_path
        from . import same_server

        mine = same_server.key(candidate, self.settings)
        for copy_ in self._existing_candidates(outcome, name):
            if copy_ is not candidate and same_server.key(copy_, self.settings) == mine:
                # The same server: what is being written refreshes it, carrying
                # the slots and values it had.
                same_server.inherit_credential(candidate, copy_, self.settings)
                return None, f"it is the same server as {candidate.label}, which refreshes it"
        return incumbent, ""

    def _ranks_above(self, incumbent: Candidate, candidate: Candidate) -> bool:
        """Does ``incumbent`` rank strictly above ``candidate`` once the values it supplies count for both?

        A value an existing entry already supplies -- build-opensuse-org's
        ``${BUILD_OPENSUSE_ORG_USERNAME:-<name>}`` -- is carried into whatever
        is written (:meth:`carry_supplied_credentials`), so it is no value the
        candidate "still needs". (``mcp.registration.compare_after_carrying``.)
        """
        from .types import evidence_key

        if not self.registration.bool_("compare_after_carrying", True) or candidate.handshake is None:
            return evidence_key(incumbent) < evidence_key(candidate)
        supplied = {str(k) for k, v in incumbent.env.items() if str(v or "").strip()}
        pending = list(candidate.handshake.credential_pending or [])
        theirs = tuple(getattr(candidate, "evidence", ()) or ())
        if not supplied or not pending or len(theirs) < 3:
            return evidence_key(incumbent) < evidence_key(candidate)
        saved = candidate.evidence
        try:
            candidate.evidence = (*theirs[:-1], len([p for p in pending if p not in supplied]))
            return evidence_key(incumbent) < evidence_key(candidate)
        finally:
            candidate.evidence = saved

    def _keepable(self, outcome: TargetOutcome, name: str) -> bool:
        """Can the existing entry ``name`` be recorded in the file in its portable form, as :meth:`_record_kept` writes it?

        Kept means written, so "may keep its place" asks exactly what writing
        needs: a copy judged this run, in a form that travels. "Nothing known:
        yes" let gitlab.example.net's old claude CLI copy -- discarded this run for
        naming a file not on the machine -- hold its place, and
        :meth:`_record_kept` then had no copy to write: .mcp.json ended with no
        gitlab entry. (``mcp.registration.unjudged_is_keepable``, off.)
        """
        if not self.registration.bool_("held_must_be_recordable", True):
            return True
        previous = self._existing_candidate(outcome, name)
        if (
            previous is not None
            and not previous.verified
            and not previous.needs_credential
            and previous.handshake is not None
            and self.registration.bool_("failed_copy_holds_nothing", True)
        ):
            # Tried this run, and it neither works nor waits for a credential:
            # it has no place to keep, wherever it is written.
            return False
        if previous is None:
            # Already in the file, it is written: keeping it keeps the file as it is.
            if name in self._existing_entries():
                return True
            return self.registration.bool_("unjudged_is_keepable", False)
        try:
            return bool(self.portability_of(previous).registrable)
        except Exception:  # noqa: BLE001 - a check must never cost the registration
            return True

    def _record_kept(self, outcome: TargetOutcome, name: str, *, force: bool = False, replace: bool = False) -> str:
        """Write a kept, working CLI entry into the project file too, portably; what was done, or "".

        developers-google-com stayed working in the claude CLI and vanished
        from ``.mcp.json`` -- the file that is copied to other machines -- because
        "keep what is there" protected the CLI and wrote nothing. Every working
        server ends up in both places: a kept entry that verified, that Claude
        Code reports connected, or that this run kept in place of a refused
        replacement (github-com: kept because it answered, never written because
        it was neither of the other two) -- or, with ``force``, the last answered
        entry of an address that would otherwise have none -- is written in its portable form
        (credentials as ``${VAR}``, a literal turned into a reference) when the
        file has no entry of that name. (``mcp.registration.record_kept_entries``.)
        """
        if not self.registration.bool_("record_kept_entries", True):
            return ""
        in_file = self._existing_entries().get(name)
        if in_file is not None and not replace:
            return ""
        candidate = self._existing_candidate(outcome, name)
        held = name in self._held_names.get(outcome.target, set())
        if candidate is None or not (candidate.verified or name in outcome.connected_existing or held or force):
            log.debug(
                "%s: %s not recorded: %s",
                outcome.target,
                name,
                "no copy of it was tried" if candidate is None else "it proved nothing",
            )
            return ""
        entry = self.entry_for(candidate, name)
        if in_file is not None:
            # The file's copy is replaced by the copy that keeps its place, as
            # it was proved: another launch that proved more, or the same one
            # proved with a credential the file's copy has no slot for --
            # gitlab-example-net's mcp-graphql passed with the held token in
            # HEADERS and was "kept as it was", without HEADERS.
            proved_with = self._credential_slots_missing(entry, in_file)
            other_launch = _launch_signature(entry) != _launch_signature(in_file)
            # A credential slot it lacks is added to the copy that keeps its
            # place whenever that copy answered -- it is the one the operator
            # will put the login into; another launch only when it verified.
            answered = bool(candidate.handshake and candidate.handshake.tools)
            # The file's copy failed this run (neither works nor waits) while the
            # copy keeping the place does one or the other: kept means written.
            usable = bool(candidate.verified or candidate.needs_credential)
            file_failed = other_launch and usable and self._file_copy_failed(outcome, name, in_file)
            if not ((other_launch and candidate.verified) or (proved_with and answered) or file_failed):
                log.debug(
                    "%s: the file's %s stays as it is (verified %s, other launch %s, credential slots %s)",
                    outcome.target,
                    name,
                    candidate.verified,
                    other_launch,
                    proved_with,
                )
                return ""
        registration = Registration(
            name=name,
            candidate=candidate,
            entry=entry,
            scope=self.config.str_("register_scope", "local"),
            target=outcome.target,
        )
        self._write_json([registration])
        if not registration.written and not registration.simulated:
            return ""
        exported = sorted(set(re.findall(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", json.dumps(entry))))
        log.info("%s: the kept %s is recorded in %s too", outcome.target, name, self.display_path)
        return (
            f"It is also recorded in {self.display_path} now, in its portable form"
            + (f" -- export {', '.join(exported)} wherever the file is used" if exported else "")
            + "."
        )

    def _file_copy_failed(self, outcome: TargetOutcome, name: str, in_file: Mapping[str, Any]) -> bool:
        """Was the file's copy of ``name`` tried this run, and found neither working nor waiting for a credential?"""
        for held in self._existing_candidates(outcome, name):
            if held.handshake is None or held.verified or held.needs_credential:
                continue
            try:
                same = _launch_signature(self.entry_for(held, name)) == _launch_signature(in_file)
            except Exception:  # noqa: BLE001 - unknown is not failed
                same = False
            if same:
                return True
        return False

    @staticmethod
    def _credential_slots_missing(proved: Mapping[str, Any], held: Mapping[str, Any]) -> list[str]:
        """Credential keys of the ``proved`` entry that ``held`` lacks -- a key named like one, or reading a variable named like one."""
        theirs = dict(held.get("env") or {})
        missing = []
        for key, value in dict(proved.get("env") or {}).items():
            if key in theirs:
                continue
            variables = [m.group(1) for m in ENTRY_VARIABLE.finditer(str(value))]
            if is_secret_name(str(key)) or any(is_secret_name(v) for v in variables):
                missing.append(str(key))
        return sorted(missing)

    def start_backups(self, outcome: TargetOutcome, chosen: Sequence[Candidate]) -> list[Candidate]:
        """Proven candidates not chosen for ``outcome``, best first.

        Working ones before ones waiting for a credential, then the order the
        stages ran in (tier order). Compared by launch (:meth:`Candidate.identity`),
        so a copy of something already chosen is never a second entry for the
        same service. Never a candidate already shown not to start
        (:attr:`Candidate.healing`), and -- as phase 4 decides before publishing
        -- no server waiting for a credential while a working one was found,
        unless ``--exhaustive-mcp`` asked for everything.
        """
        waiting_ok = self.settings.bool_("mcp.discovery.auth.register_authenticated", True) and (
            not outcome.verified or self.settings.bool_("mcp.exhaustive", False)
        )
        seen = {c.identity() for c in chosen}
        out: list[Candidate] = []
        for stage in outcome.stages:
            for candidate in stage.candidates:
                if candidate.identity() in seen:
                    continue
                if not (candidate.verified or (waiting_ok and candidate.needs_credential)):
                    continue
                healing = getattr(candidate, "healing", None)
                if healing is not None and healing.tested and not healing.ok:
                    continue
                seen.add(candidate.identity())
                out.append(candidate)
        return sorted(out, key=lambda c: not c.verified)

    # -- the same claim for unrelated addresses ----------------------------------

    def _shared_claim(self, registration: Registration) -> list[Registration]:
        """Earlier registrations of this same entry for an *unrelated* address.

        Only a verdict resting on a claim is compared
        (``mcp.registration.shared_claim_bases`` -- provenance, or a verdict
        still waiting on a probe). An entry naming its target, or a server that
        names it in what it advertises, is about that target whatever else
        uses the same package. And two addresses of one organisation are
        related -- an organisation-wide gateway serving both is ordinary -- so
        only addresses with different registrable domains count.
        """
        if not self.registration.bool_("refuse_shared_claims", True):
            return []
        grounding = registration.grounding
        weak = self.registration.str_list("shared_claim_bases", ["vendor", "unprobed"])
        if grounding is None or grounding.basis not in weak:
            return []
        key = registration.candidate.identity()
        earlier = self._claims.setdefault(key, [])
        mine = host_of(registration.target)
        unrelated = [
            r
            for r in earlier
            if r.target != registration.target
            and not same_organisation(host_of(r.target), mine)
            and not same_service(host_of(r.target), mine)
        ]
        earlier.append(registration)
        return unrelated

    def _withdraw(self, registration: Registration, because_of: str) -> None:
        """Take back an entry published for an earlier address.

        Out of ``.mcp.json`` -- restoring whatever the file held under that name
        before this run, if anything -- and out of the CLI, but only if this
        run's own ``add`` put it there. An entry already refused for the same
        reason needs nothing more.
        """
        if registration.refused:
            return  # refused entries were never written anywhere
        reason = (
            f"{registration.candidate.label} was also claimed for {because_of}, an unrelated "
            f"address, and nothing in the entry or the server's answer names "
            f"{registration.target} -- the same claim for several services belongs to none of them"
        )
        registration.refused = reason
        registration.withdrawn = True
        registration.remediation.append(
            f"{registration.name} was withdrawn: {reason}. Set "
            "mcp.registration.refuse_shared_claims = false only if one endpoint really does "
            "serve all of these addresses."
        )
        if registration.simulated:
            registration.note("would be withdrawn (dry run)")
        if registration.written:
            self._unwrite(registration)
        if registration.added_by_run:
            self._unregister(registration)
        if registration not in self._withdrawn:
            self._withdrawn.append(registration)

    def take_withdrawn(self) -> list[Registration]:
        """Withdrawals not yet reported, oldest first; each is handed out once."""
        taken, self._withdrawn = self._withdrawn, []
        return taken

    def _unwrite(self, registration: Registration) -> None:
        """Remove (or restore) this run's entry in ``.mcp.json``, if it is still ours."""
        path = self.json_path
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            registration.remediation.append(
                f"Could not read {path} to withdraw {registration.name} ({exc}); delete it by hand."
            )
            return
        servers = dict(document.get("mcpServers") or {}) if isinstance(document, Mapping) else {}
        if servers.get(registration.name) != registration.entry:
            # Somebody changed it since this run wrote it; it is theirs now.
            registration.remediation.append(
                f"{registration.name} in {self.display_path} changed after this run wrote it, so "
                "it was left alone; delete it by hand if it is the withdrawn entry."
            )
            return
        if registration.had_previous and isinstance(registration.previous_entry, Mapping):
            servers[registration.name] = dict(registration.previous_entry)
        else:
            servers.pop(registration.name, None)
        document = dict(document)
        document["mcpServers"] = dict(sorted(servers.items()))
        try:
            self._atomic_write(path, json.dumps(document, indent=2, sort_keys=False) + "\n")
        except OSError as exc:
            registration.remediation.append(
                f"Could not rewrite {path} to withdraw {registration.name} ({exc}); delete it by hand."
            )
            return
        registration.written = False
        registration.note(
            f"withdrawn from {self.display_path}"
            + (" (the earlier entry of that name was restored)" if registration.had_previous else "")
        )

    def _unregister(self, registration: Registration) -> None:
        """``claude mcp remove`` for an entry this run's own ``add`` registered."""
        claude = self.ctx.tool("claude", config_key="paths.claude")
        argv = [
            self._expand(token, claude=claude or "", name=registration.name)
            for token in self.registration.list_("remove_command", ["{claude}", "mcp", "remove", "{name}"])
        ]
        argv.extend(
            self._expand(token, claude=claude or "", scope=registration.scope)
            for token in self.registration.list_("scope_flag", ["--scope", "{scope}"])
        )
        manual = f"claude mcp remove {registration.name} --scope {registration.scope}"
        if not claude or not argv:
            registration.remediation.append(f"Remove it from the CLI by hand: {manual}")
            return
        result = self.ctx.runner.run(
            argv,
            timeout=self.registration.float_("timeout_seconds", 90),
            label=f"withdraw {registration.name}",
        )
        if result.simulated or result.ok:
            registration.registered = False
            registration.added_by_run = False
            registration.note(
                f"{'would be ' if result.simulated else ''}removed from the claude CLI (scope {registration.scope})"
            )
            self._known = None
            return
        registration.remediation.append(f"Remove it from the CLI by hand: {manual}")

    # -- existing registrations proved wrong ----------------------------------

    def retire(self, outcome: TargetOutcome, others: Sequence[str] = ()) -> list[dict[str, str]]:
        """Take out the existing registrations this run proved are not the target's.

        ``outcome.stale`` lists them (see ``TraditionalPipeline.note_stale``):
        each answered, connects only to somebody else's host, and was refused by
        the registration gate. Left in place, such an entry is loaded in every
        session as though the address were covered, and -- because the CLI never
        re-adds a name it already knows -- it also blocks the correct entry
        found in its place. Six ``api.mcp.ai`` entries stayed that way.

        Only names this program gives the address are touched
        (:meth:`base_name`, with or without a suffix); any other name is the
        operator's and is reported with the command to remove it. The file's
        entry goes only if it is unchanged since it was read; a CLI entry goes
        through ``remove_command``. ``mcp.registration.retire_refused_existing =
        false`` reports every one and removes none. A name that is another
        address's in this run (``others``) is never ours, whatever its prefix.

        Each entry is judged again first, on everything the run has found by
        now: it was recorded during reuse, before the stages had finished, and
        what they found may vouch for its host after all. Returns the entries
        acted on, each with ``action``, ``result`` (``removed``, ``would be
        removed`` or ``kept``) and ``remediation`` filled in.
        """
        from .existing import CLAUDE_CLI, PROJECT_FILE, ExistingRegistrations  # imports this module

        enabled = self.registration.bool_("retire_refused_existing", True)
        base = self.base_name(outcome.target)
        theirs = {self.base_name(o) for o in others if o != outcome.target} - {base}
        existing = ExistingRegistrations.for_run(self.ctx)
        entries = {r.name: r.entry for r in existing.registrations()}
        served_by = endpoint_hosts(outcome)
        handled: list[dict[str, str]] = []
        for stale in list(outcome.stale):
            if stale.get("action"):
                continue
            name = str(stale.get("name") or "")
            source = str(stale.get("source") or "")
            candidate = outcome.stale_candidates.get(name)
            if candidate is not None and self.grounding_of(candidate, outcome.target, served_by).registrable:
                outcome.stale.remove(stale)
                log.info(
                    "%s: %r is kept after all -- what this run found vouches for it",
                    outcome.target,
                    name,
                )
                continue
            by_hand = self._retire_by_hand(name, source)
            handled.append(stale)
            ours = name == base or name.startswith(f"{base}-")
            if ours and any(name == other or name.startswith(f"{other}-") for other in theirs):
                ours = False  # another address in this run gives that name
            if not enabled or not ours:
                stale["action"] = "reported, not removed"
                stale["result"] = "kept"
                stale["remediation"] = (
                    f"{name} is not {outcome.target}'s server: {stale.get('reason', '')}. "
                    + (
                        "mcp.registration.retire_refused_existing is off, so it was left in place. "
                        if not enabled
                        else "It is not a name this program gives that address, so it was left in place. "
                    )
                    + f"Remove it: {by_hand}."
                )
                log.info("%s: %r is not this address's server; left in place", outcome.target, name)
                continue
            done: list[str] = []
            if source == PROJECT_FILE:
                done.append(self._retire_from_file(name, entries.get(name), by_hand, stale))
                # The same name is often in the CLI too -- this program used to
                # write both -- and the reader keeps only the file's copy. A CLI
                # copy pointing at the same service is the same refused entry.
                # A dry run cannot ask the CLI, so it says what a real run checks.
                if done[-1] and self.ctx.dry_run:
                    done.append("and from the claude CLI too if it holds the same entry (dry run)")
                elif done[-1] and self._cli_twin(name, entries.get(name)):
                    done.append(self._retire_from_cli(name, self._retire_by_hand(name, CLAUDE_CLI), stale))
            elif source == CLAUDE_CLI:
                done.append(self._retire_from_cli(name, by_hand, stale))
            done = [d for d in done if d]
            if not done:
                stale["result"] = "kept"
                stale.setdefault("remediation", f"{name} was left in place; {by_hand}.")
            elif any(d.endswith("(dry run)") for d in done):
                stale["result"] = "would be removed"
                stale["remediation"] = (
                    f"{name} is not {outcome.target}'s server ({stale.get('reason', '')}); a real "
                    "run removes it. Set mcp.registration.retire_refused_existing = false to keep it."
                )
            else:
                stale["result"] = "removed"
                existing.forget(name)
                stale.setdefault(
                    "remediation",
                    f"{name} was not {outcome.target}'s server ({stale.get('reason', '')}), and was "
                    "retired. Nothing is needed unless you want it back.",
                )
            stale["action"] = "; ".join(done) or "reported, not removed"
            log.info("%s: %r is not this address's server: %s", outcome.target, name, stale["action"])
        return handled

    def _cli_twin(self, name: str, entry: Mapping[str, Any] | None) -> bool:
        """Does the CLI still hold ``name``, pointing at the same service as ``entry``?"""
        from .existing import ExistingRegistrations

        claude = self.ctx.tool("claude", config_key="paths.claude")
        if not claude or not entry or name not in self._existing(claude):
            return False
        twin = ExistingRegistrations.for_run(self.ctx).cli_entry(name)
        return bool(twin) and _service_key(twin) == _service_key(entry)

    def _retire_by_hand(self, name: str, source: str) -> str:
        from .existing import PROJECT_FILE

        if source == PROJECT_FILE:
            return f"delete {name!r} from {self.display_path}"
        return self.remove_by_hand(name)

    def remove_by_hand(self, name: str) -> str:
        """The command an operator runs to take ``name`` out of the claude CLI.

        One wording everywhere a message asks for it -- duplicates, retirement
        -- built from ``remove_command`` so it is the command this program would
        run itself.
        """
        argv = [
            self._expand(token, claude="claude", name=name)
            for token in self.registration.list_("remove_command", ["{claude}", "mcp", "remove", "{name}"])
        ]
        command = " ".join(argv) or f"claude mcp remove {name}"
        # The exact scope(s), never a bare remove: without one, the CLI takes a
        # project entry out of .mcp.json.
        scopes = self.cli_scopes(name)
        flag = self.registration.list_("scope_flag", ["--scope", "{scope}"])
        if scopes:
            return " && ".join(f"{command} {' '.join(self._expand(t, scope=scope) for t in flag)}" for scope in scopes)
        return f"{command} --scope <local|user> (the scope `{self.get_command_for(name)}` shows; never for the project file)"

    def get_command_for(self, name: str) -> str:
        """``get_command`` filled in for ``name``, for a message to quote."""
        argv = [
            self._expand(token, claude="claude", name=name)
            for token in self.registration.list_("get_command", ["{claude}", "mcp", "get", "{name}"])
        ]
        return " ".join(argv) or f"claude mcp get {name}"

    def _retire_from_file(self, name: str, entry: Mapping[str, Any] | None, by_hand: str, stale: dict[str, str]) -> str:
        path = self.json_path
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            stale["remediation"] = f"Could not read {path} ({exc}); {by_hand}."
            return ""
        servers = dict(document.get("mcpServers") or {}) if isinstance(document, Mapping) else {}
        if name not in servers:
            return f"already gone from {self.display_path}"
        if entry is None or servers.get(name) != entry:
            stale["remediation"] = (
                f"{name} in {self.display_path} changed after this run read it, so it was left "
                f"alone; {by_hand} if it is still the refused entry."
            )
            return ""
        if self.ctx.dry_run:
            return f"would be removed from {self.display_path} (dry run)"
        servers.pop(name, None)
        document = dict(document)
        document["mcpServers"] = dict(sorted(servers.items()))
        try:
            self._atomic_write(path, json.dumps(document, indent=2, sort_keys=False) + "\n")
        except OSError as exc:
            stale["remediation"] = f"Could not rewrite {path} ({exc}); {by_hand}."
            return ""
        self._known = None
        return f"removed from {self.display_path}"

    def _lost_files_cli_copy_replaced(self, registration: Registration, claude: str) -> bool:
        """Take out a CLI copy that launches a file which is gone, when the written entry's files are there.

        gitlab-example-net's CLI copy ran a bridge deleted from the run's scratch
        area; the CLI's copy loads in place of the file's, so a repaired entry
        written to the file would never have been what Claude Code started.
        (``mcp.registration.replace_lost_files_cli_copies``.)
        """
        if not self.registration.bool_("replace_lost_files_cli_copies", True) or not registration.written:
            return False
        from .existing import CLAUDE_CLI, ExistingRegistrations  # imports this module

        held = next(
            (
                r
                for r in ExistingRegistrations.for_run(self.ctx).registrations()
                if r.name == registration.name and r.source == CLAUDE_CLI
            ),
            None,
        )
        if held is None:
            return False
        lost = [p for p in known_good.launched_paths(held.entry) if not Path(p).exists()]
        mine = [p for p in known_good.launched_paths(registration.entry) if not Path(p).exists()]
        if not lost or mine:
            return False
        done = self._remove_from_cli(claude, registration.name, f"replace {registration.name} (its files are gone)")
        if done is not None:
            registration.note(f"the claude CLI's copy launched {lost[0]}, which is gone; replaced by this entry")
        return done is not None

    def _literal_cli_copy_replaced(self, registration: Registration, claude: str) -> bool:
        """Take out a CLI copy that holds a credential as a literal, when the written entry works without it.

        The claude CLI's copy loads in place of the project file's, and
        sd-example-com's held a token pasted into it -- a value in a configuration
        file that gets copied between machines. When this run wrote the same
        name to the file, proved, reading every credential from a variable the
        shell already exports, the literal copy is removed (from every scope)
        so the file's entry is what loads. Otherwise it stays, and the report
        names it (``mcp.registration.replace_literal_cli_copies``).
        """
        if not self.registration.bool_("replace_literal_cli_copies", True) or not registration.written:
            return False
        if not registration.candidate.verified or registration.needs_env:
            return False
        from .existing import CLAUDE_CLI, ExistingRegistrations  # imports this module

        held = next(
            (
                r
                for r in ExistingRegistrations.for_run(self.ctx).registrations()
                if r.name == registration.name and r.source == CLAUDE_CLI and r.dropped
            ),
            None,
        )
        if held is None:
            return False
        variables = re.findall(r"\$\{([A-Za-z_][A-Za-z0-9_]*)", json.dumps(registration.entry))
        if any(is_secret_name(v) and not os.environ.get(v) for v in variables):
            return False
        done = self._remove_from_cli(claude, registration.name, f"replace {registration.name} (literal credential)")
        if done:
            registration.note(
                f"the claude CLI's {registration.name} held a credential as a literal ({', '.join(held.dropped)}); it was "
                f"removed, and the entry in {self.display_path} -- which reads it from the environment -- is what loads"
            )
        return bool(done)

    def cli_scopes(self, name: str) -> list[str]:
        """The claude CLI scopes (``user``, ``local``) holding ``name``, from Claude Code's own configuration."""
        from .existing import ExistingRegistrations  # imports this module

        existing = ExistingRegistrations.for_run(self.ctx)
        held = existing.cli_config_entries().get(name)
        scopes = [s for s in (held[1] if held else []) if s and s != "project"]
        if scopes or not self.registration.bool_("scope_from_get", True):
            return scopes
        # Not in the configuration this program reads: the scope `claude mcp
        # get` reports, when it is a CLI scope -- never the project file's.
        try:
            label = str((self.settings.dict_("mcp.discovery.reuse.get_fields", {}) or {}).get("scope") or "Scope")
            reported = str(parse_get_scope(existing._cli("get_command", name), label) or "")
        except Exception:  # noqa: BLE001 - unknown scope: nothing is removed
            return []
        words = self.registration.dict_("cli_scope_words", dict(CLI_SCOPE_WORDS)) or dict(CLI_SCOPE_WORDS)
        if not reported or existing.project_scope(reported):
            return []
        return [scope for scope, pattern in words.items() if pattern and re.search(str(pattern), reported)][:1]

    def _remove_from_cli(self, claude: str, name: str, label: str) -> bool | None:
        """``claude mcp remove`` ``name`` from every CLI scope holding it; True done, False simulated, None failed.

        One plain remove fails when a name is in several scopes ("exists in
        multiple scopes"): build-example-net's broken copy stayed loaded that way.
        So each scope Claude Code's configuration shows is removed with
        ``scope_flag``; the project file is never asked of the CLI -- it is
        rewritten in place.
        """
        base = [
            self._expand(token, claude=claude, name=name)
            for token in self.registration.list_("remove_command", ["{claude}", "mcp", "remove", "{name}"])
        ]
        if not claude or not base:
            return None
        # Never a remove without its scope: with none, `claude mcp remove NAME`
        # takes a project entry out of .mcp.json -- build-opensuse-org's own
        # entry was removed that way as "a CLI copy". No CLI scope holds it:
        # nothing to remove.
        scopes = self.cli_scopes(name)
        if not scopes:
            log.info(
                "%s: no claude CLI scope holds it -- nothing removed (the project file is never asked of the CLI)", name
            )
            return True
        simulated = False
        for scope in scopes:
            argv = list(base)
            if scope:
                argv += [
                    self._expand(t, scope=scope) for t in self.registration.list_("scope_flag", ["--scope", "{scope}"])
                ]
            result = self.ctx.runner.run(argv, timeout=self.registration.float_("timeout_seconds", 90), label=label)
            if result.simulated:
                simulated = True
            elif not result.ok:
                return None
        return False if simulated else True

    def _retire_from_cli(self, name: str, by_hand: str, stale: dict[str, str]) -> str:
        claude = self.ctx.tool("claude", config_key="paths.claude")
        if not claude:
            stale["remediation"] = f"The claude CLI could not be run to retire it; {by_hand}."
            return ""
        done = self._remove_from_cli(claude, name, f"retire {name}")
        if done is False:
            return "would be removed from the claude CLI (dry run)"
        if done:
            self._known = None
            return "removed from the claude CLI"
        stale["remediation"] = f"The claude CLI would not remove it; {by_hand}."
        return ""

    # -- duplicates --------------------------------------------------------------

    def existing_entries(self) -> dict[str, Mapping[str, Any]]:
        """Servers already in ``.mcp.json``; see :mod:`.existing` for why they are read."""
        return self._existing_entries()

    def served_addresses(self) -> tuple[list[str], list[str]]:
        """``(addresses, unplaced)``: the address each entry in the project file serves, and the entries none was found for.

        For ``--mcp-recheck all`` -- the file copied to another machine, proved
        here without typing every address. An entry's address is the one the
        record of its last proof names (:mod:`.known_good`), else the host it
        reaches whose name it carries (``name == slugify(host)``, the rule this
        program names entries by and :class:`.existing.ExistingRegistrations`
        judges them by). Nothing is guessed: an entry neither ties to an
        address is returned in ``unplaced``, by name.
        """
        addresses: list[str] = []
        unplaced: list[str] = []
        for name, entry in self._existing_entries().items():
            address = self.address_of_entry(name, entry)
            if not address:
                unplaced.append(name)
            elif address not in addresses:
                addresses.append(address)
        return addresses, unplaced

    def address_of_entry(self, name: str, entry: Mapping[str, Any]) -> str:
        """The address an entry serves: the record of its last proof, else the one host it reaches named like it; "".

        Nothing is guessed (see :meth:`served_addresses`).
        """
        from .existing import ExistingRegistrations
        from .types import slugify

        recorded = self.__dict__.get("_recorded_addresses")
        if recorded is None:
            recorded = {}
            for target, rows in known_good.load(self.ctx).items():
                for row in rows or []:
                    if isinstance(row, Mapping) and row.get("name"):
                        recorded.setdefault(str(row["name"]), str(target))
            self.__dict__["_recorded_addresses"] = recorded
        if recorded.get(name):
            return recorded[name]
        hosts = sorted(h for h in ExistingRegistrations._hosts_of(entry) if slugify(h) == name)
        return f"https://{hosts[0]}" if len(hosts) == 1 else ""

    def _existing_entries(self) -> dict[str, Mapping[str, Any]]:
        """Servers already in ``.mcp.json`` before this run touches it."""
        path = self.json_path
        if not path.is_file():
            return {}
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        servers = loaded.get("mcpServers") if isinstance(loaded, Mapping) else None
        if not isinstance(servers, Mapping):
            return {}
        return {str(k): v for k, v in servers.items() if isinstance(v, Mapping)}

    def _duplicates_of(
        self, registration: Registration, existing: Mapping[str, Mapping[str, Any]]
    ) -> list[dict[str, str]]:
        """Entries already in the file that serve the same thing as this one.

        A second entry for one service is not a syntax error and nothing will
        complain about it: Claude Code will happily load both, offer both sets of
        tools, and leave the operator to work out which of two near-identical
        names the model just called. So it is worth saying out loud -- once, with
        the reason -- rather than leaving it to be discovered in use.
        """
        if not self.settings.bool_("mcp.instructions.warn_on_duplicates", True):
            return []
        if not _service_key(registration.entry):
            return []
        out: list[dict[str, str]] = []
        for name, entry in existing.items():
            if name == registration.name:
                continue  # the same server being re-written, not a duplicate
            if not _same_service(registration.entry, entry):
                continue
            out.append(
                {
                    "name": name,
                    "target": str(entry.get("url") or " ".join(_argv_of(entry)) or _service_key(entry)),
                    "why": (
                        f"It is the same {'endpoint host' if entry.get('url') else 'launch command'} "
                        + (
                            f"pointed at the same host ({', '.join(sorted(_entry_hosts(entry)))}) "
                            if not entry.get("url") and _entry_hosts(entry)
                            else ""
                        )
                        + f"as the newly discovered {registration.name}."
                    ),
                }
            )
        return out

    # -- the briefing ----------------------------------------------------------

    def briefing(self, registrations: Sequence[Registration] | None = None) -> dict[str, Any]:
        """What the operator needs and the servers themselves cannot say.

        This used to be written into ``.mcp.json`` under an ``_instructions``
        key, on the reasoning that the file outlives the terminal. It does --
        but the file is also the thing people copy wholesale into another
        project, and a top-level key that only this program understands turns
        every such copy into an edit somebody has to remember to make. So the
        briefing is reported instead: printed in its own framed block, captured
        in the log, and carried in the JSON report, which are three places that
        outlive the terminal and none of which is a configuration file.

        Defaults to every server published in this run, which is what the phase
        wants; pass a subset to brief on part of it.
        """
        if registrations is None:
            registrations = self.published
        # A refused entry is not in the file, so it has no place in a briefing
        # about the file -- telling somebody to export a variable for a server
        # that was never written is worse than saying nothing. The refusal and
        # its reason are reported as their own step instead.
        refused = [r for r in registrations if r.refused]
        registrations = [r for r in registrations if not r.refused]
        servers: dict[str, Any] = {}
        env_names: list[str] = []
        install: list[str] = []
        scripts: list[str] = []

        for registration in registrations:
            candidate = registration.candidate
            required = sorted(
                name
                for name in {*self.exported_variables(candidate), *[k for k, v in candidate.env.items() if v == ""]}
                if not self.env_contract.session(name)
            )
            env_names.extend(name for name in required if name not in env_names)
            steps = [" ".join(argv) for argv in candidate.install if argv]
            install.extend(step for step in steps if step not in install)
            local = self.generated_scripts(candidate)
            scripts.extend(path for path in local if path not in scripts)

            detail: dict[str, Any] = {
                "what": candidate.source or "discovered by initialize-claude-ai",
                "found_by": candidate.origin or "unknown stage",
            }
            if registration.provisional and candidate.handshake is not None and candidate.handshake.login_refused:
                detail["status"] = (
                    f"registered but NOT yet working: it {waits_for_credential(candidate)}. "
                    f"{registration.credential_hint}"
                )
            elif registration.provisional:
                detail["status"] = (
                    f"registered but NOT yet working: it {waits_for_credential(candidate)} -- it "
                    "proved it is there by answering -- and the entry is written with that "
                    "credential left empty. It starts working when the variable below holds a "
                    "valid value, and not before."
                )
                detail["before_it_works"] = (
                    "export "
                    + " ".join(
                        f"{name}={self.credential_placeholder(registration.candidate, name)}"
                        for name in registration.needs_env
                    )
                    + "".join(
                        f" [{name}: {self.credential_hint(registration.candidate, name)}]"
                        for name in registration.needs_env
                    )
                    + ", then re-run phase 4. Obtain each value from the service itself "
                    "(its profile, settings or access-token page) -- this run cannot mint one. "
                    "Re-running rather than restarting is deliberate: the next run trials the "
                    "server with the variable set and writes the entry it proved."
                )
            if required:
                detail["required_environment"] = required
                detail["how_to_supply_them"] = (
                    "export "
                    + " ".join(f"{name}=..." for name in required)
                    + " in the shell you start Claude Code from; the value is never written here."
                )
            if registration.replaces_unusable:
                # Same name, different server: say what it offers now, and what
                # it used to, so nobody looks for a tool that is gone.
                offers = list(candidate.handshake.tools) if candidate.handshake else []
                limit = self.settings.int_("mcp.instructions.replacement_tools_shown", 8)
                detail["replaces"] = (
                    f"the earlier {registration.name}"
                    + (
                        f" (tools such as {', '.join(registration.replaced_tools[:limit])})"
                        if registration.replaced_tools
                        else ""
                    )
                    + (f"; this one offers {', '.join(offers[:limit])}" if offers else "")
                    + (f" and {len(offers) - limit} more" if len(offers) > limit else "")
                )
            if candidate.handshake is not None and candidate.handshake.shape_problems:
                # Said beside the export line: the value already supplied is in
                # a shape its slot cannot use, and here is the shape it needs.
                detail["value_does_not_fit"] = [p["remedy"] for p in candidate.handshake.shape_problems]
            if steps:
                detail["install_first"] = steps
            settings = self._settings_in(registration.entry)
            if settings:
                # Every variable the server reads is in its env block; this is
                # the list of them, with the one thing the file cannot say on
                # its own: what to export to change each.
                detail["settings_in_its_env_block"] = settings
            moves = self._path_variables_in(registration.entry)
            if moves and self.registration.bool_("say_how_to_move", True):
                # Printed, not only kept in the report: build-opensuse-org ran
                # /usr/local/bin/osc-mcp, and on the next machine it was an ENOENT
                # with ICA_MCP_OSC_MCP named nowhere the operator reads.
                from .launch_form import launcher_form

                lines = []
                for variable, path in sorted(moves.items()):
                    portable, found = launcher_form([path], self.settings)
                    if portable:
                        # A launcher of one machine's install cannot be copied:
                        # bugzilla-opensuse-org's .../uv/tools/mcp-bugzilla/bin/mcp-bugzilla
                        # was, and never started. Said as what to do instead.
                        lines.append(
                            f"{path} is {found['package']}'s private launcher ({found['family']}) and runs "
                            f"only beside its own install -- do not copy it; launch "
                            f"{' '.join(portable[:4])} instead, or install {found['package']} there and "
                            f"export {variable}=<its launcher there>"
                        )
                    else:
                        lines.append(f"export {variable}=<where {PurePosixPath(path).name} is there> (here: {path})")
                detail["to_move_it"] = lines
            if local:
                detail["this_server_is_a_script_written_by_this_run"] = local
                detail["keep_it"] = (
                    "These files are not part of any package. If they are deleted or the machine "
                    "changes, the server stops working -- copy them alongside this file, or re-run "
                    "initialize-claude-ai --only mcp to regenerate them."
                )
            if registration.duplicates:
                detail["duplicates"] = registration.duplicates
            servers[registration.name] = detail

        block: dict[str, Any] = {
            "read_this_first": (
                f"Written by initialize-claude-ai. {self.display_path} holds 'mcpServers' and "
                "nothing else, so you can copy the whole file into another project as it is. "
                "Everything below is here, in the report and in the log -- not in the file."
            ),
            "servers": servers,
        }
        if env_names:
            block["required_environment_variables"] = env_names
            block["nothing_here_contains_a_secret"] = (
                f"Each ${{NAME}} in {self.display_path} is expanded from your environment when the "
                "server starts. The file can be committed; the values must not be."
            )
        awaiting = [r for r in registrations if r.provisional]
        # Given a credential, and its service refused it: not "export a value"
        # -- the operator did -- but a different credential or account.
        refused_login = [
            r for r in awaiting if r.candidate.handshake is not None and r.candidate.handshake.login_refused
        ]
        awaiting = [r for r in awaiting if r not in refused_login]
        if refused_login:
            block["these_were_given_a_credential_their_service_refused"] = {
                r.name: r.credential_hint for r in refused_login
            }
        anonymous = [r for r in registrations if r.anonymous_until and not r.refused and r not in awaiting]
        if anonymous:
            # Working for anybody is not working for the account: said apart
            # from the ones that need a credential before doing anything.
            block["these_answer_anonymously_until_given_a_login"] = {
                r.name: [self.credential_instruction(r.candidate, name) for name in r.anonymous_until]
                for r in anonymous
            }
        if awaiting:
            block["these_need_a_real_credential_before_they_work"] = {
                # The variable names alone answer "what do I export"; they do
                # not answer "with what", and for a product whose credential is
                # three values joined by colons that is the whole question.
                #
                # Per variable, not per server. One server's two variables can
                # want two different kinds of value -- a token and a header
                # list -- and describing both with one sentence is how a bare
                # token ended up in API_HEADERS.
                r.name: [self.credential_instruction(r.candidate, name) for name in r.needs_env]
                for r in awaiting
            }
            block["why_they_were_registered_anyway"] = (
                "Each of these proved it is there and speaks MCP, and is waiting for a "
                "credential. Writing the entry now, with the credential empty, means "
                "supplying the token is a one-line export rather than another full discovery "
                "run. Until then Claude Code will report the variable as missing and skip "
                "that server -- nothing else is affected."
            )
        bound = [r for r in registrations if getattr(r.portability, "kind", "") == HOST_BOUND]
        if bound:
            # .mcp.json exists to be copied, and these entries will not survive
            # the trip: they name files on this machine. Saying so here is the
            # difference between a considered decision and an ENOENT on another
            # host with nothing to explain it.
            block["these_entries_only_work_on_this_machine"] = {r.name: r.portability.paths for r in bound}
            block["why_that_matters"] = (
                f"{self.display_path} is meant to be copied -- into another project, a commit, "
                "or a machine that has not been set up yet. The entries above name files that "
                "exist here and nowhere else, so a copy of this file will fail to start them "
                "with 'no such file or directory'. Everything else in the file travels."
            )
            # Those paths are written as ${VARIABLE:-<absolute>}, so "it will not
            # travel" now has a remedy rather than only a warning: copy the
            # directory, export the variable. Saying which variable is the whole
            # point -- an operator who has to work it out from the file will not.
            # Read back what was *written*, rather than recomputing which roots
            # might apply. Those are different answers now that any absolute
            # path is named -- a PEM belonging to a third-party package gets a
            # variable from the env key holding it, which no root would have
            # predicted, and an operator told to export a variable nobody
            # mentioned is no better off than one told nothing.
            roots = {
                name: default
                for registration in bound
                for name, default in self._path_variables_in(registration.entry).items()
            }
            if roots:
                block["how_to_move_them_to_another_machine"] = [
                    f"copy {path} to the new machine, then export {variable}=<where you put it>"
                    for variable, path in sorted(roots.items())
                ]
                block["why_that_works"] = (
                    "Each of those paths is written into the entry as "
                    "${VARIABLE:-<the path it has here>}, which Claude Code expands in command, "
                    "args and env. Unset, it resolves to exactly what you see and this machine is "
                    "unaffected; set, it wins -- so the files can live anywhere on the next host "
                    "without editing the file."
                )
        elsewhere = [r for r in registrations if getattr(r.grounding, "kind", "") == SERVED_ELSEWHERE]
        if elsewhere:
            # The address in the entry is not the address you asked about. That
            # is normal for a hosted MCP service and it is still a hop somebody
            # should be able to see, so it is stated rather than left to be
            # noticed in `claude mcp list` one day.
            block["these_entries_are_served_from_another_host"] = {
                r.name: f"{r.candidate.url} -- {r.grounding.reason}" for r in elsewhere
            }
            block["why_that_is_expected"] = (
                "A vendor commonly serves MCP from a sibling domain: github.com is served from "
                "api.githubcopilot.com, developers.google.com from "
                "developerknowledge.googleapis.com. Each address above was probed and answered "
                "as an MCP endpoint, which is why it was believed -- but it is not the host you "
                "named, so it is worth a glance."
            )
        if refused:
            # Named here as well as in the report, because the briefing is the
            # thing an operator reads beside the file, and "the server I asked
            # for is not in here" is the first question it should answer.
            # Keyed by name -- and by candidate too where a fallback wrote
            # another candidate under the same name, so the two never collide.
            written = {r.name for r in registrations}
            block["these_were_found_but_not_registered"] = {
                (f"{r.name} ({r.candidate.label})" if r.name in written else r.name): (
                    r.refused + (f"; {r.replaced_by} was tried in its place" if r.replaced_by else "")
                )
                for r in refused
            }
            block["why_they_were_refused"] = (
                "Each of these answered, and each failed a check that has to pass before an "
                "entry is worth having -- it does not name the service it talks to, names a path "
                "that will not be there, does not start when launched as written, or was claimed "
                "for unrelated addresses. The reason is beside each. They are absent from "
                f"{self.display_path} rather than present and broken."
            )
        if install:
            block["install_commands"] = install
        if scripts:
            block["generated_scripts"] = scripts
        duplicates = [d for r in registrations for d in r.duplicates]
        if duplicates:
            block["duplicate_warning"] = (
                f"{len(duplicates)} entr(y/ies) already in {self.display_path} point at the same "
                "service as a server just added. Keep one and remove the other."
            )
            block["duplicates"] = duplicates
        block["how_to_use_this_file"] = self.usage_instructions()
        return block

    def generated_scripts(self, candidate: Candidate) -> list[str]:
        """Paths in a candidate's command that this run wrote itself.

        Only files under the configured bridge output directory count: a path to
        ``/usr/bin/python3`` is the interpreter, and a path into ``node_modules``
        belongs to a package the operator can reinstall. What matters here is the
        code that exists on exactly one machine.
        """
        output_dir = self.settings.path("mcp.bridge.output_dir")
        if output_dir is None:
            return []
        root = str(Path(output_dir).expanduser())
        return [token for token in candidate.full_command() if token.startswith(root) and Path(token).exists()]

    def _register(self, registration: Registration) -> None:
        claude = self.ctx.tool("claude", config_key="paths.claude")
        if not claude:
            registration.skipped = "the claude CLI is not installed"
            registration.remediation.append("Run phase 1 to install it, then re-run phase 4 to register the server.")
            return

        # What the CLI holds now, kept in memory: if the new entry cannot be
        # added after the old one was removed, the old one is put back -- an
        # address is never left with nothing because a replacement failed.
        # openqa-example-net's CLI copy was removed, the add was refused, and the
        # run reported it registered. (mcp.registration.restore_on_failure)
        before = self._cli_snapshot(registration.name)
        removed = False
        if (
            registration.name in self._existing(claude)
            and registration.replaces_unusable
            and not self._only_in_project_file(registration.name)
        ):
            # The CLI's copy is the registration this run proved unusable for
            # this account; take it out so the replacement is what loads.
            if self._remove_from_cli(claude, registration.name, f"replace {registration.name}") is not None:
                self._existing(claude).discard(registration.name)
                removed = True
            else:
                registration.remediation.append(
                    f"The claude CLI still holds the unusable {registration.name}; remove it -- "
                    f"`{self.remove_by_hand(registration.name)}` -- so the replacement in "
                    f"{self.display_path} is what loads."
                )
        if registration.name in self._existing(claude) and self._literal_cli_copy_replaced(registration, claude):
            self._existing(claude).discard(registration.name)
            removed = True
        if registration.name in self._existing(claude) and self._lost_files_cli_copy_replaced(registration, claude):
            self._existing(claude).discard(registration.name)
            removed = True
        if registration.name in self._existing(claude) and not self.registration.bool_("overwrite_existing", False):
            registration.skipped = "already registered"
            if not self.cli_scopes(registration.name):
                # `claude mcp list` shows project entries too: this one is the
                # file's own, and no CLI scope holds a copy. Claude Code loads it
                # from the file once the project's servers are approved.
                registration.note(
                    f"{registration.name} is in {self.display_path} (no claude CLI copy): Claude Code loads it from "
                    "there, and asks you to approve the project's servers the first time"
                )
            else:
                registration.note(f"{registration.name} is already known to the CLI")
            registration.registered = True
            self._report_shadowing(registration)
            return

        payload = json.dumps(registration.entry, separators=(",", ":"))
        argv = [
            self._expand(token, claude=claude, name=registration.name, json_payload=payload)
            for token in self.registration.list_("add_command", ["{claude}", "mcp", "add-json", "{name}", "{json}"])
        ]
        argv.extend(
            self._expand(token, claude=claude, scope=registration.scope)
            for token in self.registration.list_("scope_flag", ["--scope", "{scope}"])
        )
        if not argv:
            registration.skipped = "no registration command configured"
            return

        result = self.ctx.runner.run(
            argv,
            timeout=self.registration.float_("timeout_seconds", 90),
            label=f"register {registration.name}",
        )
        if result.simulated:
            registration.simulated = True
            registration.note(f"would register {registration.name} (scope {registration.scope})")
            return
        if result.ok:
            registration.registered = True
            registration.added_by_run = True
            registration.note(f"registered with the claude CLI (scope {registration.scope})")
            self._known = None  # the CLI's view changed
            return

        said = (result.stderr or result.stdout or "").strip()[-300:] or "registration failed"
        registration.note(said)
        registration.cli_failed = said
        if removed and before is not None and self._restore_cli(claude, registration.name, *before):
            registration.note(
                f"the CLI's earlier {registration.name} was put back, since the new one could not be added"
            )
        registration.remediation.append(
            f"Register it by hand: claude mcp add-json {registration.name} '<json>' "
            f"--scope {registration.scope}  (the entry is in the JSON report)"
        )

    def _cli_snapshot(self, name: str) -> tuple[dict[str, Any], list[str]] | None:
        """``(entry, scopes)`` the claude CLI holds under ``name`` now, from Claude Code's configuration; None for none."""
        if not self.registration.bool_("restore_on_failure", True):
            return None
        from .existing import ExistingRegistrations  # imports this module

        try:
            held = ExistingRegistrations.for_run(self.ctx).cli_config_entries().get(name)
        except Exception:  # noqa: BLE001 - a snapshot must never cost the registration
            return None
        return (dict(held[0]), list(held[1])) if held else None

    def _restore_cli(self, claude: str, name: str, entry: Mapping[str, Any], scopes: Sequence[str]) -> bool:
        """Put a removed CLI copy back where it was; its values are registered for redaction first, never logged."""
        for value in [*(entry.get("env") or {}).values(), *(entry.get("headers") or {}).values()]:
            redact.register(str(value))
        payload = json.dumps(dict(entry), separators=(",", ":"))
        restored = False
        for scope in scopes or [""]:
            argv = [
                self._expand(t, claude=claude, name=name, json_payload=payload)
                for t in self.registration.list_("add_command", ["{claude}", "mcp", "add-json", "{name}", "{json}"])
            ]
            if scope:
                argv += [
                    self._expand(t, scope=scope) for t in self.registration.list_("scope_flag", ["--scope", "{scope}"])
                ]
            result = self.ctx.runner.run(
                argv, timeout=self.registration.float_("timeout_seconds", 90), label=f"restore {name}"
            )
            restored = restored or bool(result.ok)
        if restored:
            self._known = None
        return restored

    # -- what Claude Code says about it ------------------------------------------

    def absorb_claude_check(self, registration: Registration, verdict: Check) -> None:
        """Act on Claude Code's own verdict instead of only printing it.

        This is the one check this program does not perform with its own
        client: ``claude mcp get <name>`` makes Claude Code connect exactly as
        the next session will. It is the closest thing to the truth available
        here -- and it was rendered into a table and consumed by nothing.

        Two servers were registered in a run where it said, in as many words,
        ``Claude Code reports: failed to connect``. The phase reported
        ``PARTIAL``, the entries were written, and the operator found out when
        the next session showed them as ``Connection closed``. A negative from
        the real client has to reach the report as a *finding about that
        server*, with something to do about it.

        The entry is not withdrawn. Discarding a server the client is momentarily
        unhappy with is how confluence.example.com was lost once already, and the
        file is the record of the run. What changes is that the run stops
        implying it worked.
        """
        registration.claude_check = verdict
        if verdict is None or verdict.ok is not False:
            return
        registration.note(f"Claude Code could not connect: {verdict.detail}")

        started = registration.start_check
        if started is not None and started.ok:
            # The sharpest diagnostic there is, and it is free: the entry runs
            # when *this* program launches it and not when the client does.
            # What differs between those two launches is the environment and
            # the working directory, so say that rather than leaving the
            # operator to compare two green ticks against one red one.
            registration.remediation.append(
                f"{registration.name}: the entry starts when this program launches it, and "
                f"Claude Code cannot connect to it ({verdict.detail}). The difference between "
                "those two launches is the environment and the working directory, so look "
                "there first: a variable exported in your shell but not in Claude Code's, a "
                "relative path, or a file that has moved since it was written. "
                f"`{self.get_command_for(registration.name)}` reproduces it."
            )
        else:
            registration.remediation.append(
                f"{registration.name}: Claude Code could not connect to this entry "
                f"({verdict.detail}). It is written to {self.display_path} so the run is not "
                "lost, but it will not work in the next session until this is resolved."
            )

    def start_check_row(self, registration: Registration) -> Check:
        """Did the finished entry start? One row, for the verification table.

        Every other row in that table is about the *candidate*. This one is
        about the entry, and the difference between them is where three servers
        were lost: each had been proven as a candidate and then written with an
        extra unset variable, so nothing that ran had ever run what shipped.

        ``-`` rather than ``no`` when there was nothing to launch -- a remote
        entry starts no process, and a dry run may not start one. Saying "no"
        there would be a finding about the server, and the truth is that nobody
        asked it.
        """
        shake = registration.start_check
        if shake is None:
            if registration.candidate.kind != "stdio":
                return Check("starts", None, "a remote entry starts no process")
            return Check("starts", None, "not launched here")
        if shake.ok:
            return Check("starts", True, "the entry as written completed a handshake")
        return Check(
            "starts",
            False,
            f"the entry as written did not start: {shake.error or 'no handshake'}",
        )

    def claude_check(self, registration: Registration) -> Check:
        """Ask the claude CLI itself whether the server it was given works.

        Everything else in the verification report is this program's own opinion,
        formed with its own client. This line is the second opinion that matters:
        ``claude mcp get <name>`` makes Claude Code connect to the server the way
        it will at the start of the next session, and print the verdict. When the
        CLI is not installed, or the entry was never registered, the check is
        reported as not performed -- never as a pass.
        """
        if registration.provisional and registration.needs_env:
            # Asking would spend a CLI round trip to be told what is already
            # known: the credential slot is empty, so the connection cannot
            # succeed. "Not performed, and here is why" beats a red "no" that
            # reads as though the endpoint were broken.
            return Check(
                "claude",
                None,
                "not asked: the entry's credential is still empty, so Claude Code cannot "
                f"connect to it until {', '.join(registration.needs_env)} is set",
            )
        if registration.provisional and registration.claude_usage:
            # The credential is in place (nothing left to set): what Claude Code
            # itself said is the answer, never "until  is set".
            usage = registration.claude_usage
            return Check("claude", usage.get("outcome") == "passed", str(usage.get("detail") or usage.get("outcome")))
        claude = self.ctx.tool("claude", config_key="paths.claude")
        if not claude:
            return Check("claude", None, "the claude CLI is not installed here")
        if not registration.registered:
            return Check("claude", None, "not registered, so Claude Code has nothing to check")

        argv = [
            self._expand(token, claude=claude, name=registration.name)
            for token in self.registration.list_("get_command", ["{claude}", "mcp", "get", "{name}"])
        ]
        if not argv:
            return Check("claude", None, "no status command configured (mcp.registration.get_command)")

        probe = self.ctx.runner.probe(argv, timeout=self.registration.float_("timeout_seconds", 90))
        if probe.simulated:
            return Check("claude", None, f"dry run: would run {' '.join(argv[1:])}")
        output = f"{probe.stdout}\n{probe.stderr}".lower()
        if not probe.ok and not output.strip():
            return Check("claude", False, "the claude CLI could not report on it")
        from .claude_check import status_verdict

        verdict = status_verdict(output, self.settings)
        said = next(
            (
                line.strip()
                for line in f"{probe.stdout}\n{probe.stderr}".splitlines()
                if status_verdict(line, self.settings) == verdict and line.strip()
            ),
            verdict,
        )
        said = re.sub(r"^\s*status:\s*", "", said, flags=re.IGNORECASE)
        if verdict == "failed":
            return Check("claude", False, f"Claude Code reports: {said} (failed, or connected with a warning)")
        if verdict == "connected":
            return Check("claude", True, f"Claude Code reports: {said}")
        # It answered, said neither, and did not error: the server is known to
        # Claude Code but this version does not report a connection state.
        return Check("claude", probe.ok, "Claude Code knows the server; it reported no connection state")

    def _existing(self, claude: str) -> set[str]:
        """Names the CLI already knows, so re-runs stay idempotent."""
        if self._known is not None:
            return self._known
        argv = [
            self._expand(token, claude=claude)
            for token in self.registration.list_(
                "list_command",
                [
                    "{claude}",
                    "mcp",
                    "list",
                ],
            )
        ]
        names: set[str] = set()
        if argv:
            probe = self.ctx.runner.probe(argv, timeout=self.registration.float_("timeout_seconds", 90))
            if probe.ok:
                for line in probe.stdout.splitlines():
                    head = line.split(":", 1)[0].strip()
                    # `claude mcp list` prints "name: command - status" lines.
                    if head and " " not in head:
                        names.add(head)
        self._known = names
        return names

    def tidy(self) -> list[str]:
        """Sweep prose an older version of this program left in ``.mcp.json``.

        :meth:`_write_json` does the same sweep, but only when there is
        something to write -- so a machine whose file was written before the
        briefing moved out of it keeps carrying that key for as long as
        discovery finds nothing new. That is precisely the file somebody copies
        into another project in the meantime, prose and all. So the sweep also
        runs on its own, at the start of every phase-4 run, whatever the run
        goes on to find.

        Returns the keys removed: empty when the file is absent, unreadable,
        already clean, or when nothing in it is ours to touch.
        """
        path = self.json_path
        if not path.is_file():
            return []
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            # Not ours to repair. The next publish rewrites it from scratch, and
            # until then an unreadable file is better left for its owner to see.
            log.debug("not tidying %s (%s)", path, exc)
            return []
        if not isinstance(loaded, Mapping):
            return []
        document = dict(loaded)
        removed = [
            key for key in self.settings.str_list("mcp.instructions.strip_keys", ["_instructions"]) if key in document
        ]
        if not removed:
            return []
        for key in removed:
            document.pop(key, None)
        if self.ctx.dry_run:
            log.info("[dry-run] would remove %s from %s", ", ".join(removed), path)
            return removed
        try:
            self._atomic_write(path, json.dumps(document, indent=2, sort_keys=False) + "\n")
        except OSError as exc:
            log.warning("could not tidy %s: %s", path, exc)
            return []
        log.info("removed %s from %s; it now holds configuration only", ", ".join(removed), path)
        return removed

    def _write_json(self, registrations: list[Registration]) -> None:
        path = self.json_path
        document: dict[str, Any] = {}
        if path.is_file():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(loaded, Mapping):
                    document = dict(loaded)
            except (OSError, ValueError) as exc:
                log.warning("could not read %s (%s); it will be rewritten", path, exc)

        servers = dict(document.get("mcpServers") or {})
        for registration in registrations:
            if not self.ctx.dry_run and self.registration.bool_("entry_files_beside_file", True):
                registration.entry = self.own_entry_files(registration.entry, registration.name)
            if registration.name in servers and not registration.had_previous:
                registration.had_previous = True
                registration.previous_entry = servers[registration.name]
            servers[registration.name] = registration.entry
        document["mcpServers"] = dict(sorted(servers.items()))

        # Nothing but configuration goes in. Earlier versions of this program
        # wrote the briefing here under `_instructions`; a file somebody copied
        # before upgrading still carries it, so the names those versions used
        # are swept out. Only those names -- a top-level key this program never
        # wrote is the operator's, and staying out of it is the whole point.
        for stale in self.settings.str_list("mcp.instructions.strip_keys", ["_instructions"]):
            document.pop(stale, None)

        text = json.dumps(document, indent=2, sort_keys=False) + "\n"
        if self.ctx.dry_run:
            for registration in registrations:
                registration.simulated = True
                registration.note(f"would be recorded in {self.display_path}")
            log.debug("dry run: %s not written", path)
            return

        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._atomic_write(path, text)
        except OSError as exc:
            log.warning("could not write %s: %s", path, exc)
            for registration in registrations:
                registration.remediation.append(f"Could not write {path}: {exc}")
            # Said at the end, beside the verdict: a run on a full disk left no
            # .mcp.json and no line saying why.
            reason = f"{exc}"
            if reason not in self.write_failures:
                self.write_failures.append(reason)
            if diskspace.is_out_of_space(exc, self.settings):
                self.ctx.state.setdefault("out_of_space", f"writing {self.display_path}: {exc}")
            return

        for registration in registrations:
            registration.written = True
            registration.note(f"recorded in {self.display_path}")

    @staticmethod
    def _atomic_write(path: Path, text: str) -> None:
        """Write via a temp file so a crash cannot truncate an existing config."""
        write_private(path, text)

    @staticmethod
    def _expand(token: Any, *, claude: str = "", name: str = "", scope: str = "", json_payload: str = "") -> str:
        text = str(token)
        for placeholder, value in (
            ("{claude}", claude),
            ("{name}", name),
            ("{scope}", scope),
            ("{json}", json_payload),
        ):
            if placeholder in text:
                text = text.replace(placeholder, value)
        return text


def _argv_of(entry: Mapping[str, Any]) -> list[str]:
    command = str(entry.get("command") or "").strip()
    args = [str(a) for a in (entry.get("args") or []) if str(a)]
    return ([command] if command else []) + args


def _service_key(entry: Mapping[str, Any]) -> str:
    """What service an entry talks to, for comparing two entries.

    For a remote server that is host and port: two entries reaching the same
    host over ``/mcp`` and ``/sse`` are two ways into one service, which is
    exactly the case worth warning about. For a local one it is the launch
    command with its arguments *and the hosts its configuration names*: the
    same package is one service per instance it is pointed at. By command
    alone, jira.example.com's ``uvx mcp-atlassian`` and sd.example.com's were called
    one service, and the report advised deleting the working one.
    """
    url = str(entry.get("url") or "").strip()
    if url:
        address = parse_address(url)
        host = (address.host or "").lower()
        return f"{host}:{address.port}" if host and address.port else host
    argv = _argv_of(entry)
    if not argv:
        return ""
    hosts = sorted(hosts_named([*argv, *(entry.get("env") or {}).values()]))
    return " ".join(argv) + (f" @ {','.join(hosts)}" if hosts else "")


def _same_service(one: Mapping[str, Any], other: Mapping[str, Any]) -> bool:
    """Do two entries serve the same thing? See :func:`_service_key`.

    Remote: the same host and port. Local: the same launch line, and -- when
    *both* name the hosts they reach -- the same hosts. An entry naming none
    cannot be told apart from any instance, so it still counts as the same.
    """
    if str(one.get("url") or "").strip() or str(other.get("url") or "").strip():
        return bool(_service_key(one)) and _service_key(one) == _service_key(other)
    if not _argv_of(one) or _argv_of(one) != _argv_of(other):
        return False
    mine, theirs = _entry_hosts(one), _entry_hosts(other)
    return not mine or not theirs or mine == theirs


def _shape_family(candidate: Candidate, variable: str) -> tuple[str, str]:
    """``(package, kind of variable)``: the same launch without its hosts, and the name minus its product word.

    ``uvx mcp-atlassian`` reads ``CONFLUENCE_PERSONAL_TOKEN`` and
    ``JIRA_PERSONAL_TOKEN``; both are ``PERSONAL_TOKEN`` of one package.
    """
    launch = " ".join(t for t in candidate.full_command() if not hosts_named([t]))
    parts = str(variable).upper().split("_")
    return launch, "_".join(parts[1:]) if len(parts) > 1 else str(variable).upper()


def _same_launch(one: Mapping[str, Any], other: Mapping[str, Any]) -> bool:
    """Do two entries start the same thing -- the same URL, or the same command line?

    Both read the same way first (:func:`_without_defaults`): ``claude mcp get``
    prints ``${ICA_MCP_UVX}`` for the file's ``${ICA_MCP_UVX:-/usr/bin/uvx}``,
    and the two were called different entries.
    """
    one, other = _without_defaults(one), _without_defaults(other)
    url_one, url_other = str(one.get("url") or "").strip(), str(other.get("url") or "").strip()
    if url_one or url_other:
        return url_one == url_other
    return _argv_of(one) == _argv_of(other)


def _without_defaults(entry: Mapping[str, Any]) -> dict[str, Any]:
    """``entry`` with every ``${NAME:-default}`` read as ``${NAME}`` -- how ``claude mcp get`` prints it."""

    def strip(value: Any) -> Any:
        if isinstance(value, str):
            return re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*):-[^}]*\}", r"${\1}", value)
        if isinstance(value, list):
            return [strip(v) for v in value]
        if isinstance(value, dict):
            return {k: strip(v) for k, v in value.items()}
        return value

    return strip(dict(entry or {}))


def _same_copy(one: Mapping[str, Any], other: Mapping[str, Any]) -> bool:
    """Are a CLI copy and the file's entry the same entry, both read the same way (:func:`_without_defaults`)?"""
    return _canonical(_without_defaults(one)) == _canonical(_without_defaults(other))


def _entry_hosts(entry: Mapping[str, Any]) -> set[str]:
    """Every host an entry reaches: its URL, or what its launch line and ``env`` name."""
    return set(hosts_named([entry.get("url") or "", *_argv_of(entry), *(entry.get("env") or {}).values()]))


#: Fallback for ``[mcp.registration] cli_scope_words``: the CLI scope a
#: ``claude mcp get`` "Scope:" line names ("Local config (private to you in
#: this project)", "User config (available in all your projects)").
CLI_SCOPE_WORDS = {"local": r"(?i)\blocal\b", "user": r"(?i)\buser\b"}


def parse_get_scope(text: str, label: str = "Scope") -> str:
    """The scope line (``mcp.discovery.reuse.get_fields.scope``) of a ``claude mcp get`` report; "" when none."""
    for raw in str(text or "").splitlines():
        found, sep, value = raw.strip().partition(":")
        if sep and found.strip().lower() == str(label or "Scope").strip().lower():
            return value.strip()
    return ""


#: Fallback for ``mcp.registration.credential_file_slots``: env keys naming a
#: credential *file* (beside every secret name) -- absent here, still a credential.
CREDENTIAL_FILE_SLOTS = (
    r"(^|_)SSH_KEY($|_)",
    r"(^|_)KEY_?FILE$",
    r"(^|_)(CLIENT_)?CERT(IFICATE)?(_FILE)?$",
    r"(^|_)IDENTITY(_FILE)?$",
    r"(^|_)PRIVATE_KEY(_PATH|_FILE)?$",
)
#: Fallback for ``mcp.verification.runtime_tuning.machine_variable_template``.
MACHINE_VARIABLE_TEMPLATE = "ICA_MCP_{SLUG}_{KEY}"
#: Fallback for ``mcp.registration.entry_files_max_bytes``: the most a file or
#: folder an entry names may weigh to be copied beside the project file.
ENTRY_FILES_MAX_BYTES = 50_000_000
#: Data files that may carry an execute bit and are still data, never a program.
DATA_SUFFIXES = (".py", ".js", ".mjs", ".json", ".pem", ".crt", ".yaml", ".yml", ".toml", ".txt")

#: An env key naming a file or folder -- its value is checked for existence.
LOCATION_KEY = re.compile(r"(^|_)(FILE|PATH|DIR|CERTS?|BUNDLE|MANIFEST|CONFIG|KEY_FILE)($|_)", re.IGNORECASE)


def _launch_signature(entry: Mapping[str, Any]) -> tuple[str, ...]:
    """What an entry launches, compared across copies: each token's last path part, defaults expanded, versions unpinned."""
    tokens = [str(entry.get("command") or entry.get("url") or ""), *[str(a) for a in entry.get("args") or []]]
    out = []
    for token in tokens:
        text = ENTRY_VARIABLE.sub(lambda m: m.group(2) or "", token)
        text = re.sub(r"(?<=.)(@v?\d[\w.\-]*|==[\w.\-]+)$", "", PurePosixPath(text).name if "/" in text else text)
        out.append(text)
    return tuple(out)


def _canonical(entry: Mapping[str, Any]) -> str:
    """An entry compared as Claude Code would run it: its type defaulted, keys sorted."""
    shaped = {k: v for k, v in dict(entry or {}).items() if v not in (None, "", [], {})}
    shaped.setdefault("type", "stdio" if shaped.get("command") else shaped.get("type", ""))
    return json.dumps(shaped, sort_keys=True)


def _credential_variables(entry: Mapping[str, Any]) -> set[str]:
    """The ``${VARIABLE}`` names an entry reads a credential from (``env`` and headers)."""
    found: set[str] = set()
    for block in (entry.get("env") or {}, entry.get("headers") or {}):
        for key, value in block.items():
            for match in ENTRY_VARIABLE.finditer(str(value)):
                variable = match.group(1)
                if is_secret_name(str(key)) or is_secret_name(variable) or holds_a_credential(str(key), str(value)):
                    found.add(variable)
    return found


#: The composer an entry starts when a slot must be built from parts at start-up
#: (McpWriter._compose_launch). Standard library only; it holds no value.
COMPOSER_SOURCE = '''#!/usr/bin/env python3
"""Set environment values composed from others, then run the server. Written by initialize-claude-ai.

  mcp_compose.py --set NAME=TEMPLATE [--set ...] -- COMMAND [ARGS...]

TEMPLATE: {VAR} is the value of VAR; {b64:USER:PASSWORD} is base64 of
"<USER's value>:<PASSWORD's value>". Nothing is stored or printed.
"""
import base64
import os
import re
import sys


def value(template):
    def one(match):
        kind, _, rest = match.group(1).partition(":")
        if kind == "b64" and ":" in rest:
            user, password = rest.split(":", 1)
            pair = os.environ.get(user, "") + ":" + os.environ.get(password, "")
            return base64.b64encode(pair.encode("utf-8")).decode("ascii")
        return os.environ.get(match.group(1), "")
    return re.sub(r"\\{([^{}]+)\\}", one, template)


def main(args):
    while len(args) >= 2 and args[0] == "--set":
        name, _, template = args[1].partition("=")
        os.environ[name] = value(template)
        args = args[2:]
    if args and args[0] == "--":
        args = args[1:]
    if not args:
        sys.stderr.write("mcp_compose: no command to run\\n")
        return 2
    os.execvp(args[0], args)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
'''


def _path_variables_file(ctx: RunContext) -> Path:
    return Path(ctx.state_dir) / ctx.settings.str_("mcp.registration.path_variables_file", "mcp-path-variables.json")


def recorded_path_variables(ctx: RunContext) -> dict[str, str]:
    """``{variable: path}`` this program named from a file's own name, on this machine."""
    try:
        data = json.loads(_path_variables_file(ctx).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {str(k): str(v) for k, v in data.items() if str(v).startswith("/")} if isinstance(data, dict) else {}


def record_path_variable(ctx: RunContext, variable: str, path: str) -> None:
    """Remember ``variable`` stands for ``path`` (a path, never a credential); nothing in a dry run."""
    if ctx.dry_run or not str(path).startswith("/"):
        return
    known = recorded_path_variables(ctx)
    if known.get(variable) == path:
        return
    known[variable] = path
    target = _path_variables_file(ctx)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(".tmp")
        temporary.write_text(json.dumps(known, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.chmod(temporary, 0o600)
        temporary.replace(target)
    except OSError as exc:
        log.debug("could not remember %s: %s", variable, exc)


__all__ = ["McpWriter", "Registration", "ENV_PLACEHOLDER", "record_path_variable", "recorded_path_variables"]
