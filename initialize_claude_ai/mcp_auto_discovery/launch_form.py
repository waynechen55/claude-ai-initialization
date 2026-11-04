"""A launcher is not the program, and installed is not runnable.

bugzilla-opensuse-org's agent installed ``mcp-bugzilla`` with ``uv tool install``
and the entry was written to run ``/root/.local/share/uv/tools/mcp-bugzilla/bin/mcp-bugzilla``
-- a 329-byte script whose first line runs that tool's private interpreter
(``.../uv/tools/mcp-bugzilla/bin/python``), where the package lives. Copied to
another machine it could never start: first "no such file" (ENOENT, the
interpreter is missing), then, pointed at the system's Python, "No module named
mcp_bugzilla" (Connection closed). The same run wrote bugzilla-example-com as
``uvx mcp-bugzilla==0.21.0``, which fetches the package wherever it starts.

Two questions, asked everywhere a launch is judged:

* :func:`private_launcher` -- is this command an installer's private launcher
  (a ``uv tool`` or pipx environment, an npm global module), and of which
  package and version? :func:`launcher_form` writes it the portable way
  (``uvx``, ``npx -y``), launching the same package anywhere.
* :func:`runnable` -- can this file run here: it exists, is executable, the
  interpreter its first line names exists, and a Python launcher's interpreter
  imports the module it starts. "The file is there" is not that.

The installer layouts and launcher forms are configuration
(``[mcp.launch_form]``); nothing names a product.
"""

from __future__ import annotations

import glob
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Mapping, Sequence

from ..core.logging_setup import get_logger

log = get_logger("mcp.launch_form")

#: Fallback for ``[mcp.launch_form.families]``, equal to the packaged table. Each
#: family: ``patterns`` (regexes over a launcher's path, its real path or the
#: interpreter its first line names, with a ``package`` group), ``launcher`` and
#: ``short`` (the portable launch: ``{spec}`` the package with its version pin,
#: ``{script}`` the command's own name; ``short`` when the two are one name),
#: ``pin`` (how a version is written), ``metadata`` (``python`` or ``node``),
#: ``installers`` (the commands whose install steps the portable form makes unneeded).
FAMILIES: dict[str, dict[str, Any]] = {
    "uv-tool": {
        "patterns": [r"/uv/tools/(?P<package>[^/]+)/bin/[^/]+$"],
        "launcher": ["uvx", "--from", "{spec}", "{script}"],
        "short": ["uvx", "{spec}"],
        "pin": "=={version}",
        "metadata": "python",
        "installers": ["uv", "uvx"],
    },
    "pipx": {
        "patterns": [r"/pipx/venvs/(?P<package>[^/]+)/bin/[^/]+$"],
        "launcher": ["uvx", "--from", "{spec}", "{script}"],
        "short": ["uvx", "{spec}"],
        "pin": "=={version}",
        "metadata": "python",
        "installers": ["pipx"],
    },
    "npm-global": {
        "patterns": [r"/lib/node_modules/(?P<package>(?:@[^/]+/)?[^/]+)/"],
        "launcher": ["npx", "-y", "--package", "{spec}", "{script}"],
        "short": ["npx", "-y", "{spec}"],
        "pin": "@{version}",
        "metadata": "node",
        "installers": ["npm", "pnpm", "yarn"],
    },
}

#: A console-script launcher's import line: ``from mcp_bugzilla import main``.
_IMPORT = re.compile(r"^\s*(?:from\s+([A-Za-z_][\w.]*)\s+import|import\s+([A-Za-z_][\w.]*))", re.MULTILINE)


def config(settings: Any) -> Any:
    return settings.section("mcp.launch_form") if settings is not None else None


def families(settings: Any) -> dict[str, dict[str, Any]]:
    cfg = config(settings)
    table = cfg.dict_("families", FAMILIES) if cfg is not None else FAMILIES
    return {str(k): dict(v or {}) for k, v in (table or FAMILIES).items()}


def shebang(path: str) -> list[str]:
    """The interpreter (and its arguments) a script's first line names; [] for anything else."""
    try:
        with open(path, "rb") as handle:
            first = handle.readline(512)
    except OSError:
        return []
    if not first.startswith(b"#!"):
        return []
    return first[2:].decode("utf-8", "replace").strip().split()


def private_launcher(argv0: str, settings: Any = None) -> dict[str, str]:
    """``{family, package, version, script, root}`` when ``argv0`` is an installer's private launcher; {} otherwise.

    Read from the path as written (it need not exist here: an entry copied from
    another machine names that machine's path), its real path, and the
    interpreter its first line names (a launcher copied elsewhere still names
    the environment it belongs to).
    """
    text = str(argv0 or "").strip()
    if not text or "${" in text:
        return {}
    places = [text]
    if os.path.exists(text):
        real = os.path.realpath(text)
        if real != text:
            places.append(real)
        interpreter = shebang(real)
        if interpreter:
            places.append(interpreter[0])
    script = os.path.basename(text)
    for family, spec in families(settings).items():
        for place in places:
            for pattern in spec.get("patterns") or []:
                try:
                    found = re.search(str(pattern), place)
                except re.error:
                    continue
                if not found or "package" not in found.groupdict():
                    continue
                package = found.group("package")
                root = place[: found.end("package")]
                return {
                    "family": family,
                    "package": package,
                    "script": script,
                    "root": root,
                    "version": installed_version(root, package, str(spec.get("metadata") or "")),
                }
    return {}


