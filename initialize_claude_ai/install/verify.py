"""Verification checks for installed components.

"Installed" is never assumed from a package manager's exit code -- every
component is proved by locating its artefact and, where possible, executing it.
Four check kinds cover everything in the catalog:

``binary``         locate an executable and run its version flag
``python_module``  import the module in the target interpreter
``node_package``   resolve the package in the global npm root
``file``           the artefact is a file/bundle, not an executable
``command``        run an arbitrary read-only command and match its output
``agent_cli``      which Claude Code the agent SDK runs, and whether it is behind

Every check has a ``purpose``. ``present`` (the default) decides whether the
component is there -- failing, it is installed. ``usable`` runs once it is
there and asks whether it works here: Docker with its daemon stopped is
installed and useless, and reinstalling it changes nothing. A usable check that
fails makes the component PARTIAL with the fix -- ``remedies`` maps a phrase in
the check's output to the sentence that fixes it, ``remedy`` is the fallback --
and is never a reason to install. Phase 4 asks the same checks before it
launches a server with that component (:func:`usable`).
"""

from __future__ import annotations

import glob
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from ..core.config import fill
from ..core.context import RunContext
from ..core.logging_setup import get_logger

log = get_logger("verify")

_VERSION_FALLBACK = re.compile(r"([0-9]+(?:\.[0-9]+){1,3})")

#: A check's ``purpose``.
PRESENT, USABLE = "present", "usable"


def purpose_of(spec: Mapping[str, Any]) -> str:
    return USABLE if str(spec.get("purpose", PRESENT)).strip().lower() == USABLE else PRESENT


def checks_for(specs: Any, purpose: str) -> list[Any]:
    """The verify specs (a component's ``verify`` list) that serve ``purpose``.

    Only a spec that says ``purpose = "usable"`` is one; everything else is a
    presence check, as every check was before there were two.
    """
    usable_ones = [spec for spec in specs or () if isinstance(spec, Mapping) and purpose_of(spec) == USABLE]
    if purpose == USABLE:
        return usable_ones
    return [spec for spec in specs or () if not any(spec is u for u in usable_ones)]


def remedy_for(spec: Mapping[str, Any], text: str) -> str:
    """The sentence that fixes what ``text`` (a failed check's words) says; the spec's ``remedy`` otherwise."""
    lowered = str(text or "").lower()
    for marker, sentence in (spec.get("remedies") or {}).items():
        if str(marker).strip() and str(marker).lower() in lowered:
            return str(sentence).strip()
    return str(spec.get("remedy") or "").strip()


@dataclass
class CheckResult:
    kind: str
    ok: bool
    detail: str = ""
    path: str = ""
    version: str = ""
    optional: bool = False
    data: dict[str, Any] = field(default_factory=dict)

    @property
    def blocking(self) -> bool:
        return not self.ok and not self.optional


def parse_version(text: str, pattern: str = "") -> str:
    """Pull a version out of a tool's ``--version`` output."""
    if not text:
        return ""
    if pattern:
        match = re.search(pattern, text)
        if match:
            return match.group(1) if match.groups() else match.group(0)
    match = _VERSION_FALLBACK.search(text)
    return match.group(1) if match else ""


def version_tuple(version: str) -> tuple[int, ...]:
    parts: list[int] = []
    for chunk in re.split(r"[.\-+]", version):
        if chunk.isdigit():
            parts.append(int(chunk))
        else:
            digits = re.match(r"(\d+)", chunk)
            if digits:
                parts.append(int(digits.group(1)))
            break
    return tuple(parts) or (0,)


def version_at_least(found: str, minimum: str) -> bool:
    if not minimum:
        return True
    if not found:
        return False
    return version_tuple(found) >= version_tuple(minimum)


