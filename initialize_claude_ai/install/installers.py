"""Installation strategies.

Each ``[[components.install]]`` block names a ``kind`` that maps to one handler
here.  Handlers are tried in catalog order until one reports success, so a
component can say "native package, else Homebrew, else the vendor's script"
without any of that logic living in the phase.

Every handler is idempotent and honours ``--dry-run``.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import tarfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

from ..core import diskspace
from ..core.config import fill
from ..core.context import RunContext
from ..core.fileio import write_private
from ..core.logging_setup import get_logger
from ..core.proc import CommandResult

log = get_logger("install")


#: One subprocess answering both questions that decide where a Python package
#: may go. The PEP 668 marker sits next to the stdlib, which is why this has to
#: be asked of the interpreter rather than guessed from the distro.
PYTHON_ENV_PROBE = (
    "import json,os,sys,sysconfig;"
    "stdlib=sysconfig.get_path('stdlib') or '';"
    "print(json.dumps({"
    "'in_venv': sys.prefix != getattr(sys,'base_prefix',sys.prefix),"
    "'externally_managed': os.path.exists(os.path.join(stdlib,'EXTERNALLY-MANAGED')),"
    "'prefix': sys.prefix}))"
)

#: pip says "externally-managed-environment"; uv says "is externally managed".
#: Matching only pip's wording is how a uv host got the raw rc=2 instead of the
#: explanation.
EXTERNALLY_MANAGED_SIGNS = ("externally-managed-environment", "externally managed")


def looks_externally_managed(result: CommandResult) -> bool:
    """True when a failed install was refused under PEP 668, by either front end."""
    text = f"{result.stderr or ''}\n{result.stdout or ''}".lower()
    return any(sign in text for sign in EXTERNALLY_MANAGED_SIGNS)


#: Fallbacks for ``install.progress_line_pattern`` and ``install.script_web_page_markers``.
PROGRESS_LINE_PATTERN = r"^[\s#=>.*-]*(\d{1,3}(\.\d+)?%)?[\s#=>.*-]*$"
WEB_PAGE_MARKERS = ("<!doctype html", "<html", "<head>", "<body")


def last_words(text: str, *, lines: int = 2, chars: int = 300, progress: str = PROGRESS_LINE_PATTERN) -> str:
    """A command's own last meaningful lines: what it said when it stopped -- progress bars left out.

    gcloud's installer ended with '"/root/google-cloud-sdk" already exists …
    Remove it … then run again'; the summary said "Run it manually to see the
    full output", and the reason never reached the reader.
    """
    kept: list[str] = []
    for raw in reversed(str(text or "").replace("\r", "\n").splitlines()):
        line = raw.strip()
        if not line or re.fullmatch(progress, line):
            continue
        kept.insert(0, line)
        if len(kept) >= lines:
            break
    joined = " ".join(kept)
    return joined if len(joined) <= chars else "…" + joined[-chars:]


def progress_of(text: str) -> str:
    """The last percentage a command printed ("69.6%"), or ""."""
    found = re.findall(r"(\d{1,3}(?:\.\d+)?)%", str(text or "")[-4000:])
    return f"{found[-1]}%" if found else ""


@dataclass
class PythonEnvironment:
    """What the resolved interpreter will and will not accept."""

    in_venv: bool = False
    externally_managed: bool = False
    prefix: str = ""

    @property
    def blocked(self) -> bool:
        """PEP 668 applies to the base interpreter, not to a virtualenv built
        from it: inside a venv there is no distro package manager to disagree
        with, and ``--user`` is not a way out -- the marker blocks that too."""
        return self.externally_managed and not self.in_venv


@dataclass
class InstallOutcome:
    ok: bool
    detail: str = ""
    changed: bool = False
    skipped: bool = False
    remediation: list[str] = field(default_factory=list)
    data: dict[str, Any] = field(default_factory=dict)
    #: Directory to add to PATH after this strategy succeeded.
    bin_dir: str = ""

    @classmethod
    def skip(cls, reason: str) -> "InstallOutcome":
        return cls(ok=False, detail=reason, skipped=True)


class Installer:
    """Dispatches an install strategy dict to the right handler."""

    #: Strategy kinds whose handler runs the install as root (through the
    #: sudo/doas prefix), or may ask the desktop for authorisation (flatpak).
    #: A fact about the handlers below, read by :mod:`.on_demand` to tell an
    #: install that could stop at a prompt from one that cannot.
    ELEVATED_KINDS = frozenset({"system", "snap", "flatpak"})
    #: Downloads handed to the native package tooling rather than unpacked.
    LOCAL_PACKAGE_SUFFIXES = (".deb", ".rpm")

    def __init__(self, ctx: RunContext) -> None:
        self.ctx = ctx
        self.runner = ctx.runner
        self.platform = ctx.platform
        self.settings = ctx.settings
        self.commands = ctx.settings.section("install.commands")
        self.tokens: dict[str, str] = {}
        self._index_refreshed: set[str] = set()
        self._python_env: dict[str, PythonEnvironment] = {}
        #: Resolved by phase 1 from ``install.python_target``; "auto" until then.
        self.python_target = ctx.settings.str_("install.python_target", "auto").strip().lower()
        #: --force-install: reinstall even when the package is already present.
        self.force = ctx.settings.bool_("install.force", False)
        #: --downgrade: resolve package names through the [install.pins] table.
        self.downgrade = ctx.settings.bool_("install.downgrade", False)
        #: Packages a forced reinstall already reinstalled in this run (each once).
        self.reinstalled: set[str] = set()

    # -- helpers ---------------------------------------------------------------

    def set_token(self, name: str, value: str) -> None:
        if value:
            self.tokens[name] = value

    def expand(self, value: str, *, arch: str = "") -> str:
        text = os.path.expandvars(os.path.expanduser(str(value)))
        return fill(
            text,
            {**self.tokens, "arch": arch or self.platform.arch, "os": self.platform.system.lower()},
        )

    def expand_all(self, values: Any) -> list[str]:
        if values in (None, ""):
            return []
        if isinstance(values, str):
            items = [v.strip() for v in values.split(",")]
        else:
            items = [str(v) for v in values]
        return [self.expand(v) for v in items if str(v).strip()]

    def expand_shell(self, value: str) -> str:
        """Shell code with only this program's ``{token}``s filled: every ``$VAR`` is the shell's.

        ``expand`` resolves ``$VAR`` from this process -- right for an argument
        vector, wrong for shell code: nvm's ``export NVM_DIR=…; . "$NVM_DIR/nvm.sh"``
        reached bash with ``$NVM_DIR`` already replaced by this process's value,
        overriding the snippet's own ``export``.
        """
        return fill(str(value), {**self.tokens, "arch": self.platform.arch, "os": self.platform.system.lower()})

    def failure_words(self, result: CommandResult) -> str:
        """Why a command failed, in its own words: a timeout says so (and how far a download got)."""
        text = f"{result.stdout or ''}\n{result.stderr or ''}"
        progress = self.settings.str_("install.progress_line_pattern", PROGRESS_LINE_PATTERN)
        if result.timed_out:
            where = progress_of(text)
            return f"timed out after {result.duration:.0f}s" + (f" while downloading ({where})" if where else "")
        said = last_words(result.stderr or "", progress=progress) or last_words(result.stdout or "", progress=progress)
        return f"rc={result.returncode}" + (f": {said}" if said else "")

    def _privileged(self, argv: list[str]) -> list[str]:
        """Prefix a command with sudo/doas when root is required and absent."""
        if self.platform.is_root:
            return argv
        prefix = self.platform.sudo_prefix()
        if not prefix:
            return argv
        return [*prefix, *argv]

    def _template(self, manager: str, name: str, default: list[str] | None = None) -> list[str]:
        value = self.commands.get(f"{manager}.{name}", default)
        if isinstance(value, str):
            return [value]
        return [str(v) for v in (value or [])]

    def _timeout(self, key: str, default: float) -> float:
        return self.settings.float_(f"install.{key}", default)

    def _install_template(self, manager: str, default: list[str] | None = None) -> list[str]:
        """The install command for ``manager``, in its forcing form when asked.

        Forcing is per-manager data: some take an extra flag (``apt --reinstall``),
        some need a different subcommand (``dnf reinstall``), and some force by
        *dropping* a flag (``pacman -S`` without ``--needed``).  ``install_force``
        replaces the command outright, ``force_flags`` is appended to it.
        """
        if not self.force:
            return self._template(manager, "install", default)
        replacement = self._template(manager, "install_force")
        if replacement:
            return replacement
        return [*self._template(manager, "install", default), *self._template(manager, "force_flags")]

    def _pinned(self, ecosystem: str, packages: list[str]) -> list[str]:
        """Apply the ``[install.pins.<ecosystem>]`` versions under --downgrade.

        A package already carrying an explicit version is left alone, and one
        with no pin resolves to its latest release as usual -- the table is a
        set of known-good floors, not an exhaustive lock file.
        """
        if not self.downgrade or not packages:
            return packages
        pins = self.settings.dict_(f"install.pins.{ecosystem}")
        if not pins:
            return packages
        separator = self.settings.str_(f"install.pin_separator.{ecosystem}", "==")
        resolved: list[str] = []
        for package in packages:
            version = pins.get(package)
            if version and not self._has_version(package, separator):
                resolved.append(f"{package}{separator}{version}")
                log.info("--downgrade: pinning %s to %s", package, version)
            else:
                resolved.append(package)
        return resolved

    @staticmethod
    def _has_version(package: str, separator: str) -> bool:
        """True when the caller already pinned the package themselves.

        An npm scope leads with ``@``, which is also npm's version separator, so
        the leading character never counts as one: ``@scope/name`` is unpinned,
        ``@scope/name@1.2.3`` is pinned.
        """
        body = package.lstrip("@")
        return separator in body or any(c in body for c in "<>=~!")

    def needs_privilege(self, spec: Mapping[str, Any]) -> bool:
        """Would this strategy run anything as root (or ask for authorisation)?"""
        kind = str(spec.get("kind", "")).strip().lower()
        if kind in self.ELEVATED_KINDS:
            return True
        if kind == "download":
            url = self.expand(str(spec.get("url", ""))).split("?")[0]
            return url.endswith(self.LOCAL_PACKAGE_SUFFIXES)
        return False

    # -- dispatch --------------------------------------------------------------

    def run(self, spec: Mapping[str, Any], component_key: str) -> InstallOutcome:
        kind = str(spec.get("kind", "")).strip().lower()
        platforms = [str(p) for p in spec.get("platforms", []) if p]
        if platforms and self.platform.system not in platforms:
            return InstallOutcome.skip(f"strategy '{kind}' is limited to {', '.join(platforms)}")

        handler: Callable[[Mapping[str, Any], str], InstallOutcome] | None = {
            "system": self._install_system,
            "brew": self._install_brew,
            "flatpak": self._install_flatpak,
            "snap": self._install_snap,
            "npm": self._install_npm,
            "pip": self._install_pip,
            "uv_tool": self._install_uv_tool,
            "script": self._install_script,
            "download": self._install_download,
            "command": self._install_command,
            "manual": self._install_manual,
        }.get(kind)
        if handler is None:
            return InstallOutcome.skip(f"unknown install kind '{kind}'")
        return handler(spec, component_key)

    # -- native package managers ----------------------------------------------

    def _refresh_index(self, manager: str) -> None:
        if manager in self._index_refreshed:
            return
        self._index_refreshed.add(manager)
        if not self.settings.bool_("install.refresh_package_index", True):
            return
        argv = self._template(manager, "refresh")
        if not argv:
            return
        binary = self.runner.which(argv[0])
        if not binary:
            return
        log.info("refreshing %s package index", manager)
        self.runner.run(
            self._privileged([binary, *argv[1:]]),
            timeout=self._timeout("package_timeout_seconds", 900),
            env=self.commands.dict_(f"{manager}.env"),
            label=f"{manager} refresh",
        )

    def _install_system(self, spec: Mapping[str, Any], component_key: str) -> InstallOutcome:
        manager = self.platform.package_manager
        if not manager:
            return InstallOutcome.skip("no native package manager detected")
        packages_by_manager = spec.get("packages", {})
        if not isinstance(packages_by_manager, Mapping):
            return InstallOutcome.skip("malformed 'packages' table")
        packages = self.expand_all(packages_by_manager.get(manager))
        if not packages:
            return InstallOutcome.skip(f"no {manager} packages listed for {component_key}")

        argv = self._install_template(manager)
        if not argv:
            return InstallOutcome.skip(f"no install command template for '{manager}'")
        binary = self.runner.which(argv[0])
        if not binary:
            return InstallOutcome.skip(f"{argv[0]} not found on PATH")
        if not self.platform.is_root and not self.platform.sudo_prefix():
            return InstallOutcome(
                ok=False,
                detail=f"{manager} install needs root and no sudo/doas is available",
                remediation=[
                    f"Run as root, or install manually: {' '.join([*argv, *packages])}",
                ],
            )

        self._refresh_index(manager)

        def install(names: list[str]) -> CommandResult:
            return self.runner.run(
                self._privileged([binary, *argv[1:], *names]),
                timeout=float(spec.get("timeout_seconds") or self._timeout("package_timeout_seconds", 900)),
                env=self.commands.dict_(f"{manager}.env"),
                label=f"{manager} install",
            )

        result = install(packages)
        if not result.ok and not result.simulated and self._no_such_package(manager, result):
            # The names in the catalog are one release's: npm22 on a machine
            # whose repositories carry npm26. What the system says provides the
            # commands, or the newest package the catalog's patterns match, is
            # tried instead -- once.
            others = self.resolve_packages(manager, spec, component_key)
            others = [p for p in others if p not in packages] or []
            if others:
                log.info(
                    "%s: %s has no %s; trying %s, which the system names for it",
                    component_key,
                    manager,
                    " ".join(packages),
                    " ".join(others),
                )
                retry = install(others)
                return self._outcome(
                    retry, f"{manager}: {' '.join(others)} (for {' '.join(packages)})", packages=others
                )
        return self._outcome(result, f"{manager}: {' '.join(packages)}", packages=packages)

    # -- asking the system what it has ---------------------------------------------

    def _no_such_package(self, manager: str, result: CommandResult) -> bool:
        """Did ``manager`` refuse because a listed package does not exist (``<mgr>.missing_markers``)?"""
        text = f"{result.stdout or ''}\n{result.stderr or ''}".lower()
        return any(m and m.lower() in text for m in self._template(manager, "missing_markers"))

    def _query(self, manager: str, name: str, fills: Mapping[str, str]) -> list[str]:
        """Package names a ``<mgr>.<name>`` query prints (``<mgr>.<name>_regex`` picks each); [] on failure."""
        template = self._template(manager, name)
        if not template or not self.runner.which(template[0]):
            return []
        argv = [fill(t, fills) for t in template]
        argv[0] = self.runner.which(argv[0]) or argv[0]
        result = self.runner.run(
            argv, timeout=self._timeout("version_timeout_seconds", 25), label=f"{manager} {name}", mutating=False
        )
        if not result.ok or result.simulated:
            return []
        pattern = str(self.commands.get(f"{manager}.{name}_regex", r"^(\S+)$") or r"^(\S+)$")
        found: list[str] = []
        for line in (result.stdout or "").splitlines():
            match = re.search(pattern, line.strip())
            if match:
                value = (match.group(1) if match.groups() else match.group(0)).strip()
                if value and value not in found:
                    found.append(value)
        return found

    def owning_packages(self, paths: list[str]) -> list[str]:
        """The installed packages the system's package manager says own ``paths`` (``<mgr>.owner``)."""
        manager = self.platform.package_manager
        out: list[str] = []
        for path in paths:
            for name in self._query(manager, "owner", {"path": path}) if path else []:
                if name not in out:
                    out.append(name)
        return out

    def providing_packages(self, capabilities: list[str]) -> list[str]:
        """The installed packages that provide ``capabilities`` (``<mgr>.provider``): npm -> npm26."""
        manager = self.platform.package_manager
        out: list[str] = []
        for capability in capabilities:
            for name in self._query(manager, "provider", {"capability": capability}) if capability else []:
                if name not in out:
                    out.append(name)
        return out

    def available_packages(self, patterns: list[str]) -> list[str]:
        """For each pattern, the newest package the repositories offer whose name matches it (``<mgr>.available``)."""
        manager = self.platform.package_manager
        out: list[str] = []
        for pattern in patterns:
            query = re.match(r"\^?([A-Za-z0-9_.+-]+)", str(pattern))
            if not query:
                continue
            names = [
                n
                for n in self._query(manager, "available", {"query": query.group(1).rstrip(".+")})
                if _matches(pattern, n)
            ]
            if names:
                best = max(names, key=_version_key)
                if best not in out:
                    out.append(best)
        return out

    def resolve_packages(
        self, manager: str, spec: Mapping[str, Any], component_key: str, capabilities: list[str] | None = None
    ) -> list[str]:
        """Package names for ``spec`` the system itself gives: installed providers, else the patterns' newest."""
        caps = [
            *(capabilities or []),
            *self.expand_all(
                (spec.get("capabilities") or {}).get(manager)
                if isinstance(spec.get("capabilities"), Mapping)
                else spec.get("capabilities")
            ),
        ]
        installed = self.providing_packages(caps) if caps else []
        if installed:
            return installed
        patterns = spec.get("package_patterns") or {}
        listed = patterns.get(manager) if isinstance(patterns, Mapping) else patterns
        return self.available_packages([str(p) for p in listed or []])

    def _install_brew(self, spec: Mapping[str, Any], component_key: str) -> InstallOutcome:
        brew = self.runner.which("brew")
        if not brew:
            return InstallOutcome.skip("Homebrew not installed")
        formulas = self.expand_all(spec.get("formulas"))
        casks = self.expand_all(spec.get("casks"))
        if casks and not self.platform.is_macos:
            # Homebrew casks are macOS-only; a Linuxbrew run would just fail.
            casks = []
        if not formulas and not casks:
            return InstallOutcome.skip("no brew formulas or casks listed")

        results: list[CommandResult] = []
        if formulas:
            argv = self._install_template("brew", ["brew", "install"])
            results.append(
                self.runner.run(
                    [brew, *argv[1:], *formulas],
                    timeout=self._timeout("package_timeout_seconds", 900),
                    label="brew install",
                )
            )
        if casks:
            argv = self._template("brew", "cask", ["brew", "install", "--cask"])
            results.append(
                self.runner.run(
                    [brew, *argv[1:], *casks],
                    timeout=self._timeout("package_timeout_seconds", 900),
                    label="brew cask",
                )
            )
        ok = all(r.ok for r in results)
        return InstallOutcome(
            ok=ok,
            changed=ok and not all(r.simulated for r in results),
            detail=f"brew: {' '.join(formulas + casks)}",
            data={"stderr": results[-1].stderr[-400:]} if not ok else {},
        )

    def _install_flatpak(self, spec: Mapping[str, Any], component_key: str) -> InstallOutcome:
        flatpak = self.runner.which("flatpak")
        if not flatpak:
            return InstallOutcome.skip("flatpak not installed")
        refs = self.expand_all(spec.get("refs"))
        if not refs:
            return InstallOutcome.skip("no flatpak ref configured")
        remote = str(spec.get("remote", self.commands.str_("flatpak.default_remote", "flathub")))
        remote_url = str(
            spec.get(
                "remote_url",
                self.commands.str_("flatpak.default_remote_url", "https://dl.flathub.org/repo/flathub.flatpakrepo"),
            )
        )
        if remote_url:
            add = self._template("flatpak", "remote_add", ["flatpak", "remote-add", "--if-not-exists"])
            self.runner.run(
                [flatpak, *add[1:], remote, remote_url],
                timeout=self._timeout("download_timeout_seconds", 600),
                label="flatpak remote-add",
            )
        argv = self._install_template("flatpak", ["flatpak", "install", "-y"])
        result = self.runner.run(
            [flatpak, *argv[1:], remote, *refs],
            timeout=self._timeout("package_timeout_seconds", 900),
            label="flatpak install",
        )
        return self._outcome(result, f"flatpak: {' '.join(refs)}")

    def _install_snap(self, spec: Mapping[str, Any], component_key: str) -> InstallOutcome:
        snap = self.runner.which("snap")
        if not snap:
            return InstallOutcome.skip("snapd not installed")
        packages = self.expand_all(spec.get("packages"))
        if not packages:
            return InstallOutcome.skip("no snap packages listed")
        argv = self._install_template("snap", ["snap", "install"])
        extra = ["--classic"] if spec.get("classic") else []
        result = self.runner.run(
            self._privileged([snap, *argv[1:], *extra, *packages]),
            timeout=self._timeout("package_timeout_seconds", 900),
            label="snap install",
        )
        return self._outcome(result, f"snap: {' '.join(packages)}")

    # -- language ecosystems ---------------------------------------------------

    def _install_npm(self, spec: Mapping[str, Any], component_key: str) -> InstallOutcome:
        npm = self.tokens.get("npm") or self.runner.which("npm")
        if not npm:
            return InstallOutcome.skip("npm not available")
        packages = self._pinned("npm", self.expand_all(spec.get("packages")))
        if not packages:
            return InstallOutcome.skip("no npm packages listed")
        argv = self._install_template("npm", ["npm", "install", "--global"])
        env: dict[str, str] = {}
        bin_dir = ""
        if not self.platform.is_root and not self.platform.sudo_prefix():
            # Global install without root: retarget the prefix into $HOME.
            prefix = Path(os.path.expanduser("~/.local"))
            env["npm_config_prefix"] = str(prefix)
            bin_dir = str(prefix / "bin")
            log.debug("npm global prefix redirected to %s (no root)", prefix)
        result = self.runner.run(
            [npm, *argv[1:], *packages],
            timeout=self._timeout("package_timeout_seconds", 900),
            env=env,
            label="npm install -g",
        )
        outcome = self._outcome(result, f"npm: {' '.join(packages)}")
        outcome.bin_dir = bin_dir
        return outcome

    def _python_install_argv(self, packages: list[str]) -> tuple[list[str], str]:
        """Choose between ``uv pip`` and ``python -m pip``; returns (argv, label)."""
        python = self.tokens.get("python") or "python3"
        env = self.python_environment(python)
        # --break-dependencies. Both front ends need telling; uv refuses an
        # externally-managed interpreter just as pip does.
        unmanage = env.blocked and self.settings.bool_("install.allow_break_system_packages", False)
        # --user is pip's alone -- 'uv pip install' has no equivalent -- so
        # asking for it is also asking for the slower front end.
        want_user = self.python_target == "user" and not env.in_venv
        if not want_user and self.settings.bool_("install.prefer_uv", True):
            uv = self.tokens.get("uv") or self.runner.which("uv")
            if uv:
                argv = self._template("uv", "pip_install", ["pip", "install", "--upgrade"])
                if self.force:
                    argv += self._template("uv", "force_flags")
                if unmanage:
                    argv += self._template("uv", "unmanaged_flags")
                return [uv, *argv, "--python", python, *packages], "uv pip install"
        argv = self._template("pip", "install", ["-m", "pip", "install", "--upgrade"])
        command = [python, *argv]
        if self.force:
            command += self._template("pip", "force_flags")
        if not env.in_venv:
            if unmanage:
                command += self._template("pip", "unmanaged_flags")
            command.append("--user")
        return [*command, *packages], "pip install"

    def python_environment(self, python: str) -> PythonEnvironment:
        """Ask an interpreter where it will let packages be installed.

        Cached per interpreter: the answer cannot change during a run, and every
        Python component would otherwise pay for the same subprocess.
        """
        cached = self._python_env.get(python)
        if cached is not None:
            return cached
        probe = self.runner.probe(
            [python, "-c", PYTHON_ENV_PROBE],
            timeout=self.settings.float_("install.python_probe_timeout_seconds", self.runner.default_timeout),
        )
        env = PythonEnvironment()
        try:
            payload = json.loads(probe.output.strip() or "{}")
            env = PythonEnvironment(
                in_venv=bool(payload.get("in_venv")),
                externally_managed=bool(payload.get("externally_managed")),
                prefix=str(payload.get("prefix", "")),
            )
        except (ValueError, TypeError):
            # An interpreter that cannot answer is treated as an ordinary one;
            # the install itself will say otherwise clearly enough.
            log.debug("could not probe %s: %r", python, probe.output[:200])
        self._python_env[python] = env
        return env

    def _install_pip(self, spec: Mapping[str, Any], component_key: str) -> InstallOutcome:
        packages = self._pinned("pip", self.expand_all(spec.get("packages")))
        if not packages:
            return InstallOutcome.skip("no python packages listed")
        python = self.tokens.get("python")
        if not python:
            return InstallOutcome.skip("no Python interpreter resolved yet")
        argv, label = self._python_install_argv(packages)
        result = self.runner.run(argv, timeout=self._timeout("package_timeout_seconds", 900), label=label)
        if not result.ok and looks_externally_managed(result):
            # Reached only when the venv fallback was overruled: install.python_target
            # =auto moves to a virtualenv before getting here. So the advice is about
            # the choice that was made, not about a default going wrong.
            return InstallOutcome(
                ok=False,
                detail=f"{python} is PEP 668 externally managed",
                remediation=[
                    "Let it use a virtualenv: --set install.python_target=auto (or =venv)",
                    "Or install into the system interpreter anyway: --break-dependencies",
                    "Or install the distro's own package for it, if there is one.",
                ],
                data={"python": python, "stderr": (result.stderr or "")[-400:]},
            )
        return self._outcome(result, f"{label}: {' '.join(packages)}", packages=packages)

    def _install_uv_tool(self, spec: Mapping[str, Any], component_key: str) -> InstallOutcome:
        uv = self.tokens.get("uv") or self.runner.which("uv")
        if not uv:
            return InstallOutcome.skip("uv not available")
        packages = self._pinned("pip", self.expand_all(spec.get("packages")))
        if not packages:
            return InstallOutcome.skip("no uv tools listed")
        argv = self._template("uv", "tool_install", ["tool", "install"])
        if self.force:
            argv = [*argv, *self._template("uv", "tool_force_flags")]
        result = self.runner.run(
            [uv, *argv, *packages],
            timeout=self._timeout("package_timeout_seconds", 900),
            label="uv tool install",
        )
        outcome = self._outcome(result, f"uv tool: {' '.join(packages)}")
        outcome.bin_dir = os.path.expanduser("~/.local/bin")
        return outcome

    # -- vendor scripts and downloads -----------------------------------------

    def _download(self, url: str, destination: Path, timeout: float) -> tuple[bool, str]:
        """Fetch ``url`` to ``destination`` using curl, wget, then Python."""
        curl = self.runner.which("curl")
        if curl:
            result = self.runner.run(
                [curl, "-fsSL", "--proto", "=https", "--tlsv1.2", "-o", str(destination), url],
                timeout=timeout,
                label="download",
            )
            if result.ok:
                return True, ""
            if not result.simulated:
                return False, f"curl rc={result.returncode}: {result.stderr[:200]}"
        wget = self.runner.which("wget")
        if wget:
            result = self.runner.run([wget, "-q", "-O", str(destination), url], timeout=timeout, label="download")
            if result.ok:
                return True, ""
            if not result.simulated:
                return False, f"wget rc={result.returncode}: {result.stderr[:200]}"
        if self.ctx.dry_run:
            return True, ""
        try:
            with self.ctx.http(timeout=timeout, max_bytes=512 * 1024 * 1024) as client:
                response = client.get(url)
            if not response.ok:
                return False, f"HTTP {response.status}"
            write_private(destination, response.text)
            return True, ""
        except Exception as exc:  # noqa: BLE001
            return False, f"{type(exc).__name__}: {exc}"

    def _install_script(self, spec: Mapping[str, Any], component_key: str) -> InstallOutcome:
        url = self.expand(str(spec.get("url", "")))
        if not url:
            return InstallOutcome.skip("no script URL configured")
        if not url.startswith("https://"):
            return InstallOutcome(
                ok=False,
                detail=f"refusing to run an installer over a non-HTTPS URL: {url}",
                remediation=["Point install.url at an https:// URL, or install manually."],
            )
        interpreter_name = self.expand(str(spec.get("interpreter", "bash")))
        interpreter = self.runner.which(interpreter_name) or interpreter_name
        args = self.expand_all(spec.get("args"))
        timeout = float(spec.get("timeout_seconds") or self._timeout("download_timeout_seconds", 600))

        with diskspace.temporary_directory("initialize-claude-ai-") as tmp:
            script_path = Path(tmp) / "install-script"
            ok, error = self._download(url, script_path, timeout)
            if not ok:
                return InstallOutcome(ok=False, detail=f"could not fetch {url}: {error}")
            if self.ctx.dry_run:
                log.info("[dry-run] would execute %s from %s", interpreter_name, url)
                return InstallOutcome(ok=True, detail=f"[dry-run] {interpreter_name} <- {url}")
            if not script_path.exists() or script_path.stat().st_size == 0:
                return InstallOutcome(ok=False, detail=f"downloaded an empty script from {url}")
            page = self._web_page(script_path)
            if page:
                # claude.ai/install.sh once answered with a web page, and bash
                # ran the HTML (rc=2). A page is not an installer.
                return InstallOutcome(
                    ok=False,
                    detail=f"{url} served a web page, not an installer ({page})",
                    remediation=[f"Open {url} to see what it serves instead of a script."],
                )

            result = self.runner.run(
                [interpreter, str(script_path), *args],
                timeout=timeout,
                label=f"install script ({component_key})",
            )
            if not result.ok:
                return InstallOutcome(
                    ok=False,
                    detail=f"installer {self.failure_words(result)}",
                    data={"stderr": result.stderr[-600:], "url": url},
                    remediation=[f"Run it manually to see the full output: {interpreter_name} <(curl -fsSL {url})"],
                )

        outcome = InstallOutcome(ok=True, changed=True, detail=f"ran vendor installer from {url}")
        bin_dir = self.expand(str(spec.get("adds_bin_dir", "")))
        if bin_dir:
            outcome.bin_dir = bin_dir

        post = self.expand_shell(str(spec.get("post_shell", "")))
        if post:
            shell = self.runner.which("bash") or "/bin/sh"
            follow_up = self.runner.run([shell, "-lc", post], timeout=timeout, label="post-install")
            if not follow_up.ok:
                # The step that puts the tool in place failed: nothing was
                # installed, whatever the script before it said. nvm's
                # `nvm install --lts` timed out, and node was reported CHANGED.
                return InstallOutcome(
                    ok=False,
                    detail=f"ran vendor installer from {url}, but its post-install step failed "
                    f"({self.failure_words(follow_up)})",
                    data={"stderr": follow_up.stderr[-600:], "url": url},
                    bin_dir=outcome.bin_dir,
                )
        return outcome

    def _web_page(self, path: Path) -> str:
        """The web-page marker a downloaded "script" starts with, or "" for a script."""
        try:
            head = path.read_bytes()[:2048].decode("utf-8", errors="replace").lstrip().lower()
        except OSError:
            return ""
        if head.startswith("#!"):
            return ""
        markers = self.settings.str_list("install.script_web_page_markers", list(WEB_PAGE_MARKERS))
        return next((m for m in markers if m and m.lower() in head[:1024]), "")

    def _install_download(self, spec: Mapping[str, Any], component_key: str) -> InstallOutcome:
        # A vendor's own spelling of the architecture (go.dev says amd64 where
        # the rest of the catalog says x64), for this strategy's {arch} only.
        arch_names = spec.get("arch_names")
        arch = str(arch_names.get(self.platform.arch, "")) if isinstance(arch_names, Mapping) else ""
        url = self.expand(str(spec.get("url", "")), arch=arch)
        if not url:
            return InstallOutcome.skip("no download URL configured")
        adds_bin_dir = self.expand(str(spec.get("adds_bin_dir", "")))
        install_dir = Path(self.expand(str(spec.get("install_dir", "~/.local/bin"))))
        binary_name = self.expand(str(spec.get("binary_name", component_key)))
        timeout = self._timeout("download_timeout_seconds", 600)

        if self.ctx.dry_run:
            return InstallOutcome(ok=True, detail=f"[dry-run] would download {url} -> {install_dir}")

        install_dir.mkdir(parents=True, exist_ok=True)
        with diskspace.temporary_directory("initialize-claude-ai-dl-") as tmp:
            suffix = Path(url.split("?")[0]).suffix
            archive = Path(tmp) / f"artifact{suffix}"
            ok, error = self._download(url, archive, timeout)
            if not ok:
                return InstallOutcome(ok=False, detail=f"download failed: {error}")

            target = install_dir / binary_name
            if suffix in (".tar", ".gz", ".tgz", ".xz", ".bz2") or ".tar." in url:
                with tarfile.open(archive) as tar:
                    tar.extractall(install_dir, filter="data")
                detail = f"extracted archive into {install_dir}"
            elif suffix == ".zip":
                with zipfile.ZipFile(archive) as zf:
                    zf.extractall(install_dir)
                detail = f"extracted archive into {install_dir}"
            elif suffix in self.LOCAL_PACKAGE_SUFFIXES:
                return self._install_local_package(archive, suffix)
            else:  # AppImage or a bare binary
                shutil.copy2(archive, target)
                target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
                detail = f"installed {target}"
        return InstallOutcome(ok=True, changed=True, detail=detail, bin_dir=adds_bin_dir or str(install_dir))

    def _install_local_package(self, path: Path, suffix: str) -> InstallOutcome:
        """Hand a downloaded .deb/.rpm to the native tooling."""
        if suffix == ".deb":
            candidates = [["apt-get", "install", "-y", str(path)], ["dpkg", "-i", str(path)]]
        else:
            candidates = [
                ["dnf", "install", "-y", str(path)],
                ["zypper", "--non-interactive", "install", str(path)],
                ["rpm", "-i", str(path)],
            ]
        for argv in candidates:
            binary = self.runner.which(argv[0])
            if not binary:
                continue
            result = self.runner.run(
                self._privileged([binary, *argv[1:]]),
                timeout=self._timeout("package_timeout_seconds", 900),
                label=f"{argv[0]} local package",
            )
            if result.ok:
                return InstallOutcome(ok=True, changed=True, detail=f"installed {path.name} with {argv[0]}")
        return InstallOutcome(ok=False, detail=f"no tool available to install {path.name}")

    # -- misc ------------------------------------------------------------------

    def _install_command(self, spec: Mapping[str, Any], component_key: str) -> InstallOutcome:
        argv = [self.expand(str(a)) for a in spec.get("argv", []) if str(a).strip()]
        shell_code = self.expand_shell(str(spec.get("shell", "") or ""))
        if not argv and shell_code:
            argv = [self.runner.which("bash") or "/bin/sh", "-lc", shell_code]
        if not argv:
            return InstallOutcome.skip("no argv configured")
        if any("{" in token and "}" in token for token in argv):
            return InstallOutcome.skip(f"unresolved template in command: {' '.join(argv)}")
        result = self.runner.run(
            argv,
            timeout=float(spec.get("timeout_seconds") or self._timeout("package_timeout_seconds", 900)),
            label=f"install command ({component_key})",
        )
        return self._outcome(result, " ".join(argv[:3]))

    def _install_manual(self, spec: Mapping[str, Any], component_key: str) -> InstallOutcome:
        instructions = [self.expand(str(line)) for line in spec.get("instructions", [])]
        return InstallOutcome(
            ok=False,
            detail="manual installation required",
            remediation=instructions,
            data={"manual": True},
        )

    # -- shared ----------------------------------------------------------------

    def _outcome(self, result: CommandResult, detail: str, **data: Any) -> InstallOutcome:
        if result.ok:
            return InstallOutcome(ok=True, changed=not result.simulated, detail=detail, data=dict(data))
        return InstallOutcome(
            ok=False,
            detail=f"{detail} ({self.failure_words(result)})",
            data={**data, "stderr": result.stderr[-600:]},
        )


