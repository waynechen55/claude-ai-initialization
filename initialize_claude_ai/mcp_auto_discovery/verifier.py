"""Verification -- the gate every candidate must pass.

"Agent should always verify found mcp server really work and can be connected."
That requirement is implemented here and nowhere else: no stage marks its own
work as successful.  A stage produces :class:`~initialize_claude_ai.mcp_auto_discovery.types.Candidate`
objects; this module attaches a :class:`~initialize_claude_ai.mcp_auto_discovery.types.Handshake` to each
one, and only a candidate whose handshake completed counts.

"Completed" is deliberately more than a 200. To be accepted a server must be
**connected** (``initialize`` negotiated a protocol version), must expose
something **callable** (tools, resources or prompts), and must answer a request
sent *after* the handshake on the session it opened -- the **two-way** check,
which is what tells a live server apart from an endpoint that can only say hello.
:func:`CandidateVerifier.audit` turns those into the report the phase prints, and
the writer adds a fourth line from Claude Code's own view of the registered
server. Each requirement is a switch in ``[mcp.verification]``.

Two safety rules apply to local (stdio) candidates:

* A trial launches third-party code, so it is a **mutating** action: it is
  skipped in dry-run mode and when ``mcp.packages.trial_run`` is off, and the
  candidate is reported as unverified rather than as working.
* Secrets are passed to the child process through the environment at launch, by
  reference, and never appear in the command line, the log or the report.

One method here judges something else entirely, and the distinction is the
point. :meth:`CandidateVerifier.verify_as_written` takes a finished
``.mcp.json`` **entry** and launches it exactly as the client will, with
``${NAME}`` resolved the way the client resolves it. Everything above tests the
candidate; the entry is a different object, built from it -- trialled with the
environment discovery assembled, shipped with placeholders that may resolve to
nothing. Three servers passed every candidate check and then exited on start-up
in the gap between the two, so the gap is now tested. See
:meth:`.McpWriter._check_it_starts`, which is where it is called from, and
:attr:`.Candidate.proven_without`, which is how the gap is kept narrow in the
first place.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import time
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence
from urllib.parse import urlsplit

from ..core import redact
from ..core.context import RunContext
from ..core.logging_setup import get_logger
from ..core.secrets import Secret
from ..core.trust_store import TrustStore
from . import login_check, trust
from .auth_profiles import profile_for
from .locks import INSTALL_STEPS
from .portability import ENTRY_VARIABLE, launch_of
from .probes import McpProber
from .provisioning import Provisioner
from .secret_policy import (
    carries_inline_credential,
    credential_fingerprint,
    holds_a_credential,
    instance_bound,
    is_secret_name,
    secret_part_prints,
)
from .types import Candidate, Handshake, Transport


def _job_done(shake: Handshake | None) -> set[str]:
    """The usage tasks and real calls a launch did -- what "doing as well" is measured by."""
    if shake is None:
        return set()
    done = {f"task:{r.get('task')}" for r in shake.contract if r.get("passed") is True}
    # A real call counts by its tool and whether it was answered, not its wording.
    answered = (login_check.ACCEPTED, login_check.EMPTY)
    done |= {
        f"call:{c.get('tool')}"
        for c in (shake.functional or [])
        if isinstance(c, Mapping) and c.get("verdict") in answered
    }
    return done


#: Fallback for ``[mcp.verification.runtime_tuning] restore_on``: how a call
#: fails when the network, not the server, is what a dropped runtime setting
#: had been tuning.
NETWORK_FAILURE_PATTERNS = (
    r"fetch failed",
    r"ETIMEDOUT",
    r"ECONNRESET",
    r"ECONNREFUSED",
    r"ENETUNREACH",
    r"EHOSTUNREACH",
    r"EAI_AGAIN",
    r"socket hang up",
    r"connect timeout",
    r"UND_ERR_CONNECT_TIMEOUT",
    r"network (is )?unreachable",
)


#: Fallback for ``[mcp.verification.runtime_tuning] runtime_option_variables``:
#: variables that tune a runtime rather than configure a server, matched whole.
RUNTIME_OPTION_VARIABLES = (
    "NODE_OPTIONS",
    "UV_THREADPOOL_SIZE",
    "JAVA_TOOL_OPTIONS",
    "_JAVA_OPTIONS",
    "JDK_JAVA_OPTIONS",
    "PYTHONOPTIMIZE",
    "PYTHONMALLOC",
    "GODEBUG",
    "GOMAXPROCS",
    "DOTNET_[A-Z_]+",
    "BUN_[A-Z_]+",
)

log = get_logger("mcp.verify")

#: Fallbacks for ``[mcp.packages.credential_probe]``, equal to the packaged values.
#: Names that carry *who* logs in: replaced with ``placeholder_user`` beside a
#: stand-in, removed for an anonymous call -- never a real user name beside a
#: fake password.
DEFAULT_IDENTITY_PATTERNS = (r"(^|_)(USER|USERNAME|USER_NAME|LOGIN|EMAIL|ACCOUNT)$",)
DEFAULT_PLACEHOLDER_USER = "initialize-claude-ai-stand-in-user"
#: Fallback for ``mcp.verification.public_call_login_markers``: a public call's
#: answer that is a login refusal -- the API asking for a login, not missing.
DEFAULT_PUBLIC_CALL_LOGIN_MARKERS = (
    r"\b40[13]\b",
    "must be authenticated",
    "unauthori[sz]ed",
    "authentication required",
)
#: Refused real logins per credential per run before it is not tried again.
DEFAULT_MAX_REFUSED_LOGINS = 2

#: Wordings that qualify a start-up crash as "it wanted a credential" when the
#: server did not name the variable itself. Defaults only --
#: ``mcp.packages.startup_credential.markers`` overrides them, because this is
#: whatever a third party chose to print and not anything this program controls.
#:
#: Kept narrow on purpose. A marker that also appears in ordinary breakage turns
#: every crash back into a credential wall, which is the mistake this whole
#: branch exists to avoid.
STARTUP_CREDENTIAL_MARKERS = (
    "missing required environment variable",
    "environment variable is required",
    "is required but not set",
    "no api key",
    "no token provided",
    "api key is required",
    "token is required",
    "credentials are required",
    "authentication required",
    "unauthorized",
    "401",
    "failed to get credentials",
    "no credentials",
    "password not found",
    "user not set",
    "username not set",
)


#: What the stand-in credential is replaced with in anything a server says back.
#: Not configuration: it is a label for a redaction, and the value it replaces
#: is the configurable part (``mcp.packages.credential_probe.placeholder``).
STAND_IN_LABEL = "<stand-in credential>"

#: How a candidate is told its verdict was reused rather than re-run. A prefix,
#: so the agent's retry brief can find it among the notes -- see
#: :func:`.agentic._correction`.
REUSED_VERDICT = "the same launch was already trialled for this target"


def _looks_like_a_command(word: str) -> str | bool:
    """Could this plausibly be the name of a program?

    Not a whitelist -- the answer has to hold for package managers nobody here
    has heard of. It only rules out what a program name cannot be: an empty
    word, or one carrying whitespace, quotes or shell punctuation, which is
    what prose looks like once it has been split on spaces.
    """
    return bool(word) and not any(ch in word for ch in " \t\"'|&;<>$`()")


def _saying(shake: Handshake) -> str:
    """The server's own words, for a message that says we read them.

    A verdict of "it did not say" is only believable if the operator can see
    what it *did* say; without this the report asserted the absence of evidence
    while showing none of it.
    """
    said = (shake.error or "").strip()
    return f" (it said: {said})" if said else " (it said nothing at all)"


#: The checks every endpoint is reported against, in the order they are shown.
#: Each one answers a different question, and an endpoint that passes the first
#: three has been *used*, not merely reached.
#: ``usable`` is here rather than only in :meth:`CandidateVerifier.audit`
#: because the table is built by *name* against this tuple. It used to be built
#: positionally, so a server that handshaked without its credential -- which
#: gets a fourth ``usable`` row -- put every mark from there on under the wrong
#: heading, and the column that said whether it worked was the one that slid.
#: A check that does not apply to a given server shows "-", as everywhere else.
#: Fallback for ``mcp.packages.credential_probe.volatile_patterns``, equal to it:
#: what differs between two launches of the same failure.
VOLATILE_PATTERNS = (
    r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(\.\d+)?([+-]\d{2}:?\d{2}|Z)?",
    r"time=\S+",
    r"(pid|PID)[=: ]\d+",
    r"0x[0-9a-fA-F]+",
    r"/tmp/\S+",
    r"rc=-?\d+",
)

#: ``authenticated`` is whether the credential was *accepted* -- one read-only
#: tool called with it (:mod:`.login_check`). "two-way" proves the channel;
#: only a call proves the service behind it takes the credential.
#: ``configured`` is whether the settings the server parses are in a form it
#: accepts -- every tool the check called answered without "misconfigured".
CHECKS = ("connected", "tools", "two-way", "usable", "authenticated", "configured", "schema", "starts", "claude")


@dataclass
class Check:
    """One named verification result, for the end-of-phase report."""

    name: str
    #: True passed, False failed, None could not be performed here.
    ok: bool | None
    detail: str = ""

    @property
    def mark(self) -> str:
        return {True: "yes", False: "no"}.get(self.ok, "-")

    def to_dict(self) -> dict[str, Any]:
        return {"check": self.name, "ok": self.ok, "detail": redact.redact(self.detail)}


#: Fallback for ``[mcp.verification.reuse_verdicts] try_later_markers``: what a
#: page in front of a server says when it asks to come back later.
TRY_LATER_MARKERS = (
    "not a bot",
    "checking your browser",
    "just a moment",
    "attention required",
    "/.within.website",
    "cf-chl",
    "challenge-platform",
    "ddos protection",
    "verify you are human",
)

#: ``ctx.state`` key: package -> the credential keys its written entries read.
_PACKAGE_CREDENTIALS = "package_credential_keys"


def _credential_keys_of(entry: Mapping[str, Any]) -> set[str]:
    """The keys of a stdio entry's env that carry a login (by name), selectors and session variables aside."""
    from .secret_policy import names_a_selector

    keys = set()
    for key in dict(entry.get("env") or {}):
        key = str(key)
        if is_secret_name(key) and not names_a_selector(key) and not re.search(r"(^|_)(HEADER|HEADER_NAME)$", key):
            keys.add(key)
    return keys


def record_package_credentials(ctx: Any, entry: Mapping[str, Any]) -> None:
    """Remember which credential keys a written entry's package reads (for :func:`sibling_credential`)."""
    from .env_contract import EnvContract

    if not isinstance(entry, Mapping) or not entry.get("command"):
        return
    argv = [str(entry.get("command") or ""), *[str(a) for a in entry.get("args") or []]]
    try:
        contract = EnvContract(ctx.settings)
        package = contract.package_of(argv)
    except Exception:  # noqa: BLE001 - a memory, never a reason to fail
        return
    keys = {k for k in _credential_keys_of(entry) if not contract.session(k)}
    if package and keys:
        ctx.state.setdefault(_PACKAGE_CREDENTIALS, {}).setdefault(package, set()).update(keys)


def sibling_credential(ctx: Any, argv: Sequence[str]) -> str:
    """The one credential key every other entry running the same package reads; "" when none, or several."""
    from .env_contract import EnvContract

    try:
        package = EnvContract(ctx.settings).package_of(list(argv))
    except Exception:  # noqa: BLE001
        return ""
    if not package:
        return ""
    keys = set(ctx.state.get(_PACKAGE_CREDENTIALS, {}).get(package, set()))
    try:
        from .existing import ExistingRegistrations

        for registration in ExistingRegistrations.for_run(ctx).registrations():
            entry = registration.entry or {}
            other = [str(entry.get("command") or ""), *[str(a) for a in entry.get("args") or []]]
            if EnvContract(ctx.settings).package_of(other) == package:
                keys.update(_credential_keys_of(entry))
    except Exception:  # noqa: BLE001 - evidence only
        pass
    return next(iter(keys)) if len(keys) == 1 else ""


