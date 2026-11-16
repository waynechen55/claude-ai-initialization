"""Which Google Cloud credential decides whether phase 2 is satisfied.

Vertex AI -- and therefore phase 3 -- presents Application Default Credentials.
The ``gcloud`` CLI's own user credential is a different token with a different
lifetime, and on managed accounts it expires first. These tests pin the rule
that the credential actually used is the one that decides the verdict.
"""

from __future__ import annotations

import io

import pytest

from initialize_claude_ai.authenticate.base import AuthOutcome
from initialize_claude_ai.authenticate.google_cloud import GoogleCloudAuth
from initialize_claude_ai.core.config import Settings
from initialize_claude_ai.core.logging_setup import Console
from initialize_claude_ai.core.proc import CommandResult
from initialize_claude_ai.core.report import Status


def fake_probes(
    *,
    adc_ok: bool,
    user_ok: bool,
    account: str = "me@example.com",
    impersonating: str = "",
):
    """Stand in for the gcloud probes ``inspect`` makes."""

    def probe(argv, **_kwargs) -> CommandResult:
        argv = list(argv)
        joined = " ".join(argv)
        if "list" in argv and "--format=json" in argv:
            payload = f'[{{"account": "{account}", "status": "ACTIVE"}}]'
            return CommandResult(argv=argv, returncode=0, stdout=payload)
        if "auth/impersonate_service_account" in argv:
            return CommandResult(argv=argv, returncode=0, stdout=impersonating or "(unset)")
        if "get-value" in argv:
            return CommandResult(argv=argv, returncode=0, stdout="my-project")
        if "application-default" in joined:
            return CommandResult(argv=argv, returncode=0 if adc_ok else 1, stdout="tok" * 8)
        if "print-access-token" in joined:
            return CommandResult(
                argv=argv,
                returncode=0 if user_ok else 1,
                stdout="tok" * 8 if user_ok else "",
                stderr="" if user_ok else "Reauthentication failed.",
            )
        return CommandResult(argv=argv, returncode=0)

    return probe


@pytest.fixture
def backend(ctx, monkeypatch):
    auth = GoogleCloudAuth(ctx)
    monkeypatch.setattr(auth, "_gcloud", lambda: "/usr/bin/gcloud")
    return auth


def configure(backend: GoogleCloudAuth, monkeypatch, **overrides) -> None:
    """Pin ``backend.config`` to the shipped section plus ``overrides``."""
    section = backend.settings.section(backend.config_key)
    patched = Settings(data={**section.data, **overrides}, environ=section.environ)
    monkeypatch.setattr(type(backend), "config", property(lambda _self: patched))


def test_working_adc_is_enough(backend, monkeypatch) -> None:
    """Regression: a stale CLI session used to fail the phase and, with it,
    phase 3 -- while the ADC token Vertex reads was minting perfectly well."""
    monkeypatch.setattr(backend.runner, "probe", fake_probes(adc_ok=True, user_ok=False))
    outcome = backend.inspect()
    assert outcome.authenticated is True
    assert "stale" in outcome.detail
    assert any("gcloud auth login" in line for line in outcome.remediation)


def test_a_healthy_credential_says_nothing_about_staleness(backend, monkeypatch) -> None:
    monkeypatch.setattr(backend.runner, "probe", fake_probes(adc_ok=True, user_ok=True))
    outcome = backend.inspect()
    assert outcome.authenticated is True
    assert "stale" not in outcome.detail
    assert outcome.remediation == []


def test_missing_adc_is_not_authenticated(backend, monkeypatch) -> None:
    monkeypatch.setattr(backend.runner, "probe", fake_probes(adc_ok=False, user_ok=True))
    outcome = backend.inspect()
    assert outcome.authenticated is False
    assert "application-default" in outcome.detail


def test_the_user_credential_decides_when_adc_is_not_wanted(backend, monkeypatch) -> None:
    configure(backend, monkeypatch, configure_adc=False)
    monkeypatch.setattr(backend.runner, "probe", fake_probes(adc_ok=False, user_ok=True))
    assert backend.inspect().authenticated is True