def installed_version(root: str, package: str, kind: str) -> str:
    """The version installed under ``root`` (a tool environment, a module folder); "" when it cannot be read."""
    if not root or not os.path.isdir(root):
        return ""
    if kind == "node":
        try:
            return str(json.loads(Path(root, "package.json").read_text(encoding="utf-8")).get("version") or "")
        except (OSError, ValueError):
            return ""
    if kind == "python":
        wanted = re.sub(r"[-_.]+", "-", package).lower()
        for meta in glob.glob(os.path.join(root, "lib", "python*", "site-packages", "*.dist-info", "METADATA")):
            name = version = ""
            try:
                for line in Path(meta).read_text(encoding="utf-8", errors="replace").splitlines()[:40]:
                    if line.startswith("Name:"):
                        name = line.split(":", 1)[1].strip()
                    elif line.startswith("Version:"):
                        version = line.split(":", 1)[1].strip()
            except OSError:
                continue
            if re.sub(r"[-_.]+", "-", name).lower() == wanted and version:
                return version
    return ""


def pinned_by_install(package: str, install: Sequence[Sequence[str]], pin: str) -> str:
    """The version an install step pins for ``package`` (``mcp-bugzilla==0.21.0``, ``pkg@1.2.3``); "" when none."""
    sep = "==" if "==" in pin else "@"
    for step in install or []:
        for token in step or []:
            token = str(token)
            if token.startswith(package + sep):
                return token[len(package) + len(sep) :]
    return ""


def launcher_form(
    argv: Sequence[str], settings: Any = None, install: Sequence[Sequence[str]] = ()
) -> tuple[list[str], dict[str, str]]:
    """``(the portable launch, what it was)`` for an installer's private launcher; ``([], {})`` for anything else."""
    if not argv:
        return [], {}
    cfg = config(settings)
    if cfg is not None and not cfg.bool_("enabled", True):
        return [], {}
    found = private_launcher(str(argv[0]), settings)
    if not found:
        return [], {}
    spec = families(settings)[found["family"]]
    pin = str(spec.get("pin") or "")
    version = found.get("version") or pinned_by_install(found["package"], install, pin)
    found["version"] = version
    package_spec = found["package"] + (pin.replace("{version}", version) if version and pin else "")
    short_name = found["package"].rsplit("/", 1)[-1]
    template = spec.get("short") if found["script"] == short_name and spec.get("short") else spec.get("launcher")
    head = [str(t).replace("{spec}", package_spec).replace("{script}", found["script"]) for t in (template or [])]
    if not head:
        return [], {}
    return [*head, *[str(a) for a in argv[1:]]], found


def without_installer_steps(
    install: Sequence[Sequence[str]], found: Mapping[str, str], settings: Any = None
) -> list[list[str]]:
    """``install`` without the steps that installed the private launcher -- the portable form fetches it itself."""
    spec = families(settings).get(str(found.get("family") or ""), {})
    installers = {str(i) for i in spec.get("installers") or []}
    package = str(found.get("package") or "")
    kept: list[list[str]] = []
    for step in install or []:
        tokens = [str(t) for t in step or []]
        if (
            tokens
            and os.path.basename(tokens[0]) in installers
            and any(t == package or t.startswith(package + "=") or t.startswith(package + "@") for t in tokens)
        ):
            continue
        kept.append(tokens)
    return kept


def runnable(path: str, settings: Any = None, *, which: Any = None) -> str:
    """Why ``path`` cannot run here, in words; "" when it can.

    It exists and is executable; the interpreter its first line names exists
    (``/usr/bin/env NAME`` is looked up on PATH); and a Python launcher's
    interpreter imports the module it starts -- asked without starting it
    (``mcp.launch_form.import_check``).
    """
    text = os.path.expanduser(str(path or "").strip())
    if not text:
        return "no command"
    if not os.path.isfile(text):
        return f"{text} is not there"
    if not os.access(text, os.X_OK):
        return f"{text} is not executable"
    interpreter = shebang(text)
    if not interpreter:
        return ""
    program = interpreter[0]
    if os.path.basename(program) == "env" and len(interpreter) > 1:
        args = [a for a in interpreter[1:] if not a.startswith("-")]
        found = (which or shutil.which)(args[0]) if args else None
        if not found:
            return (
                f"its first line runs {' '.join(interpreter)}, and {args[0] if args else 'nothing'} is not on PATH here"
            )
        program = str(found)
    elif not os.path.exists(program):
        return f"its first line runs {program}, which is not on this machine -- a launcher of another machine's install"
    cfg = config(settings)
    if cfg is not None and not cfg.bool_("import_check", True):
        return ""
    if not re.search(r"python", os.path.basename(program)):
        return ""
    try:
        source = Path(text).read_text(encoding="utf-8", errors="replace")[:4000]
    except OSError:
        return ""
    import sys

    stdlib = set(getattr(sys, "stdlib_module_names", ()))
    modules = [(m.group(1) or m.group(2) or "").split(".")[0] for m in _IMPORT.finditer(source)]
    # The module the launcher exists to start: its first import the standard
    # library does not provide (`import sys` comes first in a console script).
    module = next((m for m in modules if m and m not in stdlib), "")
    if not module:
        return ""
    timeout = cfg.float_("import_check_timeout_seconds", 15.0) if cfg is not None else 15.0
    try:
        done = subprocess.run(
            [program, "-c", f"import importlib.util,sys; sys.exit(0 if importlib.util.find_spec({module!r}) else 3)"],  # noqa: S603
            capture_output=True,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if done.returncode == 3:
        return (
            f"{program} cannot import {module}, the module this launcher starts -- the package is not installed for it"
        )
    return ""


__all__ = [
    "FAMILIES",
    "families",
    "installed_version",
    "launcher_form",
    "pinned_by_install",
    "private_launcher",
    "runnable",
    "shebang",
    "without_installer_steps",
]
