# Documentation

Start with the [project README](../README.md) for what the program does and
the common commands. These pages are the manual.

## Using it

- [Usage](usage.md) — running phases, what a run looks like, the log file,
  the command-line options explained
- [Phase 1 — install](phase-1-install.md)
- [Phase 2 — authenticate](phase-2-authenticate.md)
- [Phase 3 — provision](phase-3-provision.md)
- [Phase 4 — MCP auto-discovery](phase-4-mcp-discovery.md)
- [Configuration](configuration.md) — layering, env files, timeouts
- [Credentials](credentials.md) — reference schemes and `--secure`
- [Exit status](exit-status.md)
- [Limitations and non-goals](limitations.md)

## Reference (generated)

- [Command-line reference](cli-reference.md) — the full `--help`
- [Configuration reference](config-reference.md) — every setting and its default

Both are produced by `tools/gen_docs.py` from the parser and
`initialize_claude_ai/data/*.toml`; CI fails when they are out of date.

## Developing it

- [Development](development.md) — layout, tests, repository checks
- [Contributing](../CONTRIBUTING.md) — the rules, and the pull-request shape CI enforces
- [Lessons](lessons.md) — real failures, what had been proved, the rule adopted
- [Release process](release-process.md) — versioning, the changelog, tagging
- [Repository settings](repository-settings.md) — branch protection and merge settings
- [History](history.md) — the detailed record of development up to 0.1.0
- [Changelog](../CHANGELOG.md) · [Security policy](../SECURITY.md) · [Code of Conduct](../CODE_OF_CONDUCT.md)