def test_the_wrong_account_is_not_accepted(backend, monkeypatch) -> None:
    configure(backend, monkeypatch, account="other@example.com")
    monkeypatch.setattr(backend.runner, "probe", fake_probes(adc_ok=True, user_ok=True))
    outcome = backend.inspect()
    assert outcome.authenticated is False
    assert "other@example.com" in outcome.detail


SERVICE_ACCOUNT = "vertex@my-project.iam.gserviceaccount.com"


def test_a_named_service_account_is_required_even_on_an_authenticated_host(backend, monkeypatch) -> None:
    """--gcloud-service-account would otherwise be a no-op where it matters most.

    A machine with a perfectly valid personal credential passes every other
    check, so without this the impersonation would never be configured and the
    run would quietly proceed as the wrong identity.
    """
    configure(backend, monkeypatch, impersonate_service_account=SERVICE_ACCOUNT)
    monkeypatch.setattr(backend.runner, "probe", fake_probes(adc_ok=True, user_ok=True))
    outcome = backend.inspect()
    assert outcome.authenticated is False
    assert SERVICE_ACCOUNT in outcome.detail
    assert outcome.data["impersonate_service_account"] == SERVICE_ACCOUNT


def test_a_host_already_impersonating_it_is_satisfied(backend, monkeypatch) -> None:
    configure(backend, monkeypatch, impersonate_service_account=SERVICE_ACCOUNT)
    monkeypatch.setattr(
        backend.runner,
        "probe",
        fake_probes(adc_ok=True, user_ok=True, impersonating=SERVICE_ACCOUNT),
    )
    outcome = backend.inspect()
    assert outcome.authenticated is True
    assert "impersonating" not in outcome.detail


def test_the_wrong_account_is_reported_before_the_impersonation(backend, monkeypatch) -> None:
    """Two things are wrong; naming the first one is what the operator can act on."""
    configure(
        backend,
        monkeypatch,
        account="other@example.com",
        impersonate_service_account=SERVICE_ACCOUNT,
    )
    monkeypatch.setattr(backend.runner, "probe", fake_probes(adc_ok=True, user_ok=True))
    outcome = backend.inspect()
    assert outcome.authenticated is False
    assert "other@example.com" in outcome.detail


def test_no_named_service_account_means_no_extra_probe(backend, monkeypatch) -> None:
    """The common case must not pay for a check nobody asked for."""
    seen: list[list[str]] = []
    probe = fake_probes(adc_ok=True, user_ok=True)

    def recording(argv, **kwargs):
        seen.append(list(argv))
        return probe(argv, **kwargs)

    monkeypatch.setattr(backend.runner, "probe", recording)
    backend.inspect()
    assert not any("auth/impersonate_service_account" in argv for argv in seen)


def test_a_key_file_selects_the_service_account_method(backend, monkeypatch, tmp_path) -> None:
    key = tmp_path / "sa-key.json"
    key.write_text("{}", encoding="utf-8")
    configure(backend, monkeypatch, service_account_key_ref=f"file:{key}")
    assert backend._choose_method() == "service-account"


def test_a_named_key_file_is_required_even_on_an_authenticated_host(backend, monkeypatch, tmp_path) -> None:
    """Same trap as impersonation: the host's own credential is fine, so acquire
    would never run and the key would never be activated."""
    key = tmp_path / "sa-key.json"
    key.write_text("{}", encoding="utf-8")
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    configure(backend, monkeypatch, service_account_key_ref=f"file:{key}")
    monkeypatch.setattr(backend.runner, "probe", fake_probes(adc_ok=True, user_ok=True))
    outcome = backend.inspect()
    assert outcome.authenticated is False
    assert str(key) in outcome.detail
    assert outcome.data["service_account_key"] == str(key)


