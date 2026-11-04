#!/usr/bin/env python3
"""Every relative link in the Markdown files resolves: the file exists, and so does the heading.

External links (``http(s)://``, ``mailto:``) are not fetched -- CI must not
depend on someone else's server. Anchors are computed the way GitHub renders
them: lower-cased, punctuation dropped, spaces to hyphens, repeats numbered.

Usage::

    python tools/check_links.py              # README, top-level *.md, docs/**, .github/**/*.md
    python tools/check_links.py FILE.md ...
"""

from __future__ import annotations

import re
import sys
import unicodedata
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parents[1]
FENCE = re.compile(r"^\s*(```|~~~)")
HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
LINK = re.compile(r"(?<!!)\[(?:[^\]\\]|\\.)*\]\(\s*<?([^)\s>]+)>?(?:\s+\"[^\"]*\")?\s*\)")
IMAGE = re.compile(r"!\[[^\]]*\]\(\s*<?([^)\s>]+)>?")


def slug(text: str) -> str:
    """GitHub's anchor for a heading's text."""
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"<[^>]+>", "", text)
    out = []
    for char in text.lower():
        if char in " -_" or char.isalnum() or unicodedata.category(char).startswith("M"):
            out.append("-" if char == " " else char)
    return "".join(out)


def outside_fences(text: str) -> Iterable[tuple[int, str]]:
    fenced = False
    for number, line in enumerate(text.splitlines(), 1):
        if FENCE.match(line):
            fenced = not fenced
            continue
        if not fenced:
            yield number, line


def anchors(text: str) -> set[str]:
    seen: dict[str, int] = {}
    found: set[str] = set()
    for _number, line in outside_fences(text):
        match = HEADING.match(line)
        if not match:
            continue
        base = slug(match.group(2))
        count = seen.get(base, 0)
        seen[base] = count + 1
        found.add(base if count == 0 else f"{base}-{count}")
    found.update(re.findall(r"<a\s+(?:name|id)=\"([^\"]+)\"", text))
    return found


def default_files() -> list[Path]:
    files = sorted(ROOT.glob("*.md"))
    for folder in ("docs", ".github"):
        files.extend(sorted((ROOT / folder).rglob("*.md")))
    return files


def problems(files: Iterable[Path]) -> list[str]:
    cache: dict[Path, set[str]] = {}

    def anchors_of(path: Path) -> set[str]:
        if path not in cache:
            cache[path] = anchors(path.read_text(encoding="utf-8"))
        return cache[path]

    found: list[str] = []
    for path in files:
        text = path.read_text(encoding="utf-8")
        for number, line in outside_fences(text):
            line = re.sub(r"`[^`]*`", "", line)  # code spans are not links
            for target in [*LINK.findall(line), *IMAGE.findall(line)]:
                if re.match(r"^[a-z][a-z0-9+.-]*:", target, re.I):
                    continue
                name, _, anchor = target.partition("#")
                where = (path.parent / name).resolve() if name else path
                shown = f"{path.relative_to(ROOT)}:{number}"
                if not where.exists():
                    found.append(f"{shown}: {target} -- no such file")
                elif anchor and where.suffix == ".md" and anchor not in anchors_of(where):
                    found.append(f"{shown}: {target} -- no heading #{anchor} in {where.relative_to(ROOT)}")
    return found


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    files = [Path(a).resolve() for a in args] if args else default_files()
    found = problems(files)
    for problem in found:
        print(f"::error::{problem}")
    if not found:
        print(f"OK: links in {len(files)} Markdown files resolve")
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
