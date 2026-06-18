# ComStock(TM), Copyright (c) 2025 Alliance for Sustainable Energy, LLC. All rights reserved.
# See top level LICENSE.txt file for license terms.
# *******************************************************************************
# OpenStudio(R), Copyright (c) Alliance for Sustainable Energy, LLC.
# See also https://openstudio.net/license
# *******************************************************************************

# start the measure
class CreateTypicalBuildingFromModelComStock < OpenStudio::Measure::ModelMeasure
  require 'openstudio-standards'
  require 'pathname'

  require_relative 'resources/os_lib_model_generation_typical'

  (Pathname.new(__dir__) / 'resources/179d_standards').glob('*.rb').sort_by { |p| p.basename.to_s.size }.each { |f| require f.sub_ext('').to_s } unless defined?(ACM179dASHRAE9012007)

  # resource file modules
  include OsLib_ModelGeneration_Typical

  DEFAULT_TEMPLATE_179D = '179D 90.1-2007'.freeze unless defined?(DEFAULT_TEMPLATE_179D)

  # human readable name
  def name
    return 'Create Typical Building from Model'
  end

  # human readable description
  def description
    return 'Takes a model with space and stub space types, and assigns constructions, schedules, internal loads, hvac, and other loads such as exterior lights and service water heating.'
  end

  # human readable description of modeling approach
  def modeler_description
    return 'It is important that the template argument chosen for this measure is in line with the building types for the stub space types of the model passed in.'
  end

  # define the arguments that the user will input
  def arguments(_model)
    args = OpenStudio::Measure::OSArgumentVector.new

    templates = [DEFAULT_TEMPLATE_179D] + Array(OpenstudioStandards::CreateTypical.get_templates)

    # Phase 1 (2019 migration): build the proposed model on the vanilla 90.1-2019
    # *prototype* (it ships the building data; PRM-2019 cannot build -- master plan
    # Gotcha #1). Because this is NOT the DEFAULT_TEMPLATE_179D sentinel, is_179d
    # stays false below, so the 179D ACM availability schedules (re-enabled in
    # Phase 3b) and the fork infiltration block are skipped and infiltration is
    # added natively. See instructions/PRM-2019-MIGRATION-MASTER-PLAN.md §5 Phase 1.
    default_string = '90.1-2019'
    # see if building name contains any template values
    # templates.each do |template_string|
    #   if model.getBuilding.name.to_s.include?(template_string)
    #     default_string = template_string
    #     break
    #   end
    # end

    # Make argument for template
    template = OpenStudio::Measure::OSArgument.makeChoiceArgument('template', templates, true)
    template.setDisplayName('Target Standard')
    template.setDefaultValue(default_string)
    args << template

    # Make argument for system type
    hvac_chs = OpenStudio::StringVector.new
    hvac_chs << 'Inferred'
    hvac_chs << 'Ideal Air Loads'
    hvac_chs << 'Baseboard electric'
    hvac_chs << 'Baseboard gas boiler'
    hvac_chs << 'Baseboard central air source heat pump'
    hvac_chs << 'Baseboard district hot water'
    hvac_chs << 'Direct evap coolers with baseboard electric'
    hvac_chs << 'Direct evap coolers with baseboard gas boiler'
    hvac_chs << 'Direct evap coolers with baseboard central air source heat pump'
    hvac_chs << 'Direct evap coolers with baseboard district hot water'
    hvac_chs << 'Direct evap coolers with forced air furnace'
    hvac_chs << 'Direct evap coolers with gas unit heaters'
    hvac_chs << 'Direct evap coolers with no heat'
    hvac_chs << 'DOAS with fan coil chiller with boiler'
    hvac_chs << 'DOAS with fan coil chiller with central air source heat pump'
    hvac_chs << 'DOAS with fan coil chiller with district hot water'
    hvac_chs << 'DOAS with fan coil chiller with baseboard electric'
    hvac_chs << 'DOAS with fan coil chiller with gas unit heaters'
    hvac_chs << 'DOAS with fan coil chiller with no heat'
    hvac_chs << 'DOAS with fan coil air-cooled chiller with boiler'
    hvac_chs << 'DOAS with fan coil air-cooled chiller with central air source heat pump'
    hvac_chs << 'DOAS with fan coil air-cooled chiller with district hot water'
    hvac_chs << 'DOAS with fan coil air-cooled chiller with baseboard electric'
    hvac_chs << 'DOAS with fan coil air-cooled chiller with gas unit heaters'
    hvac_chs << 'DOAS with fan coil air-cooled chiller with no heat'
    hvac_chs << 'DOAS with fan coil district chilled water with boiler'
    hvac_chs << 'DOAS with fan coil district chilled water with central air source heat pump'
    hvac_chs << 'DOAS with fan coil district chilled water with district hot water'
    hvac_chs << 'DOAS with fan coil district chilled water with baseboard electric'
    hvac_chs << 'DOAS with fan coil district chilled water with gas unit heaters'
    hvac_chs << 'DOAS with fan coil district chilled water with no heat'
    hvac_chs << 'DOAS with VRF'
    hvac_chs << 'DOAS with water source heat pumps fluid cooler with boiler'
    hvac_chs << 'DOAS with water source heat pumps cooling tower with boiler'
    hvac_chs << 'DOAS with water source heat pumps with ground source heat pump'
    hvac_chs << 'DOAS with water source heat pumps district chilled water with district hot water'
    hvac_chs << 'Fan coil chiller with boiler'
    hvac_chs << 'Fan coil chiller with central air source heat pump'
    hvac_chs << 'Fan coil chiller with district hot water'
    hvac_chs << 'Fan coil chiller with baseboard electric'
    hvac_chs << 'Fan coil chiller with gas unit heaters'
    hvac_chs << 'Fan coil chiller with no heat'
    hvac_chs << 'Fan coil air-cooled chiller with boiler'
    hvac_chs << 'Fan coil air-cooled chiller with central air source heat pump'
    hvac_chs << 'Fan coil air-cooled chiller with district hot water'
    hvac_chs << 'Fan coil air-cooled chiller with baseboard electric'
    hvac_chs << 'Fan coil air-cooled chiller with gas unit heaters'
    hvac_chs << 'Fan coil air-cooled chiller with no heat'
    hvac_chs << 'Fan coil district chilled water with boiler'
    hvac_chs << 'Fan coil district chilled water with central air source heat pump'
    hvac_chs << 'Fan coil district chilled water with district hot water'
    hvac_chs << 'Fan coil district chilled water with baseboard electric'
    hvac_chs << 'Fan coil district chilled water with gas unit heaters'
    hvac_chs << 'Fan coil district chilled water with no heat'
    hvac_chs << 'Forced air furnace'
    hvac_chs << 'Gas unit heaters'
    hvac_chs << 'PTAC with baseboard electric'
    hvac_chs << 'PTAC with baseboard gas boiler'
    hvac_chs << 'PTAC with baseboard district hot water'
    hvac_chs << 'PTAC with gas unit heaters'
    hvac_chs << 'PTAC with electric coil'
    hvac_chs << 'PTAC with gas coil'
    hvac_chs << 'PTAC with gas boiler'
    hvac_chs << 'PTAC with central air source heat pump'
    hvac_chs << 'PTAC with district hot water'
    hvac_chs << 'PTAC with no heat'
    hvac_chs << 'PTHP'
    hvac_chs << 'PSZ-AC with baseboard electric'
    hvac_chs << 'PSZ-AC with baseboard gas boiler'
    hvac_chs << 'PSZ-AC with baseboard district hot water'
    hvac_chs << 'PSZ-AC with gas unit heaters'
    hvac_chs << 'PSZ-AC with electric coil'
    hvac_chs << 'PSZ-AC with gas coil'
    hvac_chs << 'PSZ-AC with gas boiler'
    hvac_chs << 'PSZ-AC with central air source heat pump'
    hvac_chs << 'PSZ-AC with district hot water'
    hvac_chs << 'PSZ-AC with no heat'
    hvac_chs << 'PSZ-AC district chilled water with baseboard electric'
    hvac_chs << 'PSZ-AC district chilled water with baseboard gas boiler'
    hvac_chs << 'PSZ-AC district chilled water with baseboard district hot water'
    hvac_chs << 'PSZ-AC district chilled water with gas unit heaters'
    hvac_chs << 'PSZ-AC district chilled water with electric coil'
    hvac_chs << 'PSZ-AC district chilled water with gas coil'
    hvac_chs << 'PSZ-AC district chilled water with gas boiler'
    hvac_chs << 'PSZ-AC district chilled water with central air source heat pump'
    hvac_chs << 'PSZ-AC district chilled water with district hot water'
    hvac_chs << 'PSZ-AC district chilled water with no heat'
    hvac_chs << 'PSZ-HP'
    hvac_chs << 'PVAV with gas boiler reheat'
    hvac_chs << 'PVAV with central air source heat pump reheat'
    hvac_chs << 'PVAV with district hot water reheat'
    hvac_chs << 'PVAV with PFP boxes'
    hvac_chs << 'PVAV with gas heat with electric reheat'
    hvac_chs << 'PVAV with gas coil heat with electric reheat'
    hvac_chs << 'PVAV with gas boiler heat with electric reheat'
    hvac_chs << 'Residential AC with baseboard electric'
    hvac_chs << 'Residential AC with baseboard gas boiler'
    hvac_chs << 'Residential AC with baseboard central air source heat pump'
    hvac_chs << 'Residential AC with baseboard district hot water'
    hvac_chs << 'Residential AC with residential forced air furnace'
    hvac_chs << 'Residential AC with no heat'
    hvac_chs << 'Residential heat pump'
    hvac_chs << 'Residential heat pump with no cooling'
    hvac_chs << 'Residential forced air furnace'
    hvac_chs << 'VAV chiller with gas boiler reheat'
    hvac_chs << 'VAV chiller with central air source heat pump reheat'
    hvac_chs << 'VAV chiller with district hot water reheat'
    hvac_chs << 'VAV chiller with PFP boxes'
    hvac_chs << 'VAV chiller with gas coil reheat'
    hvac_chs << 'VAV chiller with no reheat with baseboard electric'
    hvac_chs << 'VAV chiller with no reheat with gas unit heaters'
    hvac_chs << 'VAV chiller with no reheat with zone heat pump'
    hvac_chs << 'VAV air-cooled chiller with gas boiler reheat'
    hvac_chs << 'VAV air-cooled chiller with central air source heat pump reheat'
    hvac_chs << 'VAV air-cooled chiller with district hot water reheat'
    hvac_chs << 'VAV air-cooled chiller with PFP boxes'
    hvac_chs << 'VAV air-cooled chiller with gas coil reheat'
    hvac_chs << 'VAV air-cooled chiller with no reheat with baseboard electric'
    hvac_chs << 'VAV air-cooled chiller with no reheat with gas unit heaters'
    hvac_chs << 'VAV air-cooled chiller with no reheat with zone heat pump'
    hvac_chs << 'VAV district chilled water with gas boiler reheat'
    hvac_chs << 'VAV district chilled water with central air source heat pump reheat'
    hvac_chs << 'VAV district chilled water with district hot water reheat'
    hvac_chs << 'VAV district chilled water with PFP boxes'
    hvac_chs << 'VAV district chilled water with gas coil reheat'
    hvac_chs << 'VAV district chilled water with no reheat with baseboard electric'
    hvac_chs << 'VAV district chilled water with no reheat with gas unit heaters'
    hvac_chs << 'VAV district chilled water with no reheat with zone heat pump'
    hvac_chs << 'VRF'
    hvac_chs << 'Water source heat pumps fluid cooler with boiler'
    hvac_chs << 'Water source heat pumps cooling tower with boiler'
    hvac_chs << 'Water source heat pumps with ground source heat pump'
    hvac_chs << 'Water source heat pumps district chilled water with district hot water'
    hvac_chs << 'Window AC with baseboard electric'
    hvac_chs << 'Window AC with baseboard gas boiler'
    hvac_chs << 'Window AC with baseboard central air source heat pump'
    hvac_chs << 'Window AC with baseboard district hot water'
    hvac_chs << 'Window AC with forced air furnace'
    hvac_chs << 'Window AC with unit heaters'
    hvac_chs << 'Window AC with no heat'

    system_type = OpenStudio::Measure::OSArgument.makeChoiceArgument('system_type', hvac_chs, true)
    system_type.setDisplayName('HVAC System Type')
    system_type.setDefaultValue('Inferred')
    args << system_type

    # Make argument for HVAC delivery type
    hvac_type_chs = OpenStudio::StringVector.new
    hvac_type_chs << 'Forced Air'
    hvac_type_chs << 'Hydronic'
    hvac_type_chs << 'Inferred'
    hvac_delivery_type = OpenStudio::Measure::OSArgument.makeChoiceArgument('hvac_delivery_type', hvac_type_chs, true)
    hvac_delivery_type.setDisplayName('HVAC System Delivery Type')
    hvac_delivery_type.setDescription('How the HVAC system delivers heating or cooling to the zone.')
    hvac_delivery_type.setDefaultValue('Forced Air')
    args << hvac_delivery_type

    # Make argument for HVAC heating source
    htg_src_chs = OpenStudio::StringVector.new
    htg_src_chs << 'Electricity'
    htg_src_chs << 'NaturalGas'
    htg_src_chs << 'DistrictHeating'
    htg_src_chs << 'DistrictAmbient'
    htg_src_chs << 'Inferred'
    htg_src = OpenStudio::Measure::OSArgument.makeChoiceArgument('htg_src', htg_src_chs, true)
    htg_src.setDisplayName('HVAC Heating Source')
    htg_src.setDescription('The primary source of heating used by HVAC systems in the model.')
    htg_src.setDefaultValue('NaturalGas')
    args << htg_src

    # Make argument for HVAC cooling source
    clg_src_chs = OpenStudio::StringVector.new
    clg_src_chs << 'Electricity'
    clg_src_chs << 'DistrictCooling'
    clg_src_chs << 'DistrictAmbient'
    clg_src_chs << 'Inferred'
    clg_src = OpenStudio::Measure::OSArgument.makeChoiceArgument('clg_src', clg_src_chs, true)
    clg_src.setDisplayName('HVAC Cooling Source')
    clg_src.setDescription('The primary source of cooling used by HVAC systems in the model.')
    clg_src.setDefaultValue('Electricity')
    args << clg_src

    swh_src_chs = OpenStudio::StringVector.new
    swh_src_chs << 'Inferred'
    swh_src_chs << 'NaturalGas'
    swh_src_chs << 'Electricity'
    swh_src_chs << 'HeatPump'
    swh_src = OpenStudio::Measure::OSArgument.makeChoiceArgument('swh_src', swh_src_chs, true)
    swh_src.setDisplayName('Service Water Heating Source')
    swh_src.setDescription('The primary source of heating used by SWH systems in the model.')
    swh_src.setDefaultValue('Inferred')
    args << swh_src

    #     # make argument for fuel type (not used yet)
    #     fuel_type_cooking = OpenStudio::Measure::OSArgument::makeChoiceArgument("fuel_type_cooking", fuel_choices,true)
    #     fuel_type_cooking.setDisplayName("Fuel Type for Cooking Loads")
    #     fuel_type_cooking.setDefaultValue("Gas")
    #     args << fuel_type_cooking
    #
    #     # make argument for fuel type (not used yet)
    #     fuel_type_laundry = OpenStudio::Measure::OSArgument::makeChoiceArgument("fuel_type_laundry", fuel_choices,true)
    #     fuel_type_laundry.setDisplayName("Fuel Type for Laundry Dryer Loads")
    #     fuel_type_laundry.setDefaultValue("Electric")
    #     args << fuel_type_laundry

    # make argument for kitchen makeup
    kitchen_makeup_choices = OpenStudio::StringVector.new
    kitchen_makeup_choices << 'None'
    kitchen_makeup_choices << 'Largest Zone'
    kitchen_makeup_choices << 'Adjacent'
    kitchen_makeup = OpenStudio::Measure::OSArgument.makeChoiceArgument('kitchen_makeup', kitchen_makeup_choices, true)
    kitchen_makeup.setDisplayName('Kitchen Exhaust MakeUp Air Calculation Method')
    kitchen_makeup.setDescription('Determine logic to identify dining or cafe zones to provide makeup air to kitchen exhaust.')
    kitchen_makeup.setDefaultValue('Adjacent')
    args << kitchen_makeup

    # make argument for exterior_lighting_zone
    exterior_lighting_zone_choices = OpenStudio::StringVector.new
    exterior_lighting_zone_choices << '0 - Undeveloped Areas Parks'
    exterior_lighting_zone_choices << '1 - Developed Areas Parks'
    exterior_lighting_zone_choices << '2 - Neighborhood'
    exterior_lighting_zone_choices << '3 - All Other Areas'
    exterior_lighting_zone_choices << '4 - High Activity'
    exterior_lighting_zone = OpenStudio::Measure::OSArgument.makeChoiceArgument('exterior_lighting_zone', exterior_lighting_zone_choices, true)
    exterior_lighting_zone.setDisplayName('Exterior Lighting Zone')
    exterior_lighting_zone.setDescription('Identify the Exterior Lighting Zone for the Building Site.')
    exterior_lighting_zone.setDefaultValue('3 - All Other Areas')
    args << exterior_lighting_zone

    # make an argument for add_constructions
    add_constructions = OpenStudio::Measure::OSArgument.makeBoolArgument('add_constructions', true)
    add_constructions.setDisplayName('Add Constructions to Model')
    add_constructions.setDescription('Construction Set will be applied to entire building')
    add_constructions.setDefaultValue(true)
    args << add_constructions

    # make argument for wall_construction_type
    wall_construction_type_choices = OpenStudio::StringVector.new
    wall_construction_type_choices << 'Mass'
    wall_construction_type_choices << 'Metal Building'
    wall_construction_type_choices << 'SteelFramed'
    wall_construction_type_choices << 'WoodFramed'
    wall_construction_type_choices << 'Inferred'
    wall_construction_type = OpenStudio::Measure::OSArgument.makeChoiceArgument('wall_construction_type', wall_construction_type_choices, true)
    wall_construction_type.setDisplayName('Wall Construction Type')
    wall_construction_type.setDescription('Identify Exterior Wall Construction Type to be applied to entire building')
    wall_construction_type.setDefaultValue('Inferred')
    args << wall_construction_type

    # make an argument for add_space_type_loads
    add_space_type_loads = OpenStudio::Measure::OSArgument.makeBoolArgument('add_space_type_loads', true)
    add_space_type_loads.setDisplayName('Add Space Type Loads to Model')
    add_space_type_loads.setDescription('Populate existing space types in model with internal loads.')
    add_space_type_loads.setDefaultValue(true)
    args << add_space_type_loads

    # make an argument for add_elevators
    add_elevators = OpenStudio::Measure::OSArgument.makeBoolArgument('add_elevators', true)
    add_elevators.setDisplayName('Add Elevators to Model')
    add_elevators.setDescription('Elevators will be add directly to space in model vs. being applied to a space type.')
    add_elevators.setDefaultValue(true)
    args << add_elevators

    # make an argument for add_internal_mass
    add_internal_mass = OpenStudio::Measure::OSArgument.makeBoolArgument('add_internal_mass', true)
    add_internal_mass.setDisplayName('Add Internal Mass to Model')
    add_internal_mass.setDescription('Adds internal mass to each space.')
    add_internal_mass.setDefaultValue(true)
    args << add_internal_mass

    # make an argument for add_exterior_lights
    add_exterior_lights = OpenStudio::Measure::OSArgument.makeBoolArgument('add_exterior_lights', true)
    add_exterior_lights.setDisplayName('Add Exterior Lights to Model')
    add_exterior_lights.setDescription('Multiple exterior lights objects will be added for different classes of lighting such as parking and facade.')
    add_exterior_lights.setDefaultValue(true)
    args << add_exterior_lights

    # make an argument for onsite_parking_fraction
    onsite_parking_fraction = OpenStudio::Measure::OSArgument.makeDoubleArgument('onsite_parking_fraction', true)
    onsite_parking_fraction.setDisplayName('Onsite Parking Fraction')
    onsite_parking_fraction.setDescription('If set to 0 no exterior lighting for parking will be added')
    onsite_parking_fraction.setDefaultValue(1.0)
    onsite_parking_fraction.setMinValue(0.0)
    onsite_parking_fraction.setMaxValue(1.0)
    args << onsite_parking_fraction

    # make an argument for add_exhaust
    add_exhaust = OpenStudio::Measure::OSArgument.makeBoolArgument('add_exhaust', true)
    add_exhaust.setDisplayName('Add Exhaust Fans to Model')
    add_exhaust.setDescription('Depending upon building type exhaust fans can be in kitchens, restrooms or other space types')
    add_exhaust.setDefaultValue(true)
    args << add_exhaust

    # make an argument for add_swh
    add_swh = OpenStudio::Measure::OSArgument.makeBoolArgument('add_swh', true)
    add_swh.setDisplayName('Add Service Water Heating to Model')
    add_swh.setDescription('This will add both the supply and demand side of service water heating.')
    add_swh.setDefaultValue(true)
    args << add_swh

    # make an argument for add_thermostat
    add_thermostat = OpenStudio::Measure::OSArgument.makeBoolArgument('add_thermostat', true)
    add_thermostat.setDisplayName('Add Thermostats')
    add_thermostat.setDescription('Add Thermostat to model based on Space Type Standards information of spaces assigned to thermal zones.')
    add_thermostat.setDefaultValue(true)
    args << add_thermostat

    # make an argument for add_hvac
    add_hvac = OpenStudio::Measure::OSArgument.makeBoolArgument('add_hvac', true)
    add_hvac.setDisplayName('Add HVAC System to Model')
    add_hvac.setDescription('Add HVAC System to the model')
    add_hvac.setDefaultValue(true)
    args << add_hvac

    # make an argument for add_refrigeration
    add_refrigeration = OpenStudio::Measure::OSArgument.makeBoolArgument('add_refrigeration', true)
    add_refrigeration.setDisplayName('Add Refrigeration to Model')
    add_refrigeration.setDescription('Add refrigeration cases and walkins model')
    add_refrigeration.setDefaultValue(true)
    args << add_refrigeration

    # make an argument for refrigeration_template
    refrigeration_template_choices = OpenStudio::StringVector.new
    refrigeration_template_choices << 'old'
    refrigeration_template_choices << 'new'
    refrigeration_template_choices << 'advanced'
    refrigeration_template = OpenStudio::Measure::OSArgument.makeChoiceArgument('refrigeration_template', refrigeration_template_choices, true)
    refrigeration_template.setDisplayName('Refrigeration Technology Level')
    refrigeration_template.setDescription('The refrigeration technology level used for refrigerated cases, walkins, compressors, and systems.')
    refrigeration_template.setDefaultValue('new') # NOTE: ComStock uses 'new' now
    # We use new because otherwise, it will create a cooler and a freezer with
    # old: insulated_surface_uvalue_facing_zone = 2.270, insulated_floor_uvalue = 0.203
    # new: insulated_surface_uvalue_facing_zone = 0.203, insulated_floor_uvalue = 2.270
    # and we get sizing errors in PrimarySchool at least
    args << refrigeration_template

    # modify weekday hours of operation
    modify_wkdy_op_hrs = OpenStudio::Measure::OSArgument.makeBoolArgument('modify_wkdy_op_hrs', true)
    modify_wkdy_op_hrs.setDisplayName('Modify weekday hours of operation')
    modify_wkdy_op_hrs.setDescription('Modify the weekday hours of operation in the model.')
    modify_wkdy_op_hrs.setDefaultValue(false)
    args << modify_wkdy_op_hrs

    # weekday hours of operation start time
    wkdy_op_hrs_start_time = OpenStudio::Measure::OSArgument.makeDoubleArgument('wkdy_op_hrs_start_time', true)
    wkdy_op_hrs_start_time.setDisplayName('Weekday Operating Hours Start Time')
    wkdy_op_hrs_start_time.setDescription('Enter 24 hour values with fractional values converted to minutes. e.g. 17.25 = 5:15pm. Only used if Modify weekday hours of operation is true.')
    wkdy_op_hrs_start_time.setUnits('Hours')
    wkdy_op_hrs_start_time.setDefaultValue(8.0)
    args << wkdy_op_hrs_start_time

    # weekday hours of operation duration
    wkdy_op_hrs_duration = OpenStudio::Measure::OSArgument.makeDoubleArgument('wkdy_op_hrs_duration', true)
    wkdy_op_hrs_duration.setDisplayName('Weekday Operating Hours Duration')
    wkdy_op_hrs_duration.setDescription('Length of weekday operating hours. Enter 24 hour values with fractional values converted to minutes. e.g. 17.25 = 5:15pm. Only used if Modify weekday hours of operation is true.')
    wkdy_op_hrs_duration.setUnits('Hours')
    wkdy_op_hrs_duration.setDefaultValue(8.0)
    args << wkdy_op_hrs_duration

    # modify weekend hours of operation
    modify_wknd_op_hrs = OpenStudio::Measure::OSArgument.makeBoolArgument('modify_wknd_op_hrs', true)
    modify_wknd_op_hrs.setDisplayName('Modify weekend hours of operation')
    modify_wknd_op_hrs.setDescription('Modify the weekend hours of operation in the model.')
    modify_wknd_op_hrs.setDefaultValue(false)
    args << modify_wknd_op_hrs

    # weekend hours of operation start time
    wknd_op_hrs_start_time = OpenStudio::Measure::OSArgument.makeDoubleArgument('wknd_op_hrs_start_time', true)
    wknd_op_hrs_start_time.setDisplayName('Weekend Operating Hours Start Time')
    wknd_op_hrs_start_time.setDescription('Enter 24 hour values with fractional values converted to minutes. e.g. 17.25 = 5:15pm.  Only used if Modify weekend hours of operation is true.')
    wknd_op_hrs_start_time.setUnits('Hours')
    wknd_op_hrs_start_time.setDefaultValue(8.0)
    args << wknd_op_hrs_start_time

    # weekend hours of operation duration
    wknd_op_hrs_duration = OpenStudio::Measure::OSArgument.makeDoubleArgument('wknd_op_hrs_duration', true)
    wknd_op_hrs_duration.setDisplayName('Weekend Operating Hours Duration')
    wknd_op_hrs_duration.setDescription('Length of weekend operating hours. Enter 24 hour values with fractional values converted to minutes. e.g. 17.25 = 5:15pm.  Only used if Modify weekend hours of operation is true.')
    wknd_op_hrs_duration.setUnits('Hours')
    wknd_op_hrs_duration.setDefaultValue(8.0)
    args << wknd_op_hrs_duration

    # make an argument for unmet_hours_tolerance
    unmet_hours_tolerance = OpenStudio::Measure::OSArgument.makeDoubleArgument('unmet_hours_tolerance', true)
    unmet_hours_tolerance.setDisplayName('Unmet Hours Tolerance')
    unmet_hours_tolerance.setDescription('Set the thermostat setpoint tolerance for unmet hours in degrees Rankine')
    unmet_hours_tolerance.setDefaultValue(1.0)
    unmet_hours_tolerance.setMinValue(0.0)
    unmet_hours_tolerance.setMaxValue(5.0)
    unmet_hours_tolerance.setUnits('R')
    args << unmet_hours_tolerance

    # make an argument for remove_objects
    remove_objects = OpenStudio::Measure::OSArgument.makeBoolArgument('remove_objects', true)
    remove_objects.setDisplayName('Clean Model of non-geometry objects')
    remove_objects.setDescription('Only removes objects of type that are selected to be added.')
    remove_objects.setDefaultValue(true)
    args << remove_objects

    # make an argument for use_upstream_args
    use_upstream_args = OpenStudio::Measure::OSArgument.makeBoolArgument('use_upstream_args', true)
    use_upstream_args.setDisplayName('Use Upstream Argument Values')
    use_upstream_args.setDescription('When true this will look for arguments or registerValues in upstream measures that match arguments from this measure, and will use the value from the upstream measure in place of what is entered for this measure.')
    use_upstream_args.setDefaultValue(true)
    args << use_upstream_args

    # # make force daylight savings on
    # enable_dst = OpenStudio::Measure::OSArgument.makeBoolArgument('enable_dst', true)
    # enable_dst.setDisplayName('Enable Daylight Savings')
    # enable_dst.setDescription('By default this will force daylight savings to be enabled. Set to false if in a location where DST is not followed, or if needed for specific use case.')
    # enable_dst.setDefaultValue(true)
    # args << enable_dst

    # Argument used to make ComStock tsv workflow run correctly
    climate_zone = OpenStudio::Measure::OSArgument.makeChoiceArgument('climate_zone', OpenstudioStandards::CreateTypical.get_climate_zones(false, 'Lookup From Model'), true)
    climate_zone.setDisplayName('Climate Zone.')
    climate_zone.setDefaultValue('Lookup From Model')
    args << climate_zone

    return args
  end

  # define what happens when the measure is run
  def run(model, runner, user_arguments)
    super(model, runner, user_arguments)

    if !ENV['SAVE_INTERMEDIATE'].nil?
      model.save('4.1.before_create_typical_building_from_model_comstock.osm')
    end

    args = runner.getArgumentValues(arguments(model), user_arguments)
    # Keys are symbols, turn them into strings
    args.transform_keys!(&:to_s)

    if args['use_upstream_args'] == true
      args.each do |arg, _value|
        next if arg == 'use_upstream_args' # this argument should not be changed

        upstream_arg = runner.getPastStepValuesForName(arg)
        next if upstream_arg.empty?

        measure_name, new_val = upstream_arg.first

        if arg == 'template'
          runner.registerInfo("Actually NOT replacing template from '#{args[arg]}' to '#{new_val}' from #{measure_name}")
          next
        end

        runner.registerInfo("Replacing argument named #{arg} from current measure with a value of #{new_val} from #{measure_name}.")
        # TODO: - make code to handle non strings more robust. check_upstream_measure_for_arg coudl pass bakc the argument type
        case arg
        when 'total_bldg_floor_area'
          args[arg] = new_val.to_f
        when 'num_stories_above_grade'
          args[arg] = new_val.to_f
        when 'zipcode'
          args[arg] = new_val.to_i
        else
          args[arg] = new_val
        end
      end
    end

    is_179d = args['template'] == DEFAULT_TEMPLATE_179D

    add_infiltration = true

    # method run from os_lib_model_generation.rb
    result = typical_building_from_model(
      model,
      runner,
      args['template'],
      climate_zone: args['climate_zone'], # start of optional arguments
      add_hvac: args['add_hvac'],
      hvac_system_type: args['system_type'],
      hvac_delivery_type: args['hvac_delivery_type'],
      heating_fuel: args['htg_src'],
      service_water_heating_fuel: args['swh_src'],
      cooling_fuel: args['clg_src'],
      kitchen_makeup: args['kitchen_makeup'],
      exterior_lighting_zone: args['exterior_lighting_zone'],
      add_constructions: args['add_constructions'],
      wall_construction_type: args['wall_construction_type'],
      add_space_type_loads: args['add_space_type_loads'],
      add_daylighting_controls: false, # not exposed in user measure args
      # NOTE: we don't apply NIST infiltration, but we use the actual standards info from our subclass if it's 179D
      add_infiltration: is_179d ? false : add_infiltration,
      add_elevators: args['add_elevators'],
      add_internal_mass: args['add_internal_mass'],
      add_exterior_lights: args['add_exterior_lights'],
      onsite_parking_fraction: args['onsite_parking_fraction'],
      add_exhaust: args['add_exhaust'],
      add_swh: args['add_swh'],
      add_thermostat: args['add_thermostat'],
      add_refrigeration: args['add_refrigeration'],
      refrigeration_template: args['refrigeration_template'],
      modify_wkdy_op_hrs: args['modify_wkdy_op_hrs'],
      wkdy_op_hrs_start_time: args['wkdy_op_hrs_start_time'],
      wkdy_op_hrs_duration: args['wkdy_op_hrs_duration'],
      modify_wknd_op_hrs: args['modify_wknd_op_hrs'],
      wknd_op_hrs_start_time: args['wknd_op_hrs_start_time'],
      wknd_op_hrs_duration: args['wknd_op_hrs_duration'],
      hoo_var_method: nil, # not exposed in user measure args
      enable_dst: args['enable_dst'],
      unmet_hours_tolerance_r: args['unmet_hours_tolerance'],
      remove_objects: args['remove_objects'],
      user_hvac_mapping: nil, # not exposed in this measure yet?
      sizing_run_directory: nil # not exposed in user measure args
    )

    if is_179d
      runner.registerInfo('Applying HVAC Availabity schedule per ACM rules')
      std = Standard.build(args['template'])
      std.model_apply_acm_hvac_availability_schedule(model)
      if add_infiltration
        if args['remove_objects']
          model.getSpaceInfiltrationDesignFlowRates.reject { |spi| spi.nameString.include?('Makeup infil') }.each(&:remove)
          model.getSpaceInfiltrationEffectiveLeakageAreas.each(&:remove)
          model.getSpaceInfiltrationFlowCoefficients.each(&:remove)
        end
        model.getSpaceTypes.each do |space_type|
          std.space_type_apply_standard_infiltration(space_type)
          std.space_type_apply_standard_infiltration_schedule(space_type)
        end
      end
    end

    if !ENV['SAVE_INTERMEDIATE'].nil?
      model.save('4.2.after_create_typical_building_from_model_comstock.osm')
    end

    if result == false
      return false
    else
      return true
    end
  end
end

# register the measure to be used by the application
CreateTypicalBuildingFromModelComStock.new.registerWithApplication
