# GitHub Wave Orchestrator — Reference

This document contains the sub-agent templates, merge strategies, and recovery
procedures referenced by the orchestrator.

---

## Implementation Sub-agent Template

Use this template when spawning an implementation sub-agent for a single issue.

```
## Role
You are an implementation sub-agent for GitHub issue #{issue.number}: {issue.title}.

## Context
- Repository: {owner}/{repo}
- Base branch: main
- Worktree: {worktree_path}
- Branch name: fix/issue-{issue.number}-{slug}

## Your Task
1. Analyze the issue body and understand the required fix
2. Navigate to the worktree directory
3. Implement the fix following project conventions (see AGENTS.md)
4. Write tests if applicable
5. Push the branch and create a PR:
   - Title: "fix #{issue.number}: {issue.title}"
   - Body: Describe the fix, reference the issue
   - Link the PR to the issue (e.g., "Closes #{issue.number}")
6. Return the PR URL

## Implementation Guidelines
- Follow PEP 8 + type hints (see AGENTS.md §6)
- Use pathlib.Path over os.path
- Catch exceptions, log with exc_info=True, re-raise
- Do NOT break existing tests
- Run `make lint && make typecheck` before pushing

## Verification
Before creating the PR, verify:
- [ ] `make lint` passes
- [ ] `make typecheck` passes
- [ ] New tests pass (if applicable)
- [ ] No merge conflicts with main

## Output
Return a JSON object:
{{
  "pr_url": "https://github.com/{owner}/{repo}/pull/N",
  "pr_number": N,
  "branch": "fix/issue-{issue.number}-{slug}",
  "status": "success" | "partial" | "failed",
  "notes": "any caveats or follow-up items"
}}
```

---

## CI Sub-agent Template

Use this template when spawning a CI sub-agent for a single PR.

```
## Role
You are a CI sub-agent for PR #{pr_number}: {pr_title}.

## Context
- Repository: {owner}/{repo}
- PR number: {pr_number}
- PR URL: {pr_url}
- Branch: {branch}

## Your Task
1. Monitor CI status via `gh run list --pr {pr_number}`
2. If CI passes → merge the PR with `--squash --delete-branch`
3. If CI fails:
   a. Analyze the failure logs
   b. Fix the issue in the worktree
   c. Push the fix
   d. Wait for CI to re-run
4. If merge conflict → resolve via rebase onto main
5. After merge → clean up the worktree

## CI Fix Strategy
- Lint failures: Run `make format && make lint` and push
- Typecheck failures: Run `make typecheck` and fix type errors
- Test failures: Run tests locally, fix failing tests
- Timeout failures: Optimize test runtime or mark as acceptable

## Conflict Resolution
1. Fetch latest main: `git fetch origin main`
2. Rebase onto main: `git rebase origin/main`
3. Push force: `git push --force`
4. If rebase fails → resolve conflicts manually, then push

## Limits
- Max CI fix iterations: 10
- Max conflict resolution attempts: 2
- After limits reached → report as "escalated"

## Output
Return a JSON object:
{{
  "pr_number": N,
  "status": "merged" | "conflict_unresolved" | "escalated",
  "ci_attempts": N,
  "conflict_attempts": N,
  "notes": "any caveats"
}}
```

---

## Merge Ordering Strategy

### Rationale
PRs are merged in **ascending issue-number order** within a wave to minimize
the conflict surface. Lower-numbered issues are typically older and more
foundational; resolving them first establishes a stable base for later PRs.

### Procedure
1. Sort PRs by issue number (ascending)
2. For each PR in order:
   a. Check if PR is still open and mergeable
   b. If mergeable → merge with `--squash --delete-branch`
   c. After each merge → check remaining PRs for conflicts
   d. If conflict detected → attempt rebase resolution
3. If any PR cannot be merged after max attempts → escalate

### Conflict Detection
```bash
gh pr view {pr_number} --json mergeable
# MERGEABLE → safe to merge
# CONFLICTING → must resolve before merge
```

### Post-Merge Validation
After each merge:
1. Verify PR state is MERGED
2. Check remaining PRs for new conflicts
3. If new conflicts → resolve before proceeding

---

## Resume and Recovery

### State File Location
`../worktrees/wave-state.json` (parent of repo root)

### State File Schema
```json
{
  "version": 1,
  "status": "awaiting_confirmation" | "in_progress" | "wave_complete" | "all_complete",
  "total_issues": N,
  "total_waves": N,
  "current_wave": N,
  "waves": [
    {
      "wave": N,
      "issues": [N, ...],
      "prs": [N, ...],
      "merged": [N, ...]
    }
  ]
}
```

### Recovery Procedures

| Scenario | Detection | Recovery Action |
|---|---|---|
| Interrupted during PR creation | `gh pr list --search "fixes #N"` returns empty | Re-spawn implementation agent for that issue |
| Interrupted during CI | `gh pr view N --json statusCheckRollup` shows pending | Re-spawn CI agent to monitor |
| Interrupted during merge | `gh pr view N --json state` shows OPEN | Re-attempt merge |
| Worktree exists but is stale | Branch is behind main | Delete worktree, recreate, re-implement |
| State file corrupted | JSON parse fails | Start fresh from Phase 1 (Discovery) |
| Sub-agent is stuck | No PR after 5 minutes | Kill sub-agent, restart implementation |

