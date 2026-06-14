"""BYO monitoring for OSimFlow campaigns.

Three always-on pieces (per `.agents/results/monitoring-decision.md`):

  1. `RunTrace` collects per-step + per-sample metrics and writes a single
     `run.json` to `${outdir}/run.json` at the end of the campaign.
     Schema is documented in `docs/monitoring-schema.md`.

  2. `tqdm` progress bar for terminal users. Soft-dependency: if tqdm
     is not installed, the progress hook degrades to a logger.info
     line per step.

  3. Per-sample stdout/stderr log files at
     `${outdir}/work/sim/<sample_id>/stdout.log` and `stderr.log`.
     The Campaign writes the per-step `subprocess.run(...)` outputs
     into these files so the user can `cat` them without re-running.

Optional pieces (deferred to post-MVP):
  - MLflow integration (~30 LoC) behind a `--mlflow_tracking_uri` flag.
  - Streamlit dashboard for browsing past `run.json` files (~100 LoC).
"""

from __future__ import annotations

import dataclasses
import json
import logging
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

log = logging.getLogger("osimflow.monitoring")

HEARTBEAT_INTERVAL_SEC = 30
STALE_THRESHOLD_SEC = 60

# Soft dependency: tqdm is preferred but not required.
try:
    from tqdm.auto import tqdm

    _HAS_TQDM = True
except ImportError:  # pragma: no cover
    tqdm = None
    _HAS_TQDM = False


@dataclasses.dataclass
class StepTrace:
    """One row in the run.json `steps` array."""

    step: str
    cache: str  # "HIT", "MISS", "HIT×N", "MISS×N", "SKIPPED"
    elapsed_s: float
    exit_code: int

    def to_dict(self) -> dict[str, object]:
        return dataclasses.asdict(self)


@dataclasses.dataclass
class SampleTrace:
    """One row in the run.json `per_sample` array."""

    sample_id: str
    status: str  # "ok", "failed", "cached"
    elapsed_s: float
    apply_exit_code: int = 0
    sim_exit_code: int = 0
    extract_exit_code: int = 0
    eplusout_sql: str | None = None  # path if produced
    error_summary: str | None = None
    stdout_log: str | None = None
    stderr_log: str | None = None
    quality_valid: bool | None = None
    quality_warnings: int | None = None
    quality_failures: int | None = None
    generation: int | None = None  # generation index for iterative algorithms (issue #106)
    # Per-data-point worker tracking (issue #105).
    worker_id: str | None = None  # Batch job ID / Slurm job ID / Nomad alloc ID / "local"
    worker_ip: str | None = None  # IP address or hostname of the worker
    worker_region: str | None = None  # AWS region / Nomad datacenter
    # Per-sample cost tracking (issue #126).
    cost_usd: float | None = None  # estimated cost for this sample
    billed_duration_seconds: float | None = None  # wall time billed
    # runner.registerValue outputs captured from OpenStudio CLI (issue #251).
    register_values: dict[str, object] | None = None

    def to_dict(self) -> dict[str, object]:
        return {k: v for k, v in dataclasses.asdict(self).items() if v is not None}


@dataclasses.dataclass
class GenerationTrace:
    """Per-generation summary for iterative algorithms (issue #270).

    Written to ``run.json`` under the ``generations`` key so users can
    track optimisation progress across generations.
    """

    generation: int
    n_samples: int
    n_succeeded: int
    n_failed: int
    converged: bool = False
    best_objective: float | None = None
    elapsed_s: float = 0.0

    def to_dict(self) -> dict[str, object]:
        return {k: v for k, v in dataclasses.asdict(self).items() if v is not None}


