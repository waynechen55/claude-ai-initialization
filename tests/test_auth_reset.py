"""Clearing a credential: what ``--force-auth-with-reset`` actually removes.

The machinery is deliberately generic -- a backend declares ``reset.commands``
and ``reset.paths``/``reset.paths_from`` in the packaged defaults, and the base
class does the rest -- so these tests come in two halves. The first pins the
behaviour of that machinery against a made-up backend: what runs, what is
removed, what a dry run promises instead, and the fact that nothing here is ever
fatal. The second pins the real backends' configuration, because a reset that
revokes the wrong thing, or forgets the file ``inspect`` looks at, is a bug
nobody would see until the day they needed the flag.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from initialize_claude_ai import authenticate as auth_registry
from initialize_claude_ai.authenticate.base import AuthBackend, AuthOutcome
from initialize_claude_ai.core.config import Settings
from initialize_claude_ai.core.proc import CommandResult


class FakeRunner:
    """Records what was run and answers with whatever was asked for."""

    def __init__(self, returncode: int = 0, simulated: bool = False) -> None:
        self.calls: list[list[str]] = []
        self.returncode = returncode
        self.simulated = simulated
        self.dry_run = simulated

    def run(self, argv, **_kwargs) -> CommandResult:
        argv = [str(a) for a in argv]
        self.calls.append(argv)
        return CommandResult(argv=argv, returncode=self.returncode, simulated=self.simulated)


class FakeBackend(AuthBackend):
    key = "fake"
    title = "fake backend"
    config_key = "auth.fake"

    def command_tokens(self) -> dict[str, str]:
        return {"bin": "/usr/bin/fake"}

    def inspect(self) -> AuthOutcome:  # never called here
        return AuthOutcome(backend=self.key)


def make(ctx, *, runner=None, dry_run=True, **fake) -> FakeBackend:
    """A backend whose ``[auth.fake]`` section is whatever the test needs."""
    data = dict(ctx.settings.data)
    auth = dict(data.get("auth", {}))
    auth["fake"] = fake
    data["auth"] = auth
    ctx.settings = Settings(data=data, environ=ctx.settings.environ)
    ctx.runner = runner if runner is not None else FakeRunner(simulated=dry_run)
    ctx.dry_run = dry_run
    return FakeBackend(ctx)


# ---------------------------------------------------------------------------
# The machinery
# ---------------------------------------------------------------------------


def test_the_configured_commands_run_in_the_order_they_are_listed(ctx) -> None:
    """Order is configuration, not an accident: gcloud's application-default
    credential goes before the login that can mint another one."""
    runner = FakeRunner()
    backend = make(
        ctx,
        runner=runner,
        dry_run=False,
        commands={"first": ["{bin}", "one"], "second": ["{bin}", "two"]},
        reset={"commands": ["second", "first"]},
    )
    report = backend.clear_credentials()
    assert runner.calls == [["/usr/bin/fake", "two"], ["/usr/bin/fake", "one"]]
    assert report.cleared == ["two", "one"], "the binary path says nothing worth printing"
    assert report.changed


def test_a_command_whose_binary_is_missing_is_a_note_not_a_run(ctx) -> None:
    """An unresolved ``{bin}`` renders to an empty argument, and running that
    would be running something nobody asked for."""

    class NoBinary(FakeBackend):
        def command_tokens(self) -> dict[str, str]:
            return {"bin": ""}

    runner = FakeRunner()
    make(ctx, runner=runner, dry_run=False, commands={"go": ["{bin}", "x"]}, reset={"commands": ["go"]})
    report = NoBinary(ctx).clear_credentials()
    assert runner.calls == []
    assert report.changed is False
    assert "go: no command to run" in report.notes


def test_a_command_that_fails_is_not_fatal(ctx) -> None:
    """Very often it failed because there was nothing signed in, which is the
    outcome that was wanted. The return code is recorded, not judged -- and the
    sign-in that follows must still happen."""
    backend = make(
        ctx,
        runner=FakeRunner(returncode=1),
        dry_run=False,
        commands={"go": ["{bin}", "revoke"]},
        reset={"commands": ["go"]},
    )
    report = backend.clear_credentials()
    assert report.failed == []
    assert report.notes == ["revoke exited 1"]
    assert report.detail() == "revoke exited 1"


def test_a_dry_run_promises_rather_than_claims(ctx) -> None:
    """The runner answers a skipped command with success. Reporting that as a
    revoke that happened would be the one lie a --dry-run must not tell."""
    backend = make(ctx, commands={"go": ["{bin}", "revoke"]}, reset={"commands": ["go"]}, dry_run=True)
    assert backend.clear_credentials().cleared == ["would run revoke"]


def test_the_files_cleared_are_the_files_inspect_looks_at(ctx, tmp_path: Path) -> None:
    """``paths_from`` names a sibling setting instead of copying its list, so the
    two can never drift apart -- which is how a "cleared" credential stays
    detected."""
    credential = tmp_path / ".credentials.json"
    credential.write_text("{}", encoding="utf-8")
    backend = make(
        ctx,
        dry_run=False,
        credential_paths=[str(credential)],
        reset={"paths_from": ["credential_paths"]},
    )
    assert backend.reset_paths() == [credential]
    report = backend.clear_credentials()
    assert not credential.exists()
    assert report.cleared == [f"removed {credential}"]


def test_a_path_that_is_not_there_is_not_worth_mentioning(ctx, tmp_path: Path) -> None:
    backend = make(ctx, dry_run=False, reset={"paths": [str(tmp_path / "absent")]})
    report = backend.clear_credentials()
    assert report.cleared == [] and report.failed == []
    assert report.detail() == "nothing to clear"


def test_a_dry_run_removes_nothing(ctx, tmp_path: Path) -> None:
    credential = tmp_path / "creds.json"
    credential.write_text("{}", encoding="utf-8")
    backend = make(ctx, dry_run=True, reset={"paths": [str(credential)]})
    report = backend.clear_credentials()
    assert credential.exists()
    assert report.cleared == [f"would remove {credential}"]


def test_a_credential_directory_goes_whole(ctx, tmp_path: Path) -> None:
    store = tmp_path / "store"
    (store / "inner").mkdir(parents=True)
    (store / "inner" / "token").write_text("x", encoding="utf-8")
    backend = make(ctx, dry_run=False, reset={"paths": [str(store)]})
    backend.clear_credentials()
    assert not store.exists()


def test_deleting_is_the_default_because_a_backup_is_still_a_credential(ctx, tmp_path: Path) -> None:
    """Everywhere else in this program a file is moved aside before it is
    replaced. Not here: leaving a readable copy beside the file somebody just
    asked to have cleared would defeat the request it was made in answer to."""
    store = tmp_path / "secrets"
    store.mkdir()
    credential = store / "creds.json"
    credential.write_text("{}", encoding="utf-8")
    backend = make(ctx, dry_run=False, reset={"paths": [str(credential)]})
    backend.clear_credentials()
    assert list(store.iterdir()) == [], "no .bak copy of a credential is left behind"


def test_the_way_back_is_available_to_whoever_asks_for_it(ctx, tmp_path: Path) -> None:
    credential = tmp_path / "creds.json"
    credential.write_text("{}", encoding="utf-8")
    backend = make(ctx, dry_run=False, reset={"paths": [str(credential)]})
    data = dict(backend.settings.data)
    data["auth"] = {**data["auth"], "reset_backup": True, "reset_backup_suffix": "stamp"}
    backend.settings = Settings(data=data, environ=backend.settings.environ)
    report = backend.clear_credentials()
    assert not credential.exists()
    assert (tmp_path / "creds.json.stamp.bak").exists()
    assert report.cleared == [f"{credential} -> creds.json.stamp.bak"]


def test_a_file_that_will_not_go_is_reported_rather_than_raised(
    ctx, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A credential this program cannot remove must not stop the sign-in that
    was going to work; the operator is told what is still there instead.

    The refusal is injected rather than arranged with file permissions, because
    the suite runs as root about as often as this program does, and root is not
    told no by a mode bit."""
    credential = tmp_path / "creds.json"
    credential.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(Path, "unlink", lambda *_a, **_k: (_ for _ in ()).throw(PermissionError("read-only")))
    backend = make(ctx, dry_run=False, reset={"paths": [str(credential)]})
    report = backend.clear_credentials()
    assert credential.exists()
    assert report.changed is False
    assert report.failed == [f"{credential}: read-only"]
    assert "could not clear" in report.detail()


