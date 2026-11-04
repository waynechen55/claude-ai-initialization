"""Phase 1 -- installation.

Traditional (non-agentic) by necessity: this phase is what *creates* the Claude
environment, so it cannot depend on it.

For each catalog component the phase verifies first, installs only if the
verification failed, then verifies again -- so a re-run is a no-op and a fresh
machine converges in one pass.  Every component that resolves to a real
executable has its path and version recorded as an environment variable for the
later phases and for the operator's shell.
"""

from __future__ import annotations

import os
import re
import sys
import time
from pathlib import Path
from typing import Any

from ..core.report import PhaseResult, Safety, Status
from ..install import ownership, space
from ..install.components import ComponentSpec, load_components, select_components
from ..install.installers import Installer, run_strategies
from ..install.toolchains import command_index, prefetch_components, prefetch_mode, query_bin_dir
from ..install.verify import PRESENT, CheckResult, Verifier, checks_for, usable
from .base import Phase, register


@register
class InstallPhase(Phase):
    """Phase 1 -- get the toolchain onto this machine.

    Deterministic by necessity: this is the phase that creates the Claude
    environment, so it cannot use one. Every component is verified first,
    installed only if that verification failed, and verified again afterwards;
    the catalog and the per-distribution commands are entirely configuration
    (see ``10-components.toml``), which is what makes the phase portable
    across distributions without a branch per distribution in the code.
    """

    slug = "install"
    number = 1
    title = "Install the Claude AI toolchain"
    description = (
        "Verify, install and re-verify every catalog component -- the Claude Code CLI "
        "and whatever runtime, cloud CLI and MCP tooling the selected backend needs. "
        "Components already present are left alone, so a re-run costs nothing."
    )
    enable_key = "install"

    #: Tokens chosen deliberately before the loop, which verifying a component
    #: must not silently take back. The python interpreter is the one that
    #: matters: the ``python3`` component provides ``{python}``, so without this
    #: verifying it would point the token back at the system interpreter and
    #: undo the virtualenv that ``install.python_target`` had just selected.
    _pinned_tokens: frozenset[str] = frozenset()
    #: Components a registered entry needs whose catalog entry says
    #: ``prefetch = false``: verified and checked for use, never installed.
    _check_only: dict[str, str] = {}

    def banner_facts(self) -> list[tuple[str, str]]:
        rows = [
            ("Platform", f"{self.ctx.platform.pretty_name} ({self.ctx.platform.arch})"),
            ("Packages via", self.ctx.platform.package_manager or "none detected"),
            ("Fallbacks", ", ".join(self.ctx.platform.universal_managers) or "none"),
        ]
        modes = self._active_modes()
        if modes:
            rows.append(("Mode", ", ".join(modes)))
        rows.append(("Prefetch", prefetch_mode(self.settings)))
        return rows + super().banner_facts()

    def safety(self, result: PhaseResult) -> tuple[str, str]:
        """Only a *required* component can block: the optional ones are demoted.

        ``_process`` already turns a failed optional component into a skip, so a
        step that is still ``failed`` here is one of the binaries the later
        phases actually shell out to. That is the whole distinction the operator
        needs, and nothing else in the run can make it.
        """
        if not result.steps or result.fatal:
            # The phase never executed, or stopped for want of a required
            # setting -- neither is a component problem, and the base class has
            # the right words for both.
            return super().safety(result)
        failed = [s.name for s in result.steps if s.status == Status.FAILED]
        if failed:
            return Safety.BLOCKED, (
                f"Required component(s) are missing: {', '.join(failed)}. Phases 2-4 run "
                "these binaries directly, so they will fail for the same reason. Install "
                "them (or drop them from install.components) and re-run phase 1 first."
            )
        degraded = [s.name for s in result.steps if s.status == Status.SKIPPED and s.detail.startswith("optional;")]
        attention = [s.name for s in result.steps if s.status == Status.PARTIAL]
        if attention:
            # Installed but not usable here, a reinstall that kept the old copy,
            # a program a registered server runs that nothing here installs:
            # each step names its fix.
            return Safety.CAUTION, (
                f"Every required component is present. These need attention before what depends on them "
                f"works: {', '.join(attention)}; each step above names the fix."
            )
        if degraded:
            return Safety.CAUTION, (
                f"Every required component is present and verified. Optional extras could "
                f"not be installed ({', '.join(degraded)}); the run can continue, but any "
                "feature that depends on them will be unavailable."
            )
        return Safety.SAFE, (
            "Every selected component verified after install, and its path and version "
            "were recorded for the later phases. Phase 2 has the toolchain it needs."
        )

    def execute(self, result: PhaseResult) -> None:
        """Select the components, then verify -> install -> re-verify each.

        Selection comes from the catalog narrowed by group, ``--install-only``
        and ``--install-skip``; a component in an optional group that fails
        never fails the phase.
        """
        settings = self.settings
        catalog = load_components(settings)
        # --install-only / --install-skip travel in the state bag; the same
        # selection set in configuration or the environment is the fallback.
        only = self.ctx.state.get("install_only") or settings.str_list("install.only")
        skip = self.ctx.state.get("install_skip") or settings.str_list("install.skip")
        selected, notes = select_components(
            catalog,
            groups=settings.str_list("install.default_groups", ["base", "runtime", "claude", "cloud", "browser"]),
            only=only,
            skip=skip,
            system=self.ctx.platform.system,
            # Narrower than an operating system, for the components that only
            # make sense somewhere inside one -- wslview on WSL, and nowhere else.
            traits=self.ctx.platform.traits(),
        )
        prefetched = self._prefetch(catalog, selected, only=only, skip=skip, notes=notes)
        for key, reason in self._entry_demand(catalog, selected, only=only, skip=skip, notes=notes).items():
            prefetched.setdefault(key, reason)
        for note in notes:
            self.log.info("%s", note)

        if not selected:
            result.add("selection", Status.SKIPPED, "no components selected")
            self._finish_entries(result, only=only)
            return

        # The rest of the context is already in the banner; only the selection
        # is not, because it is computed here.
        self.console.kv("Components", f"{len(selected)} selected")
        self.console.write("")

        verifier = Verifier(self.ctx)
        installer = Installer(self.ctx)
        self._seed_tokens(verifier, installer)
        self._prepare_python_target(verifier, installer, result)

        optional_groups = set(
            settings.str_list(
                "install.optional_groups",
                [
                    "browser",
                    "desktop",
                    "mcp-tooling",
                    "mcp-runtimes",
                ],
            )
        )
        summary_rows: list[list[str]] = []

        self._disk_full = ""
        for component in selected:
            started = time.monotonic()
            full = self._disk_full or self.runner.out_of_space
            if full and self.settings.bool_("install.stop_on_full_disk", True):
                # A full disk stops the phase at once: every later step would
                # fail too, each in words that blame something else.
                result.add(
                    component.key,
                    Status.SKIPPED,
                    f"not attempted: the disk is full ({full})",
                    data={"title": component.title, "group": component.group},
                )
                self.console.status(Status.SKIPPED, f"{component.key:<24}", "not attempted: the disk is full")
                summary_rows.append([component.key, component.group, Status.SKIPPED, "", ""])
                continue
            status, detail, checks = self._process(component, verifier, installer, result)
            self._publish(component, checks, verifier, installer)
            fix = ""
            if status in (Status.OK, Status.CHANGED):
                # There, but does it work here? Docker with its daemon stopped
                # is installed and useless; reinstalling it changes nothing.
                works, words = usable(self.ctx, component, verifier)
                if not works:
                    status, detail = Status.PARTIAL, f"installed, not usable here: {words}"
                    fix = words

            effective = status
            if status == Status.FAILED and (
                not component.required
                or component.group in optional_groups
                # Asked for by --install-prefetch, not by the operator: a
                # later phase can still do without it.
                or component.key in prefetched
            ):
                effective = Status.SKIPPED
                detail = f"optional; {detail}"

            step = result.add(
                f"{component.key}",
                effective,
                detail,
                duration=time.monotonic() - started,
                data={
                    "title": component.title,
                    "group": component.group,
                    "required": component.required,
                    **({"prefetched": prefetched[component.key]} if component.key in prefetched else {}),
                    "checks": [{"kind": c.kind, "ok": c.ok, "detail": c.detail, "version": c.version} for c in checks],
                },
            )
            self.console.status(effective, f"{component.key:<24}", detail)
            summary_rows.append(
                [
                    component.key,
                    component.group,
                    effective,
                    next((c.version for c in checks if c.version), ""),
                    next((c.path for c in checks if c.path), ""),
                ]
            )
            if fix:
                step.remediation.append(fix)
            if effective in (Status.FAILED, Status.SKIPPED, Status.PARTIAL) and step.remediation:
                for line in step.remediation:
                    self.console.bullet(line, indent=6)

        self._finish_entries(result, only=only)
        self._record_environment(result, summary_rows)

    # -- setup -----------------------------------------------------------------

    def _prefetch(
        self,
        catalog: list[ComponentSpec],
        selected: list[ComponentSpec],
        *,
        only: list[str],
        skip: list[str],
        notes: list[str],
    ) -> dict[str, str]:
        """Add what ``install.prefetch`` asks for to ``selected``; ``key -> reason``.

        ``--install-only`` names exactly what to install, so it wins and the
        prefetch is left out -- with a note, since the setting may have come
        from a file the operator is not looking at.
        """
        mode = prefetch_mode(self.settings)
        if mode == "none":
            return {}
        if only:
            notes.append(f"install.prefetch = {mode} ignored: --install-only names what to install")
            return {}
        added, prefetch_notes = prefetch_components(
            catalog,
            self.settings,
            mode=mode,
            selected=selected,
            skip=skip,
            system=self.ctx.platform.system,
            traits=self.ctx.platform.traits(),
        )
        notes.extend(prefetch_notes)
        if not added:
            return {}
        order = {c.key: i for i, c in enumerate(catalog)}
        selected.extend(component for component, _reason in added)
        selected.sort(key=lambda c: order.get(c.key, len(order)))
        return {component.key: reason for component, reason in added}

    def _entry_demand(
        self,
        catalog: list[ComponentSpec],
        selected: list[ComponentSpec],
        *,
        only: list[str],
        skip: list[str],
        notes: list[str],
    ) -> dict[str, str]:
        """Add the catalog components behind programs registered MCP entries run and this machine lacks.

        ``install.demand_from_entries``. The launcher templates above say what
        phase 4 *may* run; the entries in ``.mcp.json`` and Claude Code's own
        configuration say what Claude Code *will* run -- a copied file named
        ``github-mcp-server`` and a client bridge's ``osc`` that nothing had
        installed. Optional like every prefetch; a component whose entry says
        ``prefetch = false`` is checked, never installed. What no catalog
        component installs is handled after the loop (:meth:`_finish_entries`).
        """
        self._check_only = {}
        if not self.settings.bool_("install.demand_from_entries", True):
            return {}
        if only:
            notes.append("install.demand_from_entries ignored: --install-only names what to install")
            return {}
        from ..mcp_auto_discovery import entry_needs

        try:
            needs = entry_needs.missing_programs(self.ctx)
        except Exception as exc:  # noqa: BLE001 - reading entries must never cost the install
            self.log.warning("the registered MCP entries could not be read: %s", exc)
            return {}
        if not needs:
            return {}
        index = command_index(catalog)
        skip_set = {str(x).strip() for x in skip if str(x).strip()}
        already = {c.key for c in selected}
        added: dict[str, str] = {}
        for need in needs:
            component = index.get(need.program) or index.get(re.sub(r"[0-9.]+$", "", need.program))
            if component is None or not component.applies_to(self.ctx.platform.system, self.ctx.platform.traits()):
                continue
            if component.key in skip_set or component.group in skip_set:
                notes.append(f"not installing '{component.key}' for {need.words()}: --install-skip names it")
                continue
            reason = need.words()
            if component.key not in already:
                if not component.prefetch:
                    self._check_only[component.key] = reason
                selected.append(component)
                already.add(component.key)
            added.setdefault(component.key, reason)
            notes.append(f"'{component.key}' for {reason}")
        order = {c.key: i for i, c in enumerate(catalog)}
        selected.sort(key=lambda c: order.get(c.key, len(order)))
        return added

    def _finish_entries(self, result: PhaseResult, *, only: list[str]) -> None:
        """After the catalog: what registered entries still lack, and this machine's paths for them.

        * a program a client bridge calls is installed by the client's own
          ``install`` steps; any program still missing is named, with the run
          that finds or replaces its server (``--mcp-recheck``);
        * :func:`.entry_needs.machine_paths` gives this machine's path to each
          program variable an entry reads and cannot resolve here
          (``install.machine_paths``).

        Never fails the phase: these are servers, not the toolchain.
        """
        from ..mcp_auto_discovery import entry_needs

        try:
            if self.settings.bool_("install.portable_launchers", True):
                # Before anything asks what the entries run: a launcher of
                # another machine's install becomes the package's portable launch.
                for line in entry_needs.repair_private_launchers(self.ctx, apply=not self.ctx.dry_run):
                    result.add(
                        "portable launch",
                        Status.CHANGED,
                        ("[dry-run] would rewrite " if self.ctx.dry_run else "rewrote ") + line,
                    )
                    self.console.status(Status.CHANGED, f"{'portable launch':<24}", line)
            if self.settings.bool_("install.demand_from_entries", True) and not only:
                self._entry_programs(result, entry_needs)
            if self.settings.bool_("install.machine_paths.enabled", True):
                self._machine_paths(result, entry_needs)
            if self.settings.bool_("install.machine_paths.repair_trust", True):
                self._repair_trust(result)
        except Exception as exc:  # noqa: BLE001 - tidying entries must never cost the install
            self.log.warning("the registered MCP entries could not be checked: %s", exc)

    def _entry_programs(self, result: PhaseResult, entry_needs: Any) -> None:
        from ..mcp_auto_discovery.client_bridge import install_with_ways

        needs = entry_needs.missing_programs(self.ctx)
        installed: list[str] = []
        for need in needs:
            if need.role != entry_needs.CALL or not need.ways:
                continue
            if self.runner.out_of_space and self.settings.bool_("install.stop_on_full_disk", True):
                break  # nothing more is installed on a full disk; what is left is named below
            if self.ctx.dry_run:
                installed.append(
                    f"[dry-run] {need.program} ({need.server}) would be installed with "
                    + "; ".join(" ".join(w) for w in need.ways)
                )
                continue
            install_with_ways(
                self.ctx,
                need.program,
                need.ways,
                timeout=self.settings.float_("install.package_timeout_seconds", 900),
                label=f"install {need.program}",
            )
            if self.runner.which(need.program):
                installed.append(f"{need.program} ({need.server})")
        if self.ctx.dry_run:
            left = [n for n in needs if not (n.role == entry_needs.CALL and n.ways)]
        else:
            left = entry_needs.missing_programs(self.ctx) if installed else needs
        if installed:
            result.add(
                "entry programs installed",
                Status.CHANGED,
                "installed for registered MCP servers: " + ", ".join(installed),
            )
            self.console.status(Status.CHANGED, f"{'entry programs':<24}", ", ".join(installed))
        if not left:
            return
        remediation: list[str] = []
        for need in left:
            how = (
                ("install it: " + "; ".join(" ".join(w) for w in need.ways))
                if need.ways
                else (
                    "nothing in the catalog installs it; find or replace the server: "
                    + entry_needs.recheck_command(need.address)
                )
            )
            remediation.append(f"{need.words()}, which is not on this machine -- {how}")
        detail = f"{len(left)} program(s) registered MCP servers run are not on this machine: " + ", ".join(
            f"{n.program} ({n.server})" for n in left
        )
        result.add("entry programs", Status.PARTIAL, detail, remediation=remediation)
        self.console.status(Status.PARTIAL, f"{'entry programs':<24}", detail)
        for line in remediation:
            self.console.bullet(line, indent=6)

    def _repair_trust(self, result: PhaseResult) -> None:
        """Every CA variable a new terminal of any kind sets to a file that cannot work, corrected on this machine.

        ``.mcp.json`` travels; a machine's startup files do not. On the machine
        it was copied to, a login shell's ``~/.profile`` and
        ``/etc/profile.d`` exported ``SSL_CERT_FILE`` as a deleted /tmp bundle,
        and every tool of every Python server failed there. Through the one
        per-machine writer (:func:`.consumer_trust.repair_broken`), logged
        place by place; nothing when the writer is off or this is a dry run.
        """
        from ..mcp_auto_discovery import consumer_trust

        if self.ctx.dry_run:
            return
        fixed = [r for r in consumer_trust.repair_broken(self.ctx) if r is not None]
        if not fixed:
            return
        detail = "; ".join(r.why for r in fixed if getattr(r, "why", ""))
        result.add("trust settings", Status.CHANGED, detail)
        self.console.status(Status.CHANGED, f"{'trust settings':<24}", detail)

    def _machine_paths(self, result: PhaseResult, entry_needs: Any) -> None:
        found = entry_needs.machine_paths(self.ctx, apply=not self.ctx.dry_run)
        if found.set:
            pairs = ", ".join(f"{k}={v} ({', '.join(found.users.get(k, []))})" for k, v in found.set.items())
            if self.ctx.dry_run:
                detail = f"[dry-run] would set {pairs} where a new terminal's Claude Code reads it"
            else:
                detail = f"set {pairs} in {', '.join(found.places) or 'nowhere (see the log)'}"
            checked = [f"{name}: {words}" for name, (_status, words) in found.checks.items()]
            from ..mcp_auto_discovery.claude_check import PASSED

            result.add(
                "machine paths",
                Status.CHANGED,
                detail,
                data={"set": found.set, "checks": {k: list(v) for k, v in found.checks.items()}},
                remediation=[
                    f"Claude Code -- {name}: {words}"
                    for name, (status, words) in found.checks.items()
                    if status != PASSED
                ],
            )
            self.console.status(Status.CHANGED, f"{'machine paths':<24}", detail)
            for line in checked:
                self.console.bullet(f"Claude Code -- {line}", indent=6)
        if found.unresolved:
            remediation = []
            for variable, why in found.unresolved.items():
                for server, path in found.missing.get(variable, {}).items():
                    remediation.append(
                        f"{server} reads {path} ({variable}): {why}; phase 4 regenerates or replaces "
                        "it: " + entry_needs.recheck_command(self._entries_for_addresses().get(server, ""))
                    )
            detail = "no path on this machine for " + ", ".join(found.unresolved)
            result.add("machine paths unresolved", Status.PARTIAL, detail, remediation=remediation)
            self.console.status(Status.PARTIAL, f"{'machine paths':<24}", detail)
            for line in remediation:
                self.console.bullet(line, indent=6)

    def _entries_for_addresses(self) -> dict[str, str]:
        """Entry name -> the address it serves, for naming the run that repairs it."""
        cached = self.__dict__.get("_entry_addresses")
        if cached is not None:
            return cached
        from ..mcp_auto_discovery.existing import loaded_entries
        from ..mcp_auto_discovery.writer import McpWriter

        writer = McpWriter(self.ctx)
        cached = {
            name: writer.address_of_entry(name, entry) for name, (entry, _where) in loaded_entries(self.ctx).items()
        }
        self.__dict__["_entry_addresses"] = cached
        return cached

    def _active_modes(self) -> list[str]:
        """Name the flags that change what installation means, so the log says so."""
        settings = self.settings
        modes = []
        if settings.bool_("install.force", False):
            modes.append("force-install (ignoring 'already installed')")
        if settings.bool_("install.downgrade", False):
            pins = settings.dict_("install.pins")
            total = sum(len(v) for v in pins.values() if isinstance(v, dict))
            modes.append(f"downgrade ({total} pinned version(s))")
        if settings.bool_("install.allow_break_system_packages", False):
            modes.append("break-dependencies (PEP 668 override)")
        return modes

    def _seed_tokens(self, verifier: Verifier, installer: Installer) -> None:
        """Seed ``{python}``/``{npm}``/... from what is already on this machine."""
        for token, names in (
            ("python", ["python3", "python"]),
            ("npm", ["npm"]),
            ("node", ["node"]),
            ("uv", ["uv"]),
            ("claude", ["claude"]),
            ("gcloud", ["gcloud"]),
        ):
            for name in names:
                path = self.ctx.tool(name)
                if path:
                    verifier.set_token(token, path)
                    installer.set_token(token, path)
                    break

    def _auto_python_target(self, installer: Installer, python: str, result: PhaseResult) -> str:
        """Decide between the system interpreter and a virtualenv.

        Most current distributions -- openSUSE, Debian, Fedora, Arch, and
        Homebrew's Python -- ship an interpreter marked externally managed under
        PEP 668, where both ``pip install`` and ``uv pip install`` refuse to
        write and ``--user`` is no escape. Two of this toolchain's components
        are Python packages, so "auto" has to mean something on those hosts: it
        means a virtualenv, which is the one answer that installs what was asked
        for without arguing with the distro's package manager.
        """
        env = installer.python_environment(python)
        if env.in_venv:
            # Already inside one -- almost certainly the operator's own choice.
            result.add("python-target", Status.OK, f"using the active virtualenv {env.prefix}")
            return "system"
        if not env.blocked:
            return "system"
        if self.settings.bool_("install.allow_break_system_packages", False):
            result.add(
                "python-target",
                Status.OK,
                "installing into the externally managed interpreter (--break-dependencies)",
            )
            return "system"
        result.add(
            "python-target",
            Status.OK,
            f"{python} is externally managed (PEP 668); using a virtualenv instead",
            remediation=[
                "Install system-wide instead with --break-dependencies, or pick the "
                "interpreter yourself with --set install.python_target=system|user|venv.",
            ],
        )
        return "venv"

    def _prepare_python_target(self, verifier: Verifier, installer: Installer, result: PhaseResult) -> None:
        """Honour ``install.python_target``; create the venv when requested."""
        target = self.settings.str_("install.python_target", "auto").strip().lower()
        python = verifier.tokens.get("python", "python3")
        if target == "auto":
            target = self._auto_python_target(installer, python, result)
        installer.python_target = target
        if target != "venv":
            return
        venv_path = self.settings.path("install.venv_path") or (self.ctx.state_dir / "venv")
        venv_python = venv_path / "bin" / "python"
        # Built from the interpreter running this program, not from whichever
        # python3 came first on PATH. Phase 4 imports claude_agent_sdk in this
        # process, and a wheel with a compiled extension -- rpds, under
        # jsonschema -- will not load across minor versions. A venv the agent
        # phase cannot import is an install that only looks like it worked.
        builder = sys.executable or python
        if venv_python.exists():
            verifier.set_token("python", str(venv_python))
            installer.set_token("python", str(venv_python))
            self._pinned_tokens = frozenset({"python"})
            result.add("python-venv", Status.OK, f"reusing {venv_path}")
            return
        created = self.runner.run(
            [builder, "-m", "venv", str(venv_path)],
            timeout=self.settings.float_("install.venv_timeout_seconds", self.runner.default_timeout),
            label="create venv",
        )
        if created.ok and (venv_python.exists() or self.ctx.dry_run):
            verifier.set_token("python", str(venv_python))
            installer.set_token("python", str(venv_python))
            self._pinned_tokens = frozenset({"python"})
            result.add("python-venv", Status.CHANGED, f"created {venv_path}")
            self.ctx.env.set("PYTHON_VENV", str(venv_path))
        else:
            result.add(
                "python-venv",
                Status.FAILED,
                f"could not create venv at {venv_path}",
                remediation=["Install the python3-venv package, or set install.python_target=auto"],
            )

    # -- per-component work ----------------------------------------------------

    def _verify(self, component: ComponentSpec, verifier: Verifier) -> list[CheckResult]:
        """The checks that say whether the component is *there* -- the ones a failure installs for."""
        return [verifier.check(spec) for spec in checks_for(component.verify, PRESENT)]

    @staticmethod
    def _satisfied(checks: list[CheckResult]) -> bool:
        return bool(checks) and not any(c.blocking for c in checks)

    def _process(
        self,
        component: ComponentSpec,
        verifier: Verifier,
        installer: Installer,
        result: PhaseResult,
    ) -> tuple[str, str, list[CheckResult]]:
        if not checks_for(component.verify, PRESENT):
            return Status.SKIPPED, "no verification defined", []

        checks = self._verify(component, verifier)
        satisfied = self._satisfied(checks)
        if not satisfied and component.key in self._check_only:
            return (
                Status.SKIPPED,
                f"not installed; needed because {self._check_only[component.key]}, but its "
                f"catalog entry says prefetch = false -- --install-only {component.key} installs it",
                checks,
            )
        force = self.settings.bool_("install.force", False)
        if satisfied and not force:
            return Status.OK, self._describe(checks), checks

        if satisfied:
            # --force-install: reinstall on purpose. Nothing is wrong with the
            # component, so a missing strategy is not a failure here.
            if not component.install:
                return Status.OK, f"{self._describe(checks)}; nothing to reinstall", checks
            self.log.info("%s reinstalling (--force-install)", component.key)
        else:
            blocking = [c for c in checks if c.blocking]
            self.log.info(
                "%s needs installation: %s",
                component.key,
                "; ".join(c.detail for c in blocking) or "verification failed",
            )
            if not component.install:
                return (
                    Status.FAILED,
                    f"not installed and no install strategy is defined ({self._describe(checks)})",
                    checks,
                )

        # Past this point a package manager owns the terminal, and on a slow
        # mirror that is minutes of nothing. Say what is being installed first,
        # so the silence reads as work rather than as a hang.
        title = str(getattr(component, "title", "") or "")
        strategies = None
        before: list[Any] = []
        owner = None
        leftovers: set[str] = set()
        if not satisfied and installer is not None and space.enabled(self.settings) and not self.ctx.dry_run:
            # A fresh install that cannot fit stops before it starts: a full disk
            # half way through fails it and every step after it.
            free, margin = space.free_mb(self.ctx.state_dir), space.margin_mb(self.settings)
            if 0 <= free < margin:
                self._disk_full = f"{free} MB free where {component.key} would install, at least {margin} MB needed"
                return Status.FAILED, f"not attempted: {self._disk_full}", checks
        if satisfied:
            # A reinstall repairs the copy in use, through whoever installed it
            # -- never a fresh install of a second copy beside it (:mod:`.ownership`).
            before = ownership.fingerprint(checks)
            if installer is not None and ownership.enabled(self.settings):
                owner = ownership.find(installer, component, checks)
                if owner.known:
                    strategies, why_none = ownership.plan(installer, component, owner)
                    self.log.info("%s: %s; reinstalling through it", component.key, owner.how)
                    if not strategies:
                        return Status.OK, f"{self._describe(checks, title)}; kept -- {why_none}", checks
                    if owner.kind == "system" and owner.packages:
                        # Each owning package is reinstalled once per run: python313-base
                        # was reinstalled three times, once per component it owns.
                        fresh = [p for p in owner.packages if p not in installer.reinstalled]
                        if not fresh:
                            return (
                                Status.OK,
                                f"{self._describe(checks, title)}; reinstalled this run with "
                                f"{', '.join(owner.packages)}",
                                checks,
                            )
                    if space.enabled(self.settings) and not self.ctx.dry_run:
                        need, how = space.need_mb(installer, owner, strategies)
                        free = space.free_mb(space.where(owner))
                        if need and 0 <= free < need:
                            # --force-install reinstalls what works: never what fills a disk.
                            return (
                                Status.OK,
                                f"{self._describe(checks, title)}; kept -- reinstalling it in place "
                                f"needs ~{need} MB ({how}), {free} MB free",
                                checks,
                            )
                    if owner.folder:
                        leftovers = space.siblings(owner.folder)
        full_before = bool(self.runner.out_of_space)
        self.console.progress(f"{component.key:<24}", f"installing {component.title}")
        if strategies is None:
            tried = run_strategies(installer, component, on_bin_dir=self._add_bin_dir, logger=self.log)
        else:
            tried = run_strategies(
                installer, component, on_bin_dir=self._add_bin_dir, strategies=strategies, logger=self.log
            )
        attempts, remediation, installed = tried.attempts, tried.remediation, tried.installed
        reasons = list(getattr(tried, "reasons", []) or [])
        # A full disk is this step's only when one of its own commands hit it.
        own_full = self.runner.out_of_space if self.runner.out_of_space and not full_before else ""
        if owner is not None and owner.kind == "system" and installed:
            installer.reinstalled.update(owner.packages)
        after_notes: list[str] = []
        if owner is not None and owner.folder and not installed and not self.ctx.dry_run:
            # A failed step must leave the tool working: if it worked before
            # and does not now, a runnable copy the step left is put back first.
            # gcloud's failed reinstall left /root/google-cloud-sdk without
            # bin/gcloud, and the staging copy -- maybe the only whole one --
            # was then deleted by its name.
            if satisfied and not self._satisfied(self._verify(component, verifier)):
                restored = space.restore_runnable(
                    owner.folder, self._relative_tool(owner), self._tool_runs, self.settings
                )
                if restored:
                    after_notes.append(restored)
            removed = space.remove_new_siblings(owner.folder, leftovers, self.settings)
            if removed:
                reasons.append(f"removed what the failed step left: {', '.join(removed)}")
            others = space.left_by_others(owner.folder, leftovers, self.settings)
            if others:
                after_notes.append("left in place, not this program's to delete: " + ", ".join(others))

        # Re-verify against reality regardless of what the installer claimed.
        checks = self._verify(component, verifier)
        if self._satisfied(checks) and satisfied and not installed and attempts and not self.ctx.dry_run:
            # A reinstall that failed while the older copy is still there is not
            # "OK": archive-tools and node were reported OK on a full disk, the
            # reinstall having just failed with "No space left on device".
            # The reason is the installer's own words, not the advice to rerun it.
            why = (
                own_full
                or (next((r for r in reversed(reasons) if not r.startswith("removed ")), ""))
                or (remediation[0] if remediation else "every strategy failed")
            )
            return (
                Status.PARTIAL,
                f"reinstall failed ({why}); kept the existing {self._describe(checks, title)}"
                + "".join(f"; {n}" for n in after_notes),
                checks,
            )
        if (
            self._satisfied(checks)
            and satisfied
            and installed
            and before
            and not self.ctx.dry_run
            and self.settings.bool_("install.changed_needs_proof", True)
            and ownership.fingerprint(checks) == before
        ):
            # Something reported success, and the binary in use is the same file
            # as before: node was "CHANGED" while nvm's install timed out and
            # /usr/bin/node was never touched.
            return (
                Status.PARTIAL,
                f"reinstall reported success, but the copy in use did not change; kept the existing "
                f"{self._describe(checks, title)}",
                checks,
            )
        if self._satisfied(checks):
            status = Status.CHANGED if installed and not self.ctx.dry_run else Status.OK
            if self.ctx.dry_run and installed:
                status = Status.CHANGED
            return status, self._describe(checks, title), checks

        if self.ctx.dry_run and installed:
            return (
                Status.CHANGED,
                "[dry-run] install simulated; verification will run for real without --dry-run",
                checks,
            )

        detail = "; ".join([*(c.detail for c in checks if c.blocking), *after_notes]) or "verification failed"
        step_remediation = remediation or [
            f"Tried: {'; '.join(attempts)}" if attempts else "No strategy was applicable.",
        ]
        if reasons:
            step_remediation = [f"Last attempt said: {reasons[-1]}", *step_remediation]
        result.remediation.extend(line for line in step_remediation if line and line not in result.remediation)
        return Status.FAILED, detail, checks

    @staticmethod
    def _relative_tool(owner: Any) -> str:
        """The tool's path inside the folder that holds it (``bin/gcloud``), "" when it is not inside."""
        import os

        folder = str(getattr(owner, "folder", "") or "")
        for path in (getattr(owner, "path", ""), getattr(owner, "real", "")):
            path = str(path or "")
            if folder and path and os.path.abspath(path).startswith(os.path.abspath(folder) + os.sep):
                return os.path.relpath(path, folder)
        return ""

    def _tool_runs(self, path: str) -> bool:
        """Does the tool at ``path`` run (``install.tool_runs_args``, default ``--version``)?"""
        import os
        import subprocess

        if not os.path.isfile(path) or not os.access(path, os.X_OK):
            return False
        args = [str(a) for a in self.settings.list_("install.tool_runs_args", ["--version"])]
        try:
            done = subprocess.run(
                [path, *args],
                capture_output=True,
                timeout=self.settings.float_(  # noqa: S603
                    "install.tool_runs_timeout_seconds", 60.0
                ),
                stdin=subprocess.DEVNULL,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        return done.returncode == 0

    @staticmethod
    def _describe(checks: list[CheckResult], title: str = "") -> str:
        parts = [c.detail for c in checks if c.ok and c.detail]
        # gcloud-auth-plugin read "kept the existing verified": its check names no path.
        return "; ".join(parts[:2]) if parts else (title or "verified")

    def _add_bin_dir(self, directory: str) -> None:
        expanded = os.path.expanduser(directory)
        if expanded and expanded not in self.runner.extra_paths:
            self.runner.extra_paths.insert(0, expanded)
            self.log.debug("added %s to the search path", expanded)

    def _publish(
        self,
        component: ComponentSpec,
        checks: list[CheckResult],
        verifier: Verifier,
        installer: Installer,
    ) -> None:
        """Record paths/versions and expose this component to later templates."""
        path = next((c.path for c in checks if c.ok and c.path), "")
        version = next((c.version for c in checks if c.ok and c.version), "")
        env_key = component.env_key() or component.key
        if path:
            self.ctx.env.set_binary(env_key, path)
            self._add_bin_dir(str(Path(path).parent))
            # Where the tool puts what it installs (`go env GOPATH`/bin), so a
            # server built with it can be launched -- in this run and in a
            # later `--phases 4` run, which reads it back from the env file.
            bin_dir = query_bin_dir(
                self.runner,
                component,
                path,
                self.settings.float_("install.bin_dir_query_timeout_seconds", 25),
            )
            if bin_dir:
                self.ctx.env.set_bin_dir(env_key, bin_dir)
                self._add_bin_dir(bin_dir)
        if version:
            self.ctx.env.set_version(env_key, version)
        if component.provides_token and path and component.provides_token not in self._pinned_tokens:
            verifier.set_token(component.provides_token, path)
            installer.set_token(component.provides_token, path)

    # -- output ----------------------------------------------------------------

    def _record_environment(self, result: PhaseResult, rows: list[list[str]]) -> None:
        self.console.write("")
        self.console.table(rows, headers=["component", "group", "status", "version", "path"])

        self.ctx.env.set("STATE_DIR", str(self.ctx.state_dir))
        self.ctx.env.set("PLATFORM", self.ctx.platform.distro_id or self.ctx.platform.system)
        self.ctx.env.set("PACKAGE_MANAGER", self.ctx.platform.package_manager)

        written = self.ctx.env.save()
        if written:
            result.add(
                "environment",
                Status.CHANGED,
                f"recorded {len(self.ctx.env)} variables in {self.ctx.state_dir}",
                data={"files": [str(p) for p in written], "variables": self.ctx.env.summary()},
                remediation=[f"Expose them in your shell: {self.ctx.env.shell_hint()}"],
            )
            self.console.write("")
            self.console.status(
                Status.CHANGED,
                "environment recorded",
                self.ctx.env.shell_hint(),
            )
        elif self.ctx.dry_run:
            result.add("environment", Status.SKIPPED, "[dry-run] environment file not written")
        else:
            result.add("environment", Status.OK, "environment already up to date")
