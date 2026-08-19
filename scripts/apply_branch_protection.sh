#!/usr/bin/env bash
# =============================================================================
# scripts/apply_branch_protection.sh — settings-as-code applier for the
# GitHub branch protection rules on `main` (issue #975).
# =============================================================================
#
# USAGE
# -----
#   scripts/apply_branch_protection.sh             # apply to remote
#   scripts/apply_branch_protection.sh --dry-run   # print what would run
#   scripts/apply_branch_protection.sh --help      # show this header
#
# ENVIRONMENT
# -----------
#   REPO      — GitHub repo slug (default: anchapin/OSimFlow)
#   BRANCH    — branch to protect    (default: main)
#
# REQUIRED STATUS CHECKS
# ---------------------
# These strings MUST match the `name:` keys in `.github/workflows/ci.yml`
# verbatim. A mismatch silently does nothing on the GitHub side because the
# required-check lookup is by exact display-name. The five names are:
#
#     - lint (ruff)
#     - typecheck (mypy --strict)
#     - test (pytest, 85% coverage gate)
#     - agents & docs contract
#     - security (pip-audit)
#
# SETTINGS APPLIED
# ----------------
#   required_status_checks.strict          = false
#   required_status_checks.contexts        = (5 names above)
#   required_linear_history.enabled        = true
#   required_pull_request_reviews          = null    (NOT required)
#   restrictions                           = null
#   required_conversation_resolution       = null
#   allow_force_pushes.enabled             = false
#   allow_deletions.enabled                = false
#   allow_fork_syncing.enabled             = false
#   block_creations                        = false
#   required_signatures                    = null
#   lock_branch                            = false
#
# See docs/branch-protection.md for the rationale behind each setting and the
# review/squash-merge decisions the orchestrator intentionally skipped.
#
# IDEMPOTENCY
# -----------
# The PUT to /branches/<branch>/protection overwrites the existing protection
# state wholesale, so the API call itself is idempotent. As an optimization,
# this script first GETs the current state and, if it already matches the
# desired payload on the fields a human would care about (check list + linear
# history), it prints "Already in desired state" and exits 0 without making
# the PUT. `jq` is used for the comparison; if `jq` is missing the script
# falls back to making the PUT unconditionally.
#
# AUTHENTICATION
# --------------
# The script uses the host's `gh` CLI for auth. Run `gh auth status` first.
# The token must have admin:repo scope on the target repository. Long-lived
# PATs are not committed to this repo — see AGENTS.md §10.
# =============================================================================

set -euo pipefail

REPO="${REPO:-anchapin/OSimFlow}"
BRANCH="${BRANCH:-main}"
DRY_RUN="false"

usage() {
    # Print the header comment block (lines starting with '#') as help text.
    # The header ends at the first blank line; we strip the leading "# " or
    # lone "#" but keep the rest of each line.
    awk '
        NR == 1 { next }
        /^[^#]/ { exit }
        { sub(/^# ?/, ""); print }
    ' "$0"
    exit "${1:-0}"
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run)
            DRY_RUN="true"
            shift
            ;;
        --help|-h)
            usage 0
            ;;
        *)
            echo "Unknown argument: $1" >&2
            echo "Run with --help for usage." >&2
            exit 64
            ;;
    esac
done

# Required status check contexts — MUST match ci.yml job `name:` fields verbatim.
# If you rename a job in .github/workflows/ci.yml, update this list in the same PR.
REQUIRED_CHECKS='[
    "lint (ruff)",
    "typecheck (mypy --strict)",
    "test (pytest, 85% coverage gate)",
    "agents & docs contract",
    "security (pip-audit)"
]'

# Build the JSON payload via heredoc into a tmpfile. Using a tmpfile (rather
# than --input -) avoids pipe-subshell quoting hazards on systems where the
# inline form is brittle.
TMP="$(mktemp -t osimflow-branch-protection.XXXXXX.json)"
# shellcheck disable=SC2064  # we want $TMP captured now, not at trap-exit time
trap "rm -f '$TMP'" EXIT

cat >"$TMP" <<JSON
{
    "required_status_checks": {
        "strict": false,
        "contexts": ${REQUIRED_CHECKS}
    },
    "required_linear_history": {
        "enabled": true
    },
    "required_pull_request_reviews": null,
    "restrictions": null,
    "required_conversation_resolution": null,
    "allow_force_pushes": {
        "enabled": false
    },
    "allow_deletions": {
        "enabled": false
    },
    "allow_fork_syncing": {
        "enabled": false
    },
    "block_creations": false,
    "required_signatures": null,
    "lock_branch": false
}
JSON

if [[ "$DRY_RUN" == "true" ]]; then
    echo "DRY RUN — would execute the following command:"
    echo
    echo "  gh api --method PUT \\"
    echo "      -H 'Accept: application/vnd.github+json' \\"
    echo "      /repos/${REPO}/branches/${BRANCH}/protection \\"
    echo "      --input ${TMP}"
    echo
    echo "Payload:"
    cat "$TMP"
    echo
    exit 0
fi

if ! command -v gh >/dev/null 2>&1; then
    echo "ERROR: 'gh' is not on PATH. Install it from https://cli.github.com/ and run 'gh auth status' first." >&2
    exit 127
fi

# Idempotency check (best-effort — falls through to PUT if jq is absent or the
# comparison fails). Compare only the fields that a human reviewer would care
# about: the required-check context list and the linear-history flag.
if command -v jq >/dev/null 2>&1; then
    CURRENT="$(gh api "/repos/${REPO}/branches/${BRANCH}/protection" 2>/dev/null || true)"
    if [[ -n "$CURRENT" ]]; then
        if echo "$CURRENT" | jq -e --argjson desired "$(cat "$TMP")" \
                '(.required_status_checks.contexts | sort) == ($desired.required_status_checks.contexts | sort)
                 and .required_linear_history.enabled == $desired.required_linear_history.enabled
                 and (.required_pull_request_reviews // null) == ($desired.required_pull_request_reviews // null)' \
                >/dev/null 2>&1; then
            echo "Already in desired state (no API call made)."
            exit 0
        fi
    fi
fi

gh api \
    --method PUT \
    -H 'Accept: application/vnd.github+json' \
    "/repos/${REPO}/branches/${BRANCH}/protection" \
    --input "$TMP" \
    >/dev/null

# Re-fetch and emit a one-line summary so the caller can eyeball the result.
NEW="$(gh api "/repos/${REPO}/branches/${BRANCH}/protection")"
CONTEXTS="$(echo "$NEW" | jq -r '.required_status_checks.contexts | join(", ")')"
LINEAR="$(echo "$NEW" | jq -r '.required_linear_history.enabled')"
REVIEWS="$(echo "$NEW" | jq -r 'if .required_pull_request_reviews == null then "disabled" else "enabled" end')"
echo "Branch protection updated for ${REPO}@${BRANCH}:"
echo "  required checks       : ${CONTEXTS}"
echo "  linear history        : ${LINEAR}"
echo "  pull request reviews  : ${REVIEWS}"