"""CLI health check module (issue #411).

Verifies system health before starting a campaign.

Checks are categorized as:

* **Critical** — Python version, core packages, SQLite, write permissions.
  A failure here means OSimFlow cannot run at all.
* **Informational** — OpenStudio CLI, Docker/Podman, optional packages,
  network connectivity, disk space. A failure here limits functionality
  but does not block basic local runs.

Usage::

    from osimflow.health import run_health_checks, format_results

    results = run_health_checks(outdir=Path("."), skip_network=False)
    print(format_results(results))
    exit_code = 1 if any(r.failed and r.critical for r in results) else 0
"""

from __future__ import annotations

import importlib.util
import json
import platform
import shutil
import sqlite3
import sys
import tempfile
import urllib.request
from dataclasses import dataclass, field
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
    spec = importlib.util.find_spec(name)
    if spec is None:
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
# Orchestration
# ---------------------------------------------------------------------------


def run_health_checks(
    outdir: Path | None = None,
    skip_network: bool = False,
) -> HealthReport:
    """Run all health checks and return an aggregated report.

    Args:
        outdir: Directory to check write permissions and disk space in.
            Defaults to the current working directory.
        skip_network: If True, skip the network connectivity check.

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