class CandidateVerifier:
    """Attaches a proven-or-not verdict to candidates."""

    def __init__(self, ctx: RunContext, prober: McpProber | None = None) -> None:
        self.ctx = ctx
        self.prober = prober or McpProber(ctx)
        self.packages = ctx.settings.section("mcp.packages")
        self.discovery = ctx.settings.section("mcp.discovery")
        self.verification = ctx.settings.section("mcp.verification")
        self._delivery_tokens: Mapping[str, str] | None = None
        self.enabled = ctx.settings.bool_("mcp.verification.enabled", True)
        # The stand-in is not a secret, but it is registered with the redactor
        # anyway: it must never reach .mcp.json, the log or the report, and a
        # server that echoes it back ("invalid token <value>") would otherwise
        # carry it into all three through its own error message.
        redact.register(
            self.packages.section("credential_probe").str_(
                "placeholder", "initialize-claude-ai-stand-in-not-a-real-credential"
            )
        )

    # -- single candidate ------------------------------------------------------

    def verify(self, candidate: Candidate) -> Candidate:
        """Attach a handshake verdict to one candidate, in place.

        Two decisions, kept apart on purpose: the prober says whether the
        server answered, and :meth:`McpProber.accepts` says whether that answer
        satisfies the configured requirements. A handshake that worked but was
        rejected by policy is recorded as a failure *with the policy's reason*,
        never as a server that did not answer.
        """
        if not self.enabled:
            candidate.add_note("verification disabled by configuration")
            return candidate
        if candidate.kind == "url":
            self._prefer_https(candidate)
            candidate.handshake = self._verify_url(candidate)
            if not candidate.handshake.ok and candidate.handshake.requires_auth:
                # Refused anonymously -- and its existing entry may carry the
                # credential the client sends. See check_in_use.
                self._adopt_existing_slots(candidate)
                self.check_in_use(candidate, candidate.handshake)
        elif candidate.kind == "stdio":
            # Every path's candidate -- an agent's, a registry's, an existing
            # copy -- is given the credential its address holds, in the form
            # its server takes one, before it is judged.
            from . import api_kinds

            api_kinds.give_held_credential(self.ctx, candidate, self._host_of(candidate))
            self.use_launcher_form(candidate)
            slotted = self.slot_server_credential(candidate)
            candidate.handshake = self._verify_stdio(candidate)
            if not slotted and self.slot_server_credential(candidate):
                # Its source was on disk only once the trial had installed it:
                # judged again with the slot, so the credential is trialled
                # (withheld, then a stand-in) like any declared one.
                candidate.handshake = self._verify_stdio(candidate)
            self._without_runtime_tuning(candidate)
        else:
            candidate.handshake = Handshake(error=f"unknown candidate kind {candidate.kind!r}")

        self.credit_auth_claim(candidate)
        self._unverify_failed_calls(candidate.handshake)
        self._apply_contract(candidate)
        self._apply_job_refused(candidate)
        self._apply_browser_login(candidate)
        from . import api_kinds

        api_kinds.note_short_of_login(self.ctx, candidate)
        accepted, why = self.prober.accepts(candidate.handshake)
        if candidate.handshake and candidate.handshake.ok and not accepted:
            # The handshake worked but policy rejected it; be explicit about which.
            candidate.handshake.ok = False
            candidate.handshake.error = why
        candidate.add_note(why)
        return candidate

    def use_launcher_form(self, candidate: Candidate) -> bool:
        """Launch an installer's private launcher the portable way -- what is trialled is what is written.

        bugzilla-opensuse-org ran ``.../uv/tools/mcp-bugzilla/bin/mcp-bugzilla``:
        a script bound to that machine's tool environment, which no copy and no
        variable can carry. The same package by its launcher form
        (``uvx mcp-bugzilla==0.21.0``) starts anywhere. Applied to every path's
        stdio candidate -- agent, traditional, existing, repaired -- when the
        launcher it needs is here. (``mcp.launch_form``.)
        """
        from . import launch_form

        if candidate.kind != "stdio":
            return False
        command = candidate.full_command()
        portable, found = launch_form.launcher_form(command, self.ctx.settings, candidate.install)
        if not portable:
            return False
        if not self.ctx.runner.which(portable[0]):
            candidate.add_note(
                f"{command[0]} is {found['package']}'s private launcher from {found['family']}, which "
                f"only its own machine can run; {portable[0]}, its portable form, is not here to try"
            )
            return False
        candidate.command, candidate.args = [portable[0]], portable[1:]
        candidate.install = launch_form.without_installer_steps(candidate.install, found, self.ctx.settings)
        version = found.get("version") or "its newest release"
        candidate.add_note(
            f"launched as {' '.join(portable[:4])}: {command[0]} was {found['package']} ({version}) "
            f"through {found['family']}'s private launcher, which runs only beside its own install -- "
            "copied elsewhere it cannot start"
        )
        log.info(
            "%s: %s is %s's private launcher (%s) -- launched as %s instead",
            candidate.label,
            command[0],
            found["package"],
            found["family"],
            " ".join(portable[:4]),
        )
        return True

    def sends_a_login(self, candidate: Candidate) -> bool:
        """Has ``candidate`` any way to send a login: a declared slot, a credential setting, or one carried in a form?"""
        if candidate.secret_env or candidate.composed:
            return True
        for key, value in candidate.env.items():
            text = str(value or "")
            if is_secret_name(str(key)) or holds_a_credential(str(key), text):
                return True
            if any(is_secret_name(m.group(1)) for m in ENTRY_VARIABLE.finditer(text)):
                return True
        return False

    def server_credential(self, candidate: Candidate) -> tuple[str, list[str]]:
        """``(the variable the launched server reads its login from, every credential variable it reads)``.

        From its own code (:meth:`.EnvContract.all_reads` -- a script, or an
        installed package): gitlab.example.net's ``graphql-mcp-server`` reads
        ``API_ACCESS_TOKEN``, bugzilla.opensuse.org's ``mcp-bugzilla`` reads
        ``BUGZILLA_API_KEY``. One credential read is that variable; among
        several, the one naming the address's product (``product_tokens``);
        several still -- a user name and a password, or a choice of logins --
        is none: which one is the operator's to say, and it is reported.
        Selectors (``*_AUTH_MODE``) and session variables are never one.
        """
        from .env_contract import EnvContract
        from .registry_client import product_tokens
        from .secret_policy import carries_credentials, names_a_selector

        try:
            contract = EnvContract(self.ctx.settings)
            _reader, reads, _files = contract.all_reads(candidate.full_command())
        except Exception:  # noqa: BLE001 - reading a source must never cost the trial
            return "", []
        offered = sorted(
            n
            for n in reads
            if re.fullmatch(r"[A-Z][A-Z0-9_]*", n)
            and is_secret_name(n)
            and not names_a_selector(n)
            and not contract.session(n)
            and not contract.ignored(n)
            and n not in candidate.env
            and not re.search(r"(^|_)(HEADER|HEADER_NAME)$", n)
            # A carrier holds a form around the credential (HEADERS = a JSON
            # object): empty, mcp-graphql crashed. Its form is the API-kind
            # adapter's to write (credential_in_taken_form), never a bare slot.
            and not carries_credentials(n)
        )
        if len(offered) == 1:
            return offered[0], offered
        host = self._host_of(candidate)
        tokens = [t.upper() for t in product_tokens(host) if len(t) > 2] if host else []
        named = [n for n in offered if any(t in n for t in tokens)]
        return (named[0] if len(named) == 1 else ""), offered

    def slot_server_credential(self, candidate: Candidate) -> list[str]:
        """Give a candidate with no way to send a login the slot its own server reads one from; the keys added.

        gitlab-example-net and bugzilla-opensuse-org were written with no
        credential at all: each candidate declared none, both servers answered
        anybody, and nothing asked what they read a login from -- the package
        source said (``API_ACCESS_TOKEN``, ``BUGZILLA_API_KEY``). The slot joins
        :attr:`.Candidate.secret_env`, so it is trialled like a declared one
        (withheld, then a stand-in), proved optional when the server works
        without it, and written optional under a name of its entry's own -- an
        anonymous answer never costs the login. Every path's stdio candidate
        passes here. (``mcp.verification.server_credential``.)
        """
        config = self.verification.section("server_credential")
        if candidate.kind != "stdio" or not config.bool_("enabled", True) or self.sends_a_login(candidate):
            return []
        variable, offered = self.server_credential(candidate)
        sibling = ""
        if not variable and not offered and config.bool_("from_sibling_entries", True):
            # Its source says nothing (a binary, an image, a package not on
            # disk): the same package's entry for another address does --
            # bugzilla-example-com's mcp-bugzilla read BUGZILLA_API_KEY, and
            # bugzilla-opensuse-org's was written with no slot at all.
            variable = sibling = sibling_credential(self.ctx, candidate.full_command())
        if not variable:
            if len(offered) > 1 and config.bool_("report_ambiguous", True):
                note = (
                    f"its server reads a login from one of {', '.join(offered)}; which one is yours to say -- "
                    "none was written"
                )
                if note not in candidate.notes:
                    candidate.add_note(note)
            return []
        candidate.secret_env[variable] = f"env:{variable}"
        why = (
            "the same package's entry for another address reads its login from it"
            if sibling
            else "its server's own code reads its login from it"
        )
        candidate.add_note(
            f"{variable} added as its credential slot: {why}, and the "
            "candidate declared none -- written optional when it works without one, so the login is never lost"
        )
        log.info(
            "%s: the server reads its login from %s, which the candidate did not declare -- slot added",
            candidate.label,
            variable,
        )
        return [variable]

    def _prefer_https(self, candidate: Candidate) -> bool:
        """Move an ``http://`` endpoint to ``https://`` when the same endpoint answers there.

        A credential sent over plain HTTP is exposed, and services refuse it:
        openqa-example-net's entry was written as ``http://openqa.example.net/mcp`` from
        a copy registered under that URL, and the service answered "personal
        access token can only be used via HTTPS". The ``https://`` form is
        asked once; when it answers as an endpoint (a handshake, or a refusal
        that says one is there), the candidate uses it.
        (``mcp.verification.prefer_https``.)
        """
        url = str(candidate.url or "")
        if not url.lower().startswith("http://") or not self.verification.bool_("prefer_https", True):
            return False
        trial = copy.deepcopy(candidate)
        trial.url = "https://" + url[len("http://") :]
        shake = self._verify_url(trial)
        # It moves whenever https answers at all -- also with a page in front
        # asking to come back later: openqa.opensuse.org showed a bot check over
        # https, and its cleartext endpoint (behind a login) was written. Whether
        # a login is needed is often not known yet here; a credential must never
        # be the reason to find out over cleartext.
        answered = bool(getattr(shake, "status", 0)) or self.answered_try_later(shake)
        if not (
            shake.ok or shake.requires_auth or (answered and self.verification.bool_("https_when_it_answers", True))
        ):
            return False
        candidate.add_note(
            f"{url} moved to {trial.url}: the same endpoint answers over HTTPS, and a credential is "
            "never sent in the clear"
        )
        log.info("%s: moved to %s -- the same endpoint answers over HTTPS", url, trial.url)
        candidate.url = trial.url
        return True

    def _without_runtime_tuning(self, candidate: Candidate) -> list[str]:
        """Drop the runtime-tuning settings a candidate does not need: tried once without them, kept only if needed.

        A variable that tunes the runtime rather than configures the server
        (``NODE_OPTIONS``, ``JAVA_TOOL_OPTIONS`` -- ``runtime_option_variables``)
        fits the machine it was written on and travels in ``.mcp.json`` to
        every other. smelt-example-net carried ``NODE_OPTIONS=--network-family-
        autoselection-attempt-timeout=30000`` from an old entry: harmless on
        the machine that ran the program (5 s), 53 s or more on the next one,
        past Claude Code's 30 s start-up wait. The candidate is launched once
        without them; when it does as well -- it starts, offers the same tools,
        waits for the same credential -- and starts no slower, they are dropped.
        Returns what was dropped. (``mcp.verification.runtime_tuning``.)
        """
        config = self.verification.section("runtime_tuning")
        if not config.bool_("enabled", True) or candidate.handshake is None:
            return []
        names = [p for p in config.str_list("runtime_option_variables", list(RUNTIME_OPTION_VARIABLES)) if p]
        tuning = [k for k in candidate.env if any(re.fullmatch(p, str(k)) for p in names)]
        if not tuning:
            return []
        shake = candidate.handshake
        slack = config.float_("start_slack_seconds", 2.0)
        # One passing launch is not proof: smelt-example-net's NODE_OPTIONS was
        # dropped on one, and Claude Code's check 20 s later failed every call
        # with "fetch failed". It is dropped only when every one of
        # ``drop_samples`` launches without it does the job as well.
        samples = max(1, config.int_("drop_samples", 3))
        as_well, alt = True, shake
        for _ in range(samples):
            trial = copy.deepcopy(candidate)
            for key in tuning:
                trial.env.pop(key, None)
                trial.references.pop(key, None)
            alt = self._verify_stdio(trial)
            # "As well" is the job, not the handshake: smelt-example-net's mcp-graphql
            # started without NODE_OPTIONS on a machine whose network needed it for
            # every real call ("fetch failed"), the setting was dropped, and the
            # working entry was judged broken and replaced.
            job_with, job_without = _job_done(shake), _job_done(alt)
            knows_job = bool(job_with or job_without or shake.contract or alt.contract)
            as_well = alt.ok and (
                not shake.ok
                or (
                    bool(alt.credential_pending) == bool(shake.credential_pending)
                    and set(alt.tools) >= set(shake.tools)
                    and job_with <= job_without
                    and float(alt.start_seconds or 0.0) <= float(shake.start_seconds or 0.0) + slack
                )
            )
            if as_well and shake.ok and not knows_job and config.bool_("unknown_job_keeps", True):
                # Nothing showed what the server is for here: whether the setting
                # matters to its real calls cannot be told, so it stays -- per
                # machine, never in the travelling file (McpWriter.machine_settings).
                as_well = False
            if not as_well or not shake.ok:
                break
        if not as_well:
            candidate.add_note(
                f"{', '.join(tuning)} kept: without it the server did not do its job as well, or "
                "nothing showed its job -- kept for this machine only"
            )
            return []
        for key in tuning:
            candidate.dropped_tuning[key] = str(candidate.env.pop(key, ""))
            candidate.references.pop(key, None)
        candidate.handshake = alt
        candidate.add_note(
            f"{', '.join(tuning)} dropped: it starts and answers as well without "
            f"({float(alt.start_seconds or 0.0):.1f}s against {float(shake.start_seconds or 0.0):.1f}s with) -- "
            "a runtime setting fits the machine it was written on, and .mcp.json travels"
        )
        log.info("%s: %s dropped -- not needed here, and it travels with .mcp.json", candidate.label, ", ".join(tuning))
        return tuning

    def auth_claims(self, candidate: Candidate) -> list[str]:
        """Why anybody believes this URL wants a credential, in words; empty if nobody does."""
        auth = self.discovery.section("auth")
        reasons: list[str] = []
        if candidate.auth_claim.strip():
            reasons.append(candidate.auth_claim.strip())
        if candidate.kind == "url" and candidate.secret_env:
            reasons.append(f"it was proposed with a credential slot ({', '.join(sorted(candidate.secret_env))})")
        if auth.bool_("profile_is_auth_claim", True) and profile_for(self.ctx.settings, candidate.url):
            reasons.append("an auth profile in mcp.discovery.auth.profiles matches its host")
        return reasons

    def credit_auth_claim(self, candidate: Candidate) -> bool:
        """Treat an inconclusive anonymous reply as a credential wall when there is reason to.

        A 2xx that was not JSON-RPC is not a "no" -- :attr:`Handshake.inconclusive`
        -- and when an agent that read the documentation, the checklist, or an
        auth profile says this URL wants a credential, the likeliest reading of
        it is a front end answering for an endpoint we were not allowed into.
        That is registered exactly as a 401 with evidence is: provisionally,
        with an empty ``${VAR}`` slot, never counted as verified. Returns
        whether the verdict changed.
        """
        shake = candidate.handshake
        if candidate.kind != "url" or shake is None or shake.ok or shake.requires_auth:
            return False
        if not shake.inconclusive:
            return False
        if not self.discovery.section("auth").bool_("credit_inconclusive_with_claim", True):
            return False
        reasons = self.auth_claims(candidate)
        if not reasons:
            return False
        # A claim is a reason to look, never the evidence: the reply must differ
        # from what a made-up path on the same host gets. confluence.example.com
        # redirected /mcp to its login page -- as it redirects every path that
        # does not exist -- and "the discovery agent says it needs a credential"
        # turned the website into an MCP endpoint that closed every lower tier.
        differs = ""
        if self.discovery.section("auth").bool_("claim_needs_control", True):
            control = self.prober.control_answer(candidate.url)
            same = (
                bool(control)
                and control[0] == shake.status
                and (not control[1] or not shake.content_type or control[1] == shake.content_type)
            )
            if same:
                note = (
                    f"answers exactly like a path that does not exist (HTTP {control[0]}"
                    f"{', ' + control[1] if control[1] else ''}): not an MCP endpoint, whatever "
                    f"anybody says ({reasons[0][:120]})"
                )
                shake.add_hint(note)
                candidate.add_note(note)
                return False
            if control:
                differs = f"it answers unlike a path that does not exist (HTTP {shake.status} against {control[0]})"
        shake.requires_auth = True
        if differs:
            shake.endpoint_proof = differs
            shake.auth_evidence.append(differs)
        shake.auth_evidence.extend(f"believed to need a credential: {r}" for r in reasons)
        shake.add_hint(
            "the anonymous reply was inconclusive, and there is independent reason to believe "
            "this endpoint wants a credential"
        )
        shake.error = f"authentication required (inconclusive anonymous reply; {reasons[0]}): {shake.error}"
        return True

    def _verify_url(self, candidate: Candidate) -> Handshake:
        headers = {**self.auth_headers(), **self.resolved_headers(candidate.headers, candidate)}
        login = real_credential(headers)
        if candidate.transport == Transport.SSE:
            return self.judge_login(self.prober.probe_sse(candidate.url, headers=headers, login=login))
        wanted = bool(candidate.secret_env) or bool(candidate.auth_claim)
        result = self.prober.probe_http(
            candidate.url,
            headers=headers,
            login=login,
            login_tokens=self.login_tokens(candidate),
            contract=self.contract_plan(candidate) if (login or not wanted) else None,
        )
        if not result.ok and not result.requires_auth and candidate.transport == "":
            # Transport unknown and Streamable HTTP said no: the server may still
            # be running the deprecated SSE transport on the same URL.
            fallback = self.prober.probe_sse(candidate.url, headers=headers, login=login)
            if fallback.ok:
                return self.judge_login(_with_endpoint_proof(fallback))
        return self.judge_login(_with_endpoint_proof(result))

    def resolved_headers(self, headers: Mapping[str, Any], candidate: Candidate | None = None) -> dict[str, str]:
        """``headers`` with each ``${NAME}`` resolved as Claude Code will resolve it -- the launch tested is the one shipped.

        Claude Code expands a URL entry's header from the environment and from
        its settings ``env``; this program keeps credentials there
        (``mcp.registration.keep_credentials_in``). Probed with the reference
        unexpanded, openqa.example.net's entry -- ``Bearer ${OPENQA_EXAMPLE_NET_AUTHORIZATION}``,
        the value in settings -- went out anonymously, got 403, and stayed
        "waiting for a credential" forever. A header whose variable nothing
        resolves is left as written, so the probe stays anonymous as before.
        Every resolved value is redacted from everything said afterwards.
        """
        settings_too = self.verification.bool_("resolve_from_claude_settings", True)
        owned_of = self._owned_namer(candidate) if candidate is not None else None
        out: dict[str, str] = {}
        for key, value in headers.items():
            text = str(value)
            unresolved = False

            def one(match: re.Match[str]) -> str:
                nonlocal unresolved
                name, default = match.group(1), match.group(2)
                owned = owned_of(name) if owned_of else name
                if owned != name:
                    # What the written entry will read (resolve_owned): its own
                    # name; the generic one only from Claude Code's settings.
                    found = os.environ.get(owned) or self._settings_value(owned) or self._settings_value(name)
                else:
                    found = os.environ.get(name) or (self._settings_value(name) if settings_too else "")
                if found:
                    redact.register(found)
                    return found
                if default is not None:
                    return default
                unresolved = True
                return match.group(0)

            expanded = ENTRY_VARIABLE.sub(one, text)
            out[str(key)] = text if unresolved else expanded
        return out

    def credential_failure(self, shake: Handshake) -> str:
        """Name a failure that was the service refusing a real credential; the verdict, or "".

        A server handed the operator's token that dies saying "401
        Unauthorized", or ends at a login page or a customer portal, did not
        break -- its service refused the credential. Recorded as the login
        verdict, with the remedy, so the report says "rejected" rather than
        passing the error through as a server failure.
        """
        if login_check.mode(self.ctx.settings) == "off":
            return ""
        text = " ".join(part for part in (shake.error, *shake.protocol_hints) if part)
        verdict = login_check.classify_failure(text, self.ctx.settings)
        if verdict == login_check.MISCONFIGURED:
            # Not the credential: a setting the server parses at start-up is in
            # a form it rejects. Recorded beside the login verdict, for
            # _prove_address_forms to act on.
            shake.misconfigured = f"start-up: {redact.redact(' '.join(text.split())[:200])}"
            shake.add_hint(f"{login_check.in_words(verdict)} -- the failure names a setting, not the credential")
        elif verdict:
            shake.login, shake.login_detail = verdict, redact.redact(" ".join(text.split())[:200])
            shake.add_hint(f"{login_check.in_words(verdict)} -- the failure came from its service, not the server")
        return verdict

    def shape_problems(self, candidate: Candidate, values: Mapping[str, Any]) -> list[dict[str, str]]:
        """``{name, remedy}`` for each value that does not fit its slot; [] when all fit or none is judged."""
        from . import value_shapes

        problems: list[dict[str, str]] = []
        method: Mapping[str, str] | None = None
        for name, value in values.items():
            text = value.reveal() if hasattr(value, "reveal") else str(value if value is not None else "")
            problem = value_shapes.mismatch(str(name), text, self.ctx.settings)
            if problem is None:
                continue
            if method is None:
                method = self.auth_method(candidate)
            hosts = self.login_tokens(candidate)
            exported = candidate.secret_env.get(str(name), "")
            exported = exported.partition("env:")[2] if exported.startswith("env:") else str(name)
            remedy = value_shapes.remedy(problem, method, hosts.get("host", ""), exported)
            problems.append({"name": str(name), "remedy": remedy})
            log.info("%s: %s", candidate.label, remedy)
        return problems

    def auth_method(self, candidate: Candidate) -> dict[str, str]:
        """How the candidate's service takes a credential, from evidence; {} when nothing says.

        The API description the candidate itself names (an env value or
        argument that is a URL to one, ``mcp.discovery.auth.spec_value_patterns``)
        first, then the 401 challenge of the addresses it names. Remembered per
        run, so one service is asked once.
        """
        from . import apispec, bridge_auth
        from .grounding import hosts_named

        texts = [str(v) for v in [candidate.url, *candidate.full_command(), *candidate.env.values()] if v]
        key = ",".join(sorted(hosts_named(texts)))
        cache = self.ctx.state.setdefault("mcp_auth_methods", {})
        if key in cache:
            return dict(cache[key])
        patterns = self.discovery.section("auth").str_list(
            "spec_value_patterns", [r"\.(ya?ml|json)(\?|$)", r"/(openapi|swagger|apidocs?)(/|\b)"]
        )
        timeout = self.discovery.float_("per_request_timeout_seconds", 8)
        method: dict[str, str] = {}
        urls = [t for t in texts if t.startswith(("http://", "https://"))]
        with self.ctx.http(timeout=timeout) as client:
            for url in urls:
                if not any(re.search(p, url, re.IGNORECASE) for p in patterns):
                    continue
                try:
                    response = client.get(url)
                except Exception:  # noqa: BLE001 - evidence is optional
                    continue
                spec = apispec.parse(getattr(response, "text", "") or "") if getattr(response, "ok", False) else None
                if spec:
                    method = bridge_auth.method_from_spec(spec)
                if method:
                    break
            for url in urls if not method else []:
                try:
                    response = client.get(url)
                except Exception:  # noqa: BLE001
                    continue
                challenge = (getattr(response, "headers", None) or {}).get("www-authenticate", "")
                if getattr(response, "status", 0) == 401 and challenge:
                    method = bridge_auth.method_from_challenge(challenge, self.ctx.settings)
                    if method:
                        break
        cache[key] = dict(method)
        return method

    def login_tokens(self, candidate: Candidate) -> dict[str, str]:
        """``{host}`` and ``{product}`` for filling a check call's arguments (``login_check.arguments``)."""
        from .grounding import hosts_named
        from .registry_client import product_tokens

        values = [candidate.url, *candidate.full_command(), *candidate.env.values()]
        hosts = hosts_named(values)
        # A bare host is an address too (OSC_MCP_API=api.opensuse.org).
        hosts += [urlsplit(f"//{v}").hostname or "" for v in values if _bare_host(str(v))]
        hosts = [h for h in hosts if h]
        if not hosts:
            return {}
        words = [t for t in product_tokens(hosts[0]) if len(t) > 2]
        return {"host": hosts[0], **({"product": words[0]} if words else {})}

    def judge_login(self, shake: Handshake) -> Handshake:
        """Let a refused login count, per ``mcp.verification.login_check.mode``.

        ``pending`` makes a server whose credential was refused, or whose account
        has no access, *wait for a working credential* -- registered with its
        slot, never counted as working, the remedy named. ``warn`` records the
        verdict and changes nothing else. One place, so a candidate trial, an
        entry launched as written and a re-check cannot disagree.
        """
        answered = shake.ok or shake.requires_auth
        if (
            answered
            and shake.login in login_check.REFUSED
            and login_check.nothing_sent(shake)
            and self.verification.section("login_check").bool_("refusal_needs_a_sent_credential", True)
        ):
            # Nothing was sent, so nothing was refused: it waits for its
            # credential like any server asked without one -- never "rejected".
            shake.login_reason = login_check.NOTHING_SENT
            if not login_check.credential_missing(shake.login_detail, self.ctx.settings):
                shake.login_detail = f"no credential was sent ({shake.login_detail or 'refused'})"
            return shake
        if answered and shake.login in login_check.REFUSED and login_check.enforced(self.ctx.settings):
            shake.login_refused = True
            shake.add_hint(
                f"{login_check.in_words(shake.login)} when {shake.login_tool} was called"
                + (f" ({shake.login_detail})" if shake.login_detail else "")
            )
        return shake

    def _provisioned(self, command: str, candidate: Candidate, reason: str) -> str:
        """Install the catalog toolchain behind a missing ``command``; its path, or "".

        Reached only when ``command`` is not on this machine, and only when a
        trial would follow -- with trials off or in a dry run there is nothing
        to install it for. "" leaves the candidate to be rejected exactly as it
        always was; see :mod:`.provisioning`.
        """
        if not self.packages.bool_("trial_run", True) or self.ctx.dry_run:
            return ""
        return Provisioner.for_run(self.ctx).ensure(command, reason=reason, server=candidate.name)

    def _unusable_launcher(self, command: str) -> str:
        """Why the catalog component behind ``command`` cannot work here; "" when it can, or nothing says.

        Asks the component's ``usable`` checks (:func:`..install.verify.usable`,
        once per run, shared with phase 1). gitlab.example.net's container
        candidate was rejected as "not accepted" with Docker's daemon stopped;
        that says nothing about the server. (``mcp.packages.check_launcher_usable``.)
        """
        if not self.packages.bool_("check_launcher_usable", True):
            return ""
        from ..install.verify import usable

        component = Provisioner.for_run(self.ctx).provider(command)
        if component is None:
            return ""
        works, words = usable(self.ctx, component)
        return "" if works else f"{command} is installed but not usable here: {words}"

    def _ensure_required(self, candidate: Candidate) -> str:
        """Install what the server runs at call time (:attr:`Candidate.requires`); why it is missing, or "".

        The launch command starting proves nothing about a program the server
        calls later: a client bridge started on a machine without ``osc``,
        listed its tools, and every call failed. Each missing program is
        installed the way phase 1 knows (:meth:`_provisioned`), else by its
        listed ways in order -- those whose installer is present -- until it is
        on the PATH. Still missing, the candidate does not start: it is broken
        and says what to install, never "waiting for a credential".
        (``mcp.discovery.clients_install_missing``.)
        """
        from .client_bridge import install_with_ways, requirements

        for program, ways in requirements(candidate, self.ctx.settings).items():
            if self.ctx.runner.which(program):
                continue
            tried: list[str] = []
            if self.ctx.settings.bool_("mcp.discovery.clients_install_missing", True):
                if self._provisioned(program, candidate, "a program it runs"):
                    continue
                if self.packages.bool_("trial_run", True) and not self.ctx.dry_run:
                    tried = install_with_ways(
                        self.ctx, program, ways, timeout=self.packages.float_("trial_timeout_seconds", 90) * 4
                    )
            if self.ctx.runner.which(program):
                continue
            listed = "; ".join(" ".join(w) for w in ways) or "none configured"
            return (
                f"{program}, which this server runs for every call, is not installed"
                + (f" and could not be installed (tried: {'; '.join(tried)})" if tried else "")
                + f" -- install it ({listed}), then re-run phase 4"
            )
        return ""

    def _verify_stdio(self, candidate: Candidate) -> Handshake:
        """Trial a local candidate; then check the credential its existing entry uses, if any."""
        trial: dict[str, Any] = {}
        self._align_slots_to_source(candidate)
        shake = self._trial_stdio(candidate, trial)
        shake = self._retry_slow_start(candidate, shake, trial)
        if trial.get("launch") is not None:
            self.check_in_use(candidate, shake, trial)
            self._anonymous_check(candidate, shake, trial)
            self._account_check(candidate, shake, trial)
            if shake.login_credential and candidate.in_use:
                log.info(
                    "%s: the credential it was offered (%s) -- %s%s",
                    candidate.label,
                    shake.login_source or "in use",
                    login_check.in_words(shake.login),
                    f" ({shake.login_reason})" if shake.login_reason else "",
                )
        if shake.misconfigured and not trial.get("forms"):
            return self._prove_address_forms(candidate, shake, trial)
        if shake.login in login_check.REFUSED and not trial.get("forms") and self._points_off_api_host(candidate):
            # Refused -- but the address setting names a host that is not the
            # one serving the address's API, where the login was offered:
            # osc-mcp with OSC_MCP_API=https://build.opensuse.org got 401 for a
            # good openSUSE login, which only api.opensuse.org knows. The host is
            # tried before the credential is blamed; only API hosts, once each.
            return self._prove_address_forms(candidate, shake, trial, refused=True)
        if (
            shake.credential_pending
            and shake.login != login_check.ACCEPTED
            and not trial.get("forms")
            and self.verification.section("address_forms").bool_("try_api_hosts_when_waiting", True)
            and self._sends_login_off_api_host(candidate)
        ):
            # Waiting for its login -- which its address setting would send to a
            # host that never asked for it. build-opensuse-org's bridge sent Basic
            # to build.opensuse.org (MCP_BRIDGE_BASE_URL; refused) while only
            # api.opensuse.org asks for it: with no credential on the run machine
            # nothing was refused, so the refusal trial above never ran. The API
            # host is tried and kept when the server does its job there as well.
            return self._prove_address_forms(candidate, shake, trial, waiting=True)
        return shake

    def _sends_login_off_api_host(self, candidate: Candidate) -> bool:
        """Do the hosts it sends its calls to (:func:`.evidence.login_hosts`) miss every API host recorded for them?"""
        from .evidence import login_hosts

        sends_to = login_hosts(candidate, self.ctx.settings)
        apis = {h for host in sends_to for h in self._api_hosts_asking(candidate, host)}
        return bool(apis) and not (sends_to & apis)

    def _api_hosts_asking(self, candidate: Candidate, host: str) -> list[str]:
        """The API hosts recorded for ``host`` that asked for the login ``candidate`` sends; [] when its login is unknown.

        build-example-net's password bridge was moved to api.example.net, which asks
        for Signature only: "the API host" is not enough, it must be the one
        asking for this login. Without a record of what each asked, every API
        host recorded for ``host`` counts.
        """
        from .evidence import scheme_of

        api = [str(h) for h in self.ctx.state.get("mcp_api_hosts", {}).get(host, [])]
        asked = self.ctx.state.get("mcp_api_logins", {}).get(host)
        if asked is None:
            return api
        scheme = scheme_of(candidate, self.ctx.settings)
        return [h for h in api if scheme and scheme in [str(x).lower() for x in asked.get(h, [])]] if scheme else []

    def _points_off_api_host(self, candidate: Candidate) -> bool:
        """Does an address-valued setting name a host whose API is served by another host this run recorded?"""
        if not self.verification.section("address_forms").bool_("try_api_hosts", True):
            return False
        api_hosts = self.ctx.state.get("mcp_api_hosts", {})
        return any(_api_hosts_for(str(value), api_hosts) for value in candidate.env.values())

    def contract_plan(self, candidate: Candidate) -> dict[str, Any] | None:
        """The usage tasks for ``candidate`` (:mod:`.contract`), and how to read the target directly; None if none."""
        from . import contract

        host = self._contract_host(candidate)
        job = self.address_job(candidate, host)
        candidate.judged_job = tuple(job or ())
        tasks = contract.tasks_for(self.ctx.settings, host, job)
        if candidate.recheck_tasks is not None:
            wanted = set(candidate.recheck_tasks)
            tasks = [t for t in tasks if str(getattr(t, "name", "") or "") in wanted]
        if not tasks:
            return None
        return {"tasks": tasks, "host": host, "truth": (lambda spec: self._read_truth(host, spec)) if host else None}

    @staticmethod
    def _contract_host(candidate: Candidate) -> str:
        """The host a candidate's usage tasks are about: one it names in a URL, else one it names bare."""
        from .grounding import hosts_named

        host = next(iter(hosts_named([candidate.url, *candidate.full_command(), *candidate.env.values()])), "")
        if not host:
            host = next((str(v).split(":", 1)[0].lower() for v in candidate.env.values() if _bare_host(str(v))), "")
        return host

    def address_job(self, candidate: Candidate, host: str) -> list[str] | None:
        """What the address ``candidate`` serves is -- product and API kinds -- as the pipeline recorded it; None if unknown.

        Recorded per host in ``ctx.state["mcp_address_job"]`` (the address and
        every host serving its APIs) by the pipeline, before its candidates are
        asked anything.

        Asked here, where every verdict is reached, rather than trusted to every
        path that leads here: when nothing is recorded yet, the learner the
        address's pipeline registered (``ctx.state["mcp_job_learners"]``, see
        :meth:`.TraditionalDiscovery.begin_stage`) is asked first. Agent mode's
        fallback package stage reached the verifier without the job, so
        gitlab.example.net's mcp-graphql was verified on "describe the API" alone.
        (``mcp.verification.learn_job_when_judging``.)
        """
        from .grounding import hosts_named

        hosts = {
            h.lower()
            for h in [host, *hosts_named([candidate.url, *candidate.full_command(), *candidate.env.values()])]
            if h
        }
        jobs = self.ctx.state.get("mcp_address_job") or {}
        if not any(h in jobs for h in hosts) and self.verification.bool_("learn_job_when_judging", True):
            self._learn_job_for(hosts)
            jobs = self.ctx.state.get("mcp_address_job") or {}
        known = [jobs[h] for h in sorted(hosts) if h in jobs]
        if not known:
            return None
        return sorted({str(n) for names in known for n in names})

    def _learn_job_for(self, hosts: Iterable[str]) -> None:
        """Ask the registered learner of each of ``hosts`` what its address is -- once at a time, never recursively."""
        learners = self.ctx.state.get("mcp_job_learners") or {}
        learning = self.ctx.state.setdefault("mcp_job_learning", set())
        for host in sorted(hosts):
            learner = learners.get(host)
            if learner is None or host in learning:
                continue
            learning.add(host)
            try:
                learner()
            except Exception as exc:  # noqa: BLE001 - not knowing only keeps product tasks unasked
                log.debug("%s: what the address is stayed unknown: %s", host, exc)
            finally:
                learning.discard(host)

    def stale_verdicts(self, candidates: Iterable[Candidate]) -> list[Candidate]:
        """The candidates judged under a job that is no longer what the address is known to be."""
        stale: list[Candidate] = []
        for candidate in candidates:
            if candidate.judged_job is None:
                continue
            current = tuple(self.address_job(candidate, self._contract_host(candidate)) or ())
            if current != candidate.judged_job:
                stale.append(candidate)
        return stale

    def _confirm_by_own_record(self, candidate: Candidate, shake: Handshake, secrets: Mapping[str, Any]) -> None:
        """A refused login is asked once more of the service itself: its own "who am I" page, with that credential.

        A tool's error is the tool's wording; the service's status on the
        account's own record says which it is -- 401 the credential refused,
        403 the account may not use the product (confluence.example.com answered
        /rest/api/user/current with 403 "Client must have sufficient
        permissions" for a token mcp-atlassian called "expired or invalid").
        Only for a single-secret login the catalogue says how to send
        (``logins[].sent_as``) to a ``protected_paths`` page of the address's
        product. (``mcp.verification.login_check.confirm_by_own_record``.)
        """
        config = self.verification.section("login_check")
        if (
            shake.login not in login_check.REFUSED
            or len(secrets) != 1
            or not config.bool_("confirm_by_own_record", True)
        ):
            return
        from .grounding import hosts_named

        host = next(iter(hosts_named([candidate.url, *candidate.full_command(), *candidate.env.values()])), "")
        if not host:
            return
        products = self.ctx.settings.dict_("mcp.discovery.products", {})
        secret = next(iter(secrets.values()))
        value = secret.reveal() if hasattr(secret, "reveal") else str(secret)
        for name in self.address_job(candidate, host) or []:
            table = products.get(name)
            if not isinstance(table, Mapping):
                continue
            sent = next(
                (
                    str(x.get("sent_as") or "")
                    for x in table.get("logins") or []
                    if isinstance(x, Mapping) and x.get("method") == "token" and x.get("sent_as")
                ),
                "",
            )
            header, _, template = sent.partition(":")
            if not header.strip() or "<token>" not in template:
                continue
            for path in table.get("protected_paths") or []:
                status = self._own_record_status(
                    host, str(path), header.strip(), template.strip().replace("<token>", value)
                )
                verdict = {401: login_check.REJECTED, 403: login_check.NO_ACCESS}.get(status, "")
                if not verdict:
                    continue
                if verdict != shake.login:
                    log.info(
                        "%s: %s answered %s with that credential -- %s, not %s",
                        candidate.label,
                        path,
                        status,
                        login_check.in_words(verdict),
                        login_check.in_words(shake.login),
                    )
                shake.login = verdict
                shake.login_detail = f"{shake.login_detail}; confirmed: {host}{path} answered HTTP {status}".lstrip(
                    "; "
                )
                if verdict == login_check.NO_ACCESS:
                    shake.login_reason = login_check.ACCOUNT_NO_ACCESS
                    candidate.add_note(
                        f"{host} took the credential and answered 403 on the account's own record ({path}): "
                        f"the account behind it may not use {host} -- open {host} with that account, or ask "
                        "for access; another server or a new token for the same account will not help"
                    )
                return

    def _own_record_status(self, host: str, path: str, header: str, value: str) -> int:
        if not path.startswith("/"):
            return 0
        timeout = self.ctx.settings.float_("mcp.discovery.per_request_timeout_seconds", 8)
        try:
            with self.ctx.http(timeout=timeout) as client:
                response = client.get(
                    f"https://{host}{path}", headers={header: value, "accept": "application/json"}, timeout=timeout
                )
        except Exception:  # noqa: BLE001 - no answer confirms nothing
            return 0
        return int(getattr(response, "status", 0) or 0)

    def _read_truth(self, host: str, spec: Mapping[str, Any]) -> tuple[list[str], str]:
        """Read ``spec['path']`` on ``host`` directly, with no credential, for a task's comparison."""
        from . import contract

        path = str(spec.get("path") or "")
        if not path.startswith("/"):
            return [], "no path to read"
        timeout = self.ctx.settings.float_("mcp.contract.truth_timeout_seconds", 15.0)
        try:
            with self.ctx.http(timeout=timeout) as client:
                response = client.get(f"https://{host}{path}", headers={"accept": "application/json"}, timeout=timeout)
        except Exception as exc:  # noqa: BLE001 - no comparison is not a failure
            return [], type(exc).__name__
        if not response.ok:
            return [], f"HTTP {response.status}"
        values = contract.truth_values(response.text, [str(f) for f in spec.get("fields") or []])
        return (values, "") if values else ([], "none of the fields were in its answer")

    def _apply_browser_login(self, candidate: Candidate) -> None:
        """A launch that asked to open a page wants a browser login: waiting for that step, never verified, never broken."""
        from . import noninteractive

        shake = candidate.handshake
        if shake is None or not shake.browser_login:
            return
        hosts = noninteractive.hosts_of(shake.browser_login) or ["a sign-in page"]
        shake.login_reason = login_check.BROWSER_LOGIN
        shake.login_refused = False
        shake.credential_pending = sorted({*shake.credential_pending, f"a browser login at {hosts[0]}"})
        candidate.add_note(
            f"it asked to open {', '.join(hosts)} to log in through a browser; discovery runs unattended, so "
            "nothing was opened and nothing waited -- it needs that login done once by you, or a token route"
        )
        log.info("%s: asked to open %s for a browser login -- recorded, not opened", candidate.label, ", ".join(hosts))

    def _apply_job_refused(self, candidate: Candidate) -> None:
        """A tool whose query the service refuses cannot do its job with any credential: a failure, never "waiting"."""
        shake = candidate.handshake
        if shake is None:
            return
        found = shake.job_refused or login_check.job_refused([*shake.functional, *shake.contract], self.ctx.settings)
        if not found:
            return
        shake.job_refused = found
        shake.ok = False
        shake.requires_auth = False
        shake.credential_pending = []
        shake.error = f"its tools cannot do the job: the service refused the query itself ({found})"
        candidate.add_note(shake.error)
        log.info("%s: %s", candidate.label, shake.error)

    def _apply_contract(self, candidate: Candidate) -> None:
        """A server that failed a usage task does not work, however well it answered everything else.

        A refused login keeps its own, more specific verdict (and remedy); the
        task failure then adds nothing.
        """
        from . import contract

        shake = candidate.handshake
        if shake is None or not shake.ok or shake.login in login_check.REFUSED:
            return
        passed, why = contract.verdict(shake.contract)
        if passed is False:
            shake.ok = False
            shake.error = why
        elif passed:
            candidate.add_note(why)
        for row in shake.contract_skipped:
            candidate.add_note(f"usage task {row['task']!r} could not be asked: {row['why']}")
        for row in shake.contract:
            if not contract.fits(row):
                candidate.add_note(f"usage task {row.get('task', '?')!r} {row.get('detail')} -- not counted")

    def _without_credentials(self, launch: Mapping[str, Any], *, standin: str = "") -> dict[str, Any]:
        """``launch`` with every credential part removed -- or, with ``standin``, replaced, identity included.

        A stand-in password beside the *real* user name is a failed login for a
        real account; enough of them lock it. So the user name, e-mail or login
        goes too (``mcp.packages.credential_probe.identity_patterns``), replaced
        by ``placeholder_user`` when a stand-in is wanted.
        """
        probe = self.packages.section("credential_probe")
        identity = probe.str_list("identity_patterns", list(DEFAULT_IDENTITY_PATTERNS))
        user = probe.str_("placeholder_user", DEFAULT_PLACEHOLDER_USER)

        def is_identity(name: str) -> bool:
            return any(re.search(pattern, name, re.IGNORECASE) for pattern in identity)

        env: dict[str, Any] = {}
        for key, value in dict(launch.get("env") or {}).items():
            secret = is_secret_name(key) or holds_a_credential(key, str(value))
            if is_identity(key):
                if standin:
                    env[key] = user
                continue
            if secret:
                if standin:
                    env[key] = standin
                continue
            env[key] = value
        secret_env = (
            {name: (user if is_identity(name) else standin) for name in dict(launch.get("secret_env") or {})}
            if standin
            else {}
        )
        # Dropped is not absent: the child inherits this run's environment, and
        # a Claude Code session exports its settings env -- the operator's
        # tokens -- to every child. gitlab-example-net's "anonymous" comparison got
        # the token it was meant to lack, answered the same, and its proven
        # login was called "proves nothing". Every name dropped is withheld
        # (probe_stdio never withholds what is supplied, so stand-ins stay).
        dropped = [str(k) for k in (launch.get("env") or {}) if k not in env]
        dropped += [str(k) for k in (launch.get("secret_env") or {}) if k not in secret_env]
        withhold = sorted({*(launch.get("withhold") or ()), *dropped})
        return {**dict(launch), "env": env, "secret_env": secret_env, "withhold": withhold}

    def login_budget_left(self, fingerprint: str) -> bool:
        """May one more real login be tried with this credential in this run?

        Every check that is refused counts (``max_refused_logins_per_credential``);
        once spent, the credential is not tried again -- several refusals in a
        row are how an account gets locked, and the run's own checks must never
        be the reason.
        """
        if not fingerprint:
            return True
        cap = self.packages.section("credential_probe").int_(
            "max_refused_logins_per_credential", DEFAULT_MAX_REFUSED_LOGINS
        )
        return cap <= 0 or self.ctx.state.setdefault("mcp_refused_logins", {}).get(fingerprint, 0) < cap

    def count_refusal(self, shake: Handshake) -> None:
        """Count a refused real login against its credential (see :meth:`login_budget_left`)."""
        if shake.login == login_check.REJECTED and shake.login_credential:
            spent = self.ctx.state.setdefault("mcp_refused_logins", {})
            spent[shake.login_credential] = spent.get(shake.login_credential, 0) + 1

    def as_claude_runs_it(self, name: str, entry: Mapping[str, Any], source: str, candidate: Candidate) -> str:
        """Why an existing entry does not work the way Claude Code runs it; "" when it does.

        The run's own trial launch is not the entry: it drops a variable nothing
        sets, where Claude Code passes the literal ``${NAME}`` -- bugzilla-opensuse-org's
        CLI copy crashed on ``--bugzilla-auth-mode '${BUGZILLA_AUTH_MODE}'`` and
        was kept as "answered during this run". So: what ``claude mcp list``
        says about it, and the entry launched exactly as written.
        """
        from .existing import CLAUDE_CLI, ExistingRegistrations

        if not self.verification.bool_("judge_existing_as_written", True):
            return ""
        repointed = dict(entry)
        if candidate.kind == "stdio" and self.ctx.settings.bool_("mcp.registration.repoint_before_trial", True):
            # Judged as it will be written: with the files it names found again,
            # as its trial was (McpWriter.repoint_missing). A path that only moved
            # -- a renamed or relocated folder -- is never why it is called broken.
            from .writer import McpWriter

            repointed = McpWriter(self.ctx).repointed_entry(entry)
        moved = repointed != dict(entry)
        if source == CLAUDE_CLI and not moved and ExistingRegistrations.for_run(self.ctx).cli_failed(name):
            # (Claude Code's status from before the run speaks of the old paths
            # when files only moved: then the entry is judged as written instead.)
            return "Claude Code reports it fails to connect"
        if candidate.kind != "stdio":
            return ""
        standins = sorted({str(r).partition("env:")[2] for r in candidate.secret_env.values()} - {""})
        written = self.verify_as_written(repointed, name, standins=standins)
        if written is not None and not written.ok and not written.requires_auth:
            return f"it does not start as written ({self.excerpt(written.error) or 'it exited'})"
        return ""

    def _withheld(
        self,
        candidate: Candidate,
        command: Sequence[str],
        env: Mapping[str, Any],
        secret_env: Mapping[str, Any],
        pending: Sequence[str],
        templates: Mapping[str, str],
    ) -> list[str]:
        """The names a trial launch must not inherit: every credential it is launched *without*.

        The pending variables; the slots left out for want of a value (each
        key a template stood for); every declared credential slot not supplied;
        and each credential-named variable the launched source reads
        (:mod:`.env_contract`) that nothing supplies -- a server may read its
        token under a second name. A name ``env``/``secret_env`` sets on
        purpose is never withheld. (``mcp.verification.withhold_unsupplied``.)
        """
        if not self.verification.bool_("withhold_unsupplied", True):
            return []
        supplied = {*env, *secret_env}
        names = {*pending, *templates, *(k for k in candidate.secret_env if k not in supplied)}
        if self.ctx.settings.bool_("mcp.env_contract.enabled", True):
            from .env_contract import EnvContract

            try:
                contract = EnvContract(self.ctx.settings)
                reads = contract.reads_of(list(command))
                # ...and what an installed package's own source reads: mcp-bugzilla
                # reads BUGZILLA_API_KEY, and a shell exporting a revoked one for
                # something else made every trial "refused".
                _reader, package_reads, _files = contract.all_reads(list(command))
            except Exception:  # noqa: BLE001 - evidence only; never a reason to fail a trial
                reads, package_reads = [], set()
            names.update(r.name for r in reads if is_secret_name(r.name))
            names.update(
                n
                for n in package_reads
                if is_secret_name(n) and not contract.session(n) and re.fullmatch(r"[A-Z][A-Z0-9_]*", n)
            )
        return sorted(n for n in names if n and n not in supplied)

    def _without_login(
        self, candidate: Candidate, shake: Handshake, launch: Mapping[str, Any], timeout: float, pending: list[str]
    ) -> list[str]:
        """Ask its read calls with the credential still missing: a public service does not need it.

        bugzilla.opensuse.org answers its read calls to anybody; recorded as
        "waiting for BUGZILLA_API_KEY", it was never counted as working. When
        the calls succeed with no credential at all, the server works without a
        login: the credential becomes optional (written ``${NAME:-}``), and is
        no longer waited for. Nothing is logged in, so no account is at risk.
        Returns what is still pending.
        """
        probe = self.prober.probe_stdio(
            **launch,
            timeout=timeout,
            login=True,
            login_tokens=self.login_tokens(candidate),
            contract=self.contract_plan(candidate),
        )
        from . import contract

        passed, _ = contract.verdict(probe.contract)
        refused_job = login_check.job_refused([*probe.functional, *probe.contract], self.ctx.settings)
        if refused_job:
            # The service refused the query the tool sends, not the caller: no
            # credential can make it work, so it is not waiting for one.
            shake.job_refused = refused_job
            return pending
        if probe.login in login_check.REFUSED or passed is False:
            return pending  # refused, or failed its job without it: it waits for it
        done = passed is True and self.verification.section("login_check").bool_("job_done_is_no_login_needed", True)
        if probe.login != login_check.ACCEPTED and not done:
            return pending  # empty or unproved without it: it waits for it
        # A declared slot is not proof a login is needed: smelt.example.net's
        # mcp-graphql was proposed with a HEADERS slot and did smelt's job --
        # its usage tasks passed -- with nothing in it. The job done with no
        # credential proves the slot optional, whatever the login tool said.
        if passed is None and self.verification.section("login_check").bool_("public_needs_the_job", True):
            # Something answered anybody -- but nothing the server is *for*:
            # no usage task applied, or none was decided. build.example.net answers
            # /about to anyone, and mcp-openapi-proxy was written with its
            # login optional (EXTRA_HEADERS "${…:-}") while every call that
            # needs the account was refused.
            candidate.add_note(
                f"{probe.login_tool} answered with no credential, but no usage task showed the server doing its "
                "job without one -- the credential stays required"
            )
            return pending
        shake.credential_optional = sorted(set(pending))
        shake.login_reason = login_check.WITHOUT_LOGIN
        shake.login_detail = (
            f"{probe.login_tool} answered with no credential at all"
            if probe.login == login_check.ACCEPTED
            else "its usage tasks passed with no credential at all"
        )
        shake.functional = list(probe.functional) or shake.functional
        shake.contract = list(probe.contract) or shake.contract
        candidate.add_note(
            f"works without a login ({shake.login_detail}); "
            f"{', '.join(shake.credential_optional)} is optional -- for what needs a login"
        )
        log.info("%s: works without a login; %s optional", candidate.label, ", ".join(shake.credential_optional))
        self._slot_delivered(candidate, shake, launch, timeout, shake.credential_optional, baseline=probe.functional)
        return []

    def _align_slots_to_source(self, candidate: Candidate) -> None:
        """A credential slot the launched script's own source never reads is renamed to the one it does read.

        sd.example.com's agent wrote a bridge reading ``JIRA_PERSONAL_TOKEN`` and
        declared the slot under another name: the token went where nothing read
        it, the bridge answered as if no one had logged in, and that was taken
        for a refusal of the token. Before the trial, each declared credential
        slot is compared with what the source reads (:class:`.env_contract.EnvContract`);
        a slot it never reads, beside exactly one credential variable it reads
        and nobody declared, is moved there. Nothing changes when the source is
        unknown or ambiguous. (``mcp.verification.align_slots_to_source``.)
        """
        if (
            candidate.kind != "stdio"
            or not candidate.secret_env
            or not self.verification.bool_("align_slots_to_source", True)
        ):
            return
        from .env_contract import EnvContract

        try:
            contract = EnvContract(self.ctx.settings)
            if not contract.scripts_in(candidate.full_command()):
                return  # a bridge is a script; an installed package keeps the keys its README names
            reader, reads, _files = contract.all_reads(candidate.full_command())
        except Exception:  # noqa: BLE001 - reading a source must never cost the trial
            return
        if not reads:
            return
        unread = [slot for slot in candidate.secret_env if slot not in reads]
        undeclared = sorted(
            n for n in reads if is_secret_name(n) and n not in candidate.secret_env and n not in candidate.env
        )
        if len(unread) != 1 or len(undeclared) != 1:
            return
        self._move_slot(candidate, unread[0], undeclared[0])
        candidate.add_note(
            f"its credential slot {unread[0]} is not read by {reader}, which reads {undeclared[0]}: "
            f"the credential is passed as {undeclared[0]}"
        )
        log.info(
            "%s: credential slot %s renamed to %s, which %s reads", candidate.label, unread[0], undeclared[0], reader
        )

    def _slot_delivered(
        self,
        candidate: Candidate,
        shake: Handshake,
        launch: Mapping[str, Any],
        timeout: float,
        variables: Sequence[str],
        baseline: Sequence[Mapping[str, Any]] = (),
    ) -> None:
        """Does the server read the credential slot it is given? Proved by a stand-in changing its answers.

        bugzilla-example-com's key was written under ``BUGZILLA_EXAMPLE_COM_API_KEY``;
        mcp-bugzilla reads ``BUGZILLA_API_KEY``. Every call succeeded anyway --
        anonymously -- so nothing noticed the key never arrived. Here each
        optional slot is filled with the stand-in and the same calls are made:
        a server that reads it answers differently (an invalid key is refused).
        One that answers exactly the same, and whose own ``--help`` names
        another credential variable and not this one, does not read the slot;
        the slot is moved to the variable it names, which must then change its
        answers in turn. Nothing real is sent, nothing is logged in.
        (``mcp.verification.delivery``.)
        """
        config = self.verification.section("delivery")
        placeholder = self.packages.section("credential_probe").str_(
            "placeholder", "initialize-claude-ai-stand-in-not-a-real-credential"
        )
        if not config.bool_("enabled", True) or not placeholder or candidate.kind != "stdio" or not variables:
            return
        slots = self._slot_keys(candidate, variables)
        if not slots:
            return
        # The same calls as the run it is compared with (CandidateVerifier.login_tokens).
        self._delivery_tokens = self.login_tokens(candidate)
        # What the launched script's (or installed package's) own source reads,
        # first: a script that reads the slot reads it, and no stand-in or
        # --help is needed to say so. --help only when the source is unknown.
        from .env_contract import EnvContract

        _reader, source_reads, _files = EnvContract(self.ctx.settings).all_reads(launch["command"])
        help_text = ""
        for key in slots:
            if source_reads and key in source_reads:
                continue
            if not source_reads and self._reads(launch, key, placeholder, timeout, baseline):
                continue
            if source_reads:
                named = sorted(n for n in source_reads if is_secret_name(n) and n not in slots)
                evidence = "its source reads"
            else:
                if not help_text:
                    help_text = self._help_text(launch, config)
                named = [
                    n
                    for n in dict.fromkeys(re.findall(r"\b[A-Z][A-Z0-9_]{2,}\b", help_text))
                    if is_secret_name(n) and n not in slots
                ]
                evidence = "its --help names"
            if source_reads and len(named) == 1:
                # The source reads exactly one credential variable, and not this
                # slot: that is where the credential goes.
                self._move_slot(candidate, key, named[0])
                shake.credential_optional = sorted({named[0] if v == key else v for v in shake.credential_optional})
                candidate.add_note(
                    f"the credential is read from {named[0]}, not {key}: {evidence} {named[0]} and never {key} "
                    f"-- the entry passes it as {named[0]}"
                )
                log.info(
                    "%s: credential slot %s is not read by its source; moved to %s", candidate.label, key, named[0]
                )
                continue
            if not help_text or key in help_text or not named:
                candidate.add_note(
                    f"a stand-in in {key} changed none of its answers, so it is not shown that the server reads "
                    f"{key} -- check its documentation for the variable it reads its credential from"
                )
                continue
            moved = next((n for n in named if self._reads(launch, n, placeholder, timeout, baseline)), "")
            if not moved:
                candidate.add_note(
                    f"a stand-in in {key} changed none of its answers; its --help names {', '.join(named[:3])}, "
                    "and a stand-in there changed nothing either"
                )
                continue
            self._move_slot(candidate, key, moved)
            shake.credential_optional = sorted({moved if v == key else v for v in shake.credential_optional})
            candidate.add_note(
                f"the credential is read from {moved}, not {key}: a stand-in in {key} changed none of its answers, "
                f"its --help names {moved}, and a stand-in there did -- the entry passes it as {moved}"
            )
            log.info(
                "%s: credential slot %s is not read; moved to %s, which the server reads", candidate.label, key, moved
            )

    @staticmethod
    def _slot_keys(candidate: Candidate, variables: Sequence[str]) -> list[str]:
        """The env keys of ``candidate`` that carry one of ``variables`` (by name or by ``${…}`` reference)."""
        keys: list[str] = []
        values = {**candidate.env, **{k: str(v) for k, v in candidate.secret_env.items()}, **candidate.references}
        for key, value in values.items():
            if key in variables or any(f"${{{v}" in str(value) for v in variables):
                if key not in keys:
                    keys.append(key)
        return keys

    def _reads(
        self,
        launch: Mapping[str, Any],
        key: str,
        placeholder: str,
        timeout: float,
        baseline: Sequence[Mapping[str, Any]] = (),
    ) -> bool:
        """Do the server's read calls answer differently with a stand-in in ``key``? (Not starting counts as different.)

        Compared call by call with the answers given with no credential
        (``baseline``): one local tool answering alike proves nothing --
        mcp-bugzilla's ``mcp_server_info_resource`` answered either way while
        every call to the service failed on the stand-in key.
        """
        probe = self.prober.probe_stdio(
            launch["command"],
            env={**launch["env"], key: placeholder},
            secret_env=launch["secret_env"],
            cwd=launch["cwd"],
            timeout=timeout,
            login=True,
            login_tokens=self._delivery_tokens,
            withhold=launch.get("withhold", ()),
        )
        _scrub(probe, placeholder)
        if not probe.ok:
            return True
        if not baseline:
            return probe.login != login_check.ACCEPTED
        before = {str(r.get("tool")): str(r.get("verdict")) for r in baseline}
        after = {str(r.get("tool")): str(r.get("verdict")) for r in probe.functional}
        # Only calls both runs made, and never one that said "try again later".
        common = [t for t in before if t in after and login_check.TEMPORARY not in (before[t], after[t])]
        return any(after[t] != before[t] for t in common)

    def _help_text(self, launch: Mapping[str, Any], config: Any) -> str:
        """What the server's own ``--help`` says (``help_args``), "" when it says nothing usable."""
        argv = [*launch["command"], *config.str_list("help_args", ["--help"])]
        # Input closed: a stdio server that ignores --help reads EOF and exits
        # instead of waiting out the timeout (sd.example.com's bridge waited 60 s).
        result = self.ctx.runner.run(
            argv,
            timeout=config.float_("help_timeout_seconds", 15),
            env=dict(launch["env"]),
            cwd=launch.get("cwd"),
            mutating=False,
            label="read the server's --help",
            stdin_text="",
        )
        return f"{result.stdout or ''}\n{result.stderr or ''}"

    @staticmethod
    def _move_slot(candidate: Candidate, key: str, moved: str) -> None:
        """Pass what ``key`` carried under ``moved`` instead, keeping the operator's variable."""
        for table in (candidate.env, candidate.secret_env, candidate.references, candidate.in_use):
            if key in table:
                table[moved] = table.pop(key)
        for table in (candidate.exported_names, candidate.reuse_names):
            if key in table:
                table[moved] = table.pop(key)
        if key in candidate.secret_env or moved in candidate.secret_env:
            candidate.exported_names.setdefault(moved, key)

    def _anonymous_check(self, candidate: Candidate, shake: Handshake, trial: Mapping[str, Any]) -> None:
        """Ask the same calls with no credential at all: does the login change anything?

        A call that succeeds with nobody logged in proves nothing about the
        login -- build-example-net "worked" for days on ``get_about``, which
        build.example.net answers anyone. And a refusal identical to the one an
        anonymous caller gets may mean the credential was never sent (a
        variable the server does not read, an auth type it does not implement,
        a header under the wrong name) as much as a wrong credential. Nothing
        is logged in for this comparison, so no account is put at risk
        (``compare_with_anonymous``).
        """
        if not self.verification.section("login_check").bool_("compare_with_anonymous", True):
            return
        launch = trial.get("launch")
        if not launch or not shake.login_credential or shake.login not in (login_check.ACCEPTED, login_check.REJECTED):
            return
        probe = self.prober.probe_stdio(
            **self._without_credentials(launch),
            timeout=trial.get("timeout"),
            login=True,
            login_tokens=self.login_tokens(candidate),
        )
        if not probe.ok or not probe.functional:
            return  # it would not even start without the credential: nothing to compare
        anonymous = {str(row.get("tool")): row for row in probe.functional}
        if shake.login == login_check.ACCEPTED:
            accepted = [row for row in shake.functional if row.get("verdict") == login_check.ACCEPTED]
            proving = [
                row
                for row in accepted
                if anonymous.get(str(row.get("tool")), {}).get("verdict") != login_check.ACCEPTED
            ]
            if proving:
                shake.login_tool = str(proving[0].get("tool") or shake.login_tool)
                return
            if not accepted:
                return
            shake.login = login_check.UNCHECKED
            shake.login_reason = login_check.PUBLIC_ONLY
            shake.login_detail = (
                f"{', '.join(str(r.get('tool')) for r in accepted)} answered the same with no credential at all -- "
                "a call anybody can make proves nothing about the login"
            )
            log.info("%s: %s", candidate.label, shake.login_detail)
            # The same answers with the credential and without it: maybe the
            # server never received it. A stand-in in each slot settles that
            # (bugzilla-example-com's key sat under a name mcp-bugzilla never reads).
            self._slot_delivered(
                candidate,
                shake,
                self._without_credentials(launch),
                float(trial.get("timeout") or 90),
                sorted({*candidate.secret_env, *(launch.get("secret_env") or {})}),
                baseline=probe.functional,
            )
            from . import contract

            job_done = contract.verdict(shake.contract)[0] is True or not self.verification.section(
                "login_check"
            ).bool_("public_needs_the_job", True)
            if (
                shake.credential_pending
                and job_done
                and self.verification.section("login_check").bool_("public_works_without_login", True)
            ):
                # The login is unproved -- and the server works without one: a
                # public service is working, its credential optional.
                shake.credential_optional = sorted(set(shake.credential_pending))
                shake.credential_pending = []
                candidate.add_note(
                    f"works without a login; {', '.join(shake.credential_optional)} is optional -- for what needs one"
                )
            return
        row = anonymous.get(shake.login_tool, {})
        if row.get("verdict") == login_check.REJECTED and login_check.same_answer(
            str(row.get("detail") or ""), shake.login_detail
        ):
            shake.login_reason = login_check.SAME_AS_ANONYMOUS
            shake.login_detail = (
                f"{shake.login_tool} answered exactly as it does with no credential ({shake.login_detail}) -- "
                "the credential may never have been sent, or it may not be valid"
            )
            log.info("%s: %s", candidate.label, shake.login_detail)
            self._slot_delivered(
                candidate,
                shake,
                self._without_credentials(launch),
                float(trial.get("timeout") or 90),
                sorted({*candidate.secret_env, *(launch.get("secret_env") or {})}),
                baseline=probe.functional,
            )
            if self.verification.section("login_check").bool_("same_as_anonymous_is_not_a_refusal", True):
                # Nothing shows the credential reached the server, so it is not
                # blamed: no refused login is counted against it (the allowance
                # that protects the account), nothing records it as refused, and
                # it ranks like a credential still missing. sd.example.com's token
                # went to an agent's bridge under a name its script never reads;
                # counted as a refusal, it was never offered to the bridge that
                # works with it.
                shake.login_refused = False
                # Not verified either: it waits until the credential is shown to arrive.
                shake.credential_pending = sorted(
                    {*shake.credential_pending, *candidate.secret_env, *(launch.get("secret_env") or {})}
                ) or ["the credential"]
                spent = self.ctx.state.get("mcp_refused_logins") or {}
                if shake.login_credential in spent:
                    spent[shake.login_credential] = max(0, int(spent[shake.login_credential]) - 1)

    def _account_check(self, candidate: Candidate, shake: Handshake, trial: Mapping[str, Any]) -> None:
        """Every call failed alike with the credential: does a stand-in for it change the answer?

        build.opensuse.org answered its entry's login with an HTML 404 for
        every path -- ``openSUSE:Factory`` included -- while no login, and a
        made-up one, got 401. The same answer for every path *with* the
        credential, and a different one without it, is a verdict about the
        account: recognised, with nothing behind it on this service. The same
        launch is run once more with a stand-in in every credential slot and
        the same read-only calls (``compare_with_standin``); the stand-in's
        answers are compared, never kept.
        """
        config = self.verification.section("login_check")
        if not config.bool_("compare_with_standin", True) or not shake.login_credential:
            return
        if shake.login not in login_check.UNPROVED:
            return
        code = login_check.uniform_failure(shake.functional)
        launch = trial.get("launch")
        if not code or not launch:
            return
        placeholder = self.packages.section("credential_probe").str_(
            "placeholder", "initialize-claude-ai-stand-in-not-a-real-credential"
        )
        slots = {*dict(launch.get("secret_env") or {}), *candidate.in_use}
        if not slots or not placeholder:
            return
        # Every credential part replaced, the user name included: a stand-in
        # password beside a real user name is a failed login for a real account.
        standin = self._without_credentials(
            {**dict(launch), "secret_env": {name: "" for name in slots}}, standin=placeholder
        )
        probe = self.prober.probe_stdio(
            **standin, timeout=trial.get("timeout"), login=True, login_tokens=self.login_tokens(candidate)
        )
        other = login_check.uniform_failure(probe.functional)
        if probe.login != login_check.REJECTED and not (other and other != code):
            return
        said = f"HTTP {other}" if other else login_check.in_words(probe.login)
        shake.login = login_check.NO_ACCESS
        shake.login_reason = login_check.NO_ACCOUNT
        shake.login_tool = str(shake.functional[0].get("tool") or "") if shake.functional else ""
        shake.login_detail = (
            f"every call answered HTTP {code} with the credential, and {said} with a stand-in for it -- "
            "the login is recognised, and the service has no account behind it"
        )
        self.judge_login(shake)
        log.info("%s: %s", candidate.label, shake.login_detail)

    def _unverify_failed_calls(self, shake: Handshake | None) -> None:
        """A server every call of which failed does not work, however well it started.

        Unproved used to be enough to count as working: build.opensuse.org's
        bridge started, listed its tools, and every call came back 404.
        ``mcp.verification.login_check.failed_calls_unverify``; a refusal, an
        empty answer or one accepted call keeps the older verdicts.
        """
        if shake is None or not shake.ok or shake.login not in login_check.UNPROVED:
            return
        if not self.verification.section("login_check").bool_("failed_calls_unverify", True):
            return
        if not login_check.calls_all_failed(shake.functional, self.ctx.settings):
            return
        shake.ok = False
        shake.error = (
            f"it starts, and every call it was asked failed ({shake.functional[0].get('detail')}) -- "
            "it does not work as configured"
        )

    def _retry_slow_start(self, candidate: Candidate, shake: Handshake, trial: dict[str, Any]) -> Handshake:
        """Trial an existing registration that timed out once more, with a longer deadline.

        A first launch that downloads (``uvx``, ``npx``) can outlast the trial:
        sd.example.com's mcp-atlassian took 93 s and answered nothing, after
        starting in about a second on every earlier run, and "does not start"
        then replaced the entry Claude Code had reported connected. Slow is not
        broken. Only an existing registration is retried -- it is the one a
        verdict can take away -- ``mcp.packages.slow_start_retries`` times, the
        deadline multiplied by ``slow_start_timeout_factor`` each time.
        """
        from .existing import ExistingRegistrations  # imports this module

        timeout = float(trial.get("timeout") or 0)
        if shake.ok or not timeout or not self.looks_transient(shake, timeout):
            return shake
        if ExistingRegistrations.for_run(self.ctx).registration_for(candidate) is None:
            return shake
        factor = max(1.0, self.packages.float_("slow_start_timeout_factor", 2.0))
        for attempt in range(max(0, self.packages.int_("slow_start_retries", 1))):
            log.info(
                "%s: no answer within %.0fs -- an existing registration, so tried again with %.0fs",
                candidate.label,
                timeout,
                timeout * factor,
            )
            timeout *= factor
            again: dict[str, Any] = {"timeout_override": timeout}
            retried = self._trial_stdio(candidate, again)
            trial.clear()
            trial.update(again)
            if retried.ok or not self.looks_transient(retried, timeout):
                return retried
            shake = retried
        shake.add_hint("it answered nothing within any deadline it was given; slow, and possibly not broken")
        return shake

    def _prove_address_forms(
        self,
        candidate: Candidate,
        shake: Handshake,
        trial: dict[str, Any],
        refused: bool = False,
        waiting: bool = False,
    ) -> Handshake:
        """Try each address-valued variable in other forms; keep the first that clears the misconfiguration.

        The form of an address a server parses is guessed -- from a variable's
        name (``mcp.packages.address_fill``) or by an agent -- and a handshake
        never parses it. osc-mcp's ``search_packages`` refused
        ``OSC_MCP_API=https://api.opensuse.org`` ("unexpected api address
        format") and took ``api.opensuse.org``. Each variable is varied alone,
        through ``[mcp.verification.address_forms]``, and relaunched exactly as
        a trial is, with whatever real credential the first check had; the
        first variant whose calls no longer answer "misconfigured" -- and that
        still starts, or still waits only for its credential -- is kept, written
        into the entry, and reported. Nothing is kept that did not help.
        """
        config = self.verification.section("address_forms")
        if not config.bool_("enabled", True):
            return shake
        forms = config.str_list(
            "forms", ["{scheme}://{host}", "{host}", "{scheme}://{host}/", "{scheme}://{host}{path}"]
        )
        scheme = config.str_("default_scheme", "https")
        budget = max(0, config.int_("max_variants", 10))
        tried: list[str] = []
        api_hosts = self.ctx.state.get("mcp_api_hosts", {}) if config.bool_("try_api_hosts", True) else {}
        from .evidence import URL_SETTING_KEYS

        url_keys = self.ctx.settings.str_list("mcp.discovery.auth.evidence.url_setting_keys", list(URL_SETTING_KEYS))
        for key, original in list(candidate.env.items()):
            if waiting and not any(re.search(k, str(key), re.IGNORECASE) for k in url_keys if k):
                continue  # only the settings that say where its calls go
            # Other forms of the address, then the same forms on the hosts that
            # serve its API: the value's host may be the website, not the API.
            # A refusal tries only the API hosts: the form of a host the login
            # was never offered on cannot be what refused it.
            variants = [] if refused or waiting else _address_variants(str(original), forms, scheme)
            if waiting:
                # The value as written, on the API host asking for its login --
                # and only there: a base URL stays a base URL.
                named = (urlsplit(str(original)).hostname or "").lower() if "://" in str(original) else ""
                variants += [
                    v
                    for host in self._api_hosts_asking(candidate, named)
                    if (v := _on_host(str(original), host)) and v != original
                ]
            variants += (
                []
                if waiting
                else [
                    v
                    for host in _api_hosts_for(str(original), api_hosts)
                    for v in [host, *_address_variants(host, forms, scheme)]
                    if v not in variants and v != original
                ]
            )
            for variant in variants:
                if budget <= 0:
                    break
                budget -= 1
                candidate.env[key] = variant
                tried.append(f"{key}={variant}")
                inner: dict[str, Any] = {"forms": True, "isolated": trial.get("isolated", False)}
                again = self._trial_stdio(candidate, inner)
                if inner.get("launch") is not None:
                    self.check_in_use(candidate, again, inner)
                # Proved only by the tool that rejected the setting, asked again:
                # a form under which that tool is no longer called proves nothing.
                culprit = shake.misconfigured.split(":", 1)[0].strip()
                answered = {row.get("tool"): row.get("verdict") for row in again.functional}
                cleared = culprit in answered and answered[culprit] != login_check.MISCONFIGURED
                if culprit == "start-up":
                    cleared = again.ok or bool(again.credential_pending)
                if refused:
                    # Cleared only by the login being accepted on that host.
                    cleared = again.login == login_check.ACCEPTED
                if waiting:
                    # Nothing to accept yet: kept when it does every part of the job it did before.
                    cleared = bool(again.credential_pending or again.ok) and _job_done(shake) <= _job_done(again)
                if (again.ok or again.credential_pending) and cleared and not again.misconfigured:
                    again.address_forms = {key: variant}
                    why = (
                        f"its login was refused ({shake.login_detail[:80]})"
                        if refused
                        else "its login would go to a host that does not ask for it"
                        if waiting
                        else f'the server answered "{shake.misconfigured}"'
                    )
                    note = f"{key} is written as {variant!r}: with {original!r} {why}, and with this one it was not"
                    again.add_hint(note)
                    candidate.add_note(note)
                    trial.update(inner)
                    log.info("%s: %s", candidate.label, note)
                    return again
            candidate.env[key] = original
        if tried and waiting:
            shake.add_hint(
                "its login would go to a host that does not ask for it; tried "
                + ", ".join(tried)
                + " -- none did the job as well, so the address is kept"
            )
        elif tried:
            what = (
                f"refused ({shake.login_detail[:80]})"
                if refused
                else f"{login_check.in_words(login_check.MISCONFIGURED)} ({shake.misconfigured})"
            )
            shake.add_hint(f"{what}; tried " + ", ".join(tried) + " -- none cleared it")
        return shake

    def check_in_use(self, candidate: Candidate, shake: Handshake, trial: Mapping[str, Any] | None = None) -> None:
        """Check, with one read-only call, the credential an existing entry uses right now.

        A credential the operator wrote into the entry itself -- a literal, or
        the default of an unset ``${NAME:-…}`` -- is what Claude Code sends, and
        it is the one that was failing at sd.example.com while the run checked only
        the exported variable, which was unset. Asked only when every credential
        still pending has such a value (:attr:`Candidate.in_use`) and
        ``mcp.verification.login_check.check_in_use`` allows it. The value is a
        :class:`~..core.secrets.Secret`: passed to the child, redacted from
        everything the server says back, and never written -- the entry still
        reads ``${NAME}``. The verdict lands on ``shake`` with its source named.
        """
        if shake.login or login_check.mode(self.ctx.settings) == "off":
            return
        if not self.verification.section("login_check").bool_("check_in_use", True):
            return
        borrowed = not candidate.in_use
        if borrowed:
            self._borrow_in_use(candidate, trial)
        if not candidate.in_use:
            return
        if not self.login_budget_left(credential_fingerprint(candidate.in_use.values())):
            candidate.add_note(
                "the credential its entry uses was refused repeatedly earlier in this run, so it was not tried again"
            )
            return
        if candidate.kind == "url":
            headers = {**self.auth_headers(), **candidate.headers}
            for slot, secret in candidate.in_use.items():
                value = secret.reveal() if hasattr(secret, "reveal") else str(secret)
                headers = {k: str(v).replace(f"${{{slot}}}", value) for k, v in headers.items()}
            if not real_credential(headers):
                return
            probe = (
                self.prober.probe_sse(candidate.url, headers=headers, login=True)
                if candidate.transport == Transport.SSE
                else self.prober.probe_http(
                    candidate.url,
                    headers=headers,
                    login=True,
                    login_tokens=self.login_tokens(candidate),
                    contract=self.contract_plan(candidate),
                )
            )
            if not probe.ok and probe.requires_auth:
                # The credential went in the header and the endpoint still said
                # 401/403: refused before any tool could be called.
                probe.login = login_check.REJECTED if probe.status != 403 else login_check.NO_ACCESS
                probe.login_detail = probe.auth_hint or f"HTTP {probe.status}"
        else:
            if trial is None:
                return
            pending = list(trial.get("pending") or [])
            if not pending or any(name not in candidate.in_use for name in pending):
                return
            launch = dict(trial["launch"])
            launch["secret_env"] = {**(launch.get("secret_env") or {}), **{n: candidate.in_use[n] for n in pending}}
            for key, template in dict(trial.get("templates") or {}).items():
                # The setting that carries the credential, rebuilt with it.
                def offered(match: re.Match[str]) -> str:
                    held = candidate.in_use.get(match.group(1))
                    if held is None:
                        return match.group(0)
                    return held.reveal() if hasattr(held, "reveal") else str(held)

                filled = re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-[^}]*)?\}", offered, template)
                if "${" not in filled:
                    launch["secret_env"][key] = Secret(filled, source=f"{key}, with the offered credential filled in")
            probe = self.prober.probe_stdio(
                **launch,
                timeout=trial.get("timeout"),
                login=True,
                login_tokens=self.login_tokens(candidate),
                contract=self.contract_plan(candidate),
            )
            if not probe.ok:
                self.credential_failure(probe)
        host = self._host_of(candidate)
        accepted = self.ctx.state.setdefault("mcp_accepted_credentials", {})
        elsewhere: set[str] = set()
        if borrowed and not candidate.held_names and probe.login != login_check.ACCEPTED:
            from .existing import ExistingRegistrations  # imports this module

            fingerprint = credential_fingerprint(candidate.in_use.values())
            elsewhere = {
                h
                for s in candidate.in_use.values()
                for h in ExistingRegistrations.for_run(self.ctx).bound_elsewhere(s, host)
            }
            # Only a token or key is issued for one instance. One user name and
            # password used on several services, each entry with its own
            # address, is the operator's choice and allowed: its answer here is
            # this service's own (instance_bound_patterns).
            if any(instance_bound(slot, self.ctx.settings) for slot in candidate.in_use):
                elsewhere |= {h for fp, h in accepted.items() if fp == fingerprint and h != host}
        if elsewhere:
            # Another instance's token: what it got here says nothing about this
            # server -- neither its calls nor its tasks are this entry's evidence.
            shake.login, shake.login_reason = login_check.UNCHECKED, login_check.FOREIGN
            shake.login_detail = (
                f"the credential it was offered is the one {', '.join(sorted(elsewhere))} use -- issued by another "
                f"instance, so its answer here says nothing about {host}; {host} needs its own"
            )
            log.info("%s: %s", candidate.label, shake.login_detail)
            return
        # What the calls showed about the settings is the entry's, whichever
        # credential made them possible.
        shake.functional = list(probe.functional) or shake.functional
        shake.contract = list(probe.contract) or shake.contract
        shake.contract_skipped = list(probe.contract_skipped) or shake.contract_skipped
        shake.misconfigured = probe.misconfigured or shake.misconfigured
        shake.unexercised = list(probe.unexercised) or shake.unexercised
        if not probe.login:
            return
        sources = sorted({getattr(s, "source", "") for s in candidate.in_use.values()} - {""})
        shake.login, shake.login_tool, shake.login_detail = probe.login, probe.login_tool, probe.login_detail
        shake.login_reason = probe.login_reason
        shake.credential_sent = probe.credential_sent
        shake.login_credential = credential_fingerprint(candidate.in_use.values())
        shake.login_parts = secret_part_prints(candidate.in_use)
        self.count_refusal(shake)
        shake.login_source = "; ".join(sources) or "the value written in the existing entry"
        self.judge_login(shake)
        self._confirm_by_own_record(candidate, shake, candidate.in_use)
        # Said once the anonymous comparison has run (_verify_stdio): "accepted"
        # followed by "a call anybody can make proves nothing" read as both.
        log.debug(
            "%s: the credential its existing entry uses -- first look: %s",
            candidate.label,
            login_check.in_words(shake.login),
        )
        if (
            borrowed
            and shake.login == login_check.ACCEPTED
            and self.verification.section("login_check").bool_("reuse_variable_names", True)
        ):
            # Proved with the credential another entry for this host reads:
            # read the same variable, so one export serves the instance.
            from .existing import ExistingRegistrations  # imports this module

            existing = ExistingRegistrations.for_run(self.ctx)
            for slot, secret in candidate.in_use.items():
                variable = existing.variable_for(secret)
                if variable and slot in candidate.secret_env:
                    candidate.reuse_names.setdefault(slot, variable)
        if shake.login == login_check.ACCEPTED and not candidate.held_names:
            self._read_what_you_export(candidate, host)
        if shake.login == login_check.ACCEPTED:
            accepted.setdefault(shake.login_credential, host)
            for slot, held in candidate.held_names.items():
                # Read what the operator already exports: a variable, or a form
                # composed of them (written as is -- Claude Code expands each).
                if "${" in held or held.startswith("COMPOSE:"):
                    candidate.composed[slot] = held
                else:
                    candidate.reuse_names.setdefault(slot, held)
                    candidate.secret_env[slot] = f"env:{held}"
            # Proved with exactly those values, which the shell already holds:
            # nothing is left to wait for in those slots.
            if candidate.held_names:
                shake.credential_pending = [n for n in shake.credential_pending if n not in candidate.held_names]
        if shake.login == login_check.ACCEPTED and candidate.held_names:
            candidate.add_note(
                "the login you already export was accepted by its service; the entry reads "
                + ", ".join(
                    sorted({n for v in candidate.held_names.values() for n in re.findall(r"[A-Z][A-Z0-9_]{2,}", v)})
                )
            )
        elif shake.login == login_check.ACCEPTED:
            candidate.add_note(
                f"the credential written in its existing entry is accepted by its service; export it as "
                f"{', '.join(sorted(candidate.reuse_names.get(k, k) for k in candidate.secret_env))} (the entry reads the variable, never the value) "
                "and remove it from the file"
            )

    def _read_what_you_export(self, candidate: Candidate, host: str) -> None:
        """A credential an entry holds as a literal that you also export for the address: read the export.

        build-opensuse-org held the openSUSE password as a literal; the shell
        exported the same value as BUILD_OPENSUSE_ORG_PASSWORD. The literal is
        never written, so the entry was made to read a new variable nobody
        exports -- "waiting" for a login that worked. Compared in memory,
        against the variables named for the address
        (:func:`.inventory.prefix_of`) only, and never logged.
        """
        from . import inventory

        if not self.verification.section("login_check").bool_("borrow_held", True):
            return
        prefix = inventory.prefix_of(host) if host else ""
        if not prefix:
            return
        for slot, secret in candidate.in_use.items():
            value = secret.reveal() if hasattr(secret, "reveal") else str(secret)
            match = next(
                (
                    name
                    for name in sorted(os.environ)
                    if name.startswith(prefix) and value and os.environ.get(name) == value
                ),
                "",
            )
            if match:
                candidate.held_names[slot] = match

    def _adopt_existing_slots(self, candidate: Candidate) -> None:
        """A remote server refused anonymously, declaring no credential: take the slots of an existing entry for the *same server*.

        The agent rediscovered developerknowledge.googleapis.com/mcp with no
        header and got 401, while this machine's developers-google-com -- the
        same URL -- sent ``Authorization`` and worked. Matched by server
        (:func:`.same_server.key`), not by host and "exactly one pending slot".
        Only references are taken; the value the entry uses stays in memory for
        the one read-only check (check_in_use).
        """
        if candidate.secret_env or any("${" in str(v) for v in candidate.headers.values()):
            return
        from . import same_server
        from .existing import ExistingRegistrations  # imports this module
        from .types import Stage

        settings = self.ctx.settings
        existing = ExistingRegistrations.for_run(self.ctx)
        mine = same_server.key(candidate, settings)
        for registration in existing.registrations():
            if existing.tier_of(registration.entry) != Stage.ENDPOINT:
                continue
            other = existing.candidate(registration, Stage.ENDPOINT, record_origin=False)
            if same_server.key(other, settings) == mine and same_server.inherit_credential(candidate, other, settings):
                return

    def _borrow_in_use(self, candidate: Candidate, trial: Mapping[str, Any] | None) -> None:
        """Offer the credential existing entries already use for this host to a new server for it.

        Only when ``borrow_in_use`` allows it, the candidate is waiting on
        exactly one credential, and the existing entries for its host use
        exactly one distinct value -- the same instance, one token. The value is
        set on :attr:`Candidate.in_use` (in memory, as a Secret) for the one
        read-only call; nothing is written.
        """
        if not self.verification.section("login_check").bool_("borrow_in_use", True):
            return
        pending = (
            list((trial or {}).get("pending") or [])
            if candidate.kind != "url"
            else [
                name
                for name in candidate.secret_env
                if not self.resolve_owned(
                    candidate, str(candidate.secret_env[name]), label=f"{candidate.label}:{name}", key=name
                )
            ]
        )
        if not pending:
            return
        from .existing import ExistingRegistrations  # imports this module
        from .grounding import hosts_named

        hosts = hosts_named([candidate.url, *candidate.full_command(), *candidate.env.values()])
        # A host named bare counts as much as one in a URL: @atlassian-dc-mcp/jira
        # reads JIRA_HOST=sd.example.com, and was never offered the token this
        # machine already sends sd.example.com -- so it stayed "waiting, unchecked"
        # and kept the address from the bridge whose login was accepted.
        hosts += [
            str(v).split(":", 1)[0].lower()
            for v in candidate.env.values()
            if _bare_host(str(v)) and str(v).split(":", 1)[0].lower() not in hosts
        ]
        if not hosts:
            return
        existing = ExistingRegistrations.for_run(self.ctx)
        values = existing.credentials_for(hosts[0]) if len(pending) == 1 else []
        # A credential goes only into a slot that holds the same kind of value:
        # build.example.net's password, the one secret its old entry carried, was
        # offered to the new bridge as its USERNAME, and the server refused the
        # key "for your account" -- recorded as a refusal of the key route.
        # (mcp.verification.login_check.borrow_same_part)
        if values and self.verification.section("login_check").bool_("borrow_same_part", True):
            from . import inventory

            wanted = inventory.slot_part(pending[0], self.ctx.settings)
            values = [
                v
                for v in values
                if not wanted or inventory.slot_part(existing.slot_of(v), self.ctx.settings) in ("", wanted)
            ]
        if len(values) == 1:
            borrowed = values[0]
            candidate.in_use[pending[0]] = type(borrowed)(
                borrowed.reveal(),
                source=f"the credential this machine already uses for {hosts[0]} ({borrowed.source})",
            )
            return
        self._borrow_held(candidate, pending, hosts[0])

    def _borrow_held(self, candidate: Candidate, pending: list[str], host: str) -> None:
        """Offer what the operator already exports (:mod:`.inventory`) to a server waiting for it -- or, a login, the same login as another address of the organisation.

        build.example.net: the shell exported BUILD_EXAMPLE_NET_BASE64_OF_USER_PASSWORD,
        and mcp-openapi-proxy's ``EXTRA_HEADERS`` takes exactly ``Authorization:
        Basic <that>`` -- but the run asked for a new variable. Each pending slot
        is filled from the held parts by what it holds (:func:`.inventory.slot_part`),
        all or none, in memory, for the one read-only login check; accepted, the
        entry reads the held variables (``reuse_names``, ``composed``). With
        nothing held for ``host``, a user name and password held for another
        address of the same organisation is tried the same way (single sign-on)
        -- never written under that address's names, only suggested.
        """
        from . import inventory

        if not self.verification.section("login_check").bool_("borrow_held", True):
            return
        settings = self.ctx.settings
        known = inventory.known_environ(self.ctx)
        mine = inventory.held(host, settings, known)
        values: dict[str, str] = {}
        names: dict[str, str] = {}
        for method in ("basic", "token", "apikey"):
            login = mine.get(method)
            if not login:
                continue
            if method == "basic":
                values.update(inventory.values_for(login, known))
            else:
                values.update({part: known.get(variable, "") for part, variable in login.items()})
            names.update(login)
        source = f"what you export for {host}"
        same = False
        if not values:
            for other in inventory.same_login_hosts(host, inventory.run_hosts(self.ctx), settings):
                login = inventory.held(other, settings, known).get("basic")
                if login:
                    values, same = inventory.values_for(login, known), True
                    source = f"the login you export for {other} (single sign-on: {', '.join(sorted(login.values()))})"
                    break
        if not values:
            return
        filled: dict[str, tuple[str, str]] = {}
        for slot in pending:
            part = inventory.slot_part(slot, settings)
            value = inventory.fill_slot(part, values) if part else ""
            if not value:
                return  # all of them or none: half a login proves nothing
            filled[slot] = (value, part)
        for slot, (value, part) in filled.items():
            candidate.in_use[slot] = Secret(value, source=source)
            if same:
                continue
            if part in names:
                candidate.held_names[slot] = names[part]
            elif part.startswith("FORM:"):
                template = part[len("FORM:") :]
                needed = re.findall(r"\{([A-Z_]+)\}", template)
                if all(n in names for n in needed):
                    candidate.held_names[slot] = re.sub(
                        r"\{([A-Z_]+)\}", lambda m: "${" + names[m.group(1)] + "}", template
                    )
                elif "ENCODED" in needed and "USERNAME" in names and "PASSWORD" in names:
                    # Only the parts are exported: built at start-up (McpWriter._compose_launch).
                    candidate.held_names[slot] = "COMPOSE:" + re.sub(
                        r"\{([A-Z_]+)\}",
                        lambda m: "{b64:%s:%s}" % (names["USERNAME"], names["PASSWORD"])
                        if m.group(1) == "ENCODED"
                        else "{" + names.get(m.group(1), m.group(1)) + "}",
                        template,
                    )
        if same:
            candidate.add_note(f"offered {source} for its login check")

    def resolve_owned(self, candidate: Candidate, reference: str, *, label: str, key: str = "") -> Secret | None:
        """A credential reference resolved as the written entry will read it -- through its owned name.

        bugzilla-example-com's trial resolved ``env:BUGZILLA_API_KEY`` from the
        run's environment, where a shell exported a revoked key for something
        else; the entry was then written reading
        ``${BUGZILLA_EXAMPLE_COM_BUGZILLA_API_KEY}``. A trial is only worth
        anything if it reads what the entry will: an ``env:NAME`` the writer
        would rename (:meth:`.McpWriter.owned_reference`) resolves that owned
        name; the generic one only from Claude Code's settings -- what the
        writer carries over -- never from a shell's export.
        (``mcp.registration.own_reference_names``.)
        """
        scheme, _, variable = reference.partition(":")
        variable = variable.strip()
        if scheme.strip().lower() != "env" or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", variable):
            return self.ctx.secrets.resolve(reference, required=False, label=label)
        if key and variable != key:
            # A name other than the key it fills was chosen on purpose -- by the
            # operator, an existing entry, a repair (EXTRA_HEADERS from
            # ${BUILD_BASE64}): read as written. Only the server's own generic
            # variable (BUGZILLA_API_KEY for BUGZILLA_API_KEY) is what a shell
            # may export for something else.
            return self.ctx.secrets.resolve(reference, required=False, label=label)
        owned = self._owned_namer(candidate)(variable)
        if owned == variable:
            return self.ctx.secrets.resolve(reference, required=False, label=label)
        found = self.ctx.secrets.resolve(f"env:{owned}", required=False, label=label)
        if found is not None:
            return found
        kept = self._settings_value(variable)
        if kept:
            redact.register(kept)
            return Secret(kept, source=f"{variable} in Claude Code's settings")
        if os.environ.get(variable):
            log.info(
                "%s: %s is exported here, but the entry reads %s -- a generic export is never what it sends",
                label,
                variable,
                owned,
            )
        return None

    def _owned_namer(self, candidate: Candidate) -> Callable[[str], str]:
        """``name -> the name the written entry reads it by`` (:meth:`.McpWriter.owned_reference`) for ``candidate``."""
        from .writer import McpWriter

        writer = McpWriter(self.ctx)
        host = self._host_of(candidate)
        server = candidate.existing_name or (writer.base_name(f"https://{host}") if host else "")
        return lambda name: writer.owned_reference(name, server, host=host) if server else name

    def _settings_value(self, name: str) -> str:
        """``name`` from Claude Code's settings.json env, where the run keeps what an entry needs ("" none)."""
        provider = getattr(self.ctx.secrets, "fallback_env", None)
        try:
            return str((provider() or {}).get(name) or "") if callable(provider) else ""
        except Exception:  # noqa: BLE001 - a lookup must never cost the trial
            return ""

    def _supplied_for(self, candidate: Candidate) -> dict[str, str]:
        """``{variable: value}`` the existing entries for the candidate's host supply (ExistingRegistrations.supplied_values)."""
        from .existing import ExistingRegistrations  # imports this module

        host = self._host_of(candidate)
        if not host:
            return {}
        try:
            return {k: v[0] for k, v in ExistingRegistrations.for_run(self.ctx).supplied_values(host).items()}
        except Exception:  # noqa: BLE001 - a lookup must never cost the trial
            return {}

    @staticmethod
    def _host_of(candidate: Candidate) -> str:
        from .grounding import hosts_named

        hosts = hosts_named([candidate.url, *candidate.full_command(), *candidate.env.values()])
        hosts += [str(v).split(":", 1)[0].lower() for v in candidate.env.values() if _bare_host(str(v))]
        return hosts[0] if hosts else ""

    def unread_credentials(self, candidate: Candidate) -> list[dict[str, str]]:
        """Credential variables of the entry that what it launches never reads, as ``{name, remedy}``.

        build.example.net's entry held its login in
        ``BUILD_EXAMPLE_NET_BASE64_OF_USER_PASSWORD``; ``mcp-openapi-proxy`` reads
        ``API_KEY`` and ``EXTRA_HEADERS``, so every call went out with no login
        and the token was blamed. Flagged only on evidence: the reader's source
        (the script, or the installed package -- :meth:`.EnvContract.all_reads`)
        never mentions the name *and* reads a credential under another one. A
        source that reads its credential through a computed name (a generated
        bridge, from its manifest) has no such read, and nothing is flagged.
        """
        if candidate.kind != "stdio" or not self.verification.section("login_check").bool_(
            "flag_unread_credentials", True
        ):
            return []
        from .env_contract import EnvContract

        contract = EnvContract(self.ctx.settings)
        reader, reads, files = contract.all_reads(candidate.full_command())
        offered = sorted(name for name in reads if is_secret_name(name))
        if not reads or not offered:
            return []
        problems = []
        for key in sorted({*candidate.env, *candidate.secret_env}):
            held = (
                key in candidate.secret_env
                or is_secret_name(key)
                or holds_a_credential(key, str(candidate.env.get(key, "")))
            )
            if not held or key in reads or contract.ignored(key) or contract.mentions(key, files):
                continue
            problems.append(
                {
                    "name": key,
                    "remedy": (
                        f"{key} is not a variable {reader} reads, so the credential in it is never sent -- every call "
                        f"goes out without a login. {reader} reads its credential from {', '.join(offered)}: put it "
                        f"there (as a ${{...}} reference) and remove {key}"
                    ),
                }
            )
        return problems

    def public_calls(self, candidate: Candidate) -> list[dict[str, Any]]:
        """The tools that need no login on a bridge generated from an API kind; see :func:`.api_kinds.public_calls`."""
        if not candidate.api_kind:
            return []
        from . import api_kinds

        return api_kinds.public_calls(self.ctx.settings, candidate.api_kind)

    def public_refusal(self, candidate: Candidate, shake: Handshake) -> str:
        """Why a bridge's public call shows it does not reach its API, in words; "" otherwise.

        A login refusal is the API answering: the call that once needed no login
        needs one now (sd.example.com's servicedeskapi/info came to answer 401).
        That shows the bridge *does* reach its API, so the bridge is waiting
        for a credential like any other -- never "broken"
        (``mcp.verification.public_call_login_markers``).
        """
        check = shake.public_check
        if not shake.ok or not check or check.get("answered"):
            return ""
        detail = str(check.get("detail") or "")
        markers = self.verification.str_list("public_call_login_markers", list(DEFAULT_PUBLIC_CALL_LOGIN_MARKERS))
        if any(re.search(m, detail, re.IGNORECASE) for m in markers if m):
            candidate.add_note(
                f"its {check.get('tool')} tool, which needed no login, now asks for one ({detail[:120]})"
            )
            return ""
        api = f"the {candidate.api_kind} API" if candidate.api_kind else "the API"
        return (
            f"its {check.get('tool')} tool, which needs no login, answered {check.get('detail') or 'nothing'} "
            f"-- the bridge does not reach {api} it was built for, so it is not waiting for a credential, it is broken"
        )

    def _trial_stdio(self, candidate: Candidate, trial: dict[str, Any]) -> Handshake:
        command = candidate.full_command()
        if not command:
            return Handshake(transport=Transport.STDIO, error="no launch command")

        binary = self.ctx.runner.which(command[0]) or self._provisioned(command[0], candidate, "launch command")
        if not binary:
            return Handshake(
                transport=Transport.STDIO,
                url=" ".join(command),
                error=f"{command[0]} is not installed",
            )
        if not self.packages.bool_("trial_run", True):
            return Handshake(
                transport=Transport.STDIO,
                url=" ".join(command),
                error="trial runs disabled (mcp.packages.trial_run = false)",
                not_launched="trial runs disabled (mcp.packages.trial_run = false)",
            )
        if self.ctx.dry_run:
            # Launching a downloaded server executes third-party code.
            return Handshake(
                transport=Transport.STDIO,
                url=" ".join(command),
                error="not launched: dry run (a trial would download and execute this package)",
                not_launched="dry run",
            )

        unusable = self._unusable_launcher(command[0])
        if unusable:
            # Installed and unable to work here -- Docker with its daemon
            # stopped: not launched, so no verdict on the server either way.
            return Handshake(
                transport=Transport.STDIO,
                url=" ".join(command),
                error=f"not launched: {unusable}",
                not_launched=unusable,
            )

        missing = self._ensure_required(candidate)
        if missing:
            return Handshake(transport=Transport.STDIO, url=" ".join(command), error=missing)

        for step in candidate.install:
            argv = [str(s).strip() for s in step if str(s).strip()]
            # An install step has to be a command. When one is not -- an agent
            # answering the question in prose, a blank list -- running its first
            # word is not a smaller version of the right thing, it is a
            # different thing. build.example.net reported "prerequisite needs the,
            # which is not installed", which is what a sentence looks like after
            # word zero is mistaken for a program.
            if not argv or not _looks_like_a_command(argv[0]):
                return Handshake(
                    transport=Transport.STDIO,
                    url=" ".join(command),
                    error=(
                        "prerequisite is not a command: "
                        f"{' '.join(argv) or '(empty)'} -- an install step must be an "
                        "argument vector, not a description of one"
                    ),
                )
            # Resolved per step rather than all at once: a step may be what puts
            # the next step's command on the PATH.
            if not self.ctx.runner.which(argv[0]) and not self._provisioned(argv[0], candidate, "install step"):
                return Handshake(
                    transport=Transport.STDIO,
                    url=" ".join(command),
                    error=(f"prerequisite needs {argv[0]}, which is not installed (step: {' '.join(argv)})"),
                )
            # Serialised per command line: under --concurrency two addresses can
            # settle on the same package, and the same install run twice at
            # once into the same place is how a half-written environment gets
            # trialled. Different commands still run side by side.
            with INSTALL_STEPS(tuple(argv)):
                result = self.ctx.runner.run(
                    argv,
                    timeout=self.packages.float_("trial_timeout_seconds", 90),
                    label="mcp prerequisite",
                )
            if not result.ok:
                return Handshake(
                    transport=Transport.STDIO,
                    url=" ".join(command),
                    error=f"prerequisite failed: {' '.join(argv)} -- {_why(result)}",
                )

        # Present is not runnable: a launcher whose interpreter is another
        # machine's, or whose interpreter lacks the package it starts. Asked
        # after the prerequisites -- they may be what installs it -- and only of
        # a file that is there (a missing one is the launch's own answer).
        from . import launch_form

        launched = self.ctx.runner.which(command[0]) or str(binary)
        if launched and os.path.isfile(str(launched)):
            cannot = launch_form.runnable(str(launched), self.ctx.settings, which=self.ctx.runner.which)
            if cannot:
                return Handshake(
                    transport=Transport.STDIO,
                    url=" ".join(command),
                    error=f"{command[0]} cannot run here: {cannot}",
                )

        secret_env: dict[str, Any] = {}
        pending: list[str] = []
        for name, reference in candidate.secret_env.items():
            secret = self.resolve_owned(candidate, str(reference), label=f"{candidate.label}:{name}", key=name)
            if secret is None:
                pending.append(name)
                continue
            secret_env[name] = secret

        if pending and not self.packages.bool_("trial_without_secrets", True):
            return Handshake(
                transport=Transport.STDIO,
                url=" ".join(command),
                error=f"required secret {pending[0]} is not available (reference: {candidate.secret_env[pending[0]]})",
            )

        # A variable that was named but never given a value is dropped, not
        # failed on. The writer already omits empty values from the entry, so
        # failing here trialled one launch and would have written another --
        # and the trial is only worth anything if it is the launch that ships.
        #
        # Dropping is also the only honest option: a name with no value tells us
        # nothing about whether the server needs it. Writing "${NAME}" for it is
        # how BUGZILLA_AUTH_MODE reached .mcp.json. See Candidate.proven_without.
        blank = sorted(name for name, value in candidate.env.items() if not value)
        for name in blank:
            candidate.add_note(
                f"{name} was named with no value, so it is neither trialled nor written: "
                "nothing here knows what it should hold, and an empty variable is a guess "
                "that the server has to survive"
            )

        if pending:
            # Launch it anyway. A stdio server almost always reads its
            # credential when a *tool is called*, not at start-up, so one
            # trialled without that credential still completes the handshake
            # and still advertises everything it can do -- which is far more
            # than we learn by refusing to start it.
            #
            # Not doing this is what lost confluence.example.com: the agent found
            # `confluence-datacenter-mcp`, honestly declared that it wants
            # CONFLUENCE_API_TOKEN, and that honesty disqualified it before
            # launch. An irrelevant package that declared no secrets started,
            # handshaked, and was registered in its place. The candidate was
            # punished for describing its own requirements.
            log.info(
                "%s: trialling without %s -- a handshake proves what it is, "
                "and the credential is only needed to call its tools",
                candidate.label,
                ", ".join(sorted(pending)),
            )

        env = {k: v for k, v in candidate.env.items() if v}
        # ${NAME} inside a setting is expanded the way Claude Code will expand
        # it -- EXTRA_HEADERS = "Authorization: Basic ${VAR}" -- in memory, as a
        # secret; a reference nothing resolves leaves the server waiting for it.
        # ${NAME:-default} too, as Claude Code expands it: NAME when set, else
        # the default. Left as text, the optional key file of a client bridge
        # reached the server as the literal "${..._SSH_KEY_FILE:-}".
        reference = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")
        templates: dict[str, str] = {}
        for key, value in list(env.items()):
            refs = reference.findall(str(value))
            names = [n for n, _ in refs]
            if not names or key in secret_env:
                continue
            defaults = {n: d for n, d in refs if f"${{{n}:-" in str(value)}
            # Only a credential is handled as one. A setting's value -- a region,
            # a mode, a default such as "fallback" -- read through the secret
            # resolver was registered for redaction for the rest of the process,
            # and every later line carrying that word was scrubbed.
            secretish = is_secret_name(key) or key in candidate.secret_env or any(is_secret_name(n) for n in names)
            resolved = {
                n: self.resolve_owned(candidate, f"env:{n}", label=f"{candidate.label}:{n}", key=key)
                if secretish
                else (os.environ.get(n) or self._settings_value(n) or None)
                for n in names
            }
            for n in list(resolved):
                if resolved[n] is None and n in defaults:
                    resolved[n] = defaults[n]
            missing_now = [n for n, v in resolved.items() if v is None]
            if missing_now and self.verification.bool_("use_supplied_values", True):
                # What an existing entry for the address supplies for the same
                # variable (its ${NAME:-value}, or a literal under that key) is
                # what the operator gave: the new entry is tried with it, and
                # the writer keeps it where Claude Code reads it.
                supplied = self._supplied_for(candidate)
                for n in missing_now:
                    if n in supplied:
                        value = supplied[n]
                        resolved[n] = (
                            Secret(value, source=f"{n}, as the existing entry supplied it") if secretish else value
                        )
            missing = [n for n, s in resolved.items() if s is None]
            if missing:
                env.pop(key)
                # Kept as written: a value the operator's credential is filled
                # into (HEADERS = '{"Authorization": "Bearer ${TOKEN}"}') is
                # rebuilt once that credential is offered (check_in_use).
                templates[key] = str(value)
                if is_secret_name(key) or key in candidate.secret_env or any(is_secret_name(n) for n in missing):
                    pending.extend(n for n in missing if n not in pending)
                elif key not in candidate.unset_settings:
                    # A setting nothing sets -- a mode, a selector -- is not a
                    # credential: launched without it, never waited for.
                    candidate.unset_settings.append(key)
                    candidate.add_note(f"{key} reads {', '.join(missing)}, which nothing sets: launched without it")
                continue

            def fill(match: re.Match[str]) -> str:
                found = resolved.get(match.group(1))
                return found.reveal() if hasattr(found, "reveal") else str(found or "")

            expanded = reference.sub(fill, str(value))
            env.pop(key)
            if expanded and (secretish or holds_a_credential(key, expanded)):
                secret_env[key] = Secret(expanded, source=f"{key}, with {', '.join(names)} filled in")
            elif expanded:
                env[key] = expanded
        # The entry for a target whose certificate needed this run's bundle
        # will name it (see McpWriter.entry_for), so the trial names it too.
        bundle = TrustStore.for_run(self.ctx).bundle_for([candidate.url, *command, *env.values()])
        # ...and where no host needed the run's bundle, the entry names this
        # host's own (trust.env_for): the trial does too. Node keeps its own
        # store and ignores the host's; gitlab.example.net's mcp-graphql was trialled
        # with nothing, "fetch failed", although the entry it would have become
        # named the host bundle. (mcp.trust.trial_names_host_bundle)
        if not bundle and self.ctx.settings.bool_("mcp.trust.trial_names_host_bundle", True):
            bundle = trust.system_bundle(self.ctx.settings)
        env.update(trust.launch_env_for(command[0], self.ctx.settings, bundle, env))
        # The trial names the bundle the entry will name: a file it brought that
        # does not carry its hosts' chains to a root is replaced here exactly as
        # McpWriter.entry_for replaces it, so the launch tried is the one shipped.
        checked = trust.check_named_bundles(
            env,
            trust.https_hosts([candidate.url, *command, *env.values()]),
            self.ctx.settings,
            TrustStore.for_run(self.ctx),
        )
        env.update(checked.replaced)
        for line in checked.expiring:
            candidate.add_note(line)
        launch = {
            "command": [binary, *command[1:]],
            "env": env,
            "secret_env": secret_env,
            "cwd": self.trial_dir() if self.packages.bool_("sandbox_install", True) else None,
            # What this launch claims not to have is really not there: the
            # trial inherits this run's environment, so a credential still
            # pending would reach the child from it, and every judgement made
            # "without the credential" (works without a login, a credential
            # wall, the stand-in comparison) would be made with it.
            "withhold": self._withheld(candidate, command, env, secret_env, pending, templates),
        }
        timeout = float(trial.get("timeout_override") or self.packages.float_("trial_timeout_seconds", 90))
        trial.update(launch=launch, timeout=timeout, pending=list(pending), templates=dict(templates))
        # Checked only with real values: a credential still pending is not
        # there to check, and the stand-in is never one.
        login = not pending and (bool(secret_env) or real_credential(env))
        if login and not self.login_budget_left(credential_fingerprint(secret_env.values())):
            # Refused often enough already in this run: not asked again, so the
            # run never adds to the refusals that lock an account.
            login = False
            candidate.add_note("its credential was refused repeatedly earlier in this run, so it was not tried again")
        shake = self.judge_login(
            self.prober.probe_stdio(
                **launch,
                timeout=timeout,
                login=login,
                login_tokens=self.login_tokens(candidate),
                public_calls=self.public_calls(candidate),
                # The usage tasks need what the server needs: a real credential, or none.
                contract=self.contract_plan(candidate) if (login or not (pending or secret_env)) else None,
            )
        )
        if login:
            self._confirm_by_own_record(candidate, shake, secret_env)
        if (
            pending
            and shake.ok
            and shake.tools
            and self.verification.section("login_check").bool_("try_without_credential", True)
        ):
            pending = self._without_login(candidate, shake, launch, timeout, pending)
            trial["pending"] = list(pending)
        if login:
            shake.login_credential = credential_fingerprint(secret_env.values())
            shake.login_parts = secret_part_prints(secret_env)
            self.count_refusal(shake)
        broken = self.public_refusal(candidate, shake)
        if broken:
            # Not waiting for a credential: the API it was built for is not
            # there. Recorded as a failure, so it is neither verified nor
            # registered with an empty slot.
            shake.ok = False
            shake.error = broken
            return shake
        # Every value this launch carries -- exported, in use by an existing
        # entry, or written as a literal -- held up against the shape its slot
        # takes. A bare token in a header list is no header at all.
        shake.shape_problems = self.shape_problems(candidate, {**env, **secret_env, **candidate.in_use})
        shake.shape_problems += self.unread_credentials(candidate)
        if login and not shake.ok:
            self.credential_failure(shake)
        advertised_something = bool(shake.tools or shake.resources or shake.prompts)
        # A handshake only counts as a credential wall if the server also said
        # what it can do -- see the second branch. Turning the requirement off
        # restores the older, credulous reading rather than falling through to
        # "it did not answer", which would be a third and wrong verdict.
        wall = shake.ok and (advertised_something or not self.packages.bool_("credential_wall_needs_tools", True))
        if pending and wall:
            # Recorded on the handshake rather than folded into ``ok``: the
            # exchange really did complete, and saying otherwise would throw
            # away the strongest evidence we have about this candidate. What it
            # changes is ``Candidate.verified`` -- the promise that the entry
            # works as written, which cannot be made with an empty slot.
            shake.credential_pending = sorted(pending)
            shake.add_hint(
                "handshaked and listed its tools without a credential; "
                f"{', '.join(sorted(pending))} is needed before any tool call will work"
            )
        elif pending and shake.ok and (gated := self._credential_test(candidate, launch, pending, shake)):
            # It listed nothing without the credential, and everything with a
            # stand-in for it: the credential is what it was waiting for. See
            # _credential_test, and mcp.packages.credential_probe.
            return gated
        elif pending and shake.ok:
            # It started, it handshaked, and it advertised **nothing** -- and
            # the second trial, with a stand-in for the credential, did not
            # change that (or could not be run). The
            # justification for the branch above is that a stdio server reads
            # its credential when a tool is *called*, so one trialled without
            # the credential "still advertises everything it can do". With an
            # empty capability list that justification is absent: we learned
            # nothing, and nothing points at the credential as the reason.
            #
            # Calling it a credential wall anyway is a guess that costs the
            # whole target. `mcp-atlassian` for confluence.example.com handshaked,
            # listed zero tools, and was recorded as "a real MCP endpoint that
            # refused an anonymous client" -- which made it provisional, settled
            # the target, closed tiers 3 and 4, and shipped an entry naming
            # `PERSONAL_TOKEN`, a variable that package does not read. Supplying
            # it changed nothing, because it was never the missing piece.
            #
            # An empty list has several causes and the credential is only one:
            # a variable named wrongly, a base URL the server rejected, a
            # package that needs an argument nobody passed. Leaving the
            # candidate unaccepted lets `accepts()` say the true thing -- "server
            # exposes no tools, resources or prompts" -- and lets the lower tiers
            # run, which is what this target needed.
            #
            # The credential test above is what makes that judgement evidence
            # rather than suspicion: the variable was supplied (as a stand-in)
            # and the list stayed empty, so supplying the real one is not known
            # to help either.
            shake.add_hint(
                f"it started without {', '.join(sorted(pending))} and then advertised no tools, "
                "resources or prompts. That is not evidence of a credential wall: a server "
                "kept waiting for a token still lists what it can do. Something else is wrong "
                "-- commonly the variable is not the one this package reads, or its base URL "
                "was rejected -- so it is not registered as an endpoint waiting for a token"
            )
            log.info(
                "%s: started without %s and advertised nothing -- not recorded as a "
                "credential wall, because an empty capability list is not evidence of one",
                candidate.label,
                ", ".join(sorted(pending)),
            )
        elif (
            pending
            and (said := self.refused_to_start_without(shake, pending))
            and (
                gated := self._credential_test(candidate, launch, pending, shake, quiet=True)
                if not self.looks_transient(shake, timeout)
                else None
            )
        ):
            # It said the credential is why -- and a stand-in for it either
            # proved that, or moved the failure on to the rest of a credential
            # in parts and found it (_companion_test). Either answer is fuller
            # than the words alone: osc-mcp said "password not found", and with
            # a stand-in password said "user not set".
            return gated
        elif pending and shake.standin_unchanged and not any(n and n in (shake.error or "") for n in pending):
            # It said something credential-shaped -- but a stand-in for every
            # withheld credential left the failure exactly as it was. A marker is
            # weaker evidence than that (a message naming the withheld variable
            # itself is not, and keeps the branch below), so this is not a
            # credential wall: it
            # refuses to start whatever credential it is given. Telling the
            # operator to export OSC_MCP_PASSWORD would have changed nothing.
            shake.credential_test = (
                f"with {', '.join(sorted(pending))} set to a stand-in value it failed exactly the same way, "
                "so it refuses to start whatever credential it is given -- the credential is not "
                f"what it is missing{_saying(shake)}"
            )
            shake.add_hint(shake.credential_test)
            log.info(
                "%s: fails the same with and without %s -- not a credential wall (it said: %s)",
                candidate.label,
                ", ".join(sorted(pending)),
                self.excerpt(shake.error) or "nothing",
            )
        elif pending and (said := self.refused_to_start_without(shake, pending)):
            # It did not answer, and it *said why*: the variable we left out.
            #
            # The branch below is right that a crash is not a credential wall,
            # but "did it answer?" cannot tell a server that broke for an
            # unrelated reason from one that correctly refuses to start without
            # its token -- and both exist. gitlab.example.net was lost to exactly
            # that conflation: the bridge stage had already built a launcher,
            # pinned GITLAB_URL at it, and verified it end-to-end, and the trial
            # without GITLAB_TOKEN was filed as a plain failure. Nothing
            # downstream could recover a good server from that verdict, so the
            # target finished with no entry at all.
            #
            # What separates the two cases is the same thing that separates a
            # real 401 from an intranet's blanket 401: evidence from the server
            # itself. Either its message names a variable we withheld, or it is
            # auth-shaped by ``mcp.packages.startup_credential``. A traceback in
            # someone else's connection pool matches neither.
            #
            # Recorded on a handshake whose ``ok`` stays false, which is what
            # makes this safe: ``verified`` remains false, ``proven_without``
            # stays empty so the writer keeps the ``${VAR}`` slot the server just
            # demanded, and ``needs_credential`` becomes true -- registered
            # provisionally, credential named, never counted as working.
            shake.credential_pending = sorted(pending)
            shake.add_hint(
                f"it refused to start without {', '.join(sorted(pending))} and said so "
                f"({said}). A server that names the variable it is missing is telling us "
                "the credential is the missing piece, so it is registered as an endpoint "
                "waiting for a token rather than discarded as broken"
            )
            log.info(
                "%s: refused to start without %s and named it -- recorded as a credential "
                "wall on the server's own evidence (%s)",
                candidate.label,
                ", ".join(sorted(pending)),
                said,
            )
        elif (
            pending
            and not self.looks_transient(shake, timeout)
            and (gated := self._credential_test(candidate, launch, pending, shake))
        ):
            # It crashed without naming anything, and started and listed its
            # tools once the variable held a stand-in. Silence about the reason
            # is not evidence against the credential; this is evidence for it.
            # @atlassian-dc-mcp/confluence exits without CONFLUENCE_API_TOKEN
            # and never says so, and was discarded as broken for exactly that.
            return gated
        elif pending:
            # It did not answer, and nothing it said points at the credential
            # -- and neither did a second trial with a stand-in for it.
            #
            # This condition used to be unguarded, so any launch that failed
            # while a secret happened to be missing was recorded as "a real MCP
            # endpoint that refused an anonymous client". `@ivotoby/openapi-mcp-server`
            # for build.example.net exited rc=1 with a Node stack trace in
            # `ToolsManager.initialize`; that became a credential wall, which
            # made it provisional, which settled the stage, which closed tiers 3
            # and 4 -- and wrote an entry whose `${...}` nobody can fill, because
            # the process dies before it reads it. smelt.example.net went the same
            # way. A broken entry is worse than none: it looks like success.
            shake.add_hint(
                f"it was launched without {', '.join(sorted(pending))} and did not answer, "
                f"and nothing it said names those variables{_saying(shake)}. That is a "
                "failure and not a credential wall -- supplying the variable is not known "
                "to fix it, so this is not registered as an endpoint waiting for a token"
            )
            log.info(
                "%s: failed without %s and did not name them -- recorded as a failure, "
                "not as needing a credential (it said: %s)",
                candidate.label,
                ", ".join(sorted(pending)),
                self.excerpt(shake.error) or "nothing",
            )
        if (
            not shake.ok
            and not shake.credential_pending
            and not trial.get("isolated")
            and not self.looks_transient(shake, timeout)
        ):
            # It does not start, and a credential is not why. One cause no
            # repair to its variables can reach: it reads the operator's own
            # configuration, and that configuration breaks it.
            isolated = self._isolated_trial(candidate, trial, shake)
            if isolated is not None:
                return isolated
        return shake

    def same_failure(self, one: Handshake, other: Handshake) -> bool:
        """Do two failed launches say the same thing, once what differs between runs is removed?

        ``mcp.packages.credential_probe.volatile_patterns`` names the parts --
        timestamps, process ids, addresses, temporary paths -- so two identical
        failures a second apart are not taken for different ones.
        """
        patterns = self.packages.section("credential_probe").str_list("volatile_patterns", list(VOLATILE_PATTERNS))

        def normalised(shake: Handshake) -> str:
            text = " ".join(part for part in (shake.error, *shake.protocol_hints) if part)
            for pattern in patterns:
                try:
                    text = re.sub(pattern, " ", text)
                except re.error:
                    continue
            return " ".join(text.lower().split())

        left, right = normalised(one), normalised(other)
        return bool(left) and left == right

    def _isolated_trial(self, candidate: Candidate, trial: dict[str, Any], first: Handshake) -> Handshake | None:
        """Launch again with a configuration scope of its own; the verdict if that is better, else None.

        osc-mcp takes its API address from the operator's oscrc before its own
        ``OSC_MCP_API``, and refuses any example.net address -- so on a machine set
        up for build.example.net every launch for build.opensuse.org failed, with
        or without a credential. Pointed at an empty directory of its own
        (``[mcp.healing.isolation]``), it reads no oscrc, takes the entry's
        address, and asks for its credential like any server.

        "Better" is started, or waiting for a credential, where the first was
        neither. The candidate then keeps the scope -- the entry writes it,
        relocatable -- and the report says why. Otherwise nothing is kept.
        """
        config = self.ctx.settings.section("mcp.healing.isolation")
        if not config.bool_("enabled", True):
            return None
        scopes = config.dict_("env", {"HOME": "", "XDG_CONFIG_HOME": ".config"})
        if not scopes or all(str(candidate.env.get(k) or "").strip() for k in scopes):
            return None
        slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", candidate.label).strip("-") or "server"
        root = config.str_("directory", "${cfg:mcp.agent.workspace}/artifacts/isolated-config/{slug}")
        directory = (
            Path(root.replace("{slug}", slug)) if "${" not in root else self.sandbox_dir() / "isolated-config" / slug
        )
        isolated = {name: str(directory / sub) if str(sub) else str(directory) for name, sub in scopes.items()}
        try:
            for path in isolated.values():
                Path(path).mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            log.info("%s: cannot create a configuration scope of its own (%s)", candidate.label, exc)
            return None
        saved = dict(candidate.env)
        candidate.env.update(isolated)
        log.info(
            "%s: failed at start-up, and not for a credential -- trialling once more with %s pointed at "
            "a configuration of its own, in case the operator's configuration is what breaks it",
            candidate.label,
            ", ".join(sorted(isolated)),
        )
        inner: dict[str, Any] = {"isolated": True}
        second = self._trial_stdio(candidate, inner)
        if not (second.ok or second.credential_pending):
            candidate.env.clear()
            candidate.env.update(saved)
            first.add_hint(
                f"with a configuration of its own ({', '.join(sorted(isolated))}) it failed too"
                f"{_saying(second)}, so the operator's configuration is not what breaks it"
            )
            return None
        said = self.excerpt(first.error) or "it said nothing"
        now = (
            "started"
            if second.ok and not second.credential_pending
            else (f"asked for {', '.join(second.credential_pending)}" if second.credential_pending else "started")
        )
        second.config_isolated = sorted(isolated)
        second.config_interference = (
            f"it reads the operator's own configuration before its entry's: with this machine's it "
            f"refused to start ({said}); with a configuration of its own ({', '.join(sorted(isolated))}) "
            f"it {now}. The entry carries that scope, so no operator's configuration can break it"
        )
        second.add_hint(second.config_interference)
        candidate.add_note(second.config_interference)
        trial.update(inner)
        return second

    def _credential_test(
        self,
        candidate: Candidate,
        launch: Mapping[str, Any],
        pending: Sequence[str],
        first: Handshake,
        *,
        quiet: bool = False,
    ) -> Handshake | None:
        """Launch once more with each missing credential set to a stand-in.

        ``quiet`` leaves ``first`` untouched when the stand-in settles nothing,
        for a caller that already has the server's own words to go on.

        Returns the handshake to record when that settles it -- the server
        started and listed what it can do once the variable was set, so the
        credential is what it was waiting for -- and ``None`` when it does not,
        after noting on ``first`` what the second trial showed.

        Only asked when the first trial was *inconclusive*: it started and
        listed nothing, or it failed without naming a variable. Those were the
        two verdicts that lost confluence.example.com -- ``mcp-atlassian`` lists no
        tools for a service that is not fully configured, and
        ``@atlassian-dc-mcp/confluence`` exits without its token and does not
        say so. Both were filed as plain failures, and an agent proposed both
        again and again for an hour.

        The result is never "verified": ``ok`` is cleared and the names are put
        on ``credential_pending``, which is exactly the shape of a server that
        refused to start and named its variable -- registered provisionally with
        the ``${VAR}`` slot kept (``proven_without`` stays empty, because the
        launch that worked *had* the variable), counted apart from working
        servers, never promised to the operator.

        What makes this safe, and each is a rule rather than a hope:

        * No tool is called. The probe is ``initialize`` and the listing calls.
        * The stand-in goes to the child process and nowhere else. It is
          scrubbed from everything the server says back and registered with the
          redactor, so it cannot reach ``.mcp.json``, the log or the report.
        * A server whose list stays empty, or that still fails, is rejected as
          before -- so a variable that changes nothing is *proved* to change
          nothing, which is the ``PERSONAL_TOKEN`` mistake ruled out rather
          than merely avoided.
        """
        rule = self.packages.section("credential_probe")
        placeholder = rule.str_("placeholder", "initialize-claude-ai-stand-in-not-a-real-credential")
        if not rule.bool_("enabled", True) or not placeholder:
            return None
        names = sorted(pending)
        wanted = ", ".join(names)
        log.info(
            "%s: trialling once more with %s set to a stand-in value -- to learn whether "
            "the credential is what it is missing (no tool is called)",
            candidate.label,
            wanted,
        )
        test = self.prober.probe_stdio(
            launch["command"],
            env={**launch["env"], **{name: placeholder for name in names}},
            secret_env=launch["secret_env"],
            cwd=launch["cwd"],
            timeout=rule.float_("timeout_seconds", 90),
            withhold=launch.get("withhold", ()),
        )
        _scrub(test, placeholder)

        accepted, why = self.prober.accepts(test)
        listed = _listing(test)
        needs_tools = self.packages.bool_("credential_wall_needs_tools", True)
        if not (test.ok and accepted and (listed or not needs_tools)):
            # Failed the same way with the credential as without it: the
            # credential is proved not to be the missing piece, whatever words
            # the failure uses. osc-mcp said "failed to get credentials" both
            # times -- it was refusing the operator's oscrc, not asking for one.
            first.standin_unchanged = not first.ok and not test.ok and self.same_failure(first, test)
            # The stand-in may have been *used*: the server got past the
            # credential it was given and complained about another part of it.
            # osc-mcp for build.opensuse.org went from "password not found" to
            # "user not set for apiurl api.opensuse.org" -- a username and a
            # password are one credential, and only the password was declared.
            completed = self._companion_test(candidate, launch, names, first, test)
            if completed is not None or quiet:
                return completed
            if test.ok and not listed:
                seen = "it still listed nothing"
            elif test.ok:
                seen = f"it started but was still not accepted ({why})"
            else:
                seen = f"it still did not answer ({self.excerpt(test.error) or 'it said nothing'})"
            first.credential_test = (
                f"a second trial with {wanted} set to a stand-in value changed nothing: {seen}. "
                f"So {wanted} is not what it is missing -- look at the other variables it reads "
                "(most often the target's address) or the arguments it expects"
            )
            first.add_hint(first.credential_test)
            log.info(
                "%s: with %s set to a stand-in, %s -- the credential is not the missing piece",
                candidate.label,
                wanted,
                seen,
            )
            return None

        before = (
            "started and listed nothing"
            if first.ok
            else f"did not start ({self.excerpt(first.error) or 'it said nothing'})"
        )
        test.credential_test = (
            f"without {wanted} it {before}; with {wanted} set to a stand-in value it started "
            f"and listed {listed or 'its capabilities'}. {wanted} is what it is waiting for"
        )
        test.ok = False
        test.credential_pending = names
        test.error = test.credential_test
        # Read by the report's "connected" line for a candidate that needs a
        # credential -- see audit().
        test.auth_hint = f"a start-up credential ({wanted})"
        test.add_hint(
            f"it needs {wanted} before it will offer anything: it {before} without it, and "
            f"listed {listed or 'its capabilities'} once the variable held a stand-in value "
            "(never a real one). Registered as an endpoint waiting for a token, with the "
            "variable named and empty; it is not counted as working"
        )
        log.info(
            "%s: without %s it %s, and with a stand-in it listed %s -- recorded as waiting "
            "for that credential, never as verified",
            candidate.label,
            wanted,
            before,
            listed or "its capabilities",
        )
        return test

    def _companion_test(
        self,
        candidate: Candidate,
        launch: Mapping[str, Any],
        names: Sequence[str],
        first: Handshake,
        test: Handshake,
    ) -> Handshake | None:
        """Try the rest of a credential the stand-in showed is in several parts.

        Only when the stand-in moved the failure on -- the trial with it failed
        differently from the one without, and the new failure is credential-shaped
        (:meth:`refused_to_start_without` against the configured markers) -- so
        the server demonstrably consumed what it was given and wants more. The
        companions are named, never guessed at random: variables the new failure
        itself names, then the secret's own prefix with each of
        ``mcp.packages.credential_probe.companion_suffixes`` (``OSC_MCP_PASSWORD``
        → ``OSC_MCP_USER``). Each is tried with the stand-in, at most
        ``max_companion_trials`` launches. The first that makes the server start
        and list its tools joins the credential: declared as a slot like the
        secret, and recorded as waiting for both.
        """
        rule = self.packages.section("credential_probe")
        placeholder = rule.str_("placeholder", "initialize-claude-ai-stand-in-not-a-real-credential")
        budget = rule.int_("max_companion_trials", 3)
        if budget <= 0 or test.ok or not placeholder:
            return None
        before = re.sub(r"\d+", "", first.error or "").strip()
        after = re.sub(r"\d+", "", test.error or "").strip()
        if not after or after == before:
            return None
        if not self.refused_to_start_without(test, names):
            return None
        have = {k for k, v in (launch.get("env") or {}).items() if str(v).strip()} | set(names)
        mentioned = [
            token for token in re.findall(r"\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+\b", test.error or "") if token not in have
        ]
        derived: list[str] = []
        for name in names:
            prefix = name.rpartition("_")[0]
            if not prefix:
                continue
            for suffix in rule.str_list("companion_suffixes", ["USER", "USERNAME", "LOGIN"]):
                companion = f"{prefix}_{str(suffix).strip().upper()}"
                if str(suffix).strip() and companion not in have:
                    derived.append(companion)
        tried: list[str] = []
        for companion in dict.fromkeys([*mentioned, *derived]):
            if len(tried) >= budget:
                break
            tried.append(companion)
            extra = [*names, companion]
            log.info(
                "%s: with %s set to a stand-in it got further and asked for more (%s); trying "
                "%s as the other part of the credential (no tool is called)",
                candidate.label,
                ", ".join(names),
                self.excerpt(test.error) or "it said nothing",
                companion,
            )
            shake = self.prober.probe_stdio(
                launch["command"],
                env={**launch["env"], **{n: placeholder for n in extra}},
                secret_env=launch["secret_env"],
                cwd=launch["cwd"],
                timeout=rule.float_("timeout_seconds", 90),
                withhold=launch.get("withhold", ()),
            )
            _scrub(shake, placeholder)
            accepted, _why = self.prober.accepts(shake)
            listed = _listing(shake)
            needs_tools = self.packages.bool_("credential_wall_needs_tools", True)
            if not (shake.ok and accepted and (listed or not needs_tools)):
                continue
            candidate.secret_env.setdefault(companion, f"env:{companion}")
            wanted = ", ".join(sorted(extra))
            shake.ok = False
            shake.credential_pending = sorted(extra)
            shake.credential_test = (
                f"without {', '.join(names)} it did not start; with a stand-in for it alone it "
                f"asked for more ({self.excerpt(test.error) or 'it said nothing'}); with stand-ins "
                f"for {wanted} it started and listed {listed or 'its capabilities'}. The "
                "credential is in parts, and all of them are needed"
            )
            shake.error = shake.credential_test
            shake.auth_hint = f"a start-up credential in parts ({wanted})"
            shake.add_hint(
                f"it needs {wanted} before it will offer anything (shown with stand-in values, "
                "never real ones). Registered as an endpoint waiting for them, each slot named "
                "and empty; it is not counted as working"
            )
            log.info(
                "%s: needs %s together -- recorded as waiting for that credential, never as verified",
                candidate.label,
                wanted,
            )
            return shake
        if tried:
            first.add_hint(
                f"a stand-in for {', '.join(names)} moved the failure on, and stand-ins for "
                f"{', '.join(tried)} as its other part did not complete it"
            )
        return None

    def looks_transient(self, shake: Handshake | None, timeout: float = 0.0) -> bool:
        """Did this trial run out of clock rather than hear from the server?

        A verdict the server gave -- it crashed, it said something, it listed
        nothing -- is a property of the launch. One where nothing came back
        before the deadline is a property of this afternoon's network: a
        download that was slow, a registry that throttled us. Only the first
        kind is worth reusing or asking a follow-up question about.
        """
        if shake is None or shake.ok:
            return False
        rule = self.verification.section("reuse_verdicts")
        fraction = rule.float_("transient_elapsed_fraction", 0.9)
        if timeout > 0 and fraction > 0 and shake.elapsed >= timeout * fraction:
            return True
        text = (shake.error or "").lower()
        return self.answered_try_later(shake) or any(
            marker and marker.lower() in text
            for marker in rule.str_list(
                "transient_markers",
                [
                    "no response to initialize",
                    "timed out",
                    "timeout",
                    "temporarily unavailable",
                    "connection reset",
                    "rate limit",
                ],
            )
        )

    def answered_try_later(self, shake: Handshake | None) -> bool:
        """Did something in front of the server answer "come back later" -- a bot check, a challenge page?

        openqa.opensuse.org answered its MCP path with "Making sure you're not a
        bot!" (an HTTP 200 page) for a while, and the working entry was judged
        "does not start" and replaced by a cleartext endpoint. Such a page says
        nothing about the server behind it. (``reuse_verdicts.try_later_markers``.)
        """
        if shake is None:
            return False
        text = (shake.error or "").lower()
        return any(
            m and m.lower() in text
            for m in self.verification.section("reuse_verdicts").str_list("try_later_markers", list(TRY_LATER_MARKERS))
        )

    def excerpt(self, text: str) -> str:
        """A failure's own words, on one line, bounded and redacted.

        Bounded by ``mcp.packages.failure_excerpt_chars``; ``0`` turns it off.
        """
        limit = self.packages.int_("failure_excerpt_chars", 300)
        one = " ".join(str(text or "").split())
        if limit <= 0 or not one:
            return ""
        return redact.redact(one if len(one) <= limit else one[:limit].rstrip() + "...")

    def refused_to_start_without(self, shake: Handshake, pending: Sequence[str]) -> str:
        """Did the server say the withheld credential is why it would not start?

        Returns the words that carried it, or ``""``. Two kinds of evidence, in
        descending strength, and both come from the server rather than from us:

        1. **It named the variable.** ``RuntimeError: GITLAB_TOKEN is required``
           leaves nothing to interpret. Only names we actually withheld count --
           a message mentioning some *other* variable is about something else.
        2. **It was auth-shaped.** ``mcp.packages.startup_credential.markers``
           lists the wordings that qualify. Weaker, and deliberately so: it is
           only consulted because a server is entitled to say "authentication
           required" without naming the environment variable it reads.

        Clearing ``markers`` leaves rule 1 alone, which needs no configuration
        because the names come from the candidate. Setting ``enabled = false``
        restores the older behaviour of treating every failed trial as a plain
        failure.
        """
        rule = self.packages.section("startup_credential")
        if not rule.bool_("enabled", True):
            return ""
        text = " ".join(part for part in (shake.error, *shake.protocol_hints) if part)
        if not text.strip():
            return ""
        named = [name for name in sorted(pending) if name and name in text]
        if named:
            return f"it named {', '.join(named)}"
        lowered = text.lower()
        for marker in rule.str_list("markers", STARTUP_CREDENTIAL_MARKERS):
            if marker and marker.lower() in lowered:
                return f"it said {marker!r}"
        return ""

    # -- the entry, rather than the candidate it came from ----------------------

    def verify_as_written(
        self,
        entry: Mapping[str, Any],
        label: str = "",
        standins: Iterable[str] = (),
        kept: Mapping[str, Any] | None = None,
    ) -> Handshake | None:
        """Launch a finished ``.mcp.json`` entry exactly as the client will.

        Every other check in this module tests a *candidate*. This one tests the
        **entry**, and the two are not the same object: the candidate is trialled
        with the environment discovery assembled, while the entry is what ships,
        with ``${VAR}`` placeholders that may resolve to nothing. Three servers
        reached the operator's machine having passed every candidate check and
        died on start-up, because the launch that was proven and the launch that
        was written differed by one unset variable.

        ``standins`` names the credential slots of a server that is waiting for
        one: while unset, each is filled with the stand-in the trial used
        (``mcp.packages.credential_probe.placeholder``), so the entry is judged
        as it will run once the operator exports the value -- which is the
        launch the trial proved -- and not with a literal ``${NAME}`` that
        neither the trial nor the client would ever pass. The stand-in is
        scrubbed from everything the server says back.

        ``kept`` holds the values the writer keeps out of the file and in
        Claude Code's settings env (:attr:`.Candidate.kept_out`): the client
        expands ``${NAME}`` from there, so the entry is launched with them --
        otherwise the launch tested would not be the launch shipped. They are
        redacted from everything the server says back.

        Returns ``None`` when there is nothing to test here -- a remote entry
        (no process to start), or a run that may not launch anything.
        """
        if str(entry.get("command", "")).strip() == "":
            return None  # a URL entry starts no process
        if not self.enabled or self.ctx.dry_run:
            return None
        if not self.packages.bool_("trial_run", True):
            return None

        placeholder = self.packages.section("credential_probe").str_(
            "placeholder", "initialize-claude-ai-stand-in-not-a-real-credential"
        )
        filled = {name: placeholder for name in standins if name and placeholder and name not in os.environ}
        known = {}
        for name, value in (kept or {}).items():
            text = value.reveal() if hasattr(value, "reveal") else str(value or "")
            if name and text and name not in os.environ:
                redact.register(text)
                known[name] = text
        argv, env, gaps = launch_of(entry, {**os.environ, **known, **filled} if (filled or known) else None)
        if not argv:
            return None
        shake = self.prober.probe_stdio(
            argv,
            env=env,
            timeout=self.packages.float_("trial_timeout_seconds", 90),
            # The client's environment, not ours. Inheriting our own was what
            # made this check unable to predict the thing it exists to predict:
            # an entry could pass here on a variable from the operator's shell
            # and fail in Claude Code, which passes only what the entry declares.
            # See McpProber.client_env.
            inherit=not self.verification.bool_("client_env.strict", True),
            # The entry's credential, as the client would pass it -- checked
            # only when every credential variable resolved to a real value,
            # never when a stand-in fills one.
            login=not filled and real_credential(env),
        )
        self.judge_login(shake)
        if not shake.ok and not filled and real_credential(env):
            self.credential_failure(shake)
        if filled:
            _scrub(shake, placeholder)
        # The same refusal the candidate met: an entry whose tools break the
        # client's naming rules breaks the session it is loaded into, however
        # well it starts.
        refusal = self.prober.schema_refusal(shake)
        if shake.ok and refusal:
            shake.ok = False
            shake.error = refusal
        if gaps:
            # Said whether it started or not: a variable with no value and no
            # default is a hole in the entry either way, and if the server
            # tolerated it today it is still a hole tomorrow.
            shake.add_hint(
                f"the entry as written leaves {', '.join(gaps)} unresolved -- "
                "neither set in this environment nor given a ${NAME:-default}"
            )
        if not shake.ok:
            log.warning(
                "%s: the entry as written did not start (%s)",
                label or argv[0],
                redact.redact(shake.error or "no handshake"),
            )
        return shake

    # -- batches ---------------------------------------------------------------

    def verify_all(
        self,
        candidates: Sequence[Candidate],
        *,
        stop_at_first: bool = True,
        budget_seconds: float = 0.0,
        keep: Callable[[Candidate], bool] | None = None,
        memo: dict[str, Handshake] | None = None,
        why: Callable[[Candidate], str] | None = None,
    ) -> list[Candidate]:
        """Verify in order, optionally stopping at the first proven server.

        Each trial is announced before it starts, and its verdict after. A
        trial launches third-party code and can take minutes; when the log fell
        silent after the relevance filter on jira.example.com, nothing in it said
        which package was running, so nobody could tell a slow trial from a
        stuck one, or which process to look at.

        ``keep`` is the caller's registration gate. "Stop at the first" means
        the first server that can *be used* -- verified **and** registrable --
        because stopping at one that is then refused is stopping at nothing.
        confluence.example.com stopped at trial 1 of 17: ``confluence-mcp-server``
        handshaked, was refused at registration for naming no address, and the
        sixteen candidates behind it, the right ones among them, were never
        launched. Everything verified is still returned, so the caller's own
        gate reports the refusals exactly as before.

        ``memo`` is the target's record of verdicts already reached (see
        :attr:`TargetOutcome.trials`). A launch identical to one already judged
        for the target is answered from it rather than started again -- unless
        the earlier verdict looks like a timeout, which says nothing about the
        server. ``None`` trials everything, which is what :meth:`recheck` wants.
        """
        verified: list[Candidate] = []
        deadline = time.monotonic() + budget_seconds if budget_seconds > 0 else 0.0
        total = len(candidates)
        for index, candidate in enumerate(candidates, 1):
            if deadline and time.monotonic() > deadline:
                candidate.add_note("not attempted: stage budget exhausted")
                log.info(
                    "%s: not trialled -- the stage budget (%gs) is spent",
                    candidate.label,
                    budget_seconds,
                )
                continue
            launch = (
                redact.redact(candidate.url)
                if candidate.kind == "url"
                else " ".join(redact.redact_argv(candidate.full_command()))
            )
            if not self._reuse(candidate, memo):
                log.info("trialling %d of %d: %s -- %s", index, total, candidate.label, launch)
                self.verify(candidate)
                self._remember(candidate, memo)
            else:
                self.credit_auth_claim(candidate)
            self._report_trial(candidate)
            if candidate.verified:
                verified.append(candidate)
                if not stop_at_first:
                    continue
                if keep is None or keep(candidate):
                    break
                log.info(
                    "%s: verified, but the registration gate would refuse it (%s) -- trying the "
                    "next candidate rather than stopping at one that cannot be written",
                    candidate.label,
                    (why(candidate) if why else "") or "see the registration step",
                )
        return verified

    @staticmethod
    def launch_key(candidate: Candidate) -> str:
        """A fingerprint of everything that decides how a candidate is launched.

        Two candidates with the same key start the same process with the same
        environment -- or dial the same URL with the same headers -- so a
        server's verdict on one is its verdict on the other. Variable and header
        *values* are hashed, never stored; secrets are represented by their
        reference, which is what the launch resolves.
        """
        parts = {
            "kind": candidate.kind,
            "url": candidate.url,
            "transport": candidate.transport,
            "command": candidate.full_command(),
            "env": sorted(candidate.env.items()),
            "secret_env": sorted(candidate.secret_env.items()),
            "headers": sorted(candidate.headers.items()),
            "install": [list(step) for step in candidate.install],
        }
        blob = json.dumps(parts, sort_keys=True, default=str)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    def _reuse(self, candidate: Candidate, memo: dict[str, Handshake] | None) -> bool:
        """Answer a launch already judged for this target from its verdict."""
        if memo is None or not self.enabled:
            return False
        if not self.verification.section("reuse_verdicts").bool_("enabled", True):
            return False
        earlier = memo.get(self.launch_key(candidate))
        if earlier is None:
            return False
        candidate.handshake = copy.deepcopy(earlier)
        candidate.add_note(
            f"{REUSED_VERDICT}, so it was not started again: "
            f"{earlier.credential_test or earlier.error or 'it was not accepted'}"
        )
        log.info(
            "%s: %s -- reusing its verdict instead of launching it again (%s)",
            candidate.label,
            REUSED_VERDICT,
            self.excerpt(earlier.credential_test or earlier.error) or "not accepted",
        )
        return True

    def _remember(self, candidate: Candidate, memo: dict[str, Handshake] | None) -> None:
        """Keep a verdict worth reusing: one the server gave, that was not a pass.

        A pass is not kept: the candidate carrying it goes on to the gates
        after this one, and a second copy of it would be a second entry.
        """
        shake = candidate.handshake
        if memo is None or shake is None or candidate.verified:
            return
        timeout = self.packages.float_("trial_timeout_seconds", 90) if candidate.kind == "stdio" else 0.0
        if self.looks_transient(shake, timeout):
            return
        memo[self.launch_key(candidate)] = copy.deepcopy(shake)

    def _report_trial(self, candidate: Candidate) -> None:
        """One line per verdict, with the server's own words when it failed."""
        shake = candidate.handshake
        if shake is None:
            return
        if candidate.verified:
            log.info("%s: verified -- %s", candidate.label, redact.redact(shake.summary()))
        elif shake.login_refused:
            log.info(
                "%s: started, but %s -- %s",
                candidate.label,
                login_check.in_words(shake.login),
                self.excerpt(shake.login_detail or shake.login_tool),
            )
        elif candidate.needs_credential:
            log.info(
                "%s: waiting for %s -- %s",
                candidate.label,
                ", ".join(shake.credential_pending) or "a credential",
                self.excerpt(shake.credential_test or shake.auth_hint or shake.error)
                or "it listed its tools and asked for nothing else",
            )
        else:
            log.info(
                "%s: not accepted -- %s",
                candidate.label,
                self.excerpt(shake.error) or "no reason given",
            )

    def recheck(self, candidates: Iterable[Candidate]) -> list[Candidate]:
        """Re-prove everything once more at the end of the phase; the ones still working (:meth:`recheck_all`)."""
        return self.recheck_all(candidates).working

    def recheck_all(self, candidates: Iterable[Candidate]) -> "Recheck":
        """Is each server still the one that was proved? ``working``, ``waiting`` for a credential, or ``gone``.

        A server that answered during discovery but is gone by the time the run
        finishes must not be written into settings as if it worked. The
        question is the one its verdict answered, and nothing new: the usage
        tasks that proved it (``mcp.verification.recheck_proved_tasks``). A
        re-check that asked more -- gitlab.example.net's "read the current user",
        added once the address was known to be a GitLab -- removed a server that
        answered every call, and called it one that "no longer answers". A job
        that changed is :meth:`stale_verdicts`' business, before the choice.
        """
        result = Recheck()
        proved_only = self.verification.bool_("recheck_proved_tasks", True)
        for candidate in candidates:
            before = candidate.handshake
            if proved_only:
                candidate.recheck_tasks = self.proved_tasks(candidate)
            try:
                self.verify(candidate)
            finally:
                candidate.recheck_tasks = None
            if candidate.verified:
                result.working.append(candidate)
                continue
            shake = candidate.handshake
            if candidate.needs_credential:
                result.waiting.append(candidate)
                candidate.add_note("final re-check: it answers, and waits for a credential")
                continue
            # A usage task that failed means the server answered the call (a
            # failed task also clears ``ok``, so ``ok`` alone cannot tell).
            failed_task = _first_failure(shake) if shake is not None else ""
            if failed_task:
                why = f"it answers, but no longer does what proved it: {failed_task}"
            else:
                why = f"it no longer answers: {(shake.error if shake is not None else '') or 'no handshake'}"
            candidate.add_note(f"final re-check: {why}")
            candidate.handshake = shake or before
            result.gone.append((candidate, why))
        return result

    @staticmethod
    def proved_tasks(candidate: Candidate) -> list[str] | None:
        """The usage tasks that passed for ``candidate``; ``None`` when none ran (ask whatever applies)."""
        shake = candidate.handshake
        rows = list(shake.contract) if shake is not None else []
        passed = [str(r.get("task") or "") for r in rows if r.get("passed") is True and r.get("task")]
        return passed or None

    # -- the report ------------------------------------------------------------

    @staticmethod
    def audit(candidate: Candidate) -> list[Check]:
        """The checks behind "verified", plus whether its tool definitions are ones the client takes."""
        checks = CandidateVerifier._audit(candidate)
        shake = candidate.handshake
        login = CandidateVerifier.login_row(shake)
        if login is not None:
            checks.append(login)
        configured = CandidateVerifier.configured_row(shake)
        if configured is not None:
            checks.append(configured)
        if shake is not None and shake.tools:
            from .probes import describe_schema_problems

            checks.append(
                Check(
                    "schema",
                    not shake.schema_problems,
                    describe_schema_problems(shake.schema_problems)
                    if shake.schema_problems
                    else f"every tool name and input property key of {len(shake.tools)} tool(s) is "
                    "one Claude Code accepts",
                )
            )
        return checks

    @staticmethod
    def configured_row(shake: Handshake | None) -> Check | None:
        """The ``configured`` row: did any call answer that a setting is in the wrong form?"""
        if shake is None or not (
            shake.functional or shake.misconfigured or shake.address_forms or shake.shape_problems
        ):
            return None
        if shake.shape_problems:
            return Check("configured", False, "; ".join(p["remedy"] for p in shake.shape_problems))
        if shake.misconfigured:
            return Check(
                "configured", False, f"{login_check.in_words(login_check.MISCONFIGURED)} ({shake.misconfigured})"
            )
        called = ", ".join(row.get("tool", "") for row in shake.functional)
        detail = f"{called} answered without a setting error" if called else "no setting error"
        if shake.address_forms:
            detail += "; proved form: " + ", ".join(f"{k}={v}" for k, v in shake.address_forms.items())
        if shake.unexercised:
            detail += f"; not exercised: {', '.join(shake.unexercised[:5])}"
        return Check("configured", True, detail)

    @staticmethod
    def login_row(shake: Handshake | None) -> Check | None:
        """The ``authenticated`` row, from the login check; None when it was not asked."""
        if shake is None or not shake.login:
            return None
        detail = login_check.in_words(shake.login)
        if shake.login_tool:
            detail += f" when {shake.login_tool} was called"
        if shake.login_detail:
            detail += f" ({shake.login_detail})"
        if shake.unexercised:
            # An accepted login is not "every tool works": say which read tools
            # nobody could call.
            detail += f"; not exercised: {', '.join(shake.unexercised[:5])}"
        if shake.login == login_check.ACCEPTED:
            return Check("authenticated", True, detail)
        if shake.login in login_check.REFUSED:
            return Check("authenticated", False, detail)
        return Check("authenticated", None, detail)

    @staticmethod
    def _audit(candidate: Candidate) -> list[Check]:
        """The checks behind "verified", one line each, for a human to read.

        The pass/fail is already decided by the time this runs -- this is the
        working shown. Three of the four checks come straight from the last
        handshake; the fourth (what Claude Code itself says) is added by the
        writer once the server has a name registered under it.

        An endpoint that demanded a credential gets "-" and not "no" for the
        checks it never got to attempt. "no tools" would be a finding about the
        server; the truth is that nobody asked it, because it declined to
        answer an anonymous client -- and that distinction is the whole reason
        such an endpoint is written out rather than discarded.
        """
        shake = candidate.handshake
        if shake is None:
            return [Check("connected", False, "never probed")]

        if shake.ok and shake.credential_pending:
            # It answered everything. Say so -- "-" here would throw away the
            # strongest evidence this program ever collects about a package,
            # and it is what the operator needs to judge whether the token is
            # worth going and getting.
            wanted = ", ".join(shake.credential_pending)
            listed = ", ".join(
                part
                for part in (
                    f"{len(shake.tools)} tool(s)" if shake.tools else "",
                    f"{len(shake.resources)} resource(s)" if shake.resources else "",
                    f"{len(shake.prompts)} prompt(s)" if shake.prompts else "",
                )
                if part
            )
            return [
                Check(
                    "connected",
                    True,
                    f"{shake.server_label} over {shake.transport}, protocol {shake.protocol_version or 'unnegotiated'}",
                ),
                Check(
                    "tools",
                    bool(shake.tools or shake.resources or shake.prompts),
                    f"{listed or 'nothing advertised'} -- listed without a credential",
                ),
                Check(
                    "two-way",
                    bool(shake.round_trip),
                    f"{shake.round_trip_method} answered after the handshake"
                    if shake.round_trip
                    else "nothing came back after the handshake",
                ),
                Check("usable", None, f"not yet: {wanted} must be set before any tool call will work"),
            ]

        if not shake.ok and shake.credential_pending and shake.credential_test:
            # Launched twice: once without the credential (inconclusive) and
            # once with a stand-in for it, when it answered. The listing is
            # real evidence and is shown; "usable" is not claimed.
            wanted = ", ".join(shake.credential_pending)
            listed = _listing(shake)
            return [
                Check(
                    "connected",
                    True,
                    f"{shake.server_label} over {shake.transport}, "
                    f"protocol {shake.protocol_version or 'unnegotiated'} -- only once "
                    f"{wanted} was set (to a stand-in value, never a real one)",
                ),
                Check(
                    "tools",
                    bool(listed),
                    f"{listed or 'nothing advertised'} -- listed once {wanted} was set, and not without it",
                ),
                Check(
                    "two-way",
                    bool(shake.round_trip),
                    f"{shake.round_trip_method} answered after the handshake"
                    if shake.round_trip
                    else "nothing came back after the handshake",
                ),
                Check("usable", None, f"not yet: {wanted} must be set before it will offer anything"),
            ]

        if candidate.needs_credential and not shake.login_refused:
            challenge = shake.auth_hint or f"HTTP {shake.status}"
            return [
                Check(
                    "connected",
                    None,
                    f"reached, and answered {challenge} -- a real endpoint that will not talk to an anonymous client",
                ),
                Check("tools", None, "not asked: the credential slot is still empty"),
                Check("two-way", None, "not attempted without a credential"),
            ]

        connected = Check(
            "connected",
            bool(shake.ok),
            f"{shake.server_label} over {shake.transport or 'unknown transport'}, "
            f"protocol {shake.protocol_version or 'unnegotiated'}"
            if shake.ok
            else (shake.error or "initialize did not complete"),
        )

        counts = [
            f"{len(shake.tools)} tool(s)" if shake.tools else "",
            f"{len(shake.resources)} resource(s)" if shake.resources else "",
            f"{len(shake.prompts)} prompt(s)" if shake.prompts else "",
        ]
        listed = ", ".join(c for c in counts if c)
        tools = Check(
            "tools",
            bool(shake.tools or shake.resources or shake.prompts),
            listed or "the server advertised nothing callable",
        )

        session = f" on session {shake.session_id[:8]}..." if shake.session_id else ""
        two_way = Check(
            "two-way",
            bool(shake.round_trip),
            f"{shake.round_trip_method} was sent after the handshake and answered{session}"
            if shake.round_trip
            else "nothing sent after the handshake came back",
        )
        return [connected, tools, two_way]

    # -- shared bits -----------------------------------------------------------

    def auth_headers(self) -> dict[str, str]:
        """Bearer header for probing an authenticated target, from a reference."""
        auth = self.discovery.section("auth")
        reference = auth.str_("bearer_token_ref")
        if not reference:
            return dict(self.discovery.dict_("headers"))
        secret = self.ctx.secrets.resolve(reference, required=False, label="MCP bearer token")
        headers = dict(self.discovery.dict_("headers"))
        if secret is not None:
            name = auth.str_("header_name", "Authorization")
            template = auth.str_("header_template", "Bearer {token}")
            headers[name] = template.replace("{token}", secret.reveal())
        return {str(k): str(v) for k, v in headers.items()}

    def sandbox_dir(self):
        path = self.packages.path("sandbox_dir") or (self.ctx.work_dir / "mcp-trials")
        return self.ctx.ensure_dir(path)

    def trial_dir(self) -> str:
        """The directory a candidate is trialled from: the sandbox, or ours."""
        return str(self.sandbox_dir()) if self.packages.bool_("sandbox_install", True) else os.getcwd()


