"""The generated references (tools/gen_docs.py) match their sources."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("gen_docs", ROOT / "tools" / "gen_docs.py")
gen_docs = importlib.util.module_from_spec(_spec)
sys.modules["gen_docs"] = gen_docs
_spec.loader.exec_module(gen_docs)


def test_the_configuration_reference_is_current() -> None:
    assert (ROOT / "docs" / "config-reference.md").read_text(encoding="utf-8") == gen_docs.render_config(), (
        "run `python tools/gen_docs.py` and commit docs/config-reference.md"
    )


def test_every_option_is_in_the_cli_reference() -> None:
    from initialize_claude_ai import cli

    text = (ROOT / "docs" / "cli-reference.md").read_text(encoding="utf-8")
    missing = sorted(
        {
            option
            for action in _actions(cli.build_parser())
            for option in action.option_strings
            if option.startswith("--") and option not in text
        }
    )
    assert missing == [], "run `python tools/gen_docs.py` and commit docs/cli-reference.md"


@pytest.mark.skipif(
    sys.version_info[:2] != (3, 13), reason="argparse's layout differs by version; CI docs job pins 3.13"
)
def test_the_cli_reference_is_current_on_the_docs_python() -> None:
    assert (ROOT / "docs" / "cli-reference.md").read_text(encoding="utf-8") == gen_docs.render_cli()


def _actions(parser):
    import argparse

    for action in parser._actions:
        yield action
        if isinstance(action, argparse._SubParsersAction):
            for sub in action.choices.values():
                yield from _actions(sub)


def test_every_table_with_settings_is_documented() -> None:
    """Each ``[table]`` written in the TOML with a setting of its own has its section in the reference."""
    import re

    text = (ROOT / "docs" / "config-reference.md").read_text(encoding="utf-8")
    documented = set(re.findall(r"^### `\[\[?(.+?)\]\]?`$", text, re.M))
    missing = []
    for path in sorted(gen_docs.DATA.glob("*.toml")):
        table, has_keys = "", False
        for line in [*path.read_text(encoding="utf-8").splitlines(), "[end-of-file]"]:
            header = gen_docs.TABLE.match(line)
            if header and not gen_docs.KEY.match(line.strip()):
                if table and has_keys and table not in documented:
                    missing.append(f"{path.name}: [{table}]")
                table, has_keys = header.group(2), False
            elif gen_docs.KEY.match(line) and not line[:1].isspace():
                has_keys = True
    assert missing == []
