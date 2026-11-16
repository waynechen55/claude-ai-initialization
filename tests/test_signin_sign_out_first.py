"""A reset has to reach the browser a pasted sign-in URL is opened in.

The failure these guard against: ``--force-auth-with-reset --headless`` cleared
every credential on the machine, and the operator -- pasting the URL into their
own browser, as ``--headless`` asks -- was signed straight back in with no email
and no password. Nothing on the machine was left; the provider recognised the
browser's session. No sign-in parameter can refuse that session (Google accepts
only ``none``, ``consent`` and ``select_account`` as prompts), but the
provider's sign-out page can end it and continue to the sign-in, and that URL
reaches whichever browser it is pasted into.
"""

from __future__ import annotations

import dataclasses
import os
from urllib.parse import parse_qs, urlsplit

import pytest

from initialize_claude_ai.authenticate.browser import STATE_KEY, BrowserPlan
from initialize_claude_ai.authenticate.claude_oauth import ClaudeOAuthAuth
from initialize_claude_ai.authenticate.google_cloud import GoogleCloudAuth
from initialize_claude_ai.authenticate.signin_url import SignInUrlRelay, UrlOnScreen, with_prompt
from initialize_claude_ai.core.config import load_settings
from initialize_claude_ai.core.context import RunContext

URL = (
    "https://accounts.google.com/o/oauth2/auth?response_type=code&client_id=32555940559.apps."
    "googleusercontent.com&redirect_uri=https%3A%2F%2Fsdk.cloud.google.com%2Fauthcode.html"
    "&scope=openid&state=8Qb2Lm&access_type=offline&code_challenge_method=S256"
)
OTHER = "https://accounts.google.com/o/oauth2/auth?response_type=code&state=SECOND" + "x" * 60


def configure(ctx: RunContext, *overrides: str) -> None:
    ctx.settings = load_settings(overrides=list(overrides), environ=dict(os.environ))


@pytest.fixture
def headless_reset(ctx: RunContext, monkeypatch: pytest.MonkeyPatch) -> RunContext:
    """The reported run: --force-auth-with-reset --headless, a display, a pty."""
    configure(ctx, "auth.reset=true", "auth.browser_mode=headless", "auth.signin.pty=always")
    ctx.platform = dataclasses.replace(ctx.platform, has_display=True)
    delivered: list[str] = []
    monkeypatch.setattr(SignInUrlRelay, "deliver", lambda self, url: delivered.append(url))
    ctx.state["delivered"] = delivered
    return ctx


def relay_for(ctx: RunContext) -> SignInUrlRelay:
    return GoogleCloudAuth(ctx).url_relay()


def handed_over(ctx: RunContext, *urls: str) -> list[str]:
    relay = relay_for(ctx)
    for url in urls:
        relay.feed(f"Go to the following link in your browser:\n\n    {url}\n\nEnter code: ")
    return ctx.state["delivered"]


def test_the_url_handed_over_signs_the_browser_out_first(headless_reset) -> None:
    [url] = handed_over(headless_reset, URL)
    signout = headless_reset.settings.str_("auth.google_cloud.signout_url")
    param = headless_reset.settings.str_("auth.google_cloud.signout_continue_param")
    assert url.startswith(signout + "?")
    assert parse_qs(urlsplit(url).query)[param][0].startswith(URL)


def test_the_sign_in_itself_is_unchanged_behind_it(headless_reset) -> None:
    """The code the page ends with belongs to gcloud's request, state and all."""
    [url] = handed_over(headless_reset, URL)
    param = headless_reset.settings.str_("auth.google_cloud.signout_continue_param")
    inner = parse_qs(urlsplit(url).query)[param][0]
    assert inner.startswith(URL + "&")
    assert {k: v for k, v in parse_qs(urlsplit(inner).query).items() if k != "prompt"} == parse_qs(urlsplit(URL).query)


def test_only_the_first_sign_in_goes_through_the_sign_out(headless_reset) -> None:
    """After signing in again the browser holds the session just chosen."""
    first, second = handed_over(headless_reset, URL, OTHER)
    assert first != URL
    assert second == OTHER


