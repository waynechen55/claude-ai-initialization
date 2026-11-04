"""Servers this machine already has registered, offered back as candidates.

A second run on a machine that already works was searching from nothing: the
``.mcp.json`` it wrote last time, and whatever the operator added with
``claude mcp add``, held a working entry for the very address being asked
about, and discovery neither looked at it nor could fall back to it when its
own search came up short. smelt.example.net had a working ``mcp-graphql`` entry and
ended a later run with nothing.

So every registration is read once per run -- from the project file and from
the ``claude`` CLI (``mcp.discovery.reuse.sources``) -- matched to an address by
host, and turned into a :class:`~.types.Candidate` of the tier it belongs to:

* a remote entry (``url``) is an endpoint -- tier 1;
* a local entry that launches a generated bridge
  (``mcp.bridge.generated.filename_template``, plus
  ``mcp.discovery.reuse.bridge_patterns``) is a bridge -- tier 3;
* any other local entry is a package -- tier 2.

Nothing is taken on trust. A reused candidate is verified, grounded and gated
exactly like one discovery found, at its own tier; being registered already is
where the idea came from, not evidence that it works.

**No credential is carried.** An entry may hold a literal token -- the operator
pasted one into ``claude mcp add`` -- and this program does not copy values. A
``${VAR}`` reference is kept as that reference; a literal is replaced by a
reference to a variable named by ``mcp.discovery.reuse.literal_env_template``,
and the candidate says which variable to export. The value never leaves the
reading of the file.

**...but the credential in use is checked.** The literal -- or the default of a
``${NAME:-…}`` whose ``NAME`` is unset -- is what Claude Code sends every time
it loads the entry. sd.example.com's entry held one that its service refused, and
the run tested only the exported variable, which was unset, so it said nothing
about the token that was failing. The value is kept in memory on the candidate
(:attr:`~.types.Candidate.in_use`, a :class:`~..core.secrets.Secret`, so it is
redacted from every log) for one read-only login call
(``mcp.verification.login_check.check_in_use``), and is still never written.
"""

from __future__ import annotations

import os
import re
import shlex
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from ..core.config import fill
from ..core.context import RunContext
from ..core.logging_setup import get_logger
from ..core.secrets import Secret
from .address import parse as parse_address
from .grounding import mentions_host
from .portability import ENTRY_VARIABLE, expand
from .secret_policy import (
    OPAQUE_MIN_CHARS,
    OPAQUE_MIN_ENTROPY,
    OPAQUE_MIN_RATIO,
    carries_inline_credential,
    credential_fingerprint,
    credential_parts,
    holds_a_credential,
    instance_bound,
    is_a_path,
    is_secret_name,
    looks_opaque,
    names_a_selector,
    remember_in_use,
)
from .types import Candidate, Stage, Transport, slugify

log = get_logger("mcp.existing")

_STATE_KEY = "mcp_existing_registrations"
_CREATE = threading.Lock()

#: The sources ``mcp.discovery.reuse.sources`` may name.
PROJECT_FILE, CLAUDE_CLI = "project_file", "claude_cli"


@dataclass
class Registration:
    """One registered server, as read -- never written back."""

    name: str
    entry: dict[str, Any]
    source: str
    #: Names of the variables whose literal value was dropped, for the note.
    dropped: list[str] = field(default_factory=list)
    #: Where the claude CLI keeps it -- ``user`` or ``local`` (``project`` is the
    #: file) -- every scope holding the name; "" when unknown.
    scopes: list[str] = field(default_factory=list)


