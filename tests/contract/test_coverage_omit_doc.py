"""Contract tests pinning the coverage ``omit`` list to docs/DEVELOPMENT.md (issue #1452).

Issue #1452 found that blanket module omissions in
``[tool.coverage.run] omit`` silently shrink what the 82% gate covers:
``storage.py`` (including the security-critical https-only endpoint
validation from #1386) and ``taskqueue.py`` were invisible to the gate
despite being in-process tested. The storage/taskqueue omissions are
removed by this change; the remaining omissions now require a
documented rationale in the "Coverage omissions" section of
``docs/DEVELOPMENT.md``.

These tests parse both files directly — pure file reads, hermetic and
fast. A future ``omit`` addition without a matching doc bullet (or a
doc bullet whose path is no longer omitted) fails here, so the
omission-to-rationale link cannot silently drift again.

  1. storage.py / taskqueue.py stay measured        -> test_storage_and_taskqueue_are_measured
  2. every omitted entry has a documented rationale -> test_every_omit_entry_is_documented
  3. every documented entry is still omitted        -> test_every_documented_omission_is_in_omit_list
"""

import tomllib
from pathlib import Path

# Project root is the parent of this package.
REPO_ROOT = Path(__file__).resolve().parents[2]

_PYPROJECT = REPO_ROOT / "pyproject.toml"
_DEV_DOCS = REPO_ROOT / "docs" / "DEVELOPMENT.md"

_OMIT_HEADING = "### Coverage omissions"

_MEASURED_MODULES = ("osimflow/storage.py", "osimflow/taskqueue.py")


def _parse_omit_list() -> list[str]:
    """Extract the ``[tool.coverage.run] omit`` list from pyproject.toml."""
    with _PYPROJECT.open("rb") as fh:
        data = tomllib.load(fh)
    omit = data["tool"]["coverage"]["run"]["omit"]
    assert isinstance(omit, list) and omit, (
        "No [tool.coverage.run] omit list found in pyproject.toml. "
        "The coverage config moved; update this test to parse the new location."
    )
    return [str(entry) for entry in omit]


def _parse_documented_omissions() -> list[str]:
    """Extract the omitted paths listed in DEVELOPMENT.md's omissions section.

    Only bullet lines of the form ``- ``osimflow/...`` — reason`` count;
    prose references (e.g. the sentence noting that storage.py and
    taskqueue.py were un-omitted) are ignored.
    """
    text = _DEV_DOCS.read_text(encoding="utf-8")
    start = text.find(_OMIT_HEADING)
    assert start >= 0, (
        f"docs/DEVELOPMENT.md has no {_OMIT_HEADING!r} section. "
        "The coverage-omission rationale moved; update this test to parse "
        "the new location (issue #1452)."
    )
    section = text[start + len(_OMIT_HEADING) :]
    next_heading = section.find("\n### ")
    if next_heading >= 0:
        section = section[:next_heading]

    entries: list[str] = []
    for line in section.splitlines():
        stripped = line.strip()
        if stripped.startswith("- `osimflow/"):
            entries.append(stripped[2:].lstrip("`").split("`", 1)[0])
    assert entries, (
        f"The {_OMIT_HEADING!r} section in docs/DEVELOPMENT.md lists no "
        "omitted paths. Restore the per-entry rationale bullets (issue #1452)."
    )
    return entries


def test_storage_and_taskqueue_are_measured() -> None:
    """The issue #1452 acceptance: storage.py and taskqueue.py stay measured.

    A future re-omission of these security/logic-critical modules fails
    here immediately instead of silently shrinking the 82% gate again.
    """
    omit = _parse_omit_list()
    for module in _MEASURED_MODULES:
        assert module not in omit, (
            f"{module} is omitted from coverage again (issue #1452 regression). "
            "Both modules are in-process tested — including the https-only "
            "storage-endpoint validation from #1386 — and must stay measured. "
            "Remove the entry from pyproject.toml [tool.coverage.run] omit."
        )


def test_every_omit_entry_is_documented() -> None:
    """Every entry in the omit list must have a rationale bullet in the docs."""
    omit = _parse_omit_list()
    documented = set(_parse_documented_omissions())
    missing = [entry for entry in omit if entry not in documented]
    assert not missing, (
        f"pyproject.toml omits {missing} but docs/DEVELOPMENT.md's "
        f"{_OMIT_HEADING!r} section has no matching bullet. Document the "
        "rationale (issue #1452 contract)."
    )


def test_every_documented_omission_is_in_omit_list() -> None:
    """Doc bullets must not list paths that are no longer omitted (stale-doc guard)."""
    omit = set(_parse_omit_list())
    stale = [entry for entry in _parse_documented_omissions() if entry not in omit]
    assert not stale, (
        f"docs/DEVELOPMENT.md documents omissions for {stale} but they are "
        "no longer in the pyproject.toml [tool.coverage.run] omit list. "
        "Drop the stale bullets."
    )
