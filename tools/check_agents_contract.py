#!/usr/bin/env python3
"""check_agents_contract.py — machine-check the AGENTS.md / code contract.

Issue #15: PRs that add a new public symbol to osimflow/__init__.py, a
new bin/*.py script, or a new file under osimflow/executors/ MUST update
AGENTS.md in the same PR. This script enforces that invariant.

Run locally:
    python tools/check_agents_contract.py

In CI:
    see .github/workflows/agents-contract.yml

Exit code 0 on success, 1 if AGENTS.md is out of sync.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Public symbol surface, parsed from osimflow/__init__.py.
PUBLIC_SYMBOLS_RE = re.compile(r"__all__\s*=\s*\[(.*?)\]", re.DOTALL)
SYMBOL_RE = re.compile(r'"([A-Za-z_][A-Za-z0-9_]*)"')


def _public_symbols() -> list[str]:
    init_py = (REPO_ROOT / "osimflow" / "__init__.py").read_text()
    m = PUBLIC_SYMBOLS_RE.search(init_py)
    if not m:
        return []
    return SYMBOL_RE.findall(m.group(1))


def _bin_scripts() -> list[str]:
    return sorted(p.name for p in (REPO_ROOT / "bin").glob("*.py"))


def _executor_files() -> list[str]:
    return sorted(p.name for p in (REPO_ROOT / "osimflow" / "executors").glob("*.py"))


def _step_names_from_campaign() -> list[str]:
    """Pull every "step=" constant from osimflow/campaign.py."""
    campaign = (REPO_ROOT / "osimflow" / "campaign.py").read_text()
    return sorted(set(re.findall(r'step="([A-Z_]+)"', campaign)))


def _cli_flags() -> list[str]:
    """Pull every --flag from the CLI parser."""
    main_py = (REPO_ROOT / "osimflow" / "__main__.py").read_text()
    return sorted(set(re.findall(r'add_argument\(\s*"--([a-z][a-z0-9-]*)"', main_py)))


def _check(agents_md: str, terms: list[str], category: str) -> list[str]:
    return [t for t in terms if t not in agents_md]


def main() -> int:
    agents_md_path = REPO_ROOT / "AGENTS.md"
    if not agents_md_path.is_file():
        print(f"ERROR: {agents_md_path} not found", file=sys.stderr)
        return 1
    agents_md = agents_md_path.read_text()

    errors: list[str] = []

    for sym in _check(agents_md, _public_symbols(), "public symbol"):
        errors.append(f"public symbol `{sym}` (from osimflow/__init__.py) missing from AGENTS.md")
    for script in _check(agents_md, _bin_scripts(), "bin script"):
        errors.append(f"bin script `{script}` missing from AGENTS.md")
    for ef in _check(agents_md, _executor_files(), "executor"):
        errors.append(f"executor `{ef}` missing from AGENTS.md")
    for step in _check(agents_md, _step_names_from_campaign(), "step"):
        errors.append(f"campaign step `{step}` missing from AGENTS.md")
    for flag in _check(agents_md, _cli_flags(), "CLI flag"):
        errors.append(f"CLI flag `--{flag}` missing from AGENTS.md")

    if errors:
        print(f"AGENTS.md contract check FAILED ({len(errors)} drift):", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        print(
            "\nFix: update AGENTS.md to mention the missing symbol/file/step/flag.",
            file=sys.stderr,
        )
        return 1

    n_syms = len(_public_symbols())
    n_bin = len(_bin_scripts())
    n_exec = len(_executor_files())
    n_steps = len(_step_names_from_campaign())
    n_flags = len(_cli_flags())
    print(
        f"AGENTS.md contract OK ({n_syms} symbols, {n_bin} bin scripts, "
        f"{n_exec} executors, {n_steps} steps, {n_flags} CLI flags)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
