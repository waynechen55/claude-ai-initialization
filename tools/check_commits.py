#!/usr/bin/env python3
"""The pull-request commit gate: exactly one commit, with a title and a body.

Run by CI on every pull request (``.github/workflows/ci.yml``, job
``commits``) and runnable locally before pushing::

    python tools/check_commits.py origin/main HEAD

Rules, each a separate failure with its own message:

* the range holds exactly one commit -- squash a branch before opening or
  updating the pull request (``git rebase -i`` / ``git reset --soft``);
* that commit is not a merge commit;
* it has a title: a non-empty first line, at most ``--max-title`` characters;
* the title is followed by a blank line and a body;
* the body says something: lines that are only trailers
  (``Signed-off-by:``, ``Co-Authored-By:`` ...) do not count as a body.

Only the standard library is used, so the check runs before anything is
installed.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass, field

#: A git trailer line: ``Token: value`` where the token has no spaces.
TRAILER = re.compile(r"^[A-Za-z][A-Za-z0-9-]*:\s+\S")
#: Default limit for the title; long enough for a real summary, short enough for one line in every view.
MAX_TITLE = 72


@dataclass
class Commit:
    sha: str
    parents: list[str]
    message: str


@dataclass
class Report:
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems


def split_message(message: str) -> tuple[str, str]:
    """``(title, body)`` of a commit message; the body excludes trailer-only lines and comments."""
    lines = message.replace("\r\n", "\n").split("\n")
    title = lines[0].strip() if lines else ""
    rest = lines[1:]
    body_lines = [line for line in rest if line.strip() and not line.lstrip().startswith("#")]
    meaningful = [line for line in body_lines if not TRAILER.match(line.strip())]
    return title, "\n".join(meaningful).strip()


def check_message(message: str, max_title: int = MAX_TITLE) -> list[str]:
    """Every problem with one commit message, in words; [] when it has a title and a body."""
    problems: list[str] = []
    title, body = split_message(message)
    if not title:
        problems.append("the commit has no title (its first line is empty)")
    elif len(title) > max_title:
        problems.append(f"the title is {len(title)} characters; keep it to {max_title} or fewer")
    lines = message.replace("\r\n", "\n").split("\n")
    if title and len(lines) > 1 and lines[1].strip():
        problems.append("the title must be followed by a blank line before the body")
    if not body:
        problems.append(
            "the commit has no body: explain what changed and why below the title "
            "(trailers such as Signed-off-by do not count)"
        )
    return problems


def check_commits(commits: list[Commit], max_title: int = MAX_TITLE) -> Report:
    """The whole gate over the commits a pull request adds."""
    report = Report()
    if not commits:
        report.problems.append("the pull request adds no commits")
        return report
    if len(commits) != 1:
        listed = ", ".join(c.sha[:12] for c in commits)
        report.problems.append(
            f"the pull request has {len(commits)} commits ({listed}); "
            "squash them into one commit and force-push the branch"
        )
    for commit in commits:
        if len(commit.parents) > 1:
            report.problems.append(f"{commit.sha[:12]} is a merge commit; rebase the branch instead of merging into it")
        for problem in check_message(commit.message, max_title):
            report.problems.append(f"{commit.sha[:12]}: {problem}")
    return report


def git(*args: str) -> str:
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True).stdout


def commits_between(base: str, head: str) -> list[Commit]:
    """The commits reachable from ``head`` and not from ``base``, oldest first."""
    shas = git("rev-list", "--reverse", f"{base}..{head}").split()
    found: list[Commit] = []
    for sha in shas:
        parents = git("rev-list", "--parents", "-n", "1", sha).split()[1:]
        message = git("log", "-1", "--format=%B", sha).rstrip("\n")
        found.append(Commit(sha=sha, parents=parents, message=message))
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("base", help="the pull request's base (e.g. origin/main or the base SHA)")
    parser.add_argument("head", nargs="?", default="HEAD", help="the pull request's head (default HEAD)")
    parser.add_argument("--max-title", type=int, default=MAX_TITLE)
    args = parser.parse_args(argv)
    try:
        commits = commits_between(args.base, args.head)
    except subprocess.CalledProcessError as exc:
        print(f"::error::cannot list commits {args.base}..{args.head}: {exc.stderr.strip()}")
        return 2
    report = check_commits(commits, args.max_title)
    for problem in report.problems:
        print(f"::error::{problem}")
    if report.ok:
        print(f"OK: one commit ({commits[0].sha[:12]}) with a title and a body")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
