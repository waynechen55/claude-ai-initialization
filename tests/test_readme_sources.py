"""The documentation's table of ordinary variables is the ``[sources]`` table.

It says so ("This table is not prose"), and it had drifted: two rows listed
fewer project variables than the packaged defaults read, so an operator who had
only ``CLOUDSDK_CORE_PROJECT`` exported was told it would not be seen.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def documentation() -> str:
    """The README and every page under docs/: the table may live on any of them."""
    return "\n".join(
        p.read_text(encoding="utf-8") for p in [ROOT / "README.md", *sorted((ROOT / "docs").rglob("*.md"))]
    )


PREFIX = "INITIALIZE_CLAUDE_AI_"
ROW = re.compile(r"^\| (`[A-Z][A-Z0-9_]*`(?:, `[A-Z][A-Z0-9_]*`)*) \| (`--[a-z-]+`|—) \| `([a-z_.<>]+)` \|$", re.M)


def rows() -> dict[str, tuple[list[str], str]]:
    return {
        setting: (re.findall(r"`([A-Z0-9_]+)`", names), flag.strip("`"))
        for names, flag, setting in ROW.findall(documentation())
    }


def key_for(setting: str) -> str:
    """The documentation writes a per-backend key as ``provision.<backend>.…``."""
    return ".".join(setting.split(".")[-2:]) if "<backend>" in setting else setting


def test_every_row_matches_the_sources_table(settings) -> None:
    table = settings.dict_("sources")
    found = rows()
    assert found, "the variables table was not found in README.md or docs/"
    for setting, (names, flag) in found.items():
        entry = table[key_for(setting)]
        assert names == [n for n in entry.get("env", []) if not n.startswith(PREFIX)], setting
        assert flag == entry.get("flag", "—"), setting


def test_every_ordinary_variable_source_has_a_row(settings) -> None:
    listed = {key_for(s) for s in rows()}
    for key, entry in settings.dict_("sources").items():
        if any(not n.startswith(PREFIX) for n in entry.get("env", [])):
            assert key in listed, key
