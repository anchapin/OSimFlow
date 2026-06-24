# GitHub Wave Orchestrator

**Skill Category:** Workflow Orchestration
**Project:** OSimFlow (this repository)
**Author:** Wave Orchestrator Team
**Version:** 1.0.0

Resolves all open GitHub issues via parallel execution waves. Each wave groups
independent issues (no shared files), spawns sub-agents in isolated worktrees,
monitors CI, merges PRs, then proceeds to the next wave.

---

## Quick Start

```bash
# Run the orchestrator
python .agents/skills/github-wave-orchestrator/orchestrator.py \
  --repo OWNER/REPO \
  --max-issues-per-wave 3

# Or with make (if added to Makefile)
make wave-orchestrate
```

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                    Wave Orchestrator                            │
│                     (orchestrator.py)                          │
├─────────────────────────────────────────────────────────────────┤
│  Phase 0: Pre-flight    →  gh auth, worktrees writable         │
│  Phase 1: Discovery     →  gh issue list --json               │
│  Phase 2: Wave Planning →  wave-planner.js                     │
│  Phase 3: Execute Wave   →  worktree → implement → PR          │
│  Phase 4: CI & Merge     →  monitor → fix → merge               │
│  Phase 5: Next Wave      →  repeat until done                    │
└─────────────────────────────────────────────────────────────────┘
```

---

## Workflow Phases

### Phase 0: Pre-flight

Verifies all prerequisites before starting:

| Check | Command | Failure Action |
|---|---|---|
| gh auth | `gh auth status` | Stop and report |
| worktrees/ writable | `mkdir -p ../worktrees && touch .test` | Stop and report |
| git fetch | `git fetch origin main` | Warning only |

### Phase 1: Discovery

```bash
gh issue list --state open --json number,title,body,labels,assignees
```

**Filters applied:**
- Remove issues with `blocked`, `on-hold`, `wontfix`, `duplicate` labels
- Remove issues assigned to someone (unless unassigned)
- Remove issues already linked to an open PR (`gh pr list --search "fixes #N"`)

### Phase 2: Wave Planning

```bash
gh issue list ... | node scripts/wave-planner.js
```

**Strategy:**
- Max 3 issues per wave (configurable via `--max-issues-per-wave`)
- Issues sorted by number ascending
- Greedy independent-set packing: issues with no shared file dependencies are packed together
- Wave plan output:
  ```
  WAVES=3
  WAVE_1_ISSUES=123,124,125
  WAVE_2_ISSUES=126,127
  WAVE_3_ISSUES=128
  ```

### Phase 3: Wave Execution (per wave)

For each issue in the current wave:

1. **Worktree Setup**
   ```bash
   git worktree add ../worktrees/issue-{N}-{slug} -b fix/issue-{N}-{slug} main
   ```

2. **Spawn Implementation Sub-agents**
   - One sub-agent per issue
   - Sub-agent implements the fix, writes tests, opens PR
   - See [REFERENCE.md](scripts/reference.md) for the template

3. **Wait**
   - Monitor until ALL sub-agents in the wave have created their PRs
   - Do not proceed to Phase 4 until every PR exists

### Phase 4: CI and Merge

1. **Merge Ordering**
   - PRs merged in ascending issue-number order
   - Rationale: Lower-numbered issues are typically older and more foundational
   - After each merge, check remaining PRs for conflicts

2. **Spawn CI Sub-agents**
   - One sub-agent per PR
   - Monitors CI status, fixes failures, resolves merge conflicts
   - See [REFERENCE.md](scripts/reference.md) for the template

3. **Wait**
   - Monitor until ALL PRs in the wave are merged (or escalated)
   - Then clean up worktrees: `git worktree prune`

### Phase 5: Next Wave

Repeat Phase 3–4 for the next wave. After the final wave, report summary:

```
WAVE ORCHESTRATION COMPLETE
===========================
Total issues: {N} | Waves: {count}
Merged: {count} | Escalated: {count} | Skipped: {count}
```

---

## Communication Rules

- **Silent during execution.** No play-by-play updates.
- **Update the user only when:**
  - Wave plan is ready for review (Phase 2)
  - A wave completes and the next begins
  - A sub-agent is stuck or CI cannot be fixed after 3 attempts
  - A merge conflict requires human resolution
  - The user asks a direct question

---

## Resume and Recovery

State is persisted to `../worktrees/wave-state.json`:

```json
{
  "version": 1,
  "current_wave": 2,
  "waves": [
    {
      "wave": 1,
      "status": "completed",
      "prs": [
        { "issue_number": 123, "pr_number": 456, "status": "merged" }
      ]
    },
    {
      "wave": 2,
      "status": "in_progress",
      "prs": [
        { "issue_number": 125, "pr_number": 458, "status": "merged" },
        { "issue_number": 126, "pr_number": null, "status": "pending" }
      ]
    }
  ],
  "last_updated": "2026-06-12T10:30:00Z"
}
```

**Recovery procedures:**

| Scenario | Recovery Action |
|---|---|
| Interrupted during PR creation | Check if PR exists via `gh pr list --search "fixes #N"`. Resume from PR creation if not found. |
| Interrupted during CI | Check CI status via `gh run list`. Resume monitoring if PR exists. |
| Interrupted during merge | Check if PR was merged via `gh pr view N --json state`. Resume merge if not merged. |
| Worktree exists but is stale | Delete and recreate the worktree before resuming. |
| State file corrupted | Start fresh from Phase 1 (Discovery). |

---

## Limits

| Parameter | Value |
|---|---|
| Max issues per wave | 3 (configurable) |
| Max CI fix iterations per PR | 10 |
| Max conflict resolution attempts | 2 |
| Worktree location | `../worktrees/` (parent of repo root) |

---

## Files

```
.agents/skills/github-wave-orchestrator/
├── SKILL.md              ← This file
├── README.md             ← Quick reference (sub-agent templates)
├── orchestrator.py       ← Main orchestrator script (CLI entry point)
├── wave-executor.py     ← Single wave executor (agent subprocess)
└── scripts/
    ├── wave-planner.js   ← Wave planning script
    ├── spawn-agent.py     ← Agent prompt generator (impl + CI)
    └── reference.md       ← Full reference (templates, strategies, recovery)
