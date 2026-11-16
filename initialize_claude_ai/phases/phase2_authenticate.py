"""Phase 2 -- authentication.

Runs every selected backend independently so that one failure does not hide the
others, then publishes the results for phase 3 to choose a provider from.

Two passes, and the order matters.  *Detection* asks every backend what
credential it already holds and changes nothing; only then does *acquisition*
sign in, and only for the backends that need it.  Interleaving the two would
mean the first backend's browser prompt blocks before the second has even been
looked at, so the operator answers questions without knowing what is actually
missing.  A backend that is already authenticated is left alone -- re-running
phase 2 is a no-op -- unless ``--force-auth`` says otherwise.

``--force-auth-with-reset`` adds a third pass between the two: the credentials
detection just found are cleared, so the sign-in that follows starts from a
machine with nothing on it.  It is a separate flag because it is the only
destructive thing this phase can do, and what it destroys belongs to every tool
on the host, not only to this program.

Which backends run is controlled by ``--auth-backend`` (or ``auth.backends``);
``all`` means every backend in ``auth.available``.  Every sign-in ends with a
code pasted back (``--no-launch-browser`` and its kind), display or not; where a
browser can be shown the URL is opened for the operator, and ``--headless``
leaves opening it to them. ``--browser-loopback-redirect`` is the opt-in
redirect flow. See :meth:`RunContext.uses_pasted_code`.

Before either pass, :mod:`initialize_claude_ai.authenticate.browser` settles
what a sign-in will open and what that browser needs in its environment in
order to be usable -- once for the whole phase, because every backend that
opens a browser opens the same one.
"""

from __future__ import annotations

import time
from typing import Iterable

from .. import authenticate as auth_registry
from ..authenticate.base import AuthBackend, AuthOutcome
from ..authenticate.browser import prepare_browser
from ..core.report import BAD, PhaseResult, Safety, Status
from ..provision import AUTH_FOR_BACKEND
from .base import Phase, register


