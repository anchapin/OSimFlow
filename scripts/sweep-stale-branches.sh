#!/usr/bin/env bash
# =============================================================================
# scripts/sweep-stale-branches.sh — safely identify (and optionally delete)
# stale remote branches on GitHub. Resolves issue #1003.
# =============================================================================
#
# USAGE
# -----
#   scripts/sweep-stale-branches.sh                 # dry-run (list only)
#   scripts/sweep-stale-branches.sh --apply         # delete proven-merged branches
#   scripts/sweep-stale-branches.sh --include-orphaned --apply   # also delete abandoned (REVIEW FIRST)
#   scripts/sweep-stale-branches.sh --report logs/sweep.txt
#   scripts/sweep-stale-branches.sh --min-age-days 60
#   scripts/sweep-stale-branches.sh --worktree               # also scan stale local worktrees
#   scripts/sweep-stale-branches.sh --worktree --apply       # remove stale worktrees too
#   scripts/sweep-stale-branches.sh --help          # show this header
#
# DEFAULT BEHAVIOUR
# -----------------
# The script is DRY-RUN by default: it prints the branches it *would* delete
# and exits without touching the remote. Pass --apply to perform the deletion.
# Two tiers of deletable branches:
#
#   1. PROVEN-MERGED  (default --apply scope)
#      A branch is "proven merged" when EITHER:
#        (a) its tip is an ancestor of the default branch (git merge-base
#            --is-ancestor) — catches true merges & rebases; OR
#        (b) it was the head of a merged GitHub PR (merged_at != null via
#            the REST /pulls list). This catches SQUASH-MERGED branches whose
#            commits never become ancestors of the default branch.
#      AND it has NO open PR, AND it is older than --min-age-days, AND it is
#      not on the keep-list below.
#
#   2. ABANDONED (unproven, opt-in only — pass --include-orphaned)
#      A branch that is old, has no open PR, is not protected, but is NOT
#      proven-merged by either signal (its work was done on a different
#      branch or the branch was abandoned). These are listed for HUMAN
#      REVIEW and are NEVER auto-deleted unless --include-orphaned is passed
#      together with --apply.
#
# KEEP-LIST (never deleted by this script)
# ----------------------------------------
#   main, master, develop                         (exact)
#   release/*, hotfix/*, wave*, releases/*      (glob)
#   Any branch matching --protect-glob (repeatable; default: as above)
#
# IDEMPOTENCY
# -----------
# Running --apply twice is safe: a branch deleted on the first run simply
# does not appear in the second run's inventory (git fetch --prune drops it).
# The script never errors on a branch that no longer exists; it skips it.
#
# ENVIRONMENT
# -----------
#   REPO  — GitHub repo slug            (default: anchapin/OSimFlow)
#   DEFAULT_BRANCH — branch to test ancestry against (default: main)
# Requires `git` and an authenticated `gh` CLI (`gh auth status`).
#
# REPORTING
# ---------
# The dry-run report is printed to stdout. Pass --report <file> to also write
# a timestamped copy. Deleted branches are echoed to stdout under "DELETED:".
# =============================================================================

set -euo pipefail

# --- defaults ---------------------------------------------------------------
REPO="${REPO:-anchapin/OSImFlow}"
DEFAULT_BRANCH="${DEFAULT_BRANCH:-main}"
MIN_AGE_DAYS="${MIN_AGE_DAYS:-30}"
DRY_RUN="true"          # dry-run is the safe default
INCLUDE_ORPHANED="false"
DO_APPLY="false"
SCAN_WORKTREES="false"  # also scan local git worktrees
REPORT_FILE=""
# Keep-list globs. Matched against the bare branch name (no origin/ prefix).
PROTECT_GLOBS=(
    'main'
    'master'
    'develop'
    'release/*'
    'hotfix/*'
    'wave*'
    'releases/*'
)

# --- arg parsing ------------------------------------------------------------
usage() {
    awk 'NR==1{next} /^[^#]/{exit} {sub(/^# ?/,""); print}' "$0"
    exit "${1:-0}"
}

