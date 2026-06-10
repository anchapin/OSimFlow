"""Tests for DEFAULT_STEP_RESOURCES and get_step_resources (issue #39)."""

from osimflow.executors import DEFAULT_STEP_RESOURCES, get_step_resources


class TestDefaultStepResources:
    """Verify the constant dict shape and get_step_resources helper."""

    EXPECTED_STEPS = [
        "GENERATE_LHS_SAMPLES",
        "APPLY_PARAMETERS",
        "RUN_OPENSTUDIO_SIM",
        "EXTRACT_KPIS",
        "AGGREGATE_RESULTS",
        "GENERATE_BASIC_PLOTS",
    ]

    def test_all_six_steps_present(self) -> None:
        for step in self.EXPECTED_STEPS:
            assert step in DEFAULT_STEP_RESOURCES, f"missing step: {step}"

    def test_each_entry_has_required_keys(self) -> None:
        for step, resources in DEFAULT_STEP_RESOURCES.items():
            assert "cpus" in resources, f"{step} missing cpus"
            assert "memory_mb" in resources, f"{step} missing memory_mb"
            assert "time_min" in resources, f"{step} missing time_min"

    def test_values_are_positive_integers(self) -> None:
        for step, resources in DEFAULT_STEP_RESOURCES.items():
            for key, value in resources.items():
                assert isinstance(value, int), f"{step}.{key} is not int"
                assert value > 0, f"{step}.{key} must be positive"

    def test_sim_step_is_the_heaviest(self) -> None:
        sim = DEFAULT_STEP_RESOURCES["RUN_OPENSTUDIO_SIM"]
        for step in self.EXPECTED_STEPS:
            if step == "RUN_OPENSTUDIO_SIM":
                continue
            other = DEFAULT_STEP_RESOURCES[step]
            assert sim["cpus"] >= other["cpus"], (
                f"SIM cpus ({sim['cpus']}) < {step} cpus ({other['cpus']})"
            )
            assert sim["memory_mb"] >= other["memory_mb"], (
                f"SIM memory ({sim['memory_mb']}) < {step} memory ({other['memory_mb']})"
            )

    def test_get_step_resources_known_step(self) -> None:
        resources = get_step_resources("RUN_OPENSTUDIO_SIM")
        assert resources == DEFAULT_STEP_RESOURCES["RUN_OPENSTUDIO_SIM"]

    def test_get_step_resources_unknown_step_returns_fallback(self) -> None:
        resources = get_step_resources("NONEXISTENT_STEP")
        assert resources == {"cpus": 1, "memory_mb": 1024, "time_min": 60}

    def test_apply_parameters_is_lightweight(self) -> None:
        apply = DEFAULT_STEP_RESOURCES["APPLY_PARAMETERS"]
        assert apply["cpus"] == 1
        assert apply["memory_mb"] <= 1024

    def test_sim_step_at_least_4_cpus(self) -> None:
        sim = DEFAULT_STEP_RESOURCES["RUN_OPENSTUDIO_SIM"]
        assert sim["cpus"] >= 4
        assert sim["memory_mb"] >= 8192
        assert sim["time_min"] >= 120
