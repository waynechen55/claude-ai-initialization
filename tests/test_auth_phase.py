"""Phase 2's two passes: detect everything first, then sign in only where needed.

Detection is separated from acquisition so that an interactive sign-in on the
first backend cannot block before the second has been looked at -- the operator
sees the whole picture before answering anything. And a credential that is
already present is left alone, so re-running phase 2 costs nothing and steals
nobody's session. ``--force-auth`` overrides the second half, never the first.

``--force-auth-with-reset`` adds a third pass in the middle, which is where it
has to be: detection is the only record of what was there, and the sign-in has
to run against a machine that no longer holds a credential. These tests pin that
order, the confirmation in front of it, and the re-inspection behind it.
"""

from __future__ import annotations

import pytest

from initialize_claude_ai import authenticate as auth_registry
from initialize_claude_ai.authenticate.base import AuthBackend, AuthOutcome, CredentialReset
from initialize_claude_ai.core.report import PhaseResult, Status
from initialize_claude_ai.phases.phase2_authenticate import AuthPhase


class RecordingBackend(AuthBackend):
    """A backend that records the order of its three beats."""

    config_key = "auth.recording"

    def __init__(self, ctx, log: list[str], *, key: str, authenticated: bool) -> None:
        super().__init__(ctx)
        self.key = key
        self.title = f"backend {key}"
        self.log = log
        self.already = authenticated
        #: What ``clear_credentials`` will report, and whether it works.
        self.reset_report = CredentialReset(backend=key, cleared=[f"revoked {key}"])
        #: Whether the credential survives the reset -- an API key in somebody's
        #: shell does, and saying so is the point of the re-inspection.
        self.survives = False

    def clear_credentials(self) -> CredentialReset:
        self.log.append(f"clear:{self.key}")
        if not self.survives:
            self.already = False
        return self.reset_report

    def is_enabled(self) -> bool:
        return True

    def inspect(self) -> AuthOutcome:
        self.log.append(f"inspect:{self.key}")
        return AuthOutcome(
            backend=self.key,
            authenticated=self.already,
            identity=f"{self.key}@example.com" if self.already else "",
            detail="existing credential" if self.already else "nothing found",
        )

    def acquire(self, current: AuthOutcome) -> AuthOutcome:
        self.log.append(f"acquire:{self.key}")
        current.authenticated = True
        current.status = Status.CHANGED
        current.detail = "signed in"
        return current

    def verify(self, current: AuthOutcome) -> AuthOutcome:
        self.log.append(f"verify:{self.key}")
        current.verified = True
        return current


@pytest.fixture
def run_phase(ctx, monkeypatch):
    """Run phase 2 over two fake backends and return (steps, call log)."""

    def go(
        *,
        first: bool,
        second: bool,
        force: bool = False,
        reset: bool = False,
        agree: bool = True,
        live: bool = False,
        prepare=None,
    ) -> tuple[PhaseResult, list[str]]:
        calls: list[str] = []
        backends = [
            RecordingBackend(ctx, calls, key="one", authenticated=first),
            RecordingBackend(ctx, calls, key="two", authenticated=second),
        ]
        if prepare is not None:
            prepare(backends)
        ctx.auto_agree = agree
        if live:
            # The survivor check only means anything when something was really
            # cleared, so that one test leaves dry-run mode behind.
            ctx.dry_run = False
        monkeypatch.setattr(auth_registry, "resolve_keys", lambda *_a, **_k: ["one", "two"])
        monkeypatch.setattr(auth_registry, "build", lambda *_a, **_k: backends)
        answers = {"auth.force": force or reset, "auth.reset": reset}
        monkeypatch.setattr(
            type(ctx.settings),
            "bool_",
            lambda self, key, default=False: answers.get(key, default),
        )
        phase = AuthPhase(ctx)
        result = PhaseResult(phase="auth", title="Authenticate")
        phase.execute(result)
        return result, calls

    return go


def steps(result: PhaseResult) -> dict[str, object]:
    return {step.name: step for step in result.steps}


def test_every_backend_is_detected_before_any_is_acquired(run_phase) -> None:
    """The point of the split: no sign-in prompt appears until the whole
    picture is known."""
    _result, calls = run_phase(first=False, second=False)
    assert calls[:2] == ["inspect:one", "inspect:two"]
    assert calls.index("inspect:two") < calls.index("acquire:one")


def test_an_existing_credential_is_not_acquired_again(run_phase) -> None:
    _result, calls = run_phase(first=True, second=False)
    assert "acquire:one" not in calls
    assert "acquire:two" in calls


def test_nothing_is_acquired_when_everything_is_already_authenticated(run_phase) -> None:
    """Re-running phase 2 is a no-op."""
    _result, calls = run_phase(first=True, second=True)
    assert not [c for c in calls if c.startswith("acquire:")]


def test_force_auth_signs_in_again_anyway(run_phase) -> None:
    _result, calls = run_phase(first=True, second=True, force=True)
    assert [c for c in calls if c.startswith("acquire:")] == ["acquire:one", "acquire:two"]