ADDL_PROTECT=()
while [[ $# -gt 0 ]]; do
    case "$1" in
        --apply)                 DO_APPLY="true"; DRY_RUN="false"; shift ;;
        --dry-run)               DRY_RUN="true"; shift ;;
        --include-orphaned)      INCLUDE_ORPHANED="true"; shift ;;
        --min-age-days)          MIN_AGE_DAYS="${2:-30}"; shift 2 ;;
        --report)                REPORT_FILE="${2:?--report requires a path}"; shift 2 ;;
        --protect-glob)          ADDL_PROTECT+=("$2"); shift 2 ;;
        --repo)                  REPO="${2:?--repo requires a value}"; shift 2 ;;
        --default-branch)        DEFAULT_BRANCH="${2:?--default-branch requires a value}"; shift 2 ;;
        --worktree)              SCAN_WORKTREES="true"; shift ;;
        --help|-h)               usage 0 ;;
        *) echo "Unknown argument: $1" >&2; usage 64 ;;
    esac
done

# --- preflight --------------------------------------------------------------
command -v git >/dev/null || { echo "ERROR: git not found" >&2; exit 69; }
if ! gh auth status >/dev/null 2>&1; then
    echo "ERROR: gh CLI not authenticated. Run 'gh auth login'." >&2
    exit 69
fi

echo "repo=${REPO} default_branch=${DEFAULT_BRANCH} min_age_days=${MIN_AGE_DAYS} dry_run=${DRY_RUN} include_orphaned=${INCLUDE_ORPHANED} scan_worktrees=${SCAN_WORKTREES}"

# --- gather inputs ----------------------------------------------------------
# Keep the local view of the remote fresh and drop deleted refs.
git fetch origin --prune --force >/dev/null

