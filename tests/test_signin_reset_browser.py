"""A reset has to reach the browser the sign-in actually happens in.

The failure these guard against: ``--force-auth-with-reset --headless`` revoked
every credential on the machine and emptied the program's own browser profile
-- and the operator was still signed straight in, no email, no password. The
pasted-code flow opens no browser, so the emptied profile went unused; the URL
went into their everyday browser, which still held the provider's session.

The first fix opened the captured URL in the emptied profile after a reset --
and that was a second bug: a pasted-code flow on a host with a display exists
only under an explicit ``--headless``, whose whole meaning is "I will open the
URL myself", so Firefox opened for exactly the operator who had asked for none.

What is pinned down here: under ``--headless`` a pasted-code sign-in opens
nothing unless the operator opted in (``auth.signin.open_url = always``), reset
or not, and its sign-in command cannot reach the exported browser either; after
a reset it says plainly what the reset did not reach and gives commands to run
for a sign-in that asks. Without ``--headless`` every sign-in still takes a
pasted code -- the loopback redirect could not be relied on to come back -- and
the whole URL is opened once, in the browser found for the run.
"""

from __future__ import annotations

import dataclasses
import os
import subprocess
import sys

import pytest

from initialize_claude_ai.authenticate.browser import (
    STATE_KEY,
    BrowserPlan,
    planned_browser,
    prepare_browser,
    private_window_command,
)
from initialize_claude_ai.authenticate.signin_url import SignInUrlRelay
from initialize_claude_ai.core.config import load_settings
from initialize_claude_ai.core.context import RunContext
from initialize_claude_ai.core.proc import CommandResult, Runner

URL = (
    "https://accounts.google.com/o/oauth2/auth?response_type=code&client_id=32555940559.apps."
    "googleusercontent.com&redirect_uri=https%3A%2F%2Fsdk.cloud.google.com%2Fauthcode.html"
    "&scope=openid&state=8Qb2Lm&access_type=offline&code_challenge_method=S256"
)
PROFILE = "/state/browser-profile/firefox"
OWN = BrowserPlan(
    command=f"/usr/bin/firefox --profile {PROFILE} --no-remote %s",
    path="/usr/bin/firefox",
    profile_dir=PROFILE,
    exportable=True,
)


def configure(ctx: RunContext, *overrides: str) -> None:
    ctx.settings = load_settings(overrides=list(overrides), environ=dict(os.environ))


