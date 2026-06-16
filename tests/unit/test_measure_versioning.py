"""Tests for osimflow.measure_versioning (issue #430)."""

from __future__ import annotations

from pathlib import Path

import pytest

from osimflow.measure_versioning import (
    MeasureVersioningError,
    compare_measure_versions,
    detect_measure_version,
    installed_versions_from_json,
    list_campaign_measure_versions,
    read_measure_versions,
    scan_measure_versions,
    write_measure_versions,
)

# ---------------------------------------------------------------------------
# Test data helpers
# ---------------------------------------------------------------------------

RUBY_MEASURE_V1 = '''
# *******************************************************************************
# Measures Name: TestMeasure
# Description: A test measure
# Argument Infomation:
#   name, type, default, required, display_name, tooltip, note
#   test_arg, Double, 0.5, false, Test Arg, A test argument, none
# Version:
#   version_id: "1.2.3"
# *******************************************************************************

require_relative 'measure.rb'

class TestMeasure < OpenStudio::Measure::ModelMeasure
  def run(model, runner, user_arguments)
    # ...
  end
end
'''

RUBY_MEASURE_NO_VERSION = '''
# *******************************************************************************
# Measures Name: NoVersionMeasure
# Description: A measure without a version
# Version:
# *******************************************************************************

require_relative 'measure.rb'

class NoVersionMeasure < OpenStudio::Measure::ModelMeasure
  def run(model, runner, user_arguments)
    # ...
  end
end
'''

PYTHON_MEASURE_V2 = '''
#!/usr/bin/env openstudio measure
"""A Python test measure.

Measure Information:
    name:        PythonTestMeasure
    description: A Python test measure
    version_id:  2.0.1
    arguments:
        - name: test_arg
          type: Double
          default: 0.5
          required: false
"""


class PythonTestMeasure(openstudio.measure.OSMeasure):
    def name(self):
        return "PythonTestMeasure"

    def run(self, model, runner, user_arguments):
        # ...
        pass
'''

PYTHON_MEASURE_NO_VERSION = '''
#!/usr/bin/env openstudio measure
"""A Python test measure without version.

Measure Information:
    name:        PythonNoVersionMeasure
    description: A measure without a version
"""


class PythonNoVersionMeasure(openstudio.measure.OSMeasure):
    def name(self):
        return "PythonNoVersionMeasure"

    def run(self, model, runner, user_arguments):
        pass
'''

RUBY_MEASURE_EDGE_CASES = '''
# Version: version_id = "0.1.0-beta"
# Another field: version_id: '1.2.3'
version_id: "3.0.0"
'''


# ---------------------------------------------------------------------------
# detect_measure_version tests
# ---------------------------------------------------------------------------

