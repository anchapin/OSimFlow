"""Unit tests for osimflow.measure_resolver."""

import os
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


class TestDependencyProbesAreSandboxed:
    """Issue #1970: availability probes must not execute untrusted code."""

    def test_unsafe_names_rejected_without_subprocess(self) -> None:
        from unittest import mock

        from osimflow import measure_resolver as mr

        with mock.patch.object(mr.subprocess, "run") as run:
            assert mr._check_ruby_gem("x'; system('id') #") is False
            assert mr._check_python_package("a b") is False
            assert mr._check_python_package("--help") is False
        run.assert_not_called()

    def test_probes_use_sanitized_env_and_no_require(self, monkeypatch) -> None:
        from unittest import mock

        from osimflow import measure_resolver as mr

        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "shh")
        monkeypatch.setattr(mr.sys, "platform", "linux")
        with mock.patch.object(mr.subprocess, "run") as run:
            run.return_value.returncode = 0
            assert mr._check_ruby_gem("json") is True
            assert mr._check_python_package("a.b") is True
        for call in run.call_args_list:
            assert "AWS_SECRET_ACCESS_KEY" not in call.kwargs["env"]
        ruby_cmd = run.call_args_list[0].args[0]
        assert "require" not in ruby_cmd[2]
        assert ruby_cmd[3] == "json"


class TestProbeResourceLimits:
    """Issue #1996: probe subprocesses run with a timeout and rlimits."""

    @pytest.fixture(autouse=True)
    def _non_darwin(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from osimflow import measure_resolver as mr

        monkeypatch.setattr(mr.sys, "platform", "linux")

    def test_probe_passes_timeout_and_preexec(self) -> None:
        import os
        from unittest import mock

        from osimflow import measure_resolver as mr

        with mock.patch.object(mr.subprocess, "run") as run:
            run.return_value.returncode = 0
            assert mr._run_probe(["true"]) is True
        assert run.call_args.kwargs["timeout"] == mr._PROBE_TIMEOUT_S
        if os.name == "posix":
            assert run.call_args.kwargs["preexec_fn"] is mr._apply_probe_rlimits

    def test_timeout_returns_false(self) -> None:
        import subprocess
        from unittest import mock

        from osimflow import measure_resolver as mr

        err = subprocess.TimeoutExpired(cmd="x", timeout=1)
        with mock.patch.object(mr.subprocess, "run", side_effect=err):
            assert mr._run_probe(["x"]) is False

    @pytest.mark.skipif(os.name != "posix", reason="rlimits are POSIX-only")
    def test_rlimits_applied_in_child(self) -> None:
        import subprocess
        import sys

        from osimflow import measure_resolver as mr

        code = "import resource; print(resource.getrlimit(resource.RLIMIT_CPU)[0], resource.getrlimit(resource.RLIMIT_FSIZE)[0])"
        out = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            check=True,
            preexec_fn=mr._apply_probe_rlimits,
        )
        cpu, fsize = map(int, out.stdout.split())
        assert cpu <= mr._PROBE_CPU_LIMIT_S
        assert fsize <= mr._PROBE_FILE_SIZE_LIMIT_BYTES


