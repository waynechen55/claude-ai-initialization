"""The pull-request commit gate (tools/check_commits.py): one commit, with a title and a body."""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("check_commits", ROOT / "tools" / "check_commits.py")
check_commits = importlib.util.module_from_spec(_spec)
sys.modules["check_commits"] = check_commits
_spec.loader.exec_module(check_commits)

GOOD = "Add the commit gate\n\nPull requests need one commit with a title and a body."


def _one(message: str, parents: int = 1):
    return check_commits.Commit(sha="a" * 40, parents=["p"] * parents, message=message)


def test_a_titled_commit_with_a_body_passes() -> None:
    assert check_commits.check_commits([_one(GOOD)]).ok


@pytest.mark.parametrize(
    "message, words",
    [
        ("", "no title"),
        ("\n\nBody only", "no title"),
        ("Title only", "no body"),
        ("Title\n\nSigned-off-by: A <a@example.com>\nCo-Authored-By: B <b@example.com>", "no body"),
        ("Title\nBody right under it", "blank line"),
        ("T" * 73 + "\n\nBody", "73 characters"),
    ],
)
def test_each_missing_part_is_named(message: str, words: str) -> None:
    report = check_commits.check_commits([_one(message)])
    assert not report.ok and any(words in p for p in report.problems), report.problems


def test_comment_lines_are_not_a_body() -> None:
    assert any("no body" in p for p in check_commits.check_message("Title\n\n# a comment"))


def test_more_than_one_commit_fails_even_when_each_is_good() -> None:
    report = check_commits.check_commits([_one(GOOD), _one(GOOD)])
    assert not report.ok and any("2 commits" in p and "squash" in p for p in report.problems)


def test_no_commits_and_merge_commits_fail() -> None:
    assert not check_commits.check_commits([]).ok
    report = check_commits.check_commits([_one(GOOD, parents=2)])
    assert any("merge commit" in p for p in report.problems)


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_the_command_reads_a_real_range(tmp_path, monkeypatch) -> None:
    def git(*args: str) -> None:
        subprocess.run(["git", "-C", str(tmp_path), *args], check=True, capture_output=True)

    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
        monkeypatch.delenv(name, raising=False)
    git("init", "-q", "-b", "main")
    git("-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "--allow-empty", "-m", "base\n\nroot")
    git("-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "--allow-empty", "-m", GOOD)
    monkeypatch.chdir(tmp_path)
    assert check_commits.main(["HEAD~1", "HEAD"]) == 0
    git("-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "--allow-empty", "-m", "fixup")
    assert check_commits.main(["HEAD~2", "HEAD"]) == 1
