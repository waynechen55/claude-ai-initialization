<!--
A pull request is ONE commit whose message has a title line, a blank line and a body.
CI fails otherwise. Squash before pushing:  git reset --soft origin/main && git commit
Check locally:                               python tools/check_commits.py origin/main HEAD
-->

## What and why

## How it was verified

- [ ] `make check` (lint, format, tests, docs, names) passes locally
- [ ] New behaviour has a test; a bug fix has a test that failed before the fix
- [ ] Lists, patterns and limits that decide behaviour are in `initialize_claude_ai/data/*.toml`, not code
- [ ] No credential values, internal host names or personal paths (use `example.com`, `/home/user`)
- [ ] Docs updated; `python tools/gen_docs.py` re-run if options or settings changed
- [ ] `CHANGELOG.md` has an entry under `[Unreleased]` for a user-visible change
