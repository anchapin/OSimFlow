"""Variable-aware sweep of a Ruby-measure OSW on real AWS Batch (issue #1869).

Skip-gated: needs real Batch infrastructure and a worker image that bundles the
Ruby measure(s). The template package is supplied via
``OSIMFLOW_AWS_BATCH_SWEEP_PACKAGE`` (a gem-style dir with ``workflow.osw``)
and ``OSIMFLOW_AWS_BATCH_SWEEP_VARIABLES`` (its variables.yml).
"""

import os
import uuid
from pathlib import Path

import pytest

_REQUIRED_ENV = (
    "OSIMFLOW_AWS_BATCH_E2E",
    "OSIMFLOW_AWS_BATCH_QUEUE",
    "OSIMFLOW_AWS_BATCH_JOB_DEFINITION",
    "OSIMFLOW_AWS_REGION",
    "OSIMFLOW_AWS_BATCH_RESULT_BUCKET",
    "OSIMFLOW_AWS_BATCH_SWEEP_PACKAGE",
    "OSIMFLOW_AWS_BATCH_SWEEP_VARIABLES",
)
_MISSING = [v for v in _REQUIRED_ENV if os.environ.get(v) in (None, "")]

pytestmark = pytest.mark.skipif(
    bool(_MISSING), reason=f"real AWS Batch sweep E2E not configured (missing: {_MISSING})"
)

N_SAMPLES = 3


def test_three_sample_ruby_measure_sweep(tmp_path: Path) -> None:
    from osimflow import Campaign, CampaignConfig
    from osimflow.executors import AWSBatchExecutor

    outdir = tmp_path / f"out-{uuid.uuid4().hex[:8]}"
    outdir.mkdir()
    cfg = CampaignConfig(
        input_variables=Path(os.environ["OSIMFLOW_AWS_BATCH_SWEEP_VARIABLES"]),
        template_sim_package=Path(os.environ["OSIMFLOW_AWS_BATCH_SWEEP_PACKAGE"]),
        n_samples=N_SAMPLES,
        outdir=outdir,
        openstudio_version=os.environ.get("OSIMFLOW_OPENSTUDIO_VERSION", "3.11.0"),
        result_storage_backend="s3",
        result_storage_bucket=os.environ["OSIMFLOW_AWS_BATCH_RESULT_BUCKET"],
        container_digest=os.environ.get("OSIMFLOW_AWS_BATCH_CONTAINER_DIGEST"),
    )
    executor = AWSBatchExecutor(
        job_queue=os.environ["OSIMFLOW_AWS_BATCH_QUEUE"],
        job_definition=os.environ["OSIMFLOW_AWS_BATCH_JOB_DEFINITION"],
        region_name=os.environ["OSIMFLOW_AWS_REGION"],
        allow_long_lived_credentials=True,
    )
    Campaign(cfg=cfg, executor=executor).run()
    executor.shutdown()

    rows = (outdir / "aggregated_results.csv").read_text().strip().splitlines()
    assert len(rows) >= N_SAMPLES + 1
