#!/usr/bin/env python3
"""The installed package carries every data file the source tree has.

Run with the interpreter of an environment the wheel was installed into, from
outside the source tree (so ``import initialize_claude_ai`` finds the installed
copy)::

    /path/to/venv/bin/python tools/check_package_data.py

The TOML defaults and the skills are read at run time; a wheel without them
installs cleanly and then fails on first use.
"""

from __future__ import annotations

import sys
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1] / "initialize_claude_ai"
PATTERNS = ["data/*.toml", "skills/*/SKILL.md", "skills/*/*/*.md"]


def main() -> int:
    import initialize_claude_ai

    installed = Path(initialize_claude_ai.__file__).resolve().parent
    if installed == SOURCE.resolve():
        print("::error::this imported the source tree, not the installed package; run it from elsewhere")
        return 2
    missing = sorted(
        str(path.relative_to(SOURCE))
        for pattern in PATTERNS
        for path in SOURCE.glob(pattern)
        if not (installed / path.relative_to(SOURCE)).is_file()
    )
    for name in missing:
        print(f"::error::the installed package lacks {name}")
    if not missing:
        count = sum(1 for pattern in PATTERNS for _ in SOURCE.glob(pattern))
        print(f"OK: {count} data files installed at {installed}")
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
