#!/usr/bin/env python3
"""check_docs_sync.py — verify docs/ references resolve to real paths and flags.

Issue #15: PRs that rename a `bin/*.py` script, a CLI flag, or any path
referenced from `docs/**/*.md` must update the docs in the same PR. This
script greps docs for path-like and flag-like references and asserts each
one still exists in the working tree.

Issue #1548: the flag check is strict. Every backticked ``--flag`` in
``docs/**/*.md`` must resolve to a real ``add_argument`` in one of the
repo's argparse surfaces (``osimflow/__main__.py`` first and foremost),
or be explicitly exempted in ``FOREIGN_CLI_FLAGS`` below (flags of other
tools shown in examples — sbatch, docker, git, repo shell scripts …).

Issue #1547: the reverse direction is also checked. Every ``--flag``
registered in ``osimflow/__main__.py`` must have user-guide coverage
(documented inline in docs/user-guide.md §4.1/§10 or pointer-linked to
the specialist guide that covers it), and every registered subcommand
must be mentioned in the user guide — mirroring how the AGENTS.md
contract checker keeps AGENTS.md complete.

A file can opt out with a `<!-- docs-skip -->` HTML comment.

Run locally:
    python tools/check_docs_sync.py

In CI:
    see .github/workflows/agents-contract.yml
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCS_DIR = REPO_ROOT / "docs"

# Reference kinds we check. Each entry: (regex, validator).
# Validators return None on "ok, skip" or an error string.
BACKTICKED_RE = re.compile(r"`([\w./\-]+\.[a-z]{1,5})`")
# NB: includes camelCase and underscore forms on purpose — those are
# exactly the drift classes the strict check exists to catch (e.g. a
# documented `--kubernetes-backoffLimit` or `--enable_cost_tracking`
# that argparse would reject).
CLI_FLAG_RE = re.compile(r"`(--[a-zA-Z][a-zA-Z0-9_-]+)`")
BIN_SCRIPT_RE = re.compile(
    r"\b(apply_params_to_model|extract_kpis|aggregate_results|"
    r"generate_lhs|generate_plots)\.py\b"
)

# Substrings that, when present in a backticked token, mean "not a path".
PATH_SKIP_SUBSTRINGS = (
    "eplusout",
    "openstudio_cli_image",  # container tag, not a local path
    "scientific_python_image",
    "nrel/openstudio",  # container image name, not a local path
    "openstudio.cli",  # external CLI binary, not a local file
    "ghcr.io",  # container registry, not a local file
    "docker.io",  # container registry, not a local file
    "scipy.stats",  # module path, not a file
    "os.path",  # module path, not a file
    "pathlib",  # module path, not a file
    "argparse",  # module path, not a file
    "sys.path",  # Python runtime attribute, not a file
    "subprocess.run",  # stdlib API, not a file
    "importlib.util",  # stdlib module, not a file
    "openstudio.cli",  # CLI binary shipped inside container, not a local path
    "containerOverride",  # AWS Batch API field, not a local path
    "containerResources",  # Azure/Google Batch API field, not a local path
    "resources.cpu",  # Nomad API field, not a local path
    "resources.memoryMB",  # Nomad API field, not a local path
    "requests.cpu",  # Kubernetes API field path, not a local path
    "limits.cpu",  # Kubernetes API field path, not a local path
    "requests.memory",  # Kubernetes API field path, not a local path
    "limits.memory",  # Kubernetes API field path, not a local path
    "containerResources",  # AWS Batch API field path, not a local path
    "resources.cpu",  # Kubernetes API field path, not a local path
    ".egg-info",
    "measure.rb",  # OpenStudio measure pattern (example in docs)
    "measure.py",  # OpenStudio measure pattern (example in docs)
    "measure.xml",  # OpenStudio measure pattern (example in docs)
    "Gemfile",  # Ruby bundler file (example in docs, not in repo)
    "requirements.txt",  # Python deps file (example in docs, not in repo)
    "/Users/",  # example absolute path in docs
    "vendor/",  # bundled deps dir (example in docs)
    "supervisord.conf",  # example config file (illustrative, not a repo file)
    "osimflow_work.py",  # example script name (illustrative, not a repo file)
    "shutil.which",  # stdlib API, not a file
    "pypi.org",  # domain name, not a path
    "campaign_results",  # conventional campaign output dir (created at runtime)
    "tar.gz",  # generated archive artifact, not a repo file
    "variables.foo.bar",  # MongoDB field name example in migration docs
)
# Extension classes that look like file refs but are documented file
# *patterns*, not real on-disk files we can resolve.
DOCUMENTED_PATTERNS = {
    "osm",
    "osw",
    "idf",
    "epw",
    "sql",
    "err",
    "log",
    "png",
    "pdf",
    "csv",
    "parquet",
    "json",
    "yml",
    "yaml",
    "toml",
    "md",
    "txt",
    "ini",
    "html",
}
SKIP_DIRS = {".agents", ".github", "__pycache__", ".venv", "node_modules", ".git"}

# ---------------------------------------------------------------------------
# Strict CLI-flag resolution (issue #1548)
# ---------------------------------------------------------------------------

# argparse surfaces that define OSimFlow's own `--flags`. Parsed from
# source at check time, so flag renames keep the check in sync without
# touching this file. Since issue #1575 the per-executor flags live in
# the add_arguments hook modules under osimflow/executor_configs/.
CLI_FLAG_SOURCE_FILES: list[Path] = [
    REPO_ROOT / "osimflow" / "__main__.py",
    *sorted((REPO_ROOT / "osimflow" / "executor_configs").glob("*.py")),
    *sorted((REPO_ROOT / "osimflow" / "_work_scripts").glob("*.py")),
    *sorted((REPO_ROOT / "bin").glob("*.py")),
    *sorted((REPO_ROOT / "scripts").glob("*.py")),
    *sorted((REPO_ROOT / "tools").glob("*.py")),
]

# Explicit exemption mechanism for legitimately-foreign flags: `--flags`
# of *other* tools that appear in doc examples. Every entry needs a
# justification string naming the tool it belongs to. Do not add
# OSimFlow flags here — fix the doc (or add the flag) instead.
FOREIGN_CLI_FLAGS: dict[str, str] = {
    "--admin": "`gh pr merge --admin` (GitHub CLI)",
    "--apply": "scripts/sweep-stale-branches.sh (shell script, no argparse)",
    "--cpus-per-task": "sbatch directive (Slurm mapping table)",
    "--find-links": "pip (air-gapped wheel bundling)",
    "--fix": "ruff / pre-commit autofix mode",
    "--include-orphaned": "scripts/sweep-stale-branches.sh (shell script, no argparse)",
    "--mem": "sbatch directive (Slurm mapping table)",
    "--min-age-days": "scripts/sweep-stale-branches.sh (shell script, no argparse)",
    "--no-verify": "git commit/push hook bypass",
    "--nv": "apptainer/singularity run --nv (host GPU libraries)",
    "--onefile": "PyInstaller build mode",
    "--protect-glob": "scripts/apply_branch_protection.sh (shell script, no argparse)",
    "--rm": "docker run --rm",
    "--skip-cache": "documented *future* OSimFlow flag (docs/distributed-cache.md)",
    "--time": "sbatch directive (Slurm mapping table)",
    "--worktree": "scripts/sweep-stale-branches.sh (shell script, no argparse)",
}

# Matches the flag name at the start of an add_argument( chunk.
_ADD_ARGUMENT_FLAG_AT_START_RE = re.compile(r'\s*"(--[a-zA-Z][a-zA-Z0-9_-]*)"')


def _collect_known_cli_flags() -> set[str]:
    """Parse every ``add_argument("--flag", ...)`` from the repo's argparse
    surfaces. Flags declared with ``action=argparse.BooleanOptionalAction``
    additionally accept a ``--no-<flag>`` spelling at runtime, so the
    negative form is synthesized for them.
    """
    known: set[str] = set()
    for path in CLI_FLAG_SOURCE_FILES:
        try:
            src = path.read_text()
        except FileNotFoundError:
            continue
        # Chunk per add_argument( call: everything up to the next call.
        # kwargs of the *next* call therefore cannot leak into this chunk.
        for chunk in src.split("add_argument(")[1:]:
            m = _ADD_ARGUMENT_FLAG_AT_START_RE.match(chunk)
            if not m:
                continue
            flag = m.group(1)
            known.add(flag)
            if "BooleanOptionalAction" in chunk:
                known.add(f"--no-{flag[2:]}")
    return known


# Regex for markdown cross-references: [text](target.md) or [text](target#anchor)
# Captures the link target (before any #anchor). We deliberately keep this
# simple — it handles relative paths like `./foo.md`, `../bar.md`, and
# bare filenames like `OSimFlow.md`. External URLs (http/https) are excluded.
MARKDOWN_LINK_RE = re.compile(r"\[(?:[^\]]*)\]\(([^)#]+\.md(?:#[^\)]*)?)\)")

# ---------------------------------------------------------------------------
# User-guide CLI coverage (issue #1547)
# ---------------------------------------------------------------------------

# Every --flag / subcommand registered in osimflow/__main__.py must appear
# in docs/user-guide.md (inline documentation, or on a line that pointer-
# links the specialist guide covering it — both count as "mentioned").
# Explicit exemption lists for justified exceptions; every entry needs a
# reason string. Keep these empty if at all possible.
USER_GUIDE_FLAG_EXEMPTIONS: dict[str, str] = {}
USER_GUIDE_SUBCOMMAND_EXEMPTIONS: dict[str, str] = {}

USER_GUIDE_REL_PATH = Path("docs") / "user-guide.md"
MAIN_MODULE_REL_PATH = Path("osimflow") / "__main__.py"
EXECUTOR_CONFIGS_REL_DIR = Path("osimflow") / "executor_configs"

# Matches the subcommand name at the start of an add_parser( chunk.
_ADD_PARSER_NAME_AT_START_RE = re.compile(r'\s*"([a-z][a-z0-9-]*)"')


def _collect_main_cli_flags(main_src: str) -> set[str]:
    """Explicitly-declared ``--flags`` in ``osimflow/__main__.py`` source
    text (the argparse surface built by ``_build_parser``). Synthesized
    ``--no-*`` negatives of ``BooleanOptionalAction`` are not required —
    covering the positive spelling in the user guide is sufficient.
    """
    flags: set[str] = set()
    for chunk in main_src.split("add_argument(")[1:]:
        m = _ADD_ARGUMENT_FLAG_AT_START_RE.match(chunk)
        if m:
            flags.add(m.group(1))
    return flags


def _collect_executor_hook_cli_flags() -> set[str]:
    """``--flags`` declared by the per-executor ``add_arguments`` hook
    modules under ``osimflow/executor_configs/`` (issue #1575). These
    moved out of ``__main__.py``; user-guide coverage must still find
    them, so the coverage check unions this set with the
    ``__main__.py`` flags.
    """
    flags: set[str] = set()
    hook_dir = REPO_ROOT / EXECUTOR_CONFIGS_REL_DIR
    for path in sorted(hook_dir.glob("*.py")):
        for chunk in path.read_text().split("add_argument(")[1:]:
            m = _ADD_ARGUMENT_FLAG_AT_START_RE.match(chunk)
            if m:
                flags.add(m.group(1))
    return flags


def _collect_main_subcommands(main_src: str) -> set[str]:
    """Subcommand names from every ``add_parser("name", ...)`` call."""
    names: set[str] = set()
    for chunk in main_src.split("add_parser(")[1:]:
        m = _ADD_PARSER_NAME_AT_START_RE.match(chunk)
        if m:
            names.add(m.group(1))
    return names


def _check_user_guide_flag_coverage(main_flags: set[str], guide_text: str) -> list[str]:
    """Issue #1547: every ``__main__.py`` flag must be mentioned in the
    user guide — documented inline (§4.1/§10) or pointer-linked to the
    specialist guide that covers it. Both are a textual mention, which
    keeps the rule simple and predictable."""
    errors: list[str] = []
    for flag in sorted(main_flags):
        if flag in USER_GUIDE_FLAG_EXEMPTIONS:
            continue
        if flag not in guide_text:
            errors.append(
                f"flag `{flag}` is registered in osimflow/__main__.py but has "
                f"no user-guide coverage — document it in user-guide.md §4.1 "
                f"or §10, or pointer-link the specialist guide that covers "
                f"it (or add a justified USER_GUIDE_FLAG_EXEMPTIONS entry in "
                f"tools/check_docs_sync.py)."
            )
    return errors


def _check_user_guide_subcommand_coverage(subcommands: set[str], guide_text: str) -> list[str]:
    """Issue #1547: every registered subcommand must be mentioned in the
    user guide as an ``osimflow <name>`` invocation (the §10 subcommand
    reference table satisfies this by construction)."""
    errors: list[str] = []
    for name in sorted(subcommands):
        if name in USER_GUIDE_SUBCOMMAND_EXEMPTIONS:
            continue
        if not re.search(r"osimflow\s+" + re.escape(name) + r"(?![\w-])", guide_text):
            errors.append(
                f"subcommand `{name}` is registered in osimflow/__main__.py "
                f"but is not mentioned in the user guide — add it to the §10 "
                f"subcommand reference (or add a justified "
                f"USER_GUIDE_SUBCOMMAND_EXEMPTIONS entry in "
                f"tools/check_docs_sync.py)."
            )
    return errors


def _is_documented_pattern(token: str) -> bool:
    """True if `token` is a documented file type we cannot resolve on disk
    (e.g. `workflow.osw`, `eplusout.sql`)."""
    if "." not in token:
        return False
    ext = token.rsplit(".", 1)[-1]
    if ext not in DOCUMENTED_PATTERNS:
        return False
    # `.md` cross-references (e.g. `deployment/slurm.md` inside a markdown
    # link) are validated by `_check_markdown_links` relative to the doc's
    # directory. Skip them here to avoid false REPO_ROOT-relative misses.
    if ext == "md":
        return True
    # Files at the repo root or in known ignored dirs (examples in the PRD
    # are typically not checked in).
    if "/" not in token:
        return True
    first = token.split("/", 1)[0]
    if first in {"template", "example", "samples"}:
        return True
    # `.osw/.osm` is a slash-separated *list* of file patterns (the PRD
    # writes "per-sample `.osw/.osm` + `eplusout.sql`"); treat as prose.
    return bool(token.startswith("."))


def _is_skipped(token: str) -> bool:
    if token.startswith(("http://", "https://")):
        return True
    if token.startswith(("*", "/usr", "/bin", "/etc")):
        return True
    for sub in PATH_SKIP_SUBSTRINGS:
        if sub in token:
            return True
    if _is_documented_pattern(token):
        return True
    # `../foo.md` is a relative path from the docs file; resolve to the
    # repo root, but only if it lands inside the repo.
    if token.startswith("../"):
        return False  # let the resolver check it
    first = token.split("/", 1)[0]
    return first in SKIP_DIRS


def _check_token(token: str) -> str | None:
    """Return None if the token is fine, else an error string describing
    what is wrong."""
    if token.startswith("../"):
        # Relative-from-doc reference. The check is "does the destination
        # exist *somewhere* under the repo root". We resolve by stripping
        # the leading `..` segments; multiple `..`s are all collapsed
        # (we don't track the source file's depth here — that's a future
        # enhancement, see issue #15 follow-ups).
        remainder = "/".join(p for p in token.split("/") if p != "..")
        if not remainder:
            return None
        target = REPO_ROOT / remainder
    else:
        target = REPO_ROOT / token
    if not target.exists():
        return f"references missing path `{token}`"
    return None


def _check_markdown_links(md: Path, text: str) -> list[str]:
    """Validate internal markdown cross-references in ``text``.

    Scans for ``[label](target.md)`` patterns and resolves ``target.md``
    relative to ``md``'s parent directory. Returns a list of error
    strings (empty if all links resolve).
    """
    errors: list[str] = []
    for m in MARKDOWN_LINK_RE.finditer(text):
        target = m.group(1)
        # Strip any trailing anchor fragment.
        if "#" in target:
            target = target.split("#", 1)[0]
        if not target:
            continue
        # Skip external URLs.
        if target.startswith(("http://", "https://", "mailto:", "ftp://")):
            continue
        resolved = (md.parent / target).resolve()
        # Skip targets that resolve outside the repo root (e.g. links
        # from docs/ to .agents/ at the repo root level).
        try:
            resolved.relative_to(REPO_ROOT)
        except ValueError:
            continue
        if not resolved.exists():
            rel_resolved = resolved.relative_to(REPO_ROOT)
            errors.append(
                f"broken internal link `[...]({m.group(1)})` — "
                f"resolved to `{rel_resolved}` which does not exist"
            )
    return errors


def _check_file(md: Path, known_flags: set[str]) -> tuple[list[tuple[Path, str]], bool]:
    """Check a single markdown file for broken references.

    Returns ``(errors, was_checked)`` where *was_checked* is ``False``
    when the file is opted out via ``<!-- docs-skip -->``.
    """
    text = md.read_text()
    if "<!-- docs-skip -->" in text:
        return [], False
    rel = md.relative_to(REPO_ROOT)
    errors: list[tuple[Path, str]] = []

    for m in BACKTICKED_RE.finditer(text):
        token = m.group(1)
        if _is_skipped(token):
            continue
        err = _check_token(token)
        if err:
            errors.append((rel, err))

    # Strict flag resolution (issue #1548): every backticked `--flag`
    # must be a real add_argument on an OSimFlow argparse surface, or an
    # explicitly-exempted foreign flag.
    for m in CLI_FLAG_RE.finditer(text):
        flag = m.group(1)
        if flag in known_flags or flag in FOREIGN_CLI_FLAGS:
            continue
        errors.append(
            (
                rel,
                f"references unknown CLI flag `{flag}` — not an "
                f"`add_argument` in osimflow/__main__.py (or the "
                f"bin/, scripts/, tools/ argparse surfaces). Fix the "
                f"flag name, or add it to FOREIGN_CLI_FLAGS in "
                f"tools/check_docs_sync.py if it belongs to another tool.",
            )
        )

    # bin script names (e.g. plain "extract_kpis.py" without backticks).
    for m in BIN_SCRIPT_RE.finditer(text):
        script = m.group(0)
        target = REPO_ROOT / "bin" / script
        if not target.is_file():
            errors.append((rel, f"references missing bin script `bin/{script}`"))

    # Internal markdown cross-references (issue #191).
    for link_err in _check_markdown_links(md, text):
        errors.append((rel, link_err))

    return errors, True


def main() -> int:
    if not DOCS_DIR.is_dir():
        print(f"ERROR: {DOCS_DIR} not found", file=sys.stderr)
        return 1

    errors: list[tuple[Path, str]] = []
    files_checked = 0
    known_flags = _collect_known_cli_flags()

    for md in sorted(DOCS_DIR.rglob("*.md")):
        file_errors, checked = _check_file(md, known_flags)
        if checked:
            files_checked += 1
        errors.extend(file_errors)

    # User-guide CLI coverage (issue #1547): every __main__.py flag and
    # subcommand must be covered by docs/user-guide.md.
    user_guide = REPO_ROOT / USER_GUIDE_REL_PATH
    main_module = REPO_ROOT / MAIN_MODULE_REL_PATH
    coverage_summary = ""
    if user_guide.is_file() and main_module.is_file():
        main_src = main_module.read_text()
        guide_text = user_guide.read_text()
        # Issue #1575: executor flags moved from __main__.py into the
        # per-executor add_arguments hooks — union both surfaces so
        # user-guide coverage keeps requiring every CLI flag.
        main_flags = _collect_main_cli_flags(main_src) | _collect_executor_hook_cli_flags()
        subcommands = _collect_main_subcommands(main_src)
        rel_guide = user_guide.relative_to(REPO_ROOT)
        for err in _check_user_guide_flag_coverage(main_flags, guide_text):
            errors.append((rel_guide, err))
        for err in _check_user_guide_subcommand_coverage(subcommands, guide_text):
            errors.append((rel_guide, err))
        coverage_summary = (
            f"; user-guide CLI coverage: {len(main_flags)} flags, {len(subcommands)} subcommands"
        )

    if errors:
        print(f"docs/ sync check FAILED ({len(errors)} drift):", file=sys.stderr)
        for path, err in errors:
            print(f"  - {path}: {err}", file=sys.stderr)
        print(
            "\nFix: update the docs to match the current code, or rename "
            "the missing file. Add `<!-- docs-skip -->` to opt a file out.",
            file=sys.stderr,
        )
        return 1

    print(f"docs/ sync check OK ({files_checked} files checked{coverage_summary})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
