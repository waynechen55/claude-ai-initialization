"""Installing a toolchain a candidate turned out to need -- ``--mcp-provision-missing``.

A local candidate is launched with a program (``uvx``, ``npx``, ``go``,
``cargo``, ``bunx``) and may name install steps run with others. When one is
not on this machine the verifier used to reject the candidate as "not
installed", which was true and also avoidable: phase 1's catalog already knows
how to install every one of those programs on this distribution.

This module closes that gap without changing anything about *which* server is
chosen:

* **Only catalog strategies run.** A command maps to the catalog component
  that verifies or ``provides`` it (:func:`.toolchains.command_index`), and
  only that component's own strategies are tried. A command a candidate or an
  agent wrote is never what causes an install.
* **The verdict text does not change.** A program that could not be installed
  leaves the candidate rejected in exactly the words it always was, so
  nothing downstream -- an agent's retry brief, the ranking, the report
  wording -- can tell the difference. What was tried, and why it failed, goes
  to the report and the log instead.
* **Once per toolchain per run.** The result, success or failure, is kept for
  the run and shared by every pipeline and every address. Under
  ``--concurrency`` the second address to need Go waits for the first one's
  install rather than starting its own, then takes its result; and two
  different toolchains are installed one after the other, never at once.
* **Never a prompt in ``auto``.** A strategy that would run as root is tried
  only when this process is root or the sudo/doas prefix answers without one.

Everything is keyed by component, never by server, so a server nobody here has
heard of is covered exactly like the ones that showed the problem.
"""

from __future__ import annotations

import os
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field
from typing import Any, Iterator

from ..core.context import RunContext
from ..core.logging_setup import get_logger
from ..install.components import ComponentSpec, load_components
from ..install.installers import Installer, run_strategies
from ..install.toolchains import command_index, provision_mode, query_bin_dir
from ..install.verify import PRESENT, Verifier, checks_for
from .locks import KeyedLocks

log = get_logger("mcp.provision")

#: What became of one toolchain.
INSTALLED = "installed"
FAILED = "failed"
DECLINED = "declined"

#: Where the run's provisioner is kept, so both pipelines share one.
_STATE_KEY = "mcp_provisioner"
_CREATE = threading.Lock()

#: The address being worked on this thread, for the report. Set by the phase
#: around each address; empty where nothing set it.
_ADDRESS: ContextVar[str] = ContextVar("mcp_provision_address", default="")


@contextmanager
def working_on(address: str) -> Iterator[None]:
    """Attribute any install that happens inside to ``address``."""
    token = _ADDRESS.set(str(address))
    try:
        yield
    finally:
        _ADDRESS.reset(token)


@dataclass
class Provision:
    """One toolchain phase 4 installed, failed to install, or declined to."""

    component: str
    title: str
    command: str
    status: str
    detail: str
    #: "launch command" or "install step" -- what in the candidate needed it.
    reason: str = ""
    address: str = ""
    server: str = ""
    duration: float = 0.0
    attempts: list[str] = field(default_factory=list)
    remediation: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["duration"] = round(self.duration, 2)
        return data


def fix_command(component: str) -> str:
    """The command that installs ``component`` outside phase 4."""
    from ..cli import PROGRAM  # the CLI imports this module's phase; import late

    return f"{PROGRAM} run --phases 1 --install-only {component}"


