"""Unit tests for AWS Batch payload spilling, image-pin parsing and client construction."""

from unittest.mock import MagicMock, patch

from osimflow.executors import AWSBatchExecutor
from osimflow.executors.base import ResultTransportConfig

DIGEST = "sha256:" + "a" * 64


def _bare() -> AWSBatchExecutor:
    ex = object.__new__(AWSBatchExecutor)
    ex._container_digest = None
    ex.ecr_repository = None
    ex._region_name = "us-east-1"
    ex._retry_config = None
    ex._boto3 = MagicMock()
    return ex


class TestPinnedImage:
    def test_operator_pin_returned(self) -> None:
        ex = _bare()
        ex._container_digest = f"repo/img@{DIGEST}"
        assert ex._pinned_image(None, None) == f"repo/img@{DIGEST}"

    def test_unresolved_sentinel_is_not_a_pin(self) -> None:
        ex = _bare()
        ex._container_digest = "unresolved"
        assert ex._pinned_image(None, None) is None

    def test_auto_resolved_cache_form_is_not_a_pin(self) -> None:
        ex = _bare()
        ex._container_digest = f"label@repo/img@{DIGEST}"
        assert ex._pinned_image(None, None) is None

    def test_ecr_repository_resolves_tag(self) -> None:
        ex = _bare()
        ex.ecr_repository = "123.dkr.ecr/x"
        assert ex._pinned_image(None, "3.10.0") == "123.dkr.ecr/x:3.10.0"

    def test_no_pin(self) -> None:
        assert _bare()._pinned_image(None, "3.10.0") is None


class TestMakeClient:
    def test_plain_client_without_botocore_session(self) -> None:
        ex = _bare()
        ex._make_client("batch")
        ex._boto3.client.assert_called_once()
        ex._boto3.Session.assert_not_called()

    def test_session_used_with_botocore_session(self) -> None:
        ex = _bare()
        ex._botocore_session = object()
        ex._make_client("ec2")
        ex._boto3.Session.assert_called_once()
        ex._boto3.Session.return_value.client.assert_called_once()


class TestSpillOversizedPayload:
    def _transport(self, mode: str = "object_storage") -> ResultTransportConfig:
        return ResultTransportConfig(mode=mode, backend="s3", bucket="b", prefix="p")

    def test_small_payload_inline(self) -> None:
        assert _bare()._spill_oversized_payload("{}", self._transport()) == "{}"

    def test_large_payload_without_storage_stays_inline(self) -> None:
        big = "x" * 5000
        assert _bare()._spill_oversized_payload(big, None) == big
        assert _bare()._spill_oversized_payload(big, self._transport("local")) == big

    def test_large_payload_spilled(self) -> None:
        big = "x" * 5000
        with (
            patch("osimflow.storage.build_result_storage") as build,
            patch("osimflow.input_staging.spill_task_payload", return_value="PTR") as spill,
        ):
            out = _bare()._spill_oversized_payload(big, self._transport())
        assert out == "PTR"
        build.assert_called_once()
        spill.assert_called_once_with(build.return_value, big)
