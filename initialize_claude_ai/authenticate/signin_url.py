"""Getting the sign-in URL out of the terminal it was printed into.

A pasted-code sign-in hands the operator a URL several hundred characters long
and asks them to open it somewhere else.  Both providers print it into a
terminal, and both of them make it hard to get back out again -- in different
ways, which is why neither has a fix that works for the other:

``gcloud auth login --no-launch-browser``
    prints the URL as one unbroken line and lets the terminal wrap it for
    display.  Most emulators will give you the whole thing back on a copy, and
    tmux, screen and a window resized after the fact will not; you get the first
    row and nothing else.  What is left is still a valid-looking authorization
    link, so Google renders a sign-in page that accepts an email address and
    then goes nowhere, with nothing on it to say the link arrived truncated.

``claude setup-token``
    draws its output through a layout engine that hard-wraps to the terminal
    width *before* printing, so the newlines are really there in the stream.  No
    terminal can give that URL back in one piece, because it was never sent as
    one piece.

Neither problem can be solved at the far end, so this solves it at the near end.
:class:`SignInUrlRelay` watches what the child writes, recovers the URL whole,
and puts it somewhere the operator can reach without selecting anything: a file
they can ``cat`` from another terminal, and the system clipboard.

Recovering it whole is possible even in the hard-wrapped case because the claude
CLI also emits each fragment as an OSC 8 hyperlink, and an OSC 8 escape carries
the *entire* target regardless of how the visible text was broken up.  So the
escape sequences are read first and the plain text second.

The URL is not a credential -- it is an authorization request, and opening it
proves nothing without signing in -- but it is short-lived and specific to one
attempt, so the file is written private to its owner and removed when the
sign-in finishes.

**A reset has to reach the browser, and a pasted URL goes around it.**
``--force-auth-with-reset`` revokes the credential on this machine, and
:mod:`.browser` empties the profile this program keeps for signing in. Neither
touches the browser the operator pastes the URL into, and that browser still
holds the provider's session cookie -- so the provider recognised them and
waved them through with no email and no password, straight after a reset that
had worked. Nothing was wrong on this machine; the sign-in simply never met the
clean profile, because in a pasted-code flow nothing opens a browser.

That is not this program's call to make, though. A pasted-code flow on a
host that has a display only happens because the operator asked for it
(``--headless``, ``auth.browser_mode = headless``), and what they asked for is
to carry the URL themselves. So nothing is opened on their behalf -- not by
this module, which opens the captured URL only under an explicit
``auth.signin.open_url = always``, and not by the sign-in command, whose
``BROWSER`` is replaced for the pasted-code flow (``auth.signin.paste_env``).
After a reset the operator is told instead that the reset did not sign their
browser out, and how to get a sign-in that asks: this program's emptied
profile, a private window, or signing out of the provider first -- each as a
command to run themselves. Where the provider allows it (``auth.<backend>.signout_continue_param``), the
captured URL itself carries the reset to that browser: it goes through the
provider's sign-out page first, which then continues to the sign-in -- see
:meth:`SignInUrlRelay.signs_out_first`.

**The provider's sign-out reaches only the provider.** Two things outlive it in
the operator's browser. The provider remembers which accounts were used there
(Google keeps them, signed out, in its account chooser) and may carry on with
one of them unasked; ``auth.<backend>.reset_prompt`` asks for the chooser
instead (Google's ``prompt=select_account``), so the operator picks or enters
the account. And an account that signs in through its organisation's own
sign-in page (single sign-on) holds a session *there*, on a site the provider's
sign-out never touches, which signs them straight back in. Each organisation
has its own such page, so no address for it can be shipped;
``auth.signin.idp_signout_url`` names one, as the first stop. The one thing
that reaches all of it at once is a browser with no cookies -- a private
window, or this program's emptied profile -- and the advice says so first. The program never touches a browser
profile that is not its own; clearing the operator's cookies would sign them out of
everything else they have open.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import quote, unquote_plus, urlsplit

from ..core.config import fill
from ..core.fileio import write_private
from ..core.logging_setup import get_logger

if TYPE_CHECKING:  # pragma: no cover - import cycle only matters to type checkers
    from ..core.context import RunContext
    from .browser import BrowserPlan

log = get_logger("auth.url")

#: ``ESC ] 8 ; <params> ; <uri> BEL`` (or terminated by ST).  The visible text
#: between an opening and closing pair may be wrapped to ribbons; the URI in the
#: escape itself never is.
OSC8 = re.compile(r"\x1b\]8;[^;]*;([^\x07\x1b]+)(?:\x07|\x1b\\)")

#: Values of ``auth.signin.open_url``. ``auto`` opens the URL unless the
#: operator asked to carry it themselves (``--headless``).
OPEN_AUTO = "auto"
OPEN_ALWAYS = "always"
OPEN_NEVER = "never"
OPEN_URL_MODES = (OPEN_AUTO, OPEN_ALWAYS, OPEN_NEVER)

#: Values of ``auth.signin.sign_out_first``; anything else means never.
SIGN_OUT_RESET = "reset"
SIGN_OUT_ALWAYS = "always"

#: Everything a terminal is meant to act on rather than show, so that what is
#: left can be searched as text.
ANSI = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b[@-Z\\-_]")


def with_prompt(url: str, prompt: str) -> str:
    """``url`` asking for the ``prompt`` values too, everything else untouched.

    OAuth's ``prompt`` is a space-separated list, so values already there are
    kept and only the missing ones are added. The rest of the query is left
    byte for byte: the sign-in belongs to the command that printed it.
    """
    wanted = prompt.split()
    if not wanted:
        return url
    base, hash_mark, fragment = url.partition("#")
    head, question, query = base.partition("?")
    parts = query.split("&") if query else []
    for index, part in enumerate(parts):
        name, _, value = part.partition("=")
        if name == "prompt":
            have = unquote_plus(value).split()
            parts[index] = "prompt=" + quote(" ".join(have + [w for w in wanted if w not in have]), safe="")
            break
    else:
        parts.append("prompt=" + quote(" ".join(wanted), safe=""))
    return f"{head}?{'&'.join(parts)}{hash_mark}{fragment}"


@dataclass
class SignInUrlRelay:
    """Watches a sign-in's output for its URL and puts it within reach."""

    ctx: "RunContext"
    #: Every URL seen, in the order they appeared. gcloud prints two.
    seen: list[str] = field(default_factory=list)
    #: Human-readable account of where the latest one was put.
    delivered: list[str] = field(default_factory=list)
    #: True for a pasted-code sign-in, where nothing else opens a browser.
    paste: bool = False
    #: The URL opened in a browser by :meth:`_open`, once per sign-in.
    opened: str = ""
    #: The backend's sign-out page (``auth.<backend>.signout_url``), or "".
    signout_url: str = ""
    #: The query parameter that page follows once the browser is signed out
    #: (``auth.<backend>.signout_continue_param``), or "" when it has none.
    signout_continue: str = ""
    #: ``prompt`` values the sign-in behind the sign-out asks for
    #: (``auth.<backend>.reset_prompt``), or "".
    reset_prompt: str = ""
    #: What each sign-in URL was handed over as, when that differs from it.
    #: :class:`UrlOnScreen` paints the same, so no copy of it goes around.
    carried_as: dict[str, str] = field(default_factory=dict)
    _tail: str = ""

    @property
    def config(self) -> Any:
        return self.ctx.settings.section("auth.signin")

    @property
    def url_file(self) -> Path:
        name = self.config.str_("url_file", "sign-in-url.txt")
        return self.ctx.state_dir / name

    # -- watching ---------------------------------------------------------------

    def feed(self, chunk: str) -> None:
        """Called with each piece of the child's output, as it is written."""
        # A URL can straddle two reads, so carry a tail forward. Bounded, because
        # this runs for as long as the sign-in does.
        text = self._tail + chunk
        self._tail = text[-4096:]
        pattern = self.config.str_("url_pattern") or r"https://\S{40,}"
        try:
            wanted = re.compile(pattern)
        except re.error:
            log.warning("auth.signin.url_pattern is not a valid regex; ignoring it")
            wanted = re.compile(r"https://\S{40,}")

        # Complete means something followed it. An OSC 8 target is whole by
        # construction; a plain-text match that runs to the end of what has
        # arrived may be continued by the next read, and opening that prefix
        # would put exactly the truncated link this class exists to prevent in
        # front of the operator.
        plain = ANSI.sub("", text)
        candidates = [(target, True) for target in OSC8.findall(text)]
        candidates += [(m.group(0), m.end() < len(plain)) for m in wanted.finditer(plain)]
        for candidate, complete in candidates:
            url = candidate.strip().strip("\"'<>")
            if wanted.match(url):
                self._remember(url)
                if complete:
                    self._open(url)

    def _remember(self, url: str) -> None:
        """Keep ``url`` unless it is a piece of one already held.

        The first row of a hard-wrapped URL is itself long enough to look like a
        URL, so reading the text as well as the escapes turns up the whole thing
        *and* a prefix of it. Delivering that prefix afterwards would hand over
        exactly the truncated link this class exists to prevent -- it is still a
        valid-looking authorization URL, and the page it opens fails silently.

        Prefixes are how a broken URL differs from a real second one: wrapping
        cuts from the end, while two genuine sign-in URLs differ at their state
        or nonce, in the middle. So the longer of a prefix pair always wins, and
        a whole URL arriving after a fragment replaces it.
        """
        for index, known in enumerate(self.seen):
            if known.startswith(url):  # already held, whole or in a longer form
                return
            if url.startswith(known):  # the whole of something seen cut short
                self.seen[index] = url
                self.deliver(self._carry(url, first=index == 0))
                return
        self.seen.append(url)
        self.deliver(self._carry(url, first=len(self.seen) == 1))

    def _carry(self, url: str, *, first: bool) -> str:
        carried = self.carried(url, first=first)
        if carried != url:
            self.carried_as[url] = carried
            log.debug("sign-in URL handed over through %s first", " then ".join(self.signout_stops()))
        return carried

    def on_screen(self, url: str) -> str:
        """What to paint in place of ``url``: the form it was handed over in."""
        return self.carried_as.get(url, url)

    # -- delivering -------------------------------------------------------------

    def deliver(self, url: str) -> None:
        """Put ``url`` everywhere the configuration asks for, quietly.

        Quietly on purpose. The child owns the screen at this moment and may be
        repainting it; anything printed here would be overwritten, or would
        corrupt a layout mid-draw. What the operator needs to know was said
        before the child started -- see :meth:`announce`.
        """
        self.delivered = []
        for method in self.config.str_list("deliver", ["file", "clipboard"]):
            handler = getattr(self, f"_to_{method.replace('-', '_')}", None)
            if handler is None:
                log.warning("unknown auth.signin.deliver method %r", method)
                continue
            try:
                note = handler(url)
            except OSError as exc:
                log.debug("delivering the sign-in URL by %s failed: %s", method, exc)
                continue
            if note:
                self.delivered.append(note)
                log.debug("sign-in URL delivered: %s", note)

    # -- signing the carrying browser out first --------------------------------

    def signs_out_first(self) -> bool:
        """Will the URL handed over sign the browser it is opened in out first?

        What a pasted URL meets is the operator's own browser, on this machine
        or another, and that browser's session with the provider is out of this
        program's reach -- except through the provider. Google's sign-out page
        follows a ``continue`` address on its own domain once the session is
        gone, so a URL that goes there first, then on to the sign-in, reaches
        whichever browser it is pasted into and arrives at a sign-in with no
        Google session behind it. No URL parameter can end that session: Google
        documents only ``none``, ``consent`` and ``select_account`` as prompts
        (the last is added through ``reset_prompt``, for the accounts Google
        still remembers). The sign-out reaches Google only; an organisation's
        own sign-in page needs ``auth.signin.idp_signout_url``, or a private
        window.

        ``auth.signin.sign_out_first`` decides when (``reset``: after
        ``--force-auth-with-reset``, the default; ``always``; ``never``). Only
        in a pasted-code flow -- the redirect flow opens this program's own
        emptied profile -- only where the child runs on this program's
        terminal proxy, the one place every copy of the URL can be made the
        same (file, clipboard, and the screen, through :class:`UrlOnScreen`),
        and only where there is a sign-out to go through: the backend's (a
        page and the parameter it follows) or the organisation's
        (``auth.signin.idp_signout_url``).
        """
        if not self.paste or not self.enabled() or not self.signout_stops():
            return False
        if not self.config.str_list("deliver", ["file", "clipboard"]):
            return False
        mode = self.config.str_("sign_out_first", SIGN_OUT_RESET).strip().lower()
        if mode == SIGN_OUT_ALWAYS:
            return True
        if mode == SIGN_OUT_RESET:
            return self.ctx.settings.bool_("auth.reset", False)
        return False

    def carried(self, url: str, *, first: bool = True) -> str:
        """The URL to hand over: ``url``, or the sign-out page continuing to it.

        Only the first sign-in URL of a run is sent through the sign-out: once
        the operator has signed in again, the session in that browser is the one
        just chosen, and signing it out a second time would only ask twice.
        Only a URL on the sign-out page's own host, too -- the provider refuses
        to continue anywhere else, and a refusal would break the sign-in rather
        than protect it.
        """
        if not first or not self.signs_out_first():
            return url
        target = url
        if self.provider_signs_out():
            if urlsplit(url).hostname != urlsplit(self.signout_url).hostname:
                log.debug("sign-in URL is not on %s; handing it over unchanged", self.signout_url)
                return url
            target = with_prompt(url, self.reset_prompt)
            joiner = "&" if urlsplit(self.signout_url).query else "?"
            target = f"{self.signout_url}{joiner}{self.signout_continue}={quote(target, safe='')}"
        idp = self.idp_signout_url()
        if idp:
            target = fill(idp, {"continue": quote(target, safe="")})
        return target

    def provider_signs_out(self) -> bool:
        """Does the backend name a sign-out page and the parameter it follows?"""
        return bool(self.signout_url and self.signout_continue)

    def idp_signout_url(self) -> str:
        """The organisation's sign-out page, ``{continue}`` and all, or "".

        Only a template that says where to continue to: a sign-out that ends
        on its own page would never reach the sign-in.
        """
        template = self.config.str_("idp_signout_url").strip()
        if template and "{continue}" not in template:
            log.warning("auth.signin.idp_signout_url has no {continue}; not signing out there")
            return ""
        return template

    def signout_stops(self) -> list[str]:
        """The sign-out pages the URL goes through, in the order it meets them."""
        idp = self.idp_signout_url()
        stops = [urlsplit(idp).hostname or idp] if idp else []
        return stops + ([self.signout_url] if self.provider_signs_out() else [])

    # -- opening it where no earlier sign-in survives ---------------------------

    def _open(self, url: str) -> None:
        """Open ``url`` in the browser found for this run, once, if it is to be.

        The whole URL, not the fragment that completed it: ``url`` may be a
        piece of one already held, and the held one is the longest seen. And
        in the form it was handed over in, so the window opened here is the
        same URL as the file, the clipboard and the screen.
        """
        if self.opened or not self.opens_browser():
            return
        whole = next((known for known in self.seen if known.startswith(url)), url)
        from .browser import planned_browser

        plan = planned_browser(self.ctx)
        if plan is None:
            return
        self.opened = whole
        result = self.ctx.runner.launch(
            plan.argv_for(self.on_screen(whole)),
            settle=self.config.float_("open_settle_seconds", 2.0),
            label="sign-in browser",
        )
        if result.ok:
            where = f" ({plan.profile_dir})" if plan.profile_dir else ""
            self.delivered.append(f"opened in {plan.path}{where}")
            log.debug("sign-in URL opened in %s%s", plan.path, where)
        else:
            log.debug("could not open the sign-in URL in %s (rc=%s)", plan.path, result.returncode)

    def opens_browser(self) -> bool:
        """Will the captured URL be opened in a browser for the operator?

        ``auth.signin.open_url`` decides. ``auto`` (the default) opens it
        unless the operator asked to carry it themselves: every sign-in takes
        a pasted code now (:meth:`RunContext.uses_pasted_code`), and without
        ``--headless`` the browser coming up by itself is what they expect.
        ``--headless`` is the request to be left alone, so under it only
        ``always`` opens anything. Beyond that: only in a pasted-code flow (in
        the redirect flow the sign-in command opens the browser itself, and a
        second window would be a second, competing sign-in); only where the URL
        is captured, since there is nothing to open otherwise; only where a
        browser can be shown, and one was found.
        """
        if not self.paste or not self.enabled():
            return False
        mode = self.open_url_mode()
        if mode == OPEN_NEVER:
            return False
        if mode == OPEN_AUTO and self.ctx.browser_mode() == "headless":
            return False
        if self.ctx.platform.headless:
            return False
        from .browser import planned_browser

        plan = planned_browser(self.ctx)
        return plan is not None and bool(plan.command)

    def open_url_mode(self) -> str:
        """``auth.signin.open_url``, normalised; anything unrecognised means never.

        ``reset`` was once the default and opened the URL after every
        ``--force-auth-with-reset`` -- under ``--headless`` too, for the
        operator who had asked for no browser. It is read as ``never`` now,
        with a note in the log, so a setting kept from then cannot bring that
        back.
        """
        mode = self.config.str_("open_url", OPEN_AUTO).strip().lower() or OPEN_AUTO
        if mode not in OPEN_URL_MODES:
            log.debug("auth.signin.open_url=%r is not one of %s; opening nothing", mode, OPEN_URL_MODES)
            return OPEN_NEVER
        return mode

    def after_reset(self) -> bool:
        """Were credentials cleared before this sign-in, and the profile with them?"""
        settings = self.ctx.settings
        return settings.bool_("auth.reset", False) and settings.bool_("auth.reset_browser_profile", True)

    def session_advice(self, signout_url: str = "") -> list[str]:
        """What a reset did and did not clear, said before the sign-in starts.

        Empty unless credentials were reset: an ordinary sign-in that reuses
        the browser's session is the sign-in working, not a problem to explain.
        """
        if not self.after_reset() and not self.signs_out_first():
            return []
        from .browser import planned_browser, private_window_command

        plan = planned_browser(self.ctx)
        to_file = self.enabled() and "file" in self.config.str_list("deliver", ["file", "clipboard"])
        private = private_window_command(self.ctx, plan) if not self.ctx.platform.headless else ""
        in_private = f': {private} "$(cat {shlex.quote(str(self.url_file))})"' if private and to_file else ""
        if self.opens_browser() and plan is not None and plan.own_profile and self.after_reset():
            lines = [
                f"Credentials were reset, so the URL opens in {Path(plan.path).name} with this "
                f"program's own profile, emptied just now ({plan.profile_dir}). No earlier sign-in "
                "survives there, so you will be asked who you are -- sign in in that window.",
            ]
            if in_private:
                lines.append(f"To use another browser instead, open the URL in a private window{in_private}.")
            return lines
        if self.signs_out_first():
            hosts = [urlsplit(stop).hostname or stop for stop in self.signout_stops()]
            lines = [
                "For a sign-in that asks for everything, open the URL in a private (incognito) "
                f"window{in_private}. It starts with no cookies for any site, so nothing signed in "
                "before can carry you through.",
                "The reset cleared the credentials on this machine, and the captured URL also "
                f"signs the browser it is opened in out of {' and '.join(hosts)} first -- every "
                "account signed in there -- before the sign-in.",
            ]
            if self.provider_signs_out() and self.reset_prompt:
                lines.append(
                    "The sign-in then shows the accounts that browser remembers: choose "
                    "'Use another account' to enter your email."
                )
            if not self.idp_signout_url():
                lines.append(
                    "A sign-out reaches only its own site. If your account signs in through your "
                    "organisation's own sign-in page (single sign-on), that page keeps you signed "
                    "in and lets you through with no password -- use the private window, or set "
                    "auth.signin.idp_signout_url to that page's sign-out address."
                )
            lines.append(
                "The URL on screen, in the file and on the clipboard is the same one: use any "
                "of them, but open it before you sign in anywhere else in that browser."
            )
            own = self.own_profile_command(plan) if not self.ctx.platform.headless else ""
            if own:
                lines.append(f"Or open it in this program's emptied profile, which also starts clean: {own}")
            return lines
        lines = [
            "The reset cleared the credentials on this machine, not your browser's session. A "
            "browser still signed in to the provider will let you through without asking for a "
            "password.",
        ]
        own = self.own_profile_command(plan) if to_file and not self.ctx.platform.headless else ""
        if own:
            lines.append(f"To be asked who you are, open the URL yourself in this program's emptied profile: {own}")
        private = private_window_command(self.ctx, plan)
        how = f' -- here: {private} "$(cat {self.url_file})"' if private and to_file else ""
        lines.append(f"{'Or open' if own else 'To be asked who you are, open'} the URL in a private window{how}.")
        if signout_url:
            lines.append(f"Or sign out of the provider first: {signout_url}")
        return lines

    def own_profile_command(self, plan: "BrowserPlan | None") -> str:
        """The command that opens the URL file in this program's emptied profile, or "".

        Said, never run: the operator chose to open the URL themselves. Empty
        unless a reset emptied that profile -- a kept one is not clean.
        """
        if plan is None or not plan.own_profile or not self.after_reset():
            return ""
        marker = "{url}"
        source = f'"$(cat {shlex.quote(str(self.url_file))})"'
        return " ".join(source if word == marker else shlex.quote(word) for word in plan.argv_for(marker))

    def _to_file(self, url: str) -> str:
        path = self.url_file
        self.ctx.ensure_dir(path.parent)
        # Short-lived and specific to one attempt, but still nobody else's
        # business on a shared machine.
        write_private(path, url + "\n")
        return f"written to {path}"

    def clipboards(self) -> list[tuple[str, str, list[str], str]]:
        """``(name, path, argv, where)`` for each clipboard tool usable here, best first.

        Installed is not usable: wl-copy on an X display (or xclip under a bare
        Wayland session) fails every time, and announcing it named a clipboard
        the URL never reached. ``auth.signin.clipboard_needs`` names the
        variable each tool needs in the environment the sign-in runs in; a tool
        with none listed is taken as usable. ``where`` is that variable's value
        -- which display's clipboard it is, because over ssh or on a machine
        reached some other way it may not be the operator's own.
        """
        env = self.ctx.runner.build_env()
        needs = self.config.dict_("clipboard_needs")
        usable = []
        for name, argv in self.config.dict_("clipboard").items():
            required = [str(var) for var in needs.get(name, [])]
            if any(not env.get(var) for var in required):
                continue
            path = self.ctx.runner.which(name)
            if not path:
                continue
            where = ", ".join(env[var] for var in required)
            usable.append((name, path, [path, *(str(a) for a in argv[1:])], where))
        return usable

    def _to_clipboard(self, url: str) -> str:
        for name, _path, command, where in self.clipboards():
            # Neither captured nor inherited. X11 and Wayland have no clipboard
            # daemon of their own, so xclip, xsel and wl-copy all fork a process
            # that stays alive owning the selection -- it would hold a captured
            # pipe open for as long as the copy lasts, and this call would never
            # return. Inheriting instead would let it paint over the sign-in the
            # operator is reading. /dev/null is the only answer that does
            # neither; the exit status still says whether the copy was taken.
            result = self.ctx.runner.run(
                command,
                stdin_text=url,
                timeout=self.config.float_("clipboard_timeout_seconds", 10),
                capture=False,
                discard_output=True,
                label="clipboard",
            )
            if result.ok:
                return f"copied to the clipboard ({name}{f' on {where}' if where else ''})"
            log.debug("%s could not take the clipboard (rc=%s)", name, result.returncode)
        return ""

    # -- telling the operator ----------------------------------------------------

    def announce(self) -> list[str]:
        """What to say *before* the child starts, while the screen is still ours."""
        if not self.enabled():
            return []
        lines = []
        if self.opens_browser():
            from .browser import planned_browser

            plan = planned_browser(self.ctx)
            name = Path(plan.path).name if plan is not None and plan.path else "the browser"
            lines.append(
                f"The URL opens in {name} by itself (auth.signin.open_url; --headless opens "
                "nothing). The copies below are for another browser."
            )
        lines += [
            "The URL will wrap on screen. You do not have to select it: it is captured whole the moment it appears.",
            f"From another terminal:  cat {self.url_file}",
        ]
        clipboard = self.clipboard_tool()
        if clipboard:
            lines.append(f"It is also copied to the clipboard ({clipboard}) -- just paste it.")
        elif any(self.ctx.runner.which(name) for name in self.config.dict_("clipboard")):
            needs = sorted({str(v) for vs in self.config.dict_("clipboard_needs").values() for v in vs})
            lines.append(
                f"No clipboard is reachable from this session (none of {', '.join(needs)} is set); use the file above."
            )
        else:
            lines.append(
                "No clipboard tool is installed; for one, run: initialize-claude-ai run "
                "--phases 1 --install-only clipboard"
            )
        return lines

    def clipboard_tool(self) -> str:
        """The clipboard the URL will go to, as said to the operator, or ""."""
        name, _path, _argv, where = next(iter(self.clipboards()), ("", "", [], ""))
        return f"{name}, display {where}" if name and where else name

    def enabled(self) -> bool:
        """Is there any point watching? Needs a terminal to sit in the middle of."""
        mode = self.config.str_("pty", "auto").strip().lower()
        if mode == "never":
            return False
        if mode == "always":
            return True
        return self.ctx.runner.tty_available()

    def cleanup(self) -> None:
        """Remove the URL file once the sign-in is over, successful or not.

        It is worth nothing by then -- the authorization request behind it is
        single-use and short-lived -- and a stale one left lying about is a file
        somebody will find later and try.
        """
        try:
            self.url_file.unlink(missing_ok=True)
        except OSError as exc:  # pragma: no cover - a read-only state dir
            log.debug("could not remove %s: %s", self.url_file, exc)


