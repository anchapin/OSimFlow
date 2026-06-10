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
import time
from pathlib import Path
from typing import Any

log = logging.getLogger("osimflow.monitoring")

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

    def to_dict(self) -> dict[str, object]:
        return {k: v for k, v in dataclasses.asdict(self).items() if v is not None}


class RunTrace:
    """The per-campaign monitoring record. Written to run.json at end."""

    SCHEMA_VERSION = 1

    def __init__(self, campaign_id: str, config_summary: dict[str, object]):
        self.campaign_id = campaign_id
        self.started_at = time.time()
        self.finished_at: float | None = None
        self.config_summary = config_summary
        self.steps: list[StepTrace] = []
        self.per_sample: list[SampleTrace] = []
        # Baseline comparison data (issue #64). Populated after
        # EXTRACT_KPIS when cfg.baseline is defined. Contains keys
        # like baseline_eui, min_improvement_pct, max_improvement_pct.
        self.baseline_comparison: dict[str, object] | None = None
        # Pre/post campaign hook timing (issue #108).
        self.init_script_duration_s: float | None = None
        self.finalize_script_duration_s: float | None = None
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
        if self.baseline_comparison is not None:
            d["baseline_comparison"] = self.baseline_comparison
        if self.init_script_duration_s is not None:
            d["init_script_duration_s"] = self.init_script_duration_s
        if self.finalize_script_duration_s is not None:
            d["finalize_script_duration_s"] = self.finalize_script_duration_s
        return d

    def write(self, path: Path) -> None:
        """Write the run.json trace to disk. Idempotent."""
        path.parent.mkdir(parents=True, exist_ok=True)
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
