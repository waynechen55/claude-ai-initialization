"""claude.ai OAuth for the Claude Code CLI and the web app.

Three ways in, in preference order:

1. **Existing session** -- ``~/.claude/.credentials.json`` (or the OS keychain)
   left behind by a previous ``claude`` login.  Only the file's existence is
   checked; its contents are never read.
2. **Long-lived token** -- ``CLAUDE_CODE_OAUTH_TOKEN``, resolved by reference.
   This is the CI/headless path.
3. **Interactive** -- ``claude auth login`` under ``--headless`` or with no
   display (prints a URL, accepts a pasted code, and *stores* the login where
   every later ``claude`` finds it), otherwise ``/login``, which opens the
   browser itself and takes the code the page shows. ``claude setup-token``
   only prints a long-lived token Claude Code never stores; it runs only when
   ``long_lived_token`` asks for one (CI), and the report says what to export.

Verification is a real one-turn round trip through the CLI, which is the only
way to know the credential is accepted by whichever backend is configured.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from ..core.config import truthy
from ..core.report import Status
from .base import AuthBackend, AuthOutcome


class ClaudeOAuthAuth(AuthBackend):
    key = "claude-oauth"
    title = "claude.ai (Claude Code CLI)"
    config_key = "auth.claude_oauth"

    # -- helpers ---------------------------------------------------------------

    def _claude(self) -> str | None:
        return self.ctx.tool("claude", config_key="paths.claude")

    def command_tokens(self) -> dict[str, str]:
        return {"claude": self._claude() or ""}

    @property
    def command_opens_browser(self) -> bool:
        """Where a browser can be shown and ``--headless`` was not given: ``/login``.

        The CLI's own sign-in opens the browser (the ``BROWSER`` phase 2
        exports) and takes a pasted code when the page shows one, so it already
        ends the way every pasted-code sign-in does. ``setup-token`` stays for
        the operator who carries the URL. ``interactive_command_opens_browser``
        turns this off, for a CLI whose ``/login`` does not take a code.
        """
        return (
            self.paste_code
            and self.config.bool_("interactive_command_opens_browser", True)
            and self.ctx.browser_mode() != "headless"
            and not self.ctx.platform.headless
        )

    def _credential_files(self) -> list[str]:
        found: list[str] = []
        for raw in self.config.str_list("credential_paths"):
            path = Path(os.path.expandvars(os.path.expanduser(raw)))
            if path.exists():
                found.append(str(path))
        return found

    def _note_account_mismatch(self, expected: str, outcome: AuthOutcome) -> None:
        """Say so when the CLI looks signed in as somebody else.

        Deliberately advisory, unlike the equivalent check for gcloud.  There the
        active account comes from ``gcloud auth list --format=json``; here it can
        only be read out of a human-readable status line, so a mismatch is a
        suspicion.  Acting on it would mean throwing away a working session over a
        string comparison -- the operator is told instead.  The check needs the
        status command to have succeeded *and* to name some account, so a CLI that
        reports neither never produces a false alarm.
        """
        if outcome.data.get("cli_auth_rc") != 0:
            return
        reported = str(outcome.data.get("cli_auth_status") or "")
        if "@" not in reported or expected.lower() in reported.lower():
            return
        outcome.data["account_mismatch"] = True
        outcome.detail = f"{outcome.detail}; the CLI does not report {expected}".lstrip("; ")
        outcome.add_remediation(
            "Confirm the signed-in account with: " + " ".join(self.command("status_command", {"claude": "claude"})),
            f"To switch, sign out and back in as {expected} at {self.config.str_('web_login_url')}",
        )

    def _third_party_backend(self) -> str:
        """Claude Code may be pointed at Vertex/Bedrock, where OAuth is not used."""
        switches = self.config.dict_("third_party_switches")
        return next((str(name) for name, var in switches.items() if truthy(os.environ.get(str(var)))), "")

    # -- lifecycle -------------------------------------------------------------

    def inspect(self) -> AuthOutcome:
        outcome = AuthOutcome(backend=self.key)
        claude = self._claude()
        if not claude:
            outcome.status = Status.FAILED
            outcome.detail = "the claude CLI is not installed"
            outcome.add_remediation("Run phase 1 first: initialize-claude-ai run --phases 1 --install-only claude-cli")
            return outcome

        backend = self._third_party_backend()
        outcome.data["third_party_backend"] = backend
        expected = self.config.str_("account")
        if expected:
            outcome.data["expected_account"] = expected

        token = self.ctx.secrets.first_available(self.config.str_list("token_ref"), label="claude oauth token")
        files = self._credential_files()
        outcome.data["credential_files"] = files
        outcome.data["token_reference_resolved"] = bool(token)

        status_argv = self.command("status_command", {"claude": claude})
        if status_argv:
            probe = self.runner.probe(status_argv, timeout=self.timeout("status_command"))
            outcome.data["cli_auth_status"] = probe.first_line()[:200]
            outcome.data["cli_auth_rc"] = probe.returncode

        if token:
            outcome.authenticated = True
            outcome.method = "long-lived token"
            outcome.identity = f"token {token.fingerprint}"
            outcome.detail = f"token from {token.source}"
            # Make it visible to child processes without ever logging the value.
            env_var = self.config.str_("token_env_var", "CLAUDE_CODE_OAUTH_TOKEN")
            os.environ.setdefault(env_var, token.reveal())
        elif files:
            outcome.authenticated = True
            outcome.method = "existing CLI session"
            outcome.detail = f"credential file present ({files[0]})"
        elif backend:
            outcome.authenticated = True
            outcome.method = f"delegated to {backend}"
            outcome.detail = f"Claude Code is configured to use {backend}; claude.ai OAuth is not required"
        else:
            outcome.detail = "no claude.ai credential found"
            if expected:
                outcome.add_remediation(
                    f"Sign in as {expected} with: {' '.join(self.command('headless_command', {'claude': 'claude'}))}"
                )

        if outcome.authenticated and expected:
            # A token is identified by its fingerprint; a session by nothing at
            # all. The configured account is the only name either of them has.
            outcome.identity = outcome.identity or expected
            self._note_account_mismatch(expected, outcome)
        return outcome

    def acquire(self, current: AuthOutcome) -> AuthOutcome:
        claude = self._claude()
        if not claude:
            return current
        if current.data.get("third_party_backend") and not self.force:
            # Nothing to acquire: the CLI authenticates through the cloud backend.
            return current

        timeout = self.settings.float_("auth.interactive_timeout_seconds", 1800)
        # The same run-wide answer every other backend reads, so --headless and
        # --browser-loopback-redirect are not two different questions here.
        paste = self.paste_code and not self.command_opens_browser
        # A stored login by default: `setup-token` only prints a token, and the
        # next `claude` in a new terminal asked to log in again.
        token = self.config.bool_("long_lived_token", False)
        name = "token_command" if token else "headless_command" if paste else "interactive_command"
        argv = self.command(name, {"claude": claude})
        if not argv:
            current.status = Status.FAILED
            current.detail = f"no {name} configured"
            return current

        # Naming the account matters most here: the browser may already be signed
        # in as somebody else, and this is the moment to notice.
        expected = self.config.str_("account")
        whom = f" as {expected}" if expected else ""
        self.console.write("")
        if paste:
            # The command, not its name: which subcommand mints the token is the
            # template's business, and this sentence has to stay true if it changes.
            self.console.status(
                "info",
                "sign-in",
                f"`{' '.join(argv)}` prints a URL: open it anywhere and sign in{whom}, then paste the code back here.",
            )
            # Not "copy the whole URL, it is one line" -- for this CLI that would
            # be a lie. It lays its output out to the terminal width and the
            # breaks in that URL are real newlines, so no amount of careful
            # selecting gets it back. Say where the unbroken copy is instead.
            self.announce_url_capture()
            self.console.bullet(
                "The CLI offers its own shortcuts too: press `c` to copy, or Ctrl+click "
                "the link if your terminal supports it.",
                indent=6,
            )
        elif self.command_opens_browser:
            self.console.status(
                "info",
                "browser sign-in",
                f"`{' '.join(argv)}` opens a browser: sign in{whom} at "
                f"{self.config.str_('web_login_url')}. If the page then shows a code, paste it "
                "back here.",
            )
            self.announce_reset_scope()
        else:
            self.console.status(
                "info",
                "browser sign-in",
                f"sign in{whom} at {self.config.str_('web_login_url')}; the browser hands the result back on its own.",
            )
            self.console.bullet(
                "If it cannot reach this machine, stop and re-run with --headless to paste a code instead.",
                indent=6,
            )
            self.announce_reset_scope()

        if not self.ctx.confirm(f"Run `{' '.join(argv)}` now?", default=True):
            current.status = Status.SKIPPED
            current.detail = "interactive sign-in declined"
            current.add_remediation(f"Authenticate later with: {' '.join(argv)}")
            return current

        # The operator drives this one: they read its prompts and paste a code
        # back. What the base class adds is that we get to read it too, and can
        # recover the URL it wrapped.
        result = self.run_sign_in(argv, timeout=timeout, label="claude sign-in")
        refreshed = self.inspect()
        current.authenticated = refreshed.authenticated
        current.identity = refreshed.identity or current.identity
        current.data.update(refreshed.data)
        if result.ok and refreshed.authenticated:
            current.status = Status.CHANGED
            current.method = "interactive sign-in"
            current.detail = refreshed.detail
        elif result.ok and token:
            # The token was printed and nothing stored it: it works where it is
            # exported, and only there.
            current.status = Status.PARTIAL
            current.detail = "a long-lived token was printed; Claude Code does not store it"
            current.add_remediation(
                f"Export it wherever claude runs: export {self.config.str_('token_env_var')}=<the token printed above> "
                "(your shell profile, or the CI secret), then re-run phase 2 to verify it.",
                f"For a login every later claude finds on its own, run without long_lived_token: "
                f"{' '.join(self.command('headless_command', {'claude': claude}))}",
            )
        else:
            current.status = Status.FAILED
            current.detail = "sign-in did not complete"
            current.add_remediation(
                f"Run it manually: {' '.join(argv)}",
                f"Or set a long-lived token: export {self.config.str_('token_env_var')}=<token>",
            )
        return current

    def verify(self, current: AuthOutcome) -> AuthOutcome:
        claude = self._claude()
        if not claude:
            current.verified = False
            return current

        prompt = self.config.str_("probe_prompt", "Reply with the single word: ok")
        argv = self.command("probe_command", {"claude": claude, "probe_prompt": prompt})
        if not argv:
            current.verified = current.authenticated
            return current

        timeout = self.config.float_("probe_timeout_seconds", 120)
        probe = self.runner.probe(argv, timeout=timeout)
        current.data["probe_rc"] = probe.returncode

        if probe.ok:
            payload = _parse_cli_json(probe.stdout)
            if payload is not None:
                current.data["probe_result"] = {
                    k: payload.get(k)
                    for k in ("is_error", "num_turns", "total_cost_usd", "session_id", "subtype")
                    if k in payload
                }
                current.verified = not payload.get("is_error", False)
            else:
                current.verified = True
            backend = current.data.get("third_party_backend") or "claude.ai"
            current.detail = f"live round trip succeeded via {backend}"
            return current

        current.verified = False
        stderr = (probe.stderr or probe.stdout or "").strip()
        current.detail = f"live round trip failed (rc={probe.returncode})"
        current.data["probe_error"] = stderr[-400:]
        if "429" in stderr or "quota" in stderr.lower() or "rate" in stderr.lower():
            # The credential is fine; the backend is throttling.
            current.verified = current.authenticated
            current.detail = "credential accepted but the backend is rate limited or out of quota"
            current.add_remediation("Check your quota for the configured model/region, or retry later.")
        else:
            current.add_remediation(
                "Inspect the failure with: claude doctor",
                f"Re-authenticate with: {' '.join(self.command('headless_command', {'claude': claude}))}",
            )
        return current


def _parse_cli_json(text: str) -> dict | None:
    """Parse ``claude -p --output-format json`` output, tolerating extra lines."""
    text = (text or "").strip()
    if not text:
        return None
    try:
        payload = json.loads(text)
        return payload if isinstance(payload, dict) else None
    except ValueError:
        pass
    for line in reversed(text.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                payload = json.loads(line)
            except ValueError:
                continue
            if isinstance(payload, dict):
                return payload
    return None
