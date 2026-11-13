# Changelog

All notable changes to this project are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Each release's section is also its GitHub Release notes: the release workflow
publishes exactly the text under the version's heading. How a release is cut
is described in [docs/release-process.md](docs/release-process.md); the
detailed record of how the program reached 0.1.0 is in
[docs/history.md](docs/history.md).

## [Unreleased]

### Fixed

- A logged-in call answered by a gateway's web page (404/406/415) is no longer
  read as a refused login or a missing account. The request's form is tried
  first (`[mcp.verification.request_forms]`): each request-form variable the
  launch reads -- `MCP_BRIDGE_ACCEPT` -- is varied, and the value under which
  the login is accepted is written into the entry. A 404 means "no account"
  only when the API itself says it.
- Generated bridges send a single `Accept` value (`*/*`, or the one media type
  an operation documents) instead of a weighted list, which Open Build
  Service rejected for logged-in requests. A read-only call answered
  404/406/415 is retried once with `*/*`, and the value that worked is kept
  for the session (`accept_retry_statuses`, `accept_fallbacks`).

## [0.1.0] - 2026-11-04

First public release, under the Apache License 2.0.

### Added

- **Phase 1 — install.** Verifies, installs and re-verifies the Claude Code
  CLI, the Python and Node SDKs, the agent SDKs, the desktop app, the Google
  Cloud CLI, a browser and the toolchains registered MCP servers run with,
  across `apt`, `dnf`, `yum`, `zypper`, `pacman`, `apk`, `flatpak`, `snap` and
  Homebrew. Disk space is checked before each step; a full disk stops the phase
  with one verdict.
- **Phase 2 — authenticate.** Google Cloud, claude.ai and the Anthropic API,
  each as inspect → acquire → verify, with headless flows for machines without
  a browser. A login counts only after a live call proves it, and it is kept
  where a new terminal's Claude Code reads it.
- **Phase 3 — provision.** Resolves backend, account, project, region and
  models — asking the backend what it will serve — then writes `settings.json`
  and the environment files and prints the inventory.
- **Phase 4 — MCP auto-discovery.** For each address: the target's own MCP
  endpoint, a published package, or a generated bridge, in that order, found by
  Claude subagents with skills (agent mode) or deterministic probing
  (`--no-agent`). Every candidate is proved by an MCP handshake and a real
  tool call, written to `.mcp.json` with its credential slots named, and
  re-checked as Claude Code itself will run it.
- `--concurrency`, `--exhaustive-mcp`, `--the-best-mcp`, `--mcp-recheck`,
  `--agent-max-turns`, `--agent-max-attempts`, `--timeout-scale` and
  `--do-cleanup` to control how wide, how deep and how long discovery runs.
- Certificate trust handled end to end: a CA the host does not trust is learned
  once per run and handed to the runtime that connects (Node, Python, curl),
  never written into an entry.
- `.mcp.json` files that travel: per-machine paths are `${ICA_MCP_*}`
  references with the original as default, and phase 1 on the next machine
  sets them.
- Layered configuration: every list, pattern, path and timeout lives in
  `initialize_claude_ai/data/*.toml` and can be overridden from a file, the
  environment or `--set`. Credentials are handled as references only.
- Documentation under [docs/](docs/README.md), with generated
  [CLI](docs/cli-reference.md) and [configuration](docs/config-reference.md)
  references.
- Offline, hermetic test suite (no network, credentials or Claude installation
  needed), including a regression corpus of real failures.
- Continuous integration: one-commit pull requests with a title and a body,
  lint and format, tests on Python 3.10–3.13 and on openSUSE Tumbleweed, a
  package build and install check, a secret and internal-name scan, and
  generated-documentation and link checks. Tagged releases are built and
  published to GitHub Releases with checksums.

[Unreleased]: https://github.com/waynechen55/claude-ai-initialization/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/waynechen55/claude-ai-initialization/releases/tag/v0.1.0
