# Repository settings

CI checks every pull request (see [CONTRIBUTING.md](../CONTRIBUTING.md#pull-requests)),
but only GitHub's settings make those checks *required*. These are the
settings the project expects; [`tools/apply-repo-settings.sh`](../tools/apply-repo-settings.sh)
applies all of them with the GitHub CLI:

```bash
gh auth login                      # as an administrator of the repository
tools/apply-repo-settings.sh waynechen55/claude-ai-initialization
```

Run it once after the first push to `main` (branch protection needs the branch
to exist, and the required check needs CI to have run once so GitHub knows its
name).

## Merging

| Setting | Value | Why |
| --- | --- | --- |
| Allow squash merging | on | `main` gets one commit per pull request |
| Squash commit title / message | pull request title / description | the commit on `main` has a title and a body, like the one CI checked |
| Allow merge commits | off | keeps history linear |
| Allow rebase merging | off | one way in |
| Automatically delete head branches | on | tidy |

## Branch protection for `main`

| Setting | Value |
| --- | --- |
| Require a pull request before merging | on (approvals: 0 for a single maintainer; set `REVIEWS=1` when there are more) |
| Require status checks to pass | on, **strict** (branch must be up to date) |
| Required check | **CI passed** — the aggregate job in `ci.yml`; it fails if any other job failed, including the commit check on pull requests |
| Require linear history | on |
| Require conversation resolution | on |
| Include administrators | on |
| Allow force pushes / deletions | off |

Requiring the single **CI passed** check rather than each job means adding or
renaming a job never needs a settings change.

## Release tags

A tag ruleset on `refs/tags/v*` forbids moving (update, non-fast-forward) and
deleting release tags; repository administrators may bypass it to repair a
failed release (see [release-process.md](release-process.md)).

## Security

- **Private vulnerability reporting:** on (the script enables it), so
  [SECURITY.md](../SECURITY.md)'s reporting link works.
- **Dependabot:** [`.github/dependabot.yml`](../.github/dependabot.yml) keeps the
  SHA-pinned actions and the Python tools current; its pull requests are
  one commit each and pass through the same checks.
- **Workflow permissions:** the workflows declare `contents: read`; only the
  release job's publish step has `contents: write`. Leave the repository
  default (Settings → Actions → General) at *read repository contents*.
