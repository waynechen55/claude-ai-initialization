"""Choosing and preparing the browser an interactive sign-in will open.

This program never opens a browser itself.  ``gcloud auth login`` does, and so
does ``claude setup-token``; all this phase can do is decide what those two
inherit.  That turns out to be enough, because the two things most likely to go
wrong are both environment:

*Which* browser opens.  Left to themselves, gcloud and the claude CLI each
consult ``BROWSER`` and then Python's or node's own guesswork, which on a host
with several browsers installed can hand the URL to a different one each time --
or to ``xdg-open``, which on a machine with nothing registered succeeds at
opening nothing at all and reports success while doing it.

Whether typing into it works.  This is the surprising one.  On a host with an X
display but no desktop session behind it -- an X server started from a tty, an
``ssh -X`` forward, a VNC target, a container, anything running as root --
``GTK_IM_MODULE`` and ``XMODIFIERS`` are unset, and GTK falls back to an XIM
bridge with no XIM server listening behind it.  Firefox then paints the
characters that are typed while the page's JavaScript never receives an
``input`` event.  On Google's sign-in form the symptom is unmistakable and
completely baffling if you do not know the cause: the address appears in the
box, and the instant the field loses focus the "Enter your email or phone"
placeholder floats back down over it, because as far as the page is concerned
nothing was ever entered.  Forcing GTK's own built-in input context makes those
keystrokes ordinary key events again.

Both fixes are applied to :attr:`~initialize_claude_ai.core.proc.Runner.base_env`,
which every child process of this run inherits, so they reach the browser
whichever backend ends up launching it.  Neither depends on
``auth.browser_mode``: the mode chooses which sign-in *flow* runs, and all of
the flows that open a browser at all open the same one.  A paste-the-code flow
is unaffected because there is no local browser in it -- except that the
operator usually pastes the URL into a browser on this machine anyway, which is
why the preparation runs regardless of mode.

Everything here is configured by the ``[auth.browser]`` table in
``data/20-auth.toml``; nothing about a particular browser is decided in this
file.
"""

from __future__ import annotations

import shlex
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..core.logging_setup import get_logger

if TYPE_CHECKING:  # pragma: no cover - import cycle only matters to type checkers
    from ..core.config import Settings
    from ..core.context import RunContext

log = get_logger("auth.browser")

#: Placeholder the BROWSER convention uses for the URL, understood by Python's
#: webbrowser, by xdg-open and by node's opener -- i.e. by everything that will
#: read what this module writes.
URL_PLACEHOLDER = "%s"

#: Where :func:`prepare_browser` leaves its plan in ``ctx.state``, so that a
#: sign-in which prints a URL can still open it in the browser chosen here.
STATE_KEY = "auth.browser_plan"


@dataclass
class BrowserPlan:
    """What phase 2 will put into the environment before any sign-in runs."""

    #: Full BROWSER-style command line, URL placeholder included.
    command: str = ""
    #: The executable the command starts with, for reporting.
    path: str = ""
    #: Dedicated profile directory, when one was created.
    profile_dir: str = ""
    #: Variables to overlay onto every child process.
    env: dict[str, str] = field(default_factory=dict)
    #: True when ``command`` may be exported as BROWSER. False for an opener
    #: that reads BROWSER itself and would otherwise recurse.
    exportable: bool = False
    #: Human-readable account of every decision, for the phase report.
    notes: list[str] = field(default_factory=list)
    #: What to suggest when nothing was found.
    remediation: list[str] = field(default_factory=list)

    @property
    def own_profile(self) -> bool:
        """True when the command opens a profile of this program's own."""
        return bool(self.command and self.profile_dir)

    def argv_for(self, url: str) -> list[str]:
        """The command, split, with ``url`` where the placeholder was."""
        words = shlex.split(self.command, comments=False, posix=True)
        return [url if word == URL_PLACEHOLDER else word.replace(URL_PLACEHOLDER, url) for word in words]

    @property
    def changes_anything(self) -> bool:
        return bool(self.env) or bool(self.command and self.exportable)

    def detail(self) -> str:
        """One line for the console, reading as a sentence rather than a dump."""
        return "; ".join(self.notes) if self.notes else "nothing to prepare"

    def apply(self, ctx: "RunContext") -> None:
        """Overlay the plan onto every child process this run will start.

        ``base_env`` rather than ``os.environ``: the change belongs to this run
        and to the processes it starts, and must not outlive either.
        """
        ctx.runner.base_env.update(self.env)
        if self.command and self.exportable:
            ctx.runner.base_env["BROWSER"] = self.command
        for key, value in sorted(self.env.items()):
            log.debug("browser env: %s=%s", key, value)


