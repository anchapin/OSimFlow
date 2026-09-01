"""Contract tests pinning the run.json docs to monitoring.py (issue #1458).

``docs/monitoring-schema.md`` and ``docs/runjson-guide.md`` are the
canonical references for consumers parsing ``run.json`` programmatically,
but nothing verified they matched the code — so the fields wired in by
recent commits (``alerts_fired`` with ``delivery_status``,
``chaos_schedule``, ``circuit_breaker_states``, ``chaos_invocations``)
were documented nowhere (issue #1458).

These tests parse the field names straight out of the ``RunTrace`` /
``StepTrace`` / ``SampleTrace`` / ``GenerationTrace`` definitions in
``osimflow/monitoring.py`` via regex and assert every field name appears
in both docs — so the next field added to the trace fails here until the
docs are extended. Pure file reads — hermetic, no osimflow imports.

  1. Every StepTrace field is documented       -> test_steptrace_fields_documented
  2. Every SampleTrace field is documented     -> test_sampletrace_fields_documented
  3. Every GenerationTrace field is documented -> test_generationtrace_fields_documented
  4. Every RunTrace field is documented        -> test_runtrace_fields_documented
  5. Issue-#1458 acceptance fields + alerts    -> test_issue_1458_fields_and_alert_entry_keys
"""

import re
from pathlib import Path

import pytest

# Project root is the parent of this package.
REPO_ROOT = Path(__file__).resolve().parents[2]

_MONITORING = REPO_ROOT / "osimflow" / "monitoring.py"
_SCHEMA_DOC = REPO_ROOT / "docs" / "monitoring-schema.md"
_GUIDE_DOC = REPO_ROOT / "docs" / "runjson-guide.md"

# Dataclass field annotation, e.g. `    elapsed_s: float` or
# `    register_values: dict[str, object] | None = None`.
_DATACLASS_FIELD_RE = re.compile(r"^(\s+)([a-z][A-Za-z0-9_]*):\s", re.MULTILINE)

# Annotated instance attribute inside RunTrace.__init__, e.g.
# `        self.chaos_invocations: list[dict[str, object]] = []`.
_RUNTRACE_ATTR_RE = re.compile(r"^\s+self\.([a-z][A-Za-z0-9_]*):\s", re.MULTILINE)

_CLASS_RE = re.compile(r"^class (\w+)")


def _class_source(class_name: str) -> str:
    """Return the source lines of *class_name* in monitoring.py.

    The body extends from the ``class`` line to the next top-level
    ``class`` line (or end of file). Fails loudly if the class moves —
    a silent empty match would defeat the drift guard.
    """
    lines = _MONITORING.read_text(encoding="utf-8").splitlines()
    starts = [i for i, line in enumerate(lines) if _CLASS_RE.match(line)]
    names = {_CLASS_RE.match(lines[i]).group(1): i for i in starts}  # type: ignore[union-attr]
    assert class_name in names, (
        f"class {class_name} not found in {_MONITORING.relative_to(REPO_ROOT)}. "
        "The trace classes moved; update this test to parse the new location."
    )
    start = names[class_name]
    following = [i for i in starts if i > start]
    end = following[0] if following else len(lines)
    return "\n".join(lines[start:end])


def _parse_fields(class_name: str, *, dataclass_style: bool) -> set[str]:
    """Extract field/attribute names from *class_name*'s definition."""
    body = _class_source(class_name)
    regex = _DATACLASS_FIELD_RE if dataclass_style else _RUNTRACE_ATTR_RE
    group = 2 if dataclass_style else 1
    return {m.group(group) for m in regex.finditer(body)}


def _assert_docmented_everywhere(field: str, doc: Path, doc_text: str) -> None:
    assert field in doc_text, (
        f"Field {field!r} (from osimflow/monitoring.py) is missing from "
        f"{doc.relative_to(REPO_ROOT)}. The run.json docs have drifted from "
        "the code — document the field (name, type, when populated, "
        "introducing issue) in both docs/monitoring-schema.md and "
        "docs/runjson-guide.md (issue #1458)."
    )


@pytest.mark.parametrize(
    ("doc", "doc_text"),
    [
        pytest.param(
            _SCHEMA_DOC, _SCHEMA_DOC.read_text(encoding="utf-8"), id="monitoring-schema.md"
        ),
        pytest.param(_GUIDE_DOC, _GUIDE_DOC.read_text(encoding="utf-8"), id="runjson-guide.md"),
    ],
)
class TestTraceFieldsDocumented:
    """Every parsed trace field must appear in each run.json doc."""

    def test_steptrace_fields_documented(self, doc: Path, doc_text: str) -> None:
        fields = sorted(_parse_fields("StepTrace", dataclass_style=True))
        assert fields, "StepTrace fields not parsed — parser regex is stale."
        for field in fields:
            _assert_docmented_everywhere(field, doc, doc_text)

    def test_sampletrace_fields_documented(self, doc: Path, doc_text: str) -> None:
        fields = sorted(_parse_fields("SampleTrace", dataclass_style=True))
        assert fields, "SampleTrace fields not parsed — parser regex is stale."
        for field in fields:
            _assert_docmented_everywhere(field, doc, doc_text)

    def test_generationtrace_fields_documented(self, doc: Path, doc_text: str) -> None:
        fields = sorted(_parse_fields("GenerationTrace", dataclass_style=True))
        assert fields, "GenerationTrace fields not parsed — parser regex is stale."
        for field in fields:
            _assert_docmented_everywhere(field, doc, doc_text)

    def test_runtrace_fields_documented(self, doc: Path, doc_text: str) -> None:
        fields = sorted(_parse_fields("RunTrace", dataclass_style=False))
        # Sanity gate: the fields issue #1458 is about must be among the
        # parsed set, or the parser regex is stale and the guard is blind.
        for must_parse in (
            "chaos_invocations",
            "chaos_schedule",
            "circuit_breaker_states",
            "alerts_fired",
        ):
            assert must_parse in fields, (
                f"RunTrace parser did not pick up {must_parse!r} — "
                "osimflow/monitoring.py changed shape; update the parser."
            )
        for field in fields:
            _assert_docmented_everywhere(field, doc, doc_text)

    def test_issue_1458_fields_and_alert_entry_keys(self, doc: Path, doc_text: str) -> None:
        """Issue #1458 acceptance: the resilience fields and the
        per-alert ``delivery_status`` key are documented in both docs.
        ``delivery_status`` is not a trace field — it is a key inside
        each ``alerts_fired`` entry (from ``osimflow.alerting.Alert``)
        — so it needs its own literal check.
        """
        for literal in (
            "chaos_invocations",
            "chaos_schedule",
            "circuit_breaker_states",
            "alerts_fired",
            "delivery_status",
        ):
            _assert_docmented_everywhere(literal, doc, doc_text)
