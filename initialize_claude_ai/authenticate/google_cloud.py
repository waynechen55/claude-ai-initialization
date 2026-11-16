"""Google Cloud authentication.

Vertex AI -- the backend Claude Code uses when ``CLAUDE_CODE_USE_VERTEX=1`` --
reads Application Default Credentials, so both the user credential and ADC are
handled here.  Five acquisition methods are supported and ``auto`` picks the
right one for the host:

``service-account``    activate a key file named by reference
``impersonate``        mint tokens as another service account
``metadata``           the GCE/GKE metadata server already provides a credential
``workload-identity``  an external account config is already in place
``user``               an interactive login

Which interactive flow the login uses is not decided here: ``auth.browser_mode``
decides it once for the whole run, and every backend reads the same answer. Under
the pasted-code flow -- the default, with the URL opened for the operator
unless ``--headless`` -- this adds ``--no-launch-browser``, so gcloud prints a
URL and waits for the verification code; only under
``--browser-loopback-redirect`` does gcloud open a browser and take the loopback
redirect itself. See :meth:`RunContext.uses_pasted_code`.

Neither is the project decided here.  ``--gcloud-project`` sets
``auth.google_cloud.project`` and ``provision.vertex.project`` together -- it is
one project for the whole run, not one per phase -- and when it is set every
gcloud command below carries ``--project`` and gcloud's own active project is
pointed at it.  See :meth:`GoogleCloudAuth.command`.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from ..core.report import Status
from .base import AuthBackend, AuthOutcome

_METADATA_HOST = "metadata.google.internal"

#: The flag that makes ``gcloud auth login`` write application-default
#: credentials as well. Its presence in the configured login template is what
#: tells :meth:`GoogleCloudAuth._login_steps` that a separate ADC sign-in would
#: be a second trip through the same flow for the same credential.
ADC_FLAG = "--update-adc"


class GoogleCloudAuth(AuthBackend):
    key = "google-cloud"
    title = "Google Cloud (Vertex AI)"
    config_key = "auth.google_cloud"

    # -- helpers ---------------------------------------------------------------

    def _gcloud(self) -> str | None:
        return self.ctx.tool("gcloud", config_key="paths.gcloud")

    def command_tokens(self) -> dict[str, str]:
        return {"gcloud": self._gcloud() or ""}

    def holds_credential(self, outcome: AuthOutcome) -> bool:
        """gcloud's credentials are not files of ours, so detection answers this.

        And it answers more than ``authenticated`` does. A host signed in as
        somebody other than ``account`` is deliberately reported as *not*
        authenticated -- proceeding as the wrong user is the failure that check
        exists to prevent -- but it is still holding a credential, and it is the
        machine most in need of being cleared before signing in again.
        """
        return bool(
            outcome.authenticated
            or outcome.data.get("accounts")
            or outcome.data.get("adc_present")
            or outcome.data.get("user_credential_present")
        )

    def project(self) -> str:
        """The project this run is for, from whichever layer supplied it.

        ``--gcloud-project``, an env file, ``GOOGLE_CLOUD_PROJECT`` and a config
        file all arrive here as one setting, so nothing in this class has to
        care which of them the operator used.
        """
        return self.config.str_("project").strip()

    def command(self, name: str, tokens: dict[str, str] | None = None) -> list[str]:
        """Render a command template and name the project in it.

        Authenticating is not a project-neutral act. Tokens are minted with a
        quota project attached, APIs are enabled on a project, and Vertex serves
        models from one -- so a gcloud left pointing at another project signs in
        perfectly and then does all of that somewhere nobody asked for. When a
        project is known, every command carries it, rather than each call site
        remembering to pass one.
        """
        argv = super().command(name, tokens)
        project = self.project()
        if not argv or not project or name in self.config.str_list("project_args_skip"):
            return argv
        if any(a == "--project" or a.startswith("--project=") for a in argv):
            return argv  # the template names one already; it wins
        return argv + [str(item).replace("{project}", project) for item in self.config.list_("project_args")]

    def set_value(self, value: str) -> str:
        """``value``, or ``""`` when it is how gcloud prints an unset property."""
        return "" if value in self.config.str_list("unset_values") else value

    def _accounts(self, gcloud: str) -> list[dict[str, Any]]:
        probe = self.runner.probe(
            self.command("list_accounts", {"gcloud": gcloud}), timeout=self.timeout("list_accounts")
        )
        if not probe.ok:
            return []
        try:
            payload = json.loads(probe.stdout or "[]")
        except ValueError:
            return []
        return payload if isinstance(payload, list) else []

    def _active_account(self, accounts: list[dict[str, Any]]) -> str:
        for entry in accounts:
            if entry.get("status") == "ACTIVE":
                return str(entry.get("account", ""))
        return ""

    def _active_impersonation(self, gcloud: str) -> str:
        """The service account gcloud is currently impersonating, if any."""
        argv = self.command("active_impersonation", {"gcloud": gcloud})
        if not argv:
            return ""
        value = self.runner.probe(argv, timeout=self.timeout("active_impersonation")).output.strip()
        return self.set_value(value)

    def _on_gce(self) -> bool:
        """Cheap check for a metadata server, without a network round trip."""
        if os.environ.get("GCE_METADATA_HOST") or os.environ.get("GCE_METADATA_IP"):
            return True
        return Path("/sys/class/dmi/id/product_name").exists() and "Google" in _read("/sys/class/dmi/id/product_name")

    def _choose_method(self) -> str:
        configured = self.config.str_("method", "auto").strip().lower()
        if configured and configured != "auto":
            return configured
        if self.config.str_("service_account_key_ref"):
            return "service-account"
        if self.config.str_("impersonate_service_account"):
            return "impersonate"
        if self._on_gce():
            return "metadata"
        return "user"

    # -- lifecycle -------------------------------------------------------------

    def inspect(self) -> AuthOutcome:
        outcome = AuthOutcome(backend=self.key)
        gcloud = self._gcloud()
        if not gcloud:
            outcome.status = Status.FAILED
            outcome.detail = "gcloud is not installed"
            outcome.add_remediation("Run phase 1 first: initialize-claude-ai run --phases 1 --install-only gcloud")
            return outcome

        accounts = self._accounts(gcloud)
        active = self._active_account(accounts)
        wanted = self.config.str_("account")
        impersonate = self.config.str_("impersonate_service_account")
        key_path = _path_from_ref(self.config.str_("service_account_key_ref"))
        outcome.data["accounts"] = [str(a.get("account", "")) for a in accounts]
        outcome.data["active_account"] = active
        if impersonate:
            outcome.data["impersonate_service_account"] = impersonate
        if key_path:
            outcome.data["service_account_key"] = key_path

        # gcloud's *own* active project, which is why get_project is on
        # project_args_skip: handed --project it would simply read it back, and
        # the comparison in _apply_project below could never find a difference.
        project_probe = self.runner.probe(
            self.command("get_project", {"gcloud": gcloud}), timeout=self.timeout("get_project")
        )
        project = self.set_value(project_probe.output.strip())
        outcome.data["project"] = project

        # ADC is what the Vertex client library actually reads.
        adc = self.runner.probe(
            self.command("print_adc_token", {"gcloud": gcloud}), timeout=self.timeout("print_adc_token")
        )
        outcome.data["adc_present"] = adc.ok
        user_token = self.runner.probe(
            self.command("print_token", {"gcloud": gcloud}), timeout=self.timeout("print_token")
        )
        outcome.data["user_credential_present"] = user_token.ok

        needs_adc = self.config.bool_("configure_adc", True)
        # Judge the credential that will actually be presented. Phase 3 and the
        # Vertex client libraries read ADC, so a stale `gcloud auth login`
        # session (org reauth expiry, typically) is worth saying out loud but
        # must not condemn a setup whose ADC token still mints.
        satisfied = adc.ok if needs_adc else user_token.ok
        if wanted and active and wanted != active:
            satisfied = False
            outcome.detail = f"active account is {active}, configuration wants {wanted}"
        elif key_path and os.environ.get("GOOGLE_APPLICATION_CREDENTIALS") != key_path:
            # A named key file is a requirement too. GOOGLE_APPLICATION_CREDENTIALS
            # is how a key becomes the ADC that Vertex reads, so if it does not
            # point here the key is not in effect, however healthy the host's own
            # credential looks. Checked without opening the file: it holds a
            # private key, and its path is all that has to be compared.
            satisfied = False
            outcome.detail = f"credential present but not the key at {key_path}"
        elif impersonate and self._active_impersonation(gcloud) != impersonate:
            # A named service account is a requirement, not a preference: a host
            # whose own credential is perfectly valid still is not what was asked
            # for, so say unauthenticated and let acquire() set the impersonation.
            satisfied = False
            outcome.detail = f"credential present but not impersonating {impersonate}"

        outcome.authenticated = satisfied
        outcome.identity = active
        outcome.method = "existing"
        if satisfied:
            outcome.detail = f"{active or 'credential present'}"
            if project:
                outcome.detail += f" / project {project}"
            if needs_adc and not user_token.ok:
                outcome.detail += " / gcloud CLI credential stale"
                outcome.add_remediation(
                    "Application-default credentials work, but `gcloud auth print-access-token`"
                    " does not; refresh the CLI session with: gcloud auth login"
                )
        elif not outcome.detail:
            missing = []
            if needs_adc and not adc.ok:
                missing.append("application-default credential")
            if not user_token.ok:
                missing.append("user credential")
            outcome.detail = f"missing: {', '.join(missing) or 'credential'}"
        return outcome

    def acquire(self, current: AuthOutcome) -> AuthOutcome:
        gcloud = self._gcloud()
        if not gcloud:
            return current
        method = self._choose_method()
        current.method = method
        self.log.info("acquiring Google Cloud credentials via '%s'", method)

        handler = {
            "service-account": self._acquire_service_account,
            "impersonate": self._acquire_impersonate,
            "metadata": self._acquire_metadata,
            "workload-identity": self._acquire_metadata,
            "adc-only": self._acquire_adc_only,
            "user": self._acquire_user,
        }.get(method, self._acquire_user)
        current = handler(gcloud, current)

        self._apply_config(gcloud, current)
        return current

    # -- acquisition methods ---------------------------------------------------

    def _acquire_service_account(self, gcloud: str, outcome: AuthOutcome) -> AuthOutcome:
        ref = self.config.str_("service_account_key_ref")
        # The key file is referenced by path; gcloud reads the file itself so the
        # contents never pass through this process.
        key_path = _path_from_ref(ref)
        if not key_path or not Path(key_path).is_file():
            outcome.status = Status.FAILED
            outcome.detail = f"service-account key not readable at {ref or '<unset>'}"
            outcome.add_remediation("Set auth.google_cloud.service_account_key_ref to 'file:/path/to/key.json'.")
            return outcome
        # gcloud reads the key itself, so the resolver's usual check is bypassed;
        # ask for it explicitly rather than let a private key sit world readable.
        self.ctx.secrets.check_file_permissions(key_path)
        result = self.runner.run(
            self.command("activate_service_account", {"gcloud": gcloud, "key_file": key_path}),
            timeout=self.timeout("activate_service_account"),
            label="gcloud activate-service-account",
        )
        if not result.ok:
            outcome.status = Status.FAILED
            outcome.detail = f"activate-service-account failed (rc={result.returncode})"
            return outcome
        # ADC for a service account is the key file itself.
        self.ctx.env.set("GOOGLE_APPLICATION_CREDENTIALS", key_path, passthrough=True)
        outcome.authenticated = True
        outcome.status = Status.CHANGED
        outcome.detail = f"service account activated from {key_path}"
        return outcome

    def _acquire_impersonate(self, gcloud: str, outcome: AuthOutcome) -> AuthOutcome:
        target = self.config.str_("impersonate_service_account")
        result = self.runner.run(
            self.command("set_impersonation", {"gcloud": gcloud, "account": target}),
            timeout=self.timeout("set_impersonation"),
            label="gcloud impersonate",
        )
        outcome.authenticated = result.ok
        outcome.status = Status.CHANGED if result.ok else Status.FAILED
        outcome.detail = f"impersonating {target}" if result.ok else "could not set impersonation"
        if result.ok:
            self._impersonate_in_adc(gcloud, target, outcome)
        return outcome

    def _impersonate_in_adc(self, gcloud: str, target: str, outcome: AuthOutcome) -> None:
        """Put the impersonation into the application-default credential Claude Code reads.

        ``gcloud config set auth/impersonate_service_account`` changes what the
        gcloud CLI does, and nothing else: Claude Code reads the
        application-default credential, which went on being the operator's own
        -- a ``claude`` started later was not the service account phase 2
        verified. So the credential is written impersonating the target
        (``adc_impersonate``, an interactive sign-in), unless it already does.
        Only the file's ``type`` and impersonation URL are read, never a token.
        (``auth.google_cloud.impersonate_adc``.)
        """
        import json

        if not self.config.bool_("impersonate_adc", True):
            return
        path = Path(self.config.str_("adc_file") or "").expanduser()
        try:
            adc = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
        except (OSError, ValueError):
            adc = {}
        if adc.get("type") == "impersonated_service_account" and target in str(
            adc.get("service_account_impersonation_url") or ""
        ):
            outcome.detail += "; the application-default credential impersonates it too"
            return
        name = "adc_impersonate_headless" if self.paste_code else "adc_impersonate"
        argv = self.command(name, {"gcloud": gcloud, "account": target})
        if not argv:
            return
        result = self.run_sign_in(
            argv,
            timeout=self.settings.float_("auth.interactive_timeout_seconds", 1800),
            label="gcloud application-default login (impersonating)",
        )
        if result.ok:
            outcome.detail += "; the application-default credential Claude Code reads impersonates it too"
        else:
            outcome.status = Status.PARTIAL
            outcome.add_remediation(
                f"Claude Code reads the application-default credential, which does not impersonate {target} yet: "
                f"run {' '.join(argv)}"
            )

    def _acquire_metadata(self, gcloud: str, outcome: AuthOutcome) -> AuthOutcome:
        probe = self.runner.probe(self.command("print_token", {"gcloud": gcloud}), timeout=self.timeout("print_token"))
        outcome.authenticated = probe.ok
        outcome.status = Status.OK if probe.ok else Status.FAILED
        outcome.detail = (
            "using the instance metadata credential"
            if probe.ok
            else f"no usable metadata credential on this host ({_METADATA_HOST} unreachable?)"
        )
        return outcome

    def _acquire_adc_only(self, gcloud: str, outcome: AuthOutcome) -> AuthOutcome:
        return self._run_login(gcloud, outcome, user_login=False)

    def _acquire_user(self, gcloud: str, outcome: AuthOutcome) -> AuthOutcome:
        return self._run_login(gcloud, outcome, user_login=True)

    def _sign_in_hint(self, paste: bool, steps: int, outcome: AuthOutcome) -> None:
        """Say what is about to happen, before the terminal goes quiet.

        gcloud prints its URL and then simply waits. Without a word of warning
        that is indistinguishable from a hang, and the one thing the person at
        the keyboard has to do -- copy a code back -- is the thing they are least
        likely to guess.
        """
        self.console.write("")
        if not paste:
            self.console.status(
                "info",
                "browser sign-in",
                "gcloud will open a browser and wait for it to redirect back to this machine.",
            )
            self.console.bullet(
                "Nothing to copy: the redirect carries the result back on its own.",
                indent=6,
            )
            self.console.bullet(
                "If the browser does not come back to this machine by itself, stop and re-run "
                "without --browser-loopback-redirect: the page then ends with a code to paste here.",
                indent=6,
            )
            self.announce_reset_scope()
            return

        opens = self.opens_sign_in_browser()
        self.console.status(
            "info",
            "sign-in",
            "gcloud prints a URL and this program opens it in a browser for you; the page ends "
            "with a code to paste back here."
            if opens
            else "gcloud will print a URL for you to open; the page ends with a code to paste back here.",
        )
        self.announce_url_capture()
        for line in (
            "Sign in in that window -- or in any other browser that holds no earlier sign-in."
            if opens
            else "Nothing opens it for you: open it yourself in any browser, on any machine, and sign in.",
            "Copy the verification code the page then shows you and paste it back here.",
            "This terminal is waiting for that code; nothing happens on its own.",
        ):
            self.console.bullet(line, indent=6)
        # The failure this warns about is silent and looks like Google's fault: a
        # URL copied from a wrapped terminal loses everything after the first
        # line, which is still a valid-looking sign-in link -- Google renders the
        # page, then refuses to go anywhere because the request has no scope and
        # no redirect. Nothing in that page says the link was truncated. Capture
        # is what removes the need to select it at all; this stays for the run
        # where capture is off or could not find the URL.
        self.console.bullet(
            "If the page takes your email and then goes nowhere, the URL arrived cut "
            "short -- take the captured copy above rather than selecting it again.",
            indent=6,
        )
        if steps > 1:
            # Two separate consents, and the second URL looks just like the first.
            self.console.bullet(
                f"You will do this {steps} times: once for your account, once for the credential the SDKs read.",
                indent=6,
            )
        if not self.ctx.stdin_is_interactive():
            self.console.bullet(
                "Nothing is attached to stdin here, so there is nowhere to paste it.",
                indent=6,
            )
            outcome.add_remediation(
                "Run `initialize-claude-ai run --phases 2` in a terminal: the sign-in needs a "
                "code pasted back, and stdin here is not a tty."
            )
        else:
            outcome.add_remediation(
                "For a browser known to reach this machine, --browser-loopback-redirect lets it "
                "hand the result back without a code."
            )

    def _login_steps(self, gcloud: str, *, paste: bool, user_login: bool) -> list[tuple[str, list[str]]]:
        """The interactive commands to run, minus the one that buys nothing.

        ``gcloud auth login --update-adc`` writes the application-default
        credential itself, so following it with ``gcloud auth
        application-default login`` overwrites that credential with an
        equivalent one -- after making the operator repeat the entire
        URL-and-code dance for it. Worse, the second URL is indistinguishable
        from the first at a glance, and two of them on one screen is exactly how
        the wrong one gets opened: a stale URL yields a sign-in page that
        accepts an email and then stops.

        So the ADC step runs only when the login step did not already cover it
        -- which is read from the rendered template rather than assumed, so an
        operator who edits ``commands.login_headless`` still decides.
        """
        steps: list[tuple[str, list[str]]] = []
        user_argv = self.command("login_headless" if paste else "login", {"gcloud": gcloud}) if user_login else []
        if user_argv:
            steps.append(("gcloud auth login", user_argv))
        if self.config.bool_("configure_adc", True) and ADC_FLAG not in user_argv:
            name = "adc_login_headless" if paste else "adc_login"
            steps.append(("gcloud auth application-default login", self.command(name, {"gcloud": gcloud})))
        return steps

    def _run_login(self, gcloud: str, outcome: AuthOutcome, *, user_login: bool) -> AuthOutcome:
        paste = self.paste_code
        timeout = self.settings.float_("auth.interactive_timeout_seconds", 1800)
        steps = self._login_steps(gcloud, paste=paste, user_login=user_login)

        self._sign_in_hint(paste, len(steps), outcome)

        ok = True
        for label, argv in steps:
            if not argv:
                continue
            self.log.info("running %s", label)
            # Interactive: the operator reads the prompts and pastes the code.
            # Routed through the base class so the URL gcloud prints is captured
            # rather than left to be selected out of a wrapped screen.
            result = self.run_sign_in(argv, timeout=timeout, label=label)
            if not result.ok:
                ok = False
                outcome.add_remediation(f"Run it manually and follow the prompts: {' '.join(argv)}")
                break

        refreshed = self.inspect()
        outcome.authenticated = refreshed.authenticated
        outcome.identity = refreshed.identity
        outcome.data.update(refreshed.data)
        outcome.status = Status.CHANGED if (ok and refreshed.authenticated) else Status.FAILED
        outcome.detail = refreshed.detail if refreshed.authenticated else "interactive sign-in did not complete"
        return outcome

    # -- post-login configuration ---------------------------------------------

    def _apply_config(self, gcloud: str, outcome: AuthOutcome) -> None:
        account = self.config.str_("account")
        if account and account != outcome.identity:
            result = self.runner.run(
                self.command("set_account", {"gcloud": gcloud, "account": account}),
                timeout=self.timeout("set_account"),
                label="gcloud config set account",
            )
            if result.ok:
                outcome.identity = account

        # A fresh credential means a fresh ADC file, so the quota project is
        # written whether or not the active project needed changing.
        self._apply_project(gcloud, outcome, quota=True)

    def _apply_project(self, gcloud: str, outcome: AuthOutcome, *, quota: bool) -> None:
        """Point gcloud itself at the project, not just the commands run here.

        ``--project`` on each command covers this run; ``gcloud config set
        project`` covers everything afterwards -- phase 3's own gcloud calls,
        and the operator's. It runs on the already-signed-in path too, because a
        project can be named on a run that acquires nothing, and skipping it
        there would leave the machine authenticated against one project and
        pointed at another.
        """
        project = self.project()
        if not project:
            return
        if outcome.data.get("project", "") != project:
            self.runner.run(
                self.command("set_project", {"gcloud": gcloud, "project": project}),
                timeout=self.timeout("set_project"),
                label="gcloud config set project",
            )
            outcome.data["project"] = project
            quota = True  # ADC's quota project follows the project it is for

        quota_project = self.config.str_("quota_project") or project
        if quota and quota_project and self.config.bool_("configure_adc", True):
            self.runner.run(
                self.command("set_quota_project", {"gcloud": gcloud, "project": quota_project}),
                timeout=self.timeout("set_quota_project"),
                label="gcloud adc set-quota-project",
            )

    # -- verification ----------------------------------------------------------

    def verify(self, current: AuthOutcome) -> AuthOutcome:
        gcloud = self._gcloud()
        if not gcloud:
            current.verified = False
            return current

        # Before the token is minted and the APIs are listed, so that both
        # happen against the project this run named.
        self._apply_project(gcloud, current, quota=False)

        needs_adc = self.config.bool_("configure_adc", True)
        name = "print_adc_token" if needs_adc else "print_token"
        probe = self.runner.probe(self.command(name, {"gcloud": gcloud}), timeout=self.timeout(name))
        current.verified = probe.ok
        if not probe.ok:
            current.detail = "credential present but no access token could be minted"
            current.add_remediation(
                "gcloud auth application-default login --no-launch-browser"
                if needs_adc
                else "gcloud auth login --no-launch-browser"
            )
            return current

        # Token minted successfully; report identity and API readiness.
        if not current.identity:
            current.identity = self._active_account(self._accounts(gcloud))
        self._check_apis(gcloud, current)
        current.detail = f"{current.identity or 'credential'} verified"
        project = current.data.get("project")
        if project:
            current.detail += f" / project {project}"
        return current

    def _check_apis(self, gcloud: str, outcome: AuthOutcome) -> None:
        required = self.config.str_list("required_apis")
        if not required:
            return
        probe = self.runner.probe(
            self.command("list_services", {"gcloud": gcloud}), timeout=self.timeout("list_services")
        )
        if not probe.ok:
            outcome.data["api_check"] = "could not list enabled services"
            return
        try:
            services = {str(s.get("config", {}).get("name", "")) for s in json.loads(probe.stdout or "[]")}
        except ValueError:
            services = set()
        missing = [api for api in required if api not in services]
        outcome.data["required_apis"] = required
        outcome.data["missing_apis"] = missing
        if not missing:
            return
        if self.config.bool_("enable_apis", False):
            for api in missing:
                self.runner.run(
                    self.command("enable_service", {"gcloud": gcloud, "service": api}),
                    timeout=self.timeout("enable_service"),
                    label=f"gcloud services enable {api}",
                )
        else:
            outcome.add_remediation(f"Enable the required API(s): gcloud services enable {' '.join(missing)}")


def _read(path: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8")
    except OSError:
        return ""


def _path_from_ref(ref: str) -> str:
    """Extract a filesystem path from a ``file:``/bare-path reference."""
    ref = (ref or "").strip()
    if not ref:
        return ""
    scheme, _, remainder = ref.partition(":")
    if scheme.lower() in ("file", "path") and remainder:
        return os.path.expanduser(remainder.strip())
    if scheme.lower() == "env":
        return os.path.expanduser(os.environ.get(remainder.strip(), ""))
    return os.path.expanduser(ref)
