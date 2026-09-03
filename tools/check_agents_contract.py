#!/usr/bin/env python3
"""check_agents_contract.py — machine-check the AGENTS.md / code contract.

Issue #15: PRs that add a new public symbol to osimflow/__init__.py, a
new bin/*.py script, or a new file under osimflow/executors/ MUST update
AGENTS.md in the same PR. This script enforces that invariant.

Issue #1575: per-executor ``--flags`` now live in the
``add_arguments(parser_group)`` hooks under osimflow/executor_configs/
(and modules there are contract-checked like executor files). The flag
list is derived from those hooks — by building the real CLI parser and
walking its actions when osimflow is importable, falling back to a
textual scan of the hook modules + osimflow/__main__.py otherwise
(e.g. the bare-python CI contract step).

Run locally:
    python tools/check_agents_contract.py

In CI:
    see .github/workflows/agents-contract.yml

Exit code 0 on success, 1 if AGENTS.md is out of sync.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Public symbol surface, parsed from osimflow/__init__.py.
PUBLIC_SYMBOLS_RE = re.compile(r"__all__\s*=\s*\[(.*?)\]", re.DOTALL)
SYMBOL_RE = re.compile(r'"([A-Za-z_][A-Za-z0-9_]*)"')

# Flag literal at the start of an add_argument( chunk. Underscore and
# mixed-case spellings are included so the textual fallback matches the
# parser introspection exactly (e.g. --input_variables, --no-tui).
ADD_ARGUMENT_FLAG_RE = re.compile(r'add_argument\(\s*"--([a-z][a-zA-Z0-9_-]*)"')


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


def _executor_config_files() -> list[str]:
    """Per-executor config/argument-hook modules (issue #1575)."""
    return sorted(p.name for p in (REPO_ROOT / "osimflow" / "executor_configs").glob("*.py"))


def _flag_source_paths() -> list[Path]:
    """Argparse surfaces that declare OSimFlow's own ``--flags``:
    osimflow/__main__.py plus the per-executor add_arguments hook
    modules (issue #1575)."""
    return [
        REPO_ROOT / "osimflow" / "__main__.py",
        *sorted((REPO_ROOT / "osimflow" / "executor_configs").glob("*.py")),
    ]


def _step_names_from_campaign() -> list[str]:
    """Pull every "step=" constant from osimflow/campaign.py."""
    campaign = (REPO_ROOT / "osimflow" / "campaign.py").read_text()
    return sorted(set(re.findall(r'step="([A-Z_]+)"', campaign)))


def _cli_flags_from_parser() -> set[str] | None:
    """Derive the flag list by building the real CLI parser (issue #1575).

    Imports ``osimflow.__main__._build_parser`` — which registers every
    executor ``add_arguments`` hook — and walks the resulting parser
    tree (all subcommands) collecting each action's *registered* option
    spelling (``option_strings[0]``, so ``BooleanOptionalAction`` counts
    only its positive form while a literal ``--no-tui`` still counts).

    Returns ``None`` when osimflow is not importable (e.g. the bare
    ``setup-python`` CI contract step) so the caller can fall back to
    the textual scan.
    """
    try:
        from osimflow.__main__ import _build_parser  # noqa: PLC0415 — deliberately lazy
    except Exception:  # noqa: BLE001 — never crash the contract check on imports
        return None

    names: set[str] = set()

    def _walk(parser: object) -> None:
        for action in getattr(parser, "_actions", []):
            option_strings = list(getattr(action, "option_strings", []))
            if option_strings:
                primary = option_strings[0]
                if primary.startswith("--") and primary != "--help":
                    names.add(primary[2:])
            if isinstance(action, argparse._SubParsersAction):
                for subparser in action.choices.values():
                    _walk(subparser)

    _walk(_build_parser())
    return names


def _cli_flags_from_sources() -> set[str]:
    """Textual fallback: scan ``add_argument("--flag"`` literals in
    ``osimflow/__main__.py`` and the executor_configs hook modules."""
    names: set[str] = set()
    for path in _flag_source_paths():
        names.update(ADD_ARGUMENT_FLAG_RE.findall(path.read_text()))
    return names


def _cli_flags() -> list[str]:
    """Pull every --flag from the CLI parser (issue #1575 derivation).

    Primary source: parser introspection (see
    ``_cli_flags_from_parser``) — the same ``add_arguments`` hooks
    ``osimflow run`` builds its argparse tree from. Fallback (when
    osimflow is not importable) and safety net (for any flag declared
    outside the hooks): the textual ``add_argument`` scan.
    """
    names = _cli_flags_from_sources()
    parsed = _cli_flags_from_parser()
    if parsed is not None:
        names |= parsed
    return sorted(names)


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
    for ecf in _check(agents_md, _executor_config_files(), "executor config module"):
        errors.append(
            f"executor config module `{ecf}` (osimflow/executor_configs/) missing from AGENTS.md"
        )
    for step in _check(agents_md, _step_names_from_campaign(), "step"):
        errors.append(f"campaign step `{step}` missing from AGENTS.md")
    for flag in _check(agents_md, [f"--{f}" for f in _cli_flags()], "CLI flag"):
        errors.append(f"CLI flag `{flag}` missing from AGENTS.md")

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
    n_exec_cfg = len(_executor_config_files())
    n_steps = len(_step_names_from_campaign())
    n_flags = len(_cli_flags())
    print(
        f"AGENTS.md contract OK ({n_syms} symbols, {n_bin} bin scripts, "
        f"{n_exec} executors, {n_exec_cfg} executor config modules, "
        f"{n_steps} steps, {n_flags} CLI flags)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
