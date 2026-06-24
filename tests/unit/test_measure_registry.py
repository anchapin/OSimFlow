"""Tests for osimflow/measures.py (issue #334).

Covers:
- MeasureRegistry.index_measures: Ruby and Python measure discovery
- MeasureRegistry.read_measure_arguments: Ruby and Python argument parsing
- MeasureRegistry.validate_variables_mapping: variable-to-argument validation
- MeasureRegistry.list_available_measures: measure listing
- UnmappedVariableError and AmbiguousVariableError exceptions
"""

from __future__ import annotations

from pathlib import Path

import pytest

from osimflow.measures import (
    MeasureRegistry,
    UnmappedVariableError,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def measures_pkg(tmp_path: Path) -> Path:
    """Create a template package with a measures/ directory."""
    pkg = tmp_path / "template"
    measures = pkg / "measures"
    measures.mkdir(parents=True)
    return pkg  # index_measures takes template root, not measures/ dir


# ---------------------------------------------------------------------------
# index_measures
# ---------------------------------------------------------------------------
def test_index_measures_discovers_ruby_measure(measures_pkg: Path) -> None:
    """Ruby measure directories are registered with their arguments."""
    measure_dir = measures_pkg / "measures" / "SetWindowToWallRatio"
    measure_dir.mkdir(parents=True)
    (measure_dir / "measure.rb").write_text(
        """
class SetWindowToWallRatio < OpenStudio::Measure::ModelMeasure
  def arguments(model)
    args = OpenStudio::Measure::OSArgumentVector.new
    arg = OpenStudio::Measure::OSArgument.makeDoubleArgument("wwr", true)
    arg.setDefaultValue(0.4)
    args << arg
    arg2 = OpenStudio::Measure::OSArgument.makeDoubleArgument("facade", false)
    arg2.setDefaultValue(1.0)
    args << arg2
    return args
  end
end
"""
    )

    registry = MeasureRegistry()
    registry.index_measures(measures_pkg)

    assert "SetWindowToWallRatio" in registry._measures
    measure = registry._measures["SetWindowToWallRatio"]
    assert measure.language == "ruby"
    assert len(measure.arguments) == 2
    arg_names = {a.name for a in measure.arguments}
    assert arg_names == {"wwr", "facade"}


def test_index_measures_discovers_python_measure(measures_pkg: Path) -> None:
    """Python measure directories are registered with their arguments."""
    measure_dir = measures_pkg / "measures" / "SetThermostatSchedule"
    measure_dir.mkdir(parents=True)
    (measure_dir / "measure.py").write_text(
        """
import openstudio

class SetThermostatSchedule(openstudio.measure.ModelMeasure):
    def arguments(self, model):
        args = openstudio.measure.OSArgumentVector()
        arg = openstudio.measure.OSArgument.makeDoubleArgument("heating_setpoint", True)
        arg.setDefaultValue(20.0)
        args.append(arg)
        arg2 = openstudio.measure.OSArgument.makeDoubleArgument("cooling_setpoint", False)
        arg2.setDefaultValue(25.0)
        args.append(arg2)
        return args
"""
    )

    registry = MeasureRegistry()
    registry.index_measures(measures_pkg)

    assert "SetThermostatSchedule" in registry._measures
    measure = registry._measures["SetThermostatSchedule"]
    assert measure.language == "python"
    assert len(measure.arguments) == 2
    arg_names = {a.name for a in measure.arguments}
    assert arg_names == {"heating_setpoint", "cooling_setpoint"}


def test_index_measures_empty_when_no_measures_dir(tmp_path: Path) -> None:
    """No measures are registered when the package has no measures/ directory."""
    pkg = tmp_path / "no_measures"
    pkg.mkdir()

    registry = MeasureRegistry()
    registry.index_measures(pkg)

    assert registry._measures == {}


def test_index_measures_skips_measure_without_entry_point(measures_pkg: Path) -> None:
    """A measure directory without measure.rb or measure.py is skipped."""
    measure_dir = measures_pkg / "EmptyMeasure"
    measure_dir.mkdir()
    (measure_dir / "README.md").write_text("This is not a measure.")

    registry = MeasureRegistry()
    registry.index_measures(measures_pkg)

    assert "EmptyMeasure" not in registry._measures


def test_index_measures_discovers_multiple_measures(measures_pkg: Path) -> None:
    """Multiple measure directories are all registered."""
    for name in ("MeasureA", "MeasureB", "MeasureC"):
        d = measures_pkg / "measures" / name
        d.mkdir(parents=True)
        (d / "measure.rb").write_text(
            f"""
class {name} < OpenStudio::Measure::ModelMeasure
  def arguments(model)
    args = OpenStudio::Measure::OSArgumentVector.new
    return args
  end
end
"""
        )

    registry = MeasureRegistry()
    registry.index_measures(measures_pkg)

    assert len(registry._measures) == 3
    assert {"MeasureA", "MeasureB", "MeasureC"} == set(registry._measures.keys())


# ---------------------------------------------------------------------------
# read_measure_arguments
# ---------------------------------------------------------------------------
def test_read_measure_arguments_ruby_all_types(tmp_path: Path) -> None:
    """Ruby argument types are correctly parsed."""
    measure_dir = tmp_path / "TestMeasure"
    measure_dir.mkdir()
    (measure_dir / "measure.rb").write_text(
        """
class TestMeasure < OpenStudio::Measure::ModelMeasure
  def arguments(model)
    args = OpenStudio::Measure::OSArgumentVector.new
    args << OpenStudio::Measure::OSArgument.makeDoubleArgument("dbl", true)
    args << OpenStudio::Measure::OSArgument.makeStringArgument("str", false)
    args << OpenStudio::Measure::OSArgument.makeIntegerArgument("int", true)
    args << OpenStudio::Measure::OSArgument.makeBooleanArgument("bool", false)
    args << OpenStudio::Measure::OSArgument.makeChoiceArgument("choice", true)
    args << OpenStudio::Measure::OSArgument.makePathArgument("path", false)
    return args
  end
end
"""
    )

    registry = MeasureRegistry()
    args = registry.read_measure_arguments(measure_dir)

    assert len(args) == 6
    types = {a.type for a in args}
    assert types == {"Double", "String", "Integer", "Boolean", "Choice", "Path"}


def test_read_measure_arguments_python_all_types(tmp_path: Path) -> None:
    """Python argument types are correctly parsed."""
    measure_dir = tmp_path / "TestMeasure"
    measure_dir.mkdir()
    (measure_dir / "measure.py").write_text(
        """
import openstudio

class TestMeasure(openstudio.measure.ModelMeasure):
    def arguments(self, model):
        args = openstudio.measure.OSArgumentVector()
        args.append(openstudio.measure.OSArgument.makeDoubleArgument("dbl", True))
        args.append(openstudio.measure.OSArgument.makeStringArgument("str", False))
        args.append(openstudio.measure.OSArgument.makeIntegerArgument("int", True))
        args.append(openstudio.measure.OSArgument.makeBooleanArgument("bool", False))
        args.append(openstudio.measure.OSArgument.makeChoiceArgument("choice", True))
        args.append(openstudio.measure.OSArgument.makePathArgument("path", False))
        return args
"""
    )

    registry = MeasureRegistry()
    args = registry.read_measure_arguments(measure_dir)

    assert len(args) == 6
    types = {a.type for a in args}
    assert types == {"Double", "String", "Integer", "Boolean", "Choice", "Path"}


def test_read_measure_arguments_required_flag(measures_pkg: Path) -> None:
    """Required (true/false) flag is correctly extracted."""
    measure_dir = measures_pkg / "measures" / "RequiredTest"
    measure_dir.mkdir(parents=True)
    (measure_dir / "measure.rb").write_text(
        """
class RequiredTest < OpenStudio::Measure::ModelMeasure
  def arguments(model)
    args = OpenStudio::Measure::OSArgumentVector.new
    args << OpenStudio::Measure::OSArgument.makeDoubleArgument("required_arg", true)
    args << OpenStudio::Measure::OSArgument.makeDoubleArgument("optional_arg", false)
    return args
  end
end
"""
    )

    registry = MeasureRegistry()
    args = registry.read_measure_arguments(measure_dir)

    required_arg = next(a for a in args if a.name == "required_arg")
    optional_arg = next(a for a in args if a.name == "optional_arg")
    assert required_arg.required is True
    assert optional_arg.required is False


def test_read_measure_arguments_empty_for_missing_file(tmp_path: Path) -> None:
    """Returns empty list when neither measure.rb nor measure.py exists."""
    measure_dir = tmp_path / "NoEntryPoint"
    measure_dir.mkdir()

    registry = MeasureRegistry()
    args = registry.read_measure_arguments(measure_dir)

    assert args == []


# ---------------------------------------------------------------------------
# validate_variables_mapping
# ---------------------------------------------------------------------------
def test_validate_variables_mapping_all_valid(measures_pkg: Path) -> None:
    """All variables that map to discovered arguments pass validation."""
    measure_dir = measures_pkg / "measures" / "TestMeasure"
    measure_dir.mkdir(parents=True)
    (measure_dir / "measure.rb").write_text(
        """
class TestMeasure < OpenStudio::Measure::ModelMeasure
  def arguments(model)
    args = OpenStudio::Measure::OSArgumentVector.new
    args << OpenStudio::Measure::OSArgument.makeDoubleArgument("wwr", true)
    args << OpenStudio::Measure::OSArgument.makeDoubleArgument("wall_r_value", true)
    return args
  end
end
"""
    )

    registry = MeasureRegistry()
    registry.index_measures(measures_pkg)

    variables = [
        {"name": "wwr"},
        {"name": "TestMeasure.wall_r_value"},
    ]
    registry.validate_variables_mapping(variables, registry)


def test_validate_variables_mapping_unmapped_error(measures_pkg: Path) -> None:
    """Unmapped variable names raise UnmappedVariableError."""
    measure_dir = measures_pkg / "TestMeasure"
    measure_dir.mkdir()
    (measure_dir / "measure.rb").write_text(
        """
class TestMeasure < OpenStudio::Measure::ModelMeasure
  def arguments(model)
    args = OpenStudio::Measure::OSArgumentVector.new
    args << OpenStudio::Measure::OSArgument.makeDoubleArgument("wwr", true)
    return args
  end
end
"""
    )

    registry = MeasureRegistry()
    registry.index_measures(measures_pkg)

    variables = [{"name": "nonexistent_var"}]
    with pytest.raises(UnmappedVariableError):
        registry.validate_variables_mapping(variables, registry)


def test_validate_variables_mapping_ambiguous_error(measures_pkg: Path) -> None:
    """Plain variable name shared by multiple measures raises error."""
    measure_a = measures_pkg / "MeasureA"
    measure_a.mkdir()
    (measure_a / "measure.rb").write_text(
        """
class MeasureA < OpenStudio::Measure::ModelMeasure
  def arguments(model)
    args = OpenStudio::Measure::OSArgumentVector.new
    args << OpenStudio::Measure::OSArgument.makeDoubleArgument("shared_arg", true)
    return args
  end
end
"""
    )
    measure_b = measures_pkg / "MeasureB"
    measure_b.mkdir()
    (measure_b / "measure.rb").write_text(
        """
class MeasureB < OpenStudio::Measure::ModelMeasure
  def arguments(model)
    args = OpenStudio::Measure::OSArgumentVector.new
    args << OpenStudio::Measure::OSArgument.makeDoubleArgument("shared_arg", true)
    return args
  end
end
"""
    )

    registry = MeasureRegistry()
    registry.index_measures(measures_pkg)

    variables = [{"name": "shared_arg"}]
    with pytest.raises(UnmappedVariableError) as exc_info:
        registry.validate_variables_mapping(variables, registry)
    assert "shared_arg" in str(exc_info.value).lower()


def test_validate_variables_mapping_dotted_form_resolves_ambiguity(
    measures_pkg: Path,
) -> None:
    """Dotted variable names (MeasureName.arg) disambiguate correctly."""
    measure_a = measures_pkg / "measures" / "MeasureA"
    measure_a.mkdir(parents=True)
    (measure_a / "measure.rb").write_text(
        """
class MeasureA < OpenStudio::Measure::ModelMeasure
  def arguments(model)
    args = OpenStudio::Measure::OSArgumentVector.new
    args << OpenStudio::Measure::OSArgument.makeDoubleArgument("shared_arg", true)
    return args
  end
end
"""
    )
    measure_b = measures_pkg / "measures" / "MeasureB"
    measure_b.mkdir(parents=True)
    (measure_b / "measure.rb").write_text(
        """
class MeasureB < OpenStudio::Measure::ModelMeasure
  def arguments(model)
    args = OpenStudio::Measure::OSArgumentVector.new
    args << OpenStudio::Measure::OSArgument.makeDoubleArgument("shared_arg", true)
    return args
  end
end
"""
    )

    registry = MeasureRegistry()
    registry.index_measures(measures_pkg)

    variables = [
        {"name": "MeasureA.shared_arg"},
        {"name": "MeasureB.shared_arg"},
    ]
    registry.validate_variables_mapping(variables, registry)


def test_validate_variables_mapping_measure_argument_dotted_ref(
    measures_pkg: Path,
) -> None:
    """measure_argument dotted reference is validated against discovered measures."""
    measure_dir = measures_pkg / "measures" / "TestMeasure"
    measure_dir.mkdir(parents=True)
    (measure_dir / "measure.rb").write_text(
        """
class TestMeasure < OpenStudio::Measure::ModelMeasure
  def arguments(model)
    args = OpenStudio::Measure::OSArgumentVector.new
    args << OpenStudio::Measure::OSArgument.makeDoubleArgument("r_value", true)
    return args
  end
end
"""
    )

    registry = MeasureRegistry()
    registry.index_measures(measures_pkg)

    variables = [
        {"name": "my_var", "measure_argument": "TestMeasure.r_value"},
    ]
    registry.validate_variables_mapping(variables, registry)


def test_validate_variables_mapping_invalid_measure_argument(
    measures_pkg: Path,
) -> None:
    """Invalid measure_argument dotted reference raises UnmappedVariableError."""
    measure_dir = measures_pkg / "measures" / "TestMeasure"
    measure_dir.mkdir(parents=True)
    (measure_dir / "measure.rb").write_text(
        """
class TestMeasure < OpenStudio::Measure::ModelMeasure
  def arguments(model)
    args = OpenStudio::Measure::OSArgumentVector.new
    args << OpenStudio::Measure::OSArgument.makeDoubleArgument("r_value", true)
    return args
  end
end
"""
    )

    registry = MeasureRegistry()
    registry.index_measures(measures_pkg)

    variables = [
        {"name": "my_var", "measure_argument": "TestMeasure.nonexistent_arg"},
    ]
    with pytest.raises(UnmappedVariableError):
        registry.validate_variables_mapping(variables, registry)


# ---------------------------------------------------------------------------
# list_available_measures
# ---------------------------------------------------------------------------
def test_list_available_measures_returns_correct_structure(
    measures_pkg: Path,
) -> None:
    """list_available_measures returns name, path, language, and arguments."""
    measure_dir = measures_pkg / "measures" / "TestMeasure"
    measure_dir.mkdir(parents=True)
    (measure_dir / "measure.rb").write_text(
        """
class TestMeasure < OpenStudio::Measure::ModelMeasure
  def arguments(model)
    args = OpenStudio::Measure::OSArgumentVector.new
    args << OpenStudio::Measure::OSArgument.makeDoubleArgument("wwr", true)
    return args
  end
end
"""
    )

    registry = MeasureRegistry()
    registry.index_measures(measures_pkg)
    result = registry.list_available_measures()

    assert len(result) == 1
    m = result[0]
    assert m["name"] == "TestMeasure"
    assert m["language"] == "ruby"
    assert m["path"] == str(measure_dir)
    assert len(m["arguments"]) == 1
    assert m["arguments"][0]["name"] == "wwr"
    assert m["arguments"][0]["type"] == "Double"
    assert m["arguments"][0]["required"] is True


def test_list_available_measures_empty_when_no_measures(measures_pkg: Path) -> None:
    """Returns empty list when no measures are indexed."""
    registry = MeasureRegistry()
    assert registry.list_available_measures() == []