class TestDetectMeasureVersion:
    """Tests for detect_measure_version."""

    def test_detect_ruby_measure_with_version(self, tmp_path: Path) -> None:
        measure_dir = tmp_path / "TestMeasure"
        measure_dir.mkdir()
        (measure_dir / "measure.rb").write_text(RUBY_MEASURE_V1, encoding="utf-8")

        mv = detect_measure_version(measure_dir)

        assert mv.name == "TestMeasure"
        assert mv.version == "1.2.3"
        assert mv.language == "ruby"
        assert mv.path == measure_dir

    def test_detect_python_measure_with_version(self, tmp_path: Path) -> None:
        measure_dir = tmp_path / "PythonTestMeasure"
        measure_dir.mkdir()
        (measure_dir / "measure.py").write_text(PYTHON_MEASURE_V2, encoding="utf-8")

        mv = detect_measure_version(measure_dir)

        assert mv.name == "PythonTestMeasure"
        assert mv.version == "2.0.1"
        assert mv.language == "python"
        assert mv.path == measure_dir

    def test_detect_ruby_measure_unknown_version(self, tmp_path: Path) -> None:
        measure_dir = tmp_path / "NoVersionMeasure"
        measure_dir.mkdir()
        (measure_dir / "measure.rb").write_text(RUBY_MEASURE_NO_VERSION, encoding="utf-8")

        mv = detect_measure_version(measure_dir)

        assert mv.name == "NoVersionMeasure"
        assert mv.version == "unknown"
        assert mv.language == "ruby"

    def test_detect_python_measure_unknown_version(self, tmp_path: Path) -> None:
        measure_dir = tmp_path / "PythonNoVersionMeasure"
        measure_dir.mkdir()
        (measure_dir / "measure.py").write_text(PYTHON_MEASURE_NO_VERSION, encoding="utf-8")

        mv = detect_measure_version(measure_dir)

        assert mv.name == "PythonNoVersionMeasure"
        assert mv.version == "unknown"
        assert mv.language == "python"

    def test_detect_raises_when_no_entry_point(self, tmp_path: Path) -> None:
        measure_dir = tmp_path / "EmptyMeasure"
        measure_dir.mkdir()

        with pytest.raises(MeasureVersioningError, match="No measure.rb or measure.py"):
            detect_measure_version(measure_dir)

    def test_detect_prefers_ruby_over_python(self, tmp_path: Path) -> None:
        measure_dir = tmp_path / "BothMeasures"
        measure_dir.mkdir()
        (measure_dir / "measure.rb").write_text(RUBY_MEASURE_V1, encoding="utf-8")
        (measure_dir / "measure.py").write_text(PYTHON_MEASURE_V2, encoding="utf-8")

        mv = detect_measure_version(measure_dir)

        assert mv.language == "ruby"

    def test_detect_ruby_edge_cases(self, tmp_path: Path) -> None:
        measure_dir = tmp_path / "EdgeCaseMeasure"
        measure_dir.mkdir()
        (measure_dir / "measure.rb").write_text(RUBY_MEASURE_EDGE_CASES, encoding="utf-8")

        mv = detect_measure_version(measure_dir)

        assert mv.version == "0.1.0-beta"


# ---------------------------------------------------------------------------
# scan_measure_versions tests
# ---------------------------------------------------------------------------

class TestScanMeasureVersions:
    """Tests for scan_measure_versions."""

    def test_scan_empty_package(self, tmp_path: Path) -> None:
        result = scan_measure_versions(tmp_path)
        assert result == []

    def test_scan_no_measures_dir(self, tmp_path: Path) -> None:
        result = scan_measure_versions(tmp_path)
        assert result == []

    def test_scan_multiple_measures(self, tmp_path: Path) -> None:
        pkg = tmp_path / "package"
        pkg.mkdir()
        measures = pkg / "measures"
        measures.mkdir()

        m1 = measures / "MeasureA"
        m1.mkdir()
        (m1 / "measure.rb").write_text(
            '# version_id: "1.0.0"\nclass M1; end', encoding="utf-8"
        )

        m2 = measures / "MeasureB"
        m2.mkdir()
        (m2 / "measure.py").write_text(
            '"""version_id: 2.0.0"""\nclass M2; end', encoding="utf-8"
        )

        result = scan_measure_versions(pkg)

        assert len(result) == 2
        names = {r.name for r in result}
        assert names == {"MeasureA", "MeasureB"}
        versions = {r.name: r.version for r in result}
        assert versions["MeasureA"] == "1.0.0"
        assert versions["MeasureB"] == "2.0.0"

    def test_scan_skips_empty_measure_dirs(self, tmp_path: Path) -> None:
        pkg = tmp_path / "package"
        pkg.mkdir()
        measures = pkg / "measures"
        measures.mkdir()

        m1 = measures / "ValidMeasure"
        m1.mkdir()
        (m1 / "measure.rb").write_text('# version_id: "1.0.0"\nclass M1; end', encoding="utf-8")

        empty = measures / "EmptyDir"
        empty.mkdir()

        result = scan_measure_versions(pkg)
        assert len(result) == 1
        assert result[0].name == "ValidMeasure"


# ---------------------------------------------------------------------------
# compare_measure_versions tests
# ---------------------------------------------------------------------------