def _listing(shake: Handshake) -> str:
    """ "12 tool(s), 3 prompt(s)" -- or "" when it advertised nothing."""
    return ", ".join(
        part
        for part in (
            f"{len(shake.tools)} tool(s)" if shake.tools else "",
            f"{len(shake.resources)} resource(s)" if shake.resources else "",
            f"{len(shake.prompts)} prompt(s)" if shake.prompts else "",
        )
        if part
    )


def _scrub(shake: Handshake, value: str) -> None:
    """Remove ``value`` from everything a handshake carries, in place.

    For the stand-in credential: a server that says "invalid token <value>"
    would otherwise put it into the error, the hints, the self-description --
    and from there into the report.
    """
    if not value:
        return

    def clean(item: Any) -> Any:
        if isinstance(item, str):
            return item.replace(value, STAND_IN_LABEL)
        if isinstance(item, list):
            return [clean(i) for i in item]
        if isinstance(item, set):
            return {clean(i) for i in item}
        if isinstance(item, dict):
            return {clean(k): clean(v) for k, v in item.items()}
        return item

    for spec in fields(shake):
        setattr(shake, spec.name, clean(getattr(shake, spec.name)))


def _why(result: Any) -> str:
    """What a failed command actually said, in one redacted line.

    "prerequisite failed" on its own sends the reader to the debug log to find
    out whether the download 404ed, the archive was corrupt or the command was
    nonsense -- and the answer decides whether the candidate or the agent that
    proposed it was at fault.
    """
    text = (getattr(result, "stderr", "") or getattr(result, "stdout", "") or "").strip()
    first = text.splitlines()[-1].strip() if text else ""
    if not first:
        return f"rc={getattr(result, 'returncode', '?')}"
    return redact.redact(first[:200])