### Resume Flow
```
1. Load wave-state.json
2. If status == "awaiting_confirmation" → present plan, wait for y/n
3. If status == "in_progress" → find current wave, resume from last incomplete phase
4. If status == "wave_complete" → proceed to next wave
5. If status == "all_complete" → print summary
```

### Phase Detection (for resume)
- **Phase 3a incomplete**: Worktree doesn't exist for an issue
- **Phase 3b incomplete**: PR doesn't exist for a worktree
- **Phase 4 incomplete**: PR exists but not merged

---

## Wave Planning Algorithm

### Input
JSON array of open issues (from `gh issue list --json`).

### Output
```json
{
  "total_issues": N,
  "total_waves": N,
  "waves": [
    {
      "wave": 1,
      "issues": [N, ...],
      "titles": ["...", ...]
    }
  ]
}
```

### Algorithm
1. **Filter**: Remove blocked/on-hold/assigned/PR-linked issues
2. **Analyze**: For each remaining issue, parse body for file paths mentioned
3. **Build dependency graph**: Issues that touch the same files are dependent
4. **Pack waves**: Greedy first-fit to pack independent issues into waves of max 3
5. **Output**: JSON plan

### File Dependency Detection
Scan issue body for patterns like:
- `osimflow/foo.py` → marks issue as touching `osimflow/foo.py`
- `bin/*.py` → marks issue as touching all files in `bin/`
- `tests/` → marks issue as touching `tests/` directory

### First-Fit Decreasing Algorithm
```
waves = []
for issue in sorted(issues_by_number):
  assigned = false
  for wave in waves:
    if wave has room AND wave issues are independent of issue:
      add issue to wave
      assigned = true
      break
  if not assigned:
    create new wave
    add issue to new wave
```

---

## Limits Reference

| Parameter | Value | Configurable via |
|---|---|---|
| Max issues per wave | 3 | `--max-issues-per-wave` |
| Max CI fix iterations per PR | 10 | `MAX_CI_FIX_ATTEMPTS` env |
| Max conflict resolution attempts | 2 | `MAX_CONFLICT_ATTEMPTS` env |
| Worktree location | `../worktrees/` | `WORKTREES_DIR` in orchestrator |
| PR creation polling attempts | 30 × 10s = 5min | hardcoded |
| CI polling interval | 30s | hardcoded |

---

## Sub-agent Result Schema

### Implementation Agent Result
```json
{
  "pr_url": "https://github.com/anchapin/OSimFlow/pull/N",
  "pr_number": N,
  "branch": "fix/issue-N-slug",
  "status": "success",
  "notes": "optional caveats"
}
```

### CI Agent Result
```json
{
  "pr_number": N,
  "status": "merged",
  "ci_attempts": 3,
  "conflict_attempts": 0,
  "notes": "merged successfully"
}
```

### Wave Result
```json
{
  "wave": 1,
  "prs": 3,
  "merged": 3,
  "escalated": 0,
  "results": [/* CI agent results */]
}
```

---

## Complete Workflow State Machine

```
                    ┌─────────────────────┐
                    │     START           │
                    └─────────┬───────────┘
                              ▼
                    ┌─────────────────────┐
         ┌──────────│  PHASE 0: PREFLIGHT │──────────┐
         │          └─────────────────────┘          │
         │ FAIL                                      │ PASS
         ▼                                            ▼
    ┌─────────┐                            ┌─────────────────────┐
    │  ERROR  │                            │  PHASE 1: DISCOVERY │
    └─────────┘                            └──────────┬──────────┘
                                                     ▼
                                           ┌─────────────────────┐
                              ┌────────────│  PHASE 2: PLANNING  │
                              │            └──────────┬──────────┘
                              │                       ▼
                              │            ┌─────────────────────┐
                              │            │ PRESENT WAVE PLAN   │
                              │            │ (awaiting confirm)  │
                              │            └──────────┬──────────┘
                              │                       │ y/n
                              │           ┌───────────┴───────────┐
                              │           ▼                       ▼
                              │    ┌──────────┐           ┌──────────┐
                              │    │   STOP   │           │ EXECUTE  │
                              │    └──────────┘           │  WAVES   │
                              │                           └────┬─────┘
                              │                                │
                              │            ┌───────────────────┴────────┐
                              │            ▼                               ▼
                              │   ┌──────────────────┐          ┌──────────────────┐
                              │   │ WAVE COMPLETE    │          │  NEXT WAVE       │
                              │   │ (all merged)     │          │  (if exists)     │
                              │   └────────┬─────────┘          └────────┬─────────┘
                              │            │                               │
                              │            └───────────────┬───────────────┘
                              │                            ▼
                              │                   ┌──────────────────┐
                              │                   │   ALL COMPLETE   │
                              │                   │   (print summary)│
                              │                   └──────────────────┘
                              │
                              └───────────────────► (resume from state)
```
