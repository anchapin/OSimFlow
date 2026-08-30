# Branch protection

> Settings-as-code for the GitHub branch protection rules on `main`. Resolves
> issue **#975** ("[infra][high] Enable branch protection rules requiring CI
> checks before merge"), which was filed after PR #969 merged to `main` with a
> failing `test (pytest, 85% coverage gate)` check because no protection rules
> were configured to gate merges on CI status.

---

## What is enabled

The settings applied to `anchapin/OSimFlow@main` by
[`scripts/apply_branch_protection.sh`](../scripts/apply_branch_protection.sh):

| Setting | Value | Why |
|---|---|---|
| `required_status_checks.contexts` | 5 checks (see below) | Gate merges on CI; prevents the #969 failure mode from recurring. |
| `required_status_checks.strict` | `false` | Don't require the PR branch to be up-to-date with `main` — keeps wave-style automation unblocked; can be tightened manually later. |
| `required_linear_history` | `true` | Linear history keeps the graph readable and `git bisect` cheap; the project already squash-merges. (Plain boolean per the GitHub API.) |
| `required_pull_request_reviews` | `null` (not required) | **Intentional:** requiring reviews would deadlock single-user automation. See *What's NOT enabled* below. |
| `restrictions` | `null` | No push restrictions — by default GitHub allows admins (including `gh`'s admin:repo token) to bypass. |
| `required_conversation_resolution` | `null` | Off — bot-driven PRs generate a high volume of stale conversation threads. |
| `allow_force_pushes` | `false` | Forbid force pushes (the project doesn't use them in the wave workflow). (Plain boolean per the GitHub API.) |
| `allow_deletions` | `false` | Forbid branch deletion — protects historical bisect targets. (Plain boolean per the GitHub API.) |
| `allow_fork_syncing` | `false` | Forbid fork-syncing — irrelevant for this repo (no forks consume it as an upstream). (Plain boolean per the GitHub API.) |
| `block_creations` | `false` | Allow new branch creation from `main`. |
| `required_signatures` | `false` | Off — signing is not part of the project's contribution flow yet. |
| `lock_branch` | `false` | Branch is open. |
| `enforce_admins` | `false` | Admins (including `gh`'s admin:repo token) bypass the rules. Required field per the GitHub API; kept `false` so admin-driven automation stays unblocked. |

### Required status checks (must match `ci.yml` verbatim)

These five names come from the `name:` keys of the corresponding jobs in
[`.github/workflows/ci.yml`](../.github/workflows/ci.yml). A mismatch silently
does nothing on the GitHub side — GitHub looks up required checks by exact
display-name.

1. **`lint (ruff)`** — `lint` job
2. **`typecheck (mypy --strict)`** — `typecheck` job
3. **`test (pytest, 82% coverage gate)`** — `test` job
4. **`agents & docs contract`** — `contract` job
5. **`security (pip-audit)`** — `security` job

> ⚠️ If you rename a CI job in `.github/workflows/ci.yml`, update the
> `REQUIRED_CHECKS` list inside `scripts/apply_branch_protection.sh` in the
> *same* PR or protection will silently start allowing merges past the renamed
> job.

---

## What is intentionally NOT enabled

### No required approving reviews (`required_pull_request_reviews = null`)

The orchestrator explicitly chose not to enable this. The same failure mode
that produced this issue (#969 merging with a failing CI gate, plus the
precedent of single-user automation deadlocks documented in the wave-orchestrator
archive) applies to review requirements: with a single maintainer, requiring
N>0 approvals becomes a hard merge-blocker that the orchestrator cannot
resolve itself. The branch protection gate alone catches the *automatable*
class of failures (test regressions, lint breakage, type errors, security
vulns, contract drift) — the class of failures the project has actually hit
in production.

If human reviews are needed for a particular PR, the maintainer reviews in
the GitHub UI as usual; this is **not** enforced by the protection rule. The
audit log will show who merged what. If your environment has multiple
maintainers and wants review gating, change the payload's
`required_pull_request_reviews` from `null` to e.g. `{"required_approving_review_count": 1}` and re-run the script.

### No "branches must be up to date" requirement (`strict = false`)

`required_status_checks.strict = true` would require the PR head to be a
strict descendant of `main` before the required checks would gate the merge.
The orchestrator's wave workflow rebases PRs but occasionally lands PRs that
depend on later merges; `strict = true` would block those for no real safety
benefit (the required checks themselves are content-equal regardless of base).
If a future need emerges, change `strict` to `true` in the payload.

### No required signed commits

The project does not currently require signed commits. This is a deliberate
choice — enabling it would create a one-time contributor-side burden with no
clear benefit until release-signing is built out. See issue #985 (planned) for
the rollout plan.

---

## How to verify the live settings

After the orchestrator has run the script, sanity-check what GitHub
currently shows for the branch:

```bash
gh api repos/anchapin/OSimFlow/branches/main/protection
```

This returns the full protection payload. Confirm:

- `required_status_checks.contexts` is the 5-element list above.
- `required_linear_history` is `true`.
- `required_pull_request_reviews` is `null`.
- `allow_force_pushes` / `allow_deletions` /
  `allow_fork_syncing` are all `false`.
- `enforce_admins` is `false`.

You can also read the same payload through the GitHub UI under
**Settings → Branches → Branch protection rules → `main`**.

---

## How to re-apply or extend

Re-running the script is safe — the PUT is idempotent at the API level, and
the script additionally does a `GET`/`compare` first and short-circuits with
`Already in desired state` if the live settings already match the payload.

```bash
scripts/apply_branch_protection.sh             # apply (or no-op if already applied)
scripts/apply_branch_protection.sh --dry-run   # print what would run without applying
```

To add or remove a required check:

1. Edit `REQUIRED_CHECKS` inside `scripts/apply_branch_protection.sh`
   (or add the matching job to `.github/workflows/ci.yml` first, then mirror
   the new name here).
2. Run `scripts/apply_branch_protection.sh` (or `--dry-run` first to inspect).
3. Commit the change in the same PR that touched `ci.yml`.
4. **After the PR merges, re-run `scripts/apply_branch_protection.sh` against
   the live repo.** The script is settings-as-code (issue #975); the script
   edit alone does not update the live GitHub branch protection — that only
   happens when the script is re-executed. Without this step the live
   `required_status_checks.contexts` silently drifts from the script's
   `REQUIRED_CHECKS` array, and any renamed check starts blocking PRs as a
   "never passing" gate (issue #1056 — the same failure mode that PR #969
   exhibited before #975 added the protection rules in the first place).

To enable review gating (e.g. when the maintainer count grows past one):

1. Change `required_pull_request_reviews` from `null` to an object, e.g.
   `{"dismiss_stale_reviews": true, "require_code_owner_reviews": false, "required_approving_review_count": 1}`.
2. Re-run the script.

---

## How to roll back (disable protection entirely)

To remove branch protection on `main` (e.g. for emergency hotfix workflows
that need to bypass CI temporarily):

```bash
gh api --method DELETE repos/anchapin/OSimFlow/branches/main/protection
```

This **deletes** the protection rule outright. After the call, GitHub returns
HTTP 204. The branch returns to "anyone with push access can push, no checks
required." To re-enable, re-run `scripts/apply_branch_protection.sh`.

> ⚠️ Deleting protection is the documented failure mode from PR #969 / #973 —
> it should be a deliberate, time-boxed choice (re-enable as soon as the
> hotfix lands), not a "set and forget" state.

---

## Operational notes

- **Token scope.** The host's `gh` token needs `admin:repo` on
  `anchapin/OSimFlow`. `gh auth status` confirms scopes; this repo never
  checks a long-lived PAT into source (AGENTS.md §10).
- **GitHub API version.** The script uses the default `Accept:
  application/vnd.github+json` header, which resolves to the v3 REST API. No
  GraphQL; no preview headers.
- **What the script does *not* do.** It does not touch repository-level
  settings (merge button visibility, default branch, etc.) — only the
  branch-protection resource. Those live in a separate
  `/repos/{owner}/{repo}` payload and are out of scope for issue #975.
- **Why a script instead of a GitHub App / Terraform provider.** The repo
  does not currently run a Terraform CI pipeline against the `main` branch's
  protection resource; using a single shell script keeps the contract auditable
  in one place and lets the orchestrator run it as a one-shot `workflow_dispatch`
  job without dragging in a new provider dependency.

---

## Stale branch sweep

> Resolves issue **#1003** ("[infra] Sweep stale remote branches"). Tracks ~270+
> stale `fix/*` and `feature/*` branches left on `origin` after PR merge, most
> of them squash-merged (so they are *not* git-ancestors of `main`).

[`scripts/sweep-stale-branches.sh`](../scripts/sweep-stale-branches.sh)
lists branches that are safe to delete and `--apply` deletes them. It is
**dry-run by default**; the scheduled cleanup workflow never deletes.

### Deletion criteria (all must hold)

1. **Proven merged** — the branch tip is an ancestor of `main` (`git merge-base
   --is-ancestor`), **or** the branch was the head of a GitHub PR with
   `merged_at != null` (catches squash merges whose commits never enter
   `main`'s ancestry). The second signal is required because this repo
   squash-merges, so the ancestry check alone misses most stale branches.
2. **No open PR** references the branch (one `gh api …/pulls?state=open` pass).
3. **Last commit older than `--min-age-days`** (default `30`).
4. **Not on the keep-list**: `main`, `master`, `develop`, `release/*`,
   `hotfix/*`, `wave*`, `releases/*`, or anything added via `--protect-glob`.

### Two tiers

- **Proven-merged** branches (criterion 1 holds) are safe to delete
  automatically with `sweep-stale-branches.sh --apply`.
- **Abandoned / unproven** branches (old, no open PR, but *not* proven
  merged) are printed under a separate `ABANDONED — requires manual review`
   heading. They are **never** auto-deleted; pass `--include-orphaned --apply`
   to opt into deleting them after human review.

### Local worktree cleanup (`--worktree`)

The `--worktree` flag extends the sweep to stale local git worktrees (e.g.
those created by `git worktree add` for in-progress fixes). When enabled, the
script reuses the same proven-merged / abandoned classification to identify
worktrees that are safe to remove:

- **Proven-merged** worktrees are listed under the same dry-run summary;
  pass `--worktree --apply` to remove them.
- Use `--worktree --include-orphaned --apply` to also remove abandoned
  worktrees after human review (same semantics as branch deletion).
- Proven-merged worktrees are safe to remove without `--include-orphaned`
  because the underlying branch has already been merged and deleted upstream.

### Idempotency

Deleting a branch makes it disappear from the next `git fetch --prune`, so
re-running `--apply` is a no-op. The script skips branches that no longer exist
rather than erroring.

### Automation

[`.github/workflows/branch-cleanup.yml`](../.github/workflows/branch-cleanup.yml)
runs the sweep **dry-run** nightly at 06:00 UTC, uploads the report as an
artifact, and posts a summary comment to tracking issue #1003. To actually
delete, an operator triggers `workflow_dispatch` with `apply=true` **and**
`confirm=DELETE` (the dual-input guard blocks accidental deletion).

---

## Related

- Issue **#975** — Enable branch protection rules requiring CI checks before merge
- PR **#969** — The merge that slipped through (test failure)
- PR **#973** — The hotfix that unblocked Wave 4
- `.github/workflows/ci.yml` — The CI workflow that the 5 required checks gate
- `tools/check_agents_contract.py` — Enforces that new public symbols and
  scripts are reflected in `AGENTS.md` (the drift gate that this file's
  `docs/branch-protection.md` reference participates in).