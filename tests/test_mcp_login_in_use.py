"""The credential an existing entry *uses* is the one checked -- in memory, never copied.

The fixes for sd.example.com were tried on another machine. The run re-trialled the
existing ``sd-example-com`` entry, which held its token as a literal; the program
rightly never copies a literal, so the exported variable it asks for was unset,
the login check had nothing real to call with, and the run said nothing about
the token Claude Code was sending -- the one sd.example.com refused. Now that value
(a literal, or the default of an unset ``${NAME:-…}``) is kept in memory as a
``Secret`` for one read-only call, redacted, and still never written.
"""

from __future__ import annotations

import json

import pytest
from test_mcp_login_check import SERVER, TOKEN

from initialize_claude_ai.mcp_auto_discovery import login_check
from initialize_claude_ai.mcp_auto_discovery.existing import PROJECT_FILE, ExistingRegistrations, Registration
from initialize_claude_ai.mcp_auto_discovery.verifier import CandidateVerifier
from initialize_claude_ai.mcp_auto_discovery.writer import McpWriter

STAGE = "package"


@pytest.fixture
def live(ctx, monkeypatch):
    ctx.dry_run = False
    ctx.runner.which = lambda name, **_: name  # type: ignore[assignment]
    ctx.settings.set("mcp.packages.trial_timeout_seconds", 20)
    ctx.settings.set("mcp.registration.verify_as_written", False)
    monkeypatch.delenv("JIRA_PERSONAL_TOKEN", raising=False)
    monkeypatch.delenv("SD_EXAMPLE_TEST_JIRA_PERSONAL_TOKEN", raising=False)
    return ctx


def registration(tmp_path, answer: str, token_value: str) -> Registration:
    import sys

    script = tmp_path / "server.py"
    script.write_text(SERVER)
    entry = {
        "type": "stdio",
        "command": sys.executable,
        "args": [str(script)],
        "env": {
            "ANSWER": answer,
            "JIRA_URL": "https://sd.example.test",
            "JIRA_PERSONAL_TOKEN": token_value,
            "TOKEN_LOG": str(tmp_path / "token.log"),
        },
    }
    return Registration("sd-example-test", entry, PROJECT_FILE)


def verified(ctx, reg: Registration):
    candidate = ExistingRegistrations(ctx).candidate(reg, STAGE)
    return CandidateVerifier(ctx).verify(candidate)


def test_a_literal_the_service_refuses_is_reported_as_refused(live, tmp_path) -> None:
    candidate = verified(live, registration(tmp_path, "portal", TOKEN))
    shake = candidate.handshake

    # The log path is a setting (a path, not a secret), so the anonymous trial
    # writes to it too; the login call is the one that carried the value.
    assert (tmp_path / "token.log").read_text().split()[-1] == "set", "the login call carried the value in use"
    assert shake.login == login_check.NO_ACCESS
    assert shake.login_refused and not candidate.verified and candidate.needs_credential
    assert "existing entry" in shake.login_source
    assert "existing entry" in login_check.waiting_words(shake.login, shake.login_source)


def test_the_value_in_use_is_never_written_reported_or_logged(live, tmp_path, caplog) -> None:
    candidate = verified(live, registration(tmp_path, "portal", TOKEN))
    writer = McpWriter(live)
    entry = writer.entry_for(candidate, "sd-example-test")

    assert TOKEN not in json.dumps(entry)
    assert entry["env"]["JIRA_PERSONAL_TOKEN"].startswith("${")
    assert TOKEN not in json.dumps(candidate.to_dict(), default=str)
    assert TOKEN not in json.dumps(candidate.handshake.to_dict(), default=str)
    assert TOKEN not in " ".join(candidate.notes)
    assert TOKEN not in caplog.text


def test_an_accepted_value_says_it_is_the_one_to_export(live, tmp_path) -> None:
    candidate = verified(live, registration(tmp_path, "ok", TOKEN))
    assert candidate.handshake.login == login_check.ACCEPTED
    assert not candidate.verified, "the entry reads the variable, which is still unset"
    assert any("accepted by its service" in note for note in candidate.notes)


def test_the_default_of_an_unset_reference_is_the_value_in_use(live, tmp_path) -> None:
    reg = registration(tmp_path, "portal", "${SD_EXAMPLE_TEST_JIRA_PERSONAL_TOKEN:-" + TOKEN + "}")
    candidate = verified(live, reg)
    assert candidate.handshake.login == login_check.NO_ACCESS


def test_it_can_be_turned_off(live, tmp_path) -> None:
    live.settings.set("mcp.verification.login_check.check_in_use", False)
    candidate = verified(live, registration(tmp_path, "portal", TOKEN))
    assert candidate.handshake.login == ""
    assert not (tmp_path / "token.log").exists() or "set" not in (tmp_path / "token.log").read_text().split()


def test_a_refused_value_in_use_reaches_the_report(live, tmp_path) -> None:
    from initialize_claude_ai.mcp_auto_discovery.types import TargetOutcome

    candidate = verified(live, registration(tmp_path, "portal", TOKEN))
    writer = McpWriter(live)
    outcome = TargetOutcome(target="https://sd.example.test", provisional=[candidate])
    published = writer.publish(outcome)[0]
    assert "existing entry" in published.credential_hint
    assert TOKEN not in published.credential_hint
    assert published.name in writer.briefing()["these_were_given_a_credential_their_service_refused"]
