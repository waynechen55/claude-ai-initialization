"""After phases 2 and 3, a ``claude`` started in a new terminal logs in on its own -- for every kind of login.

Phase 2 proved each login by running ``claude`` inside this program's process,
which holds every variable the run resolved. A new terminal holds none of them,
so a service-account key kept only in env.sh, an API key read from a file, a
headless token that was printed and never stored, and an impersonation only the
gcloud CLI knew about all passed phase 2 and left the next ``claude`` asking to
log in. These tests pin each fix; phase 3's own new-terminal check catches the
rest at run time.
"""

from __future__ import annotations

import json
import os
from types import SimpleNamespace

from fake_secrets import fake

from initialize_claude_ai.authenticate.base import AuthOutcome
from initialize_claude_ai.core.report import PhaseResult, Status
from initialize_claude_ai.phases.phase3_provision import ProvisionPhase


def test_a_login_path_phase_2_set_goes_where_claude_code_reads_it(ctx, monkeypatch) -> None:
    monkeypatch.setattr(
        ctx.env,
        "get",
        lambda name, default="": "/keys/sa.json" if name == "GOOGLE_APPLICATION_CREDENTIALS" else default,
    )
    phase = ProvisionPhase(ctx)
    block = phase._login_env(ctx.settings.section("provision.vertex"))
    assert block == {"GOOGLE_APPLICATION_CREDENTIALS": "/keys/sa.json"}
    assert "GOOGLE_APPLICATION_CREDENTIALS" in phase._managed_env_names(ctx.settings.section("provision"), [])


def test_a_credential_shaped_value_is_never_written_there(ctx, monkeypatch) -> None:
    token = fake("sk-ant-", "api03-Zq", "8vR2mXk4", "Lp9TnW7c", "Yb3HsJ6d", "Fg1QaE5u", "Vo0iNr")
    monkeypatch.setattr(
        ctx.env, "get", lambda name, default="": token if name == "GOOGLE_APPLICATION_CREDENTIALS" else default
    )
    assert ProvisionPhase(ctx)._login_env(ctx.settings.section("provision.vertex")) == {}


def test_a_key_from_a_file_is_reached_through_an_api_key_helper(ctx) -> None:
    ctx.state["anthropic_api_key"] = SimpleNamespace(source="file:/home/me/.anthropic key")
    helper = ProvisionPhase(ctx)._login_settings("anthropic-api", {})["apiKeyHelper"]
    assert helper == "cat '/home/me/.anthropic key'", "the reference, quoted -- never the value"


def test_a_key_in_an_exported_variable_needs_no_helper(ctx) -> None:
    ctx.state["anthropic_api_key"] = SimpleNamespace(source="env:ANTHROPIC_API_KEY")
    assert ProvisionPhase(ctx)._login_settings("anthropic-api", {}) == {}


def test_an_api_key_helper_the_operator_set_is_kept(ctx) -> None:
    ctx.state["anthropic_api_key"] = SimpleNamespace(source="file:/k")
    assert ProvisionPhase(ctx)._login_settings("anthropic-api", {"apiKeyHelper": "my-helper"}) == {}


def test_a_keyring_key_is_read_by_the_same_package_the_program_uses(ctx) -> None:
    ctx.state["anthropic_api_key"] = SimpleNamespace(source="keyring:anthropic/me")
    helper = ProvisionPhase(ctx)._login_settings("anthropic-api", {})["apiKeyHelper"]
    assert "keyring.get_password" in helper and helper.endswith("anthropic me")


def fake_run(ok: bool, stdout: str = "", stderr: str = ""):
    seen: dict = {}

    def run(argv, **kw):
        seen.update(kw, argv=list(argv))
        return SimpleNamespace(ok=ok, stdout=stdout, stderr=stderr, returncode=0 if ok else 1)

    return run, seen


def test_the_new_terminal_check_starts_claude_with_only_the_kept_names(ctx, monkeypatch) -> None:
    ctx.dry_run = False
    monkeypatch.setattr(type(ctx), "tool", lambda self, *a, **k: "/usr/bin/claude")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "set-in-this-run-only")
    run, seen = fake_run(True, stdout=json.dumps({"is_error": False, "result": "ok"}))
    monkeypatch.setattr(ctx.runner, "run", run)
    result = PhaseResult(phase="provision")
    phase = ProvisionPhase(ctx)
    monkeypatch.setattr(phase, "_first_start_notes", lambda: [])
    phase._fresh_terminal_check(result, "vertex")
    assert seen["env"]["ANTHROPIC_API_KEY"] is None, "nothing this run had reaches the new terminal"
    assert seen["env"]["HOME"] == os.environ["HOME"]
    [step] = [s for s in result.steps if s.name == "a new terminal"]
    assert step.status == Status.OK


