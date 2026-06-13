"""Tests for remote executor support via the REST API (issue #267).

Verifies that:
- ``CampaignCreateRequest`` accepts executor-specific fields for all executor types.
- ``_build_executor_from_request`` constructs the correct executor subclass for each type.
- Unknown executor names raise ``ValueError``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from osimflow.api.campaigns import _build_executor_from_request
from osimflow.api.schemas import CampaignCreateRequest

# Check availability of optional executor backends.
try:
    import azure.batch  # noqa: F401

    _HAS_AZURE = True
except ImportError:
    _HAS_AZURE = False

try:
    import google.cloud.batch  # noqa: F401

    _HAS_GOOGLE = True
except ImportError:
    _HAS_GOOGLE = False

try:
    import kubernetes  # noqa: F401

    _HAS_KUBERNETES = True
except ImportError:
    _HAS_KUBERNETES = False


def _req(
    executor: str,
    **overrides: object,
) -> CampaignCreateRequest:
    """Build a minimal ``CampaignCreateRequest`` for the given executor."""
    base = {
        "input_variables": "/tmp/variables.yml",
        "template_sim_package": "/tmp/pkg",
        "n_samples": 10,
        "openstudio_version": "3.11.0",
        "executor": executor,
        "algorithm": "lhs",
        "outdir": "/tmp/out",
        "archive_intermediates": False,
        "auto_start": False,
        "max_workers": 4,
        "slurm_partition": None,
        "slurm_account": None,
        "slurm_qos": None,
        "slurm_constraint": None,
        "slurm_gres": None,
        "slurm_real": False,
        "aws_batch_queue": None,
        "aws_batch_job_definition": None,
        "aws_batch_max_spot_price_usd": None,
        "aws_batch_fallback_to_on_demand": False,
        "aws_batch_max_retries": 3,
        "ecr_repository": None,
        "azure_batch_account_name": None,
        "azure_batch_account_url": None,
        "azure_batch_pool_id": None,
        "azure_batch_location": None,
        "azure_use_spot": False,
        "azure_fallback_to_on_demand": False,
        "azure_max_retries": 3,
        "google_batch_project_id": None,
        "google_batch_region": None,
        "google_batch_service_account": None,
        "google_use_spot": False,
        "google_fallback_to_on_demand": False,
        "google_max_retries": 3,
        "kubernetes_namespace": None,
        "kubernetes_poll_interval_s": None,
        "kubernetes_max_poll_interval_s": None,
        "nomad_address": None,
        "nomad_datacentre": None,
        "nomad_tls": False,
        "nomad_tls_verify": True,
        "nomad_cert": None,
        "nomad_key": None,
        "nomad_ca_cert": None,
        "pbs_server": None,
        "pbs_queue": None,
        "pbs_real": False,
        "dask_cluster_type": None,
        "dask_min_workers": None,
        "dask_max_workers": None,
        "dask_cpus_per_worker": None,
        "dask_memory_per_worker": None,
        "dask_walltime": None,
        "dask_queue": None,
        "dask_project": None,
    }
    base.update(overrides)
    return CampaignCreateRequest(**base)


class TestBuildExecutorFromRequest:
    """Unit tests for ``_build_executor_from_request``."""

    def test_local_executor(self) -> None:
        from osimflow import LocalExecutor

        req = _req("local")
        executor = _build_executor_from_request(req)
        assert isinstance(executor, LocalExecutor)

    def test_local_executor_respects_max_workers(self) -> None:
        from osimflow import LocalExecutor

        req = _req("local", max_workers=8)
        executor = _build_executor_from_request(req)
        assert isinstance(executor, LocalExecutor)

    def test_slurm_executor(self) -> None:
        from osimflow import SlurmExecutor

        req = _req(
            "slurm",
            slurm_partition="gpu",
            slurm_account="charged-project",
            slurm_qos="high",
            slurm_real=True,
        )
        executor = _build_executor_from_request(req)
        assert isinstance(executor, SlurmExecutor)
        assert executor.partition == "gpu"
        assert executor.account == "charged-project"
        assert executor.qos == "high"
        assert executor.debug is False  # slurm_real=True → debug=False

    def test_slurm_executor_debug_by_default(self) -> None:
        from osimflow import SlurmExecutor

        req = _req("slurm", slurm_real=False)
        executor = _build_executor_from_request(req)
        assert isinstance(executor, SlurmExecutor)
        assert executor.debug is True

    def test_aws_batch_executor(self) -> None:
        from osimflow import AWSBatchExecutor

        req = _req(
            "aws_batch",
            aws_batch_queue="my-queue",
            aws_batch_job_definition="my-def",
            aws_batch_max_spot_price_usd=0.5,
            aws_batch_fallback_to_on_demand=True,
            aws_batch_max_retries=5,
        )
        executor = _build_executor_from_request(req)
        assert isinstance(executor, AWSBatchExecutor)

    @pytest.mark.skipif(not _HAS_AZURE, reason="azure-batch extra not installed")
    def test_azure_batch_executor(self) -> None:
        from osimflow import AzureBatchExecutor

        req = _req(
            "azure_batch",
            azure_batch_account_name="myacct",
            azure_batch_account_url="https://myacct.batch.azure.com",
            azure_batch_pool_id="mypool",
            azure_batch_location="westus2",
            azure_use_spot=True,
            azure_max_retries=5,
        )
        executor = _build_executor_from_request(req)
        assert isinstance(executor, AzureBatchExecutor)

    @pytest.mark.skipif(not _HAS_GOOGLE, reason="google-batch extra not installed")
    def test_google_batch_executor(self) -> None:
        from osimflow import GoogleBatchExecutor

        req = _req(
            "google_batch",
            google_batch_project_id="my-project",
            google_batch_region="europe-west1",
            google_batch_service_account="sa@my-project.iam.gserviceaccount.com",
            google_use_spot=True,
        )
        executor = _build_executor_from_request(req)
        assert isinstance(executor, GoogleBatchExecutor)

    @pytest.mark.skipif(not _HAS_KUBERNETES, reason="kubernetes extra not installed")
    def test_kubernetes_executor(self) -> None:
        from osimflow import KubernetesExecutor

        req = _req(
            "kubernetes",
            kubernetes_namespace="osimflow-jobs",
            kubernetes_poll_interval_s=10.0,
            kubernetes_max_poll_interval_s=120.0,
        )
        executor = _build_executor_from_request(req)
        assert isinstance(executor, KubernetesExecutor)

    def test_nomad_executor(self) -> None:
        from osimflow import NomadExecutor

        req = _req(
            "nomad",
            nomad_address="http://nomad.internal:4646",
            nomad_datacentre="dc2",
            nomad_tls=True,
            nomad_tls_verify=True,
        )
        executor = _build_executor_from_request(req)
        assert isinstance(executor, NomadExecutor)

    def test_nomad_executor_tls_with_certs(self, tmp_path: Path) -> None:
        from osimflow import NomadExecutor

        cert_file = tmp_path / "client.crt"
        key_file = tmp_path / "client.key"
        ca_file = tmp_path / "ca.crt"
        cert_file.write_text("CERT")
        key_file.write_text("KEY")
        ca_file.write_text("CA")

        req = _req(
            "nomad",
            nomad_address="https://nomad.internal:4646",
            nomad_tls=True,
            nomad_tls_verify=True,
            nomad_cert=str(cert_file),
            nomad_key=str(key_file),
            nomad_ca_cert=str(ca_file),
        )
        executor = _build_executor_from_request(req)
        assert isinstance(executor, NomadExecutor)
        assert executor.cert == cert_file
        assert executor.key == key_file
        assert executor.ca_cert == ca_file

    def test_pbs_executor(self) -> None:
        from osimflow import PBSExecutor

        req = _req(
            "pbs",
            pbs_server="pbsserver",
            pbs_queue="batch",
            pbs_real=True,
        )
        executor = _build_executor_from_request(req)
        assert isinstance(executor, PBSExecutor)
        assert executor.debug is False  # pbs_real=True

    def test_pbs_executor_debug_by_default(self) -> None:
        from osimflow import PBSExecutor

        req = _req("pbs", pbs_real=False)
        executor = _build_executor_from_request(req)
        assert isinstance(executor, PBSExecutor)
        assert executor.debug is True

    def test_dask_jobqueue_executor(self) -> None:
        from osimflow import DaskJobQueueExecutor

        req = _req(
            "dask_jobqueue",
            dask_cluster_type="slurm",
            dask_min_workers=2,
            dask_max_workers=20,
            dask_cpus_per_worker=4,
            dask_memory_per_worker="8GiB",
            dask_walltime="04:00:00",
            dask_queue="gpu",
            dask_project="charged-project",
        )
        executor = _build_executor_from_request(req)
        assert isinstance(executor, DaskJobQueueExecutor)

    def test_unknown_executor_raises(self) -> None:
        req = _req("unknown_executor")
        with pytest.raises(ValueError, match="unknown executor"):
            _build_executor_from_request(req)


class TestCampaignCreateRequestSchema:
    """Schema validation tests for ``CampaignCreateRequest``."""

    def test_local_request_default_fields(self) -> None:
        req = _req("local")
        assert req.executor == "local"
        assert req.max_workers == 4
        assert req.slurm_partition is None
        assert req.aws_batch_queue is None

    def test_slurm_fields_accepted(self) -> None:
        req = _req(
            "slurm",
            slurm_partition="gpu",
            slurm_account="myproject",
            slurm_qos="high",
            slurm_constraint="gpu",
            slurm_gres="gpu:1",
            slurm_real=True,
        )
        assert req.slurm_partition == "gpu"
        assert req.slurm_account == "myproject"
        assert req.slurm_qos == "high"
        assert req.slurm_real is True

    def test_aws_batch_fields_accepted(self) -> None:
        req = _req(
            "aws_batch",
            aws_batch_queue="my-queue",
            aws_batch_job_definition="my-job-def",
            aws_batch_max_spot_price_usd=0.5,
            aws_batch_fallback_to_on_demand=True,
            aws_batch_max_retries=5,
            ecr_repository="123456.dkr.ecr.us-east-1.amazonaws.com/my-repo",
        )
        assert req.aws_batch_queue == "my-queue"
        assert req.ecr_repository == "123456.dkr.ecr.us-east-1.amazonaws.com/my-repo"

    def test_azure_batch_fields_accepted(self) -> None:
        req = _req(
            "azure_batch",
            azure_batch_account_name="myacct",
            azure_batch_account_url="https://myacct.batch.azure.com",
            azure_batch_pool_id="mypool",
            azure_batch_location="westus2",
            azure_use_spot=True,
            azure_fallback_to_on_demand=True,
            azure_max_retries=5,
        )
        assert req.azure_batch_account_name == "myacct"
        assert req.azure_use_spot is True

    def test_google_batch_fields_accepted(self) -> None:
        req = _req(
            "google_batch",
            google_batch_project_id="my-project",
            google_batch_region="europe-west1",
            google_batch_service_account="sa@my-project.iam.gserviceaccount.com",
            google_use_spot=True,
            google_fallback_to_on_demand=True,
            google_max_retries=5,
        )
        assert req.google_batch_project_id == "my-project"
        assert req.google_use_spot is True

    def test_kubernetes_fields_accepted(self) -> None:
        req = _req(
            "kubernetes",
            kubernetes_namespace="osimflow-jobs",
            kubernetes_poll_interval_s=10.0,
            kubernetes_max_poll_interval_s=120.0,
        )
        assert req.kubernetes_namespace == "osimflow-jobs"
        assert req.kubernetes_poll_interval_s == 10.0

    def test_nomad_fields_accepted(self) -> None:
        req = _req(
            "nomad",
            nomad_address="http://nomad:4646",
            nomad_datacentre="dc2",
            nomad_tls=True,
            nomad_tls_verify=False,
            nomad_cert="/path/to/cert.pem",
            nomad_key="/path/to/key.pem",
            nomad_ca_cert="/path/to/ca.pem",
        )
        assert req.nomad_address == "http://nomad:4646"
        assert req.nomad_tls is True
        assert req.nomad_tls_verify is False
        assert req.nomad_cert == "/path/to/cert.pem"

    def test_pbs_fields_accepted(self) -> None:
        req = _req(
            "pbs",
            pbs_server="pbs.example.com",
            pbs_queue="default",
            pbs_real=True,
        )
        assert req.pbs_server == "pbs.example.com"
        assert req.pbs_real is True

    def test_dask_jobqueue_fields_accepted(self) -> None:
        req = _req(
            "dask_jobqueue",
            dask_cluster_type="pbs",
            dask_min_workers=1,
            dask_max_workers=50,
            dask_cpus_per_worker=8,
            dask_memory_per_worker="16GiB",
            dask_walltime="08:00:00",
            dask_queue="default",
            dask_project="myproject",
        )
        assert req.dask_cluster_type == "pbs"
        assert req.dask_max_workers == 50
        assert req.dask_memory_per_worker == "16GiB"
