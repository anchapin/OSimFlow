"""Unit tests for osimflow/version_detection.py (issue #412).

Covers:
- VersionDetectionError: exception class
- detect_openstudio_version: env var, CLI, docker label, not found
- get_compatible_container_tag: tag formatting
- verify_version_compatibility: same, minor mismatch, major mismatch
- _parse_version_tuple: valid and invalid inputs
- _parse_version_output: various OpenStudio CLI output formats
"""

import os
import subprocess
from unittest.mock import patch

import pytest

from osimflow.version_detection import (  # noqa: E402
    VersionDetectionError,
    _check_docker_container_label,
    _check_env_variable,
    _check_openstudio_cli,
    _is_running_in_docker,
    _parse_version_output,
    _parse_version_tuple,
    detect_openstudio_version,
    get_compatible_container_tag,
    verify_version_compatibility,
)  # noqa: E402


class TestVersionDetectionError:
    def test_is_exception(self) -> None:
        exc = VersionDetectionError("test message")
        assert isinstance(exc, Exception)
        assert str(exc) == "test message"


class TestParseVersionTuple:
    def test_valid_version(self) -> None:
        assert _parse_version_tuple("3.11.0") == (3, 11, 0)
        assert _parse_version_tuple("3.7.0") == (3, 7, 0)
        assert _parse_version_tuple("4.0.1") == (4, 0, 1)

    def test_valid_with_extra_suffix(self) -> None:
        assert _parse_version_tuple("3.11.0-rc1+abc123") == (3, 11, 0)

    def test_invalid_version(self) -> None:
        with pytest.raises(ValueError, match="invalid version string"):
            _parse_version_tuple("not-a-version")
        with pytest.raises(ValueError, match="invalid version string"):
            _parse_version_tuple("3.11")
        with pytest.raises(ValueError, match="invalid version string"):
            _parse_version_tuple("")


class TestParseVersionOutput:
    def test_standard_output(self) -> None:
        assert _parse_version_output("OpenStudio 3.11.0-rc1+abc123") == "3.11.0"

    def test_simple_output(self) -> None:
        assert _parse_version_output("OpenStudio 3.7.0") == "3.7.0"

    def test_lowercase(self) -> None:
        assert _parse_version_output("openstudio 3.11.0") == "3.11.0"

    def test_only_version_number(self) -> None:
        assert _parse_version_output("3.11.0") == "3.11.0"

    def test_no_version(self) -> None:
        assert _parse_version_output("some random output") is None
        assert _parse_version_output("") is None


