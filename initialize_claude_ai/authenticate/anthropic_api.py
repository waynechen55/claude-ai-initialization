"""Direct Anthropic API credentials.

The credential is never held in configuration: ``api_key_refs`` and
``auth_token_refs`` list *references* (``env:``, ``file:``, ``keyring:``,
``command:``) and the first one that yields a value wins.  The value is
registered with the redaction filter the moment it is resolved, so it cannot
appear in a log line or a report.

The two lists are separate because the two credentials are not interchangeable:
an API key goes in ``x-api-key``, while a bearer token -- what a gateway or
proxy in front of the API issues -- goes in ``Authorization``.  Sending one in
the other's header reads upstream as an invalid credential, so the kind that
resolved decides the header.

Verification uses the models endpoint -- authenticated, but it does not generate
tokens, so proving the credential costs nothing.
"""

from __future__ import annotations

from ..core.report import Status
from .base import AuthBackend, AuthOutcome


class AnthropicApiAuth(AuthBackend):
    key = "anthropic-api"
    title = "Anthropic API key"
    config_key = "auth.anthropic_api"

    #: (config key holding the references, kind, human label). Ordered: an API
    #: key is the ordinary case, a bearer token the gateway one.
    CREDENTIALS = (
        ("api_key_refs", "api_key", "Anthropic API key"),
        ("auth_token_refs", "auth_token", "Anthropic bearer token"),
    )

    def inspect(self) -> AuthOutcome:
        outcome = AuthOutcome(backend=self.key)
        wanted = [(kind, label, self.config.str_list(setting)) for setting, kind, label in self.CREDENTIALS]
        outcome.data["references"] = [self.ctx.secrets.describe(ref) for _kind, _label, refs in wanted for ref in refs]

        for kind, label, refs in wanted:
            secret = self.ctx.secrets.first_available(refs, label=label)
            if not secret:
                continue
            outcome.authenticated = True
            outcome.method = label.replace("Anthropic ", "")
            outcome.identity = f"{outcome.method} {secret.fingerprint} ({secret.hint})"
            outcome.detail = f"resolved from {secret.source}"
            self.ctx.state["anthropic_api_key"] = secret
            self.ctx.state["anthropic_api_key_kind"] = kind
            return outcome

        helper = self.config.str_("api_key_helper")
        if helper:
            outcome.data["api_key_helper"] = helper
            outcome.detail = "no key resolved; an apiKeyHelper is configured for Claude Code"
        else:
            outcome.status = Status.SKIPPED
            outcome.detail = "no API key or bearer token reference resolved"
            outcome.add_remediation(
                "Export a key: export ANTHROPIC_API_KEY=...  (or point "
                "auth.anthropic_api.api_key_refs at file:/path or keyring:service/user)",
                "Name it on the command line instead: --auth-anthropic-key-ref env:ANTHROPIC_API_KEY "
                "(behind a gateway, --auth-anthropic-token-ref)",
            )
        return outcome

    def acquire(self, current: AuthOutcome) -> AuthOutcome:
        # There is no interactive flow for a raw API key: it is issued in the
        # Anthropic Console and supplied to this machine out of band.
        if not current.authenticated and current.status != Status.SKIPPED:
            current.status = Status.SKIPPED
            current.add_remediation(
                "Create a key at https://console.anthropic.com/settings/keys and expose it "
                "through one of the configured references."
            )
        return current

    def verify(self, current: AuthOutcome) -> AuthOutcome:
        secret = self.ctx.state.get("anthropic_api_key")
        if secret is None:
            current.verified = False
            return current

        base_url = self.config.str_("base_url", "https://api.anthropic.com").rstrip("/")
        path = self.config.str_("probe_path", "/v1/models?limit=1")
        timeout = self.config.float_("probe_timeout_seconds", 30)
        headers = {
            "anthropic-version": self.config.str_("version_header", "2023-06-01"),
            "accept": "application/json",
        }
        if self.ctx.state.get("anthropic_api_key_kind") == "auth_token":
            name = self.config.str_("headers.auth_token", "Authorization")
            prefix = self.config.str_("headers.auth_token_prefix", "Bearer ")
            headers[name] = f"{prefix}{secret.reveal()}"
        else:
            headers[self.config.str_("headers.api_key", "x-api-key")] = secret.reveal()

        with self.ctx.http(timeout=timeout) as client:
            response = client.get(f"{base_url}{path}", headers=headers, timeout=timeout)

        current.data["probe_status"] = response.status
        current.data["endpoint"] = f"{base_url}{path}"
        if response.ok:
            payload = response.json() or {}
            models = [m.get("id") for m in payload.get("data", []) if isinstance(m, dict)]
            current.verified = True
            current.detail = f"{current.method or 'credential'} accepted by {base_url}"
            if models:
                current.data["sample_model"] = models[0]
            return current

        current.verified = False
        if response.status in (401, 403):
            current.detail = f"{current.method or 'credential'} rejected by {base_url} (HTTP {response.status})"
            current.add_remediation(
                "Check that the credential is active and has API access.",
                "Behind a gateway, a token belongs in auth_token_refs (Authorization) rather "
                "than api_key_refs (x-api-key).",
            )
        elif response.status == 0:
            current.detail = f"could not reach {base_url}: {response.error}"
            current.add_remediation("Check network egress and any proxy configuration.")
        else:
            current.detail = f"unexpected response from {base_url}: HTTP {response.status}"
        return current
