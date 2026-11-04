#!/usr/bin/env python3
"""No internal host names or personal paths in the repository.

The project was developed against private infrastructure; examples in code,
tests and documentation use neutral names (``example.com``, ``example.net``,
``/home/user``). This check keeps it that way without itself publishing the
names it guards against: the denied names are stored only as SHA-256
fingerprints (:data:`DENIED`).

What is compared:

* every host-like token (``a.b.c``) and each of its parent domains
  (``x.y.example.com`` -> ``y.example.com``, ``example.com``);
* every alphanumeric word (catches a username in a path).

Usage::

    python tools/check_names.py            # the files git tracks, or the source tree outside a checkout
    python tools/check_names.py FILE...    # the named files

To add a name, append ``sha256(name.lower())`` to :data:`DENIED`; never the name.
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Iterable, Iterator

ROOT = Path(__file__).resolve().parents[1]

#: sha256 of each denied name, lower-cased.
DENIED = {
    "6c13caa1bd24fe7a8b901e8f231655a81f47386161cf00c741066d6dc1b2d593",  # an internal domain
    "cacd0a5031f198b3ee542143c442976ff659f0cbeaea749787dacea9fd969b62",  # an internal domain
    "d900edfb203b76fc81cb4c5b3e5c14cfff71dac0cf51b93a6ec505464240d700",  # a local username
}

#: What the source tree is, outside a git checkout (the same set the first commit is built from).
SOURCE = [
    "initialize_claude_ai",
    "tests",
    "tools",
    "docs",
    ".github",
    "README.md",
    "CHANGELOG.md",
    "CONTRIBUTING.md",
    "SECURITY.md",
    "CODE_OF_CONDUCT.md",
    "LICENSE",
    ".env.example",
    "pyproject.toml",
    ".gitignore",
    ".editorconfig",
    ".pre-commit-config.yaml",
    "Makefile",
]

HOST = re.compile(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)+")
WORD = re.compile(r"[a-z0-9]+")


def fingerprint(text: str) -> str:
    return hashlib.sha256(text.lower().encode("utf-8")).hexdigest()


def candidates(line: str) -> Iterator[str]:
    """Every string on ``line`` that is compared: host names, their parent domains, words."""
    lower = line.lower()
    for host in HOST.findall(lower):
        labels = host.split(".")
        for start in range(len(labels) - 1):
            yield ".".join(labels[start:])
    yield from WORD.findall(lower)


def tracked_files() -> list[Path]:
    try:
        top = subprocess.run(
            ["git", "-C", str(ROOT), "rev-parse", "--show-toplevel"], check=True, capture_output=True, text=True
        ).stdout.strip()
        if Path(top).resolve() == ROOT:
            listed = subprocess.run(
                ["git", "-C", str(ROOT), "ls-files", "-z"], check=True, capture_output=True, text=True
            ).stdout
            return [ROOT / name for name in listed.split("\0") if name]
    except (OSError, subprocess.CalledProcessError):
        pass
    files: list[Path] = []
    for name in SOURCE:
        path = ROOT / name
        if path.is_file():
            files.append(path)
        elif path.is_dir():
            files.extend(p for p in sorted(path.rglob("*")) if p.is_file() and "__pycache__" not in p.parts)
    return files


def findings(paths: Iterable[Path]) -> list[str]:
    found: list[str] = []
    for path in paths:
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue  # binary files carry no examples
        for number, line in enumerate(text.splitlines(), 1):
            if any(fingerprint(c) in DENIED for c in candidates(line)):
                try:
                    shown = path.relative_to(ROOT)
                except ValueError:
                    shown = path
                found.append(
                    f"{shown}:{number}: a denied internal name; use a neutral example "
                    "(example.com, example.net, /home/user)"
                )
    return found


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    paths = [Path(a).resolve() for a in args] if args else tracked_files()
    problems = findings(paths)
    for problem in problems:
        print(f"::error::{problem}")
    if not problems:
        print(f"OK: {len(paths)} files, no internal names")
    return 1 if problems else 0


if __name__ == "__main__":
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    sys.exit(main())
