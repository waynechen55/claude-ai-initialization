"""An agent failure that is about the model's credential, and what to do about it.

smelt.example.net lost its agents halfway through a run: the Google Cloud
credential behind the model expired, every agent call came back with "Could not
load Google Cloud credentials", and the failure matched none of the wordings
that mean "the runtime cannot serve any request". So each stage spent its three
attempts, each target after it did the same, and the fallback's advice was to
*raise* ``mcp.agent.max_attempts`` -- more of the thing that could not work.

A credential failure is a fact about the installation, like a model that is
not enabled. The difference is that it can often be fixed without anybody
typing: a refresh token that is still good turns into a fresh access token
when asked. So each ``[mcp.agent.credential_failures.<name>]`` table says

* ``markers`` -- wordings that mean *this* credential is the problem (matched
  case-insensitively against the failure text);
* ``refresh`` -- one non-interactive command that renews it, tried at most
  once per run: an argv whose ``{name}`` tokens are resolved as binaries
  (``{gcloud}``), or the dotted settings key of an argv defined elsewhere
  (``auth.google_cloud.commands.print_adc_token``) so it is written once;
* ``remedy`` -- what the operator runs when the refresh is not enough.

Nothing here names a provider in code; adding one is a TOML table.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Mapping

from ..core.config import fill
from ..core.context import RunContext
from ..core.logging_setup import get_logger

log = get_logger("mcp.agent_credentials")

_SECTION = "mcp.agent.credential_failures"
_STATE_KEY = "mcp_agent_credential_refreshes"
_CREATE = threading.Lock()


@dataclass
class CredentialProfile:
    name: str
    markers: list[str] = field(default_factory=list)
    refresh: list[str] = field(default_factory=list)
    remedy: str = ""
    timeout: float = 0.0

    def matches(self, text: str) -> bool:
        low = str(text or "").lower()
        return any(m and m.lower() in low for m in self.markers)


def profiles(settings: Any) -> list[CredentialProfile]:
    """Every configured credential-failure profile, in file order."""
    out: list[CredentialProfile] = []
    default_timeout = settings.float_("mcp.agent.credential_refresh_timeout_seconds", 60)
    for name, raw in settings.dict_(_SECTION, {}).items():
        if not isinstance(raw, Mapping):
            continue
        refresh = raw.get("refresh") or []
        if isinstance(refresh, str):
            refresh = settings.list_(refresh, [])
        out.append(
            CredentialProfile(
                name=str(name),
                markers=[str(m) for m in raw.get("markers") or [] if str(m).strip()],
                refresh=[str(a) for a in refresh if str(a).strip()],
                remedy=str(raw.get("remedy") or "").strip(),
                timeout=float(raw.get("timeout_seconds") or default_timeout),
            )
        )
    return out


def markers(settings: Any) -> list[str]:
    """Every wording that means "the model's credential is the problem"."""
    return [m for profile in profiles(settings) for m in profile.markers]


def profile_for(settings: Any, text: str) -> CredentialProfile | None:
    """The profile whose markers ``text`` carries, or ``None``."""
    for profile in profiles(settings):
        if profile.matches(text):
            return profile
    return None


class Refresher:
    """Tries each profile's refresh at most once per run, shared by every address."""

    def __init__(self, ctx: RunContext) -> None:
        self.ctx = ctx
        self._guard = threading.Lock()
        self._done: dict[str, tuple[bool, str]] = {}

    @classmethod
    def for_run(cls, ctx: RunContext) -> "Refresher":
        with _CREATE:
            found = ctx.state.get(_STATE_KEY)
            if not isinstance(found, cls):
                found = ctx.state[_STATE_KEY] = cls(ctx)
            return found

    def tried(self, profile: CredentialProfile) -> tuple[bool, str] | None:
        with self._guard:
            return self._done.get(profile.name)

    def refresh(self, profile: CredentialProfile) -> tuple[bool, bool, str]:
        """``(first_time, ok, detail)``. Only the first caller for a profile runs anything."""
        with self._guard:
            if profile.name in self._done:
                ok, detail = self._done[profile.name]
                return False, ok, detail
            ok, detail = self._run(profile)
            self._done[profile.name] = (ok, detail)
            return True, ok, detail

    def _run(self, profile: CredentialProfile) -> tuple[bool, str]:
        if not self.ctx.settings.bool_("mcp.agent.credential_refresh", True):
            return False, "the refresh is turned off (mcp.agent.credential_refresh)"
        if not profile.refresh:
            return False, "no refresh command is configured for it"
        argv = self._resolve(profile.refresh)
        if argv is None:
            return False, f"{profile.refresh[0]} is not installed here"
        log.info("the agent's %s credential was refused; trying one non-interactive refresh", profile.name)
        result = self.ctx.runner.probe(argv, timeout=profile.timeout, capture=True)
        if result.ok:
            return True, "refreshed"
        # The output of a token command can be a token: keep only its first
        # line of *error*, never stdout.
        err = (getattr(result, "stderr", "") or "").strip().splitlines()
        return False, f"the refresh failed (exit {result.returncode}){': ' + err[0][:200] if err else ''}"

    def _resolve(self, argv: list[str]) -> list[str] | None:
        tokens: dict[str, str] = {}
        for part in argv:
            if part.startswith("{") and part.endswith("}") and len(part) > 2:
                name = part[1:-1]
                path = self.ctx.tool(name)
                if not path:
                    return None
                tokens[name] = path
        return [fill(part, tokens) for part in argv]


__all__ = ["CredentialProfile", "Refresher", "markers", "profile_for", "profiles"]
