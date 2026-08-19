"""CLI health check module (issue #411).

Verifies system health before starting a campaign.

Checks are categorized as:

* **Critical** — Python version, core packages, SQLite, write permissions.
  A failure here means OSimFlow cannot run at all.
* **Informational** — OpenStudio CLI, Docker/Podman, optional packages,
  network connectivity, disk space, and per-executor substrate checks
  (issue #1024). A failure here limits functionality but does not block
  basic local runs. The exception is the check for the *configured*
  executor (``configured_executor=`` in :func:`run_health_checks`): that
  one is promoted to CRITICAL because a failure means the campaign
  cannot dispatch any sample.

Usage::

    from osimflow.health import run_health_checks, format_results

    results = run_health_checks(outdir=Path("."), skip_network=False)
    print(format_results(results))
    exit_code = 1 if any(r.failed and r.critical for r in results) else 0
"""

from __future__ import annotations

import contextlib
import importlib.util
import json
import os
import platform
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import Path

log_target = __name__  # re-exported below via logging.getLogger

# Minimum Python version required by OSimFlow.
MIN_PYTHON_VERSION: tuple[int, int] = (3, 12)

# Core packages that must be importable for OSimFlow to function.
CORE_PACKAGES: list[str] = [
    "numpy",
    "scipy",
    "pandas",
    "pyarrow",
    "matplotlib",
    "seaborn",
    "tqdm",
    "openpyxl",
    "yaml",  # PyYAML
]

# submitit is only required on non-Windows.
if platform.system() != "Windows":
    CORE_PACKAGES.append("submitit")

# Optional packages grouped by feature area.
OPTIONAL_PACKAGES: dict[str, list[str]] = {
    "aws": ["boto3"],
    "slurm": ["submitit"],
    "mlflow": ["mlflow"],
    "sensitivity": ["SALib"],
    "optimization": ["pymoo"],
    "kubernetes": ["kubernetes"],
    "api": ["fastapi", "uvicorn"],
    "dask": ["dask_jobqueue"],
    "tui": ["rich"],
}

# External CLI tools to check.
# Maps display name → list of binary names to search for on PATH.
EXTERNAL_TOOLS: dict[str, list[str]] = {
    "OpenStudio CLI": ["openstudio", "openstudio.cli"],
    "Docker": ["docker"],
    "Podman": ["podman"],
}

# Network check endpoint — lightweight HEAD request.
NETWORK_CHECK_URL = "https://pypi.org/pypi/osimflow/json"
NETWORK_TIMEOUT_S = 3


class CheckStatus(StrEnum):
    """Status of an individual health check."""

    PASS = "pass"
    FAIL = "fail"
    WARN = "warn"
    SKIP = "skip"


class CheckCategory(StrEnum):
    """Severity category for a health check."""

    CRITICAL = "critical"
    INFORMATIONAL = "informational"


@dataclass(frozen=True)
class CheckResult:
    """Result of a single health check.

    Attributes:
        name: Human-readable name of the check.
        status: Pass/fail/warn/skip.
        category: Critical or informational.
        message: Short one-line summary.
        detail: Extended explanation (optional).
    """

    name: str
    status: CheckStatus
    category: CheckCategory
    message: str
    detail: str = ""

    @property
    def failed(self) -> bool:
        """True if the check failed."""
        return self.status == CheckStatus.FAIL

    @property
    def critical(self) -> bool:
        """True if this check is critical."""
        return self.category == CheckCategory.CRITICAL

    def to_dict(self) -> dict[str, str]:
        """Serialize to a plain dict for JSON output."""
        return {
            "name": self.name,
            "status": self.status.value,
            "category": self.category.value,
            "message": self.message,
            "detail": self.detail,
        }


@dataclass
class HealthReport:
    """Aggregated report from all health checks."""

    results: list[CheckResult] = field(default_factory=list)

    @property
    def all_critical_pass(self) -> bool:
        """True if no critical check failed."""
        return not any(r.failed and r.critical for r in self.results)

    def to_dict(self) -> dict[str, object]:
        """Serialize the full report to a plain dict."""
        n_pass = sum(1 for r in self.results if r.status == CheckStatus.PASS)
        n_fail = sum(1 for r in self.results if r.status == CheckStatus.FAIL)
        n_warn = sum(1 for r in self.results if r.status == CheckStatus.WARN)
        n_skip = sum(1 for r in self.results if r.status == CheckStatus.SKIP)
        n_critical_fail = sum(1 for r in self.results if r.failed and r.critical)
        return {
            "summary": {
                "total": len(self.results),
                "passed": n_pass,
                "failed": n_fail,
                "warnings": n_warn,
                "skipped": n_skip,
                "critical_failures": n_critical_fail,
                "healthy": self.all_critical_pass,
            },
            "checks": [r.to_dict() for r in self.results],
        }


