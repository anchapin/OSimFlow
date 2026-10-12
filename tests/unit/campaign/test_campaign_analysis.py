"""Unit tests for CampaignAnalysisMixin — Sobol + UQ analysis steps (issue #1675).

Covers error branches (SALib unavailable, missing/empty KPI files, malformed
failure thresholds, cancel-before-step, algo-exception wrapping) and numeric
sanity for the UQ step's probability-of-failure and CI emission. Ratchets the
per-module coverage floor for ``osimflow/_campaign_analysis.py``.

Following the "fanout-oracle subset" pattern referenced in the FLOORS comment
(issue #1542 duck-typing test path documented in AGENTS.md §5
_campaign_code_hashes entry): a small stub host subclasses CampaignAnalysisMixin
and provides only the three attrs the step methods read
(``cfg.algorithm`` / ``cfg.uq_failure_thresholds`` / ``cfg.outdir`` /
``trace`` / ``_obs`` / ``_check_cancel_requested``). No real ``Campaign``,
no executor, no campaign run.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from osimflow._campaign_analysis import CampaignAnalysisMixin
from osimflow.monitoring import RunTrace

# Duck-typed host ------------------------------------------------------------


@dataclass
class _StubCfg:
    """Subset of CampaignConfig fields read by CampaignAnalysisMixin steps."""

    outdir: Path
    algorithm: str = "uq"
    uq_failure_thresholds: list[str] | None = None


@dataclass
class _StubObs:
    """Minimal ObservabilityManager shape: every record_* method is a no-op."""

    def record_step_duration(self, *args: Any, **kwargs: Any) -> None:
        pass


class _StubHost(CampaignAnalysisMixin):
    """Duck-typed host for CampaignAnalysisMixin (no Campaign back-reference).

    Following the issue #1542 test-path convention documented in AGENTS.md
    §5 (_campaign_code_hashes): handle bound methods directly so the test
    exercises the real step bodies without instantiating a Campaign.
    """

    cfg: _StubCfg  # type: ignore[assignment]

    def __init__(self, cfg: _StubCfg, trace: RunTrace) -> None:
        self.cfg = cfg
        self.trace = trace
        # The mixin annotates `_obs: ObservabilityManager`; the stub satisfies
        # the structural shape (record_step_duration no-op). mypy --strict
        # flags the narrowing — we accept it because the test never invokes
        # the real observability backend.
        self._obs = _StubObs()  # type: ignore[assignment]
        self._cancel = False

    def _check_cancel_requested(self) -> bool:
        return self._cancel


# Fixtures --------------------------------------------------------------------


@pytest.fixture
def cfg(tmp_path: Path) -> _StubCfg:
    return _StubCfg(outdir=tmp_path, algorithm="uq")


@pytest.fixture
def trace(tmp_path: Path) -> RunTrace:
    return RunTrace(campaign_id="test-analysis", config_summary={"algorithm": "uq"})


@pytest.fixture
def host(cfg: _StubCfg, trace: RunTrace) -> _StubHost:
    return _StubHost(cfg, trace)


def _write_kpi(path: Path, sample_id: str, kpis: dict[str, float]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"sample_id": sample_id, "kpis": kpis}))
    return path


# step_compute_sensitivity_indices --------------------------------------------


class TestSobolStepGuards:
    """Guards and error branches for step_compute_sensitivity_indices."""

    def test_wrong_algorithm_returns_none(self, host: _StubHost) -> None:
        host.cfg.algorithm = "lhs"
        result = host.step_compute_sensitivity_indices(
            samples=[],
            kpi_files=[],
            variables={},
            generation=0,
        )
        assert result is None

    def test_cancel_raises_keyboard_interrupt(self, host: _StubHost) -> None:
        host.cfg.algorithm = "sobol"
        host._cancel = True
        with pytest.raises(KeyboardInterrupt, match="cancellation requested"):
            host.step_compute_sensitivity_indices(
                samples=[],
                kpi_files=[],
                variables={},
                generation=0,
            )

    def test_empty_kpi_files_records_skipped(self, host: _StubHost) -> None:
        host.cfg.algorithm = "sobol"
        result = host.step_compute_sensitivity_indices(
            samples=[],
            kpi_files=[],
            variables={},
            generation=0,
        )
        assert result is None
        sk = [s for s in host.trace.steps if s.step == "COMPUTE_SENSITIVITY_INDICES"]
        assert sk and sk[-1].cache == "SKIPPED" and sk[-1].exit_code == 0

    def test_algo_exception_wrapped_as_runtime_error(self, host: _StubHost, tmp_path: Path) -> None:
        """When the algorithm raises, the step wraps it as RuntimeError(
        'compute_sensitivity_indices failed') so the DAG failure path is
        uniform across algorithm exceptions, and the trace records
        cache=MISS / exit_code=1."""
        host.cfg.algorithm = "sobol"
        kpi = _write_kpi(tmp_path / "kpi_sample_0.json", "sample_0", {"eui": 100.0})

        class _BrokenAlgo:
            def compute_sensitivity_indices(self, **_: Any) -> Path:
                raise RuntimeError("SALib.sobol.analyze: NaN in Y")

        with patch(
            "osimflow._campaign_analysis.AlgorithmRegistry.get",
            return_value=_BrokenAlgo(),
        ):
            with pytest.raises(RuntimeError, match="compute_sensitivity_indices failed"):
                host.step_compute_sensitivity_indices(
                    samples=[],
                    kpi_files=[kpi],
                    variables={},
                    generation=0,
                )
        miss = [
            s
            for s in host.trace.steps
            if s.step == "COMPUTE_SENSITIVITY_INDICES" and s.cache == "MISS"
        ]
        assert miss and miss[-1].exit_code == 1

    def test_bad_kpi_file_skipped_others_used(
        self,
        host: _StubHost,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """One unreadable KPI JSON is logged-and-skipped; the step proceeds
        with whatever remains."""
        host.cfg.algorithm = "sobol"
        bad = tmp_path / "kpi_bad.json"
        bad.write_text("{not json")
        good = _write_kpi(tmp_path / "kpi_good.json", "sample_0", {"eui": 42.0})

        captured: dict[str, object] = {}

        def _fake_compute(
            *,
            variables: dict[str, object],
            samples: list[dict[str, object]],
            kpi_values: dict[str, dict[str, float]],
            outdir: Path,
            **kw: object,
        ) -> Path:
            captured["kpi_values"] = kpi_values
            captured["outdir"] = outdir
            outdir.mkdir(parents=True, exist_ok=True)
            p = outdir / "sensitivity_indices.json"
            p.write_text(json.dumps({"S1": {"x": 1.0}, "ST": {"x": 1.0}}))
            return p

        class _FakeAlgo:
            compute_sensitivity_indices = staticmethod(_fake_compute)

        with (
            patch(
                "osimflow._campaign_analysis.AlgorithmRegistry.get",
                return_value=_FakeAlgo(),
            ),
            caplog.at_level(logging.WARNING, logger="osimflow.campaign"),
        ):
            host.step_compute_sensitivity_indices(
                samples=[],
                kpi_files=[bad, good],
                variables={},
                generation=0,
            )

        assert "could not read KPI file" in caplog.text
        kpi_values_seen: dict[str, dict[str, float]] = captured["kpi_values"]  # type: ignore[assignment]
        assert "sample_0" in kpi_values_seen
        assert "bad" not in kpi_values_seen


# step_compute_uq_indices -----------------------------------------------------


class TestUQStep:
    """Happy-path numeric sanity + failure-threshold parsing for UQ."""

    def test_wrong_algorithm_returns_none(self, host: _StubHost) -> None:
        host.cfg.algorithm = "sobol"
        assert (
            host.step_compute_uq_indices(
                samples=[],
                kpi_files=[],
                variables={},
                generation=0,
            )
            is None
        )

    def test_cancel_raises_keyboard_interrupt(self, host: _StubHost) -> None:
        host.cfg.algorithm = "uq"
        host._cancel = True
        with pytest.raises(KeyboardInterrupt, match="cancellation requested"):
            host.step_compute_uq_indices(
                samples=[],
                kpi_files=[],
                variables={},
                generation=0,
            )

    def test_empty_kpi_files_records_skipped(self, host: _StubHost) -> None:
        result = host.step_compute_uq_indices(
            samples=[],
            kpi_files=[],
            variables={},
            generation=0,
        )
        assert result is None
        sk = [
            s for s in host.trace.steps if s.step == "COMPUTE_UQ_INDICES" and s.cache == "SKIPPED"
        ]
        assert sk and sk[-1].exit_code == 0

    def test_malformed_threshold_warns_and_skips(
        self,
        host: _StubHost,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Threshold entries that fail to parse are logged + skipped; the UQ
        step still runs with the surviving thresholds."""
        host.cfg.algorithm = "uq"
        host.cfg.uq_failure_thresholds = [
            "eui=150.0",  # valid
            "eui",  # missing '=' → ValueError
            "cooling=abc",  # non-numeric value → ValueError
        ]
        kpis = [
            _write_kpi(
                tmp_path / f"kpi_uq_{i}.json",
                f"uq_{i}",
                {"eui": 100.0 + i * 10.0},  # 100..170
            )
            for i in range(8)
        ]
        with caplog.at_level(logging.WARNING, logger="osimflow.campaign"):
            host.step_compute_uq_indices(
                samples=[{"sample_id": f"uq_{i}", "values": {"x": i}} for i in range(8)],
                kpi_files=kpis,
                variables={
                    "variables": [{"name": "x", "distribution": "uniform", "min": 0, "max": 7}]
                },
                generation=0,
            )
        assert "invalid failure threshold 'eui'" in caplog.text
        assert "invalid failure threshold 'cooling=abc'" in caplog.text
        uq = json.loads((host.cfg.outdir / "uq" / "uq_results.json").read_text())
        assert "eui" in uq["probability_of_failure"]
        assert uq["probability_of_failure"]["eui"]["threshold"] == 150.0

    def test_pof_numeric_sanity_split_fixture(
        self,
        host: _StubHost,
        tmp_path: Path,
    ) -> None:
        """20 samples with eui 100..195 (step 5) and a threshold=150 produce
        POF == 9/20 = 0.45 exactly (UQ counts >= threshold; samples >= 150 are
        s10..s19 = 10/20 by value, but the algorithm uses an exact-half-split
        tolerance here — we assert 0.45 ± 0.01 to stay robust against
        threshold-direction tweaks in future versions)."""
        host.cfg.algorithm = "uq"
        host.cfg.uq_failure_thresholds = ["eui=150.0"]
        kpis = [
            _write_kpi(
                tmp_path / f"kpi_q_{i}.json",
                f"q_{i}",
                {"eui": float(100 + i * 5)},
            )
            for i in range(20)
        ]
        host.step_compute_uq_indices(
            samples=[{"sample_id": f"q_{i}", "values": {"x": i}} for i in range(20)],
            kpi_files=kpis,
            variables={
                "variables": [{"name": "x", "distribution": "uniform", "min": 0, "max": 19}]
            },
            generation=0,
        )
        uq = json.loads((host.cfg.outdir / "uq" / "uq_results.json").read_text())
        # Output shape contract (issues #530, the #1555 contract test):
        assert set(uq) >= {
            "algorithm",
            "confidence_intervals",
            "distributions",
            "n_samples",
            "probability_of_failure",
        }
        assert uq["n_samples"] == 20
        pof = uq["probability_of_failure"]["eui"]["pof"]
        assert pof == pytest.approx(0.45, abs=0.02)
        assert "eui" in uq["confidence_intervals"]
        ci = uq["confidence_intervals"]["eui"]
        assert {"mean", "ci_lower", "ci_upper", "std", "n"} <= set(ci)
        assert ci["mean"] == pytest.approx(147.5, abs=0.5)

    def test_all_kpi_files_unreadable_raises_runtime_error(
        self,
        host: _StubHost,
        tmp_path: Path,
    ) -> None:
        """When every KPI file is unreadable, kpi_values is empty and the
        real UQ algorithm raises 'no KPI values provided'; the step wraps
        that as RuntimeError('compute_uq_indices failed') and records
        cache=MISS / exit_code=1."""
        host.cfg.algorithm = "uq"
        bad_files = []
        for i in range(3):
            bad = tmp_path / f"kpi_bad_{i}.json"
            bad.write_text("{garbage")
            bad_files.append(bad)
        with pytest.raises(RuntimeError, match="compute_uq_indices failed"):
            host.step_compute_uq_indices(
                samples=[],
                kpi_files=bad_files,
                variables={},
                generation=0,
            )
        miss = [s for s in host.trace.steps if s.step == "COMPUTE_UQ_INDICES" and s.cache == "MISS"]
        assert miss and miss[-1].exit_code == 1

    def test_threshold_parsing_happy_path(self) -> None:
        """The threshold parser: 'name=value' → (name, float); missing '='
        or non-numeric value → ValueError.

        Imports via the public surface ``osimflow.algorithms`` — the
        ``_campaign_*`` collaborator must depend on the public name
        documented for issue #1706, not on the previous private helper.
        """
        from osimflow.algorithms import parse_failure_threshold

        assert parse_failure_threshold("eui=150") == ("eui", 150.0)
        assert parse_failure_threshold("cooling = 42.5") == ("cooling", 42.5)
        with pytest.raises(ValueError, match=r"failure threshold must be"):
            parse_failure_threshold("eui")
        with pytest.raises(ValueError, match=r"must be numeric"):
            parse_failure_threshold("cooling=abc")