def test_a_new_terminal_that_cannot_log_in_is_said_with_what_it_lacks(ctx, monkeypatch) -> None:
    ctx.dry_run = False
    monkeypatch.setattr(type(ctx), "tool", lambda self, *a, **k: "/usr/bin/claude")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "set-in-this-run-only")
    run, _seen = fake_run(False, stderr="Invalid API key · Please run /login")
    monkeypatch.setattr(ctx.runner, "run", run)
    result = PhaseResult(phase="provision")
    phase = ProvisionPhase(ctx)
    monkeypatch.setattr(phase, "_settings_env", lambda: {})
    monkeypatch.setattr(phase, "_first_start_notes", lambda: [])
    phase._fresh_terminal_check(result, "anthropic-api")
    [step] = [s for s in result.steps if s.name == "a new terminal"]
    assert step.status == Status.FAILED and "ANTHROPIC_API_KEY" in step.detail
    assert "shell profile" in step.remediation[0]


def test_a_headless_claude_login_is_stored_not_printed(ctx, monkeypatch) -> None:
    from initialize_claude_ai.authenticate.claude_oauth import ClaudeOAuthAuth

    auth = ClaudeOAuthAuth(ctx)
    assert auth.command("headless_command", {"claude": "claude"}) == ["claude", "auth", "login"]
    assert auth.command("token_command", {"claude": "claude"}) == ["claude", "setup-token"]


def test_a_long_lived_token_is_asked_for_only_explicitly(ctx, monkeypatch) -> None:
    from initialize_claude_ai.authenticate.claude_oauth import ClaudeOAuthAuth

    auth = ClaudeOAuthAuth(ctx)
    monkeypatch.setattr(auth, "_claude", lambda: "/usr/bin/claude")
    monkeypatch.delenv("CLAUDE_CODE_USE_VERTEX", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_USE_BEDROCK", raising=False)
    asked: list[str] = []
    monkeypatch.setattr(auth, "command", lambda name, _tokens: asked.append(name) or [])
    ctx.settings.set("auth.claude_oauth.long_lived_token", True)
    auth.acquire(AuthOutcome(backend=auth.key))
    assert asked == ["token_command"]


def test_impersonation_is_written_into_the_credential_claude_code_reads(ctx, tmp_path, monkeypatch) -> None:
    from initialize_claude_ai.authenticate.google_cloud import GoogleCloudAuth

    adc = tmp_path / "adc.json"
    adc.write_text(json.dumps({"type": "authorized_user"}))
    ctx.settings.set("auth.google_cloud.adc_file", str(adc))
    auth = GoogleCloudAuth(ctx)
    ran: list = []
    monkeypatch.setattr(auth, "run_sign_in", lambda argv, **kw: ran.append(argv) or SimpleNamespace(ok=True))
    outcome = AuthOutcome(backend=auth.key)
    outcome.detail = "impersonating sa@p.iam.gserviceaccount.com"
    auth._impersonate_in_adc("gcloud", "sa@p.iam.gserviceaccount.com", outcome)
    assert ran and "--impersonate-service-account=sa@p.iam.gserviceaccount.com" in ran[0]
    adc.write_text(
        json.dumps(
            {
                "type": "impersonated_service_account",
                "service_account_impersonation_url": ".../sa@p.iam.gserviceaccount.com:generateAccessToken",
            }
        )
    )
    ran.clear()
    auth._impersonate_in_adc("gcloud", "sa@p.iam.gserviceaccount.com", outcome)
    assert not ran, "already impersonating: no second sign-in"


def test_a_settings_default_is_never_treated_as_a_secret(ctx, tmp_path, monkeypatch) -> None:
    """A trial expanding ${UNSET:-fallback} registered "fallback" for redaction, for the rest of the process."""
    import sys

    from initialize_claude_ai.core import redact
    from initialize_claude_ai.mcp_auto_discovery.types import Candidate
    from initialize_claude_ai.mcp_auto_discovery.verifier import CandidateVerifier

    monkeypatch.delenv("SOME_UNSET_SETTING", raising=False)
    ctx.dry_run = False
    ctx.runner.which = lambda name, **_: name  # type: ignore[assignment]
    script = tmp_path / "server.py"
    script.write_text("import sys\nsys.exit(0)\n")
    before = set(redact._registered)
    CandidateVerifier(ctx).verify(
        Candidate(
            kind="stdio",
            name="x",
            command=[sys.executable, str(script)],
            env={"REGION_SETTING": "${SOME_UNSET_SETTING:-europe-west4}"},
        )
    )
    assert "europe-west4" not in set(redact._registered) - before


def test_packaged_settings_equal_the_code(settings) -> None:
    assert settings.bool_("provision.fresh_terminal.enabled") is True
    assert settings.str_list("provision.vertex.login_env") == ["GOOGLE_APPLICATION_CREDENTIALS"]
    assert settings.bool_("auth.google_cloud.impersonate_adc") is True
    assert settings.bool_("auth.claude_oauth.long_lived_token") is False
