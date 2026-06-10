"""Unit tests for --init-script / --finalize-script hooks (issue #108).

Verifies:
- Init script runs before campaign steps
- Finalize script runs after campaign steps
- Init script failure aborts campaign
- Finalize script failure does NOT abort campaign (best-effort)
- Environment variables are set correctly
- No hooks when flags not provided (default behavior unchanged)
- Hook timing is recorded in RunTrace

NOTE: Tests use dry_run=True to avoid needing the full bin/*.py stack
installed in the subprocess environment. The hook logic is tested
identically in dry-run and full-campaign modes since hooks run before
the first step and after the last step in Campaign.run().
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from osimflow import Campaign, CampaignConfig
from osimflow.executors import LocalExecutor

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_PKG = REPO_ROOT / "example_package"
EXAMPLE_VARS_YML = REPO_ROOT / "variables.yml"


@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    wd = tmp_path / "work"
    wd.mkdir()
    (wd / "variables.yml").write_text(EXAMPLE_VARS_YML.read_text())
    return wd


@pytest.fixture
def template_pkg(workdir: Path) -> Path:
    pkg = workdir / "template"
    shutil.copytree(EXAMPLE_PKG, pkg)
    return pkg


@pytest.fixture
def outdir(workdir: Path) -> Path:
    od = workdir / "out"
    od.mkdir()
    return od


def _make_cfg(
    workdir: Path,
    template_pkg: Path,
    outdir: Path,
    *,
    init_script: Path | None = None,
    finalize_script: Path | None = None,
) -> CampaignConfig:
    return CampaignConfig(
        input_variables=workdir / "variables.yml",
        template_sim_package=template_pkg,
        n_samples=2,
        outdir=outdir,
        openstudio_version="3.4.0",
        dry_run=True,
        init_script=init_script,
        finalize_script=finalize_script,
    )


def _write_hook(path: Path, body: str) -> Path:
    """Write an executable hook script and return its path."""
    path.write_text(body)
    path.chmod(0o755)
    return path


# ------------------------------------------------------------------
# Tests
# ------------------------------------------------------------------


def test_init_script_runs_before_campaign_steps(
    workdir: Path, template_pkg: Path, outdir: Path
) -> None:
    """Init script runs before GENERATE_LHS_SAMPLES."""
    marker = outdir / "init_ran"
    script = _write_hook(
        workdir / "init.sh",
        f"#!/bin/bash\ntouch {marker}\n",
    )
    cfg = _make_cfg(workdir, template_pkg, outdir, init_script=script)
    campaign = Campaign(cfg=cfg, executor=LocalExecutor(max_workers=1))
    campaign.run()

    assert marker.exists(), "init script should have created the marker file"
    # Campaign also completed successfully
    assert (outdir / "run.json").exists()


def test_finalize_script_runs_after_campaign_steps(
    workdir: Path, template_pkg: Path, outdir: Path
) -> None:
    """Finalize script runs after all steps complete."""
    marker = outdir / "finalize_ran"
    script = _write_hook(
        workdir / "finalize.sh",
        f"#!/bin/bash\ntouch {marker}\n",
    )
    cfg = _make_cfg(workdir, template_pkg, outdir, finalize_script=script)
    campaign = Campaign(cfg=cfg, executor=LocalExecutor(max_workers=1))
    campaign.run()

    assert marker.exists(), "finalize script should have created the marker file"


def test_init_script_failure_aborts_campaign(
    workdir: Path, template_pkg: Path, outdir: Path
) -> None:
    """If init script exits non-zero, campaign does NOT run steps."""
    script = _write_hook(
        workdir / "init_fail.sh",
        "#!/bin/bash\nexit 1\n",
    )
    cfg = _make_cfg(workdir, template_pkg, outdir, init_script=script)
    campaign = Campaign(cfg=cfg, executor=LocalExecutor(max_workers=1))
    with pytest.raises(subprocess.CalledProcessError):
        campaign.run()
    # No run.json written because campaign aborted before steps
    assert not (outdir / "run.json").exists()


def test_finalize_script_failure_does_not_abort_campaign(
    workdir: Path, template_pkg: Path, outdir: Path
) -> None:
    """If finalize script exits non-zero, campaign still succeeds."""
    script = _write_hook(
        workdir / "finalize_fail.sh",
        "#!/bin/bash\nexit 42\n",
    )
    cfg = _make_cfg(workdir, template_pkg, outdir, finalize_script=script)
    campaign = Campaign(cfg=cfg, executor=LocalExecutor(max_workers=1))
    result = campaign.run()

    # Campaign completed despite finalize failure
    assert result["elapsed_s"] > 0
    assert (outdir / "run.json").exists()


def test_hook_env_vars_are_set(
    workdir: Path, template_pkg: Path, outdir: Path
) -> None:
    """Hook scripts receive the correct environment variables."""
    env_log = outdir / "env.log"
    init_script = _write_hook(
        workdir / "init_env.sh",
        f"#!/bin/bash\nenv > {env_log}\n",
    )
    finalize_log = outdir / "finalize_env.log"
    finalize_script = _write_hook(
        workdir / "finalize_env.sh",
        f"#!/bin/bash\nenv > {finalize_log}\n",
    )
    cfg = _make_cfg(
        workdir,
        template_pkg,
        outdir,
        init_script=init_script,
        finalize_script=finalize_script,
    )
    campaign = Campaign(cfg=cfg, executor=LocalExecutor(max_workers=1))
    campaign.run()

    # Check init script env
    env_text = env_log.read_text()
    assert f"OSIMFLOW_OUTDIR={outdir}" in env_text
    assert "OSIMFLOW_N_SAMPLES=2" in env_text
    assert "OSIMFLOW_EXECUTOR=local" in env_text
    assert "OSIMFLOW_ALGORITHM=lhs" in env_text

    # Check finalize script env (has OSIMFLOW_STATUS + OSIMFLOW_DURATION_S)
    fin_text = finalize_log.read_text()
    assert "OSIMFLOW_STATUS=success" in fin_text
    assert "OSIMFLOW_DURATION_S=" in fin_text


def test_no_hooks_by_default(
    workdir: Path, template_pkg: Path, outdir: Path
) -> None:
    """Campaign runs normally when no hooks are configured."""
    cfg = _make_cfg(workdir, template_pkg, outdir)
    campaign = Campaign(cfg=cfg, executor=LocalExecutor(max_workers=1))
    result = campaign.run()

    assert result["elapsed_s"] > 0
    assert (outdir / "run.json").exists()
    # Trace should have no hook timing
    trace_data = json.loads((outdir / "run.json").read_text())
    assert "init_script_duration_s" not in trace_data
    assert "finalize_script_duration_s" not in trace_data


def test_hook_timing_recorded_in_trace(
    workdir: Path, template_pkg: Path, outdir: Path
) -> None:
    """Init/finalize durations are recorded in RunTrace."""
    init_script = _write_hook(
        workdir / "init_timing.sh",
        "#!/bin/bash\ntrue\n",
    )
    finalize_script = _write_hook(
        workdir / "finalize_timing.sh",
        "#!/bin/bash\ntrue\n",
    )
    cfg = _make_cfg(
        workdir,
        template_pkg,
        outdir,
        init_script=init_script,
        finalize_script=finalize_script,
    )
    campaign = Campaign(cfg=cfg, executor=LocalExecutor(max_workers=1))
    campaign.run()

    trace_data = json.loads((outdir / "run.json").read_text())
    assert "init_script_duration_s" in trace_data
    assert trace_data["init_script_duration_s"] >= 0
    assert "finalize_script_duration_s" in trace_data
    assert trace_data["finalize_script_duration_s"] >= 0


def test_init_script_missing_raises(
    workdir: Path, template_pkg: Path, outdir: Path
) -> None:
    """Init script path that does not exist raises FileNotFoundError."""
    missing = workdir / "nonexistent.sh"
    cfg = _make_cfg(workdir, template_pkg, outdir, init_script=missing)
    campaign = Campaign(cfg=cfg, executor=LocalExecutor(max_workers=1))
    with pytest.raises(FileNotFoundError, match="init script not found"):
        campaign.run()


def test_finalize_script_missing_does_not_abort(
    workdir: Path, template_pkg: Path, outdir: Path
) -> None:
    """Finalize script path that does not exist is skipped gracefully."""
    missing = workdir / "nonexistent_finalize.sh"
    cfg = _make_cfg(workdir, template_pkg, outdir, finalize_script=missing)
    campaign = Campaign(cfg=cfg, executor=LocalExecutor(max_workers=1))
    result = campaign.run()

    # Campaign still completes
    assert result["elapsed_s"] > 0


def test_init_script_sees_outdir_before_steps(
    workdir: Path, template_pkg: Path, outdir: Path
) -> None:
    """Init script runs BEFORE any campaign output is written."""
    check_log = outdir / "init_check.log"
    script = _write_hook(
        workdir / "init_check.sh",
        f"#!/bin/bash\n"
        # run.json should NOT exist yet when init runs
        f'if [ -f "{outdir}/run.json" ]; then echo "run.json exists" > {check_log}; '
        f'else echo "run.json absent" > {check_log}; fi\n',
    )
    cfg = _make_cfg(workdir, template_pkg, outdir, init_script=script)
    campaign = Campaign(cfg=cfg, executor=LocalExecutor(max_workers=1))
    campaign.run()

    assert check_log.exists()
    assert "run.json absent" in check_log.read_text()


def test_finalize_receives_failure_status_on_exception(
    workdir: Path, template_pkg: Path, outdir: Path
) -> None:
    """Finalize script sees OSIMFLOW_STATUS=failure when campaign raises."""
    status_log = outdir / "finalize_status.log"
    init_script = _write_hook(
        workdir / "init_fail.sh",
        "#!/bin/bash\nexit 1\n",
    )
    finalize_script = _write_hook(
        workdir / "finalize_status.sh",
        f"#!/bin/bash\necho $OSIMFLOW_STATUS > {status_log}\n",
    )
    cfg = _make_cfg(
        workdir,
        template_pkg,
        outdir,
        init_script=init_script,
        finalize_script=finalize_script,
    )
    campaign = Campaign(cfg=cfg, executor=LocalExecutor(max_workers=1))
    with pytest.raises(subprocess.CalledProcessError):
        campaign.run()

    # Even though init failed, finalize should have run with failure status
    assert status_log.exists()
    assert "failure" in status_log.read_text().strip()
