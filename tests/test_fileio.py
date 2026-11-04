"""Files that may hold a credential are never readable by anyone else, not even briefly."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from initialize_claude_ai.core import fileio
from initialize_claude_ai.core.fileio import write_private


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_the_file_is_owner_only_whatever_the_umask(tmp_path: Path) -> None:
    previous = os.umask(0o022)
    try:
        target = write_private(tmp_path / "env.sh", "export A=1\n")
    finally:
        os.umask(previous)
    assert mode(target) == 0o600
    assert target.read_text(encoding="utf-8") == "export A=1\n"


def test_it_is_private_before_any_byte_is_written(tmp_path: Path, monkeypatch) -> None:
    """The window the old write-then-chmod left open."""
    seen: list[int] = []
    real = os.fdopen

    def watch(handle, *args, **kwargs):
        seen.append(stat.S_IMODE(os.fstat(handle).st_mode))
        return real(handle, *args, **kwargs)

    monkeypatch.setattr(fileio.os, "fdopen", watch)
    write_private(tmp_path / "settings.json", b"{}")
    assert seen == [0o600]


def test_an_existing_file_is_replaced_and_narrowed(tmp_path: Path) -> None:
    target = tmp_path / "settings.json"
    target.write_text("old", encoding="utf-8")
    target.chmod(0o644)
    write_private(target, "new")
    assert target.read_text(encoding="utf-8") == "new" and mode(target) == 0o600


def test_a_failed_write_leaves_nothing_behind(tmp_path: Path, monkeypatch) -> None:
    target = tmp_path / "env.sh"
    target.write_text("kept", encoding="utf-8")
    monkeypatch.setattr(Path, "replace", lambda *a, **k: (_ for _ in ()).throw(OSError("full")))
    with pytest.raises(OSError):
        write_private(target, "lost")
    assert target.read_text(encoding="utf-8") == "kept"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["env.sh"]