def _matches(pattern: str, name: str) -> bool:
    try:
        return re.fullmatch(pattern, name) is not None
    except re.error:
        return False


def _version_key(name: str) -> tuple:
    """Order package names by the numbers in them: nodejs26 after nodejs24 after nodejs8."""
    return tuple(int(n) for n in re.findall(r"\d+", name)) or (0,)


@dataclass
class StrategyRun:
    """What trying a component's strategies came to."""

    installed: bool = False
    #: ``"<kind>: <what happened>"`` for every strategy looked at, in order.
    attempts: list[str] = field(default_factory=list)
    remediation: list[str] = field(default_factory=list)
    #: ``"<kind>: <its own words>"`` for every strategy that ran and failed -- the reason to show.
    reasons: list[str] = field(default_factory=list)


def run_strategies(
    installer: Installer,
    component: Any,
    *,
    on_bin_dir: Callable[[str], None],
    strategies: Any = None,
    logger: Any = log,
) -> StrategyRun:
    """Try ``component``'s install strategies in order until one succeeds.

    The one loop both installers share: phase 1 working through the catalog,
    and phase 4 installing a toolchain a candidate turned out to need
    (:mod:`.on_demand`). ``strategies`` narrows the list without reordering it.
    """
    run = StrategyRun()
    for strategy in component.install if strategies is None else strategies:
        outcome = installer.run(strategy, component.key)
        kind = str(strategy.get("kind", "?"))
        if outcome.skipped:
            run.attempts.append(f"{kind}: {outcome.detail}")
            logger.debug("%s strategy %s skipped: %s", component.key, kind, outcome.detail)
            continue
        if not outcome.ok:
            # Every handler's failure carries what the tool itself said, whether
            # or not the handler put it in its detail ("brew: node" said nothing).
            said = last_words(
                str((outcome.data or {}).get("stderr") or ""),
                progress=installer.settings.str_("install.progress_line_pattern", PROGRESS_LINE_PATTERN),
            )
            if said and said not in outcome.detail:
                outcome.detail = f"{outcome.detail}: {said}"
        run.attempts.append(f"{kind}: {'ok' if outcome.ok else outcome.detail}")
        if not outcome.ok:
            run.reasons.append(f"{kind}: {outcome.detail}")
        run.remediation.extend(outcome.remediation)
        if outcome.bin_dir:
            on_bin_dir(outcome.bin_dir)
        if outcome.ok:
            run.installed = True
            break
    return run