class ExistingRegistrations:
    """Read once per run, shared by every address and both pipelines."""

    def __init__(self, ctx: RunContext) -> None:
        self.ctx = ctx
        self.settings = ctx.settings
        self._known_variables: dict[str, str] | None = None
        self._foreign: set[str] = set()
        #: Credential fingerprint -> the variable an existing entry reads for it; see variable_for.
        self._variables: dict[str, str] = {}
        self._list_text: str | None = None
        self._held: dict[str, tuple[dict[str, Any], list[str]]] | None = None
        #: Credential fingerprint -> {entry name: the hosts it reaches}; see shared_credentials.
        self._owners: dict[str, dict[str, set[str]]] = {}
        #: Fingerprints of instance-bound credentials (tokens, API keys): shared anywhere, reported.
        self._bound: set[str] = set()
        #: id(Secret) -> the slot it was read from (credentials_for), for a borrower to match parts.
        self._slot_of: dict[int, str] = {}
        #: ``(entry, variable)`` whose ``${NAME:-value}`` holds a credential as its
        #: default -- the value sits in the file; reported, never carried over.
        self.default_credentials: list[tuple[str, str]] = []
        self.config = ctx.settings.section("mcp.discovery.reuse")
        self._guard = threading.Lock()
        self._read: list[Registration] | None = None
        #: Which registration each reused candidate was built from, so an entry
        #: proved wrong can be retired from where it was read.
        self._origin: dict[int, tuple[Candidate, Registration]] = {}

    @classmethod
    def for_run(cls, ctx: RunContext) -> "ExistingRegistrations":
        with _CREATE:
            found = ctx.state.get(_STATE_KEY)
            if not isinstance(found, cls):
                found = ctx.state[_STATE_KEY] = cls(ctx)
            return found

    @property
    def enabled(self) -> bool:
        return self.settings.bool_("mcp.discovery.reuse_existing", True)

    # -- reading ---------------------------------------------------------------

    def registrations(self) -> list[Registration]:
        with self._guard:
            if self._read is None:
                self._read = self._load() if self.enabled else []
            return list(self._read)

    def registration_for(self, candidate: Candidate) -> Registration | None:
        """The registration ``candidate`` was built from, if it was built here."""
        with self._guard:
            pair = self._origin.get(id(candidate))
        return pair[1] if pair and pair[0] is candidate else None

    def forget(self, name: str) -> None:
        """Stop offering a registration that has been retired."""
        with self._guard:
            if self._read is not None:
                self._read = [r for r in self._read if r.name != name]

    def _load(self) -> list[Registration]:
        sources = self.config.str_list("sources", [PROJECT_FILE, CLAUDE_CLI])
        found: dict[str, Registration] = {}
        if PROJECT_FILE in sources:
            from .writer import McpWriter  # the writer imports the pipelines; import late

            for name, entry in McpWriter(self.ctx).existing_entries().items():
                found[name] = Registration(name, dict(entry), PROJECT_FILE)
        if CLAUDE_CLI in sources:
            # Claude Code's own configuration first: `claude mcp get` prints
            # ${NAME:-default} as ${NAME}, and build-example-net's working client
            # bridge was judged "${ICA_MCP_PYTHON3_13} is not installed" from
            # that print while `claude mcp list` said it was connected.
            held = self.cli_config_entries()
            names = [*self._cli_names(), *[n for n in held if n not in self._cli_names()]]
            for name in names:
                if name in found:
                    if name in held:
                        found[name].scopes = list(held[name][1])
                    continue  # the file says more than the CLI prints
                entry, scopes = held.get(name, ({}, []))
                entry = dict(entry) or self._cli_entry(name)
                if entry:
                    found[name] = Registration(name, entry, CLAUDE_CLI, scopes=list(scopes))
        log.debug("existing registrations: %s", ", ".join(found) or "none")
        # Learn every credential these entries use, before anything else reads
        # a candidate: a value equal to one is a credential under any name.
        for registration in found.values():
            scratch = Candidate()
            copy = Registration(registration.name, registration.entry, registration.source)
            for key, value in (registration.entry.get("env") or {}).items():
                self._secret_safe(scratch, copy, str(key), str(value), header=False)
            for key, value in (registration.entry.get("headers") or {}).items():
                self._secret_safe(scratch, copy, str(key), str(value), header=True)
        self._foreign.clear()
        return list(found.values())

    def cli_config_entries(self) -> dict[str, tuple[dict[str, Any], list[str]]]:
        """``{name: (entry exactly as Claude Code keeps it, [scopes])}`` from its own configuration.

        See :func:`claude_config_entries`; read once per instance.
        """
        if getattr(self, "_held", None) is not None:
            return self._held
        self._held = claude_config_entries(self.ctx)
        return self._held

    def _claude(self) -> str:
        return self.ctx.tool("claude", config_key="paths.claude") or ""

    def _cli(self, key: str, name: str = "") -> str:
        claude = self._claude()
        template = self.settings.list_(f"mcp.registration.{key}", [])
        if not claude or not template:
            return ""
        argv = [fill(str(token), {"claude": claude, "name": name}) for token in template]
        probe = self.ctx.runner.probe(argv, timeout=self.settings.float_("mcp.registration.timeout_seconds", 90))
        return probe.stdout if probe.ok and not probe.simulated else ""

    def _cli_names(self) -> list[str]:
        pattern = re.compile(self.config.str_("list_line_pattern", r"^(?P<name>[^\s:]+):\s.+\s-\s"))
        names: list[str] = []
        for line in self._cli("list_command").splitlines():
            match = pattern.match(line.strip())
            if match and match.group("name") not in names:
                names.append(match.group("name"))
        return names

    def cli_connected(self, name: str) -> bool:
        """Does ``claude mcp list`` report ``name`` as connected right now?

        Read with ``mcp.registration.connected_markers`` / ``failed_markers``,
        failures first, so "failed to connect" is never read as connected.
        """
        from .claude_check import status_verdict

        pattern = re.compile(self.config.str_("list_line_pattern", r"^(?P<name>[^\s:]+):\s.+\s-\s"))
        # `claude mcp list` connects to every server: asked once per run.
        if self._list_text is None:
            self._list_text = self._cli("list_command")
        for line in self._list_text.splitlines():
            match = pattern.match(line.strip())
            if not match or match.group("name") != name:
                continue
            return status_verdict(line.strip()[match.end() :], self.settings) == "connected"
        return False

    def cli_failed(self, name: str) -> bool:
        """Does ``claude mcp list`` report ``name`` as failing (``failed_markers``)?"""
        from .claude_check import status_verdict

        pattern = re.compile(self.config.str_("list_line_pattern", r"^(?P<name>[^\s:]+):\s.+\s-\s"))
        if self._list_text is None:
            self._list_text = self._cli("list_command")
        for line in self._list_text.splitlines():
            match = pattern.match(line.strip())
            if match and match.group("name") == name:
                return status_verdict(line.strip()[match.end() :], self.settings) == "failed"
        return False

    def cli_entry(self, name: str) -> dict[str, Any]:
        """What the CLI holds under ``name`` now, in ``.mcp.json`` shape."""
        return self._cli_entry(name)

    def _cli_entry(self, name: str) -> dict[str, Any]:
        """``claude mcp get NAME``, read back into the shape of a ``.mcp.json`` entry -- {} when the CLI holds no copy.

        ``claude mcp get`` reports project-scope entries too: "Scope: Project
        config (shared via .mcp.json)" is the file's own entry, never a copy
        the CLI holds. Read as one, build-opensuse-org's own entry was called
        "a different copy that takes precedence", and the run removed it.
        (``mcp.discovery.reuse.project_scope_patterns``.)
        """
        entry = parse_get(self._cli("get_command", name), self.config.dict_("get_fields", {}))
        scope = str(entry.pop("scope", "") or "")
        if scope and self.project_scope(scope):
            return {}
        return entry

    def project_scope(self, scope: str) -> bool:
        """Does a scope ``claude mcp get`` reports mean the project file (``project_scope_patterns``)?"""
        for pattern in self.config.str_list("project_scope_patterns", list(PROJECT_SCOPE_PATTERNS)):
            try:
                if pattern and re.search(pattern, scope):
                    return True
            except re.error:
                continue
        return False

    # -- matching --------------------------------------------------------------

    def for_target(self, target: str, stage: str) -> list[Candidate]:
        """Candidates for ``stage`` built from registrations that are about ``target``."""
        if not self.enabled:
            return []
        host = (parse_address(target).host or "").lower()
        if not host:
            return []
        out: list[Candidate] = []
        for registration in self.registrations():
            if self.tier_of(registration.entry) != stage:
                continue
            if not self._about(registration, host):
                continue
            if self._inline_secret(registration.entry):
                # An address or argument with a token inside it cannot be
                # rewritten as a reference without changing what it means, and
                # it cannot be carried. Named, never shown.
                log.info(
                    "%s: the registration %r carries a credential in its address or arguments; not reusing it",
                    target,
                    registration.name,
                )
                continue
            out.append(self.candidate(registration, stage))
        return out

    def credentials_for(self, host: str) -> list[Any]:
        """The credentials existing entries for ``host`` use right now, as Secrets -- distinct values only.

        For :meth:`.CandidateVerifier.check_in_use` to offer to a *new* server
        for the same host (``mcp.verification.login_check.borrow_in_use``): a
        bridge generated for sd.example.com asks for its own variable, and the
        token sd.example.com already receives from this machine is the one to try.
        Read the same way as :meth:`candidate` reads them; nothing is kept.
        """
        host = str(host or "").lower()
        if not host or not self.enabled:
            return []
        found: list[Any] = []
        seen: set[str] = set()
        for registration in self.registrations():
            if not self._about(registration, host):
                continue
            scratch = Candidate()
            copy = Registration(registration.name, registration.entry, registration.source)
            for key, value in (registration.entry.get("env") or {}).items():
                self._secret_safe(scratch, copy, str(key), str(value), header=False)
            for key, value in (registration.entry.get("headers") or {}).items():
                self._secret_safe(scratch, copy, str(key), str(value), header=True)
            self._foreign.clear()
            for slot, secret in scratch.in_use.items():
                value = secret.reveal() if hasattr(secret, "reveal") else str(secret)
                if value not in seen:
                    seen.add(value)
                    found.append(secret)
                    self._slot_of[id(secret)] = str(slot)
        return found

    def slot_of(self, secret: Any) -> str:
        """The slot (key) an existing entry carried ``secret`` under, as :meth:`credentials_for` read it; "" unknown."""
        return self._slot_of.get(id(secret), "")

    def supplied_values(self, host: str) -> dict[str, tuple[str, str]]:
        """``{variable: (value, where)}`` the existing entries for ``host`` supply today -- in memory only.

        What an entry sends for a ``${NAME:-value}`` while NAME is unset, and a
        literal under a key named like a variable: a user name, a setting, a
        credential alike. For the writer to keep it where Claude Code reads it
        when the new entry reads the same variable with no value of its own
        (McpWriter._keep_supplied_values). Never written, logged or reported.
        """
        host = str(host or "").lower()
        if not host or not self.enabled:
            return {}
        found: dict[str, tuple[str, str]] = {}
        for registration in self.registrations():
            if not self._about(registration, host):
                continue
            where = "the project's .mcp.json" if registration.source == PROJECT_FILE else "the claude CLI"
            for key, raw in (registration.entry.get("env") or {}).items():
                text = str(raw)
                for match in ENTRY_VARIABLE.finditer(text):
                    variable, default = match.group(1), match.group(2)
                    if default and not os.environ.get(variable):
                        found.setdefault(variable, (default, f"{registration.name!r} in {where}"))
                if text and not ENTRY_VARIABLE.search(text) and re.fullmatch(r"[A-Z][A-Z0-9_]*", str(key)):
                    found.setdefault(str(key), (text, f"{registration.name!r} in {where}"))
        return found

    def in_use_slots(self, host: str) -> list[str]:
        """The credential slots (keys) existing entries for ``host`` fill with a value right now -- names only.

        For the login plan: the kind of credential this machine already sends a
        host is the one it holds for it. sd.example.com's entry carried a
        personal access token while the generated bridge was given a password
        login, borrowed from another address, and never the token.
        """
        host = str(host or "").lower()
        if not host or not self.enabled:
            return []
        slots: list[str] = []
        for registration in self.registrations():
            if not self._about(registration, host):
                continue
            scratch = Candidate()
            copy = Registration(registration.name, registration.entry, registration.source)
            for key, value in (registration.entry.get("env") or {}).items():
                self._secret_safe(scratch, copy, str(key), str(value), header=False)
            for key, value in (registration.entry.get("headers") or {}).items():
                self._secret_safe(scratch, copy, str(key), str(value), header=True)
            self._foreign.clear()
            slots += [slot for slot in scratch.in_use if slot not in slots]
        return slots

    def _about(self, registration: Registration, host: str) -> bool:
        if registration.name == slugify(host):
            return True
        entry = registration.entry
        texts = [str(entry.get("url") or ""), str(entry.get("command") or "")]
        texts += [str(a) for a in entry.get("args") or []]
        texts += [str(v) for k, v in (entry.get("env") or {}).items() if not holds_a_credential(str(k), str(v))]
        # Whole hostname labels, never a substring: `build.example.com.evil.net`
        # and `notbuild.example.com` are not about `build.example.com`.
        return any(mentions_host(expand(text), host) for text in texts)

    @staticmethod
    def _inline_secret(entry: Mapping[str, Any]) -> bool:
        texts = [str(entry.get("url") or ""), *map(str, entry.get("args") or [])]
        if any(holds_a_credential("", text) for text in texts):
            return True
        return any(is_secret_name(m.group(1)) for text in texts for m in ENTRY_VARIABLE.finditer(text))

    def _loaded_copy(self, registration: Registration) -> bool:
        """Is ``registration`` the copy Claude Code loads under its name -- the CLI's, else the project file's?"""
        if registration.source != PROJECT_FILE:
            return True
        return not any(r.name == registration.name and r.source != PROJECT_FILE for r in self.registrations())

    def tier_of(self, entry: Mapping[str, Any]) -> str:
        if str(entry.get("url") or "").strip():
            return Stage.ENDPOINT
        launch = " ".join([str(entry.get("command") or ""), *map(str, entry.get("args") or [])])
        return Stage.BRIDGE if any(p.search(launch) for p in self._bridge_patterns()) else Stage.PACKAGE

    def _bridge_patterns(self) -> list[re.Pattern[str]]:
        patterns = [str(p) for p in self.config.str_list("bridge_patterns", [])]
        template = self.settings.str_("mcp.bridge.generated.filename_template", "mcp_bridge_{slug}.py")
        if template:
            patterns.append(re.escape(template).replace(re.escape("{slug}"), ".+"))
        out = []
        for pattern in patterns:
            try:
                out.append(re.compile(pattern))
            except re.error:
                log.warning("mcp.discovery.reuse.bridge_patterns: %r is not a valid pattern", pattern)
        return out

    # -- the candidate ---------------------------------------------------------

    def _program_variables(self) -> dict[str, str]:
        """This program's own path variables (see McpWriter.program_variables), read once."""
        if getattr(self, "_known_variables", None) is None:
            from .writer import McpWriter  # the writer imports the pipelines; import late

            self._known_variables = McpWriter(self.ctx).program_variables()
        return self._known_variables

    def _resolve_program_variable(self, name: str) -> str:
        from .writer import McpWriter  # the writer imports the pipelines; import late

        return McpWriter(self.ctx).resolve_program_variable(name)

    def _login_slots_from_manifest(self, candidate: Candidate, registration: Registration) -> None:
        """Each login variable its bridge's manifest declares, still empty here, is a credential to supply.

        build-example-net's osc signing bridge, read back from the entry that last
        worked, needs only BUILD_EXAMPLE_NET_USERNAME (and a key file); a user name
        is no secret by its name, so it launched with nothing, was refused, and
        was judged "not accepted" instead of waiting for its credential -- and
        the password bridge took its place. Generated, the same bridge had the
        slot as a credential; read back, it now has it again.
        (``mcp.discovery.reuse.manifest_login_slots``.)
        """
        from .traditional import manifest_login_slots

        if not self.config.bool_("manifest_login_slots", True):
            return
        _path, slots = manifest_login_slots(registration.entry)
        raw = dict(registration.entry.get("env") or {})
        for slot in slots:
            if slot not in raw or slot in candidate.secret_env or str(candidate.env.get(slot) or "").strip():
                continue
            match = ENTRY_VARIABLE.search(str(raw[slot]))
            candidate.secret_env[slot] = f"env:{match.group(1) if match else slot}"
            candidate.env.pop(slot, None)
            if ":-" in str(raw[slot]) and slot not in candidate.completed_slots:
                # Written as the optional ${NAME:-} it was: a round trip changes nothing.
                candidate.completed_slots.append(slot)

    def _api_kind_from_manifest(self, candidate: Candidate) -> str:
        """The API kind a reused generated bridge serves, read from its manifest's operations (:func:`.api_kinds.kind_of_manifest`)."""
        import json

        from . import api_kinds

        path = next((self._expand(str(v)) for k, v in candidate.env.items() if str(k).endswith("_MANIFEST") and v), "")
        if not path:
            return ""
        try:
            manifest = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return ""  # unreadable now; judged as it starts, and judged again if it is rewritten
        return api_kinds.kind_of_manifest(manifest, self.settings) if isinstance(manifest, dict) else ""

    def _borrows_identity(self, reference: str, value: str, name: str) -> bool:
        """Would ``reference`` make the entry's *instance* depend on a variable that is not the entry's own?

        A value naming a host says which instance the entry serves. Written as
        a reference to a generic variable -- ``BUGZILLA_SERVER``, which the
        operator's shell exported for bugzilla.opensuse.org -- the entry serves
        whatever that variable says tomorrow, and two instances of one product
        cannot both own it. So such a value stays in the entry, unless the
        variable is namespaced to the entry (``BUGZILLA_OPENSUSE_ORG_URL``).
        (``mcp.discovery.reuse.keep_instance_literal``.)
        """
        if not self.config.bool_("keep_instance_literal", True):
            return False
        from .grounding import hosts_named

        if not hosts_named([str(value)]):
            return False
        slug = re.sub(r"[^A-Za-z0-9]+", "_", str(name)).strip("_").upper()
        names = [m.group(1) for m in ENTRY_VARIABLE.finditer(str(reference))]
        return bool(names) and not all(slug and n.upper().startswith(slug) for n in names)

    def _names_operator_variable(self, text: str) -> bool:
        """Does ``text`` reference a variable the operator supplies -- not one of this program's own path variables?

        A program variable (``${ICA_MCP_BRIDGES:-...}``) stands for a path this
        program owns, and the writer derives it again from the path; anything
        else is the operator's setting, and its reference is what the entry says.
        """
        if not self.config.bool_("keep_operator_references", True):
            return False
        known = self._program_variables()
        names = [m.group(1) for m in ENTRY_VARIABLE.finditer(str(text))]
        return bool(names) and not any(name in known or self._resolve_program_variable(name) for name in names)

    def _expand(self, text: str) -> str:
        """Resolve a value as the client would -- and this program's own variables as it knows them.

        The environment first, then a ``${NAME:-default}``'s default, then, for
        a bare ``${ICA_MCP_PYTHON}``-style name this program defines, what it
        stands for here. Reuse used to leave those as written and report
        "``${ICA_MCP_PYTHON}`` is not installed" about an entry this program
        wrote. A resolved path that does not exist on this machine is noted:
        the entry came from somewhere else.
        """
        known = self._program_variables()
        # Claude Code expands an entry's ${NAME} from its settings env too: an
        # entry is read as Claude Code runs it. github-com's
        # ${ICA_MCP_GITHUB_MCP_SERVER:-/root/go/bin/...} was judged "not
        # installed" while the settings named the binary that is here.
        if self.settings.bool_("mcp.discovery.reuse.expand_from_claude_settings", True):
            from .claude_check import _claude_settings_env

            try:
                claude_env = _claude_settings_env(self.ctx)
            except Exception:  # noqa: BLE001 - a lookup never costs reading an entry
                claude_env = {}
        else:
            claude_env = {}
        prefix = self.settings.str_("mcp.registration.path_variable_prefix", "ICA_MCP")
        prefix = f"{prefix}_" if prefix else ""

        def one(match: re.Match[str]) -> str:
            name, default = match.group(1), match.group(2)
            if name in os.environ:
                return os.environ[name]
            if claude_env.get(name):
                return str(claude_env[name])
            if default is not None:
                if default.startswith("/") and not os.path.exists(default) and prefix and name.startswith(prefix):
                    # This program's own variable, its written default not on
                    # this machine: what it stands for here, when that exists.
                    resolved = known.get(name) or self._resolve_program_variable(name)
                    if resolved and os.path.exists(resolved):
                        return resolved
                return default
            if name not in known:
                # A name this program derived from a file (`/usr/bin/python3.13`
                # -> ICA_MCP_PYTHON3_13), read back without its default.
                resolved = self._resolve_program_variable(name)
                if resolved:
                    known[name] = resolved
            if name in known:
                if not os.path.exists(known[name]):
                    self._foreign.add(f"{name} ({known[name]})")
                return known[name]
            return match.group(0)

        return ENTRY_VARIABLE.sub(one, str(text))

    def candidate(self, registration: Registration, stage: str, *, record_origin: bool = True) -> Candidate:
        """A candidate for ``stage`` built from ``registration``.

        ``record_origin`` false builds one that is *not* a current registration
        -- the entry that last worked (:mod:`.known_good`) -- so nothing treats
        it as something to retire or keep in place.
        """
        self._foreign.clear()
        entry = registration.entry
        where = "the project's .mcp.json" if registration.source == PROJECT_FILE else "the claude CLI"
        candidate = Candidate(
            name=registration.name,
            origin=stage,
            source=f"already registered as {registration.name!r} ({where})",
            confidence=self.config.float_("confidence", 0.9),
        )
        if record_origin:
            candidate.existing_name = registration.name
            with self._guard:
                self._origin[id(candidate)] = (candidate, registration)
        if stage == Stage.ENDPOINT:
            candidate.kind = "url"
            candidate.url = self._expand(str(entry.get("url") or ""))
            kind = str(entry.get("type") or "").lower()
            candidate.transport = (
                Transport.SSE if kind == Transport.SSE else (Transport.STREAMABLE_HTTP if kind else "")
            )
            for header, value in (entry.get("headers") or {}).items():
                candidate.headers[str(header)] = self._secret_safe(
                    candidate, registration, str(header), str(value), header=True
                )
        else:
            candidate.kind = "stdio"
            candidate.transport = Transport.STDIO
            candidate.command = [self._expand(str(entry.get("command") or ""))]
            candidate.args = [self._expand(str(a)) for a in entry.get("args") or []]
            for key, value in (entry.get("env") or {}).items():
                kept = self._secret_safe(candidate, registration, str(key), str(value), header=False)
                if kept or str(key) not in candidate.secret_env:
                    candidate.env[str(key)] = kept
                if (
                    kept != str(value)
                    and str(key) not in candidate.secret_env
                    and self._names_operator_variable(str(value))
                    and not self._borrows_identity(str(value), kept, registration.name)
                ):
                    # Expanded to be launched; written back as the reference --
                    # also when it expanded to nothing. ${BUILD_EXAMPLE_NET_USERNAME:-}
                    # became "" here, no reference was kept, and the empty value
                    # was dropped when the entry was built: the slot vanished on
                    # every round trip, and so did every optional ${NAME:-} setting.
                    candidate.references[str(key)] = str(value)
                elif (
                    kept
                    and not ENTRY_VARIABLE.search(str(value))
                    and self.config.bool_("keep_operator_references", True)
                    and os.environ.get(str(key)) == str(value)
                    and not self._borrows_identity("${" + str(key) + "}", kept, registration.name)
                ):
                    # A literal that is exactly what the operator exports under
                    # that name -- an earlier expansion written down: the
                    # reference is what the entry means (compared, never shown).
                    candidate.references[str(key)] = "${" + str(key) + "}"
        if not candidate.api_kind:
            candidate.api_kind = self._api_kind_from_manifest(candidate)
        self._login_slots_from_manifest(candidate, registration)
        if registration.dropped:
            # ``secret_env`` maps where the value goes to ``env:VARIABLE``; the
            # operator exports the variable.
            variables = sorted({str(ref).partition("env:")[2] or str(key) for key, ref in candidate.secret_env.items()})
            candidate.add_note(
                f"the registration held a literal credential in {', '.join(registration.dropped)}; "
                "it is not carried over -- export " + ", ".join(variables) + " instead"
            )
        if self._foreign:
            candidate.add_note(
                f"it names {', '.join(sorted(self._foreign))}, which is not on this machine -- the entry "
                "was probably copied from another one; regenerate it here rather than repair it"
            )
            self._foreign.clear()
        # Asked last: reading the registrations again must not disturb what
        # this one's reading collected above.
        if (
            record_origin
            and self.config.bool_("worked_before_wins", True)
            and self.config.bool_("connected_counts_as_worked", False)
            and self._loaded_copy(registration)
        ):
            try:
                if self.cli_connected(registration.name):
                    candidate.worked_before = "Claude Code shows it connected on this machine"
            except Exception:  # noqa: BLE001 - asking the CLI must never cost the candidate
                pass
        candidate.add_note(candidate.source)
        return candidate

    def _secret_safe(
        self, candidate: Candidate, registration: Registration, key: str, value: str, *, header: bool
    ) -> str:
        """The value to keep for ``key``, with every credential turned into a reference.

        Returns the header value (a template, for a header) or the non-secret
        ``env`` value; a secret ``env`` value returns "" and lands in
        ``secret_env`` instead.
        """
        references = [m.group(1) for m in ENTRY_VARIABLE.finditer(value)]
        if not header and self.config.bool_("paths_are_settings", True) and self._names_a_location(value):
            # A path -- a key file, a bundle -- is where the server finds what
            # it needs, not the secret itself: launched with it, as Claude Code
            # will. build-example-net's ${…_OBS_SSH_KEY:-/root/.ssh/id_ed25519} was
            # withheld from its trial as a credential, the SSH-signing bridge
            # could not log in, and a server that never could took its place.
            return self._expand(value)
        secret = (
            holds_a_credential(key, value)
            or any(is_secret_name(r) for r in references)
            # A long random literal is a credential whatever the variable is
            # called: SMELT_EXAMPLE_NET_HEADERS carried one into .mcp.json because
            # "HEADERS" promises nothing. See secret_policy.looks_opaque.
            or (
                not references
                and looks_opaque(
                    value,
                    self.config.int_("opaque_value_min_chars", OPAQUE_MIN_CHARS),
                    self.config.float_("opaque_value_min_entropy", OPAQUE_MIN_ENTROPY),
                    self.config.float_("opaque_value_min_entropy_ratio", OPAQUE_MIN_RATIO),
                )
                and not names_a_selector(key)
            )
        )
        if not secret:
            return self._expand(value)
        if references:
            for match in ENTRY_VARIABLE.finditer(value):
                variable, default = match.group(1), match.group(2)
                slot = variable if header else key
                candidate.secret_env.setdefault(slot, f"env:{variable}")
                if (
                    default
                    and not default.startswith("/")
                    and (registration.name, variable) not in self.default_credentials
                ):
                    self.default_credentials.append((registration.name, variable))
                if default and not os.environ.get(variable):
                    # ${NAME:-value} with NAME unset: the default is the
                    # credential the client sends. Held for the login check
                    # only -- see Candidate.in_use.
                    self._in_use(candidate, slot, default, registration, header=header, variable=variable)
                elif os.environ.get(variable):
                    # What the client will send, from the environment: only its
                    # fingerprint is kept, to tell two instances apart.
                    self._own(registration, os.environ[variable], variable)
            return value if header else ""
        # A literal. The value is dropped here, and nowhere else sees it.
        name = slugify(registration.name).upper().replace("-", "_")
        slot = slugify(key).upper().replace("-", "_")
        if slot.startswith(name + "_") and len(slot) > len(name) + 1:
            # Already named after the entry (SMELT_EXAMPLE_NET_HEADERS in
            # smelt-example-net): the name is not said twice.
            slot = slot[len(name) + 1 :]
        variable = fill(
            self.config.str_("literal_env_template", "{NAME}_{KEY}"),
            {"NAME": name, "KEY": slot},
        )
        if key not in registration.dropped:
            registration.dropped.append(key)
        if header:
            candidate.secret_env[variable] = f"env:{variable}"
            scheme = value.split(" ", 1)[0] if " " in value.strip() else ""
            self._in_use(
                candidate,
                variable,
                value.split(" ", 1)[1] if scheme else value,
                registration,
                header=True,
                variable=variable,
            )
            return f"{scheme} ${{{variable}}}" if scheme else f"${{{variable}}}"
        candidate.secret_env[key] = f"env:{variable}"
        self._in_use(candidate, key, value, registration, header=False, variable=variable)
        return ""

    def _names_a_location(self, value: str) -> bool:
        """Is ``value`` -- as written, or its references' defaults -- an absolute path and nothing else?"""
        text = str(value or "").strip()
        if not text or carries_inline_credential(text):
            return False
        expanded = ENTRY_VARIABLE.sub(lambda m: m.group(2) or "", text).strip()
        if ENTRY_VARIABLE.search(text) and not all(m.group(2) for m in ENTRY_VARIABLE.finditer(text)):
            return False  # a reference with no default: what it holds is unknown
        return is_a_path(expanded)

    def _own(self, registration: Registration, value: str, key: str = "") -> None:
        """Remember, by fingerprint only, that ``registration`` sends ``value`` -- and its parts.

        A Basic credential is compared by its parts too: build-example-net sent
        ``Basic base64(user:password)`` while build-opensuse-org held the same
        user and password as two variables. One login cannot be two instances'.
        """
        hosts = self._hosts_of(registration.entry)
        if not hosts:
            return
        bound = instance_bound(key, self.settings) if key else False
        for part in credential_parts(value):
            self._owners.setdefault(credential_fingerprint([part]), {}).setdefault(registration.name, set()).update(
                hosts
            )
            if bound:
                self._bound.add(credential_fingerprint([part]))

    @staticmethod
    def _hosts_of(entry: Mapping[str, Any]) -> set[str]:
        """The hosts an entry reaches: in URLs, and named bare (``JIRA_HOST=sd.example.com``)."""
        from .grounding import hosts_named

        texts = [
            entry.get("url", ""),
            entry.get("command", ""),
            *(entry.get("args") or []),
            *[str(v) for v in (entry.get("env") or {}).values()],
        ]
        return set(hosts_named(texts)) | {
            str(t).split(":", 1)[0].lower()
            for t in texts
            if re.fullmatch(r"[A-Za-z0-9.-]+\.[A-Za-z]{2,}(:\d+)?", str(t).strip())
        }

    def shared_credentials(self) -> list[dict[str, Any]]:
        """Entries that send one *token or API key* to different hosts: ``{servers, hosts}``, never a value.

        One server issues a token, so the same one on two servers cannot be both
        of theirs. A user name and password is different: the same one may work
        on several servers (single sign-on), and is never reported -- a server
        that does not accept it says so in its own login check.
        """
        found: dict[tuple[str, ...], set[str]] = {}
        for fingerprint, owners in self._owners.items():
            if len(owners) < 2 or len({frozenset(h) for h in owners.values()}) < 2:
                continue
            if fingerprint not in self._bound:
                continue
            found.setdefault(tuple(sorted(owners)), set()).update(*owners.values())
        return [{"servers": list(names), "hosts": sorted(hosts)} for names, hosts in sorted(found.items())]

    def bound_elsewhere(self, secret: Any, host: str) -> list[str]:
        """Other hosts whose entries send this same *token or API key* -- an instance-bound credential; never a value."""
        value = secret.reveal() if hasattr(secret, "reveal") else str(secret or "")
        fingerprint = credential_fingerprint([value])
        if fingerprint not in self._bound:
            return []
        hosts = set().union(*self._owners.get(fingerprint, {}).values()) if self._owners.get(fingerprint) else set()
        return sorted(h for h in hosts if h != str(host).lower())

    def variable_for(self, secret: Any) -> str:
        """The variable an existing entry reads (or would read, once its literal is moved out) for this credential.

        So a new server proved with that credential is told to read the same
        variable: one export for one instance's token, not two names for it.
        """
        value = secret.reveal() if hasattr(secret, "reveal") else str(secret or "")
        return self._variables.get(credential_fingerprint([value]), "")

    def _in_use(
        self,
        candidate: Candidate,
        slot: str,
        value: str,
        registration: Registration,
        *,
        header: bool,
        variable: str = "",
    ) -> None:
        """Keep the credential the entry uses now, in memory, for the login check only.

        Its fingerprint is remembered regardless (secret_policy.remember_in_use):
        the same value under any other name -- an agent that copied it -- is
        then still a credential, and is never written into a file.
        """
        if not value.strip():
            return
        remember_in_use(value.strip())
        self._own(registration, value, slot)
        if variable:
            self._variables.setdefault(credential_fingerprint([value]), variable)
        if not self.settings.bool_("mcp.verification.login_check.check_in_use", True):
            return
        where = "the project's .mcp.json" if registration.source == PROJECT_FILE else "the claude CLI"
        candidate.in_use[slot] = Secret(
            value.strip(),
            source=f"the value written in the existing entry {registration.name!r} ({where})",
        )