class Provisioner:
    """The run's on-demand installer. Get it with :meth:`for_run`."""

    def __init__(self, ctx: RunContext) -> None:
        self.ctx = ctx
        self.settings = ctx.settings
        self.mode = provision_mode(ctx.settings)
        self._catalog: list[ComponentSpec] | None = None
        self._index: dict[str, ComponentSpec] = {}
        self._locks = KeyedLocks()
        #: One install at a time, whatever it is for: two toolchains from the
        #: same package manager would otherwise meet at its lock, and the one
        #: that lost would be remembered as failed for the rest of the run.
        self._installing = threading.Lock()
        self._guard = threading.Lock()
        self._results: dict[str, Provision] = {}
        self._records: list[Provision] = []
        self._elevation: bool | None = None

    @classmethod
    def for_run(cls, ctx: RunContext) -> "Provisioner":
        """The one provisioner for this run, created on first use."""
        with _CREATE:
            found = ctx.state.get(_STATE_KEY)
            if not isinstance(found, cls):
                found = ctx.state[_STATE_KEY] = cls(ctx)
            return found

    # -- what the report reads ----------------------------------------------------

    @property
    def records(self) -> list[Provision]:
        with self._guard:
            return list(self._records)

    def provider(self, command: str) -> ComponentSpec | None:
        """The catalog component that puts ``command`` on this machine, if any."""
        if self._catalog is None:
            with self._guard:
                if self._catalog is None:
                    catalog = load_components(self.settings)
                    # The index first: another thread takes a catalog that is
                    # set as meaning the index is ready, without the guard.
                    self._index = command_index(catalog)
                    self._catalog = catalog
        component = self._index.get(os.path.basename(str(command)))
        if component is None:
            return None
        if not component.applies_to(self.ctx.platform.system, self.ctx.platform.traits()):
            return None
        return component

    # -- the one entry point ---------------------------------------------------------

    def ensure(self, command: str, *, reason: str, server: str = "") -> str:
        """The path to ``command``, installing what provides it first if allowed.

        Returns "" when it is still not there, which the caller treats exactly
        as it treated a missing program before this existed.
        """
        if self.mode == "never" or not command:
            return ""
        found = self.ctx.runner.which(command)
        if found:
            return found
        component = self.provider(command)
        if component is None:
            return ""
        with self._locks(component.key):
            done = self._results.get(component.key)
            if done is None:
                with self._installing:
                    done = self._provision(component, command, reason=reason, server=server)
                with self._guard:
                    self._results[component.key] = done
                    self._records.append(done)
                self._announce(done)
        if done.status != INSTALLED:
            return ""
        return self.ctx.runner.which(command) or ""

    # -- deciding and doing ------------------------------------------------------------

    def _provision(self, component: ComponentSpec, command: str, *, reason: str, server: str) -> Provision:
        started = time.monotonic()

        def verdict(status: str, detail: str, attempts=(), remediation=()) -> Provision:
            return Provision(
                component=component.key,
                title=component.title,
                command=command,
                status=status,
                detail=detail,
                reason=reason,
                address=_ADDRESS.get(),
                server=server,
                duration=time.monotonic() - started,
                attempts=list(attempts),
                remediation=[*remediation, f"Install it yourself: {fix_command(component.key)}"],
            )

        refusal = self._refusal(component)
        if refusal:
            return verdict(DECLINED, refusal)
        strategies, left_out = self._strategies(component)
        if not strategies:
            return verdict(
                DECLINED,
                "every catalog strategy for it would run as root, and there is no way to "
                "do that here without a password prompt" + (f" ({'; '.join(left_out)})" if left_out else ""),
            )

        from ..install import space

        if space.enabled(self.settings):
            # The same rule as phase 1: an install that cannot fit is not started,
            # and a disk that is already full stops every install.
            free, margin = space.free_mb(self.ctx.state_dir), space.margin_mb(self.settings)
            full = self.ctx.runner.out_of_space
            if full or 0 <= free < margin:
                return verdict(
                    DECLINED,
                    f"not attempted: the disk is full ({full})"
                    if full
                    else f"not attempted: {free} MB free, at least {margin} MB needed",
                )
        installer = Installer(self.ctx)
        verifier = Verifier(self.ctx)
        self._seed_tokens(installer, verifier)
        log.info("installing %s on demand: %s needs %s", component.key, reason, command)
        full_before = bool(self.ctx.runner.out_of_space)
        tried = run_strategies(installer, component, on_bin_dir=self._add_bin_dir, strategies=strategies, logger=log)
        attempts = [*tried.attempts, *left_out]
        # Is it there now? Whether it works here is asked before a launch
        # (install.verify.usable), never a reason to call the install failed.
        checks = [verifier.check(spec) for spec in checks_for(component.verify, PRESENT)]
        if checks and not any(c.blocking for c in checks):
            self._publish(component, checks)
            where = next((c.detail for c in checks if c.ok and c.detail), "verified")
            return verdict(INSTALLED, where, attempts)
        blocking = "; ".join(c.detail for c in checks if c.blocking) or "verification failed"
        detail = blocking if tried.installed else f"no strategy succeeded ({blocking})"
        if self.ctx.runner.out_of_space and not full_before:
            detail += f"; the disk filled ({self.ctx.runner.out_of_space})"
        elif getattr(tried, "reasons", None):
            detail += f"; the last attempt said: {tried.reasons[-1]}"
        return verdict(FAILED, detail, attempts, tried.remediation)

    def _refusal(self, component: ComponentSpec) -> str:
        """Why this component is not to be installed at all; "" if it may be."""
        if self.ctx.dry_run:
            return "dry run: nothing is installed"
        allowed = component.on_demand
        if allowed is None:
            allowed = self.settings.bool_("install.on_demand_default", True)
        if not allowed:
            return "its catalog entry says on_demand = false"
        skip = self.ctx.state.get("install_skip") or self.settings.str_list("install.skip")
        skipped = {str(s).strip() for s in skip or () if str(s).strip()}
        if component.key in skipped or component.group in skipped:
            return "--install-skip names it"
        if self.ctx.deadline.expired:
            return "the --timeout deadline has passed"
        if not component.install:
            return "its catalog entry has no install strategy"
        return ""

    def _strategies(self, component: ComponentSpec) -> tuple[list[dict[str, Any]], list[str]]:
        """The strategies this mode may run, and a note for each it may not."""
        if self.mode == "always":
            return list(component.install), []
        probe = Installer(self.ctx)
        kept: list[dict[str, Any]] = []
        left_out: list[str] = []
        for strategy in component.install:
            if probe.needs_privilege(strategy) and not self._can_elevate():
                kind = str(strategy.get("kind", "?"))
                left_out.append(f"{kind}: not tried, it needs root and would prompt (auto)")
                continue
            kept.append(strategy)
        return kept, left_out

    def _can_elevate(self) -> bool:
        """Can this process run a command as root without being asked for a password?"""
        if self._elevation is None:
            platform = self.ctx.platform
            if platform.is_root:
                self._elevation = True
            else:
                prefix = platform.sudo_prefix()
                probe = self.settings.str_list("mcp.packages.provision_privilege_probe", ["true"])
                if not prefix or not probe:
                    self._elevation = False
                else:
                    answer = self.ctx.runner.probe(
                        [*prefix, *probe],
                        timeout=self.settings.float_("mcp.packages.provision_privilege_probe_timeout_seconds", 10),
                    )
                    self._elevation = answer.ok
        return self._elevation

    def _seed_tokens(self, installer: Installer, verifier: Verifier) -> None:
        """``{python}``, ``{npm}``, ... from what is on this machine, as phase 1 does."""
        for component in self._catalog or ():
            token = component.provides_token
            if not token:
                continue
            path = self.ctx.tool(component.env_key() or component.key, aliases=component.binary_names())
            if path:
                installer.set_token(token, path)
                verifier.set_token(token, path)

    def _add_bin_dir(self, directory: str) -> None:
        expanded = os.path.expanduser(directory)
        with self._guard:
            if expanded and expanded not in self.ctx.runner.extra_paths:
                self.ctx.runner.extra_paths.insert(0, expanded)
                log.debug("added %s to the search path", expanded)

    def _publish(self, component: ComponentSpec, checks: list) -> None:
        """Record what was installed where phase 1 records it, for later runs too."""
        path = next((c.path for c in checks if c.ok and c.path), "")
        version = next((c.version for c in checks if c.ok and c.version), "")
        env_key = component.env_key() or component.key
        with self._guard:
            if path:
                self.ctx.env.set_binary(env_key, path)
            if version:
                self.ctx.env.set_version(env_key, version)
        if path:
            self._add_bin_dir(os.path.dirname(path))
            bin_dir = query_bin_dir(
                self.ctx.runner,
                component,
                path,
                self.settings.float_("install.bin_dir_query_timeout_seconds", 25),
            )
            if bin_dir:
                with self._guard:
                    self.ctx.env.set_bin_dir(env_key, bin_dir)
                self._add_bin_dir(bin_dir)
        with self._guard:
            self.ctx.env.save()

    def _announce(self, done: Provision) -> None:
        if done.status == INSTALLED:
            log.info("installed %s on demand for %s (%s)", done.component, done.command, done.detail)
        elif done.status == FAILED:
            log.warning("could not install %s on demand for %s: %s", done.component, done.command, done.detail)
        else:
            log.info("not installing %s on demand for %s: %s", done.component, done.command, done.detail)


__all__ = ["DECLINED", "FAILED", "INSTALLED", "Provision", "Provisioner", "fix_command", "working_on"]
