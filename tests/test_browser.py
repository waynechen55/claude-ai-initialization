"""Choosing and preparing the browser a sign-in opens.

The bug these guard against cannot be reproduced from a test: it is a browser
painting characters that the page never hears about. What *can* be pinned down
is the decision -- that the fix is applied on a host shaped like the one where
that happens, and withheld everywhere it would do harm.
"""

from __future__ import annotations

import dataclasses
import os
import stat
from pathlib import Path

import pytest

from initialize_claude_ai.authenticate.browser import plan_browser, prepare_browser
from initialize_claude_ai.core.config import load_settings
from initialize_claude_ai.core.context import RunContext

IM_KEYS = ("GTK_IM_MODULE", "QT_IM_MODULE", "XMODIFIERS")


@pytest.fixture
def bin_dir(sandbox: Path, ctx: RunContext) -> Path:
    """An empty PATH this test owns, so the host's browsers are invisible."""
    path = sandbox / "bin"
    path.mkdir()
    ctx.runner.base_env["PATH"] = str(path)
    return path


def install(bin_dir: Path, name: str) -> Path:
    """Put something executable on the test's PATH under ``name``."""
    binary = bin_dir / name
    binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
    return binary


def on_host(ctx: RunContext, **facts: object) -> RunContext:
    """Rewrite the detected platform, since the real one is whatever ran the suite."""
    ctx.platform = dataclasses.replace(ctx.platform, **facts)
    return ctx


@pytest.fixture
def tty_x_session(ctx: RunContext) -> RunContext:
    """The host where typing into a browser silently fails.

    An X display, nothing that counts as a desktop session behind it, and no
    input method configured -- an X server started from a tty, a VNC target, a
    container, or root on any of them.
    """
    return on_host(
        ctx,
        system="Linux",
        display=":0",
        session_type="tty",
        desktop="",
        input_method="",
        has_display=True,
    )


@pytest.fixture
def desktop(ctx: RunContext) -> RunContext:
    return on_host(
        ctx,
        system="Linux",
        display=":0",
        session_type="x11",
        desktop="GNOME",
        input_method="",
        has_display=True,
        is_root=False,
    )


def reconfigure(ctx: RunContext, **overrides: str) -> None:
    """Reload settings with ``auth.browser`` keys set, as a flag would."""
    ctx.settings = load_settings(
        overrides=[f"auth.browser.{k}={v}" for k, v in overrides.items()],
        environ=dict(os.environ),
    )


# -- the input-method fix ------------------------------------------------------


def test_typed_text_is_rescued_on_a_display_with_no_desktop_behind_it(tty_x_session) -> None:
    """The case the fix exists for: characters appear, the page never sees them."""
    plan = plan_browser(tty_x_session)
    assert plan.env["GTK_IM_MODULE"] == "gtk-im-context-simple"
    assert plan.env["XMODIFIERS"] == "@im=none"
    assert "input-method fix applied" in plan.detail()


def test_a_real_desktop_is_left_exactly_as_it_was(desktop) -> None:
    """Its input method works; replacing it would be the worse bug."""
    assert not set(plan_browser(desktop).env) & set(IM_KEYS)


def test_a_configured_input_method_is_never_overridden(tty_x_session) -> None:
    """Somebody using ibus may be composing the very text this would break.

    The host looks exactly like the broken one in every other respect, which is
    the point: this single fact has to be enough to hold the fix back.
    """
    on_host(tty_x_session, input_method="ibus")
    assert not set(plan_browser(tty_x_session).env) & set(IM_KEYS)


def test_a_host_with_no_x_display_is_not_given_an_x_fix(tty_x_session) -> None:
    """WSL reaching a browser on the Windows side is not headless and not X."""
    on_host(tty_x_session, display="", in_wsl=True)
    assert not set(plan_browser(tty_x_session).env) & set(IM_KEYS)


def test_always_applies_the_fix_where_detection_would_not(desktop) -> None:
    """The escape hatch, for the operator whose host detection reads wrongly."""
    reconfigure(desktop, input_method_fix="always")
    assert plan_browser(desktop).env["GTK_IM_MODULE"] == "gtk-im-context-simple"


def test_never_withholds_the_fix_where_detection_would_apply_it(tty_x_session) -> None:
    reconfigure(tty_x_session, input_method_fix="never")
    assert not set(plan_browser(tty_x_session).env) & set(IM_KEYS)


