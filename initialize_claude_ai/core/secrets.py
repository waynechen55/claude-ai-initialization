"""Secret resolution.

No credential is ever written into this repository.  A credential is always
named by a *reference string* that says where to fetch it from at run time:

======================  =======================================================
``env:NAME``            read from the process environment
``file:/path``          read (and strip) the contents of a file
``keyring:svc/user``    read from the OS keyring via the ``keyring`` package
``command:argv...``     run a command and take stdout (explicit opt-in only)
``gcloud-adc``          ``auth.google_cloud.commands.print_adc_token``
``gcloud-token``        ``auth.google_cloud.commands.print_token``
``prompt:Label``        interactively prompt (no echo); refuses when headless
``literal:VALUE``       the value itself, inline -- refused under ``--secure``
``none``                explicitly absent
======================  =======================================================

A bare string with no scheme is treated as ``env:<string>`` -- the safe reading --
so that a config file can say ``api_key_ref = "ANTHROPIC_API_KEY"``.

``literal:`` is the one scheme that carries a payload rather than a location.
It exists because a plain ``--auth-anthropic-key`` flag has to land somewhere, and
routing it through the same resolver means it is warned about, registered for
redaction and refusable in exactly one place.  ``secrets.allow_literal = false``
(what ``--secure`` sets) turns it off for every layer at once.
"""

from __future__ import annotations

import getpass
import os
import shlex
import stat
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from . import redact
from .config import fill
from .errors import SecretPolicyError, SecretResolutionError
from .logging_setup import get_logger
from .proc import Runner

log = get_logger("secrets")

#: Schemes that execute something. Disabled unless the caller opts in.
_EXECUTING_SCHEMES = {"command", "gcloud-adc", "gcloud-token"}

#: Schemes that carry no argument, so they are written without a trailing colon.
#: Without this set a bare ``gcloud-adc`` would be read as ``env:gcloud-adc``.
_ARGLESS_SCHEMES = {"gcloud-adc", "gcloud-token", "prompt"}

#: Schemes that carry the value inline instead of naming where to find it.
#: ``--secure`` turns these off, whichever layer they arrived from.
_INLINE_SCHEMES = {"literal"}


@dataclass(frozen=True)
class Secret:
    """A resolved credential that refuses to render itself."""

    _value: str
    source: str = "unknown"

    def __post_init__(self) -> None:
        redact.register(self._value)

    def reveal(self) -> str:
        """Return the raw value. Call this only when handing it to a consumer."""
        return self._value

    @property
    def fingerprint(self) -> str:
        """A stable, non-reversible identifier safe to log and to show a user."""
        import hashlib

        digest = hashlib.sha256(self._value.encode("utf-8")).hexdigest()[:12]
        return f"sha256:{digest}"

    @property
    def hint(self) -> str:
        """Last few characters, for 'is this the key I think it is?' checks."""
        tail = self._value[-4:] if len(self._value) >= 12 else ""
        return f"...{tail}" if tail else "(short)"

    def __len__(self) -> int:
        return len(self._value)

    def __bool__(self) -> bool:
        return bool(self._value)

    def __str__(self) -> str:  # pragma: no cover - defensive
        return redact.MASK

    def __repr__(self) -> str:  # pragma: no cover - defensive
        return f"Secret(source={self.source!r}, {self.fingerprint}, len={len(self._value)})"