def test_the_whole_url_replacing_a_fragment_is_still_the_first(headless_reset) -> None:
    relay = relay_for(headless_reset)
    relay.feed(f"    {URL[:90]}")
    relay.feed(f"{URL[90:]}\n")
    assert all(url.startswith("https://accounts.google.com/Logout?") for url in headless_reset.state["delivered"])


def test_a_url_on_another_host_is_handed_over_unchanged(headless_reset) -> None:
    """Google continues only to its own host; anything else would be a refusal."""
    foreign = "https://example.invalid/oauth/authorize?" + "q" * 80
    assert handed_over(headless_reset, foreign) == [foreign]


def test_without_a_reset_the_url_is_unchanged(headless_reset) -> None:
    configure(headless_reset, "auth.reset=false", "auth.browser_mode=headless", "auth.signin.pty=always")
    assert handed_over(headless_reset, URL) == [URL]


def test_always_signs_out_without_a_reset(headless_reset) -> None:
    configure(
        headless_reset,
        "auth.reset=false",
        "auth.browser_mode=headless",
        "auth.signin.pty=always",
        "auth.signin.sign_out_first=always",
    )
    assert handed_over(headless_reset, URL) != [URL]


@pytest.mark.parametrize("value", ["never", "", "sometimes"])
def test_anything_else_leaves_it_unchanged(headless_reset, value) -> None:
    configure(
        headless_reset,
        "auth.reset=true",
        "auth.browser_mode=headless",
        "auth.signin.pty=always",
        f"auth.signin.sign_out_first={value}",
    )
    assert handed_over(headless_reset, URL) == [URL]


def test_the_redirect_flow_is_left_alone(headless_reset) -> None:
    """It opens this program's emptied profile, which holds no session."""
    configure(headless_reset, "auth.reset=true", "auth.browser_mode=browser", "auth.signin.pty=always")
    assert handed_over(headless_reset, URL) == [URL]


@pytest.mark.parametrize(
    "override",
    ["auth.google_cloud.signout_url=", "auth.google_cloud.signout_continue_param="],
)
def test_a_backend_without_a_chainable_sign_out_is_left_alone(headless_reset, override) -> None:
    configure(headless_reset, "auth.reset=true", "auth.browser_mode=headless", "auth.signin.pty=always", override)
    assert handed_over(headless_reset, URL) == [URL]


def test_claude_names_no_sign_out_by_default(headless_reset) -> None:
    assert not ClaudeOAuthAuth(headless_reset).url_relay().signs_out_first()


def test_nothing_is_captured_nothing_is_changed(headless_reset) -> None:
    configure(headless_reset, "auth.reset=true", "auth.browser_mode=headless", "auth.signin.pty=never")
    assert not relay_for(headless_reset).signs_out_first()


# -- what the operator is told before the sign-in --------------------------------


def test_the_operator_is_told_the_browser_will_be_signed_out(headless_reset) -> None:
    relay = relay_for(headless_reset)
    text = " ".join(relay.session_advice(relay.signout_url))
    assert "signs the browser it is opened in out of accounts.google.com first" in text
    assert "every account" in text
    assert "on screen, in the file and on the clipboard" in text


def test_the_emptied_profile_is_offered_to_keep_the_browser_signed_in(headless_reset) -> None:
    profile = "/state/browser-profile/firefox"
    headless_reset.state[STATE_KEY] = BrowserPlan(
        command=f"/usr/bin/firefox --profile {profile} --no-remote %s",
        path="/usr/bin/firefox",
        profile_dir=profile,
    )
    relay = relay_for(headless_reset)
    assert profile in " ".join(relay.session_advice(relay.signout_url))


def test_the_chaining_is_configured(settings) -> None:
    assert settings.str_("auth.signin.sign_out_first") == "reset"
    assert settings.str_("auth.google_cloud.signout_continue_param")
    assert settings.str_("auth.claude_oauth.signout_continue_param") == ""


# -- the copy on screen ------------------------------------------------------------
#
# The second report: the file and the clipboard went through the sign-out, the
# screen did not -- and the screen is what an operator selects, Ctrl+clicks, or
# copies over ssh. Every copy has to be the same URL.