@pytest.fixture
def launches(ctx: RunContext, monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Every browser the relay starts, instead of starting it."""
    seen: list[list[str]] = []

    def launch(argv, *, settle=0.0, label=None):
        seen.append(list(argv))
        return CommandResult(argv=list(argv), returncode=0)

    monkeypatch.setattr(ctx.runner, "launch", launch)
    return seen


@pytest.fixture
def after_reset(ctx: RunContext, monkeypatch: pytest.MonkeyPatch) -> RunContext:
    """The reported run: --headless, reset, a display, a pty, and our own emptied profile."""
    configure(ctx, "auth.reset=true", "auth.browser_mode=headless", "auth.signin.pty=always")
    ctx.platform = dataclasses.replace(ctx.platform, has_display=True)
    ctx.state[STATE_KEY] = OWN
    return ctx


@pytest.fixture
def opted_in(after_reset: RunContext) -> RunContext:
    """The reported run, with the operator having asked for the URL to be opened."""
    configure(
        after_reset,
        "auth.reset=true",
        "auth.browser_mode=headless",
        "auth.signin.pty=always",
        "auth.signin.open_url=always",
    )
    return after_reset


def pasted(ctx: RunContext) -> SignInUrlRelay:
    return SignInUrlRelay(ctx, paste=True)


# -- --headless means the operator opens the URL -------------------------------


def test_a_headless_reset_opens_no_browser(after_reset, launches) -> None:
    """The reported run: --force-auth-with-reset --headless, with a display."""
    configure(after_reset, "auth.reset=true", "auth.browser_mode=headless", "auth.signin.pty=always")
    assert after_reset.uses_pasted_code()
    relay = pasted(after_reset)
    relay.feed(f"Go to the following link in your browser:\n\n    {URL}\n\nEnter code: ")
    assert launches == []
    assert not relay.opens_browser()


def test_opening_it_follows_headless_by_default(settings) -> None:
    assert settings.str_("auth.signin.open_url") == "auto"


@pytest.mark.parametrize("value", ["reset", "", "auto", "sometimes"])
def test_under_headless_anything_but_always_opens_nothing(after_reset, launches, value) -> None:
    """``reset`` was the default that caused it; a setting kept from then stays harmless."""
    configure(
        after_reset,
        "auth.reset=true",
        "auth.browser_mode=headless",
        "auth.signin.pty=always",
        f"auth.signin.open_url={value}",
    )
    pasted(after_reset).feed(f"    {URL}\n")
    assert launches == []


def test_the_sign_in_command_cannot_reach_the_exported_browser(ctx) -> None:
    from initialize_claude_ai.authenticate.google_cloud import GoogleCloudAuth

    configure(ctx, "auth.browser_mode=headless")
    ctx.runner.base_env["BROWSER"] = OWN.command
    env = ctx.runner.build_env(GoogleCloudAuth(ctx).sign_in_env())
    assert env["BROWSER"] == ctx.settings.str_("auth.signin.paste_env.BROWSER")
    assert env["BROWSER"] != OWN.command


def test_the_redirect_flow_keeps_the_exported_browser(ctx) -> None:
    from initialize_claude_ai.authenticate.google_cloud import GoogleCloudAuth

    configure(ctx, "auth.browser_mode=browser")
    assert GoogleCloudAuth(ctx).sign_in_env() == {}


def test_an_empty_value_removes_the_name(ctx) -> None:
    from initialize_claude_ai.authenticate.google_cloud import GoogleCloudAuth

    configure(ctx, "auth.browser_mode=headless", "auth.signin.paste_env.BROWSER=")
    ctx.runner.base_env["BROWSER"] = OWN.command
    assert "BROWSER" not in ctx.runner.build_env(GoogleCloudAuth(ctx).sign_in_env())


def test_the_sign_in_runs_with_that_environment(ctx, monkeypatch) -> None:
    from initialize_claude_ai.authenticate.google_cloud import GoogleCloudAuth

    configure(ctx, "auth.browser_mode=headless", "auth.signin.pty=never")
    seen: dict = {}

    def run(argv, **kwargs):
        seen.update(kwargs)
        return CommandResult(argv=list(argv), returncode=0)

    monkeypatch.setattr(ctx.runner, "run", run)
    GoogleCloudAuth(ctx).run_sign_in(["gcloud", "auth", "login"], timeout=1, label="t")
    assert "BROWSER" in seen["env"]


def test_after_a_reset_the_emptied_profile_is_offered_as_a_command(after_reset, launches) -> None:
    advice = " ".join(pasted(after_reset).session_advice())
    assert f"/usr/bin/firefox --profile {PROFILE} --no-remote" in advice
    assert "sign-in-url.txt" in advice
    assert launches == []


# -- opening it in the clean profile, when asked to ----------------------------


def test_always_opens_the_whole_url_in_the_emptied_profile(opted_in, launches) -> None:
    relay = pasted(opted_in)
    relay.feed(f"Go to the following link in your browser:\n\n    {URL}\n\nEnter code: ")
    assert launches == [OWN.argv_for(URL)]
    assert launches[0][-1] == URL
    assert PROFILE in launches[0]


def test_it_is_opened_once_however_often_the_url_is_printed(opted_in, launches) -> None:
    relay = pasted(opted_in)
    for _ in range(3):
        relay.feed(f"    {URL}\n")
    assert len(launches) == 1


def test_a_url_still_arriving_is_not_opened_until_it_is_whole(opted_in, launches) -> None:
    """Opening the first read's worth would be the truncated link all over again."""
    relay = pasted(opted_in)
    relay.feed(f"    {URL[:90]}")
    assert launches == []
    relay.feed(f"{URL[90:]}\n")
    assert launches == [OWN.argv_for(URL)]


def test_a_hyperlink_target_is_whole_on_arrival(opted_in, launches) -> None:
    relay = pasted(opted_in)
    relay.feed(f"\x1b]8;;{URL}\x1b\\{URL[:40]}")
    assert launches == [OWN.argv_for(URL)]


def test_the_redirect_flow_is_left_to_open_its_own_browser(opted_in, launches) -> None:
    """gcloud opens BROWSER itself there; a second window is a second sign-in."""
    SignInUrlRelay(opted_in, paste=False).feed(f"    {URL}\n")
    assert launches == []


def test_headless_without_a_reset_opens_nothing(after_reset, launches) -> None:
    configure(after_reset, "auth.reset=false", "auth.browser_mode=headless", "auth.signin.pty=always")
    pasted(after_reset).feed(f"    {URL}\n")
    assert launches == []


# -- without --headless: the URL is opened, the code still comes back ----------
#
# The report: without --headless the browser opened, the sign-in succeeded, and
# the loopback redirect never got back to this machine -- with no code to paste
# instead. Every sign-in takes a pasted code now; without --headless the URL is
# opened for the operator, for --force-auth and --force-auth-with-reset alike.


@pytest.mark.parametrize("reset", ["true", "false"])
def test_without_headless_the_url_is_opened(after_reset, launches, reset) -> None:
    configure(after_reset, f"auth.reset={reset}", "auth.signin.pty=always")
    assert after_reset.uses_pasted_code()
    relay = pasted(after_reset)
    assert relay.opens_browser()
    relay.feed(f"    {URL}\n")
    assert launches == [OWN.argv_for(URL)]


def test_without_headless_no_display_opens_nothing(after_reset, launches) -> None:
    configure(after_reset, "auth.reset=true", "auth.signin.pty=always")
    after_reset.platform = dataclasses.replace(after_reset.platform, has_display=False)
    pasted(after_reset).feed(f"    {URL}\n")
    assert launches == []


def test_the_window_opened_is_the_url_handed_over(after_reset, launches) -> None:
    """After a reset that is the sign-out-first URL, as in the file and on screen."""
    from initialize_claude_ai.authenticate.google_cloud import GoogleCloudAuth

    configure(after_reset, "auth.reset=true", "auth.signin.pty=always")
    relay = GoogleCloudAuth(after_reset).url_relay()
    relay.feed(f"    {URL}\n")
    [argv] = launches
    assert argv[-1] == relay.on_screen(URL) != URL
    assert argv[-1].startswith(after_reset.settings.str_("auth.google_cloud.signout_url"))


def test_the_sign_in_command_still_cannot_open_a_browser_of_its_own(after_reset) -> None:
    """One window: the relay's. gcloud's own would be a second sign-in."""
    from initialize_claude_ai.authenticate.google_cloud import GoogleCloudAuth

    configure(after_reset, "auth.signin.pty=always")
    assert GoogleCloudAuth(after_reset).sign_in_env()["BROWSER"] == after_reset.settings.str_(
        "auth.signin.paste_env.BROWSER"
    )


def test_always_opens_it_without_a_reset(after_reset, launches) -> None:
    configure(after_reset, "auth.reset=false", "auth.signin.open_url=always", "auth.signin.pty=always")
    pasted(after_reset).feed(f"    {URL}\n")
    assert len(launches) == 1


def test_never_leaves_it_to_the_operator(after_reset, launches) -> None:
    configure(after_reset, "auth.reset=true", "auth.signin.open_url=never", "auth.signin.pty=always")
    pasted(after_reset).feed(f"    {URL}\n")
    assert launches == []


def test_a_kept_profile_is_not_a_clean_one(after_reset, launches) -> None:
    configure(
        after_reset,
        "auth.reset=true",
        "auth.reset_browser_profile=false",
        "auth.browser_mode=headless",
        "auth.signin.pty=always",
    )
    relay = pasted(after_reset)
    relay.feed(f"    {URL}\n")
    assert launches == []
    assert relay.own_profile_command(OWN) == ""


def test_no_display_means_no_browser(opted_in, launches) -> None:
    opted_in.platform = dataclasses.replace(opted_in.platform, has_display=False)
    pasted(opted_in).feed(f"    {URL}\n")
    assert launches == []


@pytest.mark.parametrize(
    "plan",
    [
        None,
        BrowserPlan(path="/usr/bin/xdg-open", profile_dir=PROFILE),
    ],
    ids=["no-plan", "no-command"],
)
def test_nothing_is_opened_without_a_browser_command(opted_in, launches, plan) -> None:
    if plan is None:
        opted_in.state.pop(STATE_KEY)
    else:
        opted_in.state[STATE_KEY] = plan
    pasted(opted_in).feed(f"    {URL}\n")
    assert launches == []


def test_a_dry_run_opens_nothing(after_reset) -> None:
    result = after_reset.runner.launch(OWN.argv_for(URL), label="sign-in browser")
    assert result.simulated


# -- saying what the reset did not reach ---------------------------------------


def test_nothing_is_said_without_a_reset(ctx) -> None:
    assert SignInUrlRelay(ctx, paste=True).session_advice("https://example.invalid/out") == []


def test_the_operator_is_told_where_to_sign_in_when_it_opens(opted_in) -> None:
    advice = " ".join(pasted(opted_in).session_advice())
    assert PROFILE in advice
    assert "asked who you are" in advice


def test_otherwise_the_caveat_names_a_private_window_and_the_sign_out(after_reset) -> None:
    after_reset.platform = dataclasses.replace(after_reset.platform, has_display=False)
    advice = pasted(after_reset).session_advice("https://accounts.google.com/Logout")
    text = " ".join(advice)
    assert "not your browser's session" in text
    assert "/usr/bin/firefox --private-window" in text
    assert "sign-in-url.txt" in text
    assert "https://accounts.google.com/Logout" in text


def test_the_caveat_names_no_file_that_will_not_be_written(after_reset) -> None:
    configure(after_reset, "auth.reset=true", "auth.signin.pty=never")
    text = " ".join(pasted(after_reset).session_advice())
    assert "private window" in text
    assert "sign-in-url.txt" not in text


def test_the_sign_out_urls_are_settings(settings) -> None:
    assert settings.str_("auth.google_cloud.signout_url").startswith("https://")
    assert settings.str_("auth.claude_oauth.signout_url") == ""


# -- the browser plan ----------------------------------------------------------


def test_preparing_the_browser_leaves_the_plan_for_the_sign_in(ctx) -> None:
    plan = prepare_browser(ctx)
    assert planned_browser(ctx) is plan


def test_the_url_takes_the_placeholders_place() -> None:
    plan = BrowserPlan(command="'/opt/my browser/run' --profile '/a b' %s", path="/opt/my browser/run")
    assert plan.argv_for(URL) == ["/opt/my browser/run", "--profile", "/a b", URL]


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/usr/bin/firefox", "/usr/bin/firefox --private-window"),
        ("/usr/bin/google-chrome-stable", "/usr/bin/google-chrome-stable --incognito"),
        ("/usr/bin/chromium-browser", "/usr/bin/chromium-browser --incognito"),
        ("/usr/bin/microsoft-edge", "/usr/bin/microsoft-edge --inprivate"),
        ("/usr/bin/links", ""),
    ],
)
def test_each_browser_family_has_its_own_private_window(ctx, path, expected) -> None:
    assert private_window_command(ctx, BrowserPlan(path=path)) == expected


