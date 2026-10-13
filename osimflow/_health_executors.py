"""Per-executor substrate health checks split out of ``health.py`` (issue #1890)."""

from __future__ import annotations

import importlib.util
import logging
import os
import platform
import shutil
import subprocess
import sys

from osimflow._health_types import CheckCategory, CheckResult, CheckStatus

log = logging.getLogger("osimflow.health")


# ---------------------------------------------------------------------------
# Per-executor substrate checks (issue #1024)
# ---------------------------------------------------------------------------
# Each executor registered in ExecutorRegistry gets one health check. The
# check returns INFORMATIONAL by default — failures are reported but do not
# block a campaign unless the executor is the configured one. When the user
# passes ``configured_executor="<name>"`` to ``run_health_checks``, that
# one check's category is promoted to CRITICAL by the orchestrator.
#
# Implementation rules (per the issue acceptance criteria):
#
#  * Tooling we cannot assume is installed (Docker daemon, AWS creds,
#    Nomad binary, Slurm cluster) must produce a SKIP/WARN with a clear
#    "tool not installed" message — never raise. CI does not have any
#    of these substrates and must keep passing.
#  * The check function takes no arguments and returns a ``CheckResult``.
#  * All 10 checks are registered with ``ExecutorRegistry`` at module
#    import time (see ``_register_executor_health_checks`` at the bottom
#    of the file). Adding a new executor without a check breaks the
#    regression test in tests/unit/config/test_health_check.py.
# ---------------------------------------------------------------------------


_EXECUTOR_HEALTH_CHECK_NAME = "Executor: {name}"


def _executor_check(name: str, status: CheckStatus, message: str, detail: str = "") -> CheckResult:
    """Build a CheckResult for an executor check (INFORMATIONAL by default)."""
    return CheckResult(
        name=_EXECUTOR_HEALTH_CHECK_NAME.format(name=name),
        status=status,
        category=CheckCategory.INFORMATIONAL,
        message=message,
        detail=detail,
    )


def _check_local() -> CheckResult:
    """Verify the host can run the LocalExecutor.

    Checks ``sys.platform`` (LocalExecutor runs on Linux/macOS; Windows
    is rejected because submitit is unavailable there) and the CPU
    count. Two or more CPUs are required so the thread pool can do
    useful work — a single-CPU host can technically run but is so slow
    that the warning is warranted.
    """
    try:
        cpus = os.cpu_count() or 1
    except (OSError, NotImplementedError):
        cpus = 0
    issues: list[str] = []
    if sys.platform.startswith("win"):
        issues.append("Windows is not supported by LocalExecutor (submitit unavailable)")
    if cpus < 2:
        issues.append(f"only {cpus} CPU detected; LocalExecutor thread pool will be slow")

    if not issues:
        return _executor_check(
            "local",
            CheckStatus.PASS,
            f"{platform.system()} {platform.release()} with {cpus} CPUs",
            detail=f"sys.platform={sys.platform}; os.cpu_count()={cpus}",
        )
    return _executor_check(
        "local",
        CheckStatus.WARN,
        "; ".join(issues),
        detail="LocalExecutor will still run but is degraded.",
    )