#: A URL as painted on a terminal: it ends at whitespace, at an escape sequence
#: (colour codes, an OSC 8 terminator) or at a quote.
SCREEN_URL = re.compile(r"https://[^\s\x1b\x07\"'<>]+")
#: How much text may be held back waiting for a URL to end. A sign-in URL is a
#: few hundred characters; anything longer is released as it is.
SCREEN_HOLD_LIMIT = 16384


@dataclass
class UrlOnScreen:
    """Paints a sign-in URL the way :class:`SignInUrlRelay` handed it over.

    Handing over a sign-out-first URL through the file and the clipboard alone
    was not enough: the sign-in still printed its own URL on screen, and that
    is the copy an operator selects, Ctrl+clicks, or copies over ssh -- the
    clipboard of the display this program found may not even be theirs. The
    screen copy went straight to the sign-in, into a browser still signed in.
    So every copy has to be the same URL, and the screen is one of them.

    Only the painting changes; the relay still reads the child's own output.
    A URL still arriving is held back until something ends it, or until the
    child goes quiet (``flush``), so it is never painted half-rewritten.
    """

    relay: SignInUrlRelay
    _held: str = ""

    def feed(self, text: str) -> str:
        text = self._held + text
        self._held = ""
        painted: list[str] = []
        position = 0
        for match in SCREEN_URL.finditer(text):
            painted.append(text[position : match.start()])
            if match.end() == len(text) and len(text) - match.start() < SCREEN_HOLD_LIMIT:
                self._held = text[match.start() :]
                return "".join(painted)
            painted.append(self.relay.on_screen(match.group(0)))
            position = match.end()
        rest = text[position:]
        # The start of the scheme itself may be all that has arrived.
        keep = next((n for n in range(min(len(rest), 7), 0, -1) if "https://".startswith(rest[-n:])), 0)
        if keep:
            self._held = rest[-keep:]
            rest = rest[:-keep]
        painted.append(rest)
        return "".join(painted)

    def flush(self) -> str:
        held, self._held = self._held, ""
        return SCREEN_URL.sub(lambda m: self.relay.on_screen(m.group(0)), held)
