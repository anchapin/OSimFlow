#!/usr/bin/env python3
"""
GitHub Wave Orchestrator

Resolves all open GitHub issues via parallel execution waves. Each wave groups
independent issues (no shared files), spawns sub-agents in isolated worktrees,
monitors CI, merges PRs, then proceeds to the next wave.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional


class WaveStatus(Enum):
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    IN_PROGRESS = "in_progress"
    WAVE_COMPLETE = "wave_complete"
    ALL_COMPLETE = "all_complete"


@dataclass
class Issue:
    number: int
    title: str
    body: str = ""
    labels: list = field(default_factory=list)
    assignees: list = field(default_factory=list)


@dataclass
class Wave:
    wave_num: int
    issues: list
    prs: list = field(default_factory=list)
    merged: list = field(default_factory=list)


@dataclass
class WaveState:
    status: WaveStatus = WaveStatus.AWAITING_CONFIRMATION
    total_issues: int = 0
    total_waves: int = 0
    current_wave: int = 1
    waves: list = field(default_factory=list)


# Configuration
WORKTREES_DIR = Path(os.environ.get("WORKTREES_DIR", "/home/alex/Projects/worktrees"))
REPO_ROOT = Path("/home/alex/Projects/OSimFlow")
SKILLS_DIR = REPO_ROOT / ".agents" / "skills" / "github-wave-orchestrator"
WAVE_STATE_FILE = WORKTREES_DIR / "wave-state.json"
MAX_CI_FIX_ATTEMPTS = 10
MAX_CONFLICT_ATTEMPTS = 2
MAX_ISSUES_PER_WAVE = 3
REPO_OWNER = "anchapin"
REPO_NAME = "OSimFlow"


def run_cmd(cmd: list[str], cwd: Optional[Path] = None, input_str: Optional[str] = None) -> subprocess.CompletedProcess:
    """Run a command and return the result."""
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, input=input_str)


def gh_auth_status() -> bool:
    """Check if gh is authenticated."""
    result = run_cmd(["gh", "auth", "status"])
    return result.returncode == 0 and "Logged in to github.com" in result.stdout


def check_worktrees_writable() -> bool:
    """Check if worktrees directory is writable."""
    try:
        WORKTREES_DIR.mkdir(parents=True, exist_ok=True)
        test_file = WORKTREES_DIR / ".test"
        test_file.touch()
        test_file.unlink()
        return True
    except Exception:
        return False


def preflight_checks() -> bool:
    """Phase 0: Pre-flight checks."""
    print("Phase 0: Pre-flight checks...")

    if not gh_auth_status():
        print("ERROR: gh not authenticated. Run 'gh auth login' first.")
        return False

    result = run_cmd(["git", "fetch", "origin", "main"], cwd=REPO_ROOT)
    if result.returncode != 0:
        print(f"ERROR: Failed to fetch origin main: {result.stderr}")
        return False

    if not check_worktrees_writable():
        print(f"ERROR: Worktrees directory {WORKTREES_DIR} is not writable.")
        return False

    print("  ✓ gh authenticated")
    print("  ✓ git fetch successful")
    print("  ✓ worktrees/ writable")
    print("Pre-flight checks PASSED\n")
    return True


def discover_issues() -> list[Issue]:
    """Phase 1: Discover open issues."""
    print("Phase 1: Discovering open issues...")

    result = run_cmd([
        "gh", "issue", "list",
        "--state", "open",
        "--json", "number,title,body,labels,assignees",
        "--limit", "100"
    ])

    if result.returncode != 0:
        print(f"ERROR: Failed to list issues: {result.stderr}")
        return []

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as e:
        print(f"ERROR: Failed to parse issues JSON: {e}")
        return []

    BLOCKED_LABELS = {'blocked', 'on-hold', 'wontfix', 'duplicate'}

    issues = []
    for item in data:
        labels = []
        if item.get("labels"):
            labels = [l["name"] if isinstance(l, dict) else l for l in item["labels"]]

        # Skip blocked labels
        if any(l.lower() in BLOCKED_LABELS for l in labels):
            continue

        # Skip assigned issues
        if item.get("assignees") and len(item["assignees"]) > 0:
            continue

        # Check if already has a PR
        pr_result = run_cmd([
            "gh", "pr", "list",
            "--search", f"fixes #{item['number']}",
            "--state", "open",
            "--json", "number"
        ])
        if pr_result.returncode == 0:
            prs = json.loads(pr_result.stdout)
            if len(prs) > 0:
                continue

        issues.append(Issue(
            number=item["number"],
            title=item["title"],
            body=item.get("body", ""),
            labels=labels,
            assignees=item.get("assignees", [])
        ))

    print(f"  Found {len(issues)} open issues (after filtering)")
    return issues


def plan_waves(issues: list[Issue]) -> WaveState:
    """Phase 2: Plan waves using wave-planner.js"""
    print("Phase 2: Planning waves...")

    # Convert issues to JSON for wave-planner
    issues_json = json.dumps([{
        "number": i.number,
        "title": i.title,
        "body": i.body,
        "labels": i.labels,
        "assignees": i.assignees
    } for i in issues])

    # Run wave-planner.js with issues as stdin
    result = run_cmd(
        ["node", str(SKILLS_DIR / "scripts" / "wave-planner.js")],
        input_str=issues_json
    )
    if result.returncode != 0:
        print(f"ERROR: wave-planner.js failed: {result.stderr}")
        # Fall back to simple grouping
        waves = []
        for i in range(0, len(issues), MAX_ISSUES_PER_WAVE):
            wave_issues = issues[i:i + MAX_ISSUES_PER_WAVE]
            waves.append(Wave(
                wave_num=len(waves) + 1,
                issues=[issue.number for issue in wave_issues]
            ))
        state = WaveState(
            total_issues=len(issues),
            total_waves=len(waves),
            waves=waves
        )
    else:
        plan = json.loads(result.stdout)
        waves = []
        for w in plan["waves"]:
            waves.append(Wave(
                wave_num=w["wave"],
                issues=w["issues"]
            ))
        state = WaveState(
            status=WaveStatus.AWAITING_CONFIRMATION,
            total_issues=plan["total_issues"],
            total_waves=plan["total_waves"],
            waves=waves
        )

    print(f"  Planned {state.total_waves} waves for {state.total_issues} issues")
    return state


def save_wave_state(state: WaveState) -> None:
    """Save wave state to file."""
    data = {
        "status": state.status.value,
        "total_issues": state.total_issues,
        "total_waves": state.total_waves,
        "current_wave": state.current_wave,
        "waves": [
            {
                "wave": w.wave_num,
                "issues": w.issues,
                "prs": w.prs,
                "merged": w.merged
            }
            for w in state.waves
        ]
    }
    with open(WAVE_STATE_FILE, "w") as f:
        json.dump(data, f, indent=2)


def load_wave_state() -> Optional[WaveState]:
    """Load wave state from file."""
    if not WAVE_STATE_FILE.exists():
        return None

    try:
        with open(WAVE_STATE_FILE) as f:
            data = json.load(f)

        waves = []
        for w in data.get("waves", []):
            waves.append(Wave(
                wave_num=w["wave"],
                issues=w["issues"],
                prs=w.get("prs", []),
                merged=w.get("merged", [])
            ))

        return WaveState(
            status=WaveStatus(data.get("status", "awaiting_confirmation")),
            total_issues=data.get("total_issues", 0),
            total_waves=data.get("total_waves", 0),
            current_wave=data.get("current_wave", 1),
            waves=waves
        )
    except Exception as e:
        print(f"WARNING: Failed to load wave state: {e}")
        return None


def present_wave_plan(state: WaveState) -> None:
    """Present wave plan to user for confirmation."""
    print("\n" + "=" * 60)
    print("WAVE PLAN - Awaiting Confirmation")
    print("=" * 60)
    print(f"Total issues: {state.total_issues}")
    print(f"Total waves: {state.total_waves}\n")

    for wave in state.waves:
        print(f"Wave {wave.wave_num}: Issues {wave.issues}")

    print("\n" + "=" * 60)


def build_implementation_prompt(issue: Issue, worktree_path: Path, branch_name: str) -> str:
    """Build the implementation agent prompt for an issue."""
    slug = re.sub(r'[^a-z0-9]+', '-', issue.title.lower())[:50]

    return f"""## Role