```

---

## Configuration

| Environment Variable | Description | Default |
|---|---|---|
| `MAX_ISSUES_PER_WAVE` | Maximum issues per wave | `3` |
| `GITHUB_REPOSITORY` | Repository in OWNER/REPO format | From `GITHUB_REPOSITORY` env or `anchapin/OSimFlow` |

---

## Usage Examples

```bash
# Basic run
python .agents/skills/github-wave-orchestrator/orchestrator.py

# Specify repository
python .agents/skills/github-wave-orchestrator/orchestrator.py \
  --repo myorg/myrepo

# Increase wave size
python .agents/skills/github-wave-orchestrator/orchestrator.py \
  --max-issues-per-wave 5

# Verbose output
python .agents/skills/github-wave-orchestrator/orchestrator.py -v
```

---

## Integration with Task Tool

The orchestrator spawns sub-agents using the `Task` tool with the `general` sub-agent type. Each sub-agent receives:

- **Implementation Agent**: Issue details, worktree path, branch name, and implementation task
- **CI Agent**: PR details, CI monitoring task, conflict resolution task

See [scripts/reference.md](scripts/reference.md) for the full prompt templates.

---

## Dependencies

- `gh` CLI (authenticated)
- `git`
- `node` (for wave-planner.js)
- Python 3.12+

---

## Exit Codes

| Code | Meaning |
|---|---|
| 0 | Success (all issues resolved or no issues found) |
| 1 | Pre-flight failed or other fatal error |
| 2 | Wave execution failed (escalated) |
