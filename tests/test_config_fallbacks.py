"""A fallback in code is the shipped value -- never a second, silent default.

The TOML files are the single source of every setting. A literal fallback in a
getter call only matters when the key is missing, and one that differs from the
shipped value answers differently depending on whether the key is found: the
HTTP client fell back to a 10 s timeout and a 1 MB cap while the configuration
said 15 s and 2 MB, the protocol prober to two versions out of five, the path
generator to one path in dozens. A later audit found what the first version of
this test let through, because it accepted any "neutral" fallback and guessed
sections by suffix: ``0`` for a 24-turn budget, ``0.0`` for a 240-second
ceiling, ``""`` for URLs and templates, ``[]`` for command lines.

So the rule is structural: parse the source, resolve each literal key through
the ``section(...)`` it was read from, and require that a literal fallback equal
the TOML value. An *empty* fallback is accepted only where mirroring would make
a second copy of something large -- a table, a long list, a paragraph -- which
is itself a place to drift; "nothing configured" is then the honest answer.
Scalars and short values are always mirrored.
"""

from __future__ import annotations

import ast
import pathlib
import re

import pytest

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - 3.10
    import tomli as tomllib  # type: ignore[no-redef]

SOURCE_ROOT = pathlib.Path(__file__).resolve().parent.parent / "initialize_claude_ai"

#: Settings accessors that take ``(key, fallback)``.
GETTERS = frozenset({"str_", "int_", "float_", "bool_", "list_", "str_list", "dict_"})

#: What each accessor answers when no fallback is passed.
IMPLICIT = {"str_": "", "int_": 0, "float_": 0.0, "bool_": False, "list_": [], "str_list": [], "dict_": {}}

#: Beyond these sizes an empty fallback is accepted instead of a copy.
MIRROR_MAX_ITEMS = 8
MIRROR_MAX_CHARS = 100

_MISSING = object()
_ENV_DEFAULT = re.compile(r"\$\{[A-Za-z_][A-Za-z0-9_]*:-([^{}]*)\}")


def _defaults() -> dict:
    merged: dict = {}

    def merge(into: dict, layer: dict) -> None:
        for key, value in layer.items():
            if isinstance(value, dict) and isinstance(into.get(key), dict):
                merge(into[key], value)
            else:
                into[key] = value

    for path in sorted((SOURCE_ROOT / "data").glob("*.toml")):
        merge(merged, tomllib.loads(path.read_text(encoding="utf-8")))
    return merged


DEFAULTS = _defaults()


def _lookup(key: str):
    node = DEFAULTS
    for part in key.split("."):
        if not isinstance(node, dict) or part not in node:
            return _MISSING
        node = node[part]
    return node


def _is_settings(expr: ast.AST) -> bool:
    return ast.unparse(expr).endswith("settings")


def _prefixes(expr: ast.AST, sections: dict[str, set[str]]) -> set[str] | None:
    """The dotted prefixes ``expr`` reads under; ``{""}`` for the root, None if unknown."""
    if (
        isinstance(expr, ast.Call)
        and isinstance(expr.func, ast.Attribute)
        and expr.func.attr == "section"
        and expr.args
        and isinstance(expr.args[0], ast.Constant)
        and isinstance(expr.args[0].value, str)
    ):
        base = _prefixes(expr.func.value, sections)
        return None if base is None else {f"{b}{expr.args[0].value}." for b in base}
    if isinstance(expr, ast.BoolOp):
        found: set[str] = set()
        for value in expr.values:
            found |= _prefixes(value, sections) or set()
        return found or None
    if _is_settings(expr):
        return {""}
    return sections.get(ast.unparse(expr))