# ---------------------------------------------------------------------------
# Individual check functions
# ---------------------------------------------------------------------------


def _check_python_version() -> CheckResult:
    """Verify Python >= 3.12."""
    current = sys.version_info[:2]
    version_str = f"{current[0]}.{current[1]}"
    detail = f"Running Python {version_str} ({platform.python_implementation()})"
    if current >= MIN_PYTHON_VERSION:
        return CheckResult(
            name="Python Version",
            status=CheckStatus.PASS,
            category=CheckCategory.CRITICAL,
            message=f"Python {version_str} >= {MIN_PYTHON_VERSION[0]}.{MIN_PYTHON_VERSION[1]}",
            detail=detail,
        )
    return CheckResult(
        name="Python Version",
        status=CheckStatus.FAIL,
        category=CheckCategory.CRITICAL,
        message=f"Python {version_str} < required {MIN_PYTHON_VERSION[0]}.{MIN_PYTHON_VERSION[1]}",
        detail=f"{detail}. Please upgrade to Python {MIN_PYTHON_VERSION[0]}.{MIN_PYTHON_VERSION[1]}+.",
    )


def _check_package(name: str) -> tuple[bool, str | None]:
    """Check if a Python package is importable.

    Returns ``(found, version_string_or_none)``.
    """
    try:
        spec = importlib.util.find_spec(name)
    except (ValueError, ModuleNotFoundError):
        # find_spec raises ValueError when a module is in sys.modules
        # but has a missing/corrupted __spec__ (e.g., after moto or
        # other test frameworks manipulate sys.modules). Fall back to
        # checking sys.modules directly.
        spec = None

    if spec is None and name not in sys.modules:
        return (False, None)
    # Try to get version from importlib.metadata.
    version: str | None = None
    try:
        from importlib.metadata import PackageNotFoundError  # noqa: PLC0415
        from importlib.metadata import version as _version  # noqa: PLC0415

        dist_name = name.replace("_", "-") if name != "yaml" else "PyYAML"
        version = _version(dist_name)
    except (PackageNotFoundError, Exception):
        # importlib.metadata may fail for namespace packages; ignore.
        version = None
    return (True, version)


def _check_core_packages() -> CheckResult:
    """Verify all core Python packages are importable."""
    missing: list[str] = []
    installed: list[str] = []
    for pkg in CORE_PACKAGES:
        found, ver = _check_package(pkg)
        if found:
            label = f"{pkg}=={ver}" if ver else pkg
            installed.append(label)
        else:
            missing.append(pkg)

    detail_parts = installed
    if missing:
        detail_parts.append(f"MISSING: {', '.join(missing)}")
    detail = "; ".join(detail_parts)

    if not missing:
        return CheckResult(
            name="Core Packages",
            status=CheckStatus.PASS,
            category=CheckCategory.CRITICAL,
            message=f"All {len(CORE_PACKAGES)} core packages installed",
            detail=detail,
        )
    return CheckResult(
        name="Core Packages",
        status=CheckStatus.FAIL,
        category=CheckCategory.CRITICAL,
        message=f"Missing: {', '.join(missing)}",
        detail=f"{detail}\nInstall with: pip install -e '.[dev]'",
    )


def _check_optional_packages() -> CheckResult:
    """Report optional package availability (informational)."""
    found: list[str] = []
    missing_groups: list[str] = []
    for group, pkgs in OPTIONAL_PACKAGES.items():
        group_found: list[str] = []
        group_missing: list[str] = []
        for pkg in pkgs:
            ok, ver = _check_package(pkg)
            label = f"{pkg}=={ver}" if ok and ver else pkg
            if ok:
                group_found.append(label)
            else:
                group_missing.append(pkg)
        if group_found:
            found.extend(group_found)
        if group_missing:
            missing_groups.append(f"{group}: {', '.join(group_missing)}")

    installed_str = "; ".join(found) if found else "(none)"
    detail = f"Installed: {installed_str}"
    if missing_groups:
        detail += f"\nNot installed: {'; '.join(missing_groups)}"
        detail += "\nInstall extras with e.g. pip install osimflow[aws,slurm]"

    status = CheckStatus.WARN if missing_groups else CheckStatus.PASS
    n_found = len(found)
    n_total = sum(len(pkgs) for pkgs in OPTIONAL_PACKAGES.values())
    return CheckResult(
        name="Optional Packages",
        status=status,
        category=CheckCategory.INFORMATIONAL,
        message=f"{n_found}/{n_total} optional packages installed",
        detail=detail,
    )


