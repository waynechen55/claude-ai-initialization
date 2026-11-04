"""Authentication backend contract.

Each backend follows the same three-beat rhythm:

``inspect``   what credential already exists?  (never mutates anything)
``acquire``   obtain one if needed, choosing a headless flow when there is no
              browser
``verify``    prove the credential is accepted by the upstream service

``run`` sequences them and returns a single :class:`AuthOutcome`.  ``acquire`` is
skipped when ``inspect`` already found a credential, so a second run of phase 2
is a no-op rather than a fresh sign-in; ``auth.force`` (``--force-auth``)
overrides that.  Identities are reported by account name or by a non-reversible
fingerprint -- never by value.

There is a fourth beat, and it only ever runs when it is asked for by name:

``clear``     throw away the credential that is already there
              (``auth.reset`` / ``--force-auth-with-reset``)

It is kept apart from ``--force-auth`` deliberately.  Forcing a sign-in mints a
*new* credential and is safe to repeat; clearing one destroys what is on the
machine, and what it destroys is not this program's alone -- ``gcloud auth
revoke`` takes the credential away from every tool on the host.  So one flag
does not imply the other by accident: ``--force-auth-with-reset`` is a separate
thing to type.
"""

from __future__ import annotations

import os
import shutil
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Sequence

from ..core.config import fill
from ..core.context import RunContext
from ..core.logging_setup import get_logger
from ..core.proc import CommandResult
from ..core.report import Status

if TYPE_CHECKING:  # pragma: no cover - import cycle only matters to type checkers
    from .signin_url import SignInUrlRelay


@dataclass
class AuthOutcome:
    backend: str
    status: str = Status.SKIPPED
    authenticated: bool = False
    #: Human-readable identity: an email, a service-account, or a key fingerprint.
    identity: str = ""
    method: str = ""
    detail: str = ""
    verified: bool = False
    data: dict[str, Any] = field(default_factory=dict)
    remediation: list[str] = field(default_factory=list)

    def add_remediation(self, *lines: str) -> None:
        for line in lines:
            if line and line not in self.remediation:
                self.remediation.append(line)


@dataclass
class CredentialReset:
    """What clearing one backend's stored credential actually did.

    Separate from :class:`AuthOutcome` because it answers a different question:
    not "are we authenticated" but "what was taken away, and what could not be".
    """

    backend: str = ""
    #: Things that are gone: a revoke that ran, a file that was removed.
    cleared: list[str] = field(default_factory=list)
    #: Things that were meant to go and did not, with the reason.
    failed: list[str] = field(default_factory=list)
    #: Things worth saying that are neither -- nothing was there to clear.
    notes: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.cleared)

    def detail(self) -> str:
        parts = []
        if self.cleared:
            parts.append(", ".join(self.cleared))
        if self.failed:
            parts.append("could not clear: " + "; ".join(self.failed))
        if not parts and self.notes:
            parts.append("; ".join(self.notes))
        return "; ".join(parts) or "nothing to clear"


