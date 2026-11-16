# Usage

```bash
# See exactly what would happen; probes real state, changes nothing.
initialize-claude-ai run --dry-run

# The whole sequence.
initialize-claude-ai run

# One phase at a time (numbers, names, ranges, or 'all').
initialize-claude-ai run --phases 1 --install-only claude-cli,gcloud
initialize-claude-ai run --phases 1 --force-install --break-dependencies
initialize-claude-ai run --phases 2 --auth-backend google-cloud --headless
initialize-claude-ai run --phases 2 --gcloud-user-account me@example.com --force-auth
initialize-claude-ai run --phases 3 --provision-backend vertex --gcloud-region us-east5
initialize-claude-ai run --phases 4 --mcp-server example.com,https://api.example.com
initialize-claude-ai run --phases 4 --mcp-server example.com --the-best-mcp

# Introspection.
initialize-claude-ai phases
initialize-claude-ai config provision.vertex.region
initialize-claude-ai config components          # what --install-only can name
initialize-claude-ai env
initialize-claude-ai run --json --report /tmp/initialize-claude-ai.json
```

`run` can be left out when the first argument is an option, so
`initialize-claude-ai --dry-run` and `initialize-claude-ai run --dry-run` are the
same command. Every flag is shorthand for a setting — see
[Configuration](configuration.md#configuration) — so anything on the command line can equally
live in a config file or an environment variable. `initialize-claude-ai --help`
lists every option of every command, one section per command. Each option
appears under the phase that reads it (`phase 4 (mcp) - MCP auto-discovery`,
for instance), or under `run control` or `any phase` when no single phase does.
The number and name in the heading are what `--phases` accepts. `--help` after a subcommand (`run --help`) lists
only that command's options.

`--dry-run` is honest: read-only probes still execute against the real machine
and the real network, so what it reports is what is actually there. Only
mutations are simulated.

## What a run looks like

### It opens by saying what it was told

Before the first phase changes anything, the run prints what it is running
*with*: the command line as given, which options were given out of how many
exist, every setting this host rather than the package decides — each one
alongside the file, variable or flag that decided it — and the variables it is
reading. A run that went somewhere unexpected nearly always went there because
of a layer somebody had forgotten was set, and this is the whole diagnosis in
one block, above the phase that acted on it.

```
╭─ HOW THIS RUN IS CONFIGURED ─────────────────────────────────────────────────╮
│ Command line                                                                 │
│   initialize-claude-ai run --dry-run --phases 4 --mcp-server https://ex.test │
│                                                                              │
│ Options given (3 of 62)                                                      │
│   --dry-run      yes                                                         │
│   --mcp-server   https://ex.test                                             │
│   --phases       4                                                           │
│                                                                              │
│ Settings this host decides (3)                                               │
│   auth.google_cloud.project  my-project   from GOOGLE_CLOUD_PROJECT          │
│   provision.vertex.region    global       from GOOGLE_CLOUD_LOCATION         │
│   paths.log_file             (empty)      from the command line              │
│                                                                              │
│ Environment this run reads (11)                                              │
│   ANTHROPIC_API_KEY      ***REDACTED***                                      │
│   GOOGLE_CLOUD_PROJECT   my-project                                          │
│   ... and 9 more                                                             │
│   6 ignored as an echo of the settings file this run rewrites:               │
│   ANTHROPIC_DEFAULT_HAIKU_MODEL, CLOUD_ML_REGION, ...                        │
├──────────────────────────────────────────────────────────────────────────────┤
│ The merged configuration in full:  initialize-claude-ai config               │
│ Every option and every variable:   initialize-claude-ai run --help           │
╰──────────────────────────────────────────────────────────────────────────────╯
```

A setting is listed when a later layer overwrote it *and* when its packaged
default is a reference the environment answered — `region = "${CLOUD_ML_REGION:-global}"`
with that variable set is not a default, however packaged the line is, and the
commonest surprise of all would otherwise go unexplained. Settings still at
their packaged default are left out; `initialize-claude-ai config` prints the
merged document in full. Variables that describe the machine rather than this
program (`HOME`, `PATH`, `XDG_*` — `run.invocation.ignore_env`) are never named
as the reason for a value, since `HOME` feeds a dozen path defaults and saying
so a dozen times buries the one line that matters. Credentials are masked here
exactly as they are everywhere else this program prints.

| Setting | Environment variable | Effect |
| --- | --- | --- |
| `run.invocation.show` | `INITIALIZE_CLAUDE_AI_SHOW_INVOCATION` | `false` drops the block from the screen *and* the log. (`--quiet` only keeps it off the terminal; the log still has it, like everything else the run emits.) Either way the same facts stay under `invocation` in the `--json` report, so a report read months later still says what the run was told. |
| `run.invocation.max_rows` | `INITIALIZE_CLAUDE_AI_SHOW_INVOCATION_ROWS` | Longest list per section before it is elided with a count (default 24). |
| `run.invocation.env_patterns` | `INITIALIZE_CLAUDE_AI_RUN__INVOCATION__ENV_PATTERNS` | Shell-style patterns for the variables reported. Anything else in the environment is none of this program's business and is neither printed nor logged. |
| `run.invocation.ignore_env` | `INITIALIZE_CLAUDE_AI_RUN__INVOCATION__IGNORE_ENV` | Patterns never named as the reason a setting has its value. |

### Every phase reports the same way

Every phase opens with a banner — which phase, of how many, what it is about to
do, and the inputs it resolved before starting — and closes with a summary that
*names* what passed, what was skipped and what failed, with the reason for each
failure and the remediation for it. A count alone ("1 failed") would send you
scrolling back through a seventeen-component install; the name and the reason
are in the box. Under the divider is the phase's overall result, in words, and
then the assertion — whether it is safe to carry on from here, and why.

```
╭─ Phase 2 summary: Authenticate ──────────────────────────────────────────────╮
│ ✘ failed    1  required backends                                             │
│ • skipped   1  Anthropic API key                                             │
│ ✔ passed    1  detect                                                        │
│                                                                              │
│ Why these did not pass:                                                      │
│   ✘ required backends                                                        │
│       not authenticated: anthropic-api                                       │
│   • Anthropic API key                                                        │
│       no key or bearer-token reference resolved                              │
├──────────────────────────────────────────────────────────────────────────────┤
│ PHASE 2 RESULT: PARTIALLY PASSED                                             │
│ 1 of 3 step(s) passed  ·  1 skipped  ·  1 failed  ·  412ms                   │
│                                                                              │
│ ▲ SAFE TO CONTINUE, WITH CAVEATS                                             │
│   At least one credential works (verified: google-cloud; that is enough for  │
│   vertex, bedrock), so phase 3 can provision. The backends listed above did  │
│   not sign in and will be unavailable -- harmless unless one of them is the  │
│   backend you meant to use.                                                  │
│                                                                              │
│ Next steps:                                                                  │
│   · Export a key: export ANTHROPIC_API_KEY=…                                 │
╰──────────────────────────────────────────────────────────────────────────────╯
```

Every step that did not pass — failed *or* skipped — gets its reason spelled out
under **Why these did not pass**, because "skipped" covers both "already done,
nothing to do" and "could not be attempted", and only the reason tells you
which.

### A phase that works through a list is counted on the list

Some phases ask the same question of several subjects — one server per
`--mcp-server`, and the answer to each is "is there a working MCP server here".
Getting there costs dozens of steps: a stage per strategy, a probe per path, a
check per endpoint. Counting *those* turns a good run into a bad-looking one — a
server found by the third stage reads as two failures and a pass, and an address
with a hundred probed paths drowns out one with three.

So the scoreboard counts the subjects, each gets a section of its own with its
own arithmetic, and every step it took is listed underneath the verdict it did
not decide:

```
╭─ Phase 4 summary: MCP auto-discovery ────────────────────────────────────────╮
│ ✘ failed    1  https://jira.example.com                                      │
│ • skipped   0  -                                                             │
│ ✔ passed    2  https://github.com, https://openqa.example.com                │
│                                                                              │
│ Every server, and what happened to it:                                       │
│                                                                              │
│ ✔ https://openqa.example.com  ·  PASSED                                      │
│     working MCP server: openqa-example-com -- generated bridge over stdio    │
│     3 of 6 step(s) passed  ·  1 skipped  ·  2 failed                         │
│       ✔ address openqa.example.com  https://openqa.example.com               │
│       ✘ endpoint                    no MCP endpoint answered on 166 probed   │
│                                     path(s)                                  │
│       • package                     no published server targets this product │
│       ✔ bridge                      generated from /api_help; 12 verified    │
│       ✘ duplicate configuration     1 existing entr(y/ies) in ./.mcp.json    │
│                                     already point at this service            │
│       ↻ register openqa-example-com recorded in ./.mcp.json                  │
│                                                                              │
│ ✘ https://jira.example.com  ·  FAILED                                        │
│     no working MCP server was found                                          │
│     1 of 5 step(s) passed  ·  2 skipped  ·  2 failed                         │
│       ...                                                                    │
├──────────────────────────────────────────────────────────────────────────────┤
│ PHASE 4 RESULT: PARTIALLY PASSED                                             │
│ 2 of 3 server(s) passed · 0 skipped · 1 failed · 22 step(s) in total         │
```

The verdict on a subject is the phase's, not a roll-up of its steps: only the
phase knows that a server found by the third stage passed even though the first
two came back empty. A step that belongs to no subject — `discovery mode`, the
file that was written, the closing verdict — can only make the phase's own
result *worse*, never better, so a run-level failure still shows and a run where
no subject yielded anything reads `FAILED` rather than half-passing on the
bookkeeping around the search. An address that could not even be parsed still
counts as a subject that produced nothing, so the denominator matches the number
you typed. The run table at the end counts the same way (`2 server(s) passed`),
so it cannot disagree with the box above it.

Phases that work through no list — most of them — are summarised exactly as
before, step by step.

The overall result is one of five words, derived from the steps (or, above, from
the subjects) by the rules in [Exit status](exit-status.md#exit-status):

| Verdict | Means |
| --- | --- |
| `PASSED` | Every step worked; nothing on the machine needed changing. |
| `PASSED WITH CHANGES` | Every step worked, and something was installed, written or signed in. |
| `PARTIALLY PASSED` | Some steps worked and at least one did not — the run can continue, but read the box. |
| `SKIPPED` | The phase did not run: disabled in configuration, or nothing was selected for it. |
| `FAILED` | Nothing usable came out of the phase — or it stopped for want of a value it could not do without, whatever else passed. |

### Is it safe to go ahead?

The verdict says what *happened*. The assertion under it says what that *means
for the rest of the run* — and the two do not always agree. A phase can pass and
still leave the next one unable to proceed; a phase can be skipped without
endangering anything. Each phase decides for itself, from what it alone knows:

| Assertion | Means |
| --- | --- |
| `✔ SAFE TO CONTINUE` | Everything this phase is responsible for is in place and verified. |
| `▲ SAFE TO CONTINUE, WITH CAVEATS` | The essentials hold; something secondary did not, and the reason names it. Nothing downstream is blocked. |
| `✘ NOT SAFE TO CONTINUE` | A later phase depends on something that is not there. Fix the causes listed above and re-run. |

What each phase weighs:

- **Phase 1** blocks only on a *required* component — an optional one that fails
  is demoted to a skip, so a `failed` here is a binary phases 2–4 shell out to.
- **Phase 2** blocks only when *no* backend produced a credential that survived a
  live round trip; otherwise it names which provisioning backends the verified
  credentials unlock.
- **Phase 3** blocks when no backend was selected or `settings.json` could not be
  written. An unlisted model or a silent catalogue is a caveat, not a stop.
- **Phase 4** never blocks. Nothing is registered without a real MCP handshake, so
  the worst case is fewer servers than you hoped for.

The run is only as safe as its least safe phase, and a dry run says so in the
assertion rather than claiming a file was written.

The run ends with the same treatment across all phases — a row per phase, an
`OVERALL RESULT`, the run-level assertion with every caveat repeated (the
per-phase boxes have scrolled away by then), and the exact command to re-run only
what did not work:

```
╭─ Run summary ────────────────────────────────────────────────────────────────╮
│ ↻ 1  install    PASSED WITH CHANGES  17 passed, 1 skipped, 0 failed          │
│ ✘ 3  provision  FAILED               0 passed, 0 skipped, 1 failed           │
├──────────────────────────────────────────────────────────────────────────────┤
│ OVERALL RESULT: PARTIALLY PASSED                                             │
│ 2 phase(s) run  ·  32.9s                                                     │
│                                                                              │
│ ✘ NOT SAFE TO CONTINUE                                                       │
│   1 of 2 phase(s) needs reading before you rely on them:                     │
│   ✘ phase 3 provision                                                        │
│       No backend was selected, so nothing was configured. Claude Code is     │
│       left on whatever ~/.claude/settings.json already said. Authenticate a  │
│       backend (phase 2) or force one with --provision-backend, then re-run   │
│       phase 3.                                                               │
│                                                                              │
│ Re-run just what did not work:                                               │
│   · initialize-claude-ai run --phases 3                                      │
╰──────────────────────────────────────────────────────────────────────────────╯
```

The knobs are in `[console]`:

| Setting | Environment variable | Effect |
| --- | --- | --- |
| `console.palette` | `INITIALIZE_CLAUDE_AI_CONSOLE_PALETTE` | `auto` \| `vibrant` \| `daylight` \| `contrast` \| `soft` \| `basic`. `auto` is the default, and picks `vibrant` on most terminals; see [the palette](#the-palette) below. `daylight` is the counterpart of `vibrant` for a light background, `contrast` the calmer set with an even brightness, `soft` the quiet one, `basic` the plain eight ANSI colours for terminals that would print a 256-colour escape as literal text — also spelled `classic`, `standard` or `ansi`. `auto` picks `vibrant` — or `daylight` when `COLORFGBG` says the background is light — wherever `COLORTERM` or a 256-colour `TERM` says the terminal can render it, and falls back to `basic` where it cannot. In every palette the hue means the same thing, so switching changes only how it looks. |
| `console.glyphs` | `INITIALIZE_CLAUDE_AI_CONSOLE_GLYPHS` | `auto` \| `unicode` \| `ascii`. `auto` asks the output stream whether it can encode a box-drawing character, so a C-locale host gets `+--+` rather than mojibake. |
| `console.width` | `INITIALIZE_CLAUDE_AI_CONSOLE_WIDTH` | Total width in columns (default 80). Fixed rather than read from the terminal, so piped output matches what was on screen. |
| `console.decorate` | `INITIALIZE_CLAUDE_AI_CONSOLE_DECORATE` | `false` drops every frame and prints the same content as plain lines. |
| `console.progress` | `INITIALIZE_CLAUDE_AI_CONSOLE_PROGRESS` | `false` suppresses the `[RUNNING]` lines and leaves only the verdicts. See [badges](#the-badges) below. |
| `logging.badges` | `INITIALIZE_CLAUDE_AI_LOG_BADGES` | `false` prints log levels on screen as plain words rather than as badges. The log *file* always uses the plain format. |

Colour is dropped entirely — not softened — by `--no-color`, `NO_COLOR`, `TERM=dumb`
or a non-tty. Nothing is ever said in colour alone: see below.

### The badges

Every line that reports a state — a step's verdict, a log record, a progress
line — is drawn from one table, as a **sign**, a **word** and a **colour**:

```
✔ [   OK   ]  ↻ [CHANGED ]  • [  SKIP  ]  ▲ [PARTIAL ]  ✘ [  FAIL  ]
» [RUNNING ]  ▲ [  WARN  ]  ✘ [ ERROR  ]  · [  INFO  ]  · [ DEBUG  ]
```

Three carriers for one meaning, deliberately. Colour is the fastest to read and
the first to disappear — into a pipe, a greyscale screenshot, a reader who does
not separate red from green — so the word and the shape are what the report
actually rests on, and the colour is the accelerator. The word is centred in a
fixed eight columns so that the messages after the badges line up in one column,
and the signs come from `console.glyphs`, falling back to `+ x - ! > . *` where
the terminal cannot encode the shapes.

The colours are the ones nearly every terminal colour scheme publishes: green
pass, red fail, amber caveat, cyan in flight, bold red error, bold blue heading,
dim for detail, and the terminal's own colour for information, since information
is the default case and does not need announcing. Two deliberate departures:

- **Skipped is grey, not amber.** The published schemes have four test states
  where this program has five outcomes, and the extra one is `PARTIAL` — "it
  worked, but read this", the line the operator must not miss. A skip here is
  routinely the *good* answer (`node 3.22 already present, nothing to do`), so
  painting skips amber would put the commonest line in the run in the colour
  reserved for the rarest.
- **A failed step is bold.** In a test runner a fail is one of hundreds; here a
  failed step *is* the thing to act on, and there are rarely more than a few.

The signs are shapes rather than coloured-circle emoji. 🟢🔴🟡 would say the same
thing more loudly, but each is two cells wide with a width the terminal and the
font have to agree on, and this program pads box rows by counting characters —
one wide glyph moves the right-hand frame of every line it appears in. A report
that is legible in a C locale and inside a box is worth more than one that is
prettier where it happens to line up.

`[RUNNING]` appears only before the few steps that can go quiet for a minute
with nothing to show — a package install, a phase-4 stage, a subagent turn. One
per step would double the length of the report to say what the verdict line says
a moment later. `initialize-claude-ai palette` prints the badges in every
palette, next to the hues.

The summary rows and the box frames are not badges — they are whole lines taking
their colour from a verdict — but they take the sign and the colour from this
same table, so a phase that came out `PARTIAL` is the same amber triangle in the
run summary as it was on the step that made it partial.

### The palette

Two numbers describe the whole design, and they are not the same number.
*Contrast* — the WCAG ratio against the background — is how **readable** a colour
is; 4.5:1 is the line at which text counts as readable at all. *Saturation* is
how **colourful** it is: how far the colour sits from the grey of the same
brightness.

The default, `vibrant`, takes saturation first. Four of its six hues are at the
full chroma the 256-colour cube can give:

| | colour | saturation | on `#000000` | on `#1e1e1e` |
| --- | --- | --- | --- | --- |
| failed | coral red `#ff5f5f` | 0.63 | 7.1:1 | 5.6:1 |
| passed | emerald `#00d75f` | **1.00** | 10.9:1 | 8.6:1 |
| needs attention | amber `#ffaf00` | **1.00** | 11.4:1 | 9.0:1 |
| information | azure `#5fafff` | 0.63 | 9.1:1 | 7.2:1 |
| dry run | orchid `#ff5fff` | 0.63 | 8.3:1 | 6.6:1 |
| changed | cyan `#00d7d7` | **1.00** | 11.7:1 | 9.3:1 |
| skipped | grey `#808080` | 0.00 | 5.3:1 | 4.2:1 |

Readability is then a floor to clear rather than a thing to maximise: every hue
is far past 4.5:1 and well short of the neon corners of the cube — `#00ff00` is
15.3:1 on black, `#ffff00` is 19.6:1 — which are the colours people turn colour
off to escape. Grey is deliberately the quietest thing on screen, because
skipped is not a warning and should not read like one.

The two numbers pull against each other, which is why there is a second palette.
The most saturated colours are the corners of the cube, and the corners are not
equally bright — the eye weights green at 0.72 and blue at 0.07 — so chroma
necessarily widens the spread between the quietest and loudest hue. `vibrant`
runs 1.7×. **`contrast`** is the same six meanings with that chroma traded back
for an even weight: a 7:1–10:1 band, 1.4× spread, about two thirds the
saturation. It is the calmer thing to read for an hour; `vibrant` is the easier
thing to scan in ten seconds.

```bash
initialize-claude-ai run --set console.palette=contrast
```

Red is at the bottom of the band in both, and that is a fact about eyes rather
than a choice: with red weighted 0.21, any red bright enough to reach 11:1 on
black has stopped being red and become pink. Failures get their weight from
**bold**, the word `FAIL` and the glyph instead — which is the right place for
it, since those three survive a pipe.

`daylight` is the whole design inverted for a light terminal, where being too
*pale* is what makes a colour unreadable: every hue at full chroma, taken down to
4.4:1–6.1:1 against white. There are two palettes rather than one compromise
because a single set readable on both black and white cannot get past roughly
4:1 on either — legible, but flat. `auto` reads `COLORFGBG`, which most terminals
export as `fg;bg`, and uses `daylight` only when that plainly says the background
is light; anything absent, partial or unparseable means dark, since light-on-light
is the worse way to be wrong.

The frames carry the verdict too. A summary box is drawn in the colour of the
result inside it — green for passed, cyan for passed-with-changes, amber for
partial, red for failed — so on a four-phase run the phase that went wrong is a
red frame among green ones, found by scrolling rather than by reading every box.
Banners frame what is about to happen and have no verdict yet, so they take the
information blue. It is one column of characters, an accent rather than a wash.

Colour never carries the meaning alone. Every line also states its status in
words and carries a glyph, and the frame repeats a verdict already written
inside it, so the report survives being piped, logged, screenshotted in
greyscale, or read by anyone who does not distinguish red from green.

If your terminal theme or your eyes disagree with any of this, the program
prints all five side by side rather than arguing:

```bash
initialize-claude-ai palette                       # every scheme, hues and badges
initialize-claude-ai run --set console.palette=soft
export INITIALIZE_CLAUDE_AI_CONSOLE_PALETTE=soft   # or console.palette in a config file
```

Each scheme is shown twice over: once as the hues with their meanings, and once
as the badges themselves, because no line in a report is ever a bare colour and
the three carriers have to be judged together.

Both numbers are asserted in the test suite (`tests/test_phase_report.py`, with
the badges in `tests/test_badges.py`) —
contrast computed from the WCAG relative-luminance formula, saturation from the
sRGB values — rather than written down and hoped for. A later "just soften that
green a little" cannot quietly drain the palette one colour at a time.

`--quiet` suppresses the human output entirely and `--json` reserves stdout for
the machine-readable report; the report carries the same `verdict`, the same
pass/skip/fail `tally` and the same `safety` / `safety_label` / `safety_reason`
the boxes show, per phase and for the run, because both are rendered from one
object. A phase that
[works through a list](#a-phase-that-works-through-a-list-is-counted-on-the-list)
adds `group_noun` (`"server"`), `group_tally` (the same three counts, over
subjects) and `groups` — one entry per subject with its own `status`, `verdict`,
`detail`, `tally` and `steps` — and every step carries the `group` it is filed
under, empty for the ones about the run itself.

## The log file

Every run records itself, by default in the directory you ran it from:

```
./claude_ai_initialization.log
```

### Every line says when it happened

Both halves of the output — the narration you watch and the log records
underneath it — carry the same stamp, from the same clock, in
`logging.date_format`:

```
2026-09-16 18:30:43   Components                   18 selected
2026-09-16 18:30:44 ✔ [   OK   ] curl                /usr/bin/curl (8.14.1)
2026-09-16 18:30:46 ✔ [   OK   ] gcloud              /usr/bin/gcloud (579.0.0)
```

That is what lets the file answer questions about itself — *how long did that
stage take?*, *what was running during those eleven quiet minutes?* — which it
could not do while the narration was unstamped and only the log records were.

**Some lines are deliberately not stamped**, because their shape carries meaning
that a prefix would destroy:

| Not stamped | Why |
| --- | --- |
| box frames and their contents | a box is one event, not a line each; its alignment depends on its width, and the same second repeated twenty times down the side of a summary is noise. The heading above it carries the time |
| table rows | a stamp in front of each row stops the columns lining up, which is the only reason a table is a table |
| blank lines | a timestamp alone records nothing, and the vertical space is doing a job |
| the `--json` report, and `config KEY` output | machine-readable: a prefix would break the parse |
| the final error or interrupt message on stderr | that is a message to the person in front of you, and it sits next to the file's own `# finished <time>` line |

A rule or heading is *shortened* by the stamp rather than pushed past the
terminal width, so everything still lines up at 80 columns.

Log records printed to the screen carry the same stamp, from the same clock, so
a warning and the step it interrupted can be placed against each other. In the
file they are stamped by the log formatter instead, so nothing is stamped twice.

Turn it off with `console.timestamps = false`
(`INITIALIZE_CLAUDE_AI_CONSOLE_TIMESTAMPS=false`).

> The sample output elsewhere in this README omits the stamp, to keep the
> examples narrow enough to read. A real run has one in front of every line
> shown outside a box.

The path is resolved once, at startup, against that directory — so a run is
recorded where you are watching it, and nothing that changes directory later
moves the file mid-run. Point `paths.log_file` (or
`INITIALIZE_CLAUDE_AI_LOG_FILE`) at an absolute path to collect every run in one
place instead. The banner at the top of a run and the last line of the run
summary both name the resolved, absolute path, so it never has to be guessed at:

```
│ Log           /srv/deploy/claude_ai_initialization.log                       │
...
│ Full output recorded in /srv/deploy/claude_ai_initialization.log             │
```

(`initialize-claude-ai config paths.log_file` prints the *setting*, which is the
relative default unless you have changed it.)

It holds **everything the run emitted**: the banners, status lines and summary
boxes exactly as they appeared on screen, the JSON report when `--json` was
used, the reason the run stopped when it stopped early, and the `debug`-level
log records — subprocess invocations, probe results, decisions — interleaved
between them. Console output and log records share one open file handle rather
than being written to two files, so the order on disk is the order things
happened in.

Some consequences worth knowing:

- **`--quiet` and `--json` do not empty it.** They decide what the terminal
  shows, not what the run did; a silent run is exactly the one whose record you
  will want later.
- **Colour never reaches it.** ANSI escapes are stripped on the way in, so the
  file is readable in a pager and greppable.
- **No credential reaches it either.** It is written through the same redaction
  sink as the console (see [Credentials](credentials.md#credentials)), and the command line in
  the per-run header is scrubbed before it is recorded.
- **Runs accumulate.** The file is appended to, and each run opens with a header
  naming the version, the time, the pid and the (redacted) command line, so an
  old run can still be read on its own. It rotates to
  `claude_ai_initialization.log.1` … `.3` once it passes 5 MB.

| Setting | Environment variable | Effect |
| --- | --- | --- |
| `paths.log_file` | `INITIALIZE_CLAUDE_AI_LOG_FILE` | Where the record goes; a relative path is anchored to the working directory at startup. Empty means "record nothing", which is what `--no-log-file` sets. |
| `logging.file_level` | `INITIALIZE_CLAUDE_AI_LOG_FILE_LEVEL` | Level kept in the file, independent of what `--log-level` shows on screen. Default `debug`; `trace` adds the noisiest detail. |
| `logging.record_output` | `INITIALIZE_CLAUDE_AI_LOG_RECORD_OUTPUT` | `false` records only the log records, leaving an ordinary log file rather than a transcript. |
| `logging.max_bytes` / `logging.backups` | `INITIALIZE_CLAUDE_AI_LOG_MAX_BYTES` / `…_LOG_BACKUPS` | Rotation, checked when the file is opened so no run rotates out from under itself. `0` for either disables it. |
| `logging.format` / `logging.date_format` | — | How a log record and the per-run header are stamped. |

```bash
initialize-claude-ai run --quiet && tail -40 claude_ai_initialization.log
initialize-claude-ai run --log-file /var/log/claude-init.log   # elsewhere, once
initialize-claude-ai run --no-log-file                         # nowhere

# Every run in one place, wherever it was started from:
export INITIALIZE_CLAUDE_AI_LOG_FILE=$HOME/claude_ai_initialization.log
```

## Command-line reference

Every flag is shorthand for a setting, so the right-hand column is also what to
write in a config file or an `INITIALIZE_CLAUDE_AI_…` variable. Credential flags
have their own table under [Credentials](credentials.md#credentials).

**Subcommands** — `run` (the default), `phases`, `config [KEY]`, `palette`, `env`.

| Flag | Setting | Effect |
| --- | --- | --- |
| `--config FILE` | — | extra TOML file; repeatable, later files win |
| `--env-file FILE` | — | dotenv file loaded before interpolation; repeatable |
| `--set KEY=VALUE` | *any* | override one dotted key; repeatable, highest precedence |
| `--secure` | `secrets.allow_literal` | refuse every credential given as a value, from any layer |
| `--do-cleanup` | `run.cleanup.enabled` | at the end of the run, remove this program's own leftovers nothing uses -- old temporary directories it marked as its own (`run.cleanup.temp_prefixes` says where to look, the `.initialize-claude-ai-owned` marker says whose; `run.cleanup.require_owner_marker`) and unused work files -- never a shared cache, never a path an MCP entry names; **on by default** |
| `--no-cleanup` | `run.cleanup.enabled` | keep every leftover this run and earlier runs made |
| `--json` | — | machine-readable report on stdout, nothing else |
| `--report FILE` | — | also write that report to `FILE` |
| `--log-level LEVEL` | `logging.level` | `trace`, `debug`, `info`, `warning`, `error` — on screen only; the file keeps `logging.file_level` |
| `--log-file FILE` | `paths.log_file` | record this run in `FILE` instead of the default below |
| `--no-log-file` | `paths.log_file` | record nothing to disk |
| `--quiet` / `--no-color` | — | drop the human output / drop ANSI colour |
| `--version` | — | print the version and exit |

**`run`**

| Flag | Setting | Effect |
| --- | --- | --- |
| `--phases LIST` | `run.default_phases` | numbers, names, ranges or `all` |
| `-n`, `--dry-run` | — | simulate mutations; read-only probes still run |
| `-a`, `--auto-agree` | — | answer yes to every confirmation |
| `--ignore-failure` | `run.continue_on_failure` | carry on after a phase fails |
| `--no-partial-success` | `run.continue_on_partial` | treat a partly successful phase as a stop |
| `--timeout DURATION` | `run.deadline_seconds` | wall-clock ceiling for the whole run — `45s`, `90m`, `2h`, `1d` or plain seconds. No ceiling by default ([details](phase-4-mcp-discovery.md#--timeout-and---timeout-scale-are-two-different-questions)) |
| `--timeout-scale FACTOR` | `run.timeout_scale` | multiply every per-operation timeout by FACTOR (default `1.0`, unchanged) |

**Any phase**

A flag whose value more than one phase reads is not filed under either of them,
because a flag in the phase 3 table reads as one phase 3 acts on — and somebody
running `--phases 2` would leave it off and get a different sign-in. Give these
with whichever phases you run.

| Flag | Setting | Effect |
| --- | --- | --- |
| `--gcloud-project ID` | `auth.google_cloud.project`, `provision.vertex.project` | Google Cloud project for the whole run: phase 2 signs in against it, phase 3 bills Vertex to it. No default is invented. Alias: `--vertex-project` |
| `--auth-anthropic-key-ref REF` | `auth.anthropic_api.api_key_refs` | Anthropic API key: phase 2 verifies it, phase 3 asks it for the model catalogue. See [Credentials](credentials.md#credentials) |

**Phase 1 — install**

| Flag | Setting | Effect |
| --- | --- | --- |
| `--install-only LIST` | `install.only` | component keys or groups (`base`, `runtime`, `claude`, `cloud`, `browser`, `desktop`, `mcp-tooling`, `mcp-runtimes`); see `config components` |
| `--install-skip LIST` | `install.skip` | same syntax, and it wins over `--install-only` |
| `--install-prefetch {none,launchers,all}` | `install.prefetch` | also install what later phases launch with: `launchers` (default) the programs phase 4's command templates start with, `all` every `mcp-runtimes` toolchain as well, `none` nothing extra. Always optional; ignored under `--install-only` ([details](phase-1-install.md#prefetching-what-later-phases-launch-with)) |
| — | `install.demand_from_entries` | also install what the registered MCP entries start or call and this machine lacks; name what nothing here installs ([details](phase-1-install.md#installing-for-what-registered-servers-run)) |
| — | `install.machine_paths.enabled`, `…persist_to`, `…check_with_claude` | give each `ICA_MCP_*` variable an entry reads this machine's path when the written default does not exist here; `settings` (Claude Code's settings env), `true` |
| `--force-install` | `install.force` | reinstall even where verification already passes |
| `--break-dependencies` | `install.allow_break_system_packages` | install into a PEP 668 interpreter anyway |
| `--downgrade` | `install.downgrade` | resolve through `[install.pins]` instead of latest |

**Phase 2 — authenticate**

| Flag | Setting | Effect |
| --- | --- | --- |
| `--auth-backend LIST` | `auth.backends` | `google-cloud`, `claude-oauth`, `anthropic-api` or `all` |
| `--force-auth` | `auth.force` | sign in again even where a credential was detected |
| `--force-auth-with-reset` | `auth.force` + `auth.reset` | clear the existing credentials first, so the sign-in really asks for an account. Destructive and host-wide |
| `--headless` | `auth.browser_mode=headless` | every backend prints a URL for you to open and waits for a pasted verification code; nothing is opened. Without it the code is still pasted back, and the URL is opened for you |
| `--browser-loopback-redirect` | `auth.browser_mode=browser` | opt-in: every backend lets the browser hand the result back over the loopback redirect, with no code to fall back on. Alternatives; giving both is refused |
| `--browser COMMAND` | `auth.browser.command` | what opens a URL, `%s` standing for it: `--browser 'firefox %s'` |
| `--browser-input-fix WHEN` | `auth.browser.input_method_fix` | `auto`/`always`/`never`. Use `always` if the sign-in form loses what you type |
| `--browser-profile WHEN` | `auth.browser.clean_profile` | `auto`/`always`/`never`. Sign in in a browser profile of this program's own |
| `--sign-in-capture WHEN` | `auth.signin.pty` | `auto`/`always`/`never`. Catch the printed sign-in URL whole, so a wrapped one never has to be selected |
| `--gcloud-user-account EMAIL` | `auth.google_cloud.account` | the Google account gcloud must be signed in as |
| `--gcloud-service-account EMAIL` | `auth.google_cloud.impersonate_service_account` | act as this service account, by impersonation |
| `--gcloud-service-account-file PATH` | `auth.google_cloud.service_account_key_ref` | act as the owner of this key file; not with the flag above |
| `--claude-user-account EMAIL` | `auth.claude_oauth.account` | the claude.ai account this machine should hold |

The four account flags, and the three `--browser*` flags that decide whether the
browser a sign-in opens is usable at all, are described in full under
[Phase 2](phase-2-authenticate.md#phase-2--authenticate).

**Phase 3 — provision**

| Flag | Setting | Effect |
| --- | --- | --- |
| `--provision-backend NAME` | `provision.backend` | `vertex`, `anthropic-api`, `bedrock` or `auto` |
| `--use-vertex 0\|1` | `provision.vertex.enabled` | whether Vertex may be provisioned against at all; `1` by default |
| `--gcloud-region NAME` | `provision.vertex.region` | Vertex location; `global` by default |
| `--default-model TIER` | `provision.settings.model` | tier Claude Code opens on — `opus`, `sonnet`, `haiku` — which is what `settings.json` `model` is written as; a model id is written as the tier it belongs to. Otherwise `provision.default_tier` (`opus`) |
| `--default-opus-model ID` | `provision.<backend>.model_aliases.opus` | pin the `opus` tier instead of discovering it |
| `--default-sonnet-model ID` | `provision.<backend>.model_aliases.sonnet` | pin the `sonnet` tier |
| `--default-haiku-model ID` | `provision.<backend>.model_aliases.haiku` | pin the `haiku` tier |

`--use-vertex 0` takes Vertex out of the automatic walk down
`provision.backend_priority`, so `auto` falls through to the next enabled
backend, and turns `--provision-backend vertex` into a refusal rather than a
silent fall-through. `--use-vertex 1` restores it; it does **not** force Vertex
to be the choice — `--provision-backend vertex` is what does that.

Bedrock's region is a coordinate in another cloud and has no flag of its own:
set `provision.bedrock.region`, or `AWS_REGION`, or
`--set provision.bedrock.region=us-east-1`. Provisioning Bedrock without one
stops the phase — every call is addressed to a region and there is no default
worth guessing.

Every one of these flags can be left out and supplied by a plain `UPPER_CASE`
variable in the file given to `--env-file` instead; see
[One env file can drive the whole run](configuration.md#one-env-file-can-drive-the-whole-run).

**Where the region comes from.** `--gcloud-region`, else `CLOUD_ML_REGION`,
else `GOOGLE_CLOUD_LOCATION`, else `global` — and the report names which, the
same way it does for the project:

```
✔ [   OK   ] region  global (GOOGLE_CLOUD_LOCATION)
✔ [   OK   ] region  us-east5 (--gcloud-region)
✔ [   OK   ] region  global (default)
```

That line is derived from the `[sources]` table, so it cannot drift from the
`${VAR}` chain that actually did the reading.

**Where the Vertex project comes from.** There is no default project id
anywhere in the configuration — a wrong one bills somebody else's account and
fails with a permission error naming a project nobody chose. The project is
named once for the whole run: `--gcloud-project` sets
`auth.google_cloud.project` and `provision.vertex.project` together, so phase 2
signs in against the same project phase 3 provisions. Phase 3 takes the first
of:

1. `--gcloud-project`, `ANTHROPIC_VERTEX_PROJECT_ID`, `GOOGLE_CLOUD_PROJECT`,
   `CLOUDSDK_CORE_PROJECT`, or `provision.vertex.project` from a config or
   `--env-file`;
2. the project phase 2 actually signed in against, when phase 2 ran in the same
   invocation — preferred over asking gcloud again, because it cannot disagree
   with the credential that was verified;
3. gcloud's active project;
4. the id already in `settings.json` — this program's own previous answer, and
   the last thing tried rather than the first, because
   [that file is the weakest source](configuration.md#our-own-last-answer-is-the-weakest-source).
   It is consulted at all because, unlike the region, a project id has no
   default worth inventing: keeping the one already there beats failing a host
   with nothing else to say.

The report names which one supplied it, so it is never a guess:
`✔ [   OK   ] project  example-gemini-code-assist (from the phase 2 sign-in)`.

**Phase 4 — MCP discovery**

| Flag | Setting | Effect |
| --- | --- | --- |
| `--mcp-server LIST` | `mcp.servers` | server(s) to find an MCP endpoint for — comma separated, repeatable, and written however you have them ([details](phase-4-mcp-discovery.md#addresses-are-canonicalised-before-anything-is-probed)) |
| `--stages LIST` | `mcp.stages` | `endpoint`, `package`, `bridge`, `strategy` |
| `--concurrency [N]` | `mcp.discovery.target_concurrency` | work N addresses at once; bare means `mcp.discovery.target_concurrency_bare` (4 as shipped), absent means one at a time; 0 or less is refused ([details](phase-4-mcp-discovery.md#--concurrency-overlaps-addresses-and-nothing-else)) |
| `--agent-max-turns N` | `mcp.agent.max_turns_override` | turn budget for every phase-4 subagent, overriding each one's own. Default off, so the per-agent values stand ([details](phase-4-mcp-discovery.md#--agent-max-turns-is-the-lever-that-reduces-work)) |
| `--agent-max-attempts N` | `mcp.agent.max_attempts` | how many times the phase-4 agent may work one stage before the run moves on; the same as `--set mcp.agent.max_attempts=N`. A value is required and must be at least 1. Without it the setting decides, as before (3 as shipped) ([details](phase-4-mcp-discovery.md#--agent-max-attempts-is-how-many-corrections-a-stage-gets)) |
| `--mcp-provision-missing {auto,always,never}` | `mcp.packages.provision_missing` | when a local candidate needs a program that is not installed (`go`, `cargo`, `uvx`, `bunx`), install it from phase 1's catalog, once per run, then trial the candidate. `auto` (default) only without a password prompt; `never` rejects it as before. It never changes which server is chosen ([details](phase-4-mcp-discovery.md#a-missing-toolchain-is-installed-not-held-against-the-candidate)) |
| `--mcp-trust-ca FILE[,FILE]` | `mcp.trust.extra_ca` | extra certificate authorities (PEM) trusted for every probe, registered stdio server and generated bridge in the run, for targets behind an internal CA. Node needs the self-signed root, not only an intermediate ([details](phase-4-mcp-discovery.md#a-certificate-this-host-does-not-trust-is-handled-once-for-the-whole-run)) |
| `--mcp-trust-pin {targets,never}` | `mcp.trust.pin_presented_chain` | when a target's certificate is not trusted here, `targets` (default) learns the chain it presents — addresses you asked for only, kept under `mcp.trust.store_dir` and reported with its fingerprint; `never` does not ([details](phase-4-mcp-discovery.md#a-certificate-this-host-does-not-trust-is-handled-once-for-the-whole-run)) |
| `--exhaustive-mcp` | `mcp.exhaustive` | record every working endpoint found per address, not just the first ([details](phase-4-mcp-discovery.md#--exhaustive-mcp-records-every-endpoint-that-answers)) |
| `--mcp-recheck LIST` | `mcp.recheck` | after you acted on the report (exported a variable, logged a client in, enabled an account), prove these addresses again: only their existing and last working entries are tried, with the usage checks and repairs. Nothing is searched for. `all` (`mcp.recheck_all_word`) re-checks every entry of the project file: what to run after copying `.mcp.json` to another machine ([details](phase-4-mcp-discovery.md#what-is-registered-has-to-survive-this-machine)) |
| `--no-reuse-existing-mcp` | `mcp.discovery.reuse_existing` | do not offer servers already registered on this machine (`.mcp.json`, `claude mcp list`) as candidates for the address they point at; by default each one is verified at its own tier like anything discovery found ([details](phase-4-mcp-discovery.md#a-server-already-registered-here-is-tried-first)) |
| `--the-best-mcp` | `mcp.pick_best` | keep only the best server found per address, scored and shown ([details](phase-4-mcp-discovery.md#--the-best-mcp-picks-one-server-and-shows-its-working)) |
| `--no-agent` | `mcp.agentic` | run the stages deterministically only — no Claude subagent is started ([details](phase-4-mcp-discovery.md#discovery-is-agentic-and-the-agent-keeps-working)) |

Whatever is found is always written to `.mcp.json` in the current directory and
registered with the Claude CLI — there is no flag for half of it
([details](phase-4-mcp-discovery.md#what-gets-written-and-where)).