def _api_hosts_for(value: str, api_hosts: Mapping[str, Sequence[str]]) -> list[str]:
    """The API hosts recorded for the host ``value`` names (an address setting); [] when it names none."""
    text = str(value or "").strip()
    host = (urlsplit(text if "://" in text else f"//{text}").hostname or "").lower()
    return [str(h) for h in api_hosts.get(host, [])] if host else []


def _with_endpoint_proof(shake: Handshake) -> Handshake:
    """Say why a URL is known to speak MCP -- a handshake, or a refusal carrying MCP evidence -- if anything shows it."""
    if not shake.endpoint_proof:
        if shake.ok:
            shake.endpoint_proof = "it completed an MCP handshake"
        elif shake.requires_auth and shake.auth_evidence:
            shake.endpoint_proof = "its refusal carried MCP evidence (" + "; ".join(shake.auth_evidence[:2]) + ")"
    return shake


def _on_host(value: str, host: str) -> str:
    """``value`` (an address) with its host replaced by ``host``, everything else as written; "" if no host."""
    text = str(value or "").strip()
    if "://" not in text:
        return ""
    parts = urlsplit(text)
    if not parts.hostname:
        return ""
    netloc = host + (f":{parts.port}" if parts.port else "")
    return parts._replace(netloc=netloc).geturl()


