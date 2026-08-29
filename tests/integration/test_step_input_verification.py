"""Regression tests for ``Campaign._verify_step_inputs`` (issue #1391).

These tests pin down the cross-step data dependency check that
``_verify_step_inputs`` performs against ``_STEP_DEPENDENCIES``.  They
exercise the previously-dormant ``StepInputs.count`` and
``StepOutputs.kpi_pattern`` fields:

  * ``inputs.count`` is the declared expected file count for a fan-out
    step's required patterns; the verifier raises ``FileNotFoundError``
    when the on-disk match count disagrees.
  * ``outputs.kpi_pattern`` on an upstream fan-out step drives the
    canonical sample-count check: an explicit ``inputs.count`` that
    disagrees with the upstream-derived count is rejected (or ``count``
    may be left explicitly ``None`` to skip the consistency check).

Each test stubs out a per-test ``_STEP_DEPENDENCIES`` table via
``monkeypatch`` so the production DAG is untouched.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from osimflow import Campaign, CampaignConfig
from osimflow.campaign import DAGStep, StepInputs, StepOutputs
from osimflow.executors import LocalExecutor

# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    """A clean per-test work directory."""
    wd = tmp_path / "work"
    wd.mkdir()
    return wd


@pytest.fixture
def variables_yml(workdir: Path) -> Path:
    """Stub variables.yml — Campaign construction requires it to exist."""
    p = workdir / "variables.yml"
    p.write_text(
        "variables:\n  - name: u1\n    distribution: uniform\n    min: 0.0\n    max: 1.0\n"
    )
    return p


@pytest.fixture
def template_pkg(workdir: Path) -> Path:
    """Stub template_sim_package."""
    pkg = workdir / "template"
    pkg.mkdir()
    (pkg / "model.osm").write_text('{"attributes": {"u1": 0.0}}')
    (pkg / "workflow.osw").write_text('{"name": "stub"}')
    return pkg


@pytest.fixture
def outdir(workdir: Path) -> Path:
    od = workdir / "out"
    od.mkdir()
    return od


@pytest.fixture
def cfg(
    variables_yml: Path,
    template_pkg: Path,
    outdir: Path,
) -> CampaignConfig:
    return CampaignConfig(
        input_variables=variables_yml,
        template_sim_package=template_pkg,
        n_samples=3,
        outdir=outdir,
        openstudio_version="3.11.0",
        archive_intermediates=False,
    )


@pytest.fixture
def campaign(cfg: CampaignConfig) -> Campaign:
    return Campaign(cfg=cfg, executor=LocalExecutor(max_workers=1))


def _install_dag_steps(
    monkeypatch: pytest.MonkeyPatch,
    table: dict[str, DAGStep],
) -> None:
    """Replace ``_STEP_DEPENDENCIES`` with the supplied test-only table.

    The verifier reads from the module-level ``_STEP_DEPENDENCIES`` dict
    directly, so monkey-patching the module binding is enough for the
    duration of a single test.
    """
    monkeypatch.setattr(
        "osimflow.campaign._STEP_DEPENDENCIES",
        table,
        raising=False,
    )


def _single_step_table(
    *,
    step_name: str,
    inputs: StepInputs,
    outputs: StepOutputs,
) -> dict[str, DAGStep]:
    """Build a single-step DAG table for tests that don't need an upstream."""
    return {
        step_name: DAGStep(
            inputs=inputs,
            outputs=outputs,
            method="step_generate_samples",
        ),
    }


def _write_kpi_files(work_dir: Path, n: int) -> list[Path]:
    """Create ``n`` stub KPI files under ``sim/*/kpi_*.json``."""
    paths: list[Path] = []
    for i in range(n):
        sim_dir = work_dir / "sim" / f"sample_{i}"
        sim_dir.mkdir(parents=True, exist_ok=True)
        p = sim_dir / f"kpi_{i}.json"
        p.write_text("{}")
        paths.append(p)
    return paths


def _write_apply_files(work_dir: Path, n: int) -> list[Path]:
    """Create ``n`` stub apply dirs under ``apply/*/``."""
    paths: list[Path] = []
    for i in range(n):
        d = work_dir / "apply" / f"sample_{i}"
        d.mkdir(parents=True, exist_ok=True)
        paths.append(d)
    return paths


# ---------------------------------------------------------------------------
# Tests for ``StepInputs.count`` enforcement
# ---------------------------------------------------------------------------


