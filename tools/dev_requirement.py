#!/usr/bin/env python3
"""Print the requirement pyproject's ``dev`` extra pins for one tool (``ruff==0.11.13``).

CI installs single tools with it, so a job and ``pip install -e .[dev]`` always
agree on the version::

    python -m pip install "$(python3 tools/dev_requirement.py ruff)"
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def requirement(name: str, root: Path = ROOT) -> str:
    try:
        import tomllib
    except ModuleNotFoundError:  # Python 3.10
        import tomli as tomllib
    data = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    wanted = re.sub(r"[-_.]+", "-", name).lower()
    for spec in data["project"]["optional-dependencies"]["dev"]:
        found = re.match(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)", spec)
        if found and re.sub(r"[-_.]+", "-", found.group(1)).lower() == wanted:
            return spec
    raise SystemExit(f"pyproject.toml's dev extra does not list {name}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: dev_requirement.py NAME")
    print(requirement(sys.argv[1]))
