"""How step outcomes roll up into a phase status and an exit code."""

from __future__ import annotations

import json

from initialize_claude_ai.core.report import PhaseResult, RunReport, Status, worst


def phase(*statuses: str) -> PhaseResult:
    result = PhaseResult(phase="test")
    for index, status in enumerate(statuses):
        result.add(f"step-{index}", status)
    return result.finalize()


def test_worst_of_nothing_is_skipped() -> None:
    assert worst([]) == Status.SKIPPED


def test_all_ok_is_ok() -> None:
    assert phase(Status.OK, Status.OK).status == Status.OK


def test_any_change_is_reported_as_changed() -> None:
    assert phase(Status.OK, Status.CHANGED).status == Status.CHANGED


def test_a_failure_beside_real_work_is_partial() -> None:
    """'partial' and 'failed' drive different exit codes, so the line matters."""
    assert phase(Status.OK, Status.FAILED).status == Status.PARTIAL


def test_a_failure_alone_is_a_failure() -> None:
    assert phase(Status.FAILED, Status.SKIPPED).status == Status.FAILED


def test_one_skip_among_completed_steps_does_not_skip_the_phase() -> None:
    """Regression: 14 ok + 3 changed + 1 skipped was reported as 'skipped'."""
    assert phase(Status.OK, Status.OK, Status.SKIPPED).status == Status.OK
    assert phase(Status.CHANGED, Status.SKIPPED).status == Status.CHANGED


def test_a_phase_with_no_steps_and_a_reason_is_skipped() -> None:
    result = PhaseResult(phase="test", skipped_reason="disabled in config")
    assert result.finalize().status == Status.SKIPPED


def test_remediation_is_collected_from_steps_without_duplicates() -> None:
    result = PhaseResult(phase="test")
    result.add("a", Status.PARTIAL, remediation=["do the thing"])
    result.add("b", Status.PARTIAL, remediation=["do the thing", "and this"])
    result.finalize()
    assert result.remediation == ["do the thing", "and this"]


def test_exit_code_policy() -> None:
    report = RunReport()
    report.add(phase(Status.OK, Status.FAILED))  # -> partial
    assert report.exit_code() == 0
    assert report.exit_code(partial_is_failure=True) == 2
    report.add(phase(Status.FAILED))
    assert report.exit_code() == 1


# -- a phase that stopped for want of a value ----------------------------------


def fatal_phase() -> PhaseResult:
    """A phase that did some work and then found a setting it could not do without."""
    result = PhaseResult(phase="provision")
    result.add("backend", Status.OK)
    result.add("project", Status.FAILED, error="missing_setting")
    result.fatal = True
    return result.finalize()


def test_a_fatal_phase_is_failed_however_much_of_it_worked() -> None:
    assert fatal_phase().status == Status.FAILED


def test_a_fatal_phase_fails_the_whole_run_not_just_itself() -> None:
    """Regression: phase 1 passing softened a fatal phase 3 into an exit code of 0.

    ``roll_up`` reading "one failed, three worked" as PARTIAL is right for
    almost every failure and wrong for this one -- a configuration that was
    deliberately not written is not a run that mostly succeeded.
    """
    report = RunReport()
    report.add(phase(Status.OK, Status.CHANGED))
    report.add(fatal_phase())
    assert report.fatal is True
    assert report.status == Status.FAILED
    assert report.exit_code() == 1


def test_carrying_on_past_a_fatal_phase_does_not_make_the_run_green() -> None:
    """``--ignore-failure`` buys the remaining phases a turn, not a passing grade."""
    report = RunReport()
    report.add(fatal_phase())
    report.add(phase(Status.OK, Status.OK))  # what --ignore-failure lets happen next
    assert report.status == Status.FAILED
    assert report.exit_code() == 1


def test_a_run_with_no_fatal_phase_still_rolls_up_as_before() -> None:
    report = RunReport()
    report.add(phase(Status.OK))
    report.add(phase(Status.OK, Status.FAILED))
    assert report.fatal is False
    assert report.status == Status.PARTIAL


def test_the_json_report_says_a_value_was_missing() -> None:
    """A caller reading the report has to be able to tell the two apart too."""
    report = RunReport()
    report.add(fatal_phase())
    payload = json.loads(report.to_json())
    assert payload["fatal"] is True
    assert payload["status"] == Status.FAILED
    assert payload["phases"][0]["fatal"] is True


def test_report_serialises_and_redacts() -> None:
    result = PhaseResult(phase="auth")
    result.add("login", Status.OK, detail="ok", data={"api_key": "abcd1234"})
    result.finalize()
    report = RunReport(dry_run=True)
    report.add(result)
    payload = json.loads(report.to_json())
    assert payload["dry_run"] is True
    assert payload["phases"][0]["steps"][0]["data"]["api_key"] != "abcd1234"
    assert payload["phases"][0]["counts"] == {Status.OK: 1}


def test_the_report_says_what_the_run_was_invoked_with() -> None:
    """Read months later, a report has to say what it was run with, not only
    what it found -- and say it without carrying a credential into the ticket."""
    report = RunReport()
    report.invocation = {
        "command_line": "initialize-claude-ai run --dry-run",
        "options": {"--dry-run": "yes"},
        "environment": {"ANTHROPIC_API_KEY": "sk-ant-not-a-real-key"},
    }
    payload = json.loads(report.to_json())
    assert payload["invocation"]["command_line"].endswith("--dry-run")
    assert payload["invocation"]["options"]["--dry-run"] == "yes"
    assert "sk-ant-not-a-real-key" not in json.dumps(payload)
