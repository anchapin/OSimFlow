# ComStock(TM), Copyright (c) 2020 Alliance for Sustainable Energy, LLC. All rights reserved.
# See top level LICENSE.txt file for license terms.

# *******************************************************************************
# OpenStudio(R), Copyright (c) Alliance for Sustainable Energy, LLC.
# See also https://openstudio.net/license
# *******************************************************************************

# start the measure
class ReplaceBaselineWindowsRvalues < OpenStudio::Measure::ModelMeasure
  # human readable name
  def name
    # measure name should be the title case of the class name.
    return 'replace_baseline_windows_r_values'
  end

  # human readable description
  def description
    return 'Replaces the windows in the baseline based on corresponding U-value, SHGC, and VLT.'
  end

  # human readable description of modeling approach
  def modeler_description
    return 'First gets all building detailed fenestration surfaces. Loops over all detailed fenestration surfaces and checks to see if the surface type is a window. If the surface type is a window then it gets the then get the construction name. With the construction name it determines the simple glazing system object name. With the simple glazing system object name it modifies the U-Value, SHGC, and VLT accordingly.'
  end

  # define the arguments that the user will input
  def arguments(_model)
    # make an argument vector
    args = OpenStudio::Measure::OSArgumentVector.new

    # make argument for window_pane_type
    window_pane_type_choices = OpenStudio::StringVector.new
    window_pane_type_choices << 'Single - No LowE - Clear - Aluminum'
    window_pane_type_choices << 'Single - No LowE - Clear - Wood'
    window_pane_type_choices << 'Single - No LowE - Tinted/Reflective - Aluminum'
    window_pane_type_choices << 'Single - No LowE - Tinted/Reflective - Wood'
    window_pane_type_choices << 'Double - LowE - Clear - Aluminum'
    window_pane_type_choices << 'Double - LowE - Clear - Thermally Broken Aluminum'
    window_pane_type_choices << 'Double - LowE - Tinted/Reflective - Aluminum'
    window_pane_type_choices << 'Double - LowE - Tinted/Reflective - Thermally Broken Aluminum'
    window_pane_type_choices << 'Double - No LowE - Clear - Aluminum'
    window_pane_type_choices << 'Double - No LowE - Tinted/Reflective - Aluminum'
    window_pane_type_choices << 'Triple - LowE - Clear - Thermally Broken Aluminum'
    window_pane_type_choices << 'Triple - LowE - Tinted/Reflective - Thermally Broken Aluminum'
    window_pane_type_choices << 'Selected Values'
    window_pane_type = OpenStudio::Measure::OSArgument.makeChoiceArgument('window_pane_type', window_pane_type_choices, true)
    window_pane_type.setDisplayName('Window Pane Type')
    window_pane_type.setDescription('Identify window pane type to be applied to entire building')
    window_pane_type.setDefaultValue('Selected Values')
    args << window_pane_type

    # make argument for fenestration_frame_type
    fenestration_frame_type_choices = OpenStudio::StringVector.new
    fenestration_frame_type_choices << 'Metal Framing'
    fenestration_frame_type_choices << 'Metal Framing with Thermal Break'
    fenestration_frame_type_choices << 'Non-Metal Framing'
    fenestration_frame_type = OpenStudio::Measure::OSArgument.makeChoiceArgument('fenestration_frame_type', fenestration_frame_type_choices, true)
    fenestration_frame_type.setDisplayName('Fenestration Frame Type')
    fenestration_frame_type.setDescription('Identify window framing type to be applied to entire building for usage with Openstudio Standards')
    fenestration_frame_type.setDefaultValue('Metal Framing with Thermal Break')
    args << fenestration_frame_type

    # make an argument for window U-Value
    u_value_ip = OpenStudio::Measure::OSArgument.makeDoubleArgument('u_value_ip', true)
    u_value_ip.setDisplayName('Window U-value')
    u_value_ip.setUnits('Btu/ft^2*h*R')
    default_u_val = 1.01
    u_value_ip.setDefaultValue(default_u_val)
    args << u_value_ip

    # make an argument for window SHGC
    shgc = OpenStudio::Measure::OSArgument.makeDoubleArgument('shgc', true)
    shgc.setDisplayName('Window SHGC')
    shgc.setDefaultValue(0.744)
    args << shgc

    # make an argument for window VLT
    vlt = OpenStudio::Measure::OSArgument.makeDoubleArgument('vlt', true)
    vlt.setDisplayName('Window VLT')
    vlt.setDefaultValue(0.754)
    args << vlt
    return args
  end

  # define what happens when the measure is run
  def run(model, runner, user_arguments)
    super(model, runner, user_arguments)

    if !ENV['SAVE_INTERMEDIATE'].nil?
      model.save('8.1.before_replace_baseline_windows_Rvalues.osm')
    end

    # use the built-in error checking
    if !runner.validateUserArguments(arguments(model), user_arguments)
      return false
    end

    # create new construction hash
    # key = old construction, value = new construction
    new_construction_hash = {}

    # hardcoded window performances
    map_properties = {
      'Single - No LowE - Clear - Aluminum' => {
        'u_value_ip' => 1.01,
        'shgc' => 0.744,
        'vlt' => 0.754,
      },
      'Single - No LowE - Clear - Wood' => {
        'u_value_ip' => 0.91,
        'shgc' => 0.683,
        'vlt' => 0.723,
      },
      'Single - No LowE - Tinted/Reflective - Aluminum' => {
        'u_value_ip' => 1.01,
        'shgc' => 0.579,
        'vlt' => 0.455,
      },
      'Single - No LowE - Tinted/Reflective - Wood' => {
        'u_value_ip' => 0.91,
        'shgc' => 0.525,
        'vlt' => 0.436,
      },
      'Double - LowE - Clear - Aluminum' => {
        'u_value_ip' => 0.559,
        'shgc' => 0.386,
        'vlt' => 0.591,
      },
      'Double - LowE - Clear - Thermally Broken Aluminum' => {
        'u_value_ip' => 0.499,
        'shgc' => 0.378,
        'vlt' => 0.591,
      },
      'Double - LowE - Tinted/Reflective - Aluminum' => {
        'u_value_ip' => 0.557,
        'shgc' => 0.274,
        'vlt' => 0.359,
      },
      'Double - LowE - Tinted/Reflective - Thermally Broken Aluminum' => {
        'u_value_ip' => 0.496,
        'shgc' => 0.266,
        'vlt' => 0.359,
      },
      'Double - No LowE - Clear - Aluminum' => {
        'u_value_ip' => 0.746,
        'shgc' => 0.646,
        'vlt' => 0.671,
      },
      'Double - No LowE - Tinted/Reflective - Aluminum' => {
        'u_value_ip' => 0.749,
        'shgc' => 0.484,
        'vlt' => 0.411,
      },
      'Triple - LowE - Clear - Thermally Broken Aluminum' => {
        'u_value_ip' => 0.3,
        'shgc' => 0.328,
        'vlt' => 0.527,
      },
      'Triple - LowE - Tinted/Reflective - Thermally Broken Aluminum' => {
        'u_value_ip' => 0.299,
        'shgc' => 0.224,
        'vlt' => 0.32,
      },
    }
    ordered_maps = map_properties.sort_by { |_k, v| v['shgc'] }
    # assign the user inputs to variables
    window_pane_type = runner.getStringArgumentValue('window_pane_type', user_arguments)
    if window_pane_type == 'Selected Values'
      simple_glazing_u_ip = runner.getDoubleArgumentValue('u_value_ip', user_arguments)
      simple_glazing_shgc = runner.getDoubleArgumentValue('shgc', user_arguments)
      simple_glazing_vlt = runner.getDoubleArgumentValue('vlt', user_arguments)
      # REVIEW: values based on shgc
      selected_ordered_maps = ordered_maps.select { |_k, v| v['shgc'] >= simple_glazing_shgc }

      if !selected_ordered_maps.empty?
        runner.registerInfo("Validate range of values for SGHC: #{simple_glazing_shgc} with U_value #{simple_glazing_u_ip} and VLT #{simple_glazing_vlt}")
        #  runner.registerInfo("selected_ordered_maps:#{selected_ordered_maps}")
        if selected_ordered_maps[0][1]['vlt'] < simple_glazing_vlt
          simple_glazing_vlt = selected_ordered_maps[0][1]['vlt']
        end
      end
    else
      simple_glazing_shgc = map_properties[window_pane_type]['shgc']
      simple_glazing_u_ip = map_properties[window_pane_type]['u_value_ip']
      simple_glazing_vlt = map_properties[window_pane_type]['vlt']

    end

    runner.registerValue('Final_shgc', simple_glazing_shgc)
    runner.registerValue('Final_u_ip', simple_glazing_u_ip)
    runner.registerValue('Final_vlt', simple_glazing_vlt)

    # convert u-value to SI units
    simple_glazing_u_si = OpenStudio.convert(simple_glazing_u_ip, 'Btu/ft^2*h*R', 'W/m^2*K').get

    # get all fenestration surfaces
    sub_surfaces = []
    constructions = []

    model.getSubSurfaces.each do |sub_surface|
      next unless sub_surface.subSurfaceType.include?('Window')

      sub_surfaces << sub_surface
      constructions << sub_surface.construction.get
    end

    # check to make sure building has fenestration surfaces
    if sub_surfaces.empty?
      runner.registerAsNotApplicable('The building has no windows.')
      return true
    end

    # get all simple glazing system window materials
    simple_glazings = model.getSimpleGlazings
    if simple_glazings.length >= 1
      old_simple_glazing = simple_glazings.first

      # get old values
      old_simple_glazing_u = old_simple_glazing.uFactor
      old_simple_glazing_shgc = old_simple_glazing.solarHeatGainCoefficient
      if old_simple_glazing.visibleTransmittance.is_initialized
        old_simple_glazing_vlt = old_simple_glazing.visibleTransmittance.get
      else
        old_simple_glazing_vlt = 'null'
      end
      # register initial condition
      runner.registerInfo("Existing windows '#{old_simple_glazing.nameString}' have #{old_simple_glazing_u.round(2)} W/m2-K U-value , #{old_simple_glazing_shgc} SHGC, and #{old_simple_glazing_vlt} VLT.")
    else
      # register initial condition
      runner.registerInfo('Existing windows are not simple glazing; will be swapped with simple glazing object.')
    end

    # assign the user input for fenestration frame type to variable
    fenestration_frame_type = runner.getStringArgumentValue('fenestration_frame_type', user_arguments)
    # make new simple glazing with new properties
    new_simple_glazing = OpenStudio::Model::SimpleGlazing.new(model)
    new_simple_glazing.setName("Simple Glazing #{window_pane_type}")

    # set and register final condition
    new_simple_glazing.setUFactor(simple_glazing_u_si)
    new_simple_glazing.setSolarHeatGainCoefficient(simple_glazing_shgc)
    new_simple_glazing.setVisibleTransmittance(simple_glazing_vlt)

    # define total area changed
    area_changed_m2 = 0.0
    # loop over constructions and simple glazings
    constructions.each do |construction|
      # check if construction has been made
      if new_construction_hash.key?(construction)
        new_construction = new_construction_hash[construction]
      else
        # register final condition
        runner.registerInfo("New window #{new_simple_glazing.name.get} has #{simple_glazing_u_si.round(2)} W/m2-K U-value , #{simple_glazing_shgc.round(2)} SHGC, and #{simple_glazing_vlt.round(2)} VLT.")
        # create new construction with this new simple glazing layer
        new_construction = OpenStudio::Model::Construction.new(model)
        new_construction.setName("Window U-#{simple_glazing_u_ip.round(2)} SHGC #{simple_glazing_shgc.round(2)}")
        new_construction.insertLayer(0, new_simple_glazing)

        # set standards info
        runner.registerInfo("Setting standards info on new exterior window construction, #{new_construction.name}. The frame type is #{fenestration_frame_type}.")
        standards_info = new_construction.standardsInformation
        standards_info.setFenestrationFrameType(fenestration_frame_type)
        standards_info.setIntendedSurfaceType('ExteriorWindow')

        # update hash
        new_construction_hash[construction] = new_construction
      end

      # loop over fenestration surfaces and add new construction
      sub_surfaces.each do |sub_surface|
        # assign new construction to fenestration surfaces and add total area changed if construction names match
        next unless sub_surface.construction.get.to_Construction.get.layers[0].name.get == construction.to_Construction.get.layers[0].name.get

        sub_surface.setConstruction(new_construction)
        area_changed_m2 += sub_surface.grossArea
      end
    end

    # summary
    area_changed_ft2 = OpenStudio.convert(area_changed_m2, 'm^2', 'ft^2').get
    runner.registerFinalCondition("Changed #{area_changed_ft2.round(2)} ft2 of window to U-#{simple_glazing_u_ip.round(2)}, SHGC-#{simple_glazing_shgc.round(2)}, VLT-#{simple_glazing_vlt.round(2)}")
    runner.registerValue('env_window_fen_area_ft2', area_changed_ft2.round(2), 'ft2')

    if !ENV['SAVE_INTERMEDIATE'].nil?
      model.save('8.2.after_replace_baseline_windows_Rvalues.osm')
    end

    return true
  end
end

# register the measure to be used by the application
ReplaceBaselineWindowsRvalues.new.registerWithApplication