@register
class AuthPhase(Phase):
    """Phase 2 -- make sure each selected backend has a working credential.

    Two passes, deliberately: *detect* what is already signed in, then
    *acquire* only what is missing, so a re-run on an authenticated host is
    read-only. No credential value is ever read into a report -- what is
    recorded is whether one exists, where it came from, and that a live round
    trip proved it.
    """

    slug = "auth"
    number = 2
    title = "Authenticate"
    description = (
        "Sign in to each selected backend -- Google Cloud, claude.ai and the Anthropic "
        "API -- in two passes: detect what credentials already exist, then acquire only "
        "the missing ones. Every credential is proved with a live round trip."
    )
    enable_key = "auth"

    def _sign_in_fact(self) -> str:
        """How the sign-in will go, said before any backend runs.

        Whether the URL is opened is settled per backend once the browser is
        found, so this says what the settings ask for, not what was found.
        """
        mode = self.ctx.browser_mode()
        if mode == "browser":
            return "browser hands the result back over the loopback redirect (--browser-loopback-redirect)"
        if mode == "headless":
            return "prints a URL for you to open; paste the code back here (--headless)"
        opens = self.settings.str_("auth.signin.open_url", "auto").strip().lower() in ("auto", "always")
        if opens and not self.ctx.platform.headless:
            return "opens the URL in a browser for you; paste the code the page shows back here"
        return "prints a URL for you to open; paste the code back here"

    def banner_facts(self) -> list[tuple[str, str]]:
        requested = self.ctx.state.get("auth_backends") or self.settings.str_list("auth.backends", ["all"])
        available = self.settings.str_list("auth.available", list(auth_registry.BACKENDS))
        keys = auth_registry.resolve_keys(requested, available)
        return [
            ("Backends", ", ".join(keys) or "none selected"),
            (
                "Sign-in",
                self._sign_in_fact(),
            ),
            ("Live probe", "on" if self.settings.bool_("auth.live_probe", True) else "off"),
            (
                "Existing",
                "cleared first, then signed in again (--force-auth-with-reset)"
                if self.settings.bool_("auth.reset", False)
                else "re-authenticate anyway (--force-auth)"
                if self.settings.bool_("auth.force", False)
                else "kept; no second sign-in",
            ),
            *super().banner_facts(),
        ]

    def safety(self, result: PhaseResult) -> tuple[str, str]:
        """Phase 3 needs exactly one verified credential; the rest are optional.

        So the question is not "did every backend sign in" but "can the backend
        phase 3 is going to reach for actually be reached". A run that verified
        Google Cloud and failed the Anthropic API is perfectly safe if Vertex is
        what will be provisioned -- and is not if it is not.
        """
        if not result.steps or result.fatal:
            # The phase never executed, or stopped for want of a required
            # setting -- neither is a question about credentials, and the base
            # class has the right words for both.
            return super().safety(result)
        verified = list(result.data.get("verified_backends") or [])
        if not verified:
            return Safety.BLOCKED, (
                "No backend produced a credential that survived a live round trip. Phase 3 "
                "has nothing to provision against and will stop at backend selection, and "
                "Claude Code would not be able to reach a model afterwards either."
            )

        # Name the provisioning backends these credentials unlock, because that
        # is the sentence the operator is actually trying to compose.
        priority = self.settings.str_list("provision.backend_priority", ["vertex", "anthropic-api", "bedrock"])
        unlocked = [b for b in priority if AUTH_FOR_BACKEND.get(b, "") in ("", *verified)]
        reachable = f"verified: {', '.join(verified)}" + (
            f"; that is enough for {', '.join(unlocked)}" if unlocked else ""
        )
        if result.status in BAD:
            return Safety.CAUTION, (
                f"At least one credential works ({reachable}), so phase 3 can provision. The "
                "backends listed above did not sign in and will be unavailable -- harmless "
                "unless one of them is the backend you meant to use."
            )
        return Safety.SAFE, (f"Every selected backend signed in and was proved with a live call ({reachable}).")

    def execute(self, result: PhaseResult) -> None:
        """Resolve which backends were asked for, then inspect and acquire each.

        The backend that ends up usable is published into the run's state bag
        for phase 3, which configures Claude Code to talk to it.
        """
        requested = self.ctx.state.get("auth_backends") or self.settings.str_list("auth.backends", ["all"])
        available = self.settings.str_list("auth.available", list(auth_registry.BACKENDS))
        keys = auth_registry.resolve_keys(requested, available)

        if not keys:
            result.add("selection", Status.SKIPPED, "no authentication backends selected")
            return

        self._prepare_browser(result)

        # Selection, browser mode and force are already stated in the banner.
        force = self.settings.bool_("auth.force", False)
        required = set(self.settings.str_list("auth.required"))
        outcomes: dict[str, AuthOutcome] = {}
        succeeded: list[str] = []

        backends, detected = self._detect(result, auth_registry.build(self.ctx, keys), force=force)
        if self.settings.bool_("auth.reset", False):
            self._reset(result, backends, detected)

        for backend in backends:
            started = time.monotonic()
            outcome = backend.run(detected[backend.key])
            outcomes[backend.key] = outcome
            status = outcome.status

            # A backend the operator did not mark required must not fail the run;
            # it is reported as skipped with its remediation attached.
            if status == Status.FAILED and backend.key not in required and not required:
                status = Status.SKIPPED if not outcome.authenticated else status
            elif status == Status.FAILED and required and backend.key not in required:
                status = Status.SKIPPED

            detail = outcome.detail
            if outcome.identity and outcome.identity not in detail:
                detail = f"{outcome.identity} - {detail}" if detail else outcome.identity

            result.add(
                backend.title,
                status,
                detail,
                duration=time.monotonic() - started,
                remediation=outcome.remediation,
                data={
                    "backend": backend.key,
                    "method": outcome.method,
                    "authenticated": outcome.authenticated,
                    "verified": outcome.verified,
                    **outcome.data,
                },
            )
            self.console.status(status, f"{backend.title:<28}", detail)
            for line in outcome.remediation:
                self.console.bullet(line, indent=6)

            if outcome.authenticated and outcome.verified:
                succeeded.append(backend.key)

        self._publish(result, outcomes, succeeded, required)

    def _prepare_browser(self, result: PhaseResult) -> None:
        """Settle what a sign-in will open, once, before any backend runs.

        Every backend that opens a browser opens the same one, so deciding this
        per backend would mean three chances to decide it differently. It runs
        even when the flow is going to print a URL instead: the operator very
        often pastes that URL into a browser on this same machine, and the
        input-method fix is exactly as necessary there.

        Never fatal, and never worse than SKIPPED. A machine with no browser
        can still authenticate by carrying the URL elsewhere, so refusing to
        continue here would block a run that was going to work.
        """
        plan = prepare_browser(self.ctx)
        status = Status.OK if plan.changes_anything else Status.SKIPPED
        result.add(
            "browser",
            status,
            plan.detail(),
            remediation=plan.remediation,
            data={
                "command": plan.command,
                "profile_dir": plan.profile_dir,
                "environment": sorted(plan.env),
            },
        )
        self.console.status(status, f"{'Browser':<28}", plan.detail())
        for line in plan.remediation:
            self.console.bullet(line, indent=6)

    def _detect(
        self,
        result: PhaseResult,
        candidates: Iterable[AuthBackend],
        *,
        force: bool,
    ) -> tuple[list[AuthBackend], dict[str, AuthOutcome]]:
        """Ask every backend what it already holds, before anything is changed.

        ``inspect`` is read-only by contract, so this pass is safe to run
        unconditionally -- and it is run unconditionally, ``--force-auth`` or
        not, because knowing what was already there is what makes the phase's
        report worth reading.  The outcomes are handed to ``run`` afterwards, so
        no backend is inspected twice.
        """
        started = time.monotonic()
        backends: list[AuthBackend] = []
        detected: dict[str, AuthOutcome] = {}
        for backend in candidates:
            if not backend.is_enabled():
                self.report_step(
                    result,
                    backend.title,
                    Status.SKIPPED,
                    f"disabled ({backend.config_key}.enabled = false)",
                )
                continue
            backends.append(backend)
            detected[backend.key] = backend.inspect()

        have = [b.key for b in backends if detected[b.key].authenticated]
        need = [b.key for b in backends if not detected[b.key].authenticated]
        parts = []
        if have:
            parts.append(f"already authenticated: {', '.join(have)}")
        if need:
            parts.append(f"not yet: {', '.join(need)}")
        detail = "; ".join(parts) or "no backend to detect"
        if have and force:
            detail += " -- signing in again anyway (--force-auth)"

        self.report_step(
            result,
            "detect",
            Status.OK,
            detail,
            duration=time.monotonic() - started,
            data={
                "already_authenticated": have,
                "needs_authentication": need,
                "force": force,
                "identities": {key: outcome.identity for key, outcome in detected.items() if outcome.identity},
            },
        )
        return backends, detected

    def _reset(
        self,
        result: PhaseResult,
        backends: list[AuthBackend],
        detected: dict[str, AuthOutcome],
    ) -> None:
        """Throw away what detection just found, before signing in again.

        Between the two passes on purpose.  Detection has to run first because
        it is the only record of what was there; acquisition has to run after,
        against a machine that genuinely no longer holds a credential -- which
        is the whole point of ``--force-auth-with-reset``.  Without this pass
        ``--force-auth`` re-runs a sign-in that the provider answers from its
        own still-valid session, and nobody is ever asked for an account.

        Asked once, for every backend together: the operator typed one flag and
        should answer one question.  Nothing here is fatal -- a credential that
        could not be cleared still gets its sign-in attempt, and the worst case
        is the old session being resumed, which is what would have happened
        without the flag anyway.

        The re-``inspect`` afterwards is not bookkeeping.  It is what tells the
        operator that a backend they asked to be signed out of is *still*
        authenticated, which is the honest answer for a credential this program
        does not own: an ``ANTHROPIC_API_KEY`` exported in their shell outlives
        anything done here, and they have to be told so rather than left to
        discover it when the next sign-in is skipped.
        """
        started = time.monotonic()
        # Not "authenticated": a host signed in as the wrong person reports
        # itself unauthenticated and is holding a credential all the same, and
        # it is the machine this flag was invented for.
        holders = [b for b in backends if b.holds_credential(detected[b.key])]
        if not holders:
            self.report_step(result, "reset", Status.SKIPPED, "no stored credential was found to clear")
            return

        names = ", ".join(b.title for b in holders)
        # Said here, where the operator decides: the URL they will be handed
        # also ends their browser's session with the provider, not only this
        # machine's credential.
        browsers = [b.title for b in holders if b.url_relay().signs_out_first()]
        also = (
            f" The sign-in URL you are then given signs the browser you open it in out of "
            f"{', '.join(browsers)} first, so you are asked who you are."
            if browsers
            else ""
        )
        if not self.ctx.confirm(
            f"Clear the stored credentials for {names}? This signs this machine out, "
            f"for every tool that uses them.{also}",
            default=False,
        ):
            self.report_step(
                result,
                "reset",
                Status.SKIPPED,
                "declined; existing credentials kept",
                duration=time.monotonic() - started,
                data={"declined": True, "candidates": [b.key for b in holders]},
            )
            return

        cleared: list[str] = []
        failed: list[str] = []
        survivors: list[str] = []
        remediation: list[str] = []
        per_backend: dict[str, dict[str, list[str]]] = {}

        for backend in holders:
            report = backend.clear_credentials()
            per_backend[backend.key] = {
                "cleared": report.cleared,
                "failed": report.failed,
                "notes": report.notes,
            }
            if report.changed:
                cleared.append(f"{backend.key}: {report.detail()}")
            failed.extend(f"{backend.key}: {item}" for item in report.failed)

            # Ask again, against the machine as it now is.
            refreshed = backend.inspect()
            detected[backend.key] = refreshed
            if refreshed.authenticated and not self.ctx.dry_run:
                survivors.append(f"{backend.key} ({refreshed.detail})")
                remediation.extend(refreshed.remediation)

        if survivors:
            remediation.insert(
                0,
                "Still authenticated after the reset -- the credential is not one this "
                f"program stored: {', '.join(survivors)}",
            )

        if failed:
            status = Status.PARTIAL
        elif cleared:
            status = Status.CHANGED
        else:
            status = Status.SKIPPED

        detail = "; ".join(cleared) or "nothing was stored here to clear"
        if failed:
            detail = f"{detail}; failed: {', '.join(failed)}"
        self.report_step(
            result,
            "reset",
            status,
            detail,
            duration=time.monotonic() - started,
            remediation=remediation,
            data={"reset": per_backend, "still_authenticated": survivors},
        )
        for line in remediation:
            self.console.bullet(line, indent=6)

    def _publish(
        self,
        result: PhaseResult,
        outcomes: dict[str, AuthOutcome],
        succeeded: list[str],
        required: set[str],
    ) -> None:
        # Phase 3 reads these to decide which provider to provision.
        self.ctx.state["auth_outcomes"] = outcomes
        self.ctx.state["auth_verified"] = succeeded
        result.data["verified_backends"] = succeeded

        missing_required = sorted(required - set(succeeded))
        if missing_required:
            result.add(
                "required backends",
                Status.FAILED,
                f"not authenticated: {', '.join(missing_required)}",
                remediation=[
                    "Re-run just this phase after fixing the above: initialize-claude-ai run --phases 2",
                ],
            )
            return

        if not succeeded:
            result.add(
                "verified credentials",
                Status.FAILED,
                "no backend produced a verified credential",
                remediation=[
                    "Authenticate at least one backend; phase 3 needs one to provision against.",
                    "Headless host? Use: gcloud auth application-default login --no-launch-browser",
                ],
            )
            return

        self.console.write("")
        self.console.status(Status.OK, "verified credentials", ", ".join(succeeded))
        result.add("verified credentials", Status.OK, f"verified: {', '.join(succeeded)}")
