"""``examples/parametric_variables.yml`` is domain-general and valid for ``example_package`` (#2082)."""

from pathlib import Path

import yaml

from osimflow.measures import MeasureRegistry

REPO_ROOT = Path(__file__).resolve().parents[3]
EXAMPLE_VARS = REPO_ROOT / "examples" / "parametric_variables.yml"
EXAMPLE_PKG = REPO_ROOT / "example_package"


def test_example_variables_validate_against_example_package() -> None:
    variables = yaml.safe_load(EXAMPLE_VARS.read_text())["variables"]
    registry = MeasureRegistry()
    registry.index_measures(EXAMPLE_PKG)
    registry.validate_variables_mapping(variables, registry)


def test_example_variables_demonstrate_multiple_distributions() -> None:
    variables = yaml.safe_load(EXAMPLE_VARS.read_text())["variables"]
    assert {v["distribution"] for v in variables} >= {"uniform", "normal", "triangular", "discrete"}


def test_example_variables_have_no_legacy_measures() -> None:
    text = EXAMPLE_VARS.read_text()
    for legacy in ("comstock", "179", "bem-to-surrogate"):
        assert legacy not in text.lower()