def _check_sqlite() -> CheckResult:
    """Verify SQLite is functional (create, write, read, delete)."""
    try:
        # Use a temp file so we exercise the filesystem + SQLite together.
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            tmp_path = Path(f.name)
        try:
            conn = sqlite3.connect(str(tmp_path))
            conn.execute("CREATE TABLE _health (val INTEGER)")
            conn.execute("INSERT INTO _health VALUES (42)")
            row = conn.execute("SELECT val FROM _health").fetchone()
            conn.execute("DROP TABLE _health")
            conn.commit()
            conn.close()
            if row and row[0] == 42:
                ver = sqlite3.sqlite_version
                return CheckResult(
                    name="SQLite",
                    status=CheckStatus.PASS,
                    category=CheckCategory.CRITICAL,
                    message=f"SQLite {ver} functional",
                    detail="Create / insert / select / drop succeeded.",
                )
            return CheckResult(
                name="SQLite",
                status=CheckStatus.FAIL,
                category=CheckCategory.CRITICAL,
                message="SQLite read/write returned unexpected value",
                detail=f"Expected 42, got {row}",
            )
        finally:
            tmp_path.unlink(missing_ok=True)
    except Exception as exc:
        return CheckResult(
            name="SQLite",
            status=CheckStatus.FAIL,
            category=CheckCategory.CRITICAL,
            message=f"SQLite test failed: {exc}",
            detail=str(exc),
        )


def _check_write_permissions(outdir: Path) -> CheckResult:
    """Verify write permissions in the given directory."""
    resolved = outdir.resolve()
    try:
        resolved.mkdir(parents=True, exist_ok=True)
    except PermissionError:
        return CheckResult(
            name="Write Permissions",
            status=CheckStatus.FAIL,
            category=CheckCategory.CRITICAL,
            message=f"Cannot create directory: {resolved}",
            detail="Permission denied.",
        )
    except OSError as exc:
        return CheckResult(
            name="Write Permissions",
            status=CheckStatus.FAIL,
            category=CheckCategory.CRITICAL,
            message=f"Cannot access directory: {resolved}",
            detail=str(exc),
        )

    test_file = resolved / ".osimflow_health_test"
    try:
        test_file.write_text("ok")
        content = test_file.read_text()
        test_file.unlink()
        if content != "ok":
            return CheckResult(
                name="Write Permissions",
                status=CheckStatus.FAIL,
                category=CheckCategory.CRITICAL,
                message=f"Write/read mismatch in {resolved}",
                detail=f"Wrote 'ok', read '{content}'",
            )
    except PermissionError:
        return CheckResult(
            name="Write Permissions",
            status=CheckStatus.FAIL,
            category=CheckCategory.CRITICAL,
            message=f"Cannot write to: {resolved}",
            detail="Permission denied.",
        )
    except OSError as exc:
        return CheckResult(
            name="Write Permissions",
            status=CheckStatus.FAIL,
            category=CheckCategory.CRITICAL,
            message=f"File I/O error in {resolved}",
            detail=str(exc),
        )

    return CheckResult(
        name="Write Permissions",
        status=CheckStatus.PASS,
        category=CheckCategory.CRITICAL,
        message=f"Writable: {resolved}",
        detail="Write + read + delete succeeded.",
    )


def _check_external_tools() -> list[CheckResult]:
    """Check availability of external CLI tools (OpenStudio, Docker, Podman)."""
    results: list[CheckResult] = []
    for display_name, binaries in EXTERNAL_TOOLS.items():
        path_found: str | None = None
        for binary in binaries:
            path_found = shutil.which(binary)
            if path_found:
                break
        if path_found:
            results.append(
                CheckResult(
                    name=display_name,
                    status=CheckStatus.PASS,
                    category=CheckCategory.INFORMATIONAL,
                    message=f"Found at {path_found}",
                    detail=f"Binary: {binaries[0]}",
                ),
            )
        else:
            hint = ""
            if display_name == "OpenStudio CLI":
                hint = " Required for real simulations (stub mode available without it)."
            elif display_name in ("Docker", "Podman"):
                hint = " Required for container-based simulations."
            results.append(
                CheckResult(
                    name=display_name,
                    status=CheckStatus.WARN,
                    category=CheckCategory.INFORMATIONAL,
                    message="Not found on PATH",
                    detail=f"Searched for: {', '.join(binaries)}.{hint}",
                ),
            )
    return results


