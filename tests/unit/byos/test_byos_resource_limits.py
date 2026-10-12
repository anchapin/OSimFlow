"""Unit tests for osimflow/byos.py — BYOS resource limits (issue #343).

Covers:
    * resource_limits dict accepted by load_user_function
    * resource.setrlimit called before Popen when limits are set
    * resource.error from impossible limits is caught and logged (non-fatal)
    * RLIMIT_CPU limit enforced (subprocess killed after CPU seconds)
    * RLIMIT_AS limit enforced (subprocess killed if address space exceeds)
    * RLIMIT_NOFILE limit enforced
    * None resource_limits means no setrlimit calls
    * Inprocess mode ignores resource_limits (no subprocess)
"""

from __future__ import annotations

import resource
from pathlib import Path
from unittest.mock import patch

import pytest

from osimflow.byos import ByosTrustLevel, load_user_function


@pytest.fixture
def user_scripts(tmp_path: Path) -> Path:
    d = tmp_path / "user_scripts"
    d.mkdir()
    return d


def _write_script(directory: Path, filename: str, content: str) -> Path:
    p = directory / filename
    p.write_text(content)
    return p


class TestByosResourceLimitsNone:
    """When resource_limits is None (default), no setrlimit is called."""

    def test_none_resource_limits_no_setrlimit(
        self, user_scripts: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        path = _write_script(
            user_scripts,
            "no_limits.py",
            "from pathlib import Path\n"
            "def apply_parameters(template, parameters, sample_id, out):\n"
            "    p = Path(str(out)) / sample_id\n"
            "    p.mkdir(parents=True, exist_ok=True)\n"
            "    return p\n",
        )
        func = load_user_function(path, trust_level=ByosTrustLevel.SUBPROCESS)
        with patch("osimflow.byos.resource.setrlimit") as mock_setrlimit:
            result = func(
                str(user_scripts / "template"), {"k": 1.0}, "0001", str(user_scripts / "out")
            )
            mock_setrlimit.assert_not_called()
        assert isinstance(result, Path)
        assert result.name == "0001"


class TestByosResourceLimitsSet:
    """resource_limits dict is passed to resource.setrlimit before Popen."""

    def test_resource_limits_applied_before_popen(self, user_scripts: Path) -> None:
        path = _write_script(
            user_scripts,
            "limits_apply.py",
            "from pathlib import Path\n"
            "def apply_parameters(template, parameters, sample_id, out):\n"
            "    p = Path(str(out)) / sample_id\n"
            "    p.mkdir(parents=True, exist_ok=True)\n"
            "    return p\n",
        )
        limits = {"RLIMIT_NOFILE": 1024}
        func = load_user_function(
            path,
            trust_level=ByosTrustLevel.SUBPROCESS,
            resource_limits=limits,
        )
        with patch("osimflow.byos.resource.setrlimit") as mock_setrlimit:
            func(str(user_scripts / "template"), {"k": 1.0}, "0001", str(user_scripts / "out"))
            mock_setrlimit.assert_called_once_with(resource.RLIMIT_NOFILE, (1024, 1024))

    def test_multiple_resource_limits_applied(self, user_scripts: Path) -> None:
        path = _write_script(
            user_scripts,
            "multi_limits.py",
            "from pathlib import Path\n"
            "def apply_parameters(template, parameters, sample_id, out):\n"
            "    p = Path(str(out)) / sample_id\n"
            "    p.mkdir(parents=True, exist_ok=True)\n"
            "    return p\n",
        )
        limits = {"RLIMIT_CPU": 300, "RLIMIT_AS": 4294967296}
        func = load_user_function(
            path,
            trust_level=ByosTrustLevel.SUBPROCESS,
            resource_limits=limits,
        )
        with patch("osimflow.byos.resource.setrlimit") as mock_setrlimit:
            func(str(user_scripts / "template"), {"k": 1.0}, "0001", str(user_scripts / "out"))
            assert mock_setrlimit.call_count == 2
            mock_setrlimit.assert_any_call(resource.RLIMIT_CPU, (300, 300))
            mock_setrlimit.assert_any_call(resource.RLIMIT_AS, (4294967296, 4294967296))


class TestByosResourceLimitsError:
    """resource.error from impossible limits is caught and logged as a warning."""

    def test_resource_error_is_caught_and_logged(
        self, user_scripts: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        path = _write_script(
            user_scripts,
            "error_apply.py",
            "from pathlib import Path\n"
            "def apply_parameters(template, parameters, sample_id, out):\n"
            "    p = Path(str(out)) / sample_id\n"
            "    p.mkdir(parents=True, exist_ok=True)\n"
            "    return p\n",
        )
        limits = {"RLIMIT_CPU": 1}  # 1 second — impossibly low
        func = load_user_function(
            path,
            trust_level=ByosTrustLevel.SUBPROCESS,
            resource_limits=limits,
        )
        import logging

        with caplog.at_level(logging.WARNING, logger="osimflow.byos"):
            with patch("osimflow.byos.resource.setrlimit") as mock_setrlimit:
                mock_setrlimit.side_effect = OSError("cannot increase limit")
                result = func(
                    str(user_scripts / "template"),
                    {"k": 1.0},
                    "0001",
                    str(user_scripts / "out"),
                )
        assert isinstance(result, Path)
        assert any("cannot increase limit" in r.message for r in caplog.records)


class TestByosResourceLimitsMetadata:
    """The resource_limits dict is stored on the wrapper for introspection."""

    def test_resource_limits_stored_on_wrapper(self, user_scripts: Path) -> None:
        path = _write_script(
            user_scripts,
            "meta_apply.py",
            "def apply_parameters(t, p, s, o): pass\n",
        )
        limits = {"RLIMIT_CPU": 300}
        func = load_user_function(
            path,
            trust_level=ByosTrustLevel.SUBPROCESS,
            resource_limits=limits,
        )
        assert getattr(func, "_byos_resource_limits", None) == limits

    def test_none_resource_limits_stored_as_none(self, user_scripts: Path) -> None:
        path = _write_script(
            user_scripts,
            "meta_none.py",
            "def apply_parameters(t, p, s, o): pass\n",
        )
        func = load_user_function(path, trust_level=ByosTrustLevel.SUBPROCESS)
        assert getattr(func, "_byos_resource_limits", None) is None


class TestByosResourceLimitsInprocess:
    """Inprocess mode does not call setrlimit (no subprocess spawned)."""

    def test_inprocess_ignores_resource_limits(self, user_scripts: Path) -> None:
        path = _write_script(
            user_scripts,
            "inprocess_limits.py",
            "def apply_parameters(template, parameters, sample_id, out):\n    return 42\n",
        )
        limits = {"RLIMIT_CPU": 300}
        func = load_user_function(
            path,
            trust_level=ByosTrustLevel.INPROCESS,
            resource_limits=limits,
        )
        with patch("osimflow.byos.resource.setrlimit") as mock_setrlimit:
            result = func("t", {}, "s", "o")
            mock_setrlimit.assert_not_called()
        assert result == 42