def claude_config_entries(ctx: RunContext) -> dict[str, tuple[dict[str, Any], list[str]]]:
    """``{name: (entry exactly as Claude Code keeps it, [scopes])}`` from Claude Code's own configuration.

    User scope is the file's ``mcpServers``; local scope is the project's
    (this working directory's) ``mcpServers`` under ``projects``. The file is
    named by ``mcp.discovery.reuse.claude_config`` (``{config_dir}`` is
    ``$CLAUDE_CONFIG_DIR`` when set, else the home directory). Read only.
    """
    import json
    from pathlib import Path

    config = ctx.settings.section("mcp.discovery.reuse")
    held: dict[str, tuple[dict[str, Any], list[str]]] = {}
    variable = config.str_("claude_config_dir_variable", "CLAUDE_CONFIG_DIR")
    base = os.environ.get(variable, "") if variable else ""
    template = config.str_("claude_config", "{config_dir}/.claude.json")
    path = Path(fill(template, {"config_dir": base or str(Path.home())})).expanduser()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    if isinstance(data, Mapping):
        project = (data.get("projects") or {}).get(os.getcwd()) if isinstance(data.get("projects"), Mapping) else None
        for scope, block in (
            ("local", (project or {}).get("mcpServers") if isinstance(project, Mapping) else None),
            ("user", data.get("mcpServers")),
        ):
            for name, entry in (block or {}).items() if isinstance(block, Mapping) else ():
                if not isinstance(entry, Mapping):
                    continue
                if name in held:
                    held[name][1].append(scope)
                else:
                    held[str(name)] = (dict(entry), [scope])
    return held


