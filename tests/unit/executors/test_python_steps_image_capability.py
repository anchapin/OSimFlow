"""``python_steps_use_openstudio_image`` capability replaces executor-name checks (issue #2118)."""

from osimflow import LocalExecutor
from osimflow.executors.aws_batch_executor import AWSBatchExecutor
from osimflow.executors.base import BaseExecutor


def test_default_is_false() -> None:
    assert BaseExecutor.python_steps_use_openstudio_image.fget(LocalExecutor()) is False  # type: ignore[attr-defined]


def test_aws_batch_overrides_true() -> None:
    assert (
        AWSBatchExecutor.python_steps_use_openstudio_image.fget(object.__new__(AWSBatchExecutor))
        is True
    )  # type: ignore[attr-defined]
