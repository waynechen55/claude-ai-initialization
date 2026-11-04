# initialize-claude-ai

[![CI](https://github.com/waynechen55/claude-ai-initialization/actions/workflows/ci.yml/badge.svg)](https://github.com/waynechen55/claude-ai-initialization/actions/workflows/ci.yml)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Release](https://img.shields.io/github/v/release/waynechen55/claude-ai-initialization?include_prereleases&sort=semver)](https://github.com/waynechen55/claude-ai-initialization/releases)

**Take a bare Linux machine to a working, verified Claude Code environment — and
then find it MCP servers.**

One command installs the toolchain, signs in, configures the backend, and goes
looking for Model Context Protocol servers for whatever hosts you name. Nothing
is reported as working until it has been proved: a credential by a live round
trip, an MCP server by a real `initialize` handshake.

```bash
pip install initialize_claude_ai-*.whl                   # from a GitHub Release (see Install)
initialize-claude-ai run --dry-run                       # see it, change nothing
initialize-claude-ai run --mcp-server gitlab.example.com # do it
```

Four phases, run in order or individually:

| # | Phase | Mode | What it does |
|---|-------|------|--------------|
| 1 | `install` | traditional | Installs and verifies the Claude CLI, the Python/Node SDKs, the agent SDKs, the desktop app, the Google Cloud CLI, a browser and their prerequisites; records every binary path and version. |
| 2 | `auth` | traditional | Authenticates against Google Cloud, claude.ai (Claude Code CLI) and the Anthropic API — with headless flows for machines that have no browser. |
| 3 | `provision` | traditional | Picks the backend, project, region, account and models; writes `settings.json` and an environment file; prints the full inventory. |
| 4 | `mcp` | **agentic**, falling back to traditional | Four staged agents find, build or invent a working MCP server for a target, verify it with a real handshake, and register it. |

Phases 1–3 are deliberately traditional: they run *before* a Claude environment
exists, so they cannot depend on one. Phase 4 runs once that environment is
ready, which is what lets it use subagents and skills.

## Highlights

**Nothing is hardcoded.** Every package name, URL, path, model id, region,
timeout, command template, retry count and signal name is a key in
`initialize_claude_ai/data/*.toml`, documented in place, and overridable from a
config file, an env file, an environment variable or `--set`. Supporting one
more Linux distribution is a data change. See [Configuration](docs/configuration.md#configuration).

**No credential value is ever written anywhere.** Secrets travel as *references*
(`env:`, `file:`, `keyring:`, `command:`, `gcloud-adc`, …) and are resolved at
the moment of use — never in a log, a report, a generated file or a command line
where `ps` would show it. `--secure` refuses inline values outright.
See [Credentials](docs/credentials.md#credentials) and [SECURITY.md](SECURITY.md).

**Claimed is not the same as proved.** Every credential is confirmed with a live
round trip. Every MCP server must be *connected*, *callable* and *two-way* — a
request sent after the handshake and answered on the session it opened — then
re-checked at the end of the run and checked once more by the `claude` CLI
itself. An agent that says it found a server produces a failed stage if the
server does not answer.

**The agent owns its stage.** Phase 4 is genuinely agentic: each stage is a
Claude subagent with a skill, worked up to three times, every retry handed
exactly what went wrong with the last one. Deterministic probing is the fallback
after all of that, not the first excuse. `--no-agent` turns it off entirely.

**Tier order is enforced, not suggested.** The target's own MCP endpoint beats a
published package, which beats a bridge written on the spot — and the lower
tiers stay *shut* until a four-step checklist has disproved the higher one, with
the evidence printed. See
[Native endpoint first](docs/phase-4-mcp-discovery.md#native-endpoint-first--and-the-lower-tiers-are-gated-on-proving-there-isnt-one).

**A 401 is a finding, not a failure.** An endpoint that refuses an anonymous
client has proved it exists. It is registered with its credential slot left
empty and the variable named, so obtaining a token later is one `export` rather
than a second discovery run.

**Re-running is cheap and `--dry-run` is honest.** Everything is idempotent —
what is already installed, signed in or configured is left alone. In a dry run
the read-only probes still execute against the real machine and the real
network, so what it reports is what is actually there; only mutations are
simulated.

**It tells you what it did.** Every phase reports per step, with a verdict
derived from those steps, remediation attached to the thing that needs it, and a
`--json` report carrying the same data as the screen. The whole run is
transcribed to a log file.

## Table of contents

- [Install](#install) · [Typical use cases](#typical-use-cases) — e.g. [everything in one run](#everything-in-one-run-vertex-ai-a-pinned-opus-model-mcp-servers-for-your-hosts)
- [Documentation](docs/README.md) — the manual:
  [usage](docs/usage.md) ·
  phases [1 install](docs/phase-1-install.md) · [2 authenticate](docs/phase-2-authenticate.md) ·
  [3 provision](docs/phase-3-provision.md) · [4 MCP auto-discovery](docs/phase-4-mcp-discovery.md) ·
  [configuration](docs/configuration.md) · [credentials](docs/credentials.md) ·
  [exit status](docs/exit-status.md) · [limitations](docs/limitations.md)
- Reference: [command line](docs/cli-reference.md) · [every setting](docs/config-reference.md)
- [Contributing](#contributing) · [Security](#security) · [Changelog](CHANGELOG.md) · [License](#license)

## Install

From a [GitHub Release](https://github.com/waynechen55/claude-ai-initialization/releases)
(the project is not on PyPI), checking the download against the release's
`SHA256SUMS`:

```bash
VERSION=0.1.0
BASE=https://github.com/waynechen55/claude-ai-initialization/releases/download/v$VERSION
curl -fLO "$BASE/initialize_claude_ai-$VERSION-py3-none-any.whl" -fLO "$BASE/SHA256SUMS"
sha256sum -c SHA256SUMS --ignore-missing
python3 -m pip install --user "initialize_claude_ai-$VERSION-py3-none-any.whl"
initialize-claude-ai --version
```

Or from a clone: `python3 -m pip install -e .` (or `uv pip install -e .`).

Python 3.10 or newer on Linux. Only the standard library is required (plus
`tomli` on 3.10). `httpx` (extra: `http`) and the agent SDK (extra: `agentic`)
are used when present and degrade gracefully when not — phase 1 installs them
into the target environment anyway.

Portable across the mainstream Linux distributions: `apt`, `dnf`, `yum`,
`zypper`, `pacman`, `apk`, plus `flatpak`, `snap` and Homebrew as fallbacks.
Package-manager quirks live in `initialize_claude_ai/data/10-components.toml`, not in code.

## Typical use cases

### A fresh machine, start to finish

```bash
initialize-claude-ai run --dry-run          # read-only: what would happen
initialize-claude-ai run                    # all four phases
```

Nothing already present is touched, so this is also the command to re-run after
a change; it reports what it found and moves on.

### Everything in one run: Vertex AI, a pinned Opus model, MCP servers for your hosts

A verified end-to-end run: rebuild the toolchain, sign in to Google Cloud
again, configure Claude Code for Vertex AI with a specific Opus model, and find
MCP servers for the services your team uses — in one command. Use it to set up
a new machine completely, or to refresh one whose installation or sign-in has
gone stale.

**Before you start** — what the run checks but cannot do for you:

- a Google Cloud project with the Vertex AI API (`aiplatform.googleapis.com`)
  enabled and Claude models enabled for it, and an account allowed to call
  them. Phase 3 checks the API and reports it if it is off; add
  `--set auth.google_cloud.enable_apis=true` to have it enabled;
- `sudo` (or `doas`) rights if you are not root: phase 1 installs system
  packages through your package manager;
- network access to Google, the package registries and the hosts you name.

**Find the model id to pin** (optional). With a working Google sign-in, a dry
run of phase 3 asks Vertex which models it serves, changing nothing:

```bash
python3 -m initialize_claude_ai run --phases 3 --dry-run --gcloud-project my-gcp-project
```

The `model discovery` step lists the catalogue, and `models` shows the id it
would choose for each tier (`opus=… (inferred: opus)`). The report also points
to the full list — the project's Model Garden page and
`gcloud ai model-garden models list`. Copy an id from there.

**The run:**

```bash
cd ~/work/my-project        # .mcp.json and the run log are written here
python3 -m initialize_claude_ai run --phases all \
    --force-install \
    --force-auth --auth-backend google-cloud --gcloud-project my-gcp-project \
    --default-model opus --default-opus-model <opus-model-id> \
    --mcp-server gitlab.example.com,jira.example.net,docs.example.org,wiki \
    --agent-max-turns 16 \
    --concurrency
```

(`initialize-claude-ai run …` is the same command once the package is installed.
`--agent-max-turns` and `--concurrency` are optional.)

| Option | What it does here |
| --- | --- |
| `--phases all` | install → auth → provision → mcp, in that order (also the default) |
| `--force-install` | reinstalls every component phase 1 manages — the Claude CLI, SDKs, agent SDKs, gcloud, the toolchains registered servers run with — even those that already verify. Use it after an upgrade went wrong or to refresh everything; leave it out for a normal re-run, which skips what works. Free disk space is checked before each step |
| `--force-auth` | signs in again even where a working credential was found (the existing one is reported, not removed). `--force-auth-with-reset` revokes it first |
| `--auth-backend google-cloud` | obtain only the Google Cloud credentials Vertex AI reads (gcloud login and application-default credentials); claude.ai and the Anthropic API are left alone |
| `--gcloud-project ID` | the project for the whole run, by its project ID (as `gcloud projects list` shows it): phase 2 signs in against it and sets it in gcloud, phase 3 bills Vertex to it |
| `--default-model opus` | the tier Claude Code starts on, written as `"model"` in `settings.json` |
| `--default-opus-model ID` | pins the Opus tier to this model id, spelled as Vertex spells it. Phase 3 reads the backend's live catalogue: a pin it does not list is reported as "not in the backend's catalogue" with ids it does serve (the run continues; `provision.strict_model_check = true` makes that a failure). Leave the flag out to get the best Opus the catalogue offers |
| `--mcp-server LIST` | the services to find MCP servers for, comma separated (no spaces), written however you have them: `gitlab.example.com` is probed as `https://…`, a bare internal name like `wiki` as `http://wiki` |
| `--agent-max-turns N` | turn budget for every phase-4 subagent, replacing each stage's own (10–24). Lower is cheaper and faster; higher gives hard targets more room (pair it with `--timeout-scale`) |
| `--concurrency [N]` | works N addresses at once (bare: 4). Shortens the wall-clock wait, not the work or the tokens; output stays grouped by address |

No backend flag is needed: `auto` picks the first backend phase 2 authenticated
(`provision.backend_priority`), which with only Google Cloud signed in is Vertex
AI. Add `--provision-backend vertex` to make that explicit, and
`--gcloud-region us-east5` (default `global`) to choose a location.

**What to expect:**

- **It asks you to sign in.** The Google sign-in opens a browser, or on a
  machine without one shows a URL to open elsewhere (`--headless` forces that).
  Confirmations are asked once; `--auto-agree` answers them for unattended runs.
- **Phase 4 takes the longest** — minutes per address, since agents search,
  install and prove each candidate. `--concurrency` overlaps addresses;
  `--agent-max-turns` and `--no-agent` reduce the work itself.
- **What it writes:** `~/.claude/settings.json` (backend, project, region,
  models), the environment file phase 3 reports, `./.mcp.json` with every
  server that was proved by a handshake and a real tool call, and
  `./claude_ai_initialization.log` with the whole run.

**Reading the result.** Each phase ends with a summary box and a verdict —
`PASSED`, `PASSED WITH CHANGES`, `PARTIALLY PASSED` or `FAILED` — and every step
that needs you carries its remedy on the next line. The exit status is `0` on
success (a partial success too, unless `run.partial_is_failure = true`), `1`
when a phase failed or the configuration was rejected, `2` for a refused
command line, `130` when interrupted ([exit status](docs/exit-status.md)). Per address, phase 4 says
whether a server was written and working, written and **waiting for a
credential**, or not found — with what was tried.

- **A server waiting for a credential** is written with its slot empty, and the
  report names the exact variable to set. Set it in Claude Code's settings
  `env` (or your shell), then confirm with
  `python3 -m initialize_claude_ai run --phases 4 --mcp-recheck <address>`.
- **Afterwards**, open a new terminal in the same directory and start `claude`;
  `/mcp` lists the servers, and `claude mcp list` shows them connected.

**Variations:**

```bash
# Look first: every read-only probe runs for real, nothing is changed
python3 -m initialize_claude_ai run --phases all --dry-run --gcloud-project my-gcp-project ...

# Everyday re-run: drop --force-install/--force-auth; what works is left alone
python3 -m initialize_claude_ai run --gcloud-project my-gcp-project --mcp-server gitlab.example.com

# Only the MCP part, for more hosts, later
python3 -m initialize_claude_ai run --phases 4 --mcp-server bugzilla.example.com --concurrency

# Prove every entry of an existing .mcp.json again
python3 -m initialize_claude_ai run --phases 4 --mcp-recheck all
```

### A server with no browser

Sign-in flows that would open a browser switch to a paste-the-URL flow, chosen
once for the whole run:

```bash
initialize-claude-ai run --phases 2 --headless
```

### Pin a backend deliberately

```bash
# Vertex AI in a specific project and region
initialize-claude-ai run --phases 3 --provision-backend vertex \
    --gcloud-project my-project --gcloud-region us-east5

# The Anthropic API, with the key supplied by reference, never by value
initialize-claude-ai run --phases 2,3 --provision-backend anthropic-api \
    --auth-anthropic-key-ref env:ANTHROPIC_API_KEY

# Bedrock. It has no region flag of its own, because there is no sensible
# default for which region: AWS_REGION, or name it explicitly.
initialize-claude-ai run --phases 3 --provision-backend bedrock \
    --set provision.bedrock.region=us-east-1
```

That last line is the general escape hatch: any setting at all can be given as
`--set dotted.key=value`, whether or not it has a flag.

### Find MCP servers for the hosts your team actually uses

```bash
initialize-claude-ai run --phases 4 \
    --mcp-server gitlab.example.com,jira.example.com,https://wiki.example.com
```

Each address is canonicalised, then worked through the four stages until one
settles it. What verifies is written to `./.mcp.json` *and* registered with
`claude mcp add-json`, so the next Claude Code session in that directory has it.

### Choose how wide the search goes

```bash
# default: stop at the first server that answers, per address
initialize-claude-ai run --phases 4 --mcp-server example.com

# every endpoint that answers, each registered under its own name
initialize-claude-ai run --phases 4 --mcp-server example.com --exhaustive-mcp
initialize-claude-ai run --phases 4 --mcp-recheck example.com

# a .mcp.json copied from another machine: prove every entry here
initialize-claude-ai run --phases 4 --mcp-recheck all

# search wide, then keep the single best one, with the scorecard printed
initialize-claude-ai run --phases 4 --mcp-server example.com --the-best-mcp
```

### An internal host with a self-signed certificate

Discovery retries once with verification relaxed and says so; an endpoint found
that way is flagged and penalised rather than passed off as verified. When the
server presents its intermediates, the run learns them for that target and
reports the fingerprint. Otherwise, give it the authority that actually
verifies your CA:

```bash
initialize-claude-ai run --phases 4 --mcp-server gitlab.internal --mcp-trust-ca ~/internal-root-ca.pem
```

### Discovery without starting an agent

For an air-gapped box, a CI job, or a run that must not spend tokens:

```bash
initialize-claude-ai run --phases 4 --no-agent --mcp-server example.com
```

### Reproducible runs from one file

Put the whole configuration in an env file and keep it out of the command line:

```bash
cp .env.example .env && $EDITOR .env
initialize-claude-ai run --env-file .env
```

### Ask what it would use, without running anything

```bash
initialize-claude-ai config                       # the whole effective document
initialize-claude-ai config provision.vertex      # one subtree
initialize-claude-ai config components            # what --install-only can name
initialize-claude-ai phases                       # phases, and whether each is enabled
initialize-claude-ai env                          # what earlier runs recorded
```

### Machine-readable output for CI

```bash
initialize-claude-ai run --json --report /tmp/run.json
echo $?    # 0 ok · 1 a phase failed · 2 bad command line (or partial, with run.partial_is_failure) · 130 interrupted
```

## Contributing

Bug reports, ideas and pull requests are welcome. Start with
[CONTRIBUTING.md](CONTRIBUTING.md) — it covers the two conventions that shape
everything here (**nothing is hardcoded**; **no credential value appears in
code, logs, reports or generated files**) and the pull-request shape CI
enforces: **one commit, with a title and a body**.

```bash
git clone https://github.com/waynechen55/claude-ai-initialization
cd claude-ai-initialization
python3 -m pip install -e ".[dev]"
make check          # lint + format, tests, generated docs, links, names, version
```

The suite is offline and hermetic: it needs no credentials, no network and no
Claude installation, and it cannot touch your real `~/.claude`. Please follow
the [Code of Conduct](CODE_OF_CONDUCT.md).

## Security

This is a tool whose whole job is handling other people's credentials, so
[SECURITY.md](SECURITY.md) states the guarantees explicitly — what is stored
(nothing), how secrets travel (as references, resolved at the point of use), and
what happens to agent output before it is acted on.

Found a vulnerability? Please report it
[privately](https://github.com/waynechen55/claude-ai-initialization/security/advisories/new)
rather than opening an issue.

## License

[MIT](LICENSE) © 2026 Wayne Chen.
