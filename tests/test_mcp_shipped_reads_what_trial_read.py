"""The written entry reads the variable the trial read a credential from.

gitlab.example.net's agent declared ``GITLAB_PERSONAL_ACCESS_TOKEN`` as
``env:GITLAB_EXAMPLE_NET_TOKEN`` -- the operator's variable in Claude Code's
settings. The trial read it and the login was accepted; the entry was written
reading ``${GITLAB_PERSONAL_ACCESS_TOKEN}``, which nothing sets, so a new
terminal's Claude Code launched the server without a token.
"""

from __future__ import annotations

import json

import pytest

from initialize_claude_ai.mcp_auto_discovery.types import Candidate, Handshake
from initialize_claude_ai.mcp_auto_discovery.writer import McpWriter

KEY = "VENDOR_PERSONAL_ACCESS_TOKEN"
HELD = "ICA_TEST_EXAMPLE_TOKEN"


def _candidate() -> Candidate:
    candidate = Candidate(
        kind="stdio",
        name="vendor-mcp",
        command=["npx", "-y", "vendor-mcp@1.0.0"],
        env={"VENDOR_API_URL": "https://code.example.test/api/v4"},
    )
    candidate.secret_env = {KEY: f"env:{HELD}"}
    candidate.handshake = Handshake(ok=True, tools=["whoami"], round_trip=True)
    return candidate


def test_the_entry_reads_the_variable_the_trial_read(ctx, monkeypatch) -> None:
    monkeypatch.setenv(HELD, "x" * 26)
    candidate = _candidate()
    entry = McpWriter(ctx).entry_for(candidate, "code-example-test")
    assert entry["env"][KEY] == f"${{{HELD}}}"
    assert candidate.exported_names[KEY] == HELD, "the instructions name the same variable"
    assert "x" * 26 not in json.dumps(entry), "the variable, never the value"


def test_a_reference_that_holds_nothing_is_only_a_proposal(ctx, monkeypatch) -> None:
    monkeypatch.delenv(HELD, raising=False)
    monkeypatch.setattr(McpWriter, "_claude_settings_env", lambda self: {})
    entry = McpWriter(ctx).entry_for(_candidate(), "code-example-test")
    assert HELD not in json.dumps(entry), "nothing was read from it, so it is named as before"


@pytest.mark.parametrize("enabled", [True, False])
def test_the_setting_decides(ctx, monkeypatch, enabled) -> None:
    monkeypatch.setenv(HELD, "x" * 26)
    ctx.settings.set("mcp.registration.read_trial_source", enabled)
    entry = McpWriter(ctx).entry_for(_candidate(), "code-example-test")
    assert (entry["env"][KEY] == f"${{{HELD}}}") is enabled


def test_packaged_settings_equal_the_code(settings) -> None:
    assert settings.bool_("mcp.registration.read_trial_source") is True
