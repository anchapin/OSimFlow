"""Unit tests for osimflow.measure_resolver."""

from pathlib import Path

import pytest

from osimflow.measure_resolver import (
    MissingPythonPackage,
    MissingRubyGem,
    _scan_python_file,
    _scan_ruby_file,
    check_dependencies,
    resolve_measure_dependencies,
    resolve_sim_package_dependencies,
    scan_measure_directory,
)


class TestScanRubyFile:
    def test_single_require(self, tmp_path: Path) -> None:
        rb = tmp_path / "measure.rb"
        rb.write_text("require 'json'\nrequire 'ostruct'\n")
        assert _scan_ruby_file(rb) == {"json", "ostruct"}

    def test_double_quoted_require(self, tmp_path: Path) -> None:
        rb = tmp_path / "measure.rb"
        rb.write_text('require "yaml"\n')
        assert _scan_ruby_file(rb) == {"yaml"}

    def test_require_with_path(self, tmp_path: Path) -> None:
        rb = tmp_path / "measure.rb"
        rb.write_text("require 'pathname'\nrequire_relative 'helper'\n")
        assert _scan_ruby_file(rb) == {"pathname"}

    def test_skips_stdlib(self, tmp_path: Path) -> None:
        rb = tmp_path / "measure.rb"
        rb.write_text("require 'rubygems'\nrequire 'bundler'\nrequire 'rake'\n")
        assert _scan_ruby_file(rb) == set()

    def test_comment_not_extracted(self, tmp_path: Path) -> None:
        rb = tmp_path / "measure.rb"
        rb.write_text("# require 'unused'\nrequire 'logger'\n")
        assert _scan_ruby_file(rb) == {"logger"}

    def test_dynamic_require_not_extracted(self, tmp_path: Path) -> None:
        rb = tmp_path / "measure.rb"
        rb.write_text("require(mod_name)\nrequire 'singleton'\n")
        assert _scan_ruby_file(rb) == {"singleton"}

    def test_missing_file_returns_empty(self, tmp_path: Path) -> None:
        assert _scan_ruby_file(tmp_path / "nonexistent.rb") == set()


class TestScanPythonFile:
    def test_simple_import(self, tmp_path: Path) -> None:
        py = tmp_path / "measure.py"
        py.write_text("import json\nimport yaml\n")
        # json and yaml are stdlib - skipped
        assert _scan_python_file(py) == set()

    def test_from_import(self, tmp_path: Path) -> None:
        py = tmp_path / "measure.py"
        py.write_text("from pathlib import Path\nfrom collections import defaultdict\n")
        # pathlib and collections are stdlib - skipped
        assert _scan_python_file(py) == set()

    def test_nested_module(self, tmp_path: Path) -> None:
        py = tmp_path / "measure.py"
        py.write_text("import os.path\nimport xml.etree.ElementTree as ET\n")
        # os and xml are stdlib - skipped
        assert _scan_python_file(py) == set()

    def test_relative_import_skipped(self, tmp_path: Path) -> None:
        py = tmp_path / "measure.py"
        py.write_text("from . import helpers\nfrom ..utils import something\n")
        assert _scan_python_file(py) == set()

    def test_skips_builtins(self, tmp_path: Path) -> None:
        py = tmp_path / "measure.py"
        py.write_text("import os\nimport sys\nimport re\nimport logging\nimport openstudio\n")
        assert _scan_python_file(py) == set()

    def test_comment_not_extracted(self, tmp_path: Path) -> None:
        py = tmp_path / "measure.py"
        py.write_text("# import unused\nimport datetime\n")
        # datetime is stdlib - skipped
        assert _scan_python_file(py) == set()

    def test_missing_file_returns_empty(self, tmp_path: Path) -> None:
        assert _scan_python_file(tmp_path / "nonexistent.py") == set()