class TestCompareMeasureVersions:
    """Tests for compare_measure_versions."""

    def test_all_match(self) -> None:
        required = {"A": "1.0.0", "B": "2.0.0"}
        installed = {"A": "1.0.0", "B": "2.0.0"}

        mismatches = compare_measure_versions(required, installed)

        assert mismatches == []

    def test_version_mismatch(self) -> None:
        required = {"A": "1.0.0", "B": "2.0.0"}
        installed = {"A": "1.0.0", "B": "3.0.0"}

        mismatches = compare_measure_versions(required, installed)

        assert len(mismatches) == 1
        assert mismatches[0].measure_name == "B"
        assert mismatches[0].required_version == "2.0.0"
        assert mismatches[0].installed_version == "3.0.0"

    def test_unknown_installed_version(self) -> None:
        required = {"A": "1.0.0"}
        installed: dict[str, str] = {}

        mismatches = compare_measure_versions(required, installed)

        assert len(mismatches) == 1
        assert mismatches[0].measure_name == "A"
        assert mismatches[0].required_version == "1.0.0"
        assert mismatches[0].installed_version == "unknown"

    def test_extra_installed_measure_ignored(self) -> None:
        required: dict[str, str] = {}
        installed = {"Extra": "1.0.0"}

        mismatches = compare_measure_versions(required, installed)

        assert mismatches == []

    def test_multiple_mismatches(self) -> None:
        required = {"A": "1.0.0", "B": "2.0.0", "C": "3.0.0"}
        installed = {"A": "1.0.0", "B": "9.9.9", "C": "unknown"}

        mismatches = compare_measure_versions(required, installed)

        assert len(mismatches) == 2
        names = {m.measure_name for m in mismatches}
        assert names == {"B", "C"}


# ---------------------------------------------------------------------------
# write / read / list tests
# ---------------------------------------------------------------------------

class TestMeasureVersionFileOperations:
    """Tests for write_measure_versions and read_measure_versions."""

    def test_write_and_read_roundtrip(self, tmp_path: Path) -> None:
        measure_dir = tmp_path / "TestMeasure"
        measure_dir.mkdir()
        (measure_dir / "measure.rb").write_text(RUBY_MEASURE_V1, encoding="utf-8")

        mv = detect_measure_version(measure_dir)
        out_path = write_measure_versions(tmp_path, [mv])

        assert out_path == tmp_path / "measure_versions.json"
        assert out_path.is_file()

        records = read_measure_versions(out_path)
        assert len(records) == 1
        assert records[0]["name"] == "TestMeasure"
        assert records[0]["version"] == "1.2.3"
        assert records[0]["language"] == "ruby"

    def test_read_missing_file_returns_empty(self, tmp_path: Path) -> None:
        result = read_measure_versions(tmp_path / "nonexistent.json")
        assert result == []

    def test_installed_versions_from_json(self, tmp_path: Path) -> None:
        measure_dir = tmp_path / "TestMeasure"
        measure_dir.mkdir()
        (measure_dir / "measure.rb").write_text(RUBY_MEASURE_V1, encoding="utf-8")

        mv = detect_measure_version(measure_dir)
        write_measure_versions(tmp_path, [mv])

        path = tmp_path / "measure_versions.json"
        versions = installed_versions_from_json(path)

        assert versions == {"TestMeasure": "1.2.3"}

    def test_list_campaign_measure_versions(self, tmp_path: Path) -> None:
        measure_dir = tmp_path / "TestMeasure"
        measure_dir.mkdir()
        (measure_dir / "measure.rb").write_text(RUBY_MEASURE_V1, encoding="utf-8")

        mv = detect_measure_version(measure_dir)
        write_measure_versions(tmp_path, [mv])

        records = list_campaign_measure_versions(tmp_path)

        assert len(records) == 1
        assert records[0]["name"] == "TestMeasure"
        assert records[0]["version"] == "1.2.3"

    def test_list_campaign_measure_versions_no_file(self, tmp_path: Path) -> None:
        records = list_campaign_measure_versions(tmp_path)
        assert records == []
