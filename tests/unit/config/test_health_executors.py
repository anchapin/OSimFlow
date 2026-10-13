"""Unit tests for osimflow/_health_executors.py (issue #1944)."""

import subprocess
import sys
import types

import pytest

from osimflow import _health_executors as he
from osimflow._health_types import CheckStatus


def _which(found: dict[str, str]):
    return lambda name: found.get(name)


def _rc(rc, err=""):
    return lambda cmd, timeout_s=5.0: (rc, "", err)


class TestRunSubprocessQuiet:
    def test_success(self):
        rc, out, _ = he._run_subprocess_quiet([sys.executable, "-c", "print('hi')"])
        assert rc == 0 and out.strip() == "hi"

    @pytest.mark.parametrize(
        ("exc", "needle"),
        [
            (subprocess.TimeoutExpired("x", 1), "timeout"),
            (FileNotFoundError(), "binary not found"),
            (PermissionError("nope"), "PermissionError"),
        ],
    )
    def test_errors_never_raise(self, monkeypatch, exc, needle):
        def boom(*a, **k):
            raise exc

        monkeypatch.setattr(he.subprocess, "run", boom)
        rc, _, err = he._run_subprocess_quiet(["x"])
        assert rc is None and needle in err

    def test_empty_cmd_not_found(self, monkeypatch):
        def boom(*a, **k):
            raise FileNotFoundError

        monkeypatch.setattr(he.subprocess, "run", boom)
        assert "?" in he._run_subprocess_quiet([])[2]


class TestCheckLocal:
    def test_pass(self, monkeypatch):
        monkeypatch.setattr(he.os, "cpu_count", lambda: 8)
        monkeypatch.setattr(he.sys, "platform", "linux")
        assert he._check_local().status == CheckStatus.PASS

    def test_warn_windows_single_cpu(self, monkeypatch):
        monkeypatch.setattr(he.os, "cpu_count", lambda: 1)
        monkeypatch.setattr(he.sys, "platform", "win32")
        res = he._check_local()
        assert res.status == CheckStatus.WARN
        assert "Windows" in res.message and "1 CPU" in res.message

    def test_cpu_count_raises(self, monkeypatch):
        def boom():
            raise OSError

        monkeypatch.setattr(he.os, "cpu_count", boom)
        assert he._check_local().status == CheckStatus.WARN


CLI_CHECKS = [
    ("_check_slurm", "sinfo"),
    ("_check_pbs", "qstat"),
    ("_check_nomad", "nomad"),
    ("_check_kubernetes", "kubectl"),
    ("_check_docker_swarm", "docker"),
]


class TestCliChecks:
    @pytest.mark.parametrize(("fn", "binary"), CLI_CHECKS)
    def test_missing_binary_skips(self, monkeypatch, fn, binary):
        monkeypatch.setattr(he.shutil, "which", _which({}))
        assert getattr(he, fn)().status == CheckStatus.SKIP

    @pytest.mark.parametrize(("fn", "binary"), CLI_CHECKS)
    def test_nonzero_warns(self, monkeypatch, fn, binary):
        monkeypatch.setattr(he.shutil, "which", _which({binary: f"/bin/{binary}"}))
        monkeypatch.setattr(he, "_run_subprocess_quiet", _rc(1, "bad"))
        res = getattr(he, fn)()
        assert res.status == CheckStatus.WARN
        assert "bad" in res.detail

    @pytest.mark.parametrize(("fn", "binary"), CLI_CHECKS)
    def test_timeout_warns(self, monkeypatch, fn, binary):
        monkeypatch.setattr(he.shutil, "which", _which({binary: f"/bin/{binary}"}))
        monkeypatch.setattr(he, "_run_subprocess_quiet", _rc(None, "timeout"))
        assert getattr(he, fn)().status == CheckStatus.WARN

    @pytest.mark.parametrize(("fn", "binary"), CLI_CHECKS)
    def test_ok_passes(self, monkeypatch, fn, binary):
        monkeypatch.setattr(he.shutil, "which", _which({binary: f"/bin/{binary}"}))
        monkeypatch.setattr(he, "_run_subprocess_quiet", _rc(0))
        res = he.__dict__[fn]()
        assert res.status == CheckStatus.PASS

    def test_empty_stderr_fallbacks(self, monkeypatch):
        monkeypatch.setattr(he.shutil, "which", _which({"sinfo": "/s", "docker": "/d"}))
        monkeypatch.setattr(he, "_run_subprocess_quiet", _rc(2, ""))
        assert he._check_slurm().detail == "unknown error"
        assert he._check_docker_swarm().detail == "docker info failed"


