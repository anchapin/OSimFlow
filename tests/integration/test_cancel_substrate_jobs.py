"""Integration test: cancelling a campaign stops in-flight substrate jobs (issue #1538).

Acceptance criterion (verbatim from the issue):

    an integration test asserts that cancelling a campaign with
    in-flight substrate jobs both stops those jobs and lets ``run()``
    return with ``status="cancelled"`` written to ``run.json``.

Strategy (per the issue's guidance: stub-mode, no real substrate, with
mocked cancel calls): the campaign runs against a controllable executor
whose handles park forever — the stand-ins for in-flight cloud/HPC jobs.
Cancellation is requested from a background thread shortly after the
first fan-out step submits its work. The test then asserts:

1. every in-flight handle received a substrate kill (its ``cancel()``
   was invoked — the local stand-in for TerminateJob / scancel /
   batch delete / allocation stop);
2. ``run()`` returned (bounded fan-out pool drain — no SIGKILL needed);
3. ``run.json`` carries ``status="cancelled"``.
"""

from __future__ import annotations

import concurrent.futures
import json
import shutil
import threading
import time
from concurrent.futures import Future
from pathlib import Path
from typing import Any

from osimflow import Campaign, CampaignConfig
from osimflow.executors import BaseExecutor, Handle

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_PKG = REPO_ROOT / "example_package"


class _ControllableHandle(Handle):
    """Handle parked forever until its substrate kill lands."""

    def __init__(self, job_id: str, killed: list[str]) -> None:
        self.job_id = job_id
        self._future: Future[Any] = Future()
        self._killed = killed

    def result(self, timeout: float | None = None) -> Any:
        # Blocks until cancel() sets the exception — exactly how a
        # fan-out await thread parks on a running substrate job.
        return self._future.result(timeout=timeout)

    def done(self) -> bool:
        return self._future.done()

    def cancel(self) -> bool:
        """Substrate kill: record it and fail the job (issue #1538)."""
        if self._future.done():
            return False
        self._killed.append(self.job_id)
        # The kill unblocks the parked await thread the way TerminateJob
        # / scancel / alloc-stop moves the real job to a failed
        # terminal state.
        self._future.set_exception(
            concurrent.futures.CancelledError(f"substrate job {self.job_id} killed")
        )
        return True


class _ControllableExecutor(BaseExecutor):
    """Executor whose jobs are controllable stand-ins for substrate jobs."""

    name = "controllable"

    def __init__(self) -> None:
        self._init_rate_limiter(None)
        self.killed: list[str] = []
        self.handles: list[_ControllableHandle] = []
        self._cancel_requested_cb: list[threading.Event] = []

    def request_cancel_soon(self, campaign: Campaign, delay_s: float) -> None:
        """Ask the campaign to cancel from a background thread after delay."""

        def _fire() -> None:
            time.sleep(delay_s)
            campaign.request_cancel()

        t = threading.Thread(target=_fire, daemon=True)
        t.start()

    def _do_submit(
        self,
        fn: Any,
        *args: Any,
        name: str = "task",
        **kwargs: Any,
    ) -> Handle:
        handle = _ControllableHandle(f"job-{name}", self.killed)
        self.handles.append(handle)
        return handle

    def shutdown(self) -> None:
        pass


# ---------------------------------------------------------------------------
# Fixtures (same shape as the other executor integration tests)
# ---------------------------------------------------------------------------
def _variables_yml(tmp_path: Path) -> Path:
    vyml = tmp_path / "variables.yml"
    source = EXAMPLE_PKG / "variables.yml"
    if source.is_file():
        vyml.write_text(source.read_text())
    else:  # pragma: no cover — example_package always ships one
        vyml.write_text(
            "algorithm: lhs\n"
            "variables:\n"
            "  - name: wwr\n"
            "    distribution: uniform\n"
            "    min: 0.2\n"
            "    max: 0.6\n"
        )
    return vyml


def _template_pkg(tmp_path: Path) -> Path:
    pkg = tmp_path / "template"
    shutil.copytree(EXAMPLE_PKG, pkg)
    return pkg


def test_cancel_stops_substrate_jobs_and_returns_cancelled(tmp_path: Path) -> None:
    vyml = _variables_yml(tmp_path)
    pkg = _template_pkg(tmp_path)
    outdir = tmp_path / "out"
    outdir.mkdir()

    cfg = CampaignConfig(
        input_variables=vyml,
        template_sim_package=pkg,
        n_samples=3,
        outdir=outdir,
        openstudio_version="3.11.0",
        skip_preflight=True,
    )
    executor = _ControllableExecutor()
    campaign = Campaign(cfg=cfg, executor=executor, max_workers=2)
    executor.request_cancel_soon(campaign, delay_s=0.5)

    start = time.monotonic()
    result = campaign.run()  # must return — not hang, not raise
    elapsed = time.monotonic() - start

    # (2) run() returned promptly (bounded fan-out drain; the parked
    # handles would otherwise block until their await deadline).
    assert elapsed < 60.0, f"run() took {elapsed:.1f}s to return after cancel"
    assert result["status"] == "cancelled"

    # (1) the in-flight substrate jobs were stopped: every handle
    # submitted by the first fan-out step received its kill.
    assert executor.handles, "no substrate jobs were ever submitted"
    assert set(executor.killed) == {h.job_id for h in executor.handles}

    # (3) run.json carries status="cancelled".
    run_json = outdir / "run.json"
    assert run_json.exists()
    data = json.loads(run_json.read_text())
    assert data["status"] == "cancelled"


def test_cancelled_campaign_cleans_up_between_fanout_steps(tmp_path: Path) -> None:
    """A second cancel sweep (run()'s handler) is a cheap no-op.

    The wait loop issues the substrate kill early (via
    ``FanoutDeps.cancel_active_jobs``); run()'s KeyboardInterrupt
    handler then calls ``_cancel_active_jobs`` again. The registry was
    cleared by the first sweep, so no handle is killed twice and no
    exception escapes the shutdown path.
    """
    vyml = _variables_yml(tmp_path)
    pkg = _template_pkg(tmp_path)
    outdir = tmp_path / "out"
    outdir.mkdir()

    cfg = CampaignConfig(
        input_variables=vyml,
        template_sim_package=pkg,
        n_samples=2,
        outdir=outdir,
        openstudio_version="3.11.0",
        skip_preflight=True,
    )
    executor = _ControllableExecutor()
    campaign = Campaign(cfg=cfg, executor=executor, max_workers=2)
    executor.request_cancel_soon(campaign, delay_s=0.5)

    campaign.run()

    # Each in-flight job was killed exactly once despite the two
    # cancellation sweeps (wait-loop + run()'s KeyboardInterrupt path).
    killed = executor.killed
    assert len(killed) == len(set(killed)), f"duplicate kills: {killed}"
    data = json.loads((outdir / "run.json").read_text())
    assert data["status"] == "cancelled"
