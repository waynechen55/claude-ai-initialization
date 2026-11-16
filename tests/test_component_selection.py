"""Which components the catalog offers this particular machine."""

from __future__ import annotations

from initialize_claude_ai.core.platform_detect import PlatformInfo, _detect_display
from initialize_claude_ai.install.components import load_components, select_components


def catalog(settings, **kwargs):
    selected, notes = select_components(load_components(settings), **kwargs)
    return {c.key for c in selected}, notes


def test_wslview_is_offered_only_where_it_exists(settings) -> None:
    """No distribution outside WSL carries wslu, so offering it elsewhere is a
    guaranteed failed install and a red line in the report for nothing."""
    ordinary, _notes = catalog(settings, only=["browser"], system="Linux")
    under_wsl, _notes = catalog(settings, only=["browser"], system="Linux", traits=("wsl",))
    assert "wslu" not in ordinary
    assert "wslu" in under_wsl


def test_the_browser_check_no_longer_accepts_a_launcher(settings) -> None:
    """xdg-open satisfying the browser component meant a machine with a launcher
    and nothing to launch reported a browser, and then the sign-in opened
    nothing and waited out the timeout."""
    browser = next(c for c in load_components(settings) if c.key == "browser")
    assert "xdg-open" not in browser.binary_names()
    assert "firefox" in browser.binary_names()


def test_debian_has_somewhere_to_fall_when_firefox_is_not_the_package_name(
    settings,
) -> None:
    """One packages.apt list serves Debian and Ubuntu, and apt fails the whole
    command on a single unknown name -- so the two spellings need two
    strategies, tried in order, rather than one list naming both."""
    browser = next(c for c in load_components(settings) if c.key == "browser")
    apt_lists = [
        s.get("packages", {}).get("apt")
        for s in browser.install
        if isinstance(s.get("packages"), dict) and s.get("packages", {}).get("apt")
    ]
    assert ["firefox"] in apt_lists and ["firefox-esr"] in apt_lists


def test_the_browser_candidates_and_the_catalog_agree(settings) -> None:
    """Two lists naming browsers is one too many to keep in step by hand, so at
    least say so when they drift: a browser phase 1 installs but phase 2 will
    not look for is a browser nobody gets to use."""
    browser = next(c for c in load_components(settings) if c.key == "browser")
    unseen = set(settings.str_list("auth.browser.candidates")) - set(browser.binary_names())
    assert unseen == set()


def test_wsl_without_an_x_server_is_not_headless(monkeypatch) -> None:
    """The browser is on the Windows side and wslview reaches it, so the sign-in
    URL can be opened there. Calling it headless leaves every WSL operator to
    carry the URL across by hand for nothing."""
    monkeypatch.setattr(
        "initialize_claude_ai.core.platform_detect.shutil.which",
        lambda name, *a, **k: "/usr/bin/wslview" if name == "wslview" else None,
    )
    assert _detect_display({}, in_wsl=True) is True
    assert _detect_display({}, in_wsl=False) is False


def test_a_bare_display_is_not_a_desktop_session() -> None:
    """An X server started from a tty can draw a browser and brings none of the
    session services one expects to find behind it."""
    from_a_tty = PlatformInfo(system="Linux", machine="x86_64", session_type="tty")
    logged_in = PlatformInfo(system="Linux", machine="x86_64", session_type="x11")
    assert from_a_tty.desktop_session is False
    assert logged_in.desktop_session is True
    assert PlatformInfo(system="Linux", machine="x86_64", desktop="KDE").desktop_session is True