class TestAwsBatch:
    def _fake_boto3(self, monkeypatch, client):
        mod = types.ModuleType("boto3")
        mod.client = client
        monkeypatch.setitem(sys.modules, "boto3", mod)

    def test_no_boto3(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "boto3", None)
        assert he._check_aws_batch().status == CheckStatus.SKIP

    def test_no_region(self, monkeypatch):
        self._fake_boto3(monkeypatch, lambda *a, **k: None)
        monkeypatch.delenv("AWS_REGION", raising=False)
        monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)
        assert he._check_aws_batch().status == CheckStatus.SKIP

    def test_reachable(self, monkeypatch):
        class Client:
            def describe_compute_environments(self, maxResults):
                return {"computeEnvironments": [{}]}

        self._fake_boto3(monkeypatch, lambda *a, **k: Client())
        monkeypatch.setenv("AWS_REGION", "us-east-1")
        res = he._check_aws_batch()
        assert res.status == CheckStatus.PASS and "1 compute env" in res.message

    def test_unreachable(self, monkeypatch):
        def boom(*a, **k):
            from botocore.exceptions import NoCredentialsError

            raise NoCredentialsError()

        self._fake_boto3(monkeypatch, boom)
        monkeypatch.delenv("AWS_REGION", raising=False)
        monkeypatch.setenv("AWS_DEFAULT_REGION", "eu-west-1")
        res = he._check_aws_batch()
        assert res.status == CheckStatus.WARN and "credentials" in res.detail

    def test_unexpected_error_propagates(self, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("programming bug")

        self._fake_boto3(monkeypatch, boom)
        monkeypatch.delenv("AWS_REGION", raising=False)
        monkeypatch.setenv("AWS_DEFAULT_REGION", "eu-west-1")
        with pytest.raises(RuntimeError, match="programming bug"):
            he._check_aws_batch()


class TestAzureBatch:
    def _fake_sdk(self, monkeypatch):
        azure = types.ModuleType("azure")
        batch = types.ModuleType("azure.batch")
        models = types.ModuleType("azure.batch.models")
        azure.batch = batch
        batch.models = models
        monkeypatch.setitem(sys.modules, "azure", azure)
        monkeypatch.setitem(sys.modules, "azure.batch", batch)
        monkeypatch.setitem(sys.modules, "azure.batch.models", models)

    def test_no_sdk(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "azure.batch", None)
        assert he._check_azure_batch().status == CheckStatus.SKIP

    def test_missing_env(self, monkeypatch):
        self._fake_sdk(monkeypatch)
        for k in ("AZURE_BATCH_ACCOUNT", "AZURE_BATCH_ACCOUNT_URL", "AZURE_BATCH_POOL_ID"):
            monkeypatch.delenv(k, raising=False)
        res = he._check_azure_batch()
        assert res.status == CheckStatus.WARN and "AZURE_BATCH_POOL_ID" in res.message

    def test_configured(self, monkeypatch):
        self._fake_sdk(monkeypatch)
        monkeypatch.setenv("AZURE_BATCH_ACCOUNT", "acct")
        monkeypatch.setenv("AZURE_BATCH_ACCOUNT_URL", "https://x")
        monkeypatch.setenv("AZURE_BATCH_POOL_ID", "pool")
        res = he._check_azure_batch()
        assert res.status == CheckStatus.PASS and "pool=pool" in res.detail


class TestGoogleBatch:
    def _fake_sdk(self, monkeypatch):
        google = types.ModuleType("google")
        cloud = types.ModuleType("google.cloud")
        batch_v1 = types.ModuleType("google.cloud.batch_v1")
        google.cloud = cloud
        cloud.batch_v1 = batch_v1
        monkeypatch.setitem(sys.modules, "google", google)
        monkeypatch.setitem(sys.modules, "google.cloud", cloud)
        monkeypatch.setitem(sys.modules, "google.cloud.batch_v1", batch_v1)

    def test_no_sdk(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "google.cloud.batch_v1", None)
        assert he._check_google_batch().status == CheckStatus.SKIP

    @pytest.mark.parametrize(
        ("project", "region", "expected"),
        [
            (None, None, ["GOOGLE_CLOUD_PROJECT", "GOOGLE_CLOUD_REGION"]),
            ("p", None, ["GOOGLE_CLOUD_REGION"]),
            (None, "r", ["GOOGLE_CLOUD_PROJECT"]),
        ],
    )
    def test_missing_env(self, monkeypatch, project, region, expected):
        self._fake_sdk(monkeypatch)
        for k in ("GOOGLE_CLOUD_PROJECT", "GCP_PROJECT", "GOOGLE_CLOUD_REGION", "GOOGLE_REGION"):
            monkeypatch.delenv(k, raising=False)
        if project:
            monkeypatch.setenv("GCP_PROJECT", project)
        if region:
            monkeypatch.setenv("GOOGLE_REGION", region)
        res = he._check_google_batch()
        assert res.status == CheckStatus.WARN
        assert all(e in res.message for e in expected)

    def test_configured(self, monkeypatch):
        self._fake_sdk(monkeypatch)
        monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "p")
        monkeypatch.setenv("GOOGLE_CLOUD_REGION", "r")
        res = he._check_google_batch()
        assert res.status == CheckStatus.PASS and "project=p region=r" in res.detail