def _address_variants(value: str, forms: Sequence[str], default_scheme: str) -> list[str]:
    """``value`` in each of ``forms`` -- only when it is an address (a URL, or a bare host), else [].

    ``{scheme}`` is the value's own scheme, or ``default_scheme``; ``{host}``
    includes a port; ``{path}`` is the value's path. The value itself is never
    a variant, and each form appears once.
    """
    text = str(value or "").strip()
    if not text or "${" in text or any(c.isspace() for c in text):
        return []
    parts = urlsplit(text if "://" in text else f"//{text}")
    if not parts.hostname or "." not in parts.hostname or not parts.hostname.rsplit(".", 1)[-1].isalpha():
        return []  # an address names a host with a top-level label; "0.1.0" is a version
    if "://" not in text and (parts.path or not re.fullmatch(r"[A-Za-z0-9.-]+(:\d+)?", text)):
        return []
    tokens = {
        "scheme": parts.scheme or default_scheme,
        "host": parts.netloc,
        "path": parts.path.rstrip("/"),
    }
    out: list[str] = []
    for form in forms:
        variant = str(form)
        for key, token in tokens.items():
            variant = variant.replace("{" + key + "}", token)
        if "{" in variant or variant == text or variant in out:
            continue
        out.append(variant)
    return out