def reads(path: pathlib.Path):
    """``(line, getter, key, toml_value, fallback_node_or_None, call)`` for each resolvable read."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    sections: dict[str, set[str]] = {}
    for _ in range(3):  # a section of a section
        for node in ast.walk(tree):
            if isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
                found = _prefixes(node.value, sections)
                if found and found != {""}:
                    for target in node.targets if isinstance(node, ast.Assign) else [node.target]:
                        sections.setdefault(ast.unparse(target), set()).update(found)
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in GETTERS):
            continue
        if not node.args or not isinstance(node.args[0], ast.Constant) or not isinstance(node.args[0].value, str):
            continue
        prefixes = _prefixes(node.func.value, sections)
        if prefixes is None:
            continue
        hits = [(p + node.args[0].value, _lookup(p + node.args[0].value)) for p in sorted(prefixes)]
        hits = [(k, v) for k, v in hits if v is not _MISSING]
        if len(hits) != 1:
            continue
        fallback = (
            node.args[1] if len(node.args) > 1 else next((k.value for k in node.keywords if k.arg == "default"), None)
        )
        yield node.lineno, node.func.attr, hits[0][0], hits[0][1], fallback, node


def normalised(getter: str, value):
    """``value`` as the accessor would answer it; _MISSING when it depends on the environment."""
    if isinstance(value, str):
        while _ENV_DEFAULT.search(value):
            value = _ENV_DEFAULT.sub(lambda m: m.group(1), value)
        if "${" in value:
            return _MISSING
    if getter == "bool_":
        return value.strip().lower() in ("1", "true", "yes", "on") if isinstance(value, str) else bool(value)
    if getter in ("int_", "float_"):
        try:
            return (int if getter == "int_" else float)(value)
        except (TypeError, ValueError):
            return value
    if getter in ("str_list", "list_"):
        if isinstance(value, str):
            value = [s.strip() for s in value.split(",") if s.strip()]
        if any(isinstance(v, str) and "${" in v for v in value):
            return _MISSING
        return [str(v) for v in value] if getter == "str_list" else list(value)
    if isinstance(value, dict):
        return value
    return value


def must_mirror(value) -> bool:
    """Is ``value`` small enough that an empty fallback is just a wrong one?"""
    if isinstance(value, dict):
        return False
    if isinstance(value, list):
        return len(value) <= MIRROR_MAX_ITEMS and all(len(str(v)) <= MIRROR_MAX_CHARS for v in value)
    if isinstance(value, str):
        return len(value) <= MIRROR_MAX_CHARS
    return True


def mismatches() -> list[tuple[str, str, str, object, object]]:
    found = []
    for path in sorted(SOURCE_ROOT.rglob("*.py")):
        for line, getter, key, toml_value, fallback, _call in reads(path):
            if fallback is None:
                literal = IMPLICIT[getter]
            else:
                try:
                    literal = ast.literal_eval(fallback)
                except ValueError:
                    continue  # a computed fallback (a module constant): checked where it is defined
            expected = normalised(getter, toml_value)
            if expected is _MISSING:
                continue
            actual = normalised(getter, literal)
            if actual == expected:
                continue
            if actual in ("", [], {}) and not must_mirror(expected):
                continue
            found.append((f"{path.relative_to(SOURCE_ROOT)}:{line}", getter, key, literal, expected))
    return found


def test_the_scan_resolves_section_relative_reads() -> None:
    """Guard against the scan silently going blind."""
    keys = {key for path in sorted(SOURCE_ROOT.rglob("*.py")) for _l, _g, key, *_ in reads(path)}
    assert len(keys) > 300, len(keys)
    assert "mcp.discovery.response_scan_chars" in keys  # read through self.discovery
    assert "mcp.agent.max_turns" in keys  # read through self.config


def test_every_fallback_is_the_packaged_default() -> None:
    wrong = mismatches()
    assert wrong == [], "\n".join(
        f"{where}: {getter}({key!r}) falls back to {code!r}, the defaults say {toml!r}"
        for where, getter, key, code, toml in wrong
    )


def test_every_section_relative_read_names_a_packaged_key() -> None:
    """A single-word key read through ``section(...)`` must exist too.

    ``test_mcp_settings_reachable`` checks dotted keys only -- a single word is
    a key within a section, and it cannot tell which. This scan can: it
    resolves the section. ``mcp.registration.shared_secret_template`` was read
    through ``self.registration`` before the TOML had it, and the code's
    fallback would have answered silently forever.
    """
    from test_mcp_settings_reachable import NO_DEFAULT_BY_DESIGN

    missing = []
    for path in sorted(SOURCE_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        sections: dict[str, set[str]] = {}
        for _ in range(3):
            for node in ast.walk(tree):
                if isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
                    found = _prefixes(node.value, sections)
                    if found and found != {""}:
                        for target in node.targets if isinstance(node, ast.Assign) else [node.target]:
                            sections.setdefault(ast.unparse(target), set()).update(found)
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in GETTERS | {"path", "section"}
            ):
                continue
            if not node.args or not isinstance(node.args[0], ast.Constant) or not isinstance(node.args[0].value, str):
                continue
            prefixes = _prefixes(node.func.value, sections)
            if prefixes is None:
                continue
            keys = [p + node.args[0].value for p in prefixes]
            if set(keys) & NO_DEFAULT_BY_DESIGN:
                continue
            if all(_lookup(k) is _MISSING for k in keys):
                missing.append(f"{path.relative_to(SOURCE_ROOT)}:{node.lineno} {' or '.join(sorted(keys))}")
    assert missing == [], "read but not in data/*.toml:\n" + "\n".join(missing)


@pytest.mark.parametrize("getter", sorted(GETTERS))
def test_the_scan_sees_every_getter(getter) -> None:
    """Guard the guard: a renamed getter must not quietly leave it scanning nothing."""
    assert any(g == getter for path in sorted(SOURCE_ROOT.rglob("*.py")) for _l, g, *_ in reads(path))


def test_a_constant_used_as_a_fallback_is_the_packaged_default() -> None:
    """Module constants passed as fallbacks (``list(HOSTING_NAMESPACES)``) are checked too."""
    from initialize_claude_ai.mcp_auto_discovery import grounding

    assert normalised("str_list", _lookup("mcp.grounding.multi_part_suffixes")) == list(grounding.MULTI_PART_SUFFIXES)
    assert normalised("str_list", _lookup("mcp.packages.hosting_namespaces")) == list(grounding.HOSTING_NAMESPACES)