class TestMacOSRssWatchdog:
    """Issue #2011: RSS watchdog replaces RLIMIT_AS on macOS probes."""

    def test_darwin_uses_watchdog(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from unittest import mock

        from osimflow import measure_resolver as mr

        monkeypatch.setattr(mr.sys, "platform", "darwin")
        with mock.patch.object(mr, "_run_probe_with_rss_watchdog", return_value=True) as wd:
            assert mr._run_probe(["x"]) is True
        wd.assert_called_once_with(["x"])

    def test_watchdog_success(self) -> None:
        import sys

        from osimflow import measure_resolver as mr

        assert mr._run_probe_with_rss_watchdog([sys.executable, "-c", "pass"])
        assert not mr._run_probe_with_rss_watchdog([sys.executable, "-c", "raise SystemExit(1)"])

    @pytest.mark.skipif(os.name != "posix", reason="uses ps")
    def test_watchdog_kills_over_limit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import sys

        from osimflow import measure_resolver as mr

        monkeypatch.setattr(mr, "_PROBE_MEMORY_LIMIT_BYTES", 1)
        monkeypatch.setattr(mr, "_PROBE_WATCHDOG_INTERVAL_S", 0.01)
        assert not mr._run_probe_with_rss_watchdog(
            [sys.executable, "-c", "import time; time.sleep(20)"]
        )

    @pytest.mark.skipif(os.name != "posix", reason="uses ps")
    def test_watchdog_kills_on_timeout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import sys

        from osimflow import measure_resolver as mr

        monkeypatch.setattr(mr, "_PROBE_TIMEOUT_S", 0.3)
        monkeypatch.setattr(mr, "_PROBE_WATCHDOG_INTERVAL_S", 0.01)
        assert not mr._run_probe_with_rss_watchdog(
            [sys.executable, "-c", "import time; time.sleep(20)"]
        )

    def test_rss_bytes_handles_failure(self) -> None:
        from unittest import mock

        from osimflow import measure_resolver as mr

        with (
            mock.patch.object(mr, "_psutil_tree_rss", return_value=None),
            mock.patch.object(mr.subprocess, "run", side_effect=OSError),
        ):
            assert mr._rss_bytes(1) is None

    def test_ps_tree_rss_sums_descendants(self) -> None:
        from unittest import mock

        from osimflow import measure_resolver as mr

        table = "10 1 100\n11 10 200\n12 11 300\n13 1 999\nbad line\n"
        fake = mock.Mock(stdout=table)
        with mock.patch.object(mr.subprocess, "run", return_value=fake) as run:
            assert mr._ps_tree_rss(10) == 600 * 1024
            assert mr._ps_tree_rss(99) is None
        assert run.call_args.args[0][:2] == ["ps", "-A"]

    def test_ps_tree_rss_subprocess_failure(self) -> None:
        from unittest import mock

        from osimflow import measure_resolver as mr

        with mock.patch.object(mr.subprocess, "run", side_effect=OSError):
            assert mr._ps_tree_rss(1) is None

    def test_rss_bytes_prefers_psutil_then_ps(self) -> None:
        from unittest import mock

        from osimflow import measure_resolver as mr

        with (
            mock.patch.object(mr, "_psutil_tree_rss", return_value=5),
            mock.patch.object(mr, "_ps_tree_rss") as ps,
        ):
            assert mr._rss_bytes(1) == 5
            ps.assert_not_called()
        with (
            mock.patch.object(mr, "_psutil_tree_rss", return_value=None),
            mock.patch.object(mr, "_ps_tree_rss", return_value=7),
        ):
            assert mr._rss_bytes(1) == 7

    def test_psutil_tree_rss_missing_returns_none(self) -> None:
        from unittest import mock

        from osimflow import measure_resolver as mr

        with mock.patch.object(mr.importlib, "import_module", side_effect=ImportError):
            assert mr._psutil_tree_rss(1) is None

    def test_psutil_tree_rss_sums_children(self) -> None:
        from types import SimpleNamespace
        from unittest import mock

        from osimflow import measure_resolver as mr

        class _Err(Exception):
            pass

        def _proc(rss: int | None) -> mock.Mock:
            m = mock.Mock()
            if rss is None:
                m.memory_info.side_effect = _Err
            else:
                m.memory_info.return_value = SimpleNamespace(rss=rss)
            return m

        root = _proc(10)
        root.children.return_value = [_proc(20), _proc(None), _proc(30)]
        fake = SimpleNamespace(Error=_Err, Process=mock.Mock(return_value=root))
        with mock.patch.object(mr.importlib, "import_module", return_value=fake):
            assert mr._psutil_tree_rss(1) == 60
            fake.Process.side_effect = _Err
            assert mr._psutil_tree_rss(1) is None

    @pytest.mark.skipif(os.name != "posix", reason="uses ps")
    def test_ps_tree_rss_counts_grandchild(self) -> None:
        import subprocess
        import sys
        import threading
        import time

        from osimflow import measure_resolver as mr

        tick = threading.Event()
        code = (
            "import subprocess,sys,time;"
            "subprocess.Popen([sys.executable,'-c',"
            "'x=bytearray(60*2**20);import time;time.sleep(20)']);"
            "time.sleep(20)"
        )
        proc = subprocess.Popen([sys.executable, "-c", code])
        try:
            deadline = time.monotonic() + 10
            rss = 0
            while time.monotonic() < deadline:
                rss = mr._ps_tree_rss(proc.pid) or 0
                if rss > 60 * 2**20:
                    break
                tick.wait(0.2)
            assert rss > 60 * 2**20
        finally:
            proc.kill()
            proc.wait()