class RunTrace:
    """The per-campaign monitoring record. Written to run.json at end."""

    SCHEMA_VERSION = 1

    def __init__(self, campaign_id: str, config_summary: dict[str, object]):
        self.campaign_id = campaign_id
        self.started_at = time.time()
        self.finished_at: float | None = None
        self.status: str | None = None  # "ok", "cancelled", "failed"
        self.config_summary = config_summary
        self.steps: list[StepTrace] = []
        self.per_sample: list[SampleTrace] = []
        # Per-generation summary for iterative algorithms (issue #270).
        self.generations: list[GenerationTrace] = []
        # Baseline comparison data (issue #64). Populated after
        # EXTRACT_KPIS when cfg.baseline is defined. Contains keys
        # like baseline_eui, min_improvement_pct, max_improvement_pct.
        self.baseline_comparison: dict[str, object] | None = None
        # Pre/post campaign hook timing (issue #108).
        self.init_script_duration_s: float | None = None
        self.finalize_script_duration_s: float | None = None
        # Per-campaign cost tracking (issue #126).
        self.total_cost_usd: float = 0.0
        self.spot_savings_usd: float = 0.0
        # tqdm handles; one per fan-out step that wants a progress bar.
        self._bars: dict[str, Any] = {}

    # ------------------------------------------------------------------
    # Step hooks (called by the Campaign)
    # ------------------------------------------------------------------
    def step_started(self, step: str, total: int | None = None) -> None:
        """Optionally show a tqdm bar for the step. Total = expected item count."""
        if _HAS_TQDM and total and total > 1:
            self._bars[step] = tqdm(total=total, desc=step, unit="sample")
        else:
            log.info("step %s started", step)

    def step_item_done(self, step: str, status: str = "ok") -> None:
        bar = self._bars.get(step)
        if bar is not None:
            bar.update(1)
            if status != "ok":
                bar.set_postfix_str(f"last={status}")
        # No-op for non-bar case (per-item log spam avoided).

    def step_finished(self, step: str, cache: str, elapsed_s: float, exit_code: int) -> None:
        bar = self._bars.pop(step, None)
        if bar is not None:
            bar.close()
        self.steps.append(
            StepTrace(
                step=step,
                cache=cache,
                elapsed_s=elapsed_s,
                exit_code=exit_code,
            )
        )
        log.info("step %s done cache=%s elapsed=%.2fs exit=%d", step, cache, elapsed_s, exit_code)

    def sample_done(self, trace: SampleTrace) -> None:
        self.per_sample.append(trace)

    def generation_done(self, trace: GenerationTrace) -> None:
        """Record a completed generation (issue #270)."""
        self.generations.append(trace)

    # ------------------------------------------------------------------
    # Serialization
    # ------------------------------------------------------------------
    def finalize(self) -> None:
        self.finished_at = time.time()

    def to_dict(self) -> dict[str, object]:
        n_succeeded = sum(1 for s in self.per_sample if s.status == "ok")
        n_failed = sum(1 for s in self.per_sample if s.status == "failed")
        n_quality_failures = sum(1 for s in self.per_sample if s.quality_valid is False)
        n_quality_warnings = sum(
            1
            for s in self.per_sample
            if s.quality_warnings is not None
            and s.quality_warnings > 0
            and s.quality_valid is not False
        )
        d: dict[str, object] = {
            "schema_version": self.SCHEMA_VERSION,
            "campaign_id": self.campaign_id,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "status": self.status,
            "elapsed_s": (self.finished_at or time.time()) - self.started_at,
            "config": self.config_summary,
            "summary": {
                "n_samples": len(self.per_sample),
                "n_succeeded": n_succeeded,
                "n_failed": n_failed,
            },
            "quality_summary": {
                "n_quality_failures": n_quality_failures,
                "n_quality_warnings": n_quality_warnings,
                "n_quality_ok": n_succeeded - n_quality_failures - n_quality_warnings,
            },
            "steps": [s.to_dict() for s in self.steps],
            "per_sample": [s.to_dict() for s in self.per_sample],
        }
        # Per-generation summary (issue #270). Only present when the
        # campaign ran multiple generations (iterative algorithms).
        if self.generations:
            d["generations"] = [g.to_dict() for g in self.generations]
        if self.baseline_comparison is not None:
            d["baseline_comparison"] = self.baseline_comparison
        if self.init_script_duration_s is not None:
            d["init_script_duration_s"] = self.init_script_duration_s
        if self.finalize_script_duration_s is not None:
            d["finalize_script_duration_s"] = self.finalize_script_duration_s
        # Cost summary (issue #126). Always present; defaults to 0.0.
        d["total_cost_usd"] = self.total_cost_usd
        d["spot_savings_usd"] = self.spot_savings_usd
        return d

    def update_sample(self, trace: SampleTrace) -> None:
        """Update a single sample entry in run.json (incremental checkpoint).

        Reads the existing run.json, merges *trace* into the ``per_sample``
        list (updates existing entry or appends), and writes atomically.
        Used by the Campaign to checkpoint per-sample progress after each
        step completes so SSE clients see live updates without waiting for
        campaign end.

        If run.json does not exist yet (campaign just started but the file
        was not yet written), creates a minimal run.json with the sample
        entry so monitoring tools always have something to read.
        """
        path = Path(self._checkpoint_path) if hasattr(self, "_checkpoint_path") else None
        if path is None:
            return

        if not path.exists():
            data: dict[str, object] = {
                "schema_version": self.SCHEMA_VERSION,
                "campaign_id": self.campaign_id,
                "started_at": self.started_at,
                "finished_at": None,
                "elapsed_s": time.time() - self.started_at,
                "config": self.config_summary,
                "summary": {
                    "n_samples": 1,
                    "n_succeeded": 1 if trace.status == "ok" else 0,
                    "n_failed": 1 if trace.status == "failed" else 0,
                },
                "quality_summary": {
                    "n_quality_failures": 0,
                    "n_quality_warnings": 0,
                    "n_quality_ok": 1 if trace.status == "ok" else 0,
                },
                "steps": [],
                "per_sample": [trace.to_dict()],
                "total_cost_usd": 0.0,
                "spot_savings_usd": 0.0,
            }
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=2, default=str))
            tmp.rename(path)
            log.info("created incremental run.json at %s", path)
            return

        try:
            data = cast(dict[str, Any], json.loads(path.read_text()))
        except (json.JSONDecodeError, OSError):
            return

        samples: list[dict[str, object]] = data.get("per_sample", [])  # type: ignore[assignment]
        replaced = False
        for i, s in enumerate(samples):
            if s.get("sample_id") == trace.sample_id:
                samples[i] = trace.to_dict()
                replaced = True
                break
        if not replaced:
            samples.append(trace.to_dict())
        data["per_sample"] = samples

        n_succeeded = sum(1 for s in samples if s.get("status") == "ok")
        n_failed = sum(1 for s in samples if s.get("status") == "failed")
        if "summary" not in data:
            data["summary"] = {}
        data["summary"]["n_succeeded"] = n_succeeded  # type: ignore[index]
        data["summary"]["n_failed"] = n_failed  # type: ignore[index]
        data["summary"]["n_samples"] = len(samples)  # type: ignore[index]

        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, default=str))
        tmp.rename(path)

    def write(self, path: Path) -> None:
        """Write the run.json trace to disk. Idempotent."""
        path.parent.mkdir(parents=True, exist_ok=True)
        # Record path so update_sample() can do incremental writes.
        self._checkpoint_path = str(path)
        path.write_text(json.dumps(self.to_dict(), indent=2, default=str))
        log.info("wrote run trace to %s", path)