def test_a_key_already_in_effect_is_satisfied(backend, monkeypatch, tmp_path) -> None:
    key = tmp_path / "sa-key.json"
    key.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(key))
    configure(backend, monkeypatch, service_account_key_ref=f"file:{key}")
    monkeypatch.setattr(backend.runner, "probe", fake_probes(adc_ok=True, user_ok=True))
    assert backend.inspect().authenticated is True


def test_a_different_key_in_effect_is_not_satisfied(backend, monkeypatch, tmp_path) -> None:
    key = tmp_path / "sa-key.json"
    key.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(tmp_path / "some-other-key.json"))
    configure(backend, monkeypatch, service_account_key_ref=f"file:{key}")
    monkeypatch.setattr(backend.runner, "probe", fake_probes(adc_ok=True, user_ok=True))
    assert backend.inspect().authenticated is False


def test_a_world_readable_key_is_called_out(backend, monkeypatch, tmp_path, caplog) -> None:
    """gcloud opens the key itself, so the resolver's usual check is bypassed --
    a private key left at 0644 would otherwise pass without a word."""
    key = tmp_path / "sa-key.json"
    key.write_text("{}", encoding="utf-8")
    key.chmod(0o644)
    configure(backend, monkeypatch, service_account_key_ref=f"file:{key}")
    monkeypatch.setattr(backend.runner, "probe", fake_probes(adc_ok=True, user_ok=True))
    with caplog.at_level("WARNING"):
        backend._acquire_service_account("/usr/bin/gcloud", AuthOutcome(backend=backend.key))
    assert any("group/world accessible" in record.message for record in caplog.records)


def test_a_key_file_at_0600_says_nothing(backend, monkeypatch, tmp_path, caplog) -> None:
    key = tmp_path / "sa-key.json"
    key.write_text("{}", encoding="utf-8")
    key.chmod(0o600)
    configure(backend, monkeypatch, service_account_key_ref=f"file:{key}")
    monkeypatch.setattr(backend.runner, "probe", fake_probes(adc_ok=True, user_ok=True))
    with caplog.at_level("WARNING"):
        backend._acquire_service_account("/usr/bin/gcloud", AuthOutcome(backend=backend.key))
    assert not [r for r in caplog.records if "accessible" in r.message]


def test_a_missing_key_file_fails_with_a_way_out(backend, monkeypatch, tmp_path) -> None:
    configure(backend, monkeypatch, service_account_key_ref=f"file:{tmp_path / 'absent.json'}")
    outcome = backend._acquire_service_account("/usr/bin/gcloud", AuthOutcome(backend=backend.key))
    assert outcome.status == Status.FAILED
    assert any("service_account_key_ref" in line for line in outcome.remediation)


def test_a_missing_gcloud_is_a_failure_with_a_way_out(ctx, monkeypatch) -> None:
    auth = GoogleCloudAuth(ctx)
    monkeypatch.setattr(auth, "_gcloud", lambda: None)
    outcome = auth.inspect()
    assert outcome.authenticated is False
    assert any("--phases 1" in line for line in outcome.remediation)


# -- which sign-in flow, and whether the terminal says so ---------------------


def set_auth(backend, **overrides) -> None:
    """Patch the ``[auth]`` section on both the backend and its context.

    The flow is resolved through the context, so that the phase banner and every
    backend cannot disagree about which one is coming.
    """
    data = dict(backend.settings.data)
    data["auth"] = {**data.get("auth", {}), **overrides}
    patched = Settings(data=data, environ=backend.settings.environ)
    backend.settings = backend.ctx.settings = patched


def loud(backend) -> io.StringIO:
    """The shared ctx console is quiet; give this backend one that is not."""
    buffer = io.StringIO()
    backend.console = Console(stream=buffer, color=False)
    return buffer