You are an implementation sub-agent for GitHub issue #{issue.number}: {issue.title}.

## Context
- Repository: {REPO_OWNER}/{REPO_NAME}
- Base branch: main
- Worktree: {worktree_path}
- Branch name: {branch_name}

## Issue Body
{issue.body}

## Your Task
1. Navigate to the worktree directory: {worktree_path}
2. Analyze the issue and implement the fix
3. Follow project conventions (see AGENTS.md in the repo)
4. Run `make lint && make typecheck` to verify
5. Push the branch and create a PR:
   - Title: "fix #{issue.number}: {issue.title}"
   - Body: "Closes #{issue.number}"
   - Use `gh pr create` to create the PR

## Implementation Guidelines
- Follow PEP 8 + type hints
- Use pathlib.Path over os.path
- Catch exceptions, log with exc_info=True, re-raise
- Do NOT break existing tests
- All new code must have type annotations

## Verification
Before creating PR, verify:
- [ ] `make lint` passes
- [ ] `make typecheck` passes
- [ ] New tests pass (if applicable)

## Output
After creating the PR, output the PR URL and number as JSON:
{{"pr_url": "https://github.com/{REPO_OWNER}/{REPO_NAME}/pull/N", "pr_number": N, "status": "success", "branch": "{branch_name}"}}
"""


def build_ci_prompt(pr_number: int, pr_url: str, branch: str) -> str:
    """Build the CI agent prompt for a PR."""
    return f"""## Role
