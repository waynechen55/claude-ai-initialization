# Release process

Releases are published on
[GitHub Releases](https://github.com/waynechen55/claude-ai-initialization/releases)
only; the project is not uploaded to PyPI. Each release carries the wheel, the
source distribution and a `SHA256SUMS` file, and its notes are the version's
section of [CHANGELOG.md](../CHANGELOG.md).

## Versioning

[Semantic Versioning](https://semver.org/spec/v2.0.0.html), `MAJOR.MINOR.PATCH`:

- **PATCH** — fixes that change no documented behaviour, option or setting.
- **MINOR** — new options, settings or behaviour; while the version is 0.x, a
  change that breaks a documented option or setting also goes here, and the
  changelog says so under **Changed** or **Removed**.
- **MAJOR** — from 1.0.0 on, any breaking change to the command line, the
  configuration keys or the files the program writes.
- Pre-releases are `X.Y.Z-rc.N`; the release workflow marks them as such.

The version is written in exactly one place,
`initialize_claude_ai/__init__.py` (`__version__`). `pyproject.toml` reads it
from there, and `tools/check_version.py` (CI's `docs` job, `make version`)
fails when the changelog, the bridge's fallback `SERVER_VERSION` or a release
tag disagrees with it.

## The changelog

[Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Every user-visible
change adds a line under `## [Unreleased]`, in the pull request that makes it,
grouped as **Added**, **Changed**, **Deprecated**, **Removed**, **Fixed** or
**Security**. Write for someone deciding whether to upgrade: what changed for
them, not how the code changed.

## Cutting a release

1. Make sure `main` is green.
2. In one pull request (one commit, as always), titled e.g. `Release 0.2.0`:
   - set `__version__` in `initialize_claude_ai/__init__.py` and
     `SERVER_VERSION` in `initialize_claude_ai/mcp_auto_discovery/bridge.py`;
   - rename `## [Unreleased]` to `## [0.2.0] - YYYY-MM-DD` and add a new empty
     `## [Unreleased]` above it;
   - update the links at the bottom of the changelog:
     ```
     [Unreleased]: https://github.com/waynechen55/claude-ai-initialization/compare/v0.2.0...HEAD
     [0.2.0]: https://github.com/waynechen55/claude-ai-initialization/compare/v0.1.0...v0.2.0
     ```
   - run `make check` and `python tools/check_version.py --notes` to read the
     notes as they will be published.
3. After it is merged, tag the merge commit on `main` and push the tag:
   ```bash
   git switch main && git pull --ff-only
   git tag -a v0.2.0 -m "Release 0.2.0"
   git push origin v0.2.0
   ```
4. The [Release workflow](../.github/workflows/release.yml) then checks that the
   tag names the version and is on `main`, runs the tests, builds and checks
   the distributions, installs the wheel, and publishes the GitHub Release
   with the changelog section as its notes.

If the workflow fails before publishing, fix the cause on `main`, delete the
tag (`git push --delete origin v0.2.0 && git tag -d v0.2.0`) and tag again. A
published release is never replaced; ship the next patch version instead.

## Installing a release

```bash
VERSION=0.1.0
BASE=https://github.com/waynechen55/claude-ai-initialization/releases/download/v$VERSION
curl -fLO "$BASE/initialize_claude_ai-$VERSION-py3-none-any.whl"
curl -fLO "$BASE/SHA256SUMS"
sha256sum -c SHA256SUMS --ignore-missing
python3 -m pip install "initialize_claude_ai-$VERSION-py3-none-any.whl"
```