class AuthBackend(ABC):
    """One way of signing in, behind one interface: inspect, acquire, verify.

    Phase 2 never asks a backend to "log in". It asks what credential already
    exists (:meth:`inspect`), obtains one only if that came back empty
    (:meth:`acquire`), and then proves it with a live round trip
    (:meth:`verify`) -- so a re-run against an authenticated host changes
    nothing, and a credential is never reported as working on the strength of
    a file existing.

    Everything backend-specific -- the commands, the paths that hold the
    credential, what clearing it means -- is configuration under
    :attr:`config_key`, not code, which is why adding a backend is a subclass
    and a TOML section rather than a new branch in the phase.
    """

    key: str = ""
    title: str = ""
    #: Config section holding this backend's settings.
    config_key: str = ""

    def __init__(self, ctx: RunContext) -> None:
        self.ctx = ctx
        self.settings = ctx.settings
        self.runner = ctx.runner
        self.console = ctx.console
        self.log = get_logger(f"auth.{self.key}")

    # -- configuration helpers -------------------------------------------------

    @property
    def config(self):
        return self.settings.section(self.config_key)

    def is_enabled(self) -> bool:
        return self.config.bool_("enabled", True)

    @property
    def force(self) -> bool:
        return self.settings.bool_("auth.force", False)

    @property
    def reset(self) -> bool:
        """Whether to throw the existing credential away before signing in.

        Implies :attr:`force` in practice -- ``--force-auth-with-reset`` sets
        both -- but they are read separately, because one of them is reversible
        and the other is not.
        """
        return self.settings.bool_("auth.reset", False)

    @property
    def live_probe(self) -> bool:
        return self.settings.bool_("auth.live_probe", True)

    @property
    def paste_code(self) -> bool:
        """Whether to ask for a pasted verification code rather than a redirect.

        Every backend reads this one property, so ``--headless`` and
        ``--browser-loopback-redirect`` mean the same thing everywhere: which
        flow they select is a property of the machine, not of the credential
        being obtained. What each backend *does* with the answer still differs,
        because the providers differ -- gcloud swaps a flag, the Claude CLI runs
        a different subcommand -- but they are never in two different modes at
        once.

        The rule itself lives on :meth:`RunContext.uses_pasted_code`, so the
        phase banner can state the flow before any backend is built.
        """
        return self.ctx.uses_pasted_code()

    @property
    def command_opens_browser(self) -> bool:
        """Does this backend's sign-in command open the browser itself?

        False by default: in a pasted-code flow the command prints a URL, and
        :class:`.signin_url.SignInUrlRelay` opens it (unless ``--headless``).
        A backend whose own sign-in both opens the browser and takes a pasted
        code overrides this; it then keeps ``BROWSER``, and nothing opens a
        second window.
        """
        return False

    def command(self, name: str, tokens: dict[str, str] | None = None) -> list[str]:
        """Render a configured command template with ``{token}`` substitution."""
        raw = self.config.list_(f"commands.{name}")
        return [fill(item, tokens or {}) for item in raw]

    def timeout(self, name: str) -> float:
        """Seconds command ``name`` may run, from ``command_timeout_seconds``.

        Keyed by the same name as the template, beside it, so that every
        command this backend runs has a configurable bound that
        ``--timeout-scale`` stretches like any other timeout. A command with no
        entry gets the run's per-command default.
        """
        return self.config.float_(f"command_timeout_seconds.{name}", self.runner.default_timeout)

    def command_tokens(self) -> dict[str, str]:
        """Substitutions every template of this backend needs -- its binary.

        Declared here so that machinery which renders a template without
        knowing the backend (``clear_credentials``) does not have to.  A
        backend whose binary is missing returns an empty value, and the
        template renders to an argv nobody can run, which is caught there.
        """
        return {}

    # -- interactive sign-in ---------------------------------------------------

    def run_sign_in(self, argv: Sequence[str], *, timeout: float, label: str) -> CommandResult:
        """Run an interactive sign-in, keeping hold of the URL it prints.

        The operator drives this command directly -- they read its prompts and
        paste a code back -- so it gets the terminal either way. The difference
        is whether this process gets to watch: on a pseudo-terminal it can
        recover the sign-in URL whole and put it somewhere reachable, which is
        the only way to get a wrapped several-hundred-character URL back out of
        a screen. Every backend routes through here so that the answer, and the
        clean-up afterwards, are the same for all of them.
        """
        from .signin_url import UrlOnScreen

        relay = self.url_relay()
        env = self.sign_in_env()
        if not relay.enabled():
            return self.runner.run(argv, timeout=timeout, env=env, capture=False, label=label)
        section = self.settings.section("auth.signin")
        try:
            return self.runner.run_tty(
                argv,
                timeout=timeout,
                env=env,
                columns=section.int_("columns", 0),
                rows=section.int_("rows", 0),
                on_output=relay.feed,
                # Every copy of the URL the operator can reach must be the one
                # handed over, the one on screen included.
                screen=UrlOnScreen(relay) if relay.signs_out_first() else None,
                label=label,
            )
        finally:
            relay.cleanup()

    def sign_in_env(self) -> dict[str, str | None]:
        """What the sign-in command's environment differs by, for this flow.

        Nothing in the redirect flow: there the command is meant to open the
        browser :mod:`.browser` exported as ``BROWSER``. A pasted-code flow is
        the operator saying they will open the URL themselves, yet the same
        ``BROWSER`` is still in every child's environment, and a sign-in
        command -- or a library under it -- that opens whatever ``BROWSER``
        names would open it regardless of the flag it was given. So
        ``auth.signin.paste_env`` is overlaid: each name set to its value, or
        removed where the value is empty.
        """
        if not self.paste_code or self.command_opens_browser:
            return {}
        table = self.settings.section("auth.signin").dict_("paste_env")
        env = {str(key): (str(value) if str(value) else None) for key, value in table.items()}
        for key, value in sorted(env.items()):
            self.log.debug("pasted-code sign-in env: %s=%s", key, "(removed)" if value is None else value)
        return env

    def url_relay(self) -> "SignInUrlRelay":
        """The relay for this backend's sign-in, knowing where it signs out.

        Built in one place so the advice given before a sign-in and the URL
        handed over during it cannot disagree about whether that URL goes
        through the provider's sign-out first.
        """
        from .signin_url import SignInUrlRelay

        return SignInUrlRelay(
            self.ctx,
            paste=self.paste_code and not self.command_opens_browser,
            signout_url=self.config.str_("signout_url").strip(),
            signout_continue=self.config.str_("signout_continue_param").strip(),
            reset_prompt=self.config.str_("reset_prompt").strip(),
        )

    def announce_url_capture(self) -> None:
        """Say where the URL will be, before the child takes over the screen.

        It has to be said first: once the sign-in is running it is repainting,
        and anything printed over the top of it is either overwritten or wrecks
        the layout. So the operator is told in advance and the capture itself
        happens in silence.
        """
        for line in self.url_relay().announce():
            self.console.bullet(line, indent=6)
        self.announce_reset_scope()

    def opens_sign_in_browser(self) -> bool:
        """Will a pasted-code sign-in open its URL in this program's own profile?"""
        return self.url_relay().opens_browser()

    def announce_reset_scope(self) -> None:
        """After a reset, say whether the browser about to be used is clean too.

        A reset revokes this machine's credential; by itself it cannot sign a
        browser out of the provider, and a browser that is still signed in lets
        the operator through with no password -- which reads as "the reset did
        nothing". The redirect flow with this program's own profile needs no
        word: gcloud or the CLI opens that emptied profile itself. A pasted-code
        flow whose URL goes through the provider's sign-out first is told so,
        and where to take that URL from. Every other combination gets told what
        the reset did not reach and how to get a sign-in that asks. Silent
        without a reset.
        """
        from .browser import planned_browser

        if not self.paste_code:
            plan = planned_browser(self.ctx)
            if plan is not None and plan.own_profile:
                return
        relay = self.url_relay()
        for line in relay.session_advice(relay.signout_url):
            self.console.bullet(line, indent=6)

    # -- lifecycle -------------------------------------------------------------

    @abstractmethod
    def inspect(self) -> AuthOutcome:
        """Report the credential that already exists, without changing anything."""

    def holds_credential(self, outcome: AuthOutcome) -> bool:
        """Whether there is anything here to clear -- *authenticated or not*.

        Not the same question as ``outcome.authenticated``, and the difference
        is the case the reset matters most in: a host signed in as the wrong
        person reports "not authenticated" while still holding a perfectly good
        credential for somebody else.  Answering with authentication alone would
        skip exactly that machine.

        The default looks for the files a reset would remove; a backend whose
        credential is not a file of ours says so for itself.
        """
        return outcome.authenticated or any(path.exists() for path in self.reset_paths())

    def clear_credentials(self) -> CredentialReset:
        """Throw away the credential ``inspect`` would otherwise find.

        Config-driven and therefore the same for every backend: ``reset.commands``
        names templates under ``commands.*`` to run (a revoke, a logout),
        ``reset.paths`` and ``reset.paths_from`` name the files that *are* the
        credential.  ``paths_from`` names a sibling setting rather than copying
        its list, so the files cleared here cannot drift from the files
        ``inspect`` looks at -- they are the same list.

        A backend with none of those configured clears nothing and says so,
        which is the honest answer for a credential this program never stored:
        an API key reached by ``env:`` reference belongs to whoever exported it.

        Nothing here is fatal. A revoke that fails because there was nothing to
        revoke must not stop a sign-in that was going to work.
        """
        report = CredentialReset(backend=self.key)
        tokens = self.command_tokens()
        timeout = self.settings.float_("auth.reset_timeout_seconds", 120)

        for name in self.config.str_list("reset.commands"):
            argv = self.command(name, tokens)
            if not argv or not all(argv):
                report.notes.append(f"{name}: no command to run")
                continue
            result = self.runner.run(argv, timeout=timeout, label=f"{self.key} {name}")
            shown = " ".join(argv[1:]) or name  # the binary path says nothing useful
            if result.simulated:
                # A dry run must not claim a revoke happened. It did not: the
                # runner declined to execute it and reported success for the
                # step, which is exactly the answer that would be a lie here.
                report.cleared.append(f"would run {shown}")
            elif result.ok:
                report.cleared.append(shown)
            else:
                # Very often "there was nothing signed in", which is a success
                # for our purposes; the return code is recorded, not judged.
                report.notes.append(f"{shown} exited {result.returncode}")

        for path in self.reset_paths():
            self._retire(path, report)
        return report

    def reset_paths(self) -> list[Path]:
        """Files that *are* the credential, from ``reset.paths``/``reset.paths_from``."""
        raw = list(self.config.str_list("reset.paths"))
        for key in self.config.str_list("reset.paths_from"):
            raw.extend(self.config.str_list(key))
        seen: dict[str, Path] = {}
        for item in raw:
            text = os.path.expandvars(os.path.expanduser(str(item).strip()))
            if text:
                seen.setdefault(text, Path(text))
        return list(seen.values())

    def _retire(self, path: Path, report: CredentialReset) -> None:
        """Remove one credential file or directory, or move it aside.

        Deleting is the default rather than backing up, unlike everywhere else
        in this program: a backup of a credential is still a credential, and
        leaving `.credentials.json.20260914-101500.bak` readable beside the
        file somebody just asked to have cleared would defeat the request it
        was made in answer to. ``auth.reset_backup`` restores the usual habit
        for an operator who wants the way back.
        """
        if not path.exists():
            return
        if self.ctx.dry_run:
            report.cleared.append(f"would remove {path}")
            return
        try:
            if self.settings.bool_("auth.reset_backup", False):
                stamp = datetime.now().strftime(self.settings.str_("auth.reset_backup_suffix", "%Y%m%d-%H%M%S"))
                target = path.with_name(f"{path.name}.{stamp}.bak")
                path.replace(target)
                report.cleared.append(f"{path} -> {target.name}")
                return
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            else:
                path.unlink()
        except OSError as exc:
            report.failed.append(f"{path}: {exc}")
            return
        report.cleared.append(f"removed {path}")

    def acquire(self, current: AuthOutcome) -> AuthOutcome:
        """Obtain a credential. Default: nothing to do beyond what exists."""
        return current

    def verify(self, current: AuthOutcome) -> AuthOutcome:
        """Prove the credential works upstream. Default: trust ``inspect``."""
        current.verified = current.authenticated
        return current

    def run(self, detected: AuthOutcome | None = None) -> AuthOutcome:
        """Sequence the three beats, reusing an ``inspect`` already performed.

        Phase 2 detects every backend before acquiring on any of them, so it
        passes the outcome it already holds; nobody inspects twice.
        """
        outcome = self.inspect() if detected is None else detected
        if not outcome.authenticated or self.force:
            outcome = self.acquire(outcome)
        if outcome.authenticated and self.live_probe:
            outcome = self.verify(outcome)
        elif outcome.authenticated:
            outcome.verified = False
            outcome.detail = (outcome.detail + " (live probe disabled)").strip()

        if outcome.authenticated and (outcome.verified or not self.live_probe):
            if outcome.status not in (Status.CHANGED,):
                outcome.status = Status.OK
        elif outcome.authenticated and not outcome.verified:
            outcome.status = Status.PARTIAL
        elif outcome.status == Status.SKIPPED:
            pass
        else:
            outcome.status = Status.FAILED
        return outcome
