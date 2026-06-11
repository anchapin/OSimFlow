"""Unit tests for OSA algorithm resolution (issue #104).

Covers:
- LHS algorithm resolves correctly
- ``nsga_nrel`` maps to ``nsga2`` (or raises if pymoo not installed)
- Unknown algorithm raises :class:`OSAImportError` with supported list
- Name translation table covers all expected OSA types
- Backward compatibility: no algorithm defaults to LHS
- Backward compatibility: ``{"type": "lhs"}`` works as before
"""

import json
from pathlib import Path

import pytest
import yaml

from osimflow.algorithms import AlgorithmRegistry, LHSAlgorithm
from osimflow.importers.osa import (
    _OSA_ALGORITHM_MAP,
    OSAImportError,
    _resolve_algorithm,
    osa_to_variables_yml,
    parse_osa,
)

# ---------------------------------------------------------------------------
# Minimal OSA fixtures
# ---------------------------------------------------------------------------

_MINIMAL_OSA_LHS: dict = {
    "analysis": {
        "problem": {
            "algorithm": {"type": "lhs", "number_of_samples": 10, "seed": 42},
            "variables": [
                {
                    "name": "x",
                    "distribution": {"type": "uniform", "minimum": 0, "maximum": 1},
                },
            ],
        },
    }
}

_MINIMAL_OSA_NO_ALGO: dict = {
    "analysis": {
        "problem": {
            "variables": [
                {
                    "name": "x",
                    "distribution": {"type": "uniform", "minimum": 0, "maximum": 1},
                },
            ],
        },
    }
}


def _write_json(path: Path, data: dict) -> Path:
    path.write_text(json.dumps(data))
    return path


# ---------------------------------------------------------------------------
# _resolve_algorithm unit tests
# ---------------------------------------------------------------------------


class TestResolveAlgorithm:
    """Direct tests of the ``_resolve_algorithm`` helper."""

    def test_lhs_resolves(self) -> None:
        algo = _resolve_algorithm({"type": "lhs"})
        assert isinstance(algo, LHSAlgorithm)
        assert algo.name() == "lhs"

    def test_latin_hypercube_aliases_to_lhs(self) -> None:
        algo = _resolve_algorithm({"type": "latin_hypercube"})
        assert isinstance(algo, LHSAlgorithm)

    def test_doe_maps_to_lhs(self) -> None:
        algo = _resolve_algorithm({"type": "doe"})
        assert isinstance(algo, LHSAlgorithm)

    def test_case_insensitive(self) -> None:
        algo = _resolve_algorithm({"type": "LHS"})
        assert isinstance(algo, LHSAlgorithm)

    def test_empty_type_raises(self) -> None:
        with pytest.raises(OSAImportError, match="Unknown algorithm"):
            _resolve_algorithm({"type": ""})

    def test_unknown_raises_with_supported_list(self) -> None:
        with pytest.raises(OSAImportError) as exc_info:
            _resolve_algorithm({"type": "raptor"})
        msg = str(exc_info.value)
        assert "raptor" in msg
        assert "lhs" in msg
        assert "Supported OSA types" in msg

    def test_unknown_includes_osimflow_algorithms(self) -> None:
        with pytest.raises(OSAImportError) as exc_info:
            _resolve_algorithm({"type": "imaginary"})
        msg = str(exc_info.value)
        assert "Registered OSimFlow algorithms" in msg

    def test_unregistered_mapping_raises_osa_import_error(self) -> None:
        """If an OSA type maps to an algorithm not in the registry, raise OSAImportError."""
        # "nsga_nrel" maps to "nsga2" which is not registered by default
        # (only "lhs" is built-in). This should raise OSAImportError with
        # install instructions.
        with pytest.raises(OSAImportError, match="not available"):
            _resolve_algorithm({"type": "nsga_nrel"})

    def test_unregistered_mapping_includes_install_hint(self) -> None:
        """The error for a missing registry algorithm should suggest installing deps."""
        with pytest.raises(OSAImportError, match="pip install"):
            _resolve_algorithm({"type": "nsga_nrel"})

    def test_sobol_raises_when_not_registered(self) -> None:
        """sobol maps to 'sobol' in the registry, but is not registered by default."""
        with pytest.raises(OSAImportError, match="not available"):
            _resolve_algorithm({"type": "sobol"})

    def test_morris_raises_when_not_registered(self) -> None:
        with pytest.raises(OSAImportError, match="not available"):
            _resolve_algorithm({"type": "morris"})

    def test_pso_raises_when_not_registered(self) -> None:
        with pytest.raises(OSAImportError, match="not available"):
            _resolve_algorithm({"type": "pso"})

    def test_ga_raises_when_not_registered(self) -> None:
        with pytest.raises(OSAImportError, match="not available"):
            _resolve_algorithm({"type": "ga"})


# ---------------------------------------------------------------------------
# Translation table coverage
# ---------------------------------------------------------------------------


class TestAlgorithmTranslationTable:
    """Ensure the translation table covers the expected OSA algorithm types."""

    @pytest.mark.parametrize(
        "osa_type",
        [
            "lhs",
            "latin_hypercube",
            "nsga_nrel",
            "pso",
            "ga",
            "optim",
            "sobol",
            "morris",
            "fast99",
            "doe",
        ],
    )
    def test_expected_osa_types_in_map(self, osa_type: str) -> None:
        assert osa_type in _OSA_ALGORITHM_MAP

    def test_map_has_10_entries(self) -> None:
        assert len(_OSA_ALGORITHM_MAP) == 10

    def test_all_mapped_values_are_strings(self) -> None:
        for osimflow_name in _OSA_ALGORITHM_MAP.values():
            assert isinstance(osimflow_name, str)
            assert osimflow_name  # non-empty


