"""Tests for the section-scoped AGENTS.md contract checker (issue #1893)."""

import importlib.util
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location(
    "check_agents_contract", REPO_ROOT / "tools" / "check_agents_contract.py"
)
assert _spec is not None and _spec.loader is not None
checker = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(checker)

DOC = "# T\n\n## A thing\nfoo --aws-batch-queue bar\n\n## Directory map (x)\n`work.py`\n"


def test_section_extracts_only_named_body() -> None:
    assert "work.py" in checker._section(DOC, "Directory map")
    assert "work.py" not in checker._section(DOC, "A thing")
    assert checker._section(DOC, "Missing") == ""


def test_whole_token_rejects_prefix_and_embedded_matches() -> None:
    assert not checker._mentions("--aws-batch-queue", "--aws-batch")
    assert not checker._mentions("my_work.py", "work.py")
    assert not checker._mentions("osimflow/work.pyc", "work.py")
    assert checker._mentions("see `--aws-batch`, ok", "--aws-batch")
    assert checker._mentions("`osimflow/work.py` —", "work.py")


def test_term_in_wrong_section_is_reported() -> None:
    scope = checker._section(DOC, "Directory map")
    assert checker._check(scope, ["--aws-batch-queue"], "flag") == ["--aws-batch-queue"]


def test_real_repo_passes() -> None:
    assert checker.main() == 0


def test_reference_doc_has_required_sections() -> None:
    ref = (REPO_ROOT / checker.REFERENCE_DOC).read_text(encoding="utf-8")
    assert checker._section(ref, checker.DIRECTORY_MAP_HEADING).strip()
    assert checker._section(ref, checker.CLI_FLAGS_HEADING).strip()


def test_agents_md_stays_small() -> None:
    assert (REPO_ROOT / "AGENTS.md").stat().st_size < 45_000