def _run_subprocess_quiet(cmd: list[str], timeout_s: float = 5.0) -> tuple[int | None, str, str]:
    """Run *cmd* and return ``(returncode, stdout, stderr)``.

    Never raises — exits and IO errors are converted to ``(None, "", str(exc))``.
    Used by executor checks to probe external tools (sinfo, qstat, aws, …)
    without crashing the health subcommand when the tool is missing.
    """
    try:
        result = subprocess.run(  # noqa: S603 — caller owns the argv
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
        return result.returncode, result.stdout, result.stderr
    except subprocess.TimeoutExpired:
        return None, "", f"timeout after {timeout_s:.0f}s"
    except FileNotFoundError:
        return None, "", f"binary not found: {cmd[0] if cmd else '?'}"
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        return None, "", f"{type(exc).__name__}: {exc}"


def _check_slurm() -> CheckResult:
    """Verify Slurm tooling is reachable.

    Probes ``sinfo`` (falls back to ``sinfo -V`` for version-only check
    on hosts where sinfo cannot reach a controller). When ``sinfo`` is
    absent, returns SKIP with a clear message — the executor itself
    wraps submitit which can fall back to its ``DebugExecutor`` locally.
    """
    sinfo = shutil.which("sinfo")
    if not sinfo:
        return _executor_check(
            "slurm",
            CheckStatus.SKIP,
            "sinfo not installed",
            detail=(
                "SlurmExecutor needs a Slurm cluster. On dev hosts the "
                "executor falls back to submitit's local DebugExecutor."
            ),
        )
    rc, _stdout, stderr = _run_subprocess_quiet(["sinfo", "-V"], timeout_s=5.0)
    if rc is None or rc != 0:
        return _executor_check(
            "slurm",
            CheckStatus.WARN,
            "sinfo did not respond",
            detail=stderr.strip() or "unknown error",
        )
    return _executor_check(
        "slurm",
        CheckStatus.PASS,
        f"sinfo available ({sinfo})",
        detail="Use --slurm-partition <name> to override the default partition.",
    )


def _check_pbs() -> CheckResult:
    """Verify PBS tooling is reachable.

    Probes ``qstat -B`` (server/queue summary). When ``qstat`` is not on
    PATH, returns SKIP — PBSExecutor wraps submitit's PBS backend which
    requires a real PBS install to actually dispatch.
    """
    qstat = shutil.which("qstat")
    if not qstat:
        return _executor_check(
            "pbs",
            CheckStatus.SKIP,
            "qstat not installed",
            detail="PBSExecutor requires a PBS Pro / OpenPBS cluster.",
        )
    rc, _stdout, stderr = _run_subprocess_quiet(["qstat", "-B"], timeout_s=5.0)
    if rc is None or rc != 0:
        return _executor_check(
            "pbs",
            CheckStatus.WARN,
            "qstat did not respond",
            detail=stderr.strip() or f"exit code {rc}",
        )
    return _executor_check(
        "pbs",
        CheckStatus.PASS,
        f"qstat available ({qstat})",
        detail="PBS server reachable.",
    )


def _check_aws_batch() -> CheckResult:
    """Verify the AWS Batch substrate (SDK + creds).

    Checks that ``boto3`` is importable. Reaching the AWS control plane
    (describe-compute-environments) is best-effort: a missing AWS_REGION
    or no credentials produces SKIP, not FAIL — those are user errors
    we surface separately at submit time.
    """
    try:
        import boto3  # noqa: PLC0415, F401
    except ImportError:
        return _executor_check(
            "aws_batch",
            CheckStatus.SKIP,
            "boto3 not installed",
            detail="Install with: pip install 'osimflow[aws]'",
        )
    region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
    if not region:
        return _executor_check(
            "aws_batch",
            CheckStatus.SKIP,
            "boto3 installed; AWS region not configured",
            detail="Set AWS_REGION or AWS_DEFAULT_REGION to reach AWS Batch.",
        )
    from botocore.exceptions import BotoCoreError, ClientError  # noqa: PLC0415

    try:
        client = boto3.client("batch", region_name=region)  # noqa: PLC0415
        response = client.describe_compute_environments(maxResults=1)
        n_ce = len(response.get("computeEnvironments", []))
        return _executor_check(
            "aws_batch",
            CheckStatus.PASS,
            f"boto3 reachable; region={region}; {n_ce} compute env(s) found",
            detail="describe_compute_environments succeeded.",
        )
    except (BotoCoreError, ClientError, OSError) as exc:  # surface SDK/network errors as WARN
        return _executor_check(
            "aws_batch",
            CheckStatus.WARN,
            f"AWS Batch unreachable: {type(exc).__name__}",
            detail=str(exc),
        )


def _check_azure_batch() -> CheckResult:
    """Verify the Azure Batch substrate (SDK + env).

    Checks for the ``azure-batch`` SDK plus the four required env vars
    (account name, account URL, pool ID, location). Missing SDK is SKIP;
    missing env vars is WARN (the executor still constructs without
    them, but submission will fail without configuration).
    """
    try:
        import azure.batch  # noqa: PLC0415, F401
        import azure.batch.models  # noqa: PLC0415, F401
    except ImportError:
        return _executor_check(
            "azure_batch",
            CheckStatus.SKIP,
            "azure-batch SDK not installed",
            detail="Install with: pip install 'osimflow[azure]'",
        )

    env_keys = ("AZURE_BATCH_ACCOUNT", "AZURE_BATCH_ACCOUNT_URL", "AZURE_BATCH_POOL_ID")
    missing = [k for k in env_keys if not os.environ.get(k)]
    if missing:
        return _executor_check(
            "azure_batch",
            CheckStatus.WARN,
            f"missing env vars: {', '.join(missing)}",
            detail=(
                "Azure Batch submission requires AZURE_BATCH_ACCOUNT, "
                "AZURE_BATCH_ACCOUNT_URL, AZURE_BATCH_POOL_ID. Set them "
                "or pass CLI flags --azure-batch-* (issue #411)."
            ),
        )
    return _executor_check(
        "azure_batch",
        CheckStatus.PASS,
        "azure-batch SDK installed; account + pool configured",
        detail=f"account={os.environ.get('AZURE_BATCH_ACCOUNT')} pool={os.environ.get('AZURE_BATCH_POOL_ID')}",
    )


def _check_google_batch() -> CheckResult:
    """Verify the Google Cloud Batch substrate (SDK + env).

    Checks for the ``google-cloud-batch`` SDK plus GOOGLE_CLOUD_PROJECT
    and GOOGLE_CLOUD_REGION env vars. Missing SDK is SKIP; missing env
    is WARN.
    """
    try:
        import google.cloud.batch_v1  # noqa: PLC0415, F401
    except ImportError:
        return _executor_check(
            "google_batch",
            CheckStatus.SKIP,
            "google-cloud-batch SDK not installed",
            detail="Install with: pip install 'osimflow[google]'",
        )

    project = os.environ.get("GOOGLE_CLOUD_PROJECT") or os.environ.get("GCP_PROJECT")
    region = os.environ.get("GOOGLE_CLOUD_REGION") or os.environ.get("GOOGLE_REGION")
    if not project or not region:
        missing = []
        if not project:
            missing.append("GOOGLE_CLOUD_PROJECT")
        if not region:
            missing.append("GOOGLE_CLOUD_REGION")
        return _executor_check(
            "google_batch",
            CheckStatus.WARN,
            f"missing env vars: {', '.join(missing)}",
            detail="Google Batch submission requires project + region.",
        )
    return _executor_check(
        "google_batch",
        CheckStatus.PASS,
        "google-cloud-batch installed; project + region configured",
        detail=f"project={project} region={region}",
    )


def _check_nomad() -> CheckResult:
    """Verify the Nomad CLI is reachable.

    Probes ``nomad version`` (lightweight, works against any reachable
    Nomad agent — local or remote). When ``nomad`` is not on PATH,
    returns SKIP. A reachable agent with a non-zero exit produces WARN.
    """
    nomad = shutil.which("nomad")
    if not nomad:
        return _executor_check(
            "nomad",
            CheckStatus.SKIP,
            "nomad CLI not installed",
            detail="Install Nomad from https://developer.hashicorp.com/nomad/install",
        )
    rc, _stdout, stderr = _run_subprocess_quiet(["nomad", "version"], timeout_s=5.0)
    if rc is None or rc != 0:
        return _executor_check(
            "nomad",
            CheckStatus.WARN,
            "nomad CLI did not respond",
            detail=stderr.strip() or f"exit code {rc}",
        )
    return _executor_check(
        "nomad",
        CheckStatus.PASS,
        f"nomad CLI available ({nomad})",
        detail="Use --nomad-address <url> to point at a remote Nomad agent.",
    )


def _check_kubernetes() -> CheckResult:
    """Verify the Kubernetes substrate (CLI + cluster auth).

    Probes ``kubectl auth can-i get jobs`` in the current namespace.
    The check is a stand-in for "can we submit jobs at all" — it
    exercises both the kubeconfig and the API server. When ``kubectl``
    is not on PATH, returns SKIP.
    """
    kubectl = shutil.which("kubectl")
    if not kubectl:
        return _executor_check(
            "kubernetes",
            CheckStatus.SKIP,
            "kubectl not installed",
            detail="Install kubectl from https://kubernetes.io/docs/tasks/tools/",
        )
    rc, _stdout, stderr = _run_subprocess_quiet(
        ["kubectl", "auth", "can-i", "get", "jobs"], timeout_s=5.0
    )
    if rc is None:
        return _executor_check(
            "kubernetes",
            CheckStatus.WARN,
            "kubectl auth probe timed out",
            detail=stderr.strip(),
        )
    if rc != 0:
        return _executor_check(
            "kubernetes",
            CheckStatus.WARN,
            "kubectl auth probe failed",
            detail=stderr.strip() or f"exit code {rc}",
        )
    return _executor_check(
        "kubernetes",
        CheckStatus.PASS,
        "kubectl reachable; can get jobs",
        detail="Use --kubernetes-namespace <name> to override the default namespace.",
    )


def _check_docker_swarm() -> CheckResult:
    """Verify the Docker Swarm substrate.

    Probes the local Docker socket via ``docker info`` (works against
    any reachable daemon, swarm mode or not). When ``docker`` is not on
    PATH, returns SKIP.
    """
    docker = shutil.which("docker")
    if not docker:
        return _executor_check(
            "docker_swarm",
            CheckStatus.SKIP,
            "docker CLI not installed",
            detail="Install Docker from https://docs.docker.com/engine/install/",
        )
    rc, _stdout, stderr = _run_subprocess_quiet(["docker", "info"], timeout_s=5.0)
    if rc is None or rc != 0:
        return _executor_check(
            "docker_swarm",
            CheckStatus.WARN,
            "docker daemon not reachable",
            detail=stderr.strip() or "docker info failed",
        )
    return _executor_check(
        "docker_swarm",
        CheckStatus.PASS,
        f"docker daemon reachable ({docker})",
        detail="Use --docker-swarm-network <name> to override the default network.",
    )


def _check_dask_jobqueue() -> CheckResult:
    """Verify the Dask-JobQueue substrate (Python SDK).

    The dask-jobqueue executor wraps ``dask_jobqueue.SLURMCluster`` /
    ``PBSCluster`` / ``KubeCluster``. We check the SDK is importable;
    cluster connectivity is the Slurm/PBS/Kubernetes check above. When
    dask-jobqueue is not installed, returns SKIP.
    """
    try:
        import dask_jobqueue  # noqa: PLC0415, F401
    except ImportError:
        return _executor_check(
            "dask_jobqueue",
            CheckStatus.SKIP,
            "dask-jobqueue not installed",
            detail="Install with: pip install 'osimflow[dask]'",
        )
    # Probe for at least one cluster backend.
    backends: list[str] = []
    for backend, dep in (
        ("SLURMCluster", "submitit"),
        ("PBSCluster", None),
        ("KubeCluster", "kubernetes"),
    ):
        try:
            getattr(dask_jobqueue, backend)  # noqa: B009
        except AttributeError:
            continue
        if dep is None or importlib.util.find_spec(dep) is not None:
            backends.append(backend)
    if not backends:
        return _executor_check(
            "dask_jobqueue",
            CheckStatus.WARN,
            "no dask-jobqueue backends importable",
            detail=(
                "Install one of: pip install 'osimflow[slurm]' (SLURMCluster), "
                "'osimflow[dask]' (PBSCluster), 'osimflow[kubernetes]' (KubeCluster)."
            ),
        )
    return _executor_check(
        "dask_jobqueue",
        CheckStatus.PASS,
        f"dask-jobqueue installed ({', '.join(backends)})",
        detail="Use --dask-cluster-type <type> to pick a backend at runtime.",
    )


def _check_task_payload_signing(configured_executor: str | None) -> CheckResult:
    """Warn when the HMAC task-payload contract is mis-wired (issue #1404).

    ``remote_runner`` fails closed (issue #1205): with no
    ``OSIMFLOW_TASK_PAYLOAD_SECRET`` configured, every per-sample job on a
    payload-dispatching executor refuses to execute; with a secret
    configured, an executor that does not sign its payloads fails
    signature verification. This check makes both misconfigurations a
    first-class health finding instead of a runtime surprise.

    Returns SKIP for executors that do not dispatch via the remote-runner
    payload contract, WARN for the two broken combinations above, and
    PASS when the orchestrator secret is set and the executor signs.
    """
    name = _EXECUTOR_HEALTH_CHECK_NAME.format(name="task-payload-signing")
    if configured_executor is None:
        return CheckResult(
            name=name,
            status=CheckStatus.SKIP,
            category=CheckCategory.INFORMATIONAL,
            message="Skipped (no --executor selected)",
            detail="Task-payload signing is only checked for the configured executor.",
        )
    try:
        from osimflow.executors import ExecutorRegistry  # noqa: PLC0415

        cls = ExecutorRegistry.get(configured_executor)
    except (ValueError, ImportError) as exc:
        return CheckResult(
            name=name,
            status=CheckStatus.SKIP,
            category=CheckCategory.INFORMATIONAL,
            message=f"Skipped (executor {configured_executor!r} unavailable)",
            detail=str(exc),
        )
    # The overrides return constants, so evaluating the property on a
    # bare instance (no __init__) is safe and avoids constructing a
    # fully-configured executor here.
    requires_payload = cls.__new__(cls).requires_remote_runner_payload
    if not requires_payload:
        return CheckResult(
            name=name,
            status=CheckStatus.SKIP,
            category=CheckCategory.INFORMATIONAL,
            message=f"{configured_executor} does not use the remote-runner payload contract",
            detail="Task-payload HMAC signing is not applicable to this executor.",
        )

    secret_set = bool(os.environ.get("OSIMFLOW_TASK_PAYLOAD_SECRET"))
    if not secret_set:
        return CheckResult(
            name=name,
            status=CheckStatus.WARN,
            category=CheckCategory.INFORMATIONAL,
            message=(
                f"OSIMFLOW_TASK_PAYLOAD_SECRET is not set; {configured_executor} "
                "per-sample jobs will refuse to execute (remote_runner fails closed)"
            ),
            detail=(
                "Configure OSIMFLOW_TASK_PAYLOAD_SECRET on the orchestrator "
                "to enable HMAC-SHA256 payload verification (issue #1205)."
            ),
        )
    if not getattr(cls, "signs_task_payload", False):
        return CheckResult(
            name=name,
            status=CheckStatus.WARN,
            category=CheckCategory.INFORMATIONAL,
            message=(
                f"{configured_executor} requires the payload contract but does not "
                "sign OSIMFLOW_TASK_PAYLOAD — per-sample jobs will fail signature "
                "verification even though the orchestrator has a secret configured"
            ),
            detail=(
                "The executor swallows OSIMFLOW_TASK_PAYLOAD_SECRET instead of "
                "propagating it via build_signature_env (issue #1404)."
            ),
        )
    return CheckResult(
        name=name,
        status=CheckStatus.PASS,
        category=CheckCategory.INFORMATIONAL,
        message=(
            f"{configured_executor} signs OSIMFLOW_TASK_PAYLOAD and the "
            "orchestrator secret is configured"
        ),
    )