def test_a_backend_with_nothing_configured_clears_nothing(ctx) -> None:
    """The honest answer for a credential this program never stored."""
    backend = make(ctx, dry_run=False, runner=FakeRunner())
    report = backend.clear_credentials()
    assert report.changed is False
    assert report.detail() == "nothing to clear"


def test_a_leftover_credential_file_counts_even_if_nothing_is_authenticated(ctx, tmp_path: Path) -> None:
    """ "Is anything authenticated" and "is there anything here" are different
    questions, and the reset asks the second one."""
    credential = tmp_path / "creds.json"
    backend = make(ctx, dry_run=False, reset={"paths": [str(credential)]})
    assert backend.holds_credential(AuthOutcome(backend="fake")) is False
    credential.write_text("{}", encoding="utf-8")
    assert backend.holds_credential(AuthOutcome(backend="fake")) is True


# ---------------------------------------------------------------------------
# What the real backends are configured to clear
# ---------------------------------------------------------------------------


def real(ctx, key: str) -> AuthBackend:
    return {backend.key: backend for backend in auth_registry.build(ctx, [key])}[key]


def test_google_cloud_revokes_the_credential_vertex_reads_first(ctx) -> None:
    backend = real(ctx, "google-cloud")
    assert backend.config.str_list("reset.commands") == ["revoke_adc", "revoke"]
    argv = backend.command("revoke_adc", {"gcloud": "gcloud"})
    assert argv[:4] == ["gcloud", "auth", "application-default", "revoke"]