def loaded_entries(ctx: RunContext) -> dict[str, tuple[dict[str, Any], str]]:
    """``{name: (entry, where)}`` for every MCP server Claude Code starts from this directory.

    The project file first (it says more than Claude Code's copy), then the
    local and user scopes of Claude Code's own configuration -- the same two
    readers :class:`ExistingRegistrations` uses, without asking the ``claude``
    CLI, so phase 1 can read them before Claude Code is even installed.
    """
    from .writer import McpWriter  # the writer imports the pipelines; import late

    found: dict[str, tuple[dict[str, Any], str]] = {}
    try:
        for name, entry in McpWriter(ctx).existing_entries().items():
            if isinstance(entry, Mapping):
                found[str(name)] = (dict(entry), PROJECT_FILE)
    except Exception as exc:  # noqa: BLE001 - an unreadable file is "no entries", never a crash
        log.debug("project file entries not read: %s", exc)
    for name, (entry, scopes) in claude_config_entries(ctx).items():
        found.setdefault(name, (entry, "/".join(scopes)))
    return found


#: Fallback for ``[mcp.discovery.reuse] project_scope_patterns``: the scope
#: ``claude mcp get`` reports for an entry that lives in the project's .mcp.json.
#: Anchored: "Local config (private to you in this project)" names a project too.
PROJECT_SCOPE_PATTERNS = (r"(?i)^\s*project\b", r"(?i)\.mcp\.json")


