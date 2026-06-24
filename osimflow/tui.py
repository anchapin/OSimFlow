"""Rich terminal UI for live campaign tracking (issue #197).

Provides an optional ``RichTUI`` class that renders a live-updating
terminal dashboard during campaign execution.  The TUI is a *passive
observer* — it reads ``run.json`` from disk on a polling thread and
never modifies campaign state.

Activation:
  * ``rich >= 13.0`` is installed **and**
  * ``sys.stdout`` is a TTY (i.e. not piped / CI) **and**
  * ``--no-tui`` was **not** passed on the CLI.

When any of those conditions is false the module degrades silently to
standard ``logging`` output — the caller never needs to handle an
exception from this module.

Thread-safety:
  The polling thread is a daemon.  It is started by ``start()`` and
  stopped by ``stop()``.  The render callback is invoked on the polling
  thread but ``rich.live.Live`` handles cross-thread rendering safely.
"""

from __future__ import annotations

import contextlib
import json
import logging
import sys
import threading
import time
from pathlib import Path
from typing import Any

log = logging.getLogger("osimflow.tui")

# ---------------------------------------------------------------------------
# Soft dependency: rich is preferred but not required.
# ---------------------------------------------------------------------------
try:
    from rich.live import Live
    from rich.panel import Panel
    from rich.progress_bar import ProgressBar
    from rich.table import Table
    from rich.text import Text

    _HAS_RICH = True
except ImportError:  # pragma: no cover
    Live = None  # type: ignore[assignment, misc]
    Table = None  # type: ignore[assignment, misc]
    Panel = None  # type: ignore[assignment, misc]
    Text = None  # type: ignore[assignment, misc]
    ProgressBar = None  # type: ignore[assignment, misc]
    _HAS_RICH = False

# Polling interval in seconds.
_POLL_INTERVAL = 0.5


def is_tui_available() -> bool:
    """Return True when all runtime conditions for the TUI are met."""
    return _HAS_RICH and sys.stdout.isatty()


def _read_run_json(path: Path) -> dict[str, Any] | None:
    """Read run.json and return parsed dict, or None if unreadable."""
    try:
        if path.exists():
            data = json.loads(path.read_text())
            if isinstance(data, dict):
                return data
    except (json.JSONDecodeError, OSError):
        pass
    return None


def _build_display(data: dict[str, Any], campaign_elapsed: float) -> Any:
    """Build a Rich renderable from run.json data.

    Returns a Rich Panel containing a progress table and summary bar.
    """
    assert Table is not None  # for type-checker
    assert Panel is not None
    assert Text is not None

    # --- Summary ---
    summary = data.get("summary", {})
    n_total = int(summary.get("n_samples", 0))
    n_succeeded = int(summary.get("n_succeeded", 0))
    n_failed = int(summary.get("n_failed", 0))
    n_queued = max(0, n_total - n_succeeded - n_failed)

    # --- Current step ---
    steps: list[dict[str, Any]] = data.get("steps", [])
    current_step = _infer_current_step(steps)

    # --- Cost ---
    total_cost = float(data.get("total_cost_usd", 0))
    spot_savings = float(data.get("spot_savings_usd", 0))

    # --- Progress table ---
    table = Table(
        title="OSimFlow Campaign",
        show_header=True,
        header_style="bold cyan",
        expand=True,
    )
    table.add_column("Sample", style="white", no_wrap=True)
    table.add_column("Status", no_wrap=True)
    table.add_column("Elapsed", justify="right", style="dim")
    table.add_column("Error", style="red", no_wrap=True, max_width=40)

    per_sample: list[dict[str, Any]] = data.get("per_sample", [])
    for s in per_sample[-20:]:  # show last 20 samples
        sample_id = str(s.get("sample_id", "?"))
        status = str(s.get("status", "?"))
        elapsed = f"{s.get('elapsed_s', 0):.1f}s" if s.get("elapsed_s") else "—"
        error = str(s.get("error_summary", "")) or ""

        if status == "ok":
            status_style = "green"
        elif status == "failed":
            status_style = "red"
        elif status == "cached":
            status_style = "dim"
        else:
            status_style = "yellow"

        table.add_row(sample_id, f"[{status_style}]{status}[/]", elapsed, error)

    # --- Summary bar ---
    parts: list[str] = []
    if current_step:
        parts.append(f"Step: [bold]{current_step}[/]")
    parts.append(f"Total: {n_total}")
    parts.append(f"[green]✓ {n_succeeded}[/]")
    parts.append(f"[red]✗ {n_failed}[/]")
    if n_queued > 0:
        parts.append(f"[yellow]◷ {n_queued}[/]")
    if total_cost > 0:
        parts.append(f"Cost: ${total_cost:.4f}")
        if spot_savings > 0:
            parts.append(f"Saved: ${spot_savings:.4f}")
    parts.append(f"Wall: {campaign_elapsed:.0f}s")

    summary_text = " │ ".join(parts)

    return Panel(
        table,
        subtitle=summary_text,
        border_style="blue",
        padding=(0, 1),
    )


