"""Issue #1833: ``--aws-batch-allow-long-lived-credentials`` CLI/config wiring."""

import argparse
import logging
from pathlib import Path
from unittest.mock import MagicMock, patch

from osimflow.config import AWSBatchConfig, CampaignConfig
from osimflow.executor_configs import aws_batch
from osimflow.executors.aws_batch_executor import AWSBatchExecutor

FLAG = "--aws-batch-allow-long-lived-credentials"


def _parse(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    aws_batch.add_arguments(parser)
    return parser.parse_args(argv)


def test_flag_defaults_off() -> None:
    assert _parse([]).aws_batch_allow_long_lived_credentials is False


def test_flag_enables() -> None:
    assert _parse([FLAG]).aws_batch_allow_long_lived_credentials is True


def test_kwargs_for_executor_default_and_enabled() -> None:
    assert aws_batch.kwargs_for_executor()["allow_long_lived_credentials"] is False
    kwargs = aws_batch.kwargs_for_executor(aws_batch_allow_long_lived_credentials=True)
    assert kwargs["allow_long_lived_credentials"] is True


def test_config_field_default_and_propagation() -> None:
    assert AWSBatchConfig().allow_long_lived_credentials is False
    cfg = CampaignConfig(
        input_variables=Path("v.yml"),
        template_sim_package=Path("pkg"),
        n_samples=1,
        outdir=Path("out"),
        openstudio_version="3.11.0",
        aws_batch_allow_long_lived_credentials=True,
    )
    assert cfg.aws_batch is not None
    assert cfg.aws_batch.allow_long_lived_credentials is True


def test_executor_receives_flag_and_warns(caplog) -> None:  # type: ignore[no-untyped-def]
    kwargs = aws_batch.kwargs_for_executor(aws_batch_allow_long_lived_credentials=True)
    with patch("boto3.client", MagicMock()), caplog.at_level(logging.WARNING):
        executor = AWSBatchExecutor(**kwargs)
    assert executor._allow_long_lived_credentials is True
    assert any("allow_long_lived_credentials=True" in r.getMessage() for r in caplog.records)


def test_executor_default_does_not_enable() -> None:
    kwargs = aws_batch.kwargs_for_executor()
    with patch("boto3.client", MagicMock()):
        executor = AWSBatchExecutor(**kwargs)
    assert executor._allow_long_lived_credentials is False