# ---------------------------------------------------------------------------
# Integration: osa_to_variables_yml with algorithm
# ---------------------------------------------------------------------------


class TestOsaToVariablesYmlAlgorithm:
    """End-to-end tests for algorithm resolution within the import flow."""

    def test_lhs_algorithm_in_output(self, tmp_path: Path) -> None:
        osa_data = parse_osa(_write_json(tmp_path / "input.json", _MINIMAL_OSA_LHS))
        out = tmp_path / "variables.yml"
        osa_to_variables_yml(osa_data, out)

        with out.open() as f:
            yml = yaml.safe_load(f)

        assert yml["algorithm"] == "lhs"
        assert len(yml["variables"]) == 1

    def test_no_algorithm_defaults_to_lhs(self, tmp_path: Path) -> None:
        osa_data = parse_osa(_write_json(tmp_path / "input.json", _MINIMAL_OSA_NO_ALGO))
        out = tmp_path / "variables.yml"
        osa_to_variables_yml(osa_data, out)

        with out.open() as f:
            yml = yaml.safe_load(f)

        assert yml["algorithm"] == "lhs"

    def test_backward_compat_lhs_type(self, tmp_path: Path) -> None:
        """An OSA with ``{"type": "lhs"}`` works exactly as before."""
        osa_data = parse_osa(_write_json(tmp_path / "input.json", _MINIMAL_OSA_LHS))
        out = tmp_path / "variables.yml"
        osa_to_variables_yml(osa_data, out)

        with out.open() as f:
            yml = yaml.safe_load(f)

        # Same as the old behavior — LHS is the algorithm
        assert yml["algorithm"] == "lhs"
        # Variables are still converted correctly
        assert yml["variables"][0]["name"] == "x"

    def test_unknown_algorithm_raises_during_conversion(self, tmp_path: Path) -> None:
        data = {
            "analysis": {
                "problem": {
                    "algorithm": {"type": "unknown_algo"},
                    "variables": [
                        {
                            "name": "x",
                            "distribution": {"type": "uniform", "minimum": 0, "maximum": 1},
                        },
                    ],
                },
            }
        }
        osa_data = parse_osa(_write_json(tmp_path / "input.json", data))
        out = tmp_path / "variables.yml"
        with pytest.raises(OSAImportError, match="Unknown algorithm 'unknown_algo'"):
            osa_to_variables_yml(osa_data, out)

    def test_unregistered_algorithm_raises_during_conversion(self, tmp_path: Path) -> None:
        """nsga_nrel maps to nsga2 which is not in the default registry."""
        data = {
            "analysis": {
                "problem": {
                    "algorithm": {"type": "nsga_nrel"},
                    "variables": [
                        {
                            "name": "x",
                            "distribution": {"type": "uniform", "minimum": 0, "maximum": 1},
                        },
                    ],
                },
            }
        }
        osa_data = parse_osa(_write_json(tmp_path / "input.json", data))
        out = tmp_path / "variables.yml"
        with pytest.raises(OSAImportError, match="not available"):
            osa_to_variables_yml(osa_data, out)

    def test_resolved_algorithm_logged(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        osa_data = parse_osa(_write_json(tmp_path / "input.json", _MINIMAL_OSA_LHS))
        out = tmp_path / "variables.yml"
        with caplog.at_level("INFO", logger="osimflow.importers.osa"):
            osa_to_variables_yml(osa_data, out)

        assert any("resolved to OSimFlow algorithm" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# Hypothetical: if nsga2 were registered, nsga_nrel should resolve
# ---------------------------------------------------------------------------


class TestHypotheticalRegisteredAlgorithm:
    """Verify that OSA→OSimFlow mapping works when the algorithm IS registered."""

    def test_nsga_nrel_resolves_when_registered(self) -> None:
        """Register a stub nsga2, verify nsga_nrel resolves, then clean up."""
        from osimflow.algorithms import BaseAlgorithm

        class StubNSGA2(BaseAlgorithm):
            def generate_samples(
                self, variables: dict, n_samples: int, seed: int | None, outdir: Path
            ) -> Path:
                return Path("stub")

            def observe(self, history: list[dict]) -> list[dict]:
                return []

            def is_converged(self, history: list[dict]) -> bool:
                return True

            def name(self) -> str:
                return "nsga2"

            def is_iterative(self) -> bool:
                return True

        AlgorithmRegistry.register("nsga2", StubNSGA2)
        try:
            algo = _resolve_algorithm({"type": "nsga_nrel"})
            assert algo.name() == "nsga2"
            assert algo.is_iterative() is True
        finally:
            AlgorithmRegistry._registry.pop("nsga2", None)

    def test_sobol_resolves_when_registered(self) -> None:
        from osimflow.algorithms import BaseAlgorithm

        class StubSobol(BaseAlgorithm):
            def generate_samples(
                self, variables: dict, n_samples: int, seed: int | None, outdir: Path
            ) -> Path:
                return Path("stub")

            def observe(self, history: list[dict]) -> list[dict]:
                return []

            def is_converged(self, history: list[dict]) -> bool:
                return True

            def name(self) -> str:
                return "sobol"

            def is_iterative(self) -> bool:
                return False

        AlgorithmRegistry.register("sobol", StubSobol)
        try:
            algo = _resolve_algorithm({"type": "sobol"})
            assert algo.name() == "sobol"
        finally:
            AlgorithmRegistry._registry.pop("sobol", None)
