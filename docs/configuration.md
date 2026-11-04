# Configuration

Nothing is hardcoded. Every package name, URL, path, model id, region, timeout
and command template lives in `initialize_claude_ai/data/*.toml` and can be overridden,
in increasing precedence, by:

0. the `env` block of the `settings.json` this program itself rewrites, which a
   parent Claude Code exports into our environment — our own previous answer,
   and therefore the weakest source there is ([why](#our-own-last-answer-is-the-weakest-source))
1. packaged defaults — `initialize_claude_ai/data/*.toml`
2. `/etc/initialize-claude-ai/config.toml` (or `$INITIALIZE_CLAUDE_AI_SYSTEM_CONFIG`)
3. `$XDG_CONFIG_HOME/initialize-claude-ai/config.toml`
4. `./initialize-claude-ai.toml`
5. `--config FILE` (repeatable, later wins)
6. `--env-file FILE` / `$INITIALIZE_CLAUDE_AI_ENV_FILE`
7. `INITIALIZE_CLAUDE_AI_<SECTION>__<KEY>=value` — `__` separates nesting levels
8. `--set dotted.key=value`

String values interpolate `${ENV_VAR}`, `${ENV_VAR:-default}` and
`${cfg:dotted.key}`, including nested forms, which is how the shipped defaults
reference the host environment instead of baking values in.

```bash
initialize-claude-ai config                       # the whole effective document
initialize-claude-ai config mcp.discovery         # one subtree
INITIALIZE_CLAUDE_AI_MCP__EXHAUSTIVE=true initialize-claude-ai run --phases 4 --mcp-server example.com
```

See `.env.example` for the variables worth knowing about.

## A timeout bounds the wait, not just the command

`run.command_timeout_seconds` (600 by default; individual calls override it, and
the discovery agents get `mcp.agent.timeout_seconds`) is the longest any one
command may take. The other half of that promise is what happens when a command
takes longer:

| key | default | what it does |
| --- | --- | --- |
| `run.stop_signals` | `["TERM", "KILL"]` | sent in order to the timed-out command and everything it started |
| `run.stop_grace_seconds` | `5` | how long each of those signals is given to work |

Children are started in a session of their own, so a signal reaches the whole
tree rather than only the process we launched — the usual reason a "killed"
command carries on is a grandchild nobody had a handle on. If the last signal
in the list goes unanswered the child is *abandoned*: its pipes are dropped, the
step is recorded as timed out, and the run continues. Waiting is never the
fallback, because a wait with no deadline is how a 900-second limit turns into
an overnight stall.

A command that talks to the operator directly (a sign-in prompt, anything run
with the terminal handed over) stays in this process's session, so Ctrl-C still
reaches it; it is then signalled on its own.

**Trialled MCP servers get the same guarantee.** Phase 4 launches each stdio
candidate to handshake with it: package trials, generated bridges and the check
that every finished entry starts. That happens in agent and traditional mode
alike. Such a server runs in a process group of its own, and ending the trial
takes bounded time:

| key | default | what it does |
| --- | --- | --- |
| `mcp.verification.stdio_shutdown_grace_seconds` | `2` | how long the server is given to exit on its own after its stdin is closed; then its whole group goes down `run.stop_signals` |
| `mcp.verification.stdio_session_timeout_seconds` | `240` | ceiling on one whole session (initialize, capability lists, round trip); each request keeps its own timeout |

The shutdown used to close the server's output while a reader was blocked on
it. So a server that ignored stdin closing, or a launcher (`npx`, `uvx`,
`docker run`) whose child held the pipe, stalled the run with no further log
line. Each trial is now logged before it starts (`trialling 2 of 3: <name> --
<command>`), so a slow trial names itself.

## One env file can drive the whole run

No flag carries a default in the argument parser. A flag you leave out is
*silent* rather than assertive, so the value is looked for in the layers
underneath it — and `--env-file` lands in the environment before any `${VAR}`
in the packaged defaults is expanded. That is what lets a single global env
file of plain `UPPER_CASE` variables configure a run with no flags at all:

```bash
initialize-claude-ai run --env-file /etc/claude/global.env
```

The defaults read these by their ordinary names — no `INITIALIZE_CLAUDE_AI_`
prefix, because these are the names people already have exported:

| Variable | Equivalent flag | Setting |
| --- | --- | --- |
| `GOOGLE_CLOUD_USER_ACCOUNT` | `--gcloud-user-account` | `auth.google_cloud.account` |
| `GOOGLE_CLOUD_PROJECT`, `CLOUDSDK_CORE_PROJECT`, `ANTHROPIC_VERTEX_PROJECT_ID` | `--gcloud-project` | `auth.google_cloud.project` |
| `ANTHROPIC_VERTEX_PROJECT_ID`, `GOOGLE_CLOUD_PROJECT`, `CLOUDSDK_CORE_PROJECT` | `--gcloud-project` | `provision.vertex.project` |
| `CLOUD_ML_REGION`, `GOOGLE_CLOUD_LOCATION` | `--gcloud-region` | `provision.vertex.region` |
| `CLAUDE_CODE_USE_VERTEX` | `--use-vertex` | `provision.vertex.enabled` |
| `DEFAULT_MODEL` | `--default-model` | `provision.settings.model` |
| `ANTHROPIC_DEFAULT_OPUS_MODEL` | `--default-opus-model` | `provision.<backend>.model_aliases.opus` |
| `ANTHROPIC_DEFAULT_SONNET_MODEL` | `--default-sonnet-model` | `provision.<backend>.model_aliases.sonnet` |
| `ANTHROPIC_DEFAULT_HAIKU_MODEL` | `--default-haiku-model` | `provision.<backend>.model_aliases.haiku` |
| `ANTHROPIC_BASE_URL` | — | `provision.anthropic_api.base_url` |
| `AWS_REGION` | — | `provision.bedrock.region` |

Where two variables are listed, the first one set wins. An exported shell
variable beats the file — the file is a floor, not a ceiling — and both are
beaten by a flag. Anything not in the table is still reachable by its generated
name, `INITIALIZE_CLAUDE_AI_PROVISION__VERTEX__REGION` and so on, or with
`--set provision.vertex.region=us-east5`.

This table is not prose: it is the `[sources]` section of
`initialize_claude_ai/data/00-core.toml`, which names for each setting the flag
and the variables that feed it. Adding a source is a name in a list there and a
`${...}` in the packaged defaults, with nothing to change in Python.

## Our own last answer is the weakest source

Claude Code exports the `env` block of its settings files into every process it
launches. Phase 3 *writes* that block — so a run started from inside Claude Code
is handed back its own previous output, dressed as the environment:
`CLOUD_ML_REGION`, `ANTHROPIC_VERTEX_PROJECT_ID`, `CLAUDE_CODE_USE_VERTEX`, the
three tier pins. Read at face value those outrank an env file and every
configured default, and the result is a loop — the first value ever written is
the one that goes on being written, and correcting it anywhere but on the
command line has no effect. A hand-edited `CLOUD_ML_REGION=us` survived every
run on a machine whose shell said `GOOGLE_CLOUD_LOCATION=global`, exactly this
way.

So those variables are taken back out of the environment before anything is
resolved, which leaves the order you would expect:

**command line → env file / environment → configured defaults → the settings
file already on disk.**

Only an exact match is demoted: a variable whose value *differs* from the file
cannot be that file's echo, so it is somebody's real answer and is left alone.
Only the names `environment.echo_names` covers are eligible — the coordinates
this program writes — and never the ones `environment.echo_keep` covers, since a
credential kept in the settings file is the only copy of it there is; demoting
that would not correct a stale answer, it would lose the answer.

Last is still a source. The values are kept, and a setting with nothing else to
go on may fall back to one — the Vertex project does, because unlike the region
it has no default worth inventing:

```
✔ [   OK   ] project  old-project (already in settings.json)
✔ [   OK   ] region  global (GOOGLE_CLOUD_LOCATION)
```

The run header lists each demoted variable as `echo:NAME` beside the other
configuration sources. To switch the whole thing off and go back to treating the
exported block as an ordinary environment:

```bash
initialize-claude-ai run --set environment.demote_echo=false
```

## When there is nothing to use, the run stops

Some values have no honest default and cannot be inferred: the Google Cloud
project Vertex bills, the region Bedrock lives in. If one of those is still
empty after the flag, the config files, the environment and the env file have
all been consulted, the phase **fails outright** and the run stops with a
non-zero exit status. Nothing is written.

```
✘ [  FAIL  ] project  vertex needs a project and none was found
      · Pass it on the command line: --gcloud-project <value>
      · Or set one of these in the environment, or in a file given to --env-file:
        ANTHROPIC_VERTEX_PROJECT_ID, GOOGLE_CLOUD_PROJECT, CLOUDSDK_CORE_PROJECT
      · Or: the project phase 2 signed in against
      · Or: gcloud config set project <project>
      · Or set it directly: --set provision.vertex.project=<value>
```

The alternative would be to substitute something plausible, and that is worse
than stopping: a wrong project bills somebody else and then fails with a
permission error naming a project nobody chose — much harder to trace back than
being told here. The failure is also *fatal* rather than partial, which matters
because the steps before it did pass. A failure sitting beside successes
normally rolls up to `PARTIAL`, at both levels: the phase would report "2 of 3
passed", and a run whose phases 1 and 2 went well would report *partially
passed* and exit `0`. That is the right reading of most failures and the wrong
reading of this one, so a fatal result overrides the roll-up in both places —
the phase is `FAILED`, the run is `FAILED`, and the exit status is `1`. The JSON
report carries `"fatal": true` on the phase and on the run, for a caller that
has to tell this apart from an ordinary failure.

`--ignore-failure` still does what it says and lets the remaining phases run,
but it cannot turn this green: the run is reported as failed either way.

Which values are required is configuration, not code — `provision.required`
lists them per backend, and `provision.require_model = false` relaxes the
"at least one model resolved" rule.
