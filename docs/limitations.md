# Limitations and non-goals

Worth knowing before you adopt it.

## Platform

- **Linux is what this targets.** The package managers, the credential paths and
  the process handling are written for it. macOS gets as far as Homebrew and
  will partly work; Windows is not supported, and the subprocess teardown
  (process groups, signal ladders) is POSIX-only.
- **Python 3.10 or newer.** 3.10 additionally needs `tomli`, which is declared
  as a dependency for that version only.

## Phase 4 is slow, and that is inherent

- A stage is a Claude subagent reasoning and making network requests. **Five to
  ten minutes per stage per address is normal**, and a wide run over a dozen
  addresses is measured in hours. There is no per-target wall-clock budget yet
  (`--timeout` bounds the whole run), and addresses are worked **one at a time**
  unless you pass `--concurrency`
  ([details](phase-4-mcp-discovery.md#--concurrency-overlaps-addresses-and-nothing-else)).
- It **costs tokens**. `mcp.agent.max_budget_usd` caps the spend and
  `--no-agent` removes it entirely, at the price of the reasoning that finds an
  endpoint convention would not have guessed.
- Agent behaviour is not deterministic. Two runs against the same host can
  search differently — which is why *verification* is deterministic, and why the
  registered result is reproducible even when the search is not.

## What it will not do for you

- **It does not obtain credentials you do not have.** An endpoint behind a login
  is registered with an empty slot and the variable named; getting a token is
  yours.
- **It does not manage secrets.** There is no vault, no storage, no rotation —
  it resolves references at the point of use and forgets them. Where the value
  lives is your decision.
- **It does not maintain what it generates.** A tier-3 bridge is code that did
  not exist a minute ago and that only this machine has. That is exactly why the
  tier order pushes so hard against reaching for one.
- **It does not sandbox third-party code.** Trialling a published MCP server
  runs someone else's package on your machine. It happens in a scratch
  directory, never in a dry run, and `mcp.packages.trial_run = false` turns it
  off — but the process is not confined.
- **It is not a Claude Code replacement or wrapper.** It sets the environment
  up; what you do in it afterwards is Claude Code's business.

## Known rough edges

- No WebSocket MCP transport, because Claude Code has none. A WebSocket endpoint
  is detected and reported as needing a stdio bridge in front of it.
- A host whose certificate chain this machine cannot verify produces noisy
  warnings during discovery; the fix is to install the CA, not to disable the
  check.
- `claude mcp list` is invoked per registered server rather than cached, which
  adds a second or two each.
- Pre-1.0: settings keys may be renamed between versions. They are all
  documented in `data/*.toml`, and removals are noted in
  [CHANGELOG.md](../CHANGELOG.md).
