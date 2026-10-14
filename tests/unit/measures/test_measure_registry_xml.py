"""Unit tests for MeasureRegistry XML parsing, delimiter-aware Ruby/Python
OSArgument scanning, and OSW package variable validation (issue #2052).
"""

from pathlib import Path
from typing import Any

import pytest

from osimflow.measures import MeasureArgument, MeasureRegistry, UnmappedVariableError

# ---------------------------------------------------------------------------
# 1. measure.xml parsing, precedence, union/deduplication, malformed fallback
# ---------------------------------------------------------------------------


def test_read_measure_arguments_parses_measure_xml(tmp_path: Path) -> None:
    """measure.xml <argument> entries are parsed with normalized types and defaults."""
    measure_dir = tmp_path / "ServerDirectoryCleanup"
    measure_dir.mkdir()
    (measure_dir / "measure.xml").write_text(
        """<?xml version="1.0"?>
<measure>
  <schema_version>3.1</schema_version>
  <name>server_directory_cleanup</name>
  <class_name>ServerDirectoryCleanup</class_name>
  <arguments>
    <argument>
      <name>sql</name>
      <type>Boolean</type>
      <required>true</required>
      <default_value>true</default_value>
    </argument>
    <argument>
      <name>Sizing Run Directories</name>
      <type>Bool</type>
      <required>false</required>
      <default_value>false</default_value>
    </argument>
    <argument>
      <name>r_value</name>
      <type>Double</type>
      <required>true</required>
      <default_value>13.0</default_value>
    </argument>
    <argument>
      <name>timesteps_per_hr</name>
      <type>Integer</type>
      <required>true</required>
      <default_value>4</default_value>
    </argument>
    <argument>
      <name>template</name>
      <type>Choice</type>
      <required>true</required>
      <default_value>90.1-2019</default_value>
    </argument>
    <argument>
      <name>epw_path</name>
      <type>Path</type>
      <required>false</required>
      <default_value/>
    </argument>
    <argument>
      <name>custom_tag</name>
      <type>UnknownCustomType</type>
      <required>0</required>
    </argument>
  </arguments>
</measure>
""",
        encoding="utf-8",
    )

    registry = MeasureRegistry()
    args = registry.read_measure_arguments(measure_dir)

    assert [a.name for a in args] == [
        "sql",
        "Sizing Run Directories",
        "r_value",
        "timesteps_per_hr",
        "template",
        "epw_path",
        "custom_tag",
    ]
    by_name = {a.name: a for a in args}
    assert by_name["sql"] == MeasureArgument(
        name="sql", type="Boolean", required=True, default="true"
    )
    assert by_name["Sizing Run Directories"] == MeasureArgument(
        name="Sizing Run Directories", type="Boolean", required=False, default="false"
    )
    assert by_name["r_value"] == MeasureArgument(
        name="r_value", type="Double", required=True, default="13.0"
    )
    assert by_name["timesteps_per_hr"] == MeasureArgument(
        name="timesteps_per_hr", type="Integer", required=True, default="4"
    )
    assert by_name["template"] == MeasureArgument(
        name="template", type="Choice", required=True, default="90.1-2019"
    )
    assert by_name["epw_path"] == MeasureArgument(
        name="epw_path", type="Path", required=False, default=None
    )
    assert by_name["custom_tag"] == MeasureArgument(
        name="custom_tag", type="String", required=False, default=None
    )


