# Phase 2 — authenticate

Three backends, each independently selectable with `--auth-backend`
(`google-cloud`, `claude-oauth`, `anthropic-api`, or `all`):

- **Google Cloud** — user login, service-account key, impersonation, ADC-only,
  workload identity or the GCE metadata server, chosen automatically from what
  is configured and what the host looks like. Application-default credentials
  are what Vertex actually reads, so that is what decides the verdict; a stale
  `gcloud auth login` session is reported without failing the phase.
- **claude.ai / Claude Code CLI** — `claude setup-token` under the pasted-code
  flow, `claude /login` under the redirect flow, plus an existing-session check.
  Note these mint *different* credentials: a long-lived OAuth token versus an
  interactive session.
- **Anthropic API** — a key named by reference and proven with the cheapest
  authenticated call there is.

The phase runs in two passes. **Detection** asks every backend what credential it
already holds and changes nothing; only then does **acquisition** sign in, and
only for the backends that came up empty:

```
✔ [   OK   ] detect  already authenticated: google-cloud, claude-oauth; not yet: anthropic-api
```

Interleaving the two would mean the first backend's browser prompt blocks before
the second has even been looked at — you would be answering questions without
knowing what is actually missing. A credential that is already there is left
alone, so re-running phase 2 is a no-op rather than a fresh sign-in.

`--force-auth` (`auth.force`) signs in again regardless. It changes what is done
about the detection result, never whether detection happens: the report says
what was already there either way.

## When `--force-auth` does not ask you for anything

Forcing a sign-in often finishes without a password, and that is not a bug in
this program: the provider still holds a session, the browser still holds its
cookie, and the login is rubber-stamped. `--force-auth` mints a *new*
credential; it does not take away the old one, and neither of those is where
the session lives.

`--force-auth-with-reset` (`auth.force` + `auth.reset`) adds a third pass,
between detection and acquisition, that clears what detection just found:

```
✔ [   OK   ] detect  already authenticated: google-cloud, claude-oauth
↻ [CHANGED ] reset   google-cloud: auth application-default revoke, auth revoke;
                     claude-oauth: auth logout, removed ~/.claude/.credentials.json
```

| Backend | What a reset clears |
| --- | --- |
| `google-cloud` | `gcloud auth application-default revoke` then `gcloud auth revoke` — the active account only, not `--all` |
| `claude-oauth` | `claude auth logout`, then the credential files `inspect` looks for, named by `auth.claude_oauth.reset.paths_from` so the two lists cannot drift apart |
| `anthropic-api` | nothing, and it says so. `api_key_refs` is a *reference* to a key that lives somewhere else — an exported variable, a file you wrote. Deleting it would be deleting your property, so the reset reports the backend is still authenticated and where from |
| the browser | this program's own sign-in profile under the state directory (`auth.reset_browser_profile`), never your real one. A reset also promotes `--browser-profile auto` to `always`, because the cookie that would skip the password is in a profile |

**A reset cannot sign your own browser out.** The provider's session cookie
lives in whichever browser you sign in with, and a browser that is still signed
in to Google or claude.ai lets you through with no email and no password, even
straight after a reset that worked. Without `--headless` the URL is opened in the
emptied profile for you, so that case is covered. Under `--headless` no browser
is opened. That is the point of `--headless`: you copy the URL and open it
yourself, reset or not. The browser phase 2 exports as `BROWSER`
is swapped out of the sign-in command's environment for this flow
(`[auth.signin.paste_env]`, `INITIALIZE_CLAUDE_AI_AUTH_SIGNIN_PASTE_BROWSER`).
That way no sign-in command, or library under it, can open that browser anyway.
Whether the URL is opened for you is `auth.signin.open_url`
(`INITIALIZE_CLAUDE_AI_AUTH_SIGNIN_OPEN_URL`):