class Verifier:
    """Runs the ``[[components.verify]]`` checks for a component."""

    def __init__(self, ctx: RunContext) -> None:
        self.ctx = ctx
        self.runner = ctx.runner
        #: Substitutions available in command templates, e.g. ``{python}``.
        self.tokens: dict[str, str] = {}

    def _timeout(self, key: str) -> float:
        """``install.<key>``, or the run's per-command default when unset."""
        return self.ctx.settings.float_(f"install.{key}", self.runner.default_timeout)

    # -- template plumbing -----------------------------------------------------

    def set_token(self, name: str, value: str) -> None:
        if value:
            self.tokens[name] = value

    def expand(self, argv: list[str]) -> list[str]:
        out: list[str] = []
        for raw in argv:
            out.append(fill(os.path.expandvars(os.path.expanduser(str(raw))), self.tokens))
        return out

    def has_unresolved(self, argv: list[str]) -> bool:
        return any(re.search(r"\{[a-z_]+\}", token) for token in argv)

    # -- dispatch --------------------------------------------------------------

    def check(self, spec: Mapping[str, Any]) -> CheckResult:
        kind = str(spec.get("kind", "binary")).strip().lower()
        handler = {
            "binary": self._check_binary,
            "python_module": self._check_python_module,
            "node_package": self._check_node_package,
            "file": self._check_file,
            "command": self._check_command,
            "agent_cli": self._check_agent_cli,
        }.get(kind)
        if handler is None:
            return CheckResult(kind=kind, ok=False, detail=f"unknown verify kind '{kind}'")
        try:
            result = handler(spec)
            if not result.ok and purpose_of(spec) == USABLE:
                result.data["remedy"] = remedy_for(spec, f"{result.detail}\n{result.data.get('output', '')}")
            return result
        except Exception as exc:  # noqa: BLE001 - a broken check must not abort the phase
            log.debug("verify %s raised: %s", kind, exc)
            return CheckResult(
                kind=kind,
                ok=False,
                detail=f"check raised {type(exc).__name__}: {exc}",
                optional=bool(spec.get("optional", False)),
            )

    # -- individual checks -----------------------------------------------------

    def _check_binary(self, spec: Mapping[str, Any]) -> CheckResult:
        optional = bool(spec.get("optional", False))
        names = [str(n) for n in spec.get("names", []) if n]
        extra_dirs = self.ctx.settings.str_list("paths.extra_bin_dirs", [])
        extra_dirs = [os.path.expandvars(os.path.expanduser(d)) for d in extra_dirs]

        found_path = ""
        found_name = ""
        for name in names:
            path = self.runner.which(name, extra_dirs=extra_dirs)
            if path:
                found_path, found_name = path, name
                break
        if not found_path:
            return CheckResult(
                kind="binary",
                ok=False,
                detail=f"not found on PATH: {', '.join(names)}",
                optional=optional,
            )

        version_args = [str(a) for a in spec.get("version_args", ["--version"])]
        version = ""
        functional = True
        detail = found_path
        if version_args:
            probe = self.runner.probe([found_path, *version_args], timeout=self._timeout("version_timeout_seconds"))
            # Some tools exit non-zero on --version but still print it.
            version = parse_version(probe.output, str(spec.get("version_regex", "")))
            if not probe.ok and not version:
                functional = False
                detail = f"{found_path} (exec failed: rc={probe.returncode})"
            elif version:
                detail = f"{found_path} ({version})"

        minimum = str(spec.get("min_version", ""))
        if functional and minimum and not version_at_least(version, minimum):
            return CheckResult(
                kind="binary",
                ok=False,
                detail=f"{found_path} has version {version or 'unknown'}, need >= {minimum}",
                path=found_path,
                version=version,
                optional=optional,
                data={"name": found_name, "min_version": minimum},
            )

        return CheckResult(
            kind="binary",
            ok=functional,
            detail=detail,
            path=found_path,
            version=version,
            optional=optional,
            data={"name": found_name, "env_key": spec.get("env_key", "")},
        )

    def _check_python_module(self, spec: Mapping[str, Any]) -> CheckResult:
        optional = bool(spec.get("optional", False))
        modules = [str(m) for m in spec.get("modules", []) if m]
        python = (
            self.tokens.get("python") or self.ctx.settings.str_("install.python_executable", "python3") or "python3"
        )
        if not modules:
            return CheckResult(kind="python_module", ok=False, detail="no modules listed", optional=optional)

        distribution = str(spec.get("distribution", "")) or modules[0].replace("_", "-")
        # Import and read the installed distribution version in one shot.
        script = (
            "import importlib, json, sys\n"
            f"mods = {modules!r}\n"
            f"dist = {distribution!r}\n"
            "out = {'ok': True, 'missing': [], 'version': ''}\n"
            "for m in mods:\n"
            "    try:\n"
            "        importlib.import_module(m)\n"
            "    except Exception as exc:\n"
            "        out['ok'] = False\n"
            "        out['missing'].append('%s: %s' % (m, type(exc).__name__))\n"
            "try:\n"
            "    from importlib.metadata import version\n"
            "    out['version'] = version(dist)\n"
            "except Exception:\n"
            "    pass\n"
            "print(json.dumps(out))\n"
        )
        probe = self.runner.probe([python, "-c", script], timeout=self._timeout("python_import_timeout_seconds"))
        try:
            payload = json.loads(probe.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            return CheckResult(
                kind="python_module",
                ok=False,
                detail=f"import check failed for {', '.join(modules)}",
                optional=optional,
                data={"stderr": probe.stderr[-400:]},
            )
        ok = bool(payload.get("ok"))
        version = str(payload.get("version", ""))
        detail = (
            f"{distribution} {version} in {python}".strip()
            if ok
            else f"cannot import {', '.join(payload.get('missing', modules))}"
        )
        return CheckResult(
            kind="python_module",
            ok=ok,
            detail=detail,
            path=python,
            version=version,
            optional=optional,
            data={"interpreter": python, "distribution": distribution},
        )

    def _check_node_package(self, spec: Mapping[str, Any]) -> CheckResult:
        optional = bool(spec.get("optional", False))
        packages = [str(p) for p in spec.get("packages", []) if p]
        any_of = bool(spec.get("any", False))
        npm = self.tokens.get("npm") or self.runner.which("npm")
        if not npm:
            return CheckResult(kind="node_package", ok=False, detail="npm not available", optional=optional)
        commands = self.ctx.settings.section("install.commands")
        query = commands.str_list("npm.query", ["npm", "ls", "--global", "--depth", "0", "--json"])
        if not query:
            return CheckResult(
                kind="node_package", ok=False, detail="install.commands.npm.query is empty", optional=optional
            )
        probe = self.runner.probe([npm, *query[1:]], timeout=commands.float_("npm.query_timeout_seconds", 90.0))
        installed: dict[str, Any] = {}
        try:
            installed = (json.loads(probe.stdout or "{}") or {}).get("dependencies", {}) or {}
        except ValueError:
            pass
        present = {name: installed[name].get("version", "") for name in packages if name in installed}
        ok = bool(present) if any_of else len(present) == len(packages)
        missing = [p for p in packages if p not in present]
        detail = ", ".join(f"{k}@{v}" for k, v in present.items()) if ok else f"missing: {', '.join(missing)}"
        return CheckResult(
            kind="node_package",
            ok=ok,
            detail=detail,
            version=next(iter(present.values()), ""),
            optional=optional,
            data={"present": present, "missing": missing},
        )

    def _check_file(self, spec: Mapping[str, Any]) -> CheckResult:
        optional = bool(spec.get("optional", False))
        raw_paths = [str(p) for p in spec.get("paths", []) if p]
        any_of = bool(spec.get("any", True))
        found: list[str] = []
        for raw in raw_paths:
            expanded = os.path.expandvars(os.path.expanduser(raw))
            if any(ch in expanded for ch in "*?["):
                found.extend(sorted(glob.glob(expanded)))
            elif Path(expanded).exists():
                found.append(expanded)
        ok = bool(found) if any_of else len(found) == len(raw_paths)
        detail = found[0] if found else f"none of {len(raw_paths)} paths present"
        return CheckResult(
            kind="file",
            ok=ok,
            detail=detail,
            path=found[0] if found else "",
            optional=optional,
            data={"found": found[:5]},
        )

    def _check_command(self, spec: Mapping[str, Any]) -> CheckResult:
        optional = bool(spec.get("optional", False))
        argv = self.expand([str(a) for a in spec.get("argv", []) if a])
        if not argv:
            return CheckResult(kind="command", ok=False, detail="no argv", optional=optional)
        if self.has_unresolved(argv):
            return CheckResult(
                kind="command",
                ok=False,
                detail=f"prerequisite not resolved: {' '.join(argv)}",
                optional=True,  # a dangling {token} means an earlier component failed
            )
        timeout = float(spec.get("timeout_seconds", 60))
        probe = self.runner.probe(argv, timeout=timeout)
        expected = str(spec.get("expect_substring", ""))
        ok = probe.ok and (not expected or expected.lower() in probe.output.lower())
        detail = probe.first_line()[:200] if ok else f"rc={probe.returncode} {probe.stderr[:160]}"
        return CheckResult(
            kind="command",
            ok=ok,
            detail=detail,
            version=parse_version(probe.output, str(spec.get("version_regex", ""))),
            optional=optional,
            # Kept for remedies to read (never reported): "Cannot connect to
            # the Docker daemon" may be on either stream, past 160 characters.
            data={"argv": argv, "output": (probe.output or "")[-4000:]},
        )

    def _check_agent_cli(self, spec: Mapping[str, Any]) -> CheckResult:
        """Which Claude Code the agent SDK will run, asked of the interpreter it is installed in.

        The answer phase 4 acts on (:func:`..mcp_auto_discovery.runtime.sdk_cli`),
        so phase 1 reports what will happen, not a guess at it. Not ok when the
        copy that runs is older than one this machine has.
        """
        from ..mcp_auto_discovery.runtime import sdk_cli  # the MCP layer sits above this one

        optional = bool(spec.get("optional", False))
        python = self.tokens.get("python") or self.ctx.settings.str_("install.python_executable", "python3")
        choice = sdk_cli(self.ctx, python=python)
        if not choice.runs:
            return CheckResult(
                kind="agent_cli",
                ok=False,
                optional=optional,
                detail="the SDK has no Claude Code to run: neither bundled nor installed",
            )
        behind = choice.behind
        detail = behind or f"the SDK runs {choice.runs}" + (f" ({choice.runs_version})" if choice.runs_version else "")
        return CheckResult(
            kind="agent_cli",
            ok=not behind,
            detail=detail,
            path=choice.runs,
            version=choice.runs_version,
            optional=optional,
            data={"mode": choice.mode, "why": choice.why, "output": behind},
        )


def usable(ctx: RunContext, component: Any, verifier: "Verifier | None" = None) -> tuple[bool, str]:
    """Do ``component``'s usable checks pass here? ``(ok, "<what failed> -- <the fix>")``, asked once per run.

    Shared by phase 1, which reports it, and phase 4, which does not launch a
    server with a component that cannot work (``not launched``, never "broken").
    """
    specs = checks_for(getattr(component, "verify", ()), USABLE)
    if not specs:
        return True, ""
    cache = ctx.state.setdefault("install_usable", {})
    key = getattr(component, "key", "") or repr(component)
    if key in cache:
        return cache[key]
    checker = verifier or Verifier(ctx)
    answer = (True, "")
    for spec in specs:
        result = checker.check(spec)
        if not result.ok:
            remedy = str(result.data.get("remedy") or "")
            answer = (False, f"{result.detail}" + (f" -- {remedy}" if remedy else ""))
            break
    cache[key] = answer
    return answer