def parse_get(text: str, fields: Mapping[str, Any]) -> dict[str, Any]:
    """A ``claude mcp get`` report as an entry: ``type``, ``command``/``args`` or ``url``, ``env``, ``headers``.

    The labels it prints are configuration (``mcp.discovery.reuse.get_fields``)
    because they belong to whichever CLI version is installed. Values are held
    only long enough to be classified; see :meth:`ExistingRegistrations._secret_safe`.
    """
    labels = {str(v).lower(): str(k) for k, v in fields.items()}
    entry: dict[str, Any] = {}
    block = ""
    for raw in text.splitlines():
        if not raw.strip():
            block = ""
            continue
        indent = len(raw) - len(raw.lstrip())
        line = raw.strip()
        if block and indent >= 4:
            if block == "env" and "=" in line:
                key, _, value = line.partition("=")
                entry.setdefault("env", {})[key.strip()] = value.strip()
            elif block == "headers" and ":" in line:
                key, _, value = line.partition(":")
                entry.setdefault("headers", {})[key.strip()] = value.strip()
            continue
        label, sep, value = line.partition(":")
        key = labels.get(label.strip().lower()) if sep else None
        block = ""
        if key in ("env", "headers"):
            block = key
        elif key == "args":
            entry["args"] = shlex.split(value.strip()) if value.strip() else []
        elif key:
            entry[key] = value.strip()
    if not entry.get("url") and not entry.get("command"):
        return {}
    return entry


__all__ = [
    "CLAUDE_CLI",
    "PROJECT_FILE",
    "ExistingRegistrations",
    "Registration",
    "claude_config_entries",
    "loaded_entries",
    "parse_get",
]