def run_login(backend, monkeypatch, **settings_overrides) -> list[list[str]]:
    """Drive ``_run_login`` in dry-run and return the argv it would have used."""
    if settings_overrides:
        set_auth(backend, **settings_overrides)
    seen: list[list[str]] = []

    def record(argv, **_kwargs) -> CommandResult:
        seen.append(list(argv))
        return CommandResult(argv=list(argv), returncode=0)

    monkeypatch.setattr(backend.runner, "run", record)
    monkeypatch.setattr(backend.runner, "probe", fake_probes(adc_ok=True, user_ok=True))
    backend._run_login("/usr/bin/gcloud", AuthOutcome(backend=backend.key), user_login=True)
    return seen


def display(backend, monkeypatch, present: bool) -> None:
    """Decide what host detection sees, so ``auto`` is testable either way."""
    monkeypatch.setattr(type(backend.ctx.platform), "headless", property(lambda _self: not present))


def test_auto_takes_a_pasted_code_where_there_is_a_display(backend, monkeypatch) -> None:
    """A display proves a browser can be drawn, not that its redirect will get
    back to gcloud's listener; the code works either way."""
    display(backend, monkeypatch, True)
    for argv in run_login(backend, monkeypatch):
        assert "--no-launch-browser" in argv


def test_auto_pastes_a_code_where_there_is_no_display(backend, monkeypatch) -> None:
    """Nothing to launch, so the code has to be carried back by hand."""
    display(backend, monkeypatch, False)
    for argv in run_login(backend, monkeypatch):
        assert "--no-launch-browser" in argv


def test_headless_overrides_a_display_that_was_detected(backend, monkeypatch) -> None:
    """What detection cannot see: an X display forwarded from a session that is
    not really reachable, or a link that opens in somebody else's profile. The
    flag has to win, or it is useless in exactly the case it exists for."""
    display(backend, monkeypatch, True)
    for argv in run_login(backend, monkeypatch, browser_mode="headless"):
        assert "--no-launch-browser" in argv


def test_the_loopback_redirect_can_be_forced_where_no_display_was_detected(backend, monkeypatch) -> None:
    display(backend, monkeypatch, False)
    for argv in run_login(backend, monkeypatch, browser_mode="browser"):
        assert "--no-launch-browser" not in argv


def test_the_terminal_is_told_what_it_is_waiting_for(backend) -> None:
    """gcloud prints a URL and then goes quiet. Unannounced, that is
    indistinguishable from a hang -- and pasting the code back is exactly the
    step nobody guesses."""
    printed = loud(backend)
    backend._sign_in_hint(True, 2, AuthOutcome(backend=backend.key))
    printed = printed.getvalue()
    assert "paste" in printed.lower()
    assert "nothing happens on its own" in printed
    assert "2 times" in printed  # the account, then the ADC


def test_the_browser_flow_warns_that_it_may_not_come_back(backend) -> None:
    printed = loud(backend)
    backend._sign_in_hint(False, 1, AuthOutcome(backend=backend.key))
    printed = printed.getvalue()
    assert "redirect" in printed and "without --browser-loopback-redirect" in printed
    assert "Nothing to copy" in printed


def test_a_pipe_is_named_as_the_problem_before_the_wait_starts(backend) -> None:
    """Told at the end of a ten-minute timeout, this is useless."""
    printed = loud(backend)
    outcome = AuthOutcome(backend=backend.key)
    backend._sign_in_hint(True, 1, outcome)
    printed = printed.getvalue()
    # Under pytest stdin is not a tty, which is the case being described.
    assert "nowhere to paste" in printed
    assert any("not a tty" in line for line in outcome.remediation)


# -- how many times the operator is sent to a browser -------------------------


def steps(backend, monkeypatch, *, paste: bool = True, user_login: bool = True, **config):
    """The labels of the interactive commands a sign-in would actually run."""
    if config:
        configure(backend, monkeypatch, **config)
    return [label for label, _argv in backend._login_steps("/usr/bin/gcloud", paste=paste, user_login=user_login)]


