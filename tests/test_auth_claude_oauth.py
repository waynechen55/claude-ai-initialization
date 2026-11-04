"""--claude-user-account: naming the account without over-trusting the CLI.

A claude.ai session has no machine-readable identity. The token path knows only
a fingerprint, the existing-session path knows only that a file exists, so the
configured account is the only name either of them can be given.

The mismatch check is the interesting half. ``gcloud auth list --format=json``
states the active account as data, so the Google backend treats a disagreement
as fact and re-authenticates. Claude Code's status is prose, so the same
disagreement here is a suspicion -- these tests pin that it warns and keeps the
session rather than throwing away a credential that may well be correct.
"""

from __future__ import annotations

import pytest

from initialize_claude_ai.authenticate.base import AuthOutcome
from initialize_claude_ai.authenticate.claude_oauth import ClaudeOAuthAuth
from initialize_claude_ai.core.config import Settings
from initialize_claude_ai.core.proc import CommandResult

ACCOUNT = "me@example.com"
SOMEBODY_ELSE = "someone-else@example.com"


@pytest.fixture
def backend(ctx, monkeypatch):
    """A backend with an existing CLI session and nothing else."""
    auth = ClaudeOAuthAuth(ctx)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_USE_VERTEX", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_USE_BEDROCK", raising=False)
    monkeypatch.setattr(auth, "_claude", lambda: "/usr/bin/claude")
    monkeypatch.setattr(auth, "_credential_files", lambda: ["/sandbox/.credentials.json"])
    return auth


def configure(backend: ClaudeOAuthAuth, monkeypatch, **overrides) -> None:
    section = backend.settings.section(backend.config_key)
    # No token by default: these tests are about the existing-session path.
    patched = Settings(data={**section.data, "token_ref": "", **overrides}, environ=section.environ)
    monkeypatch.setattr(type(backend), "config", property(lambda _self: patched))


def status(backend, monkeypatch, *, stdout: str, returncode: int = 0) -> None:
    monkeypatch.setattr(
        backend.runner,
        "probe",
        lambda argv, **_kw: CommandResult(argv=list(argv), returncode=returncode, stdout=stdout),
    )


def test_every_command_template_is_reachable(ctx) -> None:
    """Regression: these were flat keys, where ``AuthBackend.command()`` does not
    look. Every one resolved to an empty argv, so the status check, both sign-in
    flows and the live round trip were skipped without a word -- and 'verified'
    meant nothing more than 'a credential file exists'."""
    auth = ClaudeOAuthAuth(ctx)
    for name in ("headless_command", "interactive_command", "status_command", "probe_command"):
        assert auth.command(name, {"claude": "claude"}), f"{name} resolves to an empty argv"


def test_the_configured_account_becomes_the_identity(backend, monkeypatch) -> None:
    """An existing session is otherwise anonymous in the report."""
    configure(backend, monkeypatch, account=ACCOUNT)
    status(backend, monkeypatch, stdout="")
    outcome = backend.inspect()
    assert outcome.authenticated is True
    assert outcome.identity == ACCOUNT


def test_no_account_configured_leaves_the_identity_alone(backend, monkeypatch) -> None:
    configure(backend, monkeypatch, account="")
    status(backend, monkeypatch, stdout=f"Logged in as {SOMEBODY_ELSE}")
    outcome = backend.inspect()
    assert outcome.authenticated is True
    assert outcome.identity == ""
    assert "account_mismatch" not in outcome.data


def test_a_different_reported_account_warns_but_keeps_the_session(backend, monkeypatch) -> None:
    configure(backend, monkeypatch, account=ACCOUNT)
    status(backend, monkeypatch, stdout=f"Logged in as {SOMEBODY_ELSE}")
    outcome = backend.inspect()
    assert outcome.authenticated is True, "a prose mismatch must not discard a credential"
    assert outcome.data["account_mismatch"] is True
    assert ACCOUNT in outcome.detail
    assert any(ACCOUNT in line for line in outcome.remediation)


def test_the_expected_account_in_the_status_line_says_nothing(backend, monkeypatch) -> None:
    configure(backend, monkeypatch, account=ACCOUNT)
    status(backend, monkeypatch, stdout=f"Logged in as {ACCOUNT.upper()}")
    outcome = backend.inspect()
    assert "account_mismatch" not in outcome.data
    assert outcome.remediation == []


def test_a_status_line_naming_nobody_is_not_evidence(backend, monkeypatch) -> None:
    """No '@' means the CLI did not report an account, not that it reported a
    different one -- the difference decides whether the operator gets a warning
    they cannot act on."""
    configure(backend, monkeypatch, account=ACCOUNT)
    status(backend, monkeypatch, stdout="Authenticated")
    assert "account_mismatch" not in backend.inspect().data


def test_a_failed_status_command_is_not_evidence(backend, monkeypatch) -> None:
    configure(backend, monkeypatch, account=ACCOUNT)
    status(backend, monkeypatch, stdout=f"unknown command; try {SOMEBODY_ELSE}", returncode=2)
    assert "account_mismatch" not in backend.inspect().data


def test_an_absent_credential_says_who_to_sign_in_as(backend, monkeypatch) -> None:
    configure(backend, monkeypatch, account=ACCOUNT)
    monkeypatch.setattr(backend, "_credential_files", lambda: [])
    status(backend, monkeypatch, stdout="", returncode=1)
    outcome = backend.inspect()
    assert outcome.authenticated is False
    assert any(ACCOUNT in line for line in outcome.remediation)


def on_host(backend, monkeypatch, mode: str, *, display: bool) -> None:
    data = dict(backend.ctx.settings.data)
    data["auth"] = {**data.get("auth", {}), "browser_mode": mode}
    backend.ctx.settings = Settings(data=data, environ=backend.ctx.settings.environ)
    monkeypatch.setattr(type(backend.ctx.platform), "headless", property(lambda _self: not display))


@pytest.mark.parametrize(
    ("mode", "display", "opens"),
    [("auto", True, True), ("auto", False, False), ("headless", True, False), ("browser", True, False)],
)
def test_login_opens_the_browser_only_where_one_can_be_shown(backend, monkeypatch, mode, display, opens) -> None:
    """Without --headless, /login opens the browser and still takes a pasted
    code; --headless keeps setup-token, whose URL the operator carries."""
    on_host(backend, monkeypatch, mode, display=display)
    assert backend.command_opens_browser is opens


def test_login_opening_the_browser_can_be_turned_off(backend, monkeypatch) -> None:
    on_host(backend, monkeypatch, "auto", display=True)
    configure(backend, monkeypatch, interactive_command_opens_browser=False)
    assert backend.command_opens_browser is False
    assert backend.sign_in_env()


def test_a_command_that_opens_the_browser_keeps_its_browser(backend, monkeypatch) -> None:
    """paste_env would stop /login opening anything, and nothing else would."""
    on_host(backend, monkeypatch, "auto", display=True)
    assert backend.sign_in_env() == {}


@pytest.mark.parametrize(("mode", "chosen"), [("auto", "interactive_command"), ("headless", "headless_command")])
def test_the_command_run_follows_the_flow(backend, monkeypatch, mode, chosen) -> None:
    on_host(backend, monkeypatch, mode, display=True)
    asked: list[str] = []
    monkeypatch.setattr(backend, "command", lambda name, _tokens: asked.append(name) or [])
    backend.acquire(AuthOutcome(backend=backend.key))
    assert asked == [chosen]