def test_the_fix_does_not_depend_on_which_sign_in_flow_was_chosen(tty_x_session) -> None:
    """--headless and --browser-loopback-redirect choose a flow, not a browser.

    The operator reported the same failure under both and under neither, which
    is exactly what one would expect: all three end at the same local browser.
    """
    applied = []
    for mode in ("auto", "headless", "browser"):
        tty_x_session.settings = load_settings(overrides=[f"auth.browser_mode={mode}"], environ=dict(os.environ))
        applied.append(plan_browser(tty_x_session).env.get("GTK_IM_MODULE"))
    assert applied == ["gtk-im-context-simple"] * 3


# -- which browser -------------------------------------------------------------


def test_the_first_known_browser_on_the_path_is_chosen(tty_x_session, bin_dir) -> None:
    install(bin_dir, "chromium")
    firefox = install(bin_dir, "firefox")
    plan = plan_browser(tty_x_session)
    # Preference order, not PATH order: firefox leads auth.browser.candidates.
    assert plan.path == str(firefox)
    assert plan.command.endswith(" %s")
    assert plan.exportable


def test_a_launcher_alone_does_not_count_as_a_browser(tty_x_session, bin_dir) -> None:
    """xdg-open with nothing registered opens nothing and reports success.

    It used to satisfy the browser check on its own, which turned "no browser
    here" into a sign-in that hung waiting for a redirect that could not come.
    """
    install(bin_dir, "xdg-open")
    plan = plan_browser(tty_x_session)
    assert "none installed" in plan.detail()


def test_an_opener_that_reads_browser_is_never_named_in_browser(tty_x_session, bin_dir) -> None:
    """Otherwise xdg-open hands the URL to itself, for ever."""
    install(bin_dir, "xdg-open")
    plan = plan_browser(tty_x_session)
    assert not plan.exportable
    prepare_browser(on_host(tty_x_session, is_root=False))
    assert "BROWSER" not in tty_x_session.runner.base_env


def test_wslview_is_named_in_browser_because_it_does_not_read_it(tty_x_session, bin_dir) -> None:
    wslview = install(bin_dir, "wslview")
    plan = plan_browser(on_host(tty_x_session, in_wsl=True))
    assert plan.exportable and plan.path == str(wslview)


def test_nothing_at_all_says_how_to_sign_in_anyway(tty_x_session, bin_dir) -> None:
    """A machine with no browser can still authenticate; it must not read as broken."""
    plan = plan_browser(tty_x_session)
    assert plan.command == ""
    assert any("--headless" in line for line in plan.remediation)


def test_a_configured_command_is_used_verbatim(tty_x_session, bin_dir) -> None:
    reconfigure(tty_x_session, command="surf %s")
    assert plan_browser(tty_x_session).command == "surf %s"


def test_a_configured_command_without_a_placeholder_still_gets_the_url(tty_x_session) -> None:
    reconfigure(tty_x_session, command="surf")
    assert plan_browser(tty_x_session).command == "surf %s"


# -- the dedicated profile -----------------------------------------------------


def test_a_host_with_no_desktop_session_signs_in_in_its_own_profile(tty_x_session, bin_dir) -> None:
    install(bin_dir, "firefox")
    plan = plan_browser(tty_x_session)
    assert plan.profile_dir.endswith("browser-profile/firefox")
    assert f"--profile {plan.profile_dir}" in plan.command


def test_a_desktop_user_keeps_their_own_profile(desktop, bin_dir) -> None:
    """They are probably already signed in there; that is the fast path."""
    install(bin_dir, "firefox")
    plan = plan_browser(desktop)
    assert plan.profile_dir == ""
    assert "--profile" not in plan.command


def test_root_on_a_desktop_still_gets_its_own_profile(desktop, bin_dir) -> None:
    """Root's Firefox is a different profile from the desktop user's anyway."""
    install(bin_dir, "firefox")
    assert plan_browser(on_host(desktop, is_root=True)).profile_dir != ""


def test_a_chromium_family_browser_is_asked_the_way_it_expects(tty_x_session, bin_dir) -> None:
    install(bin_dir, "chromium")
    plan = plan_browser(tty_x_session)
    assert f"--user-data-dir={plan.profile_dir}" in plan.command