def test_a_private_window_is_configurable(ctx) -> None:
    configure(ctx, 'auth.browser.private_args.links=["-private"]')
    assert private_window_command(ctx, BrowserPlan(path="/usr/bin/links")) == "/usr/bin/links -private"


# -- Runner.launch -------------------------------------------------------------


def test_launch_leaves_the_child_running(monkeypatch) -> None:
    started: list[dict] = []

    class Child:
        def wait(self, timeout=None):
            raise subprocess.TimeoutExpired("browser", timeout)

    def popen(argv, **kwargs):
        started.append(kwargs)
        return Child()

    monkeypatch.setattr(subprocess, "Popen", popen)
    result = Runner(dry_run=False).launch(["browser", URL], settle=0.01)
    assert result.ok
    assert started[0]["start_new_session"] is True
    assert started[0]["stdin"] is subprocess.DEVNULL


def test_launch_reports_a_browser_that_died_at_once() -> None:
    result = Runner(dry_run=False).launch([sys.executable, "-c", "raise SystemExit(3)"], settle=5)
    assert result.returncode == 3


def test_launch_reports_a_missing_browser() -> None:
    assert Runner(dry_run=False).launch(["/nonexistent/browser"]).returncode == 127


def test_the_everyday_browser_is_opened_where_there_is_no_profile_of_our_own(opted_in, launches) -> None:
    """What gcloud's own redirect flow would have opened, now with a code to paste."""
    everyday = BrowserPlan(command="/usr/bin/firefox %s", path="/usr/bin/firefox")
    opted_in.state[STATE_KEY] = everyday
    pasted(opted_in).feed(f"    {URL}\n")
    assert launches == [everyday.argv_for(URL)]
