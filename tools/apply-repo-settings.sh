#!/usr/bin/env bash
# Apply the repository settings described in docs/repository-settings.md.
#
# Needs the GitHub CLI, signed in as an administrator of the repository:
#   gh auth login
#   tools/apply-repo-settings.sh [OWNER/REPO]        # default: the origin remote
#
# Idempotent: re-running sets the same values. Prints each step.
set -euo pipefail

repo="${1:-$(gh repo view --json nameWithOwner --jq .nameWithOwner)}"
branch="${BRANCH:-main}"
check="${REQUIRED_CHECK:-CI passed}"
echo "repository: $repo, protected branch: $branch, required check: $check"

echo "== merge settings: squash only, title and body from the pull request"
gh api -X PATCH "repos/$repo" \
  -F allow_squash_merge=true -F allow_merge_commit=false -F allow_rebase_merge=false \
  -F delete_branch_on_merge=true -F allow_auto_merge=false \
  -f squash_merge_commit_title=PR_TITLE -f squash_merge_commit_message=PR_BODY >/dev/null

echo "== branch protection on $branch"
gh api -X PUT "repos/$repo/branches/$branch/protection" --input - >/dev/null <<JSON
{
  "required_status_checks": {"strict": true, "contexts": ["$check"]},
  "enforce_admins": true,
  "required_pull_request_reviews": {
    "required_approving_review_count": ${REVIEWS:-0},
    "dismiss_stale_reviews": true,
    "require_code_owner_reviews": false
  },
  "restrictions": null,
  "required_linear_history": true,
  "allow_force_pushes": false,
  "allow_deletions": false,
  "required_conversation_resolution": true
}
JSON

echo "== private vulnerability reporting"
gh api -X PUT "repos/$repo/private-vulnerability-reporting" >/dev/null || \
  echo "   could not enable it here; enable it under Settings -> Code security"

echo "== release tags v*: never moved or deleted (administrators may bypass)"
existing="$(gh api "repos/$repo/rulesets" --jq '.[] | select(.name=="release tags") | .id' || true)"
method=POST; path="repos/$repo/rulesets"
if [ -n "$existing" ]; then method=PUT; path="repos/$repo/rulesets/$existing"; fi
gh api -X "$method" "$path" --input - >/dev/null <<'JSON'
{
  "name": "release tags",
  "target": "tag",
  "enforcement": "active",
  "conditions": {"ref_name": {"include": ["refs/tags/v*"], "exclude": []}},
  "rules": [{"type": "deletion"}, {"type": "non_fast_forward"}, {"type": "update"}],
  "bypass_actors": [{"actor_id": 5, "actor_type": "RepositoryRole", "bypass_mode": "always"}]
}
JSON

echo "done. Verify under Settings -> Branches / Rules / General."