class TestDaskJobqueue:
    def test_not_installed(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "dask_jobqueue", None)
        assert he._check_dask_jobqueue().status == CheckStatus.SKIP

    def test_no_backends(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "dask_jobqueue", types.ModuleType("dask_jobqueue"))
        assert he._check_dask_jobqueue().status == CheckStatus.WARN

    def test_backends_available(self, monkeypatch):
        mod = types.ModuleType("dask_jobqueue")
        mod.SLURMCluster = object
        mod.PBSCluster = object
        mod.KubeCluster = object
        monkeypatch.setitem(sys.modules, "dask_jobqueue", mod)
        monkeypatch.setattr(he.importlib.util, "find_spec", lambda name: None)
        res = he._check_dask_jobqueue()
        assert res.status == CheckStatus.PASS and res.message.endswith("(PBSCluster)")


class TestTaskPayloadSigning:
    def test_no_executor(self):
        assert he._check_task_payload_signing(None).status == CheckStatus.SKIP

    def test_unknown_executor(self):
        res = he._check_task_payload_signing("definitely_not_an_executor")
        assert res.status == CheckStatus.SKIP and "unavailable" in res.message

    def test_not_payload_executor(self):
        res = he._check_task_payload_signing("local")
        assert res.status == CheckStatus.SKIP
        assert "does not use" in res.message

    def test_secret_missing(self, monkeypatch):
        monkeypatch.delenv("OSIMFLOW_TASK_PAYLOAD_SECRET", raising=False)
        res = he._check_task_payload_signing("aws_batch")
        assert res.status == CheckStatus.WARN and "not set" in res.message

    def test_pass(self, monkeypatch):
        monkeypatch.setenv("OSIMFLOW_TASK_PAYLOAD_SECRET", "s")
        res = he._check_task_payload_signing("aws_batch")
        assert res.status == CheckStatus.PASS

    def test_executor_does_not_sign(self, monkeypatch):
        from osimflow.executors import ExecutorRegistry

        class Unsigned:
            requires_remote_runner_payload = True
            signs_task_payload = False

        monkeypatch.setenv("OSIMFLOW_TASK_PAYLOAD_SECRET", "s")
        monkeypatch.setattr(ExecutorRegistry, "get", classmethod(lambda cls, n: Unsigned))
        res = he._check_task_payload_signing("unsigned")
        assert res.status == CheckStatus.WARN and "does not" in res.message