def painted(ctx: RunContext, *chunks: str, quiet: bool = True) -> str:
    """What the operator sees when the child writes ``chunks``."""
    relay = relay_for(ctx)
    screen = UrlOnScreen(relay)
    out = []
    for chunk in chunks:
        relay.feed(chunk)
        out.append(screen.feed(chunk))
    if quiet:
        out.append(screen.flush())
    return "".join(out)


def test_the_screen_shows_the_url_that_signs_out_first(headless_reset) -> None:
    text = painted(headless_reset, f"Go to the following link in your browser:\n\n    {URL}\n\nEnter code: ")
    [handed] = headless_reset.state["delivered"]
    assert handed in text
    assert f"    {URL}\n" not in text
    assert text.endswith("Enter code: ")


def test_a_url_split_across_reads_is_painted_whole(headless_reset) -> None:
    text = painted(headless_reset, "link:\n    ", URL[:7], URL[7:90], URL[90:] + "\n")
    [handed] = headless_reset.state["delivered"][-1:]
    assert handed in text and URL + "\n" not in text


def test_nothing_is_painted_half_rewritten_while_it_arrives(headless_reset) -> None:
    relay = relay_for(headless_reset)
    screen = UrlOnScreen(relay)
    relay.feed("    " + URL[:90])
    assert screen.feed("    " + URL[:90]) == "    "


def test_a_url_ending_the_output_is_released_when_the_child_goes_quiet(headless_reset) -> None:
    text = painted(headless_reset, f"    {URL}")
    assert text.startswith("    https://accounts.google.com/Logout?")


def test_colour_codes_around_it_are_kept(headless_reset) -> None:
    text = painted(headless_reset, f"\x1b[1m{URL}\x1b[0m\n")
    assert text.startswith("\x1b[1mhttps://accounts.google.com/Logout?") and text.endswith("\x1b[0m\n")


def test_everything_else_is_painted_unchanged(headless_reset) -> None:
    other = "See https://cloud.google.com/sdk/docs for help.\nhttps://x\nplain text\n"
    assert painted(headless_reset, other) == other


def test_without_the_sign_out_the_screen_is_left_alone(headless_reset) -> None:
    configure(headless_reset, "auth.reset=false", "auth.browser_mode=headless", "auth.signin.pty=always")
    assert painted(headless_reset, f"    {URL}\n") == f"    {URL}\n"


def test_the_sign_in_is_run_with_the_screen_rewritten(headless_reset, monkeypatch) -> None:
    seen: dict = {}

    def run_tty(argv, **kwargs):
        seen.update(kwargs)
        from initialize_claude_ai.core.proc import CommandResult

        return CommandResult(argv=list(argv), returncode=0)

    monkeypatch.setattr(headless_reset.runner, "run_tty", run_tty)
    GoogleCloudAuth(headless_reset).run_sign_in(["gcloud", "auth", "login"], timeout=1, label="t")
    assert isinstance(seen["screen"], UrlOnScreen)


def test_no_rewriting_without_the_sign_out(headless_reset, monkeypatch) -> None:
    configure(headless_reset, "auth.reset=false", "auth.browser_mode=headless", "auth.signin.pty=always")
    seen: dict = {}

    def run_tty(argv, **kwargs):
        seen.update(kwargs)
        from initialize_claude_ai.core.proc import CommandResult

        return CommandResult(argv=list(argv), returncode=0)

    monkeypatch.setattr(headless_reset.runner, "run_tty", run_tty)
    GoogleCloudAuth(headless_reset).run_sign_in(["gcloud", "auth", "login"], timeout=1, label="t")
    assert seen["screen"] is None


# -- what a sign-out cannot reach ---------------------------------------------------
#
# The third report: the browser was signed out of Google, and still went straight
# to the organisation's own sign-in page, signed in there. Google remembered the
# account; the organisation's page kept its own session on its own site.


def inner_of(ctx: RunContext, url: str) -> str:
    param = ctx.settings.str_("auth.google_cloud.signout_continue_param")
    return parse_qs(urlsplit(url).query)[param][0]


def test_the_sign_in_behind_the_sign_out_asks_for_the_account_chooser(headless_reset) -> None:
    [url] = handed_over(headless_reset, URL)
    prompt = headless_reset.settings.str_("auth.google_cloud.reset_prompt")
    assert prompt
    assert parse_qs(urlsplit(inner_of(headless_reset, url)).query)["prompt"] == [prompt]