def plan_browser(ctx: "RunContext") -> BrowserPlan:
    """Decide which browser to open and what it needs to work, changing nothing."""
    cfg = ctx.settings.section("auth.browser")
    plan = BrowserPlan()
    _plan_input_method(ctx, cfg, plan)
    _plan_command(ctx, cfg, plan)
    return plan


def prepare_browser(ctx: "RunContext") -> BrowserPlan:
    """Plan, then apply -- unless this is a rehearsal, in which case only plan."""
    plan = plan_browser(ctx)
    if not ctx.dry_run:
        plan.apply(ctx)
    ctx.state[STATE_KEY] = plan
    return plan


def planned_browser(ctx: "RunContext") -> BrowserPlan | None:
    """The plan :func:`prepare_browser` settled for this run, if it ran."""
    plan = ctx.state.get(STATE_KEY)
    return plan if isinstance(plan, BrowserPlan) else None


def private_window_command(ctx: "RunContext", plan: BrowserPlan | None) -> str:
    """How to open a URL in a private window of the browser found here, or "".

    A private window starts with no cookies, which is the one way to be asked
    who you are in a browser this program does not own. The arguments are
    ``auth.browser.private_args``, matched against the browser's file name the
    same way the profile arguments are.
    """
    if plan is None or not plan.path:
        return ""
    table: dict[str, Any] = ctx.settings.section("auth.browser").dict_("private_args")
    name = Path(plan.path).name.lower()
    key = max((k for k in table if k.lower() in name), key=len, default="")
    if not key:
        return ""
    return " ".join(shlex.quote(str(part)) for part in (plan.path, *table[key]))


# -- the input-method fix ------------------------------------------------------


def _plan_input_method(ctx: "RunContext", cfg: "Settings", plan: BrowserPlan) -> None:
    """Decide whether keystrokes need help reaching the page, and record why.

    The ``auto`` test is deliberately narrow.  Overriding a working input
    method would stop somebody composing Chinese, Japanese or Korean text in
    the very form they are trying to fill in, which is a worse failure than the
    one being fixed -- so the fix is applied only where all three of its
    preconditions hold at once and no input method is configured to break.
    """
    mode = cfg.str_("input_method_fix", "auto").strip().lower()
    overlay = {str(k): str(v) for k, v in cfg.dict_("input_method_env").items()}
    if not overlay or mode == "never":
        return

    if mode == "always":
        plan.env.update(overlay)
        plan.notes.append("input-method fix forced on (auth.browser.input_method_fix)")
        return

    host = ctx.platform
    if not host.is_linux or not host.display or host.desktop_session:
        return
    if host.input_method:
        log.debug("input method already configured (%s), leaving it alone", host.input_method)
        return

    plan.env.update(overlay)
    plan.notes.append(
        f"input-method fix applied ({host.display} has no desktop session and no input "
        "method, where a browser can paint typed text without the page receiving it)"
    )


# -- which browser -------------------------------------------------------------