# ---------------------------------------------------------------------------
# Per-sample log files
# ---------------------------------------------------------------------------
def sample_log_paths(outdir: Path, sample_id: str) -> tuple[Path, Path]:
    """Return the (stdout.log, stderr.log) paths for a sample's run step.

    The Campaign creates these by passing them to the executor's `submit()`
    call so the per-step subprocess writes to a known location.
    """
    d = outdir / "work" / "sim" / sample_id
    d.mkdir(parents=True, exist_ok=True)
    return d / "stdout.log", d / "stderr.log"


# ---------------------------------------------------------------------------
# Worker heartbeat (issue #341)
# ---------------------------------------------------------------------------


class WorkerHeartbeat:
    """Writes a heartbeat file every HEARTBEAT_INTERVAL_SEC while a job runs.

    The heartbeat file is written to
    ``${outdir}/work/sim/<sample_id>/heartbeat.json`` and contains:
    ``worker_id``, ``sample_id``, ``last_seen`` (ISO timestamp), and
    ``job_handle`` state. A watchdog can use this to detect stale workers.
    """

    def __init__(
        self,
        outdir: Path,
        sample_id: str,
        worker_id: str,
        job_handle_state: str = "running",
    ) -> None:
        self.outdir = outdir
        self.sample_id = sample_id
        self.worker_id = worker_id
        self.job_handle_state = job_handle_state
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._path: Path | None = None

    def _heartbeat_path(self) -> Path:
        if self._path is None:
            d = self.outdir / "work" / "sim" / self.sample_id
            d.mkdir(parents=True, exist_ok=True)
            self._path = d / "heartbeat.json"
        return self._path

    def _write(self) -> None:
        data = {
            "worker_id": self.worker_id,
            "sample_id": self.sample_id,
            "last_seen": datetime.now(UTC).isoformat(),
            "job_handle": self.job_handle_state,
        }
        path = self._heartbeat_path()
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, default=str))
        tmp.rename(path)

    def start(self) -> None:
        """Start the background heartbeat writer thread."""
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name=f"heartbeat-{self.sample_id}"
        )
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop_event.wait(HEARTBEAT_INTERVAL_SEC):
            try:
                self._write()
            except Exception as exc:
                log.warning("heartbeat write failed for %s: %s", self.sample_id, exc)

    def stop(self) -> None:
        """Stop the heartbeat writer and write a final 'stopped' entry."""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
        self.job_handle_state = "stopped"
        try:
            self._write()
        except Exception as exc:
            log.warning("final heartbeat write failed for %s: %s", self.sample_id, exc)

    def update_state(self, state: str) -> None:
        """Update the job_handle state (e.g. 'completed', 'failed')."""
        self.job_handle_state = state
        try:
            self._write()
        except Exception as exc:
            log.warning("heartbeat state update failed for %s: %s", self.sample_id, exc)


def check_heartbeat(outdir: Path, sample_id: str) -> bool:
    """Return True if the heartbeat for *sample_id* is stale (> STALE_THRESHOLD_SEC old).

    A stale heartbeat indicates the worker has not updated it in the
    expected interval, suggesting the job is hung or the worker crashed.
    """
    path = outdir / "work" / "sim" / sample_id / "heartbeat.json"
    if not path.is_file():
        return True
    try:
        data = json.loads(path.read_text())
        last_seen_str = data.get("last_seen")
        if not last_seen_str:
            return True
        last_seen = datetime.fromisoformat(last_seen_str)
        age = time.time() - last_seen.timestamp()
        return age > STALE_THRESHOLD_SEC
    except (json.JSONDecodeError, OSError, ValueError):
        return True
