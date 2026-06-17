#!/usr/bin/env python3
"""
Helper script to spawn implementation and CI sub-agents.
This is called by the orchestrator to spawn Task sub-agents.
"""

import argparse
import json
import sys


def build_implementation_prompt(issue_number: int, issue_title: str, issue_body: str,
                                worktree_path: str, branch_name: str, repo: str) -> str:
    """Build the implementation agent prompt."""
    return f"""## Role
You are an implementation sub-agent for GitHub issue #{issue_number}: {issue_title}.

## Context
- Repository: {repo}
- Base branch: main
- Worktree: {worktree_path}
- Branch name: {branch_name}

## Issue Body
{issue_body}

## Your Task
1. Navigate to the worktree directory: {worktree_path}
2. Analyze the issue and implement the fix
3. Follow project conventions (see AGENTS.md in the repo)
4. Run `make lint && make typecheck` to verify
5. Push the branch and create a PR:
   - Title: "fix #{issue_number}: {issue_title}"
   - Body: "Closes #{issue_number}"
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
{{"pr_url": "https://github.com/{repo}/pull/N", "pr_number": N, "status": "success", "branch": "{branch_name}"}}
"""


def build_ci_prompt(pr_number: int, pr_url: str, branch: str, repo: str) -> str:
    """Build the CI agent prompt."""
    return f"""## Role
You are a CI sub-agent for PR #{pr_number}: {pr_url}.

## Context
- Repository: {repo}
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


def main():
    parser = argparse.ArgumentParser(description="Spawn sub-agents for wave orchestrator")
    parser.add_argument("--agent-type", type=str, required=True,
                        choices=["implementation", "ci"],
                        help="Type of agent to spawn")
    parser.add_argument("--issue-number", type=int, help="Issue number (for implementation agents)")
    parser.add_argument("--issue-title", type=str, help="Issue title (for implementation agents)")
    parser.add_argument("--issue-body", type=str, default="", help="Issue body (for implementation agents)")
    parser.add_argument("--worktree-path", type=str, help="Worktree path (for implementation agents)")
    parser.add_argument("--branch-name", type=str, help="Branch name")
    parser.add_argument("--pr-number", type=int, help="PR number (for CI agents)")
    parser.add_argument("--pr-url", type=str, help="PR URL (for CI agents)")
    parser.add_argument("--repo", type=str, default="anchapin/OSimFlow",
                        help="Repository in OWNER/REPO format")
    args = parser.parse_args()

    if args.agent_type == "implementation":
        if not all([args.issue_number, args.issue_title, args.worktree_path, args.branch_name]):
            print("ERROR: --issue-number, --issue-title, --worktree-path, and --branch-name are required for implementation agents")
            sys.exit(1)

        prompt = build_implementation_prompt(
            args.issue_number,
            args.issue_title,
            args.issue_body,
            args.worktree_path,
            args.branch_name,
            args.repo
        )

        agent_info = {
            "prompt": prompt,
            "description": f"impl-issue-{args.issue_number}",
            "agent_type": "backend-engineer",
            "issue_number": args.issue_number,
            "worktree_path": args.worktree_path,
            "branch": args.branch_name
        }

    elif args.agent_type == "ci":
        if not all([args.pr_number, args.pr_url, args.branch_name]):
            print("ERROR: --pr-number, --pr-url, and --branch-name are required for CI agents")
            sys.exit(1)

        prompt = build_ci_prompt(
            args.pr_number,
            args.pr_url,
            args.branch_name,
            args.repo
        )

        agent_info = {
            "prompt": prompt,
            "description": f"ci-pr-{args.pr_number}",
            "agent_type": "general",
            "pr_number": args.pr_number,
            "branch": args.branch_name
        }

    else:
        print(f"ERROR: Unknown agent type: {args.agent_type}")
        sys.exit(1)

    # Print the agent info as JSON for the orchestrator to use
    print(json.dumps(agent_info, indent=2))


if __name__ == "__main__":
    main()