def test_discover_measure_xml_only_uses_directory_name(tmp_path: Path) -> None:
    """_discover_measure accepts measure.xml-only dirs and keys by directory name."""
    pkg = tmp_path / "pkg"
    rb_dir = pkg / "measures" / "ChangeBuildingLocation"
    rb_dir.mkdir(parents=True)
    (rb_dir / "measure.xml").write_text(
        """<?xml version="1.0"?>
<measure>
  <name>change_building_location</name>
  <class_name>ChangeBuildingLocation</class_name>
  <arguments>
    <argument>
      <name>weather_file_name</name>
      <type>String</type>
      <required>true</required>
    </argument>
  </arguments>
</measure>
""",
        encoding="utf-8",
    )

    py_dir = pkg / "measures" / "PythonXmlOnlyMeasure"
    py_dir.mkdir(parents=True)
    (py_dir / "measure.xml").write_text(
        """<?xml version="1.0"?>
<measure>
  <name>python_xml_only</name>
  <arguments>
    <argument>
      <name>setpoint</name>
      <type>Double</type>
      <required>true</required>
    </argument>
  </arguments>
  <files>
    <file>
      <filename>measure.py</filename>
      <filetype>py</filetype>
      <usage_type>script</usage_type>
    </file>
  </files>
</measure>
""",
        encoding="utf-8",
    )

    registry = MeasureRegistry()
    registry.index_measures(pkg)

    assert "ChangeBuildingLocation" in registry._measures
    assert "change_building_location" not in registry._measures
    assert registry._measures["ChangeBuildingLocation"].language == "ruby"
    assert registry._measures["PythonXmlOnlyMeasure"].language == "python"


def test_read_measure_arguments_xml_precedence_and_source_union(tmp_path: Path) -> None:
    """measure.xml is authoritative first; extra source args are appended in source order."""
    measure_dir = tmp_path / "HybridMeasure"
    measure_dir.mkdir()
    (measure_dir / "measure.xml").write_text(
        """<?xml version="1.0"?>
<measure>
  <arguments>
    <argument>
      <name>first_xml_arg</name>
      <type>Choice</type>
      <required>true</required>
      <default_value>opt_a</default_value>
    </argument>
    <argument>
      <name>shared_arg</name>
      <type>Double</type>
      <required>false</required>
      <default_value>42.5</default_value>
    </argument>
  </arguments>
</measure>
""",
        encoding="utf-8",
    )
    (measure_dir / "measure.rb").write_text(
        """
class HybridMeasure < OpenStudio::Measure::ModelMeasure
  def arguments(model)
    args = OpenStudio::Measure::OSArgumentVector.new
    args << OpenStudio::Measure::OSArgument.makeStringArgument('extra_rb_first', true)
    args << OpenStudio::Measure::OSArgument.makeDoubleArgument('shared_arg', true)
    args << OpenStudio::Measure::OSArgument.makeBoolArgument('extra_rb_second', false)
    args
  end
end
""",
        encoding="utf-8",
    )

    registry = MeasureRegistry()
    args = registry.read_measure_arguments(measure_dir)

    assert [a.name for a in args] == [
        "first_xml_arg",
        "shared_arg",
        "extra_rb_first",
        "extra_rb_second",
    ]
    by_name = {a.name: a for a in args}
    # XML metadata wins on duplicate 'shared_arg' (required=False, default='42.5').
    assert by_name["shared_arg"].required is False
    assert by_name["shared_arg"].default == "42.5"
    assert by_name["extra_rb_first"].type == "String"
    assert by_name["extra_rb_second"].type == "Boolean"


