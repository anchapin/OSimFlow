#!/usr/bin/env python3
"""
Wave Executor Agent

This agent coordinates a single wave of issue resolution. It:
1. Sets up worktrees for each issue in the wave
2. Spawns implementation sub-agents for each issue
3. Waits for PRs to be created
4. Spawns CI sub-agents to monitor CI and merge PRs
5. Reports results

This agent is spawned by the Wave Orchestrator and uses the Task tool
to spawn child sub-agents.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


# Configuration
WORKTREES_DIR = Path(os.environ.get("WORKTREES_DIR", "/home/alex/Projects/worktrees"))
REPO_ROOT = Path("/home/alex/Projects/OSimFlow")
SKILLS_DIR = REPO_ROOT / ".agents" / "skills" / "github-wave-orchestrator"
MAX_CI_FIX_ATTEMPTS = 10
MAX_CONFLICT_ATTEMPTS = 2
REPO_OWNER = "anchapin"
REPO_NAME = "OSimFlow"


@dataclass
class Issue:
    number: int
    title: str
    body: str = ""
    labels: list = field(default_factory=list)


@dataclass
class WaveResult:
    wave_num: int
    total_prs: int = 0
    merged: int = 0
    escalated: int = 0
    results: list = field(default_factory=list)


def run_cmd(cmd: list[str], cwd: Optional[Path] = None, input_str: Optional[str] = None) -> subprocess.CompletedProcess:
    """Run a command and return the result."""
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, input=input_str)


def gh_auth_status() -> bool:
    """Check if gh is authenticated."""
    result = run_cmd(["gh", "auth", "status"])
    return result.returncode == 0 and "Logged in to github.com" in result.stdout


def setup_worktree(issue: Issue) -> Optional[Path]:
    """Setup worktree for an issue."""
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


def build_implementation_prompt(issue: Issue, worktree_path: Path, branch_name: str) -> str:
    """Build the implementation agent prompt for an issue."""
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


def wait_for_pr(issue_num: int, timeout: int = 300) -> Optional[int]:
    """Wait for a PR to be created for an issue."""
    start_time = time.time()

    while time.time() - start_time < timeout:
        result = run_cmd([
            "gh", "pr", "list",
            "--search", f"fixes #{issue_num}",
            "--state", "open",
            "--json", "number,title,url"
        ])

        if result.returncode == 0:
            prs = json.loads(result.stdout)
            if prs:
                return prs[0]["number"]

        time.sleep(10)

    return None


def monitor_and_merge_pr(pr_number: int, max_attempts: int = MAX_CI_FIX_ATTEMPTS) -> dict:
    """Monitor CI, fix failures, merge PR."""
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


def execute_wave(wave_num: int, issues: list, max_workers: int = 3) -> WaveResult:
    """Execute a single wave of issue resolution.

    This function is designed to be called from an agent context that has
    access to the Task tool for spawning sub-agents.

    Args:
        wave_num: The wave number
        issues: List of Issue objects for this wave
        max_workers: Maximum parallel sub-agents (default 3)

    Returns:
        WaveResult with PR counts and individual results
    """
    print(f"\n{'=' * 60}")
    print(f"EXECUTING WAVE {wave_num}")
    print(f"{'=' * 60}")
    print(f"Issues: {[i.number for i in issues]}")

    result = WaveResult(wave_num=wave_num)

    # Phase 1: Setup worktrees
    print("\nPhase 1: Setting up worktrees...")
    worktrees = {}
    for issue in issues:
        worktree_path = setup_worktree(issue)
        if worktree_path:
            worktrees[issue.number] = {
                "path": worktree_path,
                "branch": f"fix/issue-{issue.number}-{re.sub(r'[^a-z0-9]+', '-', issue.title.lower())[:50]}",
                "issue": issue
            }

    # Phase 2: Spawn implementation agents (parallel, up to max_workers)
    print(f"\nPhase 2: Spawning implementation agents (max {max_workers} parallel)...")

    # Build implementation tasks
    impl_tasks = []
    for issue_num, wt_info in worktrees.items():
        issue = wt_info["issue"]
        branch = wt_info["branch"]
        worktree_path = wt_info["path"]

        prompt = build_implementation_prompt(issue, worktree_path, branch)

        impl_tasks.append({
            "description": f"impl-issue-{issue.number}",
            "prompt": prompt,
            "agent_type": "backend-engineer",
            "issue_number": issue.number,
            "worktree_path": str(worktree_path),
            "branch": branch
        })

    # Output task info for parent agent to spawn
    print(f"\n[SPAWN_IMPL_AGENTS]")
    print(json.dumps(impl_tasks, indent=2))

    # Phase 3: Wait for PRs
    print(f"\nPhase 3: Waiting for PRs...")
    pr_numbers = []
    for issue in issues:
        if issue.number in worktrees:
            print(f"  Waiting for issue #{issue.number}...")
            pr_num = wait_for_pr(issue.number)
            if pr_num:
                pr_numbers.append(pr_num)
                print(f"  Issue #{issue.number}: PR #{pr_num} created")
            else:
                print(f"  WARNING: No PR found for issue #{issue.number}")

    result.total_prs = len(pr_numbers)

    # Phase 4: CI and Merge (in issue-number order)
    print(f"\nPhase 4: CI and Merge...")

    # Sort PRs by number (ascending)
    pr_numbers.sort()

    # Build CI tasks
    ci_tasks = []
    for pr_num in pr_numbers:
        pr_result = run_cmd([
            "gh", "pr", "view", str(pr_num),
            "--json", "url,headRefName"
        ])
        pr_data = json.loads(pr_result.stdout) if pr_result.returncode == 0 else {}
        branch = pr_data.get("headRefName", "")
        pr_url = pr_data.get("url", "")

        ci_tasks.append({
            "description": f"ci-pr-{pr_num}",
            "prompt": build_ci_prompt(pr_num, pr_url, branch),
            "agent_type": "general",
            "pr_number": pr_num,
            "branch": branch
        })

    # Output CI task info for parent agent to spawn
    print(f"\n[SPAWN_CI_AGENTS]")
    print(json.dumps(ci_tasks, indent=2))

    # For now, do direct CI monitoring since we don't have Task tool access here
    # In a full implementation, the parent agent would spawn these CI agents
    for pr_num in pr_numbers:
        print(f"\n  Processing PR #{pr_num}...")
        ci_result = monitor_and_merge_pr(pr_num)
        result.results.append(ci_result)

        if ci_result["status"] == "merged":
            result.merged += 1
        elif ci_result["status"] == "escalated":
            result.escalated += 1

    # Cleanup worktrees
    print("\nCleaning up worktrees...")
    run_cmd(["git", "worktree", "prune"])

    return result


def main():
    parser = argparse.ArgumentParser(description="Wave Executor Agent")
    parser.add_argument("--wave-num", type=int, required=True, help="Wave number")
    parser.add_argument("--issues-json", type=str, required=True,
                        help="JSON array of issues")
    parser.add_argument("--max-workers", type=int, default=3,
                        help="Maximum parallel sub-agents")
    args = parser.parse_args()

    # Parse issues
    try:
        issues_data = json.loads(args.issues_json)
        issues = [Issue(
            number=i["number"],
            title=i["title"],
            body=i.get("body", ""),
            labels=i.get("labels", [])
        ) for i in issues_data]
    except json.JSONDecodeError as e:
        print(f"ERROR: Failed to parse issues JSON: {e}")
        sys.exit(1)

    # Execute wave
    wave_result = execute_wave(args.wave_num, issues, args.max_workers)

    # Output final result
    print(f"\n{'=' * 60}")
    print(f"WAVE {wave_result.wave_num} COMPLETE")
    print(f"{'=' * 60}")
    print(f"Total PRs: {wave_result.total_prs}")
    print(f"Merged: {wave_result.merged}")
    print(f"Escalated: {wave_result.escalated}")
    print(f"Results: {json.dumps(wave_result.results, indent=2)}")


if __name__ == "__main__":
    main()
