"""Every setting the code reads has to be a setting the defaults define.

This exists because of one whitespace bug. ``max_provisional`` was written
underneath ``[mcp.discovery.auth.profiles.openqa]`` instead of
``[mcp.discovery.auth]``, so the key the code asked for --
``mcp.discovery.auth.max_provisional`` -- did not exist, the call site's own
default silently answered instead, and the documented comment describing the
limit sat in the file next to a value nobody read. Setting the environment
variable for it would have done nothing at all.

Nothing failed. That is the problem: a setting with a plausible fallback is
invisible when it goes missing, and the two rules this project is built on both
depend on settings actually being reachable -- "nothing is hardcoded" is not
true of a key that silently resolves to a literal in a ``.py`` file.

So the test is structural rather than about any one key: parse the source, find
every literal dotted key handed to a ``Settings`` accessor, and require that the
packaged defaults define it somewhere. Section-relative reads are normal here
(``settings.section("mcp.discovery")`` then ``config.bool_("auth.control_probe")``),
so a key counts as reachable if it resolves under any table in the defaults.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from initialize_claude_ai.core.config import load_settings

#: Accessors that take a dotted settings key as their first argument. ``get`` is
#: deliberately absent: plain dictionaries have one too, and a test that cannot
#: tell ``steps.get("settings.json")`` from a settings read is a test that will
#: be silenced with an allowlist the first time it is wrong.
ACCESSORS = frozenset({"str_", "int_", "bool_", "float_", "list_", "str_list", "section", "table"})

#: Keys that are genuinely the operator's to supply and have no default.
#: Each needs a reason, and "the test complained" is not one.
NO_DEFAULT_BY_DESIGN = {
    # The addresses to discover. There is no sensible default set of other
    # people's hosts, and the flag (--mcp-server) is the usual way in; this is
    # the older config-file spelling, still honoured.
    "mcp.targets",
}

SOURCE_ROOT = pathlib.Path(__file__).resolve().parent.parent / "initialize_claude_ai"


def _table_prefixes(node: object, prefix: str = "") -> list[str]:
    """Every dotted path in the defaults that names a table, plus the root."""
    found = [prefix]
    if isinstance(node, dict):
        for key, value in node.items():
            if isinstance(value, dict):
                found += _table_prefixes(value, f"{prefix}{key}.")
    return found


def _keys_read_from_source() -> list[tuple[str, str]]:
    """(key, "file:line") for every literal dotted settings key in the package."""
    found: list[tuple[str, str]] = []
    for path in sorted(SOURCE_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr not in ACCESSORS or not node.args:
                continue
            first = node.args[0]
            if not (isinstance(first, ast.Constant) and isinstance(first.value, str)):
                continue
            if "." not in first.value:
                continue  # a single word is a key within a section, not a path
            found.append((first.value, f"{path.relative_to(SOURCE_ROOT.parent)}:{node.lineno}"))
    return found


READS = _keys_read_from_source()


def test_the_scan_finds_the_settings_reads_it_is_meant_to() -> None:
    """A scanner that quietly matches nothing would pass every test below."""
    keys = {key for key, _ in READS}
    assert len(keys) > 100, "the AST scan stopped finding settings reads"
    assert "mcp.discovery.auth.max_provisional" in keys, "the key this test was written for"


@pytest.mark.parametrize(("key", "where"), sorted(set(READS)))
def test_a_setting_the_code_reads_is_a_setting_the_defaults_define(key: str, where: str) -> None:
    """Named individually, so a failure says which key and which line."""
    if key in NO_DEFAULT_BY_DESIGN:
        pytest.skip(f"{key} is supplied by the operator")
    settings = load_settings()
    reachable = any(settings.get(prefix + key, None) is not None for prefix in _table_prefixes(settings.data))
    assert reachable, (
        f"{where} reads {key!r}, which the packaged defaults do not define -- "
        "so the call site's fallback answers instead and the setting cannot be "
        "overridden. Add it to the right data/*.toml with a comment, or add it "
        "to NO_DEFAULT_BY_DESIGN with the reason."
    )


def test_the_cap_that_started_this_is_where_the_code_looks_for_it() -> None:
    """The specific regression, asserted where a reader will look for it.

    Not redundant with the scan above: this one pins the *location*, which is
    what actually moved.
    """
    settings = load_settings()
    assert settings.int_("mcp.discovery.auth.max_provisional", -1) == 1
    profile = settings.get("mcp.discovery.auth.profiles.openqa", {})
    assert "max_provisional" not in profile, "the cap drifted back into a product profile"


# --------------------------------------------------------------------------- #
# a setting with its own environment variable is a setting somebody will look for
# --------------------------------------------------------------------------- #


def test_every_named_environment_variable_is_in_the_env_example() -> None:
    """`.env.example` is the file operators copy, so it is the documentation.

    Most settings are reachable only through the generic
    ``INITIALIZE_CLAUDE_AI_<SECTION>__<KEY>`` form and there are far too many to
    list. But a setting given its *own* variable was given one because somebody
    was expected to reach for it, and a shorthand nobody can find is a shorthand
    nobody uses. CONTRIBUTING asks for the entry; this checks it was written.
    """
    import re

    example = (SOURCE_ROOT.parent / ".env.example").read_text(encoding="utf-8")
    named: set[str] = set()
    for path in sorted((SOURCE_ROOT / "data").glob("*.toml")):
        named.update(re.findall(r"\$\{(INITIALIZE_CLAUDE_AI_[A-Z0-9_]+)", path.read_text(encoding="utf-8")))
    assert named, "the scan stopped finding environment variables"
    missing = sorted(name for name in named if name not in example)
    assert missing == [], (
        f"these settings have their own environment variable but no entry in .env.example: {', '.join(missing)}"
    )