| `open_url` | What a pasted-code sign-in does |
| --- | --- |
| `auto` (default) | Opens the whole captured URL in the browser found for the run (this program's own profile when it keeps one), unless `--headless`. You sign in there and paste the code back |
| `always` | The same, under `--headless` too |
| `never` | Leaves opening the URL to you |

It needs a display, a browser and the capture (`auth.signin.pty`). The window
opened is the same URL as the file, the clipboard and the screen, sign-out
first included. Any other value means `never`. That includes `reset`, the old default, which
opened a browser under `--headless` after every `--force-auth-with-reset`.

**After a reset, the pasted URL signs your browser out first.** With
`--headless` you open the URL in a browser of your own, and that browser is out
of this program's reach. Google's sign-in parameters cannot refuse its session
either: the only prompts it documents are `none`, `consent` and
`select_account`. What does reach that browser is Google's own sign-out page,
which continues to a Google sign-in address once the session has ended. So
after `--force-auth-with-reset`, the URL written to `sign-in-url.txt` and the
clipboard is `signout_url?continue=<the sign-in URL>`. Opening it anywhere signs
that browser out of Google (every account in it), then shows a sign-in that asks
for your email or the accounts it remembers. The verification code at the end
still belongs to gcloud's own request.

**A sign-out reaches only its own site, so the surest way is a private window.**
Two things outlive Google's sign-out in your browser:
- **The accounts Google remembers there.** Signing out keeps them in Google's
  account chooser, and Google may carry on with one of them unasked. So the
  sign-in behind the sign-out asks for the chooser (`prompt=select_account`,
  from `auth.google_cloud.reset_prompt`); choose *Use another account* to type
  an email.
- **Your organisation's sign-in page.** An account that signs in through its
  organisation's own page (single sign-on) is sent there by Google, and that
  page keeps its own session on its own site. It signs you straight back in
  with no password. Every organisation's page is different, so no address is
  shipped. Set `auth.signin.idp_signout_url` to its sign-out address, with
  `{continue}` where the page takes the address to go to next, and the URL goes
  there first. Most identity providers only continue to addresses they have
  been told to allow.

A private (incognito) window starts with no cookies for any site, so it clears
both with nothing to configure. The sign-in names it first, with the command
for the browser found here.

| Setting | Default | Meaning |
| --- | --- | --- |
| `auth.signin.sign_out_first` (`INITIALIZE_CLAUDE_AI_AUTH_SIGNIN_SIGN_OUT_FIRST`) | `reset` | `reset`: after a reset. `always`: every pasted-code sign-in. `never`, or anything else: hand the URL over unchanged |
| `auth.<backend>.signout_url` | Google's Logout page; none for claude.ai | the provider's sign-out page |
| `auth.<backend>.signout_continue_param` | `continue` for Google; none for claude.ai | the parameter that page follows afterwards. Empty turns the chaining off |
| `auth.<backend>.reset_prompt` (`INITIALIZE_CLAUDE_AI_GCP_RESET_PROMPT`, `INITIALIZE_CLAUDE_AI_CLAUDE_RESET_PROMPT`) | `select_account` for Google; none for claude.ai | `prompt` values added to the sign-in behind the sign-out, keeping any already there. Empty adds nothing |
| `auth.signin.idp_signout_url` (`INITIALIZE_CLAUDE_AI_AUTH_SIGNIN_IDP_SIGNOUT_URL`) | none | your organisation's sign-out page, with `{continue}`; the first stop when set |

Details:
- Only the first sign-in URL of a run is chained. After you sign in again, the
  browser holds the session you just chose.
- Only a URL on the sign-out page's own host is chained, because Google refuses
  to continue anywhere else. An `idp_signout_url` without `{continue}` is
  ignored, with a warning, because it would never reach the sign-in.
- Every copy you can reach is the same sign-out-first URL: the file, the
  clipboard, and the URL on screen. The screen copy is repainted as it passes
  through the program's terminal proxy, so selecting it, Ctrl+clicking it or
  copying it over ssh all sign the browser out first. What gcloud itself reads
  and does is unchanged. The sign-in and the reset confirmation both say so.

Where no chaining applies (claude.ai, no capture, or `sign_out_first=never`),
the sign-in says before it starts that your browser was not signed out. It then
lists commands you can run yourself to get a sign-in that asks who you are:
- the emptied profile, reading the URL from `sign-in-url.txt`;
- a private window, whose arguments come from `[auth.browser.private_args]` for
  each browser family;
- the provider's sign-out page.

Your everyday browser profile is never touched.

A backend is cleared when it *holds* something, which is not the same as being
authenticated: a host signed in as somebody other than `--gcloud-user-account`
is deliberately reported as not authenticated — proceeding as the wrong user is
the failure that check exists to prevent — and it is the machine most in need of
being cleared before signing in again.

It is a separate flag rather than part of `--force-auth` because it is the one
destructive thing phase 2 can do, and what it destroys is not this program's
alone: a revoked gcloud credential is gone for every tool on the host. It is
guarded three ways — it never runs unless asked for by name, it asks before
clearing anything (`--auto-agree` answers yes; a non-interactive session answers
no), and `--dry-run` reports what it would remove without removing it.
`auth.reset_backup` moves credential files aside as `<name>.<timestamp>.bak`
instead of deleting them; it is off by default, because a backup of a credential
is still a credential.

## Signing in is not project-neutral

Quota is charged to a project, APIs are enabled on a project, and Vertex serves
models from a project — so a gcloud pointed somewhere else signs in perfectly
and then does all of that in the wrong place. When a project is known,
**wherever it came from** — `--gcloud-project`, `--env-file`,
`GOOGLE_CLOUD_PROJECT`, `CLOUDSDK_CORE_PROJECT`, `ANTHROPIC_VERTEX_PROJECT_ID`
or `auth.google_cloud.project` in a config file — phase 2 does two things with
it:

- every gcloud command it runs carries `--project <id>`, appended from
  `auth.google_cloud.project_args`, which covers this run;
- `gcloud config set project` points gcloud itself at it, which covers
  everything afterwards — phase 3's own gcloud calls, and yours. This happens
  on a host that was already signed in too, so naming a project on a run that
  acquires nothing still moves it.

Four commands are left alone, listed in `auth.google_cloud.project_args_skip`:
the two that *set* the project take it as their argument, and the two that
*read* one would otherwise be handed the answer and echo it back — hiding the
very difference the phase is checking for.

Setting `project_args = []` hands project resolution back to gcloud entirely.

## One sign-in flow, chosen once for the whole run

The flow is answered once, by `auth.browser_mode`, and every backend reads that
same answer. `--headless` cannot mean one thing for Google Cloud and another for
claude.ai.

**Every sign-in ends with a code pasted back, display or not.** The alternative
is a loopback redirect, where the browser hands the result to a listener on
`localhost`. Its last step can't be checked beforehand: which machine the page
ends up on, an organisation's sign-in page or browser policy that stops it
returning to `http://localhost`, a port in use. A display proves only that a
browser can be drawn. When the redirect does not arrive, the sign-in waits out
`auth.interactive_timeout_seconds` with nothing for you to paste. The code,
shown by the page at the end, works wherever signing in works. This holds for
`--force-auth` and `--force-auth-with-reset` alike.

| `auth.browser_mode` | Flag | What every backend does |
| --- | --- | --- |
| `auto` *(default)* | — | print a URL and take a pasted code; where a browser can be shown, the URL is opened in it for you (`auth.signin.open_url`) |
| `headless` | `--headless` | print a URL and take a pasted code, and open nothing: you carry the URL to any browser, on any machine |
| `browser` | `--browser-loopback-redirect` | the loopback redirect, for a browser known to reach this machine: no code to paste, and nothing to fall back on if it does not come back |

The two flags are alternatives; giving both is refused rather than letting the
later one quietly win. An unrecognised `browser_mode` is read as `auto`, so a
typo never picks the redirect.

The Claude Code CLI's own `/login` opens the browser itself and takes a pasted
code when the page shows one, so without `--headless` it runs `/login`, and
`setup-token` is kept for `--headless`
(`auth.claude_oauth.interactive_command_opens_browser`).

Detection itself can be overridden for the whole run, not just the sign-in:
`INITIALIZE_CLAUDE_AI_FORCE_HEADLESS=1` makes the host count as having no
display, `INITIALIZE_CLAUDE_AI_FORCE_DISPLAY=1` as having one (the first wins if
both are set; `1`, `true`, `yes` and `on` are accepted).

What each backend *does* with the answer still differs, because the providers
differ — gcloud swaps `--no-launch-browser` onto its login commands, the Claude
Code CLI runs `setup-token` instead of `/login`, and the Anthropic API backend
has no interactive flow at all — but they are never in two different modes at
once. The phase banner states the chosen flow before any backend runs:

```
Sign-in       prints a URL; paste the code back here (--headless)
```

Under `--headless`, the terminal then says what it is waiting for, before it
goes quiet:

```
· [  INFO  ] sign-in  gcloud will print a URL instead of opening a browser.
      - The URL will wrap on screen. You do not have to select it: it is captured
        whole the moment it appears.
      - From another terminal:  cat /root/.local/state/initialize-claude-ai/sign-in-url.txt
      - It is also copied to the clipboard (xclip, display :1) -- just paste it.
      - Open it in any browser, on any machine, and sign in.
      - Copy the verification code the page then shows you and paste it back here.
      - This terminal is waiting for that code; nothing happens on its own.
      - If the page takes your email and then goes nowhere, the URL arrived cut short […]
```

That last line is the one failure worth knowing about in advance, and the three
above it are what removes it — see
[You should never have to select the sign-in URL](#you-should-never-have-to-select-the-sign-in-url).

**One sign-in, not two.** `gcloud auth login --update-adc` writes the
application-default credential itself, so the separate `gcloud auth
application-default login` is skipped when the login template carries that flag
— it would only overwrite the credential just obtained, after a second trip
through the same flow. It matters beyond the wasted minute: two near-identical
URLs on one screen is how the stale one gets opened, which produces exactly the
dead sign-in page described below. Drop `--update-adc` from
`auth.google_cloud.commands.login` to go back to two independent logins.

If stdin is not a tty — a pipe, a CI step — a pasted-code flow says so *before*
the wait, since there is nowhere to paste a code from.
`auth.interactive_timeout_seconds` (default 1800, `0` for no limit) bounds the
wait; it is generous because expiring mid-flow kills the provider's CLI while
the browser still says "success", which reads like a hang too.

## You should never have to select the sign-in URL

A pasted-code sign-in prints a URL of several hundred characters and asks you to
open it somewhere else. Both providers make getting it back out of the terminal
harder than it sounds, in two different ways — so neither has a fix that works
for the other:

- **gcloud** prints it as one unbroken line and lets the terminal wrap it for
  display. Most emulators give you the whole thing back on a copy; tmux, screen,
  and a window resized after the fact do not. What you get is the first row and
  nothing else — still a valid-*looking* authorization link, so Google renders a
  sign-in page that takes your email address and then goes nowhere, with nothing
  on it to say the link arrived truncated.
- **`claude setup-token`** draws its output through a layout engine that
  hard-wraps to the terminal width *before* printing, so those newlines are
  really in the stream. No terminal can give that URL back in one piece, because
  it was never sent as one piece. Selecting more carefully cannot help.

Neither can be fixed at the far end, so it is fixed at the near end: the sign-in
runs on a pseudo-terminal this program holds the other end of. Bytes are
forwarded both ways unaltered — the child sees a real terminal of the real size
and draws exactly what it would have drawn — while a copy of everything it
writes is watched for the URL. Where the text was hard-wrapped, the URL is
recovered from the **OSC 8 hyperlink escape** around it, which carries the whole
target however the visible text was broken up.

| Flag | Setting | |
| --- | --- | --- |
| `--sign-in-capture WHEN` | `auth.signin.pty` | `auto` *(default)* / `always` / `never`. `auto` captures whenever stdin and stdout are both terminals |

Once caught, the URL goes wherever `auth.signin.deliver` says — by default both:

- **a file**, `auth.signin.url_file` under the state directory, written `0600`
  and deleted when the sign-in ends. `cat` it from another terminal, or from the
  machine you are opening the browser on, over ssh.
- **the clipboard**, using the first of `auth.signin.clipboard` found on `PATH`
  (`wl-copy`, `xclip`, `xsel`, `pbcopy`, `clip.exe`) that this session can
  reach: `auth.signin.clipboard_needs` names the variable each one needs
  (`WAYLAND_DISPLAY` for `wl-copy`, `DISPLAY` for `xclip`/`xsel`), and a tool
  whose variable is unset is skipped, not tried. The announcement names the
  display, because over ssh that clipboard may not be the one you paste from.
  Install a tool with `initialize-claude-ai run --phases 1 --install-only clipboard`.

Both are announced *before* the sign-in starts, because from then on the screen
belongs to the child process and anything printed over it is either overwritten
or wrecks a half-drawn layout. The capture itself is silent.

Nothing typed *into* the sign-in is inspected or kept — the verification code you
paste back is the one genuinely sensitive thing in the exchange — and the
captured stream is not kept either; only the URL is, and only until the sign-in
ends. The URL is not a credential: it is an authorization request, and opening
it proves nothing without signing in.

Use `--sign-in-capture never` if the pseudo-terminal upsets your terminal
emulator; the sign-in then gets your terminal directly, exactly as before, and
you are back to selecting the URL by hand. The claude CLI's own shortcuts are
worth knowing for that case: press **`c`** at its prompt to copy, or Ctrl+click
the link if your emulator supports OSC 8 — both need a clipboard tool installed.

## The browser itself, whichever flow you chose

`auth.browser_mode` picks the *flow*. It says nothing about whether the browser
at the end of that flow works, and that is a separate question with its own
answers. Phase 2 settles it once, before any backend runs, and reports the
decision as its first step:

```
✔ [   OK   ] Browser                      browser: /usr/bin/firefox; own profile at
                                          /root/.local/state/initialize-claude-ai/browser-profile/firefox
                                          (no desktop session owns the default profile);
                                          input-method fix applied (:0 has no desktop session
                                          and no input method, where a browser can paint typed
                                          text without the page receiving it)
```

This program never opens a browser itself — gcloud and the Claude Code CLI do —
so all of this works by deciding what those two inherit. Nothing here depends on
the mode: the preparation runs under `--headless` too, because the URL that gets
printed is very often pasted into a browser on this same machine.

| Flag | Setting | Effect |
| --- | --- | --- |
| `--browser COMMAND` | `auth.browser.command` | the command that opens a URL, `%s` standing for it — `--browser 'firefox %s'`. Defaults to your `BROWSER` if you have one, else the first entry of `auth.browser.candidates` found on `PATH` |
| `--browser-input-fix WHEN` | `auth.browser.input_method_fix` | `auto` *(default)* / `always` / `never` — see below |
| `--browser-profile WHEN` | `auth.browser.clean_profile` | `auto` *(default)* / `always` / `never`. A profile of this program's own, with no extension filtering the sign-in form and no other account already signed in. `auto` means "where the default profile is unlikely to be usable": no desktop session, or running as root |

**If the sign-in form loses what you type, use `--browser-input-fix always`.**
The symptom is specific and, until you know the cause, inexplicable: you type
your address, the characters appear in the box, and the moment you click
anywhere else the "Enter your email or phone" placeholder floats back down over
what you typed. Nothing you enter is ever accepted.

The browser is painting those characters and the page is never hearing about
them. On a host with an X display but no desktop session behind it — an X server
started from a tty, an `ssh -X` forward, a VNC target, a container, anything
running as root — `GTK_IM_MODULE` and `XMODIFIERS` are unset, GTK falls back to
an XIM bridge, and there is no XIM server listening. The keystrokes reach the
widget and no `input` event reaches the page, so as far as Google's form is
concerned the field is still empty and the floating label belongs back where it
started. Forcing GTK's own built-in input context makes them ordinary key events
again:

```toml
[auth.browser.input_method_env]
GTK_IM_MODULE = "gtk-im-context-simple"
QT_IM_MODULE = "simple"
XMODIFIERS = "@im=none"
```

`auto` applies that only where all of those conditions hold at once *and* no
input method is configured, because overriding a working one would stop somebody
composing Chinese, Japanese or Korean text in the very form they are filling in.
`never` is the right answer on a desktop running ibus or fcitx. Because it is
environment rather than behaviour, it applies identically under `--headless`,
under `--browser-loopback-redirect`, and under neither.

`auth.browser.candidates` lists what counts as a browser, Firefox first: it is
packaged by every distribution in this program's reach, ships the same build to
all of them, and needs no sandbox concession to run as root. `xdg-open` is
deliberately *not* in that list, and is not in phase 1's browser check either —
it is a launcher, not a browser, and on a machine with nothing registered it
succeeds at opening nothing while reporting success. It is tried as a last
resort through `auth.browser.openers`, where `BROWSER` is then left alone,
since `xdg-open` reads `BROWSER` itself and naming it there makes it call itself
for ever.

On WSL, `wslview` (from the `wslu` package, which phase 1 installs only on WSL)
hands the URL to the browser on the Windows side, so a WSL host counts as having
a display: the sign-in URL is opened there for you, and the code comes back as
everywhere else.

Every backend ends with a live round-trip, so "authenticated" means a token was
actually accepted upstream — detected credentials included, since "it exists" is
not "it still works".

Four flags name *who* to authenticate as. An account is an identity rather than
a credential, so unlike the flags in [Credentials](credentials.md#credentials) they take the
value directly and `--secure` has nothing to say about them:

| Flag | Setting | Effect |
| --- | --- | --- |
| `--gcloud-user-account EMAIL` | `auth.google_cloud.account` | gcloud must be signed in as this account; a different active account is treated as unauthenticated, so phase 2 signs in rather than proceeding as the wrong user |
| `--gcloud-service-account EMAIL` | `auth.google_cloud.impersonate_service_account` | act as this service account by impersonation — no key file anywhere. Selects the `impersonate` method, and a host that is not already impersonating it is re-authenticated |
| `--gcloud-service-account-file PATH` | `auth.google_cloud.service_account_key_ref` | authenticate as the key's owner. Takes a bare path and compiles it to an absolute `file:` reference; gcloud opens the file, so the key never passes through this process. The two service-account flags are alternatives and giving both is refused |
| `--claude-user-account EMAIL` | `auth.claude_oauth.account` | the claude.ai account this machine should hold. Reported as the identity, named in the sign-in prompt, and checked against the CLI's status |

The two mismatch checks are not equally strict, deliberately. `gcloud auth list
--format=json` states the active account as data, so disagreeing with it is a
fact and phase 2 acts on it. Claude Code's status is prose, so a mismatch there
is only a suspicion — it warns and records `account_mismatch` in the report
rather than discarding a session that may well be fine.
