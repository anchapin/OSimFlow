"""Tests for ECR image URI resolution in AWSBatchExecutor (issue #144)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest


@pytest.fixture
def _mock_boto3(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch boto3 import so AWSBatchExecutor can be instantiated."""
    import importlib
    import sys

    mock_boto3 = MagicMock()
    sys.modules["boto3"] = mock_boto3
    # Reimport to pick up mock
    import osimflow.executors as exec_mod
    importlib.reload(exec_mod)


class TestECRImageResolution:
    """Test container image URI resolution with and without ECR."""

    def test_default_uses_docker_hub(self, _mock_boto3: None) -> None:
        from osimflow.executors import AWSBatchExecutor

        executor = AWSBatchExecutor.__new__(AWSBatchExecutor)
        executor.ecr_repository = None
        assert executor._resolve_container_image("3.5.0") == "nrel/openstudio:3.5.0"

    def test_ecr_overrides_docker_hub(self, _mock_boto3: None) -> None:
        from osimflow.executors import AWSBatchExecutor

        executor = AWSBatchExecutor.__new__(AWSBatchExecutor)
        executor.ecr_repository = "123456.dkr.ecr.us-east-1.amazonaws.com/osimflow/openstudio"
        result = executor._resolve_container_image("3.5.0")
        assert result == "123456.dkr.ecr.us-east-1.amazonaws.com/osimflow/openstudio:3.5.0"

    def test_ecr_uri_includes_version_tag(self, _mock_boto3: None) -> None:
        from osimflow.executors import AWSBatchExecutor

        executor = AWSBatchExecutor.__new__(AWSBatchExecutor)
        executor.ecr_repository = "acct.dkr.ecr.eu-west-1.amazonaws.com/osim/openstudio"
        assert executor._resolve_container_image("3.4.0").endswith(":3.4.0")
        assert executor._resolve_container_image("3.6.0").endswith(":3.6.0")

    def test_none_version_uses_latest(self, _mock_boto3: None) -> None:
        from osimflow.executors import AWSBatchExecutor

        executor = AWSBatchExecutor.__new__(AWSBatchExecutor)
        executor.ecr_repository = None
        assert executor._resolve_container_image(None) == "nrel/openstudio:latest"

    def test_ecr_none_version_uses_latest(self, _mock_boto3: None) -> None:
        from osimflow.executors import AWSBatchExecutor

        executor = AWSBatchExecutor.__new__(AWSBatchExecutor)
        executor.ecr_repository = "acct.dkr.ecr.us-west-2.amazonaws.com/os/os"
        assert executor._resolve_container_image(None) == "acct.dkr.ecr.us-west-2.amazonaws.com/os/os:latest"

    def test_environment_includes_resolved_ecr_image(self, _mock_boto3: None) -> None:
        from osimflow.executors import AWSBatchExecutor

        executor = AWSBatchExecutor.__new__(AWSBatchExecutor)
        executor.ecr_repository = "123456.dkr.ecr.us-east-1.amazonaws.com/osimflow/openstudio"
        env = executor._build_environment(container=None, openstudio_version="3.5.0")
        container_env = [e for e in env if e["name"] == "OSIMFLOW_CONTAINER"]
        assert len(container_env) == 1
        assert container_env[0]["value"] == "123456.dkr.ecr.us-east-1.amazonaws.com/osimflow/openstudio:3.5.0"

    def test_environment_default_without_ecr(self, _mock_boto3: None) -> None:
        from osimflow.executors import AWSBatchExecutor

        executor = AWSBatchExecutor.__new__(AWSBatchExecutor)
        executor.ecr_repository = None
        env = executor._build_environment(container=None, openstudio_version="3.5.0")
        container_env = [e for e in env if e["name"] == "OSIMFLOW_CONTAINER"]
        assert len(container_env) == 1
        assert container_env[0]["value"] == "nrel/openstudio:3.5.0"