def test_a_prompt_already_there_is_kept_and_merged(headless_reset) -> None:
    [url] = handed_over(headless_reset, URL + "&prompt=consent")
    [prompt] = parse_qs(urlsplit(inner_of(headless_reset, url)).query)["prompt"]
    wanted = headless_reset.settings.str_("auth.google_cloud.reset_prompt").split()
    assert prompt.split() == ["consent", *[w for w in wanted if w != "consent"]]


def test_an_empty_reset_prompt_adds_nothing(headless_reset) -> None:
    configure(
        headless_reset,
        "auth.reset=true",
        "auth.browser_mode=headless",
        "auth.signin.pty=always",
        "auth.google_cloud.reset_prompt=",
    )
    [url] = handed_over(headless_reset, URL)
    assert inner_of(headless_reset, url) == URL


@pytest.mark.parametrize(
    ("url", "prompt", "expected"),
    [
        ("https://h/a?x=1", "select_account", "https://h/a?x=1&prompt=select_account"),
        ("https://h/a", "select_account", "https://h/a?prompt=select_account"),
        ("https://h/a?prompt=consent&x=1", "select_account", "https://h/a?prompt=consent%20select_account&x=1"),
        ("https://h/a?prompt=select_account", "select_account", "https://h/a?prompt=select_account"),
        ("https://h/a?x=%2F#f", "select_account", "https://h/a?x=%2F&prompt=select_account#f"),
        ("https://h/a?x=1", "", "https://h/a?x=1"),
    ],
)
def test_with_prompt(url, prompt, expected) -> None:
    assert with_prompt(url, prompt) == expected


IDP = "https://sso.example.invalid/logout?next={continue}"


def with_idp(ctx: RunContext, template: str = IDP, *extra: str) -> None:
    configure(
        ctx,
        "auth.reset=true",
        "auth.browser_mode=headless",
        "auth.signin.pty=always",
        f"auth.signin.idp_signout_url={template}",
        *extra,
    )


def test_the_organisations_sign_out_is_the_first_stop(headless_reset) -> None:
    with_idp(headless_reset)
    [url] = handed_over(headless_reset, URL)
    assert url.startswith("https://sso.example.invalid/logout?next=")
    google = parse_qs(urlsplit(url).query)["next"][0]
    assert google.startswith(headless_reset.settings.str_("auth.google_cloud.signout_url") + "?")
    assert inner_of(headless_reset, google).startswith(URL)


def test_the_organisations_sign_out_alone_is_enough(headless_reset) -> None:
    """A backend with no sign-out page of its own still reaches the organisation's."""
    with_idp(headless_reset, IDP, "auth.google_cloud.signout_url=")
    [url] = handed_over(headless_reset, URL)
    assert parse_qs(urlsplit(url).query)["next"] == [URL]


def test_an_organisation_sign_out_that_goes_nowhere_is_ignored(headless_reset, caplog) -> None:
    with_idp(headless_reset, "https://sso.example.invalid/logout")
    [url] = handed_over(headless_reset, URL)
    assert url.startswith(headless_reset.settings.str_("auth.google_cloud.signout_url") + "?")
    assert "idp_signout_url" in caplog.text


def test_there_is_no_organisation_sign_out_by_default(settings) -> None:
    assert settings.str_("auth.signin.idp_signout_url") == ""


def test_the_private_window_is_offered_first(headless_reset) -> None:
    headless_reset.state[STATE_KEY] = BrowserPlan(command="/usr/bin/firefox %s", path="/usr/bin/firefox")
    relay = relay_for(headless_reset)
    advice = relay.session_advice(relay.signout_url)
    private = " ".join(headless_reset.settings.section("auth.browser").dict_("private_args")["firefox"])
    assert "private" in advice[0] and private in advice[0]
    assert "single sign-on" in " ".join(advice)
    assert "Use another account" in " ".join(advice)


def test_the_advice_names_every_stop(headless_reset) -> None:
    with_idp(headless_reset)
    relay = relay_for(headless_reset)
    text = " ".join(relay.session_advice(relay.signout_url))
    assert "sso.example.invalid and accounts.google.com" in text
    assert "idp_signout_url to" not in text