# All current remote branches (bare names, no 'origin/' prefix, no HEAD).
mapfile -t ALL_BRANCHES < <(
    git for-each-ref refs/remotes/origin/ --format='%(refname:short)' \
        | sed 's#^origin/##' \
        | grep -v '^HEAD$' \
        || true
)
ALL_BRANCHES=("${ALL_BRANCHES[@]}")
total_branches=${#ALL_BRANCHES[@]}

# Open PR head refs: one authenticated, paginated request.
mapfile -t OPEN_HEADS < <(
    gh api "repos/${REPO}/pulls?state=open" --paginate --jq '.head.ref' 2>/dev/null || true
)
declare -A open_heads=()
for h in "${OPEN_HEADS[@]}"; do
    [[ -n "$h" ]] && open_heads["$h"]=1
done

# Merged PR head refs (squash merges leave no ancestry trail, so we rely on
# merged_at != null from the REST /pulls list). Paginated, single auth flow.
mapfile -t MERGED_HEADS < <(
    gh api "repos/${REPO}/pulls?state=closed" --paginate \
        --jq '.[] | select(.merged_at != null) | .head.ref' 2>/dev/null || true
)
declare -A merged_heads=()
for h in "${MERGED_HEADS[@]}"; do
    [[ -n "$h" ]] && merged_heads["$h"]=1
done

# Merge the user's --protect-glob extras into the keep-list.
PROTECT_GLOBS+=("${ADDL_PROTECT[@]}")

is_protected() {
    local name="$1" g
    for g in "${PROTECT_GLOBS[@]}"; do
        # shellcheck disable=SC2053  # intentional glob match against a bare name
        if [[ "$name" == $g ]]; then
            return 0
        fi
    done
    return 1
}

ancestry_merged() {  # is origin/<1> an ancestor of origin/<DEFAULT_BRANCH>?
    git merge-base --is-ancestor "origin/$1" "origin/${DEFAULT_BRANCH}" 2>/dev/null
}

branch_age_days() {  # last commit age of origin/<1> in whole days
    local epoch
    epoch=$(git log -1 --format='%ct' "origin/$1" 2>/dev/null) || return 0
    # age = now - commit; integer division by 86400
    awk -v now="$(date +%s)" -v c="$epoch" 'BEGIN{printf "%d", (now - c)/86400}'
}

# --- classify ---------------------------------------------------------------
declare -a safe=()          # proven-merged + old + no-open-PR + protected
declare -a safe_orphaned=() # abandoned/unproven but old + no-open-PR + protected
protected_count=0

for b in "${ALL_BRANCHES[@]}"; do
    if is_protected "$b"; then
        protected_count=$((protected_count + 1))
        continue
    fi

    local_anc=0
    if ancestry_merged "$b"; then
        local_anc=1
    fi
    pr_merged=0
    if [[ -n "${merged_heads[$b]:-}" ]]; then
        pr_merged=1
    fi
    open_pr=0
    if [[ -n "${open_heads[$b]:-}" ]]; then
        open_pr=1
    fi

    # Criterion: no open PR references the branch head.
    [[ "$open_pr" -ne 0 ]] && continue

    age=$(branch_age_days "$b")
    # Criterion: last commit older than the minimum age.
    [[ "$age" -lt "$MIN_AGE_DAYS" ]] && continue

    if [[ "$local_anc" -eq 1 || "$pr_merged" -eq 1 ]]; then
        # PROVEN-MERGED: the branch's work lives in the default branch.
        # Safe to delete automatically.
        safe+=("$b")
    else
        # ABANDONED / unproven: old, no open PR, but not provably merged.
        # Listed for human review; only deleted with --include-orphaned --apply.
        safe_orphaned+=("$b")
    fi
done

# --- emit report ------------------------------------------------------------
emit() {
    if [[ -n "$REPORT_FILE" ]]; then
        printf '%s\n' "$*" >>"$REPORT_FILE"
    fi
    printf '%s\n' "$*"
}

if [[ -n "$REPORT_FILE" ]]; then
    : >"$REPORT_FILE"
    emit "# OSimFlow stale-branch sweep report"
    emit "# generated: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
    emit "# repo: ${REPO}  default_branch: ${DEFAULT_BRANCH}  min_age_days: ${MIN_AGE_DAYS}"
    emit "# dry_run: ${DRY_RUN}  include_orphaned: ${INCLUDE_ORPHANED}"
    emit ""
fi

emit "=== INVENTORY ==="
emit "total remote branches:        ${total_branches}"
emit "protected (keep-list) kept:   ${protected_count}"
emit "proven-merged (safe to delete): ${#safe[@]}"
emit "abandoned (review-only):      ${#safe_orphaned[@]}"
emit ""
emit "=== PROVEN-MERGED — safe to delete (${#safe[@]}) ==="
if ((${#safe[@]} == 0)); then
    emit "(none)"
else
    printf '%s\n' "${safe[@]}" | sort | while IFS= read -r line; do emit "  $line"; done
fi
emit ""
emit "=== ABANDONED — requires manual review (NOT auto-deleted) ==="
if ((${#safe_orphaned[@]} == 0)); then
    emit "(none)"
else
    emit "These branches are old and have no open PR, but are not proven"
    emit "merged into ${DEFAULT_BRANCH}. Review each before deleting. To"
    emit "include them, pass --include-orphaned --apply."
    printf '%s\n' "${safe_orphaned[@]}" | sort | while IFS= read -r line; do emit "  $line"; done
fi
emit ""
emit "=== END ==="

# --- stale local worktrees (opt-in: --worktree) -----------------------------
# Detects local git worktrees whose branch is stale (old + no open PR + not
# protected). A worktree's branch is classified using the SAME criteria as
# the remote-branch sweep above, so a superseded/abandoned branch (e.g.
# fix/issue-919 — work was moved to PR #999 on a different branch) is also
# detected. Removes stale worktrees with `git worktree remove --force` +
# `git worktree prune`. This is the programmatic analogue of the manual
# cleanup in issue #1002.
if [[ "$SCAN_WORKTREES" == "true" ]]; then
    # The main checkout (cwd) is never a "stale" worktree.
    main_abspath="$(git rev-parse --show-toplevel 2>/dev/null || true)"

    # Build lookup sets from the already-classified branch arrays.
    declare -A stale_branch_set=()
    for b in "${safe[@]}" "${safe_orphaned[@]}"; do
        stale_branch_set["$b"]=1
    done

    declare -a stale_worktrees=()
    declare -a stale_worktree_branches=()
    declare -a stale_worktree_orphaned=()
    # Parse `git worktree list --porcelain` — each worktree is a block
    # separated by a blank line:
    #   worktree <path>
    #   HEAD <sha>
    #   branch <ref>   (omitted for detached HEAD)
    # We accumulate path+branch per block, then evaluate on blank line / EOF.
    wt_path="" wt_branch=""
    process_worktree() {
        [[ -z "${wt_path:-}" ]] && return
        [[ "$wt_path" == "$main_abspath" ]] && return
        [[ -z "${wt_branch:-}" ]] && return
        if [[ -n "${stale_branch_set[$wt_branch]:-}" ]]; then
            age=$(branch_age_days "$wt_branch" 2>/dev/null || echo 0)
            if [[ "$age" -ge "$MIN_AGE_DAYS" ]]; then
                stale_worktrees+=("$wt_path")
                stale_worktree_branches+=("$wt_branch")
                # Track which ones are abandoned (not proven-merged).
                if ancestry_merged "$wt_branch" || [[ -n "${merged_heads[$wt_branch]:-}" ]]; then
                    : # proven-merged, safe to remove with just --worktree
                else
                    stale_worktree_orphaned+=("$wt_branch")
                fi
            fi
        fi
    }
    while IFS= read -r line; do
        case "$line" in
            '')
                process_worktree
                wt_path=""; wt_branch=""
                ;;
            worktree\ *) wt_path="${line#worktree }" ;;
            branch\ *)   wt_branch="${line#branch }"; wt_branch="${wt_branch#refs/heads/}" ;;
            *)           ;;
        esac
    done < <(git worktree list --porcelain 2>/dev/null)
    # Flush the last block (no trailing blank line guaranteed).
    process_worktree

    emit ""
    emit "=== STALE LOCAL WORKTREES (${#stale_worktrees[@]}) ==="
    if ((${#stale_worktrees[@]} == 0)); then
        emit "(none)"
    else
        for i in "${!stale_worktrees[@]}"; do
            emit "  ${stale_worktrees[$i]} (${stale_worktree_branches[$i]})"
        done
        emit "Use '--worktree --apply' to remove proven-merged worktrees."
        emit "Use '--worktree --include-orphaned --apply' to also remove abandoned worktrees."
    fi
    emit "=== END WORKTREES ==="

    # Act: remove stale worktrees. Proven-merged always; abandoned only with
    # --include-orphaned (mirrors the branch deletion semantics above).
    if [[ "$DO_APPLY" == "true" && ${#stale_worktrees[@]} -gt 0 ]]; then
        emit "REMOVING ${#stale_worktrees[@]} worktree(s):"
        for i in "${!stale_worktrees[@]}"; do
            wt_path="${stale_worktrees[$i]}"
            wt_branch="${stale_worktree_branches[$i]}"
            # Skip abandoned worktrees unless --include-orphaned is set.
            is_orphaned=false
            for ob in "${stale_worktree_orphaned[@]}"; do
                if [[ "$ob" == "$wt_branch" ]]; then is_orphaned=true; break; fi
            done
            if [[ "$is_orphaned" == "true" && "$INCLUDE_ORPHANED" == "false" ]]; then
                emit "SKIP (abandoned, needs --include-orphaned): $wt_path"
                continue
            fi
            if git worktree remove "$wt_path" --force >/dev/null 2>&1; then
                emit "REMOVED WORKTREE: $wt_path"
            else
                emit "FAILED (busy?): $wt_path"
            fi
        done
        git worktree prune >/dev/null 2>&1 || true
    fi
fi

# --- act (delete) -----------------------------------------------------------
if [[ "$DO_APPLY" == "false" ]]; then
    exit 0
fi

# --apply: delete only proven-merged branches (and orphaned if opted in).
delete_set=("${safe[@]}")
if [[ "$INCLUDE_ORPHANED" == "true" ]]; then
    delete_set+=("${safe_orphaned[@]}")
fi

if ((${#delete_set[@]} == 0)); then
    emit "no branches to delete."
    exit 0
fi

emit "DELETING ${#delete_set[@]} branch(es):"
for b in "${delete_set[@]}"; do
    if git push origin --delete "$b" >/dev/null 2>&1; then
        emit "DELETED: $b"
        git fetch origin --prune >/dev/null 2>&1 || true
    else
        emit "FAILED (not found?): $b"
    fi
done