def test_a_revoke_is_never_narrowed_to_one_project(ctx) -> None:
    """--project on a revoke would suggest the credential belongs to a project.
    It belongs to an account, and revoking it signs the account out everywhere."""
    data = dict(ctx.settings.data)
    auth = dict(data["auth"])
    auth["google_cloud"] = {**auth["google_cloud"], "project": "some-project"}
    data["auth"] = auth
    ctx.settings = Settings(data=data, environ=ctx.settings.environ)
    backend = real(ctx, "google-cloud")
    for name in backend.config.str_list("reset.commands"):
        assert "--project" not in backend.command(name, {"gcloud": "gcloud"}), name


def test_a_machine_signed_in_as_the_wrong_person_is_cleared_too(ctx) -> None:
    """It reports itself unauthenticated -- proceeding as the wrong user is the
    failure that check exists to prevent -- while holding a perfectly good
    credential for somebody else. Skipping it would skip the very host the flag
    was invented for."""
    backend = real(ctx, "google-cloud")
    wrong = AuthOutcome(backend="google-cloud", authenticated=False)
    wrong.data["accounts"] = ["somebody-else@example.com"]
    assert backend.holds_credential(wrong) is True
    assert backend.holds_credential(AuthOutcome(backend="google-cloud")) is False


def test_google_cloud_deletes_no_files_of_its_own(ctx) -> None:
    """gcloud owns ~/.config/gcloud; reaching into it behind its back is how a
    half-deleted credential store happens. A service-account key file is the
    operator's property and is not ours to remove either."""
    assert real(ctx, "google-cloud").reset_paths() == []


def test_claude_logs_out_and_then_checks_the_logout_worked(ctx) -> None:
    backend = real(ctx, "claude-oauth")
    assert backend.command("logout_command", {"claude": "claude"}) == ["claude", "auth", "logout"]
    cleared = {str(p) for p in backend.reset_paths()}
    inspected = {str(Path(p).expanduser()) for p in backend.config.str_list("credential_paths")}
    assert cleared == inspected and cleared, "the two lists are the same list"


def test_the_anthropic_api_key_is_somebody_elses_property(ctx) -> None:
    """It is reached by reference -- an exported variable, a file the operator
    wrote. Clearing a reference would mean deleting something this program never
    stored and does not own."""
    backend = real(ctx, "anthropic-api")
    assert backend.config.str_list("reset.commands") == []
    assert backend.reset_paths() == []
    assert backend.clear_credentials().detail() == "nothing to clear"