def _bare_host(value: str) -> bool:
    """Is ``value`` a bare host name (``api.opensuse.org``, ``host:8080``) -- not a version, path or word?"""
    text = value.strip()
    if not re.fullmatch(r"[A-Za-z0-9.-]+(:\d+)?", text) or "." not in text:
        return False
    return text.split(":", 1)[0].rsplit(".", 1)[-1].isalpha()


def real_credential(values: Mapping[str, Any]) -> bool:
    """Does ``values`` carry a complete, real credential -- every credential one set?

    A credential is a value under a name the secret policy calls one
    (``JIRA_PERSONAL_TOKEN``), or one carrying a credential inline
    (``Authorization: Bearer …``). The login check asks only when there is at
    least one and none is empty or still a ``${NAME}`` reference; the stand-in
    is never passed in here.
    """

    def text(value: Any) -> str:
        return value.reveal() if hasattr(value, "reveal") else str(value if value is not None else "")

    named = {str(k): text(v) for k, v in values.items() if is_secret_name(str(k)) or carries_inline_credential(text(v))}
    return bool(named) and all(v.strip() and "${" not in v for v in named.values())


__all__ = ["CandidateVerifier", "Check", "CHECKS", "REUSED_VERDICT", "STAND_IN_LABEL", "real_credential"]


@dataclass
class Recheck:
    """What the final re-check found, per candidate (:meth:`CandidateVerifier.recheck_all`)."""

    working: list[Candidate] = field(default_factory=list)
    waiting: list[Candidate] = field(default_factory=list)
    gone: list[tuple[Candidate, str]] = field(default_factory=list)


def _first_failure(shake: Handshake) -> str:
    """The first usage task that did not pass, in words; "" without one."""
    for row in shake.contract or []:
        if row.get("passed") is False:
            return f"usage task {row.get('task')!r} failed: {row.get('detail') or 'no detail'}"
    return ""