def _infer_current_step(steps: list[dict[str, Any]]) -> str:
    """Return the name of the currently running DAG step from the steps list.

    The last step in the list is the most recently completed one.  The
    DAG step *after* it is what's currently running.  If no steps exist
    yet the campaign is in its first step.
    """
    dag_order = [
        "GENERATE_LHS_SAMPLES",
        "PREFLIGHT_RUN_MODEL",
        "APPLY_PARAMETERS",
        "RUN_OPENSTUDIO_SIM",
        "EXTRACT_KPIS",
        "AGGREGATE_RESULTS",
        "GENERATE_BASIC_PLOTS",
    ]
    if not steps:
        return dag_order[0] if dag_order else ""
    last_step = str(steps[-1].get("step", ""))
    for i, name in enumerate(dag_order):
        # Handle algorithm-prefixed step names (e.g. GENERATE_SOBOL_SAMPLES
        # maps to the GENERATE_LHS_SAMPLES DAG slot).  Only steps matching
        # GENERATE_*_SAMPLES are alternative names for sample generation;
        # GENERATE_BASIC_PLOTS is a separate DAG step entirely.
        is_match = last_step == name or (
            name == "GENERATE_LHS_SAMPLES"
            and last_step.startswith("GENERATE_")
            and last_step.endswith("_SAMPLES")
        )
        if is_match:
            if i + 1 < len(dag_order):
                return dag_order[i + 1]
            return "COMPLETE"
    return ""


class RichTUI:
    """Live-updating rich terminal dashboard for a running campaign.

    Usage::

        tui = RichTUI(outdir)
        tui.start()
        try:
            campaign.run()       # blocks
        finally:
            tui.stop()
    """

    def __init__(self, outdir: Path) -> None:
        self._run_json_path = outdir / "run.json"
        self._start_time = time.time()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._live: Any = None  # rich.live.Live instance

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def start(self) -> None:
        """Start the polling/render thread (daemon)."""
        if not _HAS_RICH:
            log.info("rich not installed; TUI disabled")
            return
        self._live = Live(auto_refresh=False, console=None, transient=False)
        self._live.__enter__()
        self._thread = threading.Thread(
            target=self._poll_loop,
            name="osimflow-tui",
            daemon=True,
        )
        self._thread.start()
        log.debug("TUI polling thread started")

    def stop(self) -> None:
        """Stop the polling thread and tear down the Live display."""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        if self._live is not None:
            with contextlib.suppress(Exception):
                self._live.__exit__(None, None, None)
            self._live = None
        log.debug("TUI stopped")

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------
    def _poll_loop(self) -> None:
        """Background loop: read run.json → render → sleep."""
        while not self._stop_event.is_set():
            try:
                self._render_once()
            except Exception:  # pragma: no cover
                log.debug("TUI render error", exc_info=True)
            self._stop_event.wait(_POLL_INTERVAL)

    def _render_once(self) -> None:
        """Read run.json and push a new renderable to Live."""
        if self._live is None:
            return
        data = _read_run_json(self._run_json_path)
        elapsed = time.time() - self._start_time
        if data is not None:
            renderable = _build_display(data, elapsed)
        else:
            assert Text is not None  # for type-checker
            renderable = Text(f"  Waiting for campaign to start… ({elapsed:.0f}s)")
        self._live.update(renderable)
        self._live.refresh()