def test_never_leaves_the_default_profile_alone(tty_x_session, bin_dir) -> None:
    install(bin_dir, "firefox")
    reconfigure(tty_x_session, clean_profile="never")
    assert plan_browser(tty_x_session).profile_dir == ""


# -- and what a credentials reset does to it -----------------------------------


def resetting(ctx: RunContext, **overrides: str) -> None:
    """Settings as ``--force-auth-with-reset`` would leave them."""
    ctx.settings = load_settings(
        overrides=["auth.reset=true", *(f"{k}={v}" for k, v in overrides.items())],
        environ=dict(os.environ),
    )


def profile_of(ctx: RunContext) -> Path:
    return ctx.state_dir / "browser-profile" / "firefox"


def test_a_reset_signs_in_somewhere_the_provider_has_never_seen(desktop, bin_dir) -> None:
    """A desktop's own profile is normally the right place to sign in -- but a
    reset is a request to be asked for an account again, and the cookie that
    would prevent that is exactly what makes the default profile convenient."""
    install(bin_dir, "firefox")
    resetting(desktop)
    plan = plan_browser(desktop)
    assert plan.profile_dir == str(profile_of(desktop))
    assert any("credentials reset" in note for note in plan.notes)


def test_a_reset_empties_the_profile_last_time_left_behind(desktop, bin_dir) -> None:
    """Our own profile carries the cookie too, once it has been signed in."""
    install(bin_dir, "firefox")
    cookie = profile_of(desktop) / "cookies.sqlite"
    cookie.parent.mkdir(parents=True)
    cookie.write_text("x", encoding="utf-8")
    resetting(desktop)
    desktop.dry_run = False
    plan = plan_browser(desktop)
    assert not cookie.exists()
    assert profile_of(desktop).is_dir(), "emptied, then made ready for the sign-in"
    assert any("cleared the sign-in profile" in note for note in plan.notes)


def test_a_rehearsal_only_says_what_it_would_clear(desktop, bin_dir) -> None:
    install(bin_dir, "firefox")
    cookie = profile_of(desktop) / "cookies.sqlite"
    cookie.parent.mkdir(parents=True)
    cookie.write_text("x", encoding="utf-8")
    resetting(desktop)  # ctx.dry_run is True
    plan = plan_browser(desktop)
    assert cookie.exists()
    assert any("would clear the sign-in profile" in note for note in plan.notes)


def test_never_still_wins_over_a_reset(desktop, bin_dir) -> None:
    """An explicit answer beats an inferred one, in this direction as in the other."""
    install(bin_dir, "firefox")
    cookie = profile_of(desktop) / "cookies.sqlite"
    cookie.parent.mkdir(parents=True)
    cookie.write_text("x", encoding="utf-8")
    resetting(desktop, **{"auth.browser.clean_profile": "never"})
    desktop.dry_run = False
    assert plan_browser(desktop).profile_dir == ""
    assert cookie.exists()


def test_your_own_browser_profile_is_never_the_thing_being_reset(desktop, bin_dir) -> None:
    """The path is built here rather than read from configuration, so there is
    no setting that could point this at a real profile. Switching the behaviour
    off leaves the desktop's own default profile in use, untouched."""
    install(bin_dir, "firefox")
    resetting(desktop, **{"auth.reset_browser_profile": "false"})
    assert plan_browser(desktop).profile_dir == ""


# -- applying it ---------------------------------------------------------------


def test_the_plan_reaches_every_child_process(tty_x_session, bin_dir) -> None:
    """gcloud and the claude CLI launch the browser, so the overlay has to be theirs."""
    install(bin_dir, "firefox")
    tty_x_session.dry_run = False
    plan = prepare_browser(tty_x_session)
    env = tty_x_session.runner.build_env()
    assert env["BROWSER"] == plan.command
    assert env["GTK_IM_MODULE"] == "gtk-im-context-simple"


def test_a_rehearsal_changes_nothing(tty_x_session, bin_dir) -> None:
    install(bin_dir, "firefox")
    prepare_browser(tty_x_session)  # ctx.dry_run is True
    assert "BROWSER" not in tty_x_session.runner.base_env
    assert not (tty_x_session.state_dir / "browser-profile").exists()
