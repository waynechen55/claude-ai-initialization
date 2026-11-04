"""One sign-in flow for the whole run, not one per backend.

``--headless`` and ``--browser-loopback-redirect`` answer a question about the
machine -- can a browser here finish a login by itself? -- so the answer cannot
legitimately differ between Google Cloud and claude.ai. It used to: Google Cloud
read ``auth.paste_code`` while claude.ai read a separate headless property, and
a run could be in both modes at once depending on which credential happened to
be missing. These tests pin every backend, and the phase banner, to the same
answer.
"""

from __future__ import annotations

import pytest

from initialize_claude_ai import authenticate as auth_registry
from initialize_claude_ai.core.config import Settings


def with_mode(ctx, monkeypatch, mode: str, *, display: bool):
    """A context whose flow is ``mode`` on a host with or without a display."""
    data = dict(ctx.settings.data)
    data["auth"] = {**data.get("auth", {}), "browser_mode": mode}
    ctx.settings = Settings(data=data, environ=ctx.settings.environ)
    monkeypatch.setattr(type(ctx.platform), "headless", property(lambda _self: not display))
    return ctx


def backends(ctx):
    return auth_registry.build(ctx, list(auth_registry.BACKENDS))


@pytest.mark.parametrize(
    ("mode", "display", "expected"),
    [
        ("auto", True, True),  # a browser is there; it is opened, the code comes back
        ("auto", False, True),  # nothing to launch; carry the code back
        ("headless", True, True),
        ("browser", True, False),  # the redirect is an explicit choice...
        ("browser", False, False),  # ...display or not
    ],
)
def test_every_backend_agrees_on_the_flow(ctx, monkeypatch, mode, display, expected) -> None:
    ctx = with_mode(ctx, monkeypatch, mode, display=display)
    chosen = {backend.key: backend.paste_code for backend in backends(ctx)}
    assert chosen, "no backends were built, so this proves nothing"
    assert set(chosen.values()) == {expected}, chosen


def test_the_banner_states_the_flow_the_backends_will_use(ctx, monkeypatch) -> None:
    """The banner is printed before any backend is built, from the same rule.

    Reporting one flow and running another is worse than reporting nothing: the
    line is what tells someone whether to go and find a browser.
    """
    for mode, display in (("auto", True), ("auto", False), ("headless", True), ("browser", False)):
        ctx = with_mode(ctx, monkeypatch, mode, display=display)
        announced = ctx.uses_pasted_code()
        assert all(backend.paste_code == announced for backend in backends(ctx)), mode


def test_an_unknown_mode_is_read_as_auto(ctx, monkeypatch) -> None:
    """A typo in a config file must not silently pick the redirect flow."""
    for display in (True, False):
        ctx = with_mode(ctx, monkeypatch, "brwoser", display=display)
        assert ctx.browser_mode() == "auto"
        assert ctx.uses_pasted_code() is True


@pytest.mark.parametrize("display", [True, False])
def test_the_redirect_is_never_chosen_for_the_operator(ctx, monkeypatch, display) -> None:
    """The report: the browser opened, the sign-in succeeded, and the redirect
    back to this machine never arrived -- with no code to paste instead."""
    ctx = with_mode(ctx, monkeypatch, "auto", display=display)
    assert ctx.uses_pasted_code() is True