def test_one_sign_in_is_enough_when_the_login_writes_adc(backend, monkeypatch) -> None:
    """Regression: the operator was sent to a browser twice, for one credential.

    `gcloud auth login --update-adc` writes the application-default credential
    itself, so the second login only overwrote it -- at the cost of a second
    URL that looked exactly like the first, on the same screen. Opening the
    stale one lands on a sign-in page that takes an email and then stops.
    """
    assert steps(backend, monkeypatch) == ["gcloud auth login"]


def test_dropping_the_flag_from_the_template_brings_the_adc_step_back(backend, monkeypatch) -> None:
    """The rule is read from the template, not assumed, so editing it decides."""
    section = backend.settings.section(backend.config_key)
    commands = {**section.data["commands"], "login_headless": ["{gcloud}", "auth", "login"]}
    patched = Settings(data={**section.data, "commands": commands}, environ=section.environ)
    monkeypatch.setattr(type(backend), "config", property(lambda _self: patched))
    assert steps(backend, monkeypatch) == [
        "gcloud auth login",
        "gcloud auth application-default login",
    ]


def test_adc_only_still_signs_in_for_adc(backend, monkeypatch) -> None:
    """Nothing covered ADC here, so the step that does must survive."""
    assert steps(backend, monkeypatch, user_login=False) == ["gcloud auth application-default login"]


def test_no_adc_wanted_means_just_the_user_login(backend, monkeypatch) -> None:
    assert steps(backend, monkeypatch, configure_adc=False) == ["gcloud auth login"]


def test_a_truncated_url_is_named_by_its_symptom(backend) -> None:
    """The page renders and then refuses to move, saying nothing about why.

    Whoever hits it blames Google, not their own copy, so the hint has to
    describe the symptom rather than the cause.
    """
    printed = loud(backend)
    backend._sign_in_hint(True, 1, AuthOutcome(backend=backend.key))
    printed = printed.getvalue()
    assert "goes nowhere" in printed and "cut short" in printed
    # And it points at the copy that was captured whole, rather than telling
    # anyone to select more carefully off a screen that wrapped the URL.
    assert "captured copy" in printed


# -- the project every gcloud command is run against --------------------------


def gcloud_runs(backend, monkeypatch) -> list[list[str]]:
    """Record the argv of every command ``runner.run`` is asked to execute."""
    seen: list[list[str]] = []

    def record(argv, **_kwargs) -> CommandResult:
        seen.append(list(argv))
        return CommandResult(argv=list(argv), returncode=0)

    monkeypatch.setattr(backend.runner, "run", record)
    return seen


def test_a_known_project_is_named_on_every_gcloud_command(backend, monkeypatch) -> None:
    """Signing in is not project-neutral. Quota is charged to a project, APIs
    are enabled on one and Vertex serves models from one, so a gcloud pointed
    somewhere else authenticates perfectly and then does all of that in the
    wrong place."""
    configure(backend, monkeypatch, project="proj-x")
    for name in ("login", "login_headless", "adc_login", "print_adc_token", "list_services"):
        argv = backend.command(name, {"gcloud": "/usr/bin/gcloud"})
        assert argv[-2:] == ["--project", "proj-x"], name


def test_the_project_comes_from_whichever_layer_supplied_it(backend, monkeypatch) -> None:
    """An env file, GOOGLE_CLOUD_PROJECT and --gcloud-project all arrive as one
    setting, so nothing in the backend has to know which was used."""
    section = backend.settings.section(backend.config_key)
    patched = Settings(
        data={**section.data, "project": "${GOOGLE_CLOUD_PROJECT:-}"},
        environ={"GOOGLE_CLOUD_PROJECT": "from-the-environment"},
    )
    monkeypatch.setattr(type(backend), "config", property(lambda _self: patched))
    argv = backend.command("login", {"gcloud": "/usr/bin/gcloud"})
    assert argv[-2:] == ["--project", "from-the-environment"]