class TestStepInputsCount:
    """``_verify_step_inputs`` must honor ``StepInputs.count`` (issue #1391)."""

    def test_count_matches_passes(
        self,
        campaign: Campaign,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """No exception when on-disk match count equals ``inputs.count``."""
        _write_apply_files(campaign.cfg.work_dir, n=3)
        _install_dag_steps(
            monkeypatch,
            _single_step_table(
                step_name="TEST_FANOUT_STEP",
                inputs=StepInputs(required_patterns=("apply/*/",), count=3),
                outputs=StepOutputs(),
            ),
        )
        campaign._verify_step_inputs("TEST_FANOUT_STEP")

    def test_count_mismatch_raises(
        self,
        campaign: Campaign,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``FileNotFoundError`` when only N-1 of N expected files exist."""
        _write_apply_files(campaign.cfg.work_dir, n=3)
        _install_dag_steps(
            monkeypatch,
            _single_step_table(
                step_name="TEST_FANOUT_STEP",
                inputs=StepInputs(required_patterns=("apply/*/",), count=5),
                outputs=StepOutputs(),
            ),
        )
        with pytest.raises(FileNotFoundError, match=r"declared count=5 but found 3"):
            campaign._verify_step_inputs("TEST_FANOUT_STEP")

    def test_count_none_skips_check(
        self,
        campaign: Campaign,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``count=None`` opts out of the count check entirely."""
        _write_apply_files(campaign.cfg.work_dir, n=3)
        _install_dag_steps(
            monkeypatch,
            _single_step_table(
                step_name="TEST_FANOUT_STEP",
                inputs=StepInputs(required_patterns=("apply/*/",), count=None),
                outputs=StepOutputs(),
            ),
        )
        campaign._verify_step_inputs("TEST_FANOUT_STEP")

    def test_kpi_pattern_drives_count(
        self,
        campaign: Campaign,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An upstream ``kpi_pattern`` drives the canonical count check.

        Acceptance criterion (issue #1391): ``StepOutputs.kpi_pattern``
        must drive the count check for fan-out steps.  When the
        consuming step's ``required_patterns`` matches the upstream
        step's ``kpi_pattern``, an explicit ``inputs.count`` that
        agrees with the upstream-derived count passes, and ``count``
        being left explicitly ``None`` opts out of the upstream
        consistency check.

        To exercise the upstream-derived check distinctly from the
        on-disk match-count check we install a downstream step whose
        ``required_patterns`` is a *superset* of the upstream
        ``kpi_pattern`` (so the two globs may legitimately yield
        different counts): when count matches the downstream glob but
        the upstream glob disagrees, the upstream check fires.
        """
        # 4 KPI files under 4 sim dirs (matches upstream kpi_pattern).
        _write_kpi_files(campaign.cfg.work_dir, n=4)
        upstream_outputs = StepOutputs(kpi_pattern="sim/*/kpi_*.json")
        _install_dag_steps(
            monkeypatch,
            {
                "UPSTREAM_EXTRACT_KPIS": DAGStep(
                    inputs=StepInputs(),
                    outputs=upstream_outputs,
                    method="step_extract_kpis",
                ),
                "TEST_AGGREGATE_RESULTS": DAGStep(
                    inputs=StepInputs(
                        required_patterns=("sim/*/kpi_*.json",),
                        count=4,
                    ),
                    outputs=StepOutputs(),
                    method="step_generate_samples",
                ),
            },
        )
        # Matching count → no exception (both checks satisfied).
        campaign._verify_step_inputs("TEST_AGGREGATE_RESULTS")

        # ``count=None`` opts out of the upstream consistency check
        # even when the upstream-derived count is known.
        _install_dag_steps(
            monkeypatch,
            {
                "UPSTREAM_EXTRACT_KPIS": DAGStep(
                    inputs=StepInputs(),
                    outputs=upstream_outputs,
                    method="step_extract_kpis",
                ),
                "TEST_AGGREGATE_RESULTS": DAGStep(
                    inputs=StepInputs(
                        required_patterns=("sim/*/kpi_*.json",),
                        count=None,
                    ),
                    outputs=StepOutputs(),
                    method="step_generate_samples",
                ),
            },
        )
        campaign._verify_step_inputs("TEST_AGGREGATE_RESULTS")


def test_n_minus_one_kpi_files_raises(
    campaign: Campaign,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression test for issue #1391 — N-1 of N KPI files raises.

    Acceptance criterion: ``_verify_step_inputs`` against a stub
    ``work_dir`` with N-1 of N KPI files must raise ``FileNotFoundError``,
    preventing downstream steps (e.g. ``AGGREGATE_RESULTS``) from
    silently proceeding against a partially-failed upstream fan-out.
    """
    n_total = 5
    n_present = n_total - 1
    _write_kpi_files(campaign.cfg.work_dir, n=n_present)
    upstream_outputs = StepOutputs(kpi_pattern="sim/*/kpi_*.json")
    _install_dag_steps(
        monkeypatch,
        {
            "UPSTREAM_EXTRACT_KPIS": DAGStep(
                inputs=StepInputs(),
                outputs=upstream_outputs,
                method="step_extract_kpis",
            ),
            "TEST_AGGREGATE_RESULTS": DAGStep(
                inputs=StepInputs(
                    required_patterns=("sim/*/kpi_*.json",),
                    count=n_total,
                ),
                outputs=StepOutputs(),
                method="step_generate_samples",
            ),
        },
    )
    with pytest.raises(
        FileNotFoundError,
        match=r"declared count=5 but found 4",
    ):
        campaign._verify_step_inputs("TEST_AGGREGATE_RESULTS")
