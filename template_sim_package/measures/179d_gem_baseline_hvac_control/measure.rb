# frozen_string_literal: true

# *******************************************************************************
# OpenStudio(R), Copyright (c) Alliance for Sustainable Energy, LLC.
# See also https://openstudio.net/license
# *******************************************************************************

# Start the measure
class HVACControl179DBaseline < OpenStudio::Measure::ModelMeasure
  # load OpenStudio measure libraries from openstudio-extension gem
  require 'openstudio-extension'
  require 'openstudio-standards'
  require 'fileutils'
  require 'pathname'
  (Pathname.new(__dir__) / 'resources/179d_standards').glob('*.rb').sort_by { |p| p.basename.to_s.size }.each { |f| require f.sub_ext('').to_s } unless defined?(ACM179dASHRAE9012007)

  # Define the name of the Measure.
  def name
    return 'HVAC control 179D Baseline'
  end

  # Human readable description
  def description
    return 'Creates the Performance Rating Method baseline HVAC control'
  end

  # Human readable description of modeling approach
  def modeler_description
    return ''
  end

  # function for executing os stds methods
  def std_method_source_location(std, method_str, debug = false)
    source_location = std.method(method_str.to_sym).source_location
    message_str = "source_location of #{method_str} is #{source_location.join(':')}"
    pp message_str if debug
    puts(message_str)
  end

  def system_report(model)
    system_report_map = {
      'PlantLoop' => [],
      'AirLoop' => [],
      'Zone_Equipment' => [],
      'VRF_OD' => [],
      'DOASAirLoop' => [],
    }
    # Plant loops
    model.getPlantLoops.sort.each do |loop|
      system_report_map['PlantLoop'] << loop.handle
    end

    # Air loops
    model.getAirLoopHVACs.each do |air_loop|
      system_report_map['AirLoop'] << air_loop.handle
    end

    # Zone equipment
    model.getThermalZones.sort.each do |zone|
      zone.equipment.each do |zone_equipment|
        # @runner.registerInfo("zone_equipment:#{zone_equipment}")
        # @runner.registerInfo("zone_equipment_nil:#{zone_equipment.nil?}")
        # @runner.registerInfo("zone_equipment_handle:#{zone_equipment.handle}")
        system_report_map['Zone_Equipment'] << zone_equipment.handle
      end
    end

    # Outdoor VRF units (not in zone, not in loops)
    model.getAirConditionerVariableRefrigerantFlows.each do |airCon|
      system_report_map['VRF_OD'] << airCon.handle
    end

    # Air loop dedicated outdoor air systems
    model.getAirLoopHVACDedicatedOutdoorAirSystems.each do |airLoop|
      system_report_map['DOASAirLoop'] << airLoop.handle
    end
    return system_report_map
  end

  # Define the arguments that the user will input.
  def arguments(model)
    args = OpenStudio::Measure::OSArgumentVector.new

    # Make an argument for the standard
    standard_chs = OpenStudio::StringVector.new
    # standard_chs << '90.1-2004'
    standard_chs << '90.1-2007 BETA'
    standard_chs << '90.1-2007'
    standard_chs << '179D 90.1-2007'
    # 2019 Appendix-G migration: the new 179D PRM-2019 subclass (proposed
    # normalization), plus vanilla PRM-2019 for A/B comparison.
    standard_chs << '179D 90.1-2019'
    standard_chs << '90.1-PRM-2019'
    # 1.13.1 onward supports 90.1-2010
    if model.version > OpenStudio::VersionString.new('1.13.0')
      standard_chs << '90.1-2010 BETA'
      standard_chs << '90.1-2010'
    end
    standard_chs << '90.1-2013'
    # standard_chs << 'India ECBC 2007'
    standard = OpenStudio::Measure::OSArgument.makeChoiceArgument('standard', standard_chs, true)
    standard.setDisplayName('Standard')
    standard.setDefaultValue('179D 90.1-2019')
    args << standard

    # Make an argument for the building type
    building_type_chs = OpenStudio::StringVector.new
    building_type_chs << 'MidriseApartment'
    building_type_chs << 'HighriseApartment'
    building_type_chs << 'SecondarySchool'
    building_type_chs << 'PrimarySchool'
    building_type_chs << 'SmallOffice'
    building_type_chs << 'MediumOffice'
    building_type_chs << 'LargeOffice'
    building_type_chs << 'SmallHotel'
    building_type_chs << 'LargeHotel'
    building_type_chs << 'Warehouse'
    building_type_chs << 'RetailStandalone'
    building_type_chs << 'RetailStripmall'
    building_type_chs << 'QuickServiceRestaurant'
    building_type_chs << 'FullServiceRestaurant'
    building_type_chs << 'Hospital'
    building_type_chs << 'Outpatient'
    building_type = OpenStudio::Measure::OSArgument.makeChoiceArgument('building_type', building_type_chs, true)
    building_type.setDisplayName('Building Type.')
    building_type.setDefaultValue('SmallOffice')
    args << building_type

    # Make an argument for the climate zone
    climate_zone_chs = OpenStudio::StringVector.new
    climate_zone_chs << 'ASHRAE 169-2013-1A'
    climate_zone_chs << 'ASHRAE 169-2013-2A'
    climate_zone_chs << 'ASHRAE 169-2013-2B'
    climate_zone_chs << 'ASHRAE 169-2013-3A'
    climate_zone_chs << 'ASHRAE 169-2013-3B'
    climate_zone_chs << 'ASHRAE 169-2013-3C'
    climate_zone_chs << 'ASHRAE 169-2013-4A'
    climate_zone_chs << 'ASHRAE 169-2013-4B'
    climate_zone_chs << 'ASHRAE 169-2013-4C'
    climate_zone_chs << 'ASHRAE 169-2013-5A'
    climate_zone_chs << 'ASHRAE 169-2013-5B'
    climate_zone_chs << 'ASHRAE 169-2013-6A'
    climate_zone_chs << 'ASHRAE 169-2013-6B'
    climate_zone_chs << 'ASHRAE 169-2013-7A'
    climate_zone_chs << 'ASHRAE 169-2013-8A'
    # climate_zone_chs << 'India ECBC Composite'
    # climate_zone_chs << 'India ECBC Hot and Dry'
    # climate_zone_chs << 'India ECBC Warm and Humid'
    # climate_zone_chs << 'India ECBC Moderate'
    # climate_zone_chs << 'India ECBC Cold'
    climate_zone = OpenStudio::Measure::OSArgument.makeChoiceArgument('climate_zone', climate_zone_chs, true)
    climate_zone.setDisplayName('Climate Zone.')
    climate_zone.setDefaultValue('ASHRAE 169-2013-2A')
    args << climate_zone

    # Make an argument for the customization
    custom_chs = OpenStudio::StringVector.new
    custom_chs << 'Xcel Energy CO EDA'
    custom_chs << '*None*'
    custom_chs << '179d'
    custom = OpenStudio::Measure::OSArgument.makeChoiceArgument('custom', custom_chs, true)
    custom.setDisplayName('Customization')
    custom.setDescription('If selected, some of the standard process will be replaced by custom logic specific to particular programs.  If these do not apply to you, select None.')
    custom.setDefaultValue('*None*')
    args << custom

    # Make an argument for enabling debug messages
    debug = OpenStudio::Measure::OSArgument.makeBoolArgument('debug', true)
    debug.setDisplayName('Show debug messages?')
    debug.setDefaultValue(false)
    args << debug

    # make an argument for use_upstream_args
    use_upstream_args = OpenStudio::Measure::OSArgument.makeBoolArgument('use_upstream_args', true)
    use_upstream_args.setDisplayName('Use Upstream Argument Values')
    use_upstream_args.setDescription('When true this will look for arguments or registerValues in upstream measures that match arguments from this measure, and will use the value from the upstream measure in place of what is entered for this measure.')
    use_upstream_args.setDefaultValue(true)
    args << use_upstream_args
    return args
  end

  # Define what happens when the measure is run.
  def run(model, runner, user_arguments)
    super(model, runner, user_arguments)

    if !ENV['SAVE_INTERMEDIATE'].nil?
      model.save('9.1.before_179d_gem_baseline_hvac_control.osm')
    end

    # Use the built-in error checking
    return false unless runner.validateUserArguments(arguments(model), user_arguments)

    # load input arguments
    args = runner.getArgumentValues(arguments(model), user_arguments)
    # Keys are symbols, turn them into strings
    args.transform_keys!(&:to_s)
    debug = args['debug']
    if debug
      # save model before measure application for debugging purpose
      ft = OpenStudio::EnergyPlus::ForwardTranslator.new
      workspace = ft.translateModel(model)
      model.save('initial_upgraded_model.osm')
      workspace.save('initial_upgraded_workspace.idf')
    end
    # lookup and replace argument values from upstream measures
    additional_arg_names = ['reported_climate_zone']
    additional_args = {}
    if args['use_upstream_args'] == true
      args.each_key do |arg|
        next if arg == 'use_upstream_args' # this argument should not be changed

        value_from_osw = runner.getPastStepValuesForName(arg)
        value_from_osw = value_from_osw.collect { |k, v| { measure_name: k, value: v } }.first if !value_from_osw.empty?
        next if value_from_osw.empty?

        runner.registerInfo("Replacing argument named #{arg} from current measure with a value of #{value_from_osw[:value]} from #{value_from_osw[:measure_name]}.")
        new_val = value_from_osw[:value]
        # TODO: - make code to handle non strings more robust. check_upstream_measure_for_arg could pass back the argument type
        args[arg] = case arg
                    when 'total_bldg_floor_area'
                      new_val.to_f
                    when 'num_stories_above_grade'
                      new_val.to_f
                    when 'zipcode'
                      new_val.to_i
                    else
                      new_val
                    end
      end
      additional_arg_names.each do |arg|
        next if arg == 'use_upstream_args' # this argument should not be changed

        value_from_osw = runner.getPastStepValuesForName(arg)
        value_from_osw = value_from_osw.collect { |k, v| { measure_name: k, value: v } }.first if !value_from_osw.empty?
        next if value_from_osw.empty?

        runner.registerInfo("Replacing argument named #{arg} from current measure with a value of #{value_from_osw[:value]} from #{value_from_osw[:measure_name]}.")
        new_val = value_from_osw[:value]
        additional_args[arg] = new_val
      end
    end

    # update input arguments
    # TODO: eventually just use the same std for everything
    primary_bldg_type = Standard.build('179D 90.1-2007').model_get_primary_building_type(model, remap_office: false, remap_retail: false)
    runner.registerInfo("primary_bldg_type (#{primary_bldg_type}) GetBuildingType179D.")
    building_type = Standard.build('179D 90.1-2007').model_get_primary_building_type(model, remap_office: true, remap_retail: true)
    runner.registerInfo("building_type (#{building_type}) GetBuildingType179D.")
    runner.registerValue('applied_building_type_prm_hvac', building_type)
    climate_zone = additional_args.key?('reported_climate_zone') ? additional_args['reported_climate_zone'] : args['climate_zone']
    runner.registerValue('applied_climate_zone_prm_hvac', climate_zone)
    custom = args['custom']

    custom = nil if custom == '*None*'

    # Strip BETA from the standard choice
    standard = args['standard'] # fixed as 2007 for PRM appendix G
    if standard.include?(' BETA')
      runner.registerWarning("You have chosen #{standard}, which is still under development.  It should generally be correct, but has not been heavily tested.  Please review the output messages closely.")
      standard = standard.gsub(' BETA', '')
    end

    # List of unsupported things
    us = []
    us << 'Lighting controls (occ/vac sensors) are assumed to already be present in proposed lighting schedules, and will not be added or removed'
    us << 'Exterior lighting in the baseline model is left as found in proposed'
    us << 'Optimal start of HVAC systems is not supported'
    us << 'Skylights are not added to model, but existing skylights are scaled per Appendix G skylight-to-roof areas'
    us << 'Changing baseline glazing types based on WWR and orientation' if standard == '90.1-2004'
    us << 'No fan power allowances for MERV filters or ducted supply/return present in proposed model HVAC'
    us << 'Laboratory-specific ventilation is not handled'
    us << 'Kitchen ventilation is not handled; exhaust fans left as found in proposed'
    us << 'Commercial refrigeration equipment is left as found in proposed'
    us << 'Transformers are not added to the baseline model'
    us << 'System types 11 (for data centers) and 12/13 (for public assembly buildings)' if standard == '90.1-2013'
    us << 'Zone humidity control present in the proposed model HVAC systems is not added to baseline HVAC'

    # Report out to users
    OpenStudio.logFree(OpenStudio::Info, 'openstudio.standards.Model', '*** Currently unsupported ***')
    us.each do |msg|
      OpenStudio.logFree(OpenStudio::Info, 'openstudio.standards.Model', msg)
    end

    # List of known issues or limitations
    # ! ACM schedule source https://nrel.sharepoint.com/:x:/s/179d/EUyFXLmyjb1IleYfUBV0icMBAFL9-4ZkXf-k7RIdg--aKg?e=Bl1lFw
    issues = []
    issues << 'Some control and efficiency determinations do not scale capacities/flow rates down to reflect zone multipliers'
    issues << 'Daylighting control illuminance setpoint does not vary based on space type'
    issues << 'Daylighting area calcs do not include windows in non-vertical walls'
    issues << 'Daylighting area calcs do not include skylights in non-horizontal roofs'

    # Report out to users
    OpenStudio.logFree(OpenStudio::Info, 'openstudio.standards.Model', '*** Known issues ***')
    issues.each do |msg|
      OpenStudio.logFree(OpenStudio::Info, 'openstudio.standards.Model', msg)
    end

    # Make a directory to save the resulting models for debugging
    build_dir = "#{Dir.pwd}/output"

    FileUtils.mkdir_p(build_dir)

    initital_system_report_map = system_report(model)

    # load standards
    if debug
      puts("### DEBUGGING: standards location = #{Standard.const_source_location(:STANDARDS_LIST)}")
      puts("### DEBUGGING: standard template = #{standard}")
    end
    std = Standard.build(standard)

    # convert to prm baseline model
    pp 'model 179d methods:'
    [
      'model_create_prm_any_baseline_building',
      'model_prm_baseline_system_number'
    ].each do |method_str|
      std_method_source_location(std, method_str, debug)
    end

    pp 'model_create_prm_any_baseline_building detail methods:'
    [
      'plant_loop_apply_prm_baseline_chilled_water_pumping_type',
      'handle_user_input_data', # dumped
      'model_identify_return_air_type',
      'space_conditioning_category',
      'model_get_district_heating_zones',
      'get_fan_schedule_for_each_zone',
      'model_identify_non_mechanically_cooled_systems',
      'model_get_fan_power_breakdown',
      'model_mark_zone_dcv_existence',
      'model_add_dcv_user_exception_properties',
      'model_add_dcv_requirement_properties',
      'model_add_apxg_dcv_properties',
      'model_raise_user_model_dcv_errors',
      'model_prm_baseline_system_type',
      'model_add_prm_baseline_system',
      'model_determine_baseline_return_air_type',
      'thermal_zone_apply_prm_baseline_supply_temperatures',
      'air_loop_hvac_apply_prm_sizing_temperatures',
      'model_apply_prm_baseline_sizing_schedule',
      'model_apply_prm_sizing_parameters',
      'air_loop_hvac_apply_prm_baseline_controls',
      'plant_loop_apply_prm_baseline_temperatures',
      'air_loop_hvac_apply_minimum_vav_damper_positions',
      'model_apply_multizone_vav_outdoor_air_sizing',
      'air_loop_hvac_apply_prm_baseline_fan_power',
      'zone_hvac_component_apply_prm_baseline_fan_power',
      'plant_loop_apply_prm_number_of_boilers',
      'plant_loop_apply_prm_number_of_chillers',
      'plant_loop_apply_prm_number_of_cooling_towers',
      'plant_loop_apply_prm_baseline_pump_power',
      'plant_loop_apply_prm_baseline_pumping_type',
      'model_apply_hvac_efficiency_standard',
      'air_loop_hvac_apply_standard_controls',
      'model_set_baseline_demand_control_ventilation',
      'model_refine_size_dependent_values',
      'model_temp_fix_ems_references',
      'model_remove_unused_resource_objects',
      'model_add_reporting_tolerances',
      'model_prm_baseline_system_groups',
      'model_prm_baseline_system_number',
      'air_loop_hvac_enable_unoccupied_fan_shutoff',
      'air_loop_hvac_unoccupied_threshold',
      'space_type_apply_internal_loads',
      'model_create_multizone_fan_schedule',
      'zone_hvac_get_fan_object',
      'zone_hvac_component_occupancy_ventilation_control',
      'model_apply_hvac_efficiency_standard',
      'zone_hvac_component_apply_standard_controls',
      'zone_hvac_component_occupancy_ventilation_control',
      'model_apply_prm_baseline_window_to_wall_ratio',
      'model_does_require_wwr_adjustment?'
      # "model_get_fan_power_breakdown",
      # "model_prm_baseline_system_change_fuel_type",
      # "model_create_prm_any_baseline_building",
      # "model_add_prm_baseline_system",
      # "plant_loop_apply_prm_number_of_cooling_towers",
      # "plant_loop_apply_prm_number_of_chillers",
      # "run_all_orientations",
      # "handle_user_input_data"
    ].each do |method_str|
      std_method_source_location(std, method_str, debug)
    end
    baseline_179d = false
    # unmet_load_hours_check = building_type == 'PrimarySchool'
    unmet_load_hours_check = false
    puts "standard=#{standard}, building_type=#{building_type}, climate_zone=#{climate_zone}, custom=#{custom}, build_dir=#{build_dir}, debug=#{debug}, baseline_179d=#{baseline_179d}, unmet_load_hours_check=#{unmet_load_hours_check}"
    # building_type -> hvac_building_type for the 2019 baseline_system_type tag
    # inference (mirrors the baseline measure; prm_baseline_hvac has no
    # 'All others' row -- dry-run L11).
    hvac_bldg_type =
      case building_type
      when 'MidriseApartment', 'HighriseApartment', 'SmallHotel', 'LargeHotel' then 'residential'
      when 'RetailStandalone', 'RetailStripmall' then 'retail'
      when 'Warehouse' then 'heated-only storage'
      when 'Hospital' then 'hospital'
      else 'other nonresidential'
      end
    # Capture per-air-loop Sizing:System.DesignOutdoorAirFlowRate before the
    # PRM call. Inside model_create_prm_any_baseline_building, when DCV is
    # enabled, air_loop_hvac_enable_demand_control_ventilation zeros
    # Controller:OutdoorAir.minimumOutdoorAirFlowRate and then
    # consistent_outdoor_airflow_rate copies that 0 into Sizing:System. The
    # baseline track has an `if baseline_179d` block (Model.rb:793-842) that
    # scales it back up to the captured proposed value; the proposed track has
    # no equivalent, leaving the AHU coils sized for zero OA. Restore is gated
    # below on post-call DCV state so PSZ-HP (no DCV) keeps its correct value.
    # Capture mirrors get_minimum_and_design_outdoor_airflow_rates (Model.rb:1217)
    # which notes that sizing_system.autosizedDesignOutdoorAirFlowRate is
    # unreliable; use controller_oa.autosizedMinimumOutdoorAirFlowRate as the
    # autosize fallback.
    proposed_sizing_oa_m3s = {}
    if standard == '179D 90.1-2007'
      model.getAirLoopHVACs.each do |air_loop|
        next if air_loop.airLoopHVACOutdoorAirSystem.empty?

        sizing_system = air_loop.sizingSystem
        controller_oa = air_loop.airLoopHVACOutdoorAirSystem.get.getControllerOutdoorAir
        value =
          if sizing_system.designOutdoorAirFlowRate.is_initialized
            sizing_system.designOutdoorAirFlowRate.get
          elsif controller_oa.autosizedMinimumOutdoorAirFlowRate.is_initialized
            controller_oa.autosizedMinimumOutdoorAirFlowRate.get
          end
        proposed_sizing_oa_m3s[air_loop.handle.to_s] = value if value && value > 0.0
      end
    end

    if standard == '179D 90.1-2007'
      std.model_create_prm_baseline_building(model, building_type, climate_zone, custom, build_dir, debug, baseline_179d, unmet_load_hours_check)
    elsif ['179D 90.1-2019', '90.1-PRM-2019'].include?(standard)
      # 2019 proposed-normalization path (master plan §5 Phase 1 / PR-07).
      # Keeps real HVAC + LPD + as-built efficiency; applies neutral baseline-grade
      # knobs to the retained HVAC (Gotcha #2 infer+tag up front). The 2007-only
      # OA/ventilation restore blocks above/below stay dormant for 2019 (re-ported
      # reactively in Phase 3 / PR-14/15 if Phase-2 triage confirms they're needed).
      std.model_create_179d_proposed_normalization(model, climate_zone, hvac_bldg_type, build_dir, debug)
    else
      std.model_create_prm_baseline_building(model, building_type, climate_zone, custom, build_dir, debug)
    end

    # Restore Sizing:System.DesignOutdoorAirFlowRate to the captured pre-PRM
    # value only on air loops where DCV got enabled by the PRM call. PSZ-HP
    # systems don't trigger DCV so they skip the restore and keep the correct
    # Sizing:System value that consistent_outdoor_airflow_rate left in place.
    if standard == '179D 90.1-2007'
      model.getAirLoopHVACs.each do |air_loop|
        target = proposed_sizing_oa_m3s[air_loop.handle.to_s]
        next if target.nil?
        next if air_loop.airLoopHVACOutdoorAirSystem.empty?

        controller_oa = air_loop.airLoopHVACOutdoorAirSystem.get.getControllerOutdoorAir
        controller_mv = controller_oa.controllerMechanicalVentilation
        next unless controller_mv.demandControlledVentilation

        sizing_system = air_loop.sizingSystem
        current =
          if sizing_system.designOutdoorAirFlowRate.is_initialized
            sizing_system.designOutdoorAirFlowRate.get
          else
            0.0
          end
        next if (current - target).abs < 1e-6

        sizing_system.setDesignOutdoorAirFlowRate(target)
        runner.registerInfo("PROPOSED OA sizing fix: restored Sizing:System.DesignOutdoorAirFlowRate on '#{air_loop.nameString}' from #{current.round(3)} to #{target.round(3)} m^3/s (DCV enabled).")
      end
    end

    # Mirror baseline-track addition of ZoneVentilationDesignFlowRate for
    # heated-only zones (UnitHeater) so the proposed track models the ASHRAE
    # 62.1 ventilation requirement for spaces where the HVAC system provides
    # no OA itself. Baseline does this at Model.rb:703 inside
    # model_create_prm_baseline_building but only when baseline_179d=true and
    # only as part of HVAC rebuild. For the proposed track we apply the same
    # helper to existing UnitHeater zones with no air-loop OA. Without this,
    # Warehouse storage zones (Bulk/Fine, served by UnitHeater) modeled zero
    # ventilation on proposed while baseline had the per-area ventilation,
    # producing a large baseline-vs-proposed Outdoor Airflow Design Rate
    # mismatch.
    if standard == '179D 90.1-2007'
      heated_only_zones = model.getThermalZones.select do |zone|
        next false unless zone.airLoopHVACs.empty?

        zone.equipment.any? { |eq| eq.to_ZoneHVACUnitHeater.is_initialized }
      end
      unless heated_only_zones.empty?
        runner.registerInfo("PROPOSED ventilation fix: adding ZoneVentilationDesignFlowRate to #{heated_only_zones.size} heated-only zone(s): #{heated_only_zones.map(&:nameString).join(', ')}")
        std.model_add_equivalent_zone_ventilation_for_heated_only_zones_with_dsoa(model, heated_only_zones, ventilation_type: 'Exhaust', ensure_ddy_infiltration: true)
      end
    end

    # adding additional properties
    building = model.getBuilding
    if building.additionalProperties.hasFeature('prm_baseline_system_type')
      prm_baseline_system_type_str = building.additionalProperties.getFeatureAsString('prm_baseline_system_type').get
      runner.registerValue('prm_baseline_system_type', prm_baseline_system_type_str)

    end
    modified_system_report_map = system_report(model)

    # verify if system report map is indentical after and before
    initital_system_report_map.each do |system_type, handle_list|
      check_origin = handle_list.all? { |x| modified_system_report_map[system_type].include? x }
      diff = handle_list.reject { |x| modified_system_report_map[system_type].include? x }
      runner.registerValue("prm_hvac_#{system_type}", check_origin)
      runner.registerInfo("prm_hvac_#{system_type}: #{check_origin}")
      runner.registerInfo("prm_hvac_#{system_type}_initial_handle: #{handle_list.join('***')}")
      runner.registerInfo("prm_hvac_#{system_type}_modiffied_handle: #{handle_list.join('***')}")
      runner.registerInfo("prm_hvac_#{system_type}_diff: #{diff.join('***')}")
    end
    if debug
      # save model after measure application for debugging purpose
      workspace = ft.translateModel(model)
      model.save('baseline_hvac_control_model.osm')
      workspace.save('baseline_hvac_control_workspace.idf')
    end

    if !ENV['SAVE_INTERMEDIATE'].nil?
      model.save('9.2.after_179d_gem_baseline_hvac_control.osm')
    end

    true
  end
end

# this allows the measure to be use by the application
HVACControl179DBaseline.new.registerWithApplication