def test_detection_still_runs_under_force_auth(run_phase) -> None:
    """--force-auth changes what is done about the answer, not whether it is asked."""
    result, calls = run_phase(first=True, second=True, force=True)
    assert calls[:2] == ["inspect:one", "inspect:two"]
    assert steps(result)["detect"].data["force"] is True


def test_detection_is_reported_before_the_backends(run_phase) -> None:
    result, _calls = run_phase(first=True, second=False)
    # The browser is settled first of all -- once, for every backend that will
    # go on to open one -- and only then is anything inspected.
    assert [step.name for step in result.steps][:2] == ["browser", "detect"]
    detect = result.steps[1]
    assert detect.data["already_authenticated"] == ["one"]
    assert detect.data["needs_authentication"] == ["two"]
    assert detect.data["identities"] == {"one": "one@example.com"}
    assert "already authenticated: one" in detect.detail
    assert "not yet: two" in detect.detail


# ---------------------------------------------------------------------------
# --force-auth-with-reset: the third pass
# ---------------------------------------------------------------------------


def test_the_reset_happens_between_detection_and_the_sign_in(run_phase) -> None:
    """The order is the whole design. Detection has to see the credential before
    it is destroyed -- it is the only record of what was there -- and the sign-in
    has to run against a machine that no longer has one."""
    _result, calls = run_phase(first=True, second=True, reset=True)
    assert calls.index("inspect:one") < calls.index("clear:one") < calls.index("acquire:one")
    assert calls.index("clear:two") < calls.index("acquire:one"), "cleared one at a time"


def test_nothing_is_cleared_without_the_flag(run_phase) -> None:
    result, calls = run_phase(first=True, second=True, force=True)
    assert not [c for c in calls if c.startswith("clear:")]
    assert "reset" not in steps(result)


def test_a_declined_reset_keeps_the_credentials(run_phase) -> None:
    """Answering no must leave the machine exactly as it was found."""
    result, calls = run_phase(first=True, second=True, reset=True, agree=False)
    assert not [c for c in calls if c.startswith("clear:")]
    step = steps(result)["reset"]
    assert step.status == Status.SKIPPED
    assert step.data["declined"] is True
    # Declining the destruction does not decline the sign-in: --force-auth-with-reset
    # still asked for one, and the operator only said no to throwing things away.
    assert [c for c in calls if c.startswith("acquire:")] == ["acquire:one", "acquire:two"]


def test_there_is_nothing_to_clear_on_an_unauthenticated_machine(run_phase) -> None:
    result, calls = run_phase(first=False, second=False, reset=True)
    assert not [c for c in calls if c.startswith("clear:")]
    assert steps(result)["reset"].status == Status.SKIPPED


def test_only_the_backends_that_hold_something_are_cleared(run_phase) -> None:
    _result, calls = run_phase(first=True, second=False, reset=True)
    assert "clear:one" in calls and "clear:two" not in calls


def test_the_machine_is_inspected_again_after_being_cleared(run_phase) -> None:
    """What the sign-in pass acts on has to be the state after the reset, not
    the stale answer from before it."""
    _result, calls = run_phase(first=True, second=True, reset=True)
    assert calls.count("inspect:one") == 2
    assert calls.index("clear:one") < calls.index("inspect:one", calls.index("clear:one"))


def test_a_credential_that_survives_the_reset_is_reported(run_phase) -> None:
    """An API key exported in somebody's shell outlives anything done here, and
    saying nothing would leave them to discover it when the sign-in is skipped."""

    def prepare(backends) -> None:
        backends[0].survives = True

    result, _calls = run_phase(first=True, second=True, reset=True, live=True, prepare=prepare)
    step = steps(result)["reset"]
    assert step.data["still_authenticated"] == ["one (existing credential)"]
    assert any("Still authenticated after the reset" in line for line in step.remediation)


def test_a_reset_that_could_not_clear_everything_is_not_reported_as_clean(run_phase) -> None:
    def prepare(backends) -> None:
        backends[0].reset_report = CredentialReset(backend="one", failed=["/tmp/creds.json: Permission denied"])

    result, _calls = run_phase(first=True, second=True, reset=True, prepare=prepare)
    step = steps(result)["reset"]
    assert step.status == Status.PARTIAL
    assert "one: /tmp/creds.json: Permission denied" in step.detail


def test_the_banner_says_the_credentials_will_be_cleared(ctx, monkeypatch) -> None:
    monkeypatch.setattr(
        type(ctx.settings),
        "bool_",
        lambda self, key, default=False: key in ("auth.force", "auth.reset") or default,
    )
    facts = dict(AuthPhase(ctx).banner_facts())
    assert "--force-auth-with-reset" in facts["Existing"]


def test_a_detected_credential_is_still_verified(run_phase) -> None:
    """Not acquiring is not the same as not checking: phase 3 needs to know the
    credential still works, not merely that it exists."""
    _result, calls = run_phase(first=True, second=True)
    assert "verify:one" in calls and "verify:two" in calls