def test_read_measure_arguments_malformed_xml_falls_back_to_source(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Malformed measure.xml logs a warning and falls back to measure.rb parsing."""
    measure_dir = tmp_path / "BrokenXmlMeasure"
    measure_dir.mkdir()
    (measure_dir / "measure.xml").write_text(
        "<measure><arguments><argument><name>unclosed",
        encoding="utf-8",
    )
    (measure_dir / "measure.rb").write_text(
        """
class BrokenXmlMeasure < OpenStudio::Measure::ModelMeasure
  def arguments(model)
    args = OpenStudio::Measure::OSArgumentVector.new
    args << OpenStudio::Measure::OSArgument.makeDoubleArgument('fallback_r_value', true)
    args
  end
end
""",
        encoding="utf-8",
    )

    registry = MeasureRegistry()
    with caplog.at_level("WARNING", logger="osimflow.measures"):
        args = registry.read_measure_arguments(measure_dir)

    assert len(args) == 1
    assert args[0].name == "fallback_r_value"
    assert any("Failed to parse measure.xml" in r.message for r in caplog.records)


def test_server_directory_cleanup_dynamic_loop_via_measure_xml(tmp_path: Path) -> None:
    """ServerDirectoryCleanup defines args in a Ruby loop; measure.xml supplies all 12."""
    measure_dir = tmp_path / "ServerDirectoryCleanup"
    measure_dir.mkdir()
    expected_names = [
        "sql",
        "eso",
        "audit",
        "osm",
        "idf",
        "bnd",
        "eio",
        "shd",
        "mdd",
        "rdd",
        "csv",
        "Sizing Run Directories",
    ]
    xml_items = "\n".join(
        f"    <argument><name>{n}</name><type>Boolean</type><required>true</required><default_value>true</default_value></argument>"
        for n in expected_names
    )
    (measure_dir / "measure.xml").write_text(
        f"<measure><arguments>\n{xml_items}\n</arguments></measure>",
        encoding="utf-8",
    )
    (measure_dir / "measure.rb").write_text(
        """
class ServerDirectoryCleanup < OpenStudio::Measure::UtilityMeasure
  def arguments
    args = OpenStudio::Measure::OSArgumentVector.new
    ServerDirectoryCleanup.file_types.each do |file_type, _|
      arg = OpenStudio::Measure::OSArgument.makeBoolArgument(file_type, true)
      arg.setDefaultValue(true)
      args << arg
    end
    args
  end
end
""",
        encoding="utf-8",
    )

    registry = MeasureRegistry()
    args = registry.read_measure_arguments(measure_dir)
    assert [a.name for a in args] == expected_names


# ---------------------------------------------------------------------------
# 2. Delimiter-aware Ruby & Python OSArgument call scanner
# ---------------------------------------------------------------------------


def test_ruby_scanner_ruleset_namespace_and_nested_calls(tmp_path: Path) -> None:
    """Ruby scanner handles OpenStudio::Ruleset::OSArgument and nested parens/commas."""
    measure_dir = tmp_path / "simulation_settings"
    measure_dir.mkdir()
    (measure_dir / "measure.rb").write_text(
        """
class SimulationSettings < OpenStudio::Measure::ModelMeasure
  def arguments(model)
    args = OpenStudio::Measure::OSArgumentVector.new
    timesteps_per_hr = OpenStudio::Measure::OSArgument.makeIntegerArgument('timesteps_per_hr', true)
    enable_dst = OpenStudio::Measure::OSArgument.makeBoolArgument('enable_dst', true)
    dst_start = OpenStudio::Ruleset::OSArgument.makeStringArgument('dst_start', true)
    dst_end = OpenStudio::Ruleset::OSArgument.makeStringArgument('dst_end', true)
    calendar_year = OpenStudio::Measure::OSArgument.makeIntegerArgument('calendar_year', true)
    jan_first_day_of_wk = OpenStudio::Ruleset::OSArgument.makeChoiceArgument(
      'jan_first_day_of_wk', days_of_week_choices, true
    )
    begin_month = OpenStudio::Ruleset::OSArgument.makeIntegerArgument('begin_month', true)
    begin_day = OpenStudio::Ruleset::OSArgument.makeIntegerArgument('begin_day', true)
    end_month = OpenStudio::Ruleset::OSArgument.makeIntegerArgument('end_month', true)
    end_day = OpenStudio::Ruleset::OSArgument.makeIntegerArgument('end_day', true)
    output_diagnostics = OpenStudio::Measure::OSArgument.makeChoiceArgument(
      'output_diagnostics', diag_chs, false
    )
    # climate_zone = OpenStudio::Measure::OSArgument.makeChoiceArgument("climate_zone_commented", get_climate_zones(false, "Lookup From EPW File"), true)
    climate_zone = OpenStudio::Measure::OSArgument.makeChoiceArgument(
      'climate_zone',
      OpenstudioStandards::CreateTypical.get_climate_zones(false, 'Lookup From Model'),
      true
    )
    args
  end
end
""",
        encoding="utf-8",
    )

    registry = MeasureRegistry()
    args = registry.read_measure_arguments(measure_dir)

    assert [a.name for a in args] == [
        "timesteps_per_hr",
        "enable_dst",
        "dst_start",
        "dst_end",
        "calendar_year",
        "jan_first_day_of_wk",
        "begin_month",
        "begin_day",
        "end_month",
        "end_day",
        "output_diagnostics",
        "climate_zone",
    ]
    by_name = {a.name: a for a in args}
    assert by_name["dst_start"].type == "String"
    assert by_name["jan_first_day_of_wk"].type == "Choice"
    assert by_name["output_diagnostics"].required is False
    assert by_name["climate_zone"].type == "Choice"
    assert by_name["climate_zone"].required is True
    assert "climate_zone_commented" not in by_name


def test_adjacent_multiline_choice_calls_do_not_span_across_calls(tmp_path: Path) -> None:
    """Two adjacent multiline makeChoiceArgument calls keep their own required flags."""
    measure_dir = tmp_path / "upgrade_hvac_add_heat_pump_rtu"
    measure_dir.mkdir()
    (measure_dir / "measure.rb").write_text(
        """
class AddHeatPumpRtu < OpenStudio::Measure::ModelMeasure
  def arguments(_model)
    args = OpenStudio::Measure::OSArgumentVector.new
    backup_ht_fuel_scheme = OpenStudio::Measure::OSArgument.makeChoiceArgument(
      'backup_ht_fuel_scheme',
      ['match_original_primary_heating_fuel', 'electric_resistance_backup'],
      false
    )
    performance_oversizing_factor = OpenStudio::Measure::OSArgument.makeDoubleArgument(
      'performance_oversizing_factor',
      true
    )
    htg_sizing_option = OpenStudio::Measure::OSArgument.makeChoiceArgument(
      'htg_sizing_option',
      get_options(1, 2, { a: [3, 4] }),
      true
    )
    three_arg_bool = OpenStudio::Measure::OSArgument.makeBoolArgument(
      'three_arg_bool',
      false,
      true
    )
    args
  end
end
""",
        encoding="utf-8",
    )

    registry = MeasureRegistry()
    args = registry.read_measure_arguments(measure_dir)

    assert [a.name for a in args] == [
        "backup_ht_fuel_scheme",
        "performance_oversizing_factor",
        "htg_sizing_option",
        "three_arg_bool",
    ]
    by_name = {a.name: a for a in args}
    assert by_name["backup_ht_fuel_scheme"].required is False
    assert by_name["performance_oversizing_factor"].required is True
    assert by_name["htg_sizing_option"].required is True
    assert by_name["three_arg_bool"].type == "Boolean"
    assert by_name["three_arg_bool"].required is False


def test_python_scanner_ruleset_3arg_choice_and_multiline(tmp_path: Path) -> None:
    """Python scanner handles openstudio.ruleset, 3-arg Choice, 3-arg Bool, 4-arg Path."""
    measure_dir = tmp_path / "PyAdvancedMeasure"
    measure_dir.mkdir()
    (measure_dir / "measure.py").write_text(
        """
import openstudio

class PyAdvancedMeasure(openstudio.measure.ModelMeasure):
    def arguments(self, model):
        args = openstudio.measure.OSArgumentVector()
        # 3-arg makeChoiceArgument with nested function call containing commas
        c1 = openstudio.measure.OSArgument.makeChoiceArgument(
            "hvac_system",
            get_hvac_choices("commercial", include_legacy=False),
            False,
        )
        # 4-arg makeChoiceArgument in openstudio.ruleset namespace
        c2 = openstudio.ruleset.OSArgument.makeChoiceArgument(
            "climate_zone",
            ["1A", "2A", "3A"],
            True,
            model,
        )
        # 3-arg makeBoolArgument
        b1 = openstudio.measure.OSArgument.makeBoolArgument("use_dcv", True, False)
        # 4-arg makePathArgument (name, isRead, extension, required)
        p1 = openstudio.ruleset.OSArgument.makePathArgument(
            "schedule_file", True, "csv", False
        )
        # Keyword required=True
        d1 = openstudio.measure.OSArgument.makeDoubleArgument(
            "cop", required=True
        )
        return args
""",
        encoding="utf-8",
    )

    registry = MeasureRegistry()
    args = registry.read_measure_arguments(measure_dir)

    assert [a.name for a in args] == [
        "hvac_system",
        "climate_zone",
        "use_dcv",
        "schedule_file",
        "cop",
    ]
    by_name = {a.name: a for a in args}
    assert by_name["hvac_system"] == MeasureArgument("hvac_system", "Choice", False)
    assert by_name["climate_zone"] == MeasureArgument("climate_zone", "Choice", True)
    assert by_name["use_dcv"] == MeasureArgument("use_dcv", "Boolean", True)
    assert by_name["schedule_file"] == MeasureArgument("schedule_file", "Path", False)
    assert by_name["cop"] == MeasureArgument("cop", "Double", True)


# ---------------------------------------------------------------------------
# 3. All 23 commercial-building measures discovery + validation (__epw_file__, __SKIP__, .osm)
# ---------------------------------------------------------------------------

_COMMERCIAL_23_MEASURES: dict[str, list[tuple[str, str]]] = {
    "baseline_hvac_control": [
        ("standard", "Choice"),
        ("building_type", "Choice"),
        ("climate_zone", "Choice"),
        ("custom", "Choice"),
        ("debug", "Boolean"),
        ("use_upstream_args", "Boolean"),
    ],
    "ChangeBuildingLocation": [
        ("weather_file_name", "String"),
        ("set_year", "Integer"),
        ("use_upstream_args", "Boolean"),
        ("epw_gsub", "Choice"),
    ],
    "IncreaseInsulationRValueForExteriorWalls": [
        ("r_value", "Double"),
        ("allow_reduction", "Boolean"),
        ("material_cost_increase_ip", "Double"),
        ("one_time_retrofit_cost_ip", "Double"),
        ("years_until_retrofit_cost", "Integer"),
    ],
    "IncreaseInsulationRValueForRoofs": [
        ("r_value", "Double"),
        ("allow_reduction", "Boolean"),
        ("material_cost_increase_ip", "Double"),
        ("one_time_retrofit_cost_ip", "Double"),
        ("years_until_retrofit_cost", "Integer"),
    ],
    "ServerDirectoryCleanup": [
        ("sql", "Boolean"),
        ("eso", "Boolean"),
        ("audit", "Boolean"),
        ("osm", "Boolean"),
        ("idf", "Boolean"),
        ("bnd", "Boolean"),
        ("eio", "Boolean"),
        ("shd", "Boolean"),
        ("mdd", "Boolean"),
        ("rdd", "Boolean"),
        ("csv", "Boolean"),
        ("Sizing Run Directories", "Boolean"),
    ],
    "SetLightingLoadsByLPD": [("lpd", "Double")],
    "create_baseline_building": [
        ("standard", "Choice"),
        ("building_type", "Choice"),
        ("climate_zone", "Choice"),
        ("custom", "Choice"),
        ("debug", "Boolean"),
        ("use_upstream_args", "Boolean"),
    ],
    "create_bar_from_building_type_ratios_comstock": [
        ("bldg_type_a", "Choice"),
        ("total_bldg_floor_area", "Double"),
        ("num_stories_above_grade", "Double"),
        ("num_stories_below_grade", "Integer"),
        ("building_rotation", "Double"),
        ("template", "Choice"),
        ("ns_to_ew_ratio", "Double"),
        ("wwr", "Double"),
        ("climate_zone", "Choice"),
    ],
    "create_typical_building_from_model_comstock": [
        ("template", "Choice"),
        ("system_type", "Choice"),
        ("hvac_delivery_type", "Choice"),
        ("htg_src", "Choice"),
        ("clg_src", "Choice"),
        ("swh_src", "Choice"),
        ("wkdy_op_hrs_start_time", "Double"),
        ("wkdy_op_hrs_duration", "Double"),
        ("climate_zone", "Choice"),
    ],
    "hardsize_model": [("apply_hardsize", "Boolean")],
    "hardsize_model_copy": [("apply_hardsize", "Boolean")],
    "nze_hvac": [
        ("remove_existing_hvac", "Boolean"),
        ("hvac_system_type", "Choice"),
        ("doas_dcv", "Boolean"),
        ("hvac_system_partition", "Choice"),
    ],
    "replace_baseline_windows_Rvalues": [
        ("window_pane_type", "Choice"),
        ("fenestration_frame_type", "Choice"),
        ("u_value_ip", "Double"),
        ("shgc", "Double"),
        ("vlt", "Double"),
    ],
    "openstudio_results": [
        ("disable_upstream_arg", "Boolean"),
        ("add_warning_severe_counts", "Boolean"),
    ],
    "openstudio_results_support": [
        ("disable_upstream_arg", "Boolean"),
        ("hvac_type_from_upgrade", "Boolean"),
    ],
    "set_heating_cooling_cop_efficiency": [
        ("cooling_cop", "Double"),
        ("heating_cop", "Double"),
        ("efficiency", "Double"),
        ("boiler_efficiency", "Double"),
        ("chiller_cop", "Double"),
    ],
    "set_water_heater_efficiency": [
        ("performance_category_ratio", "Double"),
        ("capacity_btu_per_hr_ratio", "Double"),
        ("first_hour_rating_ratio", "Double"),
    ],
    "simulation_settings": [
        ("timesteps_per_hr", "Integer"),
        ("enable_dst", "Boolean"),
        ("dst_start", "String"),
        ("dst_end", "String"),
        ("calendar_year", "Integer"),
        ("jan_first_day_of_wk", "String"),
        ("begin_month", "Integer"),
        ("begin_day", "Integer"),
        ("end_month", "Integer"),
        ("end_day", "Integer"),
        ("output_diagnostics", "Boolean"),
    ],
    "swh_patch": [
        ("capacity_btu_per_hr_ratio", "Double"),
        ("first_hour_rating_ratio", "Double"),
        ("use_upstream_arg", "Boolean"),
    ],
    "upgrade_hvac_add_heat_pump_rtu": [
        ("backup_ht_fuel_scheme", "Choice"),
        ("performance_oversizing_factor", "Double"),
        ("htg_sizing_option", "Choice"),
        ("clg_oversizing_estimate", "Double"),
        ("htg_to_clg_hp_ratio", "Double"),
        ("hp_min_comp_lockout_temp_f", "Double"),
        ("hprtu_scenario", "Choice"),
        ("hr", "Boolean"),
        ("dcv", "Boolean"),
        ("econ", "Boolean"),
        ("roof", "Boolean"),
        ("window", "Boolean"),
        ("sizing_run", "Boolean"),
        ("debug_verbose", "Boolean"),
        ("modify_setbacks", "Boolean"),
        ("setback_value", "Double"),
    ],
    "upgrade_hvac_doas_hp_minisplits": [
        ("area_limit_sf", "Double"),
        ("doas_htg_fuel", "Choice"),
        ("performance_oversizing_factor", "Double"),
        ("apply_measure", "Boolean"),
    ],
    "upgrade_hvac_erv_dcv": [("erv_or_dcv", "Choice")],
    "upgrade_hvac_vrf_hr_doas": [
        ("vrf_defrost_strategy", "Choice"),
        ("disable_defrost", "Boolean"),
        ("upsizing_allowance_pct", "Double"),
        ("apply_measure", "Boolean"),
    ],
}


def test_all_23_commercial_measures_indexed_and_validated(tmp_path: Path) -> None:
    """All 23 commercial measures are indexed and validate including __SKIP__ and __epw_file__."""
    pkg = tmp_path / "commercial_package"
    measures_root = pkg / "measures"
    for measure_name, arg_specs in _COMMERCIAL_23_MEASURES.items():
        mdir = measures_root / measure_name
        mdir.mkdir(parents=True)
        xml_args = "\n".join(
            f"    <argument><name>{aname}</name><type>{atype}</type><required>true</required></argument>"
            for aname, atype in arg_specs
        )
        (mdir / "measure.xml").write_text(
            f"<measure><name>{measure_name.lower()}</name><arguments>\n{xml_args}\n</arguments></measure>",
            encoding="utf-8",
        )

    registry = MeasureRegistry()
    registry.index_measures(pkg)

    assert len(registry._measures) == 23
    assert set(registry._measures.keys()) == set(_COMMERCIAL_23_MEASURES.keys())

    variables: list[dict[str, Any]] = [
        {"name": "__epw_file__"},
        {"name": "weather_override", "measure_argument": "__epw_file__"},
        {
            "name": "upgrade_hvac_doas_hp_minisplits.__SKIP__",
            "measure_argument": "upgrade_hvac_doas_hp_minisplits.__SKIP__",
        },
        {"name": "upgrade_hvac_vrf_hr_doas.__SKIP__"},
        {
            "name": "create_typical_building_from_model_comstock.climate_zone",
            "measure_argument": "create_typical_building_from_model_comstock.climate_zone",
        },
        {
            "name": "simulation_settings.jan_first_day_of_wk",
            "measure_argument": "simulation_settings.jan_first_day_of_wk",
        },
        {
            "name": "ServerDirectoryCleanup.Sizing Run Directories",
            "measure_argument": "ServerDirectoryCleanup.Sizing Run Directories",
        },
        {
            "name": "upgrade_hvac_add_heat_pump_rtu.backup_ht_fuel_scheme",
            "measure_argument": "upgrade_hvac_add_heat_pump_rtu.backup_ht_fuel_scheme",
        },
    ]
    registry.validate_variables_mapping(variables, registry)


def test_validate_variables_mapping_rejects_unknown_measure_skip(tmp_path: Path) -> None:
    """UnknownMeasure.__SKIP__ is rejected in both name and measure_argument."""
    pkg = tmp_path / "pkg"
    mdir = pkg / "measures" / "KnownMeasure"
    mdir.mkdir(parents=True)
    (mdir / "measure.rb").write_text(
        'OpenStudio::Measure::OSArgument.makeDoubleArgument("x", true)\n',
        encoding="utf-8",
    )

    registry = MeasureRegistry()
    registry.index_measures(pkg)

    with pytest.raises(UnmappedVariableError, match="UnknownMeasure.__SKIP__"):
        registry.validate_variables_mapping(
            [{"name": "UnknownMeasure.__SKIP__"}],
            registry,
        )

    with pytest.raises(UnmappedVariableError, match="UnknownMeasure.__SKIP__"):
        registry.validate_variables_mapping(
            [
                {
                    "name": "skip_unknown",
                    "measure_argument": "UnknownMeasure.__SKIP__",
                }
            ],
            registry,
        )


def test_validate_variables_mapping_mixed_osm_and_measures(tmp_path: Path) -> None:
    """Valid .osm attributes pass when model.osm exists in package, fail when absent."""
    pkg = tmp_path / "mixed_pkg"
    mdir = pkg / "measures" / "SetLightingLoadsByLPD"
    mdir.mkdir(parents=True)
    (mdir / "measure.rb").write_text(
        'OpenStudio::Measure::OSArgument.makeDoubleArgument("lpd", true)\n',
        encoding="utf-8",
    )

    registry = MeasureRegistry()
    registry.index_measures(pkg)

    mixed_vars: list[dict[str, Any]] = [
        {"name": "lpd"},
        {"name": "lighting_power_density"},
        {"name": "SpaceType_General_Office.lighting_power_density"},
        {"name": "ThermalZone_Core_ZN.cooling_setpoint"},
    ]

    # Without model.osm in pkg (and has_osm not set), .osm attributes fail.
    with pytest.raises(UnmappedVariableError, match="lighting_power_density"):
        registry.validate_variables_mapping(mixed_vars, registry)

    # With model.osm present in pkg, auto-detected has_osm accepts .osm attributes.
    (pkg / "model.osm").write_text("OS:Version,\n  {00000000-0000-0000-0000-000000000000};\n")
    registry.validate_variables_mapping(mixed_vars, registry)

    # Unknown .osm object or attribute still fails even when model.osm is present.
    with pytest.raises(UnmappedVariableError, match="UnknownObject_Foo.bar"):
        registry.validate_variables_mapping(
            [*mixed_vars, {"name": "UnknownObject_Foo.bar"}],
            registry,
        )
