"""Secret references: resolution, dispatch and non-disclosure.

The rule these tests defend is the project's hardest constraint -- a credential
value must never reach a log line, a report, a repr or a config file.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from initialize_claude_ai.core import redact
from initialize_claude_ai.core.errors import SecretResolutionError
from initialize_claude_ai.core.proc import Runner
from initialize_claude_ai.core.secrets import Secret, SecretResolver


@pytest.fixture
def resolver() -> SecretResolver:
    return SecretResolver(Runner(dry_run=True), allow_prompt=False)


def test_bare_name_means_environment_variable(resolver: SecretResolver, monkeypatch) -> None:
    monkeypatch.setenv("MY_TOKEN", "value-from-env")
    secret = resolver.resolve("MY_TOKEN")
    assert secret is not None and secret.reveal() == "value-from-env"
    assert resolver.describe("MY_TOKEN") == "env:MY_TOKEN"


def test_argument_less_scheme_is_not_read_as_a_variable_name(resolver: SecretResolver) -> None:
    """'gcloud-adc' has no colon; it must still dispatch to the gcloud handler.

    Regression: it used to be treated as ``env:gcloud-adc`` -- an environment
    variable that cannot exist -- so Vertex discovery silently found no token.
    """
    scheme, remainder = SecretResolver._split("gcloud-adc")
    assert (scheme, remainder) == ("gcloud-adc", "")
    assert SecretResolver._split("gcloud-token")[0] == "gcloud-token"
    assert SecretResolver._split("SOME_VAR") == ("env", "SOME_VAR")
    assert SecretResolver._split("file:/tmp/t") == ("file", "/tmp/t")


def test_gcloud_reference_without_gcloud_is_an_error(resolver: SecretResolver, monkeypatch) -> None:
    monkeypatch.setenv("PATH", "")
    with pytest.raises(SecretResolutionError):
        resolver.resolve("gcloud-adc")


def test_file_reference_strips_whitespace(resolver: SecretResolver, tmp_path: Path) -> None:
    path = tmp_path / "token"
    path.write_text("  secret-in-file \n", encoding="utf-8")
    path.chmod(0o600)
    secret = resolver.resolve(f"file:{path}")
    assert secret is not None and secret.reveal() == "secret-in-file"


def test_loose_file_permissions_warn_but_still_resolve(resolver: SecretResolver, tmp_path: Path, caplog) -> None:
    path = tmp_path / "loose"
    path.write_text("x" * 20, encoding="utf-8")
    path.chmod(0o644)
    assert path.stat().st_mode & stat.S_IROTH
    with caplog.at_level("WARNING"):
        assert resolver.resolve(f"file:{path}") is not None
    assert any("group/world" in r.message for r in caplog.records)


def test_missing_reference_is_none_unless_required(resolver: SecretResolver) -> None:
    assert resolver.resolve(None) is None
    assert resolver.resolve("none") is None
    with pytest.raises(SecretResolutionError):
        resolver.resolve(None, required=True, label="api key")


def test_unknown_scheme_is_rejected(resolver: SecretResolver) -> None:
    with pytest.raises(SecretResolutionError):
        resolver.resolve("smoke-signal:token")


def test_executing_schemes_can_be_disabled(tmp_path: Path) -> None:
    strict = SecretResolver(Runner(dry_run=True), allow_execution=False)
    with pytest.raises(SecretResolutionError):
        strict.resolve("command:echo hi")


def test_command_reference_runs_even_in_dry_run() -> None:
    """Reading a credential mutates nothing, so a dry run must still resolve it."""
    resolver = SecretResolver(Runner(dry_run=True))
    secret = resolver.resolve("command:printf token-from-command")
    assert secret is not None and secret.reveal() == "token-from-command"


def test_first_available_skips_unusable_references(resolver: SecretResolver, monkeypatch) -> None:
    monkeypatch.delenv("ABSENT_ONE", raising=False)
    monkeypatch.setenv("PRESENT_ONE", "here")
    secret = resolver.first_available(["env:ABSENT_ONE", "env:PRESENT_ONE"])
    assert secret is not None and secret.reveal() == "here"


def test_prompt_refuses_without_a_terminal() -> None:
    resolver = SecretResolver(Runner(dry_run=True), allow_prompt=False)
    with pytest.raises(SecretResolutionError):
        resolver.resolve("prompt:API key")


def test_describe_never_reveals_a_literal() -> None:
    assert SecretResolver.describe("literal:hunter2") == "literal:<inline>"


def test_secret_does_not_render_itself() -> None:
    secret = Secret("super-secret-value", source="env:X")
    assert str(secret) == redact.MASK
    assert "super-secret-value" not in repr(secret)
    assert secret.fingerprint.startswith("sha256:")
    assert len(secret) == len("super-secret-value")


def test_resolving_registers_the_value_for_redaction(monkeypatch) -> None:
    monkeypatch.setenv("REDACT_ME", "abcdef123456")
    resolver = SecretResolver(Runner(dry_run=True))
    assert resolver.resolve("env:REDACT_ME") is not None
    assert "abcdef123456" not in redact.redact("token is abcdef123456")


def test_environment_is_not_mutated_by_resolution(resolver: SecretResolver, monkeypatch) -> None:
    before = dict(os.environ)
    monkeypatch.setenv("SOME_TOKEN", "value")
    resolver.resolve("env:SOME_TOKEN")
    monkeypatch.delenv("SOME_TOKEN")
    assert dict(os.environ) == before