def _check_disk_space(outdir: Path) -> CheckResult:
    """Report available disk space (informational)."""
    resolved = outdir.resolve()
    try:
        resolved.mkdir(parents=True, exist_ok=True)
    except OSError:
        resolved = Path.cwd()

    usage = shutil.disk_usage(str(resolved))
    free_gb = usage.free / (1024**3)
    total_gb = usage.total / (1024**3)

    # Warn if less than 1 GB free.
    status = CheckStatus.WARN if free_gb < 1.0 else CheckStatus.PASS
    threshold_note = ""
    if free_gb < 1.0:
        threshold_note = " — LOW! Campaigns may fail due to insufficient space."

    return CheckResult(
        name="Disk Space",
        status=status,
        category=CheckCategory.INFORMATIONAL,
        message=f"{free_gb:.1f} GB free of {total_gb:.1f} GB",
        detail=f"Directory: {resolved}{threshold_note}",
    )


def _check_network() -> CheckResult:
    """Check network connectivity to PyPI (informational)."""
    try:
        req = urllib.request.Request(NETWORK_CHECK_URL, method="HEAD")
        with urllib.request.urlopen(req, timeout=NETWORK_TIMEOUT_S) as resp:  # noqa: S310
            status_code = resp.status
        if 200 <= status_code < 400:
            return CheckResult(
                name="Network Connectivity",
                status=CheckStatus.PASS,
                category=CheckCategory.INFORMATIONAL,
                message=f"Reachable ({status_code})",
                detail=f"Connected to {NETWORK_CHECK_URL} in {NETWORK_TIMEOUT_S}s timeout.",
            )
        return CheckResult(
            name="Network Connectivity",
            status=CheckStatus.WARN,
            category=CheckCategory.INFORMATIONAL,
            message=f"Unexpected HTTP {status_code}",
            detail=f"URL: {NETWORK_CHECK_URL}",
        )
    except Exception as exc:
        return CheckResult(
            name="Network Connectivity",
            status=CheckStatus.WARN,
            category=CheckCategory.INFORMATIONAL,
            message=f"Unreachable: {type(exc).__name__}",
            detail=f"{exc}\nNetwork is not required for local campaigns.",
        )


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
#    regression test in tests/unit/test_health_check.py.
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
    except Exception:  # noqa: BLE001 — never raise from a health check
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
    except Exception as exc:  # noqa: BLE001
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
    except Exception as exc:  # noqa: BLE001 — surface all SDK errors as WARN
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


# Built-in executor ↔ health-check dispatch table. The orchestrator iterates
# ``ExecutorRegistry.iter_health_checks()`` (registered below at module
# import time) — keep these names in sync with the registrations in
# ``_register_executor_health_checks`` at the bottom of the file.
_BUILTIN_EXECUTOR_HEALTH_CHECKS: dict[str, Callable[[], CheckResult]] = {
    "local": _check_local,
    "slurm": _check_slurm,
    "pbs": _check_pbs,
    "aws_batch": _check_aws_batch,
    "azure_batch": _check_azure_batch,
    "google_batch": _check_google_batch,
    "nomad": _check_nomad,
    "kubernetes": _check_kubernetes,
    "docker_swarm": _check_docker_swarm,
    "dask_jobqueue": _check_dask_jobqueue,
}


def _register_executor_health_checks() -> None:
    """Register every built-in executor health check with ExecutorRegistry.

    Called once at module import. Wrapped in a try/except so the rest of
    ``osimflow.health`` keeps working even if executors cannot be imported
    in some exotic test environment — the health check registration is a
    nice-to-have, not a hard dependency of the CLI subcommand.
    """
    try:
        from osimflow.executors import ExecutorRegistry  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return
    for name, fn in _BUILTIN_EXECUTOR_HEALTH_CHECKS.items():
        # Executor not registered yet — happens if a third-party build
        # filters out an executor. We don't want to hard-fail the
        # health module over it; the regression test catches the
        # canonical 10-executor case.
        with contextlib.suppress(ValueError):
            ExecutorRegistry.register_health_check(name, fn)