def test_no_project_means_no_flag(backend, monkeypatch) -> None:
    """Nothing is invented: without a project, gcloud resolves its own."""
    configure(backend, monkeypatch, project="")
    assert "--project" not in backend.command("login", {"gcloud": "/usr/bin/gcloud"})


def test_the_commands_that_name_a_project_themselves_are_left_alone(backend, monkeypatch) -> None:
    """The two that set it take it as their argument, and the one that reads it
    would be handed the answer and echo it back."""
    configure(backend, monkeypatch, project="proj-x")
    for name in ("get_project", "set_project", "set_quota_project"):
        argv = backend.command(name, {"gcloud": "/usr/bin/gcloud", "project": "proj-x"})
        assert argv[-2:] != ["--project", "proj-x"], name


def test_a_template_that_already_names_one_wins(backend, monkeypatch) -> None:
    """Whoever wrote --project into a template meant it."""
    section = backend.settings.section(backend.config_key)
    commands = {**section.data["commands"], "login": ["{gcloud}", "auth", "login", "--project=pinned"]}
    patched = Settings(
        data={**section.data, "project": "proj-x", "commands": commands},
        environ=section.environ,
    )
    monkeypatch.setattr(type(backend), "config", property(lambda _self: patched))
    argv = backend.command("login", {"gcloud": "/usr/bin/gcloud"})
    assert argv.count("--project=pinned") == 1 and "--project" not in argv


def test_emptying_project_args_hands_resolution_back_to_gcloud(backend, monkeypatch) -> None:
    configure(backend, monkeypatch, project="proj-x", project_args=[])
    assert "--project" not in backend.command("login", {"gcloud": "/usr/bin/gcloud"})


def test_inspect_reports_gcloud_s_own_project_not_the_configured_one(backend, monkeypatch) -> None:
    """Regression: with --project appended to the accessor, gcloud reads the
    flag back and the difference the phase is looking for disappears."""
    configure(backend, monkeypatch, project="wanted-project")
    monkeypatch.setattr(backend.runner, "probe", fake_probes(adc_ok=True, user_ok=True))
    assert backend.inspect().data["project"] == "my-project"


def test_a_host_already_signed_in_is_still_moved_to_the_project(backend, monkeypatch) -> None:
    """A project can be named on a run that acquires nothing. Skipping it there
    would leave the machine authenticated against one project and pointed at
    another -- and phase 3, which asks gcloud, would follow the wrong one."""
    configure(backend, monkeypatch, project="wanted-project")
    monkeypatch.setattr(backend.runner, "probe", fake_probes(adc_ok=True, user_ok=True))
    seen = gcloud_runs(backend, monkeypatch)
    outcome = AuthOutcome(backend=backend.key, authenticated=True, data={"project": "my-project"})
    backend.verify(outcome)
    assert ["/usr/bin/gcloud", "config", "set", "project", "wanted-project"] in seen
    assert outcome.data["project"] == "wanted-project"
    assert "wanted-project" in outcome.detail


def test_a_host_already_on_the_project_is_left_alone(backend, monkeypatch) -> None:
    configure(backend, monkeypatch, project="my-project")
    monkeypatch.setattr(backend.runner, "probe", fake_probes(adc_ok=True, user_ok=True))
    seen = gcloud_runs(backend, monkeypatch)
    backend.verify(AuthOutcome(backend=backend.key, authenticated=True, data={"project": "my-project"}))
    assert not [argv for argv in seen if "set" in argv]


def test_a_fresh_sign_in_writes_the_quota_project_even_when_nothing_moved(backend, monkeypatch) -> None:
    """A new credential means a new ADC file, whose quota project starts empty
    however long gcloud has been pointed at the right place."""
    configure(backend, monkeypatch, project="my-project")
    seen = gcloud_runs(backend, monkeypatch)
    backend._apply_config(
        "/usr/bin/gcloud",
        AuthOutcome(backend=backend.key, data={"project": "my-project"}),
    )
    assert [argv for argv in seen if "set-quota-project" in argv]
