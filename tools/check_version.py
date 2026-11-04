#!/usr/bin/env python3
"""One version, everywhere it is written.

The version lives in ``initialize_claude_ai/__init__.py`` (``__version__``);
``pyproject.toml`` reads it from there. This check fails when anything else
disagrees:

* the version is SemVer (``MAJOR.MINOR.PATCH`` with an optional pre-release);
* ``pyproject.toml`` takes the version dynamically and writes none of its own;
* ``CHANGELOG.md`` has an ``[Unreleased]`` section and a ``[VERSION]`` section,
  each with its compare/tag link at the bottom;
* the generated bridge's fallback ``SERVER_VERSION`` equals it;
* with ``--tag vX.Y.Z`` (the release workflow): the tag names this version.

``--notes`` prints the changelog section for the version: the release notes.
``--print`` prints the version.
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SEMVER = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-[0-9A-Za-z.-]+)?$")


def package_version(root: Path = ROOT) -> str:
    tree = ast.parse((root / "initialize_claude_ai" / "__init__.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(getattr(t, "id", "") == "__version__" for t in node.targets):
            return str(ast.literal_eval(node.value))
    raise SystemExit("initialize_claude_ai/__init__.py defines no __version__")


def changelog_section(version: str, root: Path = ROOT) -> str:
    """The body of ``## [version]`` in CHANGELOG.md; "" when there is none."""
    text = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    found = re.search(rf"^## \[{re.escape(version)}\][^\n]*\n(.*?)(?=^## \[|^\[[^\]]+\]: |\Z)", text, re.M | re.S)
    return found.group(1).strip() if found else ""


def problems(root: Path = ROOT, tag: str = "") -> list[str]:
    found: list[str] = []
    version = package_version(root)
    if not SEMVER.match(version):
        found.append(f"__version__ {version!r} is not SemVer (MAJOR.MINOR.PATCH)")
    pyproject = (root / "pyproject.toml").read_text(encoding="utf-8")
    project = pyproject.split("[project]", 1)[-1].split("\n[", 1)[0]
    if re.search(r"^version\s*=", project, re.M):
        found.append('pyproject.toml writes its own version; it must read __version__ (dynamic = ["version"])')
    if 'dynamic = ["version"]' not in project:
        found.append('pyproject.toml [project] does not declare dynamic = ["version"]')
    changelog = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    for heading in ("Unreleased", version):
        if not re.search(rf"^## \[{re.escape(heading)}\]", changelog, re.M):
            found.append(f"CHANGELOG.md has no '## [{heading}]' section")
        if not re.search(rf"^\[{re.escape(heading)}\]: https://", changelog, re.M):
            found.append(f"CHANGELOG.md has no link for [{heading}] at the bottom")
    if not changelog_section(version, root):
        found.append(f"CHANGELOG.md's [{version}] section is empty: it is the release notes")
    bridge = (root / "initialize_claude_ai" / "mcp_auto_discovery" / "bridge.py").read_text(encoding="utf-8")
    fallback = re.search(r'^SERVER_VERSION = "([^"]+)"', bridge, re.M)
    if not fallback or fallback.group(1) != version:
        found.append(f"bridge.py SERVER_VERSION is {fallback.group(1) if fallback else 'missing'}, not {version}")
    if tag and tag != f"v{version}":
        found.append(f"the tag {tag} does not name version {version} (expected v{version})")
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--tag", default="", help="the release tag to check (vX.Y.Z)")
    parser.add_argument("--notes", action="store_true", help="print the release notes for the version and exit")
    parser.add_argument("--print", action="store_true", help="print the version and exit")
    args = parser.parse_args(argv)
    version = package_version()
    if args.print:
        print(version)
        return 0
    if args.notes:
        notes = changelog_section(version)
        if not notes:
            print(f"::error::CHANGELOG.md has no notes for {version}", file=sys.stderr)
            return 1
        print(notes)
        return 0
    found = problems(tag=args.tag)
    for problem in found:
        print(f"::error::{problem}")
    if not found:
        print(f"OK: version {version} is consistent")
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