_register_executor_health_checks()


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def run_health_checks(
    outdir: Path | None = None,
    skip_network: bool = False,
    configured_executor: str | None = None,
) -> HealthReport:
    """Run all health checks and return an aggregated report.

    Args:
        outdir: Directory to check write permissions and disk space in.
            Defaults to the current working directory.
        skip_network: If True, skip the network connectivity check.
        configured_executor: Name of the executor the user intends to
            dispatch the campaign with (e.g. ``"slurm"`` or
            ``"aws_batch"``). When set, the matching per-executor check
            is promoted from INFORMATIONAL to CRITICAL — a failure means
            the campaign cannot dispatch. Issue #1024.

    Returns:
        A :class:`HealthReport` containing all :class:`CheckResult` objects.
    """
    check_dir = outdir if outdir is not None else Path.cwd()
    results: list[CheckResult] = []

    # --- Critical checks ---
    results.append(_check_python_version())
    results.append(_check_core_packages())
    results.append(_check_sqlite())
    results.append(_check_write_permissions(check_dir))

    # --- Informational checks ---
    results.append(_check_optional_packages())
    results.extend(_check_external_tools())
    results.append(_check_disk_space(check_dir))

    if skip_network:
        results.append(
            CheckResult(
                name="Network Connectivity",
                status=CheckStatus.SKIP,
                category=CheckCategory.INFORMATIONAL,
                message="Skipped (--offline)",
                detail="Network check skipped by user request.",
            ),
        )
    else:
        results.append(_check_network())

    # --- Per-executor substrate checks (issue #1024) ---
    # Dispatch through the ExecutorRegistry instead of a hardcoded list.
    # Each registered check returns INFORMATIONAL; the configured executor's
    # check is promoted to CRITICAL below.
    try:
        from osimflow.executors import ExecutorRegistry  # noqa: PLC0415

        registry_available = True
    except Exception:  # noqa: BLE001
        registry_available = False

    if registry_available:
        for name, check_fn in ExecutorRegistry.iter_health_checks():
            try:
                result = check_fn()
            except Exception as exc:  # noqa: BLE001 — never let a single check kill the report
                result = CheckResult(
                    name=_EXECUTOR_HEALTH_CHECK_NAME.format(name=name),
                    status=CheckStatus.WARN,
                    category=CheckCategory.INFORMATIONAL,
                    message=f"check raised {type(exc).__name__}",
                    detail=str(exc),
                )
            if configured_executor is not None and name == configured_executor:
                result = replace(result, category=CheckCategory.CRITICAL)
            results.append(result)

    return HealthReport(results=results)


def format_results(report: HealthReport, *, use_color: bool = True) -> str:
    """Format a health report as a human-readable string.

    Args:
        report: The :class:`HealthReport` to format.
        use_color: If True, use emoji indicators (✅/❌/⚠️/⏭).

    Returns:
        A multi-line string suitable for printing.
    """
    indicators = {
        CheckStatus.PASS: "\u2705" if use_color else "[PASS]",
        CheckStatus.FAIL: "\u274c" if use_color else "[FAIL]",
        CheckStatus.WARN: "\u26a0\ufe0f" if use_color else "[WARN]",
        CheckStatus.SKIP: "\u23ed" if use_color else "[SKIP]",
    }

    lines: list[str] = []
    lines.append("OSimFlow Health Check")
    lines.append("=" * 60)

    # Group by category.
    critical = [r for r in report.results if r.critical]
    informational = [r for r in report.results if not r.critical]

    lines.append("\nCritical Checks:")
    lines.append("-" * 60)
    for r in critical:
        indicator = indicators[r.status]
        lines.append(f"  {indicator} {r.name}: {r.message}")
        if r.detail and r.status in (CheckStatus.FAIL, CheckStatus.WARN):
            for dline in r.detail.split("\n"):
                lines.append(f"       {dline}")

    lines.append("\nInformational Checks:")
    lines.append("-" * 60)
    for r in informational:
        indicator = indicators[r.status]
        lines.append(f"  {indicator} {r.name}: {r.message}")

    # Summary.
    d = report.to_dict()
    summary = d["summary"]
    assert isinstance(summary, dict)
    lines.append("\n" + "=" * 60)
    healthy = summary["healthy"]
    if healthy:
        lines.append(
            f"Result: HEALTHY  "
            f"({summary['passed']} passed, {summary['failed']} failed, "
            f"{summary['warnings']} warnings)"
        )
    else:
        lines.append(
            f"Result: UNHEALTHY  "
            f"({summary['passed']} passed, {summary['failed']} failed, "
            f"{summary['warnings']} warnings)"
        )
        lines.append(f"  {summary['critical_failures']} critical check(s) failed!")

    return "\n".join(lines)


def to_json(report: HealthReport) -> str:
    """Serialize a health report to a JSON string.

    Args:
        report: The :class:`HealthReport` to serialize.

    Returns:
        A JSON string with ``summary`` and ``checks`` keys.
    """
    return json.dumps(report.to_dict(), indent=2)


def get_exit_code(report: HealthReport) -> int:
    """Return the appropriate exit code for a health report.

    Returns 0 if all critical checks pass, 1 otherwise.
    """
    return 0 if report.all_critical_pass else 1