def _plan_command(ctx: "RunContext", cfg: "Settings", plan: BrowserPlan) -> None:
    explicit = cfg.str_("command").strip()
    export = cfg.bool_("set_browser_variable", True)

    if explicit:
        plan.command = explicit if URL_PLACEHOLDER in explicit else f"{explicit} {URL_PLACEHOLDER}"
        plan.path = next(iter(shlex.split(explicit, comments=False, posix=True)), explicit)
        plan.exportable = export
        # First, like every other branch: which browser is the thing the
        # operator is looking for, and the environment note is the footnote.
        plan.notes.insert(0, f"browser: {plan.command} (configured)")
        return

    for name in cfg.str_list("candidates"):
        found = ctx.runner.which(name)
        if not found:
            continue
        plan.path = found
        args = _profile_args(ctx, cfg, plan)
        plan.command = " ".join(shlex.quote(part) for part in (found, *args, URL_PLACEHOLDER))
        plan.exportable = export
        plan.notes.insert(0, f"browser: {found}")
        return

    # Nothing this program recognises. An opener may still reach one -- the
    # Windows side of a WSL install, a flatpak, a handler with no binary on
    # PATH -- but it cannot be named in BROWSER if it is going to read BROWSER.
    recursive = {n.lower() for n in cfg.str_list("openers_reading_browser_variable", ["xdg-open", "gio"])}
    for name in cfg.str_list("openers", ["wslview", "xdg-open", "gio", "open"]):
        found = ctx.runner.which(name)
        if not found:
            continue
        plan.path = found
        plan.command = f"{shlex.quote(found)} {URL_PLACEHOLDER}"
        plan.exportable = export and name.lower() not in recursive
        plan.notes.insert(
            0,
            f"browser: none installed; {name} will be asked to find one"
            if not plan.exportable
            else f"browser: {found}",
        )
        return

    plan.notes.insert(0, "browser: none found")
    plan.remediation = [
        "No browser and no way to launch one was found on this machine.",
        "Either install one -- initialize-claude-ai run --phases 1 --install-only browser --",
        "or sign in by carrying the URL to a browser elsewhere: add --headless.",
    ]


def _profile_args(ctx: "RunContext", cfg: "Settings", plan: BrowserPlan) -> list[str]:
    """Arguments that give the sign-in a profile of its own, when that helps.

    The default profile is the right place to sign in on somebody's desktop:
    they are probably already signed in there, and the flow finishes in two
    clicks.  It is the wrong place everywhere else.  Root's Firefox and the
    desktop user's Firefox are different profiles anyway; a shared machine's
    default profile may hold a colleague's account, which is the "it signed in
    as the wrong person" failure; and an ad blocker that hides a form field
    turns a sign-in into an unexplained dead end.
    """
    mode = cfg.str_("clean_profile", "auto").strip().lower()
    if mode == "never":
        return []
    # A reset is a request to be asked for an account again, and the cookie that
    # would prevent that lives in a browser profile. So the "auto" judgement --
    # is the default profile safe to reuse -- stops being the question: a
    # profile of our own is required, and the one from last time is emptied
    # below. ``never`` still wins, because it is an explicit answer.
    reset = ctx.settings.bool_("auth.reset", False) and ctx.settings.bool_("auth.reset_browser_profile", True)
    if mode == "always" or reset:
        why = "requested" if mode == "always" else "credentials reset"
    elif not ctx.platform.desktop_session:
        why = "no desktop session owns the default profile"
    elif ctx.platform.is_root:
        why = "running as root"
    else:
        return []

    table: dict[str, Any] = cfg.dict_("profile_args")
    name = Path(plan.path).name.lower()
    # Longest match wins so that a family key cannot be shadowed by a shorter
    # one that happens to be a substring of the same file name.
    key = max((k for k in table if k.lower() in name), key=len, default="")
    if not key:
        log.debug("no profile arguments known for %s, using its default profile", name)
        return []

    profile = ctx.state_dir / "browser-profile" / key
    if reset and profile.exists():
        # Only ever this program's own scratch profile, never the operator's
        # real one -- that is why the path is built here rather than read from
        # configuration, and why ``never`` was allowed to return above.
        if ctx.dry_run:
            plan.notes.append(f"would clear the sign-in profile at {profile}")
        else:
            try:
                shutil.rmtree(profile)
                plan.notes.append(f"cleared the sign-in profile at {profile}")
            except OSError as exc:
                plan.notes.append(f"could not clear the sign-in profile at {profile}: {exc}")
    profile = ctx.ensure_dir(profile)
    plan.profile_dir = str(profile)
    plan.notes.append(f"own profile at {profile} ({why})")
    return [str(arg).replace("{profile}", str(profile)) for arg in table[key]]