class TestCheckEnvVariable:
    def test_env_var_set(self) -> None:
        with patch.dict(os.environ, {"OPENSTUDIO_VERSION": "3.11.0"}):
            assert _check_env_variable() == "3.11.0"

    def test_env_var_set_with_whitespace(self) -> None:
        with patch.dict(os.environ, {"OPENSTUDIO_VERSION": "  3.11.0  "}):
            assert _check_env_variable() == "3.11.0"

    def test_env_var_not_set(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            assert _check_env_variable() is None

    def test_env_var_empty(self) -> None:
        with patch.dict(os.environ, {"OPENSTUDIO_VERSION": ""}):
            assert _check_env_variable() is None


class TestCheckOpenstudioCli:
    def test_cli_available(self) -> None:
        with patch("shutil.which", return_value="/usr/bin/openstudio"):
            with patch("subprocess.run") as mock_run:
                mock_run.return_value.returncode = 0
                mock_run.return_value.stdout = "OpenStudio 3.11.0\n"
                mock_run.return_value.stderr = ""
                assert _check_openstudio_cli() == "3.11.0"
                mock_run.assert_called_once()

    def test_cli_not_on_path(self) -> None:
        with patch("shutil.which", return_value=None):
            assert _check_openstudio_cli() is None

    def test_cli_returns_error(self) -> None:
        with patch("shutil.which", return_value="/usr/bin/openstudio"):
            with patch("subprocess.run") as mock_run:
                mock_run.return_value.returncode = 1
                mock_run.return_value.stdout = ""
                mock_run.return_value.stderr = "error"
                assert _check_openstudio_cli() is None

    def test_cli_times_out(self) -> None:
        with patch("shutil.which", return_value="/usr/bin/openstudio"):
            with patch("subprocess.run") as mock_run:
                mock_run.side_effect = subprocess.TimeoutExpired("cmd", 30)
                assert _check_openstudio_cli() is None


class TestIsRunningInDocker:
    def test_in_docker(self) -> None:
        cgroup_content = "12:devices:/docker/abc123\n"
        with patch("pathlib.Path.is_file", return_value=True):
            with patch("pathlib.Path.read_text", return_value=cgroup_content):
                assert _is_running_in_docker() is True

    def test_in_containerd(self) -> None:
        cgroup_content = "12:devices:/containerd/abc123\n"
        with patch("pathlib.Path.is_file", return_value=True):
            with patch("pathlib.Path.read_text", return_value=cgroup_content):
                assert _is_running_in_docker() is True

    def test_not_in_container(self) -> None:
        cgroup_content = "12:devices:/\n"
        with patch("pathlib.Path.is_file", return_value=True):
            with patch("pathlib.Path.read_text", return_value=cgroup_content):
                assert _is_running_in_docker() is False

    def test_cgroup_file_missing(self) -> None:
        with patch("pathlib.Path.is_file", return_value=False):
            assert _is_running_in_docker() is False


class TestCheckDockerContainerLabel:
    def test_in_docker_with_label(self) -> None:
        with patch("osimflow.version_detection._is_running_in_docker", return_value=True):
            with patch("subprocess.run") as mock_run:
                mock_run.return_value.returncode = 0
                mock_run.return_value.stdout = "OpenStudio 3.11.0-rc1+abc123\n"
                mock_run.return_value.stderr = ""
                assert _check_docker_container_label() == "3.11.0"

    def test_in_docker_no_label(self) -> None:
        with patch("osimflow.version_detection._is_running_in_docker", return_value=True):
            with patch("subprocess.run") as mock_run:
                mock_run.return_value.returncode = 0
                mock_run.return_value.stdout = "<no value>\n"
                mock_run.return_value.stderr = ""
                assert _check_docker_container_label() is None

    def test_not_in_docker(self) -> None:
        with patch("osimflow.version_detection._is_running_in_docker", return_value=False):
            assert _check_docker_container_label() is None

    def test_docker_inspect_fails(self) -> None:
        with patch("osimflow.version_detection._is_running_in_docker", return_value=True):
            with patch("subprocess.run") as mock_run:
                mock_run.side_effect = subprocess.SubprocessError("docker not available")
                assert _check_docker_container_label() is None


class TestDetectOpenstudioVersion:
    def test_from_env_variable(self) -> None:
        with patch.object(
            osimflow.version_detection, "_check_env_variable", return_value="3.11.0"
        ):
            with patch.object(
                osimflow.version_detection, "_check_openstudio_cli", return_value=None
            ):
                with patch.object(
                    osimflow.version_detection,
                    "_check_docker_container_label",
                    return_value=None,
                ):
                    assert detect_openstudio_version() == "3.11.0"

    def test_from_cli(self) -> None:
        with patch.object(
            osimflow.version_detection, "_check_env_variable", return_value=None
        ):
            with patch.object(
                osimflow.version_detection, "_check_openstudio_cli", return_value="3.7.0"
            ):
                with patch.object(
                    osimflow.version_detection,
                    "_check_docker_container_label",
                    return_value=None,
                ):
                    assert detect_openstudio_version() == "3.7.0"

    def test_from_docker_label(self) -> None:
        with patch.object(
            osimflow.version_detection, "_check_env_variable", return_value=None
        ):
            with patch.object(
                osimflow.version_detection, "_check_openstudio_cli", return_value=None
            ):
                with patch.object(
                    osimflow.version_detection,
                    "_check_docker_container_label",
                    return_value="3.5.1",
                ):
                    assert detect_openstudio_version() == "3.5.1"

    def test_not_found(self) -> None:
        with patch.object(
            osimflow.version_detection, "_check_env_variable", return_value=None
        ):
            with patch.object(
                osimflow.version_detection, "_check_openstudio_cli", return_value=None
            ):
                with patch.object(
                    osimflow.version_detection,
                    "_check_docker_container_label",
                    return_value=None,
                ):
                    with pytest.raises(VersionDetectionError, match="Could not detect"):
                        detect_openstudio_version()


class TestGetCompatibleContainerTag:
    def test_tag_format(self) -> None:
        assert get_compatible_container_tag("3.11.0") == "nrel/openstudio:3.11.0"
        assert get_compatible_container_tag("3.7.0") == "nrel/openstudio:3.7.0"
        assert get_compatible_container_tag("4.0.1") == "nrel/openstudio:4.0.1"


class TestVerifyVersionCompatibility:
    def test_exact_match(self) -> None:
        assert verify_version_compatibility("3.11.0", "3.11.0") is True

    def test_minor_patch_differs(self) -> None:
        assert verify_version_compatibility("3.11.0", "3.11.1") is True
        assert verify_version_compatibility("3.11.0", "3.11.2") is True

    def test_major_differs(self) -> None:
        assert verify_version_compatibility("3.11.0", "4.0.0") is False
        assert verify_version_compatibility("3.11.0", "2.9.0") is False

    def test_minor_differs(self) -> None:
        assert verify_version_compatibility("3.11.0", "3.12.0") is False
        assert verify_version_compatibility("3.11.0", "3.10.0") is False

    def test_invalid_version_strings(self) -> None:
        assert verify_version_compatibility("invalid", "3.11.0") is False
        assert verify_version_compatibility("3.11.0", "invalid") is False
        assert verify_version_compatibility("invalid", "invalid") is False


# Import osimflow.version_detection for mocking internal functions
import osimflow.version_detection  # noqa: E402
