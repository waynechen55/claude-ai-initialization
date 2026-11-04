"""The repository carries no internal names (tools/check_names.py), and the check itself names none."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("check_names", ROOT / "tools" / "check_names.py")
check_names = importlib.util.module_from_spec(_spec)
sys.modules["check_names"] = check_names
_spec.loader.exec_module(check_names)


def test_the_repository_has_no_denied_name() -> None:
    assert check_names.findings(check_names.tracked_files()) == []


def test_a_denied_fingerprint_is_found_in_hosts_parents_and_paths(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        check_names, "DENIED", {check_names.fingerprint("corp.invalid"), check_names.fingerprint("someone")}
    )
    sample = tmp_path / "sample.md"
    sample.write_text(
        "ok: https://jira.example.com\nbad: https://jira.CORP.invalid/x\nbad: /home/someone/x\nok: someone55\n"
    )
    found = check_names.findings([sample])
    assert [f.split(":")[1] for f in found] == ["2", "3"]


def test_parent_domains_are_compared_but_not_substrings() -> None:
    assert {"corp.invalid", "b.corp.invalid"} <= set(check_names.candidates("see a.b.corp.invalid"))
    assert "corp.invalid" not in set(check_names.candidates("see notcorp.invalidx"))
