"""The :class:`RunContext` handed to every phase.

A phase never reaches for a global: configuration, the console, the subprocess
runner, the secret resolver, platform facts, the environment store and the
cross-phase state bag all arrive through this object.  That is what makes the
phases independently runnable and testable.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from .config import Settings
from .deadline import Deadline
from .envfile import EnvStore
from .http import HttpClient
from .logging_setup import Console, get_logger
from .platform_detect import PlatformInfo
from .proc import Runner
from .report import RunReport
from .secrets import SecretResolver
from .trust_store import TrustStore

log = get_logger("context")


@dataclass
class RunContext:
    """Everything a phase is allowed to reach: one object, passed everywhere.

    Phases do not construct their own settings, console, process runner or HTTP
    client -- they are handed this, which is what makes ``--dry-run``,
    redaction, the log transcript and the timeout policy apply to every phase
    identically rather than wherever each one remembered to honour them.
    Built once by :func:`~initialize_claude_ai.cli.build_context`.
    """

    settings: Settings
    console: Console
    runner: Runner
    secrets: SecretResolver
    platform: PlatformInfo
    env: EnvStore
    report: RunReport
    dry_run: bool = False
    auto_agree: bool = False
    #: When this run must stop starting new work (--timeout). Never expires
    #: unless one was asked for, so no phase needs to check whether it is set.
    deadline: Deadline = field(default_factory=Deadline.none)
    #: Free-form bag used to pass discoveries between phases within one run
    #: (e.g. phase 2 publishes the active backend for phase 3).
    state: dict[str, Any] = field(default_factory=dict)

    # -- derived paths ---------------------------------------------------------

    @property
    def state_dir(self) -> Path:
        return self.env.state_dir

    @property
    def claude_home(self) -> Path:
        configured = self.settings.path("paths.claude_home")
        return configured or Path.home() / ".claude"

    @property
    def work_dir(self) -> Path:
        configured = self.settings.path("paths.work_dir")
        return configured or self.state_dir / "work"

    def ensure_dir(self, path: Path) -> Path:
        if not self.dry_run:
            path.mkdir(parents=True, exist_ok=True)
        return path

    # -- helpers ---------------------------------------------------------------

    def http(self, **overrides: Any) -> HttpClient:
        """Build an HTTP client from configuration, with per-call overrides."""
        section = self.settings.section("http")
        kwargs: dict[str, Any] = {
            "timeout": section.float_("timeout_seconds", 15.0),
            "connect_timeout": section.float_("connect_timeout_seconds", 5.0),
            "user_agent": section.str_("user_agent", "initialize-claude-ai/0.1 (+https://modelcontextprotocol.io)"),
            "verify_tls": section.bool_("verify_tls", True),
            "max_bytes": section.int_("max_response_bytes", 2_000_000),
            "follow_redirects": section.bool_("follow_redirects", True),
        }
        kwargs.update(overrides)
        if kwargs["verify_tls"] and "ssl_context" not in kwargs:
            # One trust store for every request this program makes, whichever
            # backend is installed: the host's, certifi's and whatever this
            # run adds. Additive only -- see core/trust_store.py.
            kwargs["ssl_context"] = TrustStore.for_run(self).context()
        return HttpClient(**kwargs)

    def browser_mode(self) -> str:
        """``auth.browser_mode``, normalised: ``auto``, ``headless`` or ``browser``."""
        mode = self.settings.str_("auth.browser_mode", "auto").strip().lower() or "auto"
        return mode if mode in ("auto", "headless", "browser") else "auto"

    def uses_pasted_code(self) -> bool:
        """True when an interactive sign-in should print a URL and take a code back.

        One answer for the entire run, not one per backend, so ``--headless``
        means the same thing whichever credential happens to be missing. Every
        backend reads this, and the phase banner reports it before any of them
        runs.

        The pasted code is the default everywhere, display or not. The
        alternative -- a loopback redirect, where the browser hands the result
        to a listener on ``localhost`` -- depends on its last step, and nothing
        here can check that step beforehand: which machine the page ends up on,
        whether an organisation's sign-in page or a browser policy lets it
        return to ``http://localhost``, whether the port is free. A display
        proves only that a browser can be drawn. When the redirect does not
        arrive, the sign-in waits with nothing for the operator to paste. The
        code, shown by the page at the end, works wherever signing in works.

        ``auth.browser_mode`` is the single knob:

        ``auto``      (default) take a pasted code; where a browser can be
                      shown, the URL is opened in it for you
                      (``auth.signin.open_url``).
        ``headless``  take a pasted code and open nothing: you carry the URL
                      yourself. ``--headless``.
        ``browser``   the loopback redirect, for a browser known to reach this
                      machine. ``--browser-loopback-redirect``.
        """
        return self.browser_mode() != "browser"

    def stdin_is_interactive(self) -> bool:
        """True when there is a keyboard behind stdin.

        A sign-in that ends by pasting a code back needs one. Piped or
        redirected input cannot supply it, and the flow would otherwise fail at
        the end of a long wait rather than before it started.
        """
        import sys

        try:
            return bool(sys.stdin) and sys.stdin.isatty()
        except (AttributeError, ValueError):  # closed or replaced stdin
            return False

    def confirm(self, question: str, *, default: bool = False) -> bool:
        """Ask the operator, honouring --auto-agree and non-interactive sessions."""
        if self.auto_agree:
            return True
        if self.dry_run:
            return default
        import sys

        if not sys.stdin.isatty():
            log.debug("non-interactive session, using default %s for: %s", default, question)
            return default
        suffix = "[Y/n]" if default else "[y/N]"
        try:
            answer = input(f"{question} {suffix} ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return False
        if not answer:
            return default
        return answer in ("y", "yes")

    def tool(self, name: str, *, config_key: str = "", aliases: Sequence[str] = ()) -> str | None:
        """Resolve a binary: explicit config, then env store, then PATH.

        ``aliases`` are the other names the same tool answers to -- a component
        keyed ``python`` is installed as ``python3`` nearly everywhere.
        """
        if config_key:
            configured = self.settings.str_(config_key)
            if configured:
                resolved = self.runner.which(configured)
                if resolved:
                    return resolved
                log.warning("configured binary %r for %s not found", configured, name)
        recorded = self.env.binary(name)
        if recorded and Path(recorded).exists():
            return recorded
        extra_dirs = [os.path.expanduser(d) for d in self.settings.str_list("paths.extra_bin_dirs", [])]
        for candidate in (name, *aliases):
            found = self.runner.which(candidate, extra_dirs=extra_dirs)
            if found:
                return found
        return None
