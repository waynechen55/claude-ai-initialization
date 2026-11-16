"""One version everywhere (tools/check_version.py), and the release notes come from the changelog."""

from __future__ import annotations

import importlib.util
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("check_version", ROOT / "tools" / "check_version.py")
check_version = importlib.util.module_from_spec(_spec)
sys.modules["check_version"] = check_version
_spec.loader.exec_module(check_version)


def test_the_repository_is_consistent() -> None:
    assert check_version.problems() == []


def test_the_package_reports_the_same_version() -> None:
    import initialize_claude_ai

    assert initialize_claude_ai.__version__ == check_version.package_version()


def test_the_release_notes_are_the_version_s_changelog_section() -> None:
    notes = check_version.changelog_section(check_version.package_version())
    assert notes and "## [" not in notes and "]: https://" not in notes


def _copy(tmp_path: Path) -> Path:
    for name in ("pyproject.toml", "CHANGELOG.md"):
        shutil.copy(ROOT / name, tmp_path / name)
    for name in ("__init__.py", "mcp_auto_discovery/bridge.py"):
        (tmp_path / "initialize_claude_ai" / name).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(ROOT / "initialize_claude_ai" / name, tmp_path / "initialize_claude_ai" / name)
    return tmp_path


def test_each_disagreement_is_named(tmp_path) -> None:
    root = _copy(tmp_path)
    init = root / "initialize_claude_ai" / "__init__.py"
    init.write_text(init.read_text().replace(f'"{check_version.package_version()}"', '"9.9.9"'))
    found = " | ".join(check_version.problems(root, tag="v1.0.0"))
    assert "no '## [9.9.9]' section" in found and "SERVER_VERSION" in found and "does not name version" in found


def test_a_version_written_in_pyproject_is_refused(tmp_path) -> None:
    root = _copy(tmp_path)
    py = root / "pyproject.toml"
    py.write_text(py.read_text().replace('dynamic = ["version"]', 'version = "0.1.0"'))
    assert any("writes its own version" in p for p in check_version.problems(root))