You are a CI sub-agent for PR #{pr_number}: {pr_url}.

## Context
- Repository: {REPO_OWNER}/{REPO_NAME}
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
{{"pr_number": {pr_number}, "status": "merged" | "conflict_unresolved" | "escalated", "ci_attempts": N, "conflict_attempts": N, "notes": "any caveats"}}
"""


def setup_worktree(issue: Issue) -> Optional[Path]:
    """3a. Setup worktree for an issue."""
    slug = re.sub(r'[^a-z0-9]+', '-', issue.title.lower())[:50]
    worktree_path = WORKTREES_DIR / f"issue-{issue.number}-{slug}"
    branch_name = f"fix/issue-{issue.number}-{slug}"

    # Check if worktree already exists
    if worktree_path.exists():
        print(f"  Worktree already exists: {worktree_path}")
        return worktree_path

    # Create worktree
    result = run_cmd([
        "git", "worktree", "add",
        str(worktree_path),
        "-b", branch_name,
        "main"
    ], cwd=REPO_ROOT)

    if result.returncode != 0:
        print(f"  ERROR: Failed to create worktree: {result.stderr}")
        return None

    print(f"  Created worktree: {worktree_path}")
    return worktree_path


def wait_for_prs(state: WaveState, wave: Wave, issues: dict) -> None:
    """3c. Wait for all PRs to be created."""
    print(f"\nWaiting for PRs for wave {wave.wave_num}...")

    # Get PR info for each issue
    for issue_num in wave.issues:
        issue = issues.get(issue_num)
        if not issue:
            continue

        # Check if PR exists
        result = run_cmd([
            "gh", "pr", "list",
            "--search", f"fixes #{issue_num}",
            "--state", "open",
            "--json", "number,title,url"
        ])

        if result.returncode == 0:
            prs = json.loads(result.stdout)
            if prs:
                wave.prs.append(prs[0]["number"])
                print(f"  Issue #{issue_num}: PR #{prs[0]['number']} created")
                continue

        # Wait and check again
        for attempt in range(30):  # 30 attempts * 10s = 5 minutes max
            time.sleep(10)
            result = run_cmd([
                "gh", "pr", "list",
                "--search", f"fixes #{issue_num}",
                "--state", "open",
                "--json", "number,title,url"
            ])
            if result.returncode == 0:
                prs = json.loads(result.stdout)
                if prs:
                    wave.prs.append(prs[0]["number"])
                    print(f"  Issue #{issue_num}: PR #{prs[0]['number']} created")
                    break
        else:
            print(f"  WARNING: No PR found for issue #{issue_num}")


def monitor_and_merge_pr(pr_number: int, max_attempts: int = MAX_CI_FIX_ATTEMPTS) -> dict:
    """4b. Monitor CI, fix failures, merge PR."""
    ci_attempts = 0
    conflict_attempts = 0

    while ci_attempts < max_attempts:
        # Check CI status
        result = run_cmd([
            "gh", "pr", "view", str(pr_number),
            "--json", "statusCheckRollup,state,mergeable"
        ])

        if result.returncode != 0:
            print(f"  ERROR: Failed to get PR status: {result.stderr}")
            return {"status": "error", "notes": result.stderr}

        data = json.loads(result.stdout)
        state = data.get("state", "")

        if state == "MERGED":
            return {"status": "merged", "ci_attempts": ci_attempts, "notes": "PR merged"}

        if state == "CLOSED":
            return {"status": "closed", "notes": "PR was closed"}

        # Check if mergeable
        if data.get("mergeable") == "MERGEABLE":
            # Merge the PR
            merge_result = run_cmd([
                "gh", "pr", "merge", str(pr_number),
                "--squash", "--delete-branch"
            ])
            if merge_result.returncode == 0:
                return {"status": "merged", "ci_attempts": ci_attempts, "notes": "PR merged"}
            else:
                # Check for merge conflict
                if "merge conflict" in merge_result.stderr.lower():
                    conflict_attempts += 1
                    if conflict_attempts > MAX_CONFLICT_ATTEMPTS:
                        return {
                            "status": "conflict_unresolved",
                            "ci_attempts": ci_attempts,
                            "conflict_attempts": conflict_attempts,
                            "notes": "Max conflict resolution attempts reached"
                        }
                    # Try to resolve conflict
                    print(f"  Resolving merge conflict (attempt {conflict_attempts})...")
                    # Rebase onto main
                    run_cmd(["git", "fetch", "origin", "main"], cwd=REPO_ROOT)
                    run_cmd(["git", "rebase", "origin/main"], cwd=REPO_ROOT)
                    run_cmd(["git", "push", "--force"], cwd=REPO_ROOT)

        time.sleep(30)  # Wait for CI
        ci_attempts += 1

    return {
        "status": "escalated",
        "ci_attempts": ci_attempts,
        "notes": "Max CI fix attempts reached"
    }


def cleanup_worktrees() -> None:
    """Clean up worktrees after wave completion."""
    print("Cleaning up worktrees...")
    run_cmd(["git", "worktree", "prune"])
    print("  Worktrees pruned")


def execute_wave(state: WaveState, wave: Wave, issues: dict) -> dict:
    """Execute a single wave."""
    print(f"\n{'=' * 60}")
    print(f"EXECUTING WAVE {wave.wave_num}")
    print(f"{'=' * 60}")
    print(f"Issues: {wave.issues}")

    state.status = WaveStatus.IN_PROGRESS
    save_wave_state(state)

    # Phase 3a & 3b: Setup worktrees and spawn implementation agents
    print("\nPhase 3: Setting up worktrees and spawning implementation agents...")

    agent_tasks = []
    for issue_num in wave.issues:
        issue = issues.get(issue_num)
        if not issue:
            print(f"  Issue #{issue_num} not found in issues dict")
            continue

        worktree_path = setup_worktree(issue)
        if worktree_path:
            branch_name = f"fix/issue-{issue.number}-{re.sub(r'[^a-z0-9]+', '-', issue.title.lower())[:50]}"
            prompt = build_implementation_prompt(issue, worktree_path, branch_name)

            # Output agent info for the orchestrator runner to spawn
            print(f"\n[SPAWN] Implementation agent for issue #{issue.number}")
            print(f"  Worktree: {worktree_path}")
            print(f"  Branch: {branch_name}")

            agent_tasks.append({
                "issue_number": issue.number,
                "worktree_path": str(worktree_path),
                "branch": branch_name,
                "prompt": prompt,
                "status": "spawned"
            })

    # Phase 3c: Wait for PRs
    wait_for_prs(state, wave, issues)

    # Phase 4: CI and Merge
    print("\nPhase 4: CI and Merge...")

    # Sort PRs by number (ascending)
    wave.prs.sort()

    results = []
    for pr_num in wave.prs:
        print(f"\n  Processing PR #{pr_num}...")

        # Get PR info for CI agent prompt
        pr_result = run_cmd([
            "gh", "pr", "view", str(pr_num),
            "--json", "url,headRefName"
        ])
        pr_data = json.loads(pr_result.stdout) if pr_result.returncode == 0 else {}
        branch = pr_data.get("headRefName", "")

        # Output CI agent info
        print(f"\n[SPAWN] CI agent for PR #{pr_num}")
        ci_prompt = build_ci_prompt(pr_num, pr_data.get("url", ""), branch)
        print(f"  Branch: {branch}")

        result = monitor_and_merge_pr(pr_num)
        results.append(result)

        if result["status"] == "merged":
            wave.merged.append(pr_num)

        # After each merge, check remaining PRs for conflicts
        save_wave_state(state)

    # Cleanup
    cleanup_worktrees()

    state.status = WaveStatus.WAVE_COMPLETE
    save_wave_state(state)

    return {
        "wave": wave.wave_num,
        "prs": len(wave.prs),
        "merged": len(wave.merged),
        "results": results
    }


def print_summary(state: WaveState) -> None:
    """Print final summary."""
    total_merged = sum(len(w.merged) for w in state.waves)
    total_prs = sum(len(w.prs) for w in state.waves)

    print("\n" + "=" * 60)
    print("WAVE ORCHESTRATION COMPLETE")
    print("=" * 60)
    print(f"Total issues: {state.total_issues} | Waves: {state.total_waves}")
    print(f"Merged: {total_merged} | Total PRs: {total_prs}")
    print("=" * 60)


def main():
    global MAX_ISSUES_PER_WAVE, REPO_OWNER, REPO_NAME

    parser = argparse.ArgumentParser(description="GitHub Wave Orchestrator")
    parser.add_argument("--resume", action="store_true", help="Resume from wave-state.json")
    parser.add_argument("--force", action="store_true", help="Force re-plan waves")
    parser.add_argument("--yes", action="store_true", help="Skip confirmation prompt")
    parser.add_argument("--max-issues-per-wave", type=int, default=MAX_ISSUES_PER_WAVE,
                        help="Maximum issues per wave")
    parser.add_argument("--repo", type=str, default=f"{REPO_OWNER}/{REPO_NAME}",
                        help="Repository in OWNER/REPO format")
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose output")
    args = parser.parse_args()

    MAX_ISSUES_PER_WAVE = args.max_issues_per_wave

    # Parse repo
    if "/" in args.repo:
        parts = args.repo.split("/")
        REPO_OWNER, REPO_NAME = parts[0], parts[1]

    # Phase 0: Pre-flight
    if not preflight_checks():
        sys.exit(1)

    # Check for existing state
    state = load_wave_state()

    if args.resume and state:
        print("\nResuming from wave-state.json...")
        if state.status == WaveStatus.AWAITING_CONFIRMATION:
            present_wave_plan(state)
            # Would wait for user input in interactive mode
        elif state.status == WaveStatus.IN_PROGRESS:
            # Resume from current wave
            current_wave = next((w for w in state.waves if w.wave_num == state.current_wave), None)
            if current_wave:
                issues = {i.number: i for i in discover_issues()}
                execute_wave(state, current_wave, issues)
    elif args.force or not state:
        # Phase 1: Discover
        issues = discover_issues()
        if not issues:
            print("No issues to process.")
            sys.exit(0)

        # Phase 2: Plan
        state = plan_waves(issues)

        # Present plan
        present_wave_plan(state)

        # Would wait for user confirmation in interactive mode
        # For now, auto-proceed unless --yes is not set
        if not args.yes:
            response = input("Proceed with wave execution? (y/n): ").strip().lower()
            if response != 'y':
                print("Aborted by user.")
                sys.exit(0)

        state.status = WaveStatus.IN_PROGRESS
        save_wave_state(state)

        # Execute waves
        issues_dict = {i.number: i for i in issues}
        for wave in state.waves:
            execute_wave(state, wave, issues_dict)
            state.current_wave = wave.wave_num + 1
            save_wave_state(state)

        state.status = WaveStatus.ALL_COMPLETE
        save_wave_state(state)
        print_summary(state)
    else:
        print("\nUse --resume to continue or --force to re-plan")
        present_wave_plan(state)


if __name__ == "__main__":
    main()
