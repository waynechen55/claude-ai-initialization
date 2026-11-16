"""Every command this program runs is bounded by a setting.

The failure these guard against: about twenty subprocess timeouts were figures
typed at the call site. None of them could be changed without editing code, and
``--timeout-scale`` -- which stretches every setting whose name ends in
``timeout_seconds`` -- silently skipped them all, so a slow host told to allow
twice the time still had its gcloud token mint cut off at 90 seconds.
"""

from __future__ import annotations

import ast
import os
import re
from pathlib import Path

import pytest

import initialize_claude_ai
from initialize_claude_ai.core.config import load_settings

PACKAGE = Path(initialize_claude_ai.__file__).parent


def runner_calls():
    for path in sorted(PACKAGE.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in {"probe", "run", "run_tty"}
                and "runner" in ast.unparse(node.func)
            ):
                yield path.relative_to(PACKAGE), node


def test_no_command_is_given_a_literal_timeout() -> None:
    literal = [
        f"{path}:{node.lineno}"
        for path, node in runner_calls()
        for kw in node.keywords
        if kw.arg == "timeout" and isinstance(kw.value, ast.Constant)
    ]
    assert literal == []


@pytest.mark.parametrize(
    ("module", "section"),
    [("authenticate/google_cloud.py", "auth.google_cloud"), ("authenticate/claude_oauth.py", "auth.claude_oauth")],
)
def test_every_timed_command_has_an_entry(settings, module, section) -> None:
    names = set(re.findall(r'self\.timeout\("(\w+)"\)', (PACKAGE / module).read_text(encoding="utf-8")))
    table = settings.dict_(f"{section}.command_timeout_seconds")
    commands = settings.dict_(f"{section}.commands")
    assert names and names <= set(table)
    assert set(table) <= set(commands), "an entry for a command that does not exist"


def test_the_scale_reaches_a_table_of_timeouts() -> None:
    scaled = load_settings(overrides=["run.timeout_scale=2"], environ=dict(os.environ))
    plain = load_settings(overrides=[], environ=dict(os.environ))
    key = "auth.google_cloud.command_timeout_seconds.print_token"
    assert scaled.float_(key) == 2 * plain.float_(key)
    view = scaled.section("auth.google_cloud")
    assert view.float_("command_timeout_seconds.print_token") == 2 * plain.float_(key)


def test_the_scale_still_leaves_non_timeouts_alone() -> None:
    scaled = load_settings(overrides=["run.timeout_scale=2"], environ=dict(os.environ))
    plain = load_settings(overrides=[], environ=dict(os.environ))
    assert scaled.float_("run.stop_grace_seconds") == plain.float_("run.stop_grace_seconds")
