"""Authentication backends for phase 2."""

from __future__ import annotations

from typing import Iterable

from ..core.context import RunContext
from ..core.errors import InitializeClaudeAiError
from .anthropic_api import AnthropicApiAuth
from .base import AuthBackend, AuthOutcome
from .claude_oauth import ClaudeOAuthAuth
from .google_cloud import GoogleCloudAuth

#: Registry keyed by the identifier used in ``auth.backends`` / ``--auth-backend``.
BACKENDS: dict[str, type[AuthBackend]] = {
    GoogleCloudAuth.key: GoogleCloudAuth,
    ClaudeOAuthAuth.key: ClaudeOAuthAuth,
    AnthropicApiAuth.key: AnthropicApiAuth,
}

#: Friendly aliases so the CLI accepts the obvious short forms.
ALIASES = {
    "gcp": GoogleCloudAuth.key,
    "google": GoogleCloudAuth.key,
    "gcloud": GoogleCloudAuth.key,
    "vertex": GoogleCloudAuth.key,
    "oauth": ClaudeOAuthAuth.key,
    "claude": ClaudeOAuthAuth.key,
    "claude-ai": ClaudeOAuthAuth.key,
    "website": ClaudeOAuthAuth.key,
    "cli": ClaudeOAuthAuth.key,
    "api": AnthropicApiAuth.key,
    "api-key": AnthropicApiAuth.key,
    "anthropic": AnthropicApiAuth.key,
}


def resolve_keys(tokens: Iterable[str], available: Iterable[str]) -> list[str]:
    """Expand ``--auth-backend`` tokens (including ``all``) to backend keys."""
    available = [a for a in available if a in BACKENDS]
    chosen: list[str] = []
    for raw in tokens:
        token = str(raw).strip().lower()
        if not token:
            continue
        if token in ("all", "*"):
            for key in available:
                if key not in chosen:
                    chosen.append(key)
            continue
        if token in ("none", "-"):
            return []
        key = ALIASES.get(token, token)
        if key not in BACKENDS:
            raise InitializeClaudeAiError(
                f"unknown authentication backend {raw!r}",
                remediation=("Choose from: " + ", ".join(sorted(BACKENDS)) + ", or 'all'."),
            )
        if key not in chosen:
            chosen.append(key)
    return chosen


def build(ctx: RunContext, keys: Iterable[str]) -> list[AuthBackend]:
    return [BACKENDS[key](ctx) for key in keys if key in BACKENDS]


__all__ = [
    "AuthBackend",
    "AuthOutcome",
    "AnthropicApiAuth",
    "ClaudeOAuthAuth",
    "GoogleCloudAuth",
    "BACKENDS",
    "ALIASES",
    "resolve_keys",
    "build",
]
