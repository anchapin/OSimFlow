# GitHub Wave Orchestrator

Quick reference for sub-agents and operators. See [`scripts/reference.md`](scripts/reference.md) for the full documentation.

---

## Quick Start

```bash
# Run the orchestrator
python .agents/skills/github-wave-orchestrator/orchestrator.py

# Resume from interruption
python .agents/skills/github-wave-orchestrator/orchestrator.py --resume

# Force re-plan waves
python .agents/skills/github-wave-orchestrator/orchestrator.py --force
```

---

## File Structure

```
github-wave-orchestrator/
├── SKILL.md              ← Full skill documentation
├── README.md             ← This file (quick reference)
├── orchestrator.py       ← Main orchestrator script (CLI entry point)
├── wave-executor.py     ← Single wave executor (agent subprocess)
└── scripts/
    ├── wave-planner.js   ← Wave planning (node)
    ├── spawn-agent.py     ← Agent prompt generator (impl + CI)
    └── reference.md      ← Full reference documentation
```

---

## Sub-agent Templates

### Implementation Agent

Used for: implementing a fix for a single GitHub issue

Key prompts:
- Repository: `anchapin/OSimFlow`
- Base branch: `main`
- Worktree: `../worktrees/issue-{N}-{slug}`
- Branch: `fix/issue-{N}-{slug}`

Output format:
```json
{
  "pr_url": "https://github.com/anchapin/OSimFlow/pull/N",
  "pr_number": N,
  "branch": "fix/issue-N-slug",
  "status": "success",
  "notes": "optional caveats"
}
```

### CI Agent

Used for: monitoring CI, fixing failures, merging a PR

Key prompts:
- PR number, URL, branch
- Max CI fix iterations: 10
- Max conflict resolution attempts: 2

Output format:
```json
{
  "pr_number": N,
  "status": "merged",
  "ci_attempts": 3,
  "conflict_attempts": 0,
  "notes": "merged successfully"
}
```

---

## Merge Ordering Strategy

PRs are merged in **ascending issue-number order** within a wave.

After each merge, check remaining PRs for conflicts. If conflict detected, rebase onto main before merging.

---

## Wave State File

`../worktrees/wave-state.json`

Resume from interruption by running with `--resume` flag.

---

## Limits

| Parameter | Value |
|---|---|
| Max issues per wave | 3 |
| Max CI fix iterations per PR | 10 |
| Max conflict resolution attempts | 2 |
| Worktree location | `../worktrees/` |