class SecretResolver:
    """Resolves reference strings into :class:`Secret` objects."""

    def __init__(
        self,
        runner: Runner,
        *,
        allow_execution: bool = True,
        allow_prompt: bool = True,
        allow_literal: bool = True,
        strict_file_permissions: bool = True,
        command_timeout: float | None = None,
        gcloud_commands: Mapping[str, Sequence[str]] | None = None,
        gcloud_timeouts: Mapping[str, float] | None = None,
    ) -> None:
        """``gcloud_commands`` are the ``print_token`` and ``print_adc_token``
        templates of ``auth.google_cloud.commands``, with ``{gcloud}`` still in
        them, and ``gcloud_timeouts`` their entries in
        ``auth.google_cloud.command_timeout_seconds``: the same commands phase 2
        runs, so a reference mints a token exactly the way the sign-in checks
        one. ``command_timeout`` bounds ``command:``; unset, the runner's
        default applies to all three."""
        self.runner = runner
        self.command_timeout = command_timeout
        self.gcloud_commands = dict(gcloud_commands or {})
        self.gcloud_timeouts = dict(gcloud_timeouts or {})
        self.allow_execution = allow_execution
        self.allow_prompt = allow_prompt
        self.allow_literal = allow_literal
        self.strict_file_permissions = strict_file_permissions
        self._cache: dict[str, Secret | None] = {}

    def resolve(self, ref: str | None, *, required: bool = False, label: str = "secret") -> Secret | None:
        """Resolve ``ref``; return ``None`` when absent and ``required`` is False."""
        if ref is None or not str(ref).strip() or str(ref).strip().lower() == "none":
            if required:
                raise SecretResolutionError(
                    f"no reference configured for {label}",
                    remediation="Set a reference such as 'env:MY_TOKEN' or 'file:/path/to/token'.",
                )
            return None
        ref = str(ref).strip()
        if ref in self._cache:
            cached = self._cache[ref]
            if cached is None and required:
                raise SecretResolutionError(f"{label} could not be resolved from {self.describe(ref)}")
            return cached

        scheme, remainder = self._split(ref)

        if scheme in _EXECUTING_SCHEMES and not self.allow_execution:
            raise SecretPolicyError(
                f"reference scheme '{scheme}' is disabled",
                remediation="Re-run with secret execution enabled, or use env:/file:.",
            )

        if scheme in _INLINE_SCHEMES and not self.allow_literal:
            # Deliberately describes the reference and not the payload.
            raise SecretPolicyError(
                f"{label} was supplied inline as '{scheme}:', which --secure forbids",
                remediation="Name the credential instead: env:NAME, file:/path or keyring:service/user.",
            )

        try:
            value = self._dispatch(scheme, remainder.strip(), label)
        except SecretResolutionError:
            raise
        except Exception as exc:  # noqa: BLE001 - never leak the payload in a traceback
            raise SecretResolutionError(f"failed to resolve {label} from {self.describe(ref)}: {exc}") from None

        secret = Secret(value, source=self.describe(ref)) if value else None
        self._cache[ref] = secret
        if secret is None and required:
            raise SecretResolutionError(
                f"{label} is empty at {self.describe(ref)}",
                remediation="Populate the reference, or point the config at a different one.",
            )
        if secret:
            log.debug("resolved %s from %s (%s, %d chars)", label, secret.source, secret.fingerprint, len(secret))
        return secret

    def first_available(self, refs: list[str], *, label: str = "secret") -> Secret | None:
        """Return the first reference that yields a value.

        A reference that simply holds nothing is stepped over; one that was
        *refused* is raised, so a forbidden credential is never mistaken for an
        absent one.
        """
        for ref in refs:
            try:
                secret = self.resolve(ref, label=label)
            except SecretPolicyError:
                raise
            except SecretResolutionError as exc:
                log.debug("secret reference %s unusable: %s", self.describe(ref), exc)
                continue
            if secret:
                return secret
        return None

    # -- scheme handlers ------------------------------------------------------

    def _dispatch(self, scheme: str, remainder: str, label: str) -> str:
        handler = {
            "env": self._from_env,
            "file": self._from_file,
            "path": self._from_file,
            "keyring": self._from_keyring,
            "command": self._from_command,
            "gcloud-adc": lambda _r: self._from_gcloud(adc=True),
            "gcloud-token": lambda _r: self._from_gcloud(adc=False),
            "prompt": lambda r: self._from_prompt(r or label),
            "literal": self._from_literal,
        }.get(scheme)
        if handler is None:
            raise SecretResolutionError(
                f"unknown secret reference scheme '{scheme}'",
                remediation="Use one of: env, file, keyring, command, gcloud-adc, gcloud-token, prompt.",
            )
        return handler(remainder)

    #: Another place values are read from when the environment lacks one --
    #: set by phase 4 to Claude Code's own ``settings.json`` env, from which
    #: Claude Code expands an entry's ``${NAME}``, so a trial sees what the
    #: entry will get. A callable returning a mapping; None for none.
    fallback_env: object = None

    def _from_env(self, name: str) -> str:
        value = (os.environ.get(name) or "").strip()
        if value or self.fallback_env is None:
            return value
        try:
            return str((self.fallback_env() or {}).get(name) or "").strip()
        except Exception:  # noqa: BLE001 - a fallback must never break resolving
            return ""

    def check_file_permissions(self, raw_path: str | Path) -> None:
        """Warn when a credential file is readable by more than its owner.

        Public because not every credential travels through this resolver: gcloud
        reads a service-account key itself, and that file holds a private key
        just the same.  Loud rather than fatal -- refusing to authenticate over
        a permission bit would strand anyone whose CI checks keys out world
        readable, which is exactly who most needs to be told.
        """
        path = Path(raw_path).expanduser()
        if not self.strict_file_permissions or not path.is_file():
            return
        mode = path.stat().st_mode
        if mode & (stat.S_IRWXG | stat.S_IRWXO):
            log.warning(
                "credential file %s is group/world accessible (mode %o); tighten it with chmod 600",
                path,
                stat.S_IMODE(mode),
            )

    def _from_file(self, raw_path: str) -> str:
        path = Path(raw_path).expanduser()
        if not path.is_file():
            return ""
        self.check_file_permissions(path)
        return path.read_text(encoding="utf-8").strip()

    def _from_keyring(self, spec: str) -> str:
        try:
            import keyring  # type: ignore[import-not-found]
        except ModuleNotFoundError:
            raise SecretResolutionError(
                "the 'keyring' package is not installed",
                remediation="pip install keyring, or use a different secret reference scheme.",
            ) from None
        service, _, username = spec.partition("/")
        if not username:
            raise SecretResolutionError("keyring reference must be 'keyring:service/username'")
        return (keyring.get_password(service, username) or "").strip()

    def _from_command(self, spec: str) -> str:
        argv = shlex.split(spec)
        if not argv:
            raise SecretResolutionError("empty command in secret reference")
        result = self.runner.probe(argv, timeout=self.command_timeout or self.runner.default_timeout)
        if not result.ok:
            raise SecretResolutionError(f"secret command exited with {result.returncode}")
        return result.stdout.strip()

    def _from_gcloud(self, *, adc: bool) -> str:
        gcloud = self.runner.which("gcloud")
        if not gcloud:
            raise SecretResolutionError(
                "gcloud is not on PATH",
                remediation="Run phase 1 (install) first, or set the gcloud binary path.",
            )
        name = "print_adc_token" if adc else "print_token"
        argv = [fill(part, {"gcloud": gcloud}) for part in self.gcloud_commands.get(name, ())]
        if not argv:
            raise SecretResolutionError(
                f"no gcloud command is configured to mint this token (auth.google_cloud.commands.{name})",
                remediation=f"Set auth.google_cloud.commands.{name} in a config file.",
            )
        result = self.runner.probe(argv, timeout=self.gcloud_timeouts.get(name) or self.runner.default_timeout)
        if not result.ok:
            raise SecretResolutionError(
                "gcloud could not mint an access token",
                remediation=(
                    "Run `gcloud auth application-default login --no-launch-browser`"
                    if adc
                    else "Run `gcloud auth login --no-launch-browser`"
                ),
            )
        return result.stdout.strip()

    def _from_prompt(self, label: str) -> str:
        if not self.allow_prompt or not sys.stdin.isatty():
            raise SecretResolutionError(
                f"cannot prompt for {label}: no interactive terminal",
                remediation="Provide the value through env: or file: instead.",
            )
        return getpass.getpass(f"Enter {label}: ").strip()

    def _from_literal(self, value: str) -> str:
        log.warning(
            "a literal secret was supplied in configuration; prefer env:, file: or keyring: "
            "so the value never lands in a file that can be committed"
        )
        return value

    # -- helpers --------------------------------------------------------------

    @staticmethod
    def _split(ref: str) -> tuple[str, str]:
        """Split a reference into ``(scheme, remainder)``.

        A reference with no colon is either an argument-less scheme
        (``gcloud-adc``) or, the safe default, the name of an environment
        variable -- so ``api_key_ref = "ANTHROPIC_API_KEY"`` keeps working.
        """
        scheme, separator, remainder = ref.partition(":")
        scheme = scheme.strip().lower()
        if not separator:
            return (scheme, "") if scheme in _ARGLESS_SCHEMES else ("env", ref.strip())
        return scheme, remainder

    @classmethod
    def describe(cls, ref: str) -> str:
        """A loggable description of a reference (never includes the value)."""
        scheme, remainder = cls._split(ref)
        if scheme == "literal":
            return "literal:<inline>"
        return f"{scheme}:{remainder}" if remainder else scheme