class TestScanMeasureDirectory:
    def test_empty_directory(self, tmp_path: Path) -> None:
        measure_dir = tmp_path / "EmptyMeasure"
        measure_dir.mkdir()
        result = scan_measure_directory(measure_dir)
        assert result["ruby"] == set()
        assert result["python"] == set()

    def test_single_measure_rb(self, tmp_path: Path) -> None:
        measure_dir = tmp_path / "TestMeasure"
        measure_dir.mkdir()
        (measure_dir / "measure.rb").write_text("require 'json'\nrequire 'ostruct'\n")
        result = scan_measure_directory(measure_dir)
        assert result["ruby"] == {"json", "ostruct"}
        assert result["python"] == set()

    def test_single_measure_py(self, tmp_path: Path) -> None:
        measure_dir = tmp_path / "TestMeasure"
        measure_dir.mkdir()
        (measure_dir / "measure.py").write_text("import json\nimport numpy as np\n")
        result = scan_measure_directory(measure_dir)
        assert result["ruby"] == set()
        # json is stdlib - skipped; numpy is third-party - detected
        assert result["python"] == {"numpy"}

    def test_nested_helper_files(self, tmp_path: Path) -> None:
        measure_dir = tmp_path / "TestMeasure"
        measure_dir.mkdir()
        (measure_dir / "measure.rb").write_text("require 'json'\n")
        helpers = measure_dir / "helpers"
        helpers.mkdir()
        (helpers / "parser.rb").write_text("require 'yaml'\nrequire 'ostruct'\n")
        result = scan_measure_directory(measure_dir)
        assert result["ruby"] == {"json", "yaml", "ostruct"}

    def test_mixed_rb_and_py(self, tmp_path: Path) -> None:
        measure_dir = tmp_path / "TestMeasure"
        measure_dir.mkdir()
        (measure_dir / "measure.rb").write_text("require 'json'\n")
        (measure_dir / "measure.py").write_text("import json\nimport numpy\n")
        result = scan_measure_directory(measure_dir)
        assert result["ruby"] == {"json"}
        # json is stdlib - skipped; numpy is third-party - detected
        assert result["python"] == {"numpy"}


class TestCheckDependencies:
    def test_all_available(self) -> None:
        missing_r, missing_p = check_dependencies({"json"}, {"json"})
        assert missing_r == []
        assert missing_p == []

    def test_missing_ruby(self) -> None:
        missing_r, missing_p = check_dependencies({"nonexistent_gem_xyz"}, set())
        assert missing_r == ["nonexistent_gem_xyz"]
        assert missing_p == []

    def test_missing_python(self) -> None:
        missing_r, missing_p = check_dependencies(set(), {"nonexistent_pkg_xyz"})
        assert missing_r == []
        assert missing_p == ["nonexistent_pkg_xyz"]


class TestResolveMeasureDependencies:
    def test_no_deps_raises_nothing(self, tmp_path: Path) -> None:
        measure_dir = tmp_path / "NoDepsMeasure"
        measure_dir.mkdir()
        (measure_dir / "measure.rb").write_text("# no deps\n")
        result = resolve_measure_dependencies(measure_dir)
        assert result["measure_name"] == "NoDepsMeasure"
        assert result["missing_ruby"] == []
        assert result["missing_python"] == []

    def test_missing_ruby_raises(self, tmp_path: Path) -> None:
        measure_dir = tmp_path / "MissingDepsMeasure"
        measure_dir.mkdir()
        (measure_dir / "measure.rb").write_text("require 'nonexistent_ruby_gem_xyz'\n")
        with pytest.raises(MissingRubyGem) as exc_info:
            resolve_measure_dependencies(measure_dir)
        assert "nonexistent_ruby_gem_xyz" in str(exc_info.value)
        assert exc_info.value.measure_name == "MissingDepsMeasure"

    def test_missing_python_raises(self, tmp_path: Path) -> None:
        measure_dir = tmp_path / "MissingDepsMeasure"
        measure_dir.mkdir()
        (measure_dir / "measure.py").write_text("import nonexistent_python_pkg_xyz\n")
        with pytest.raises(MissingPythonPackage) as exc_info:
            resolve_measure_dependencies(measure_dir)
        assert "nonexistent_python_pkg_xyz" in str(exc_info.value)
        assert exc_info.value.measure_name == "MissingDepsMeasure"

    def test_auto_install_does_not_raise_when_successful(self, tmp_path: Path) -> None:
        measure_dir = tmp_path / "AutoInstallMeasure"
        measure_dir.mkdir()
        # json is always available, but we test the structure
        (measure_dir / "measure.rb").write_text("require 'json'\n")
        result = resolve_measure_dependencies(measure_dir, auto_install=True)
        assert result["missing_ruby"] == []
        assert result["missing_python"] == []


class TestResolveSimPackageDependencies:
    def test_no_measures_dir(self, tmp_path: Path) -> None:
        pkg = tmp_path / "pkg"
        pkg.mkdir()
        results = resolve_sim_package_dependencies(pkg)
        assert results == []

    def test_empty_measures_dir(self, tmp_path: Path) -> None:
        pkg = tmp_path / "pkg"
        pkg.mkdir()
        (pkg / "measures").mkdir()
        results = resolve_sim_package_dependencies(pkg)
        assert results == []

    def test_multiple_measures(self, tmp_path: Path) -> None:
        pkg = tmp_path / "pkg"
        pkg.mkdir()
        measures = pkg / "measures"
        measures.mkdir()

        m1 = measures / "MeasureA"
        m1.mkdir()
        (m1 / "measure.rb").write_text("require 'json'\n")

        m2 = measures / "MeasureB"
        m2.mkdir()
        (m2 / "measure.py").write_text("import json\n")

        results = resolve_sim_package_dependencies(pkg)
        assert len(results) == 2
        names = {r["measure_name"] for r in results}
        assert names == {"MeasureA", "MeasureB"}
