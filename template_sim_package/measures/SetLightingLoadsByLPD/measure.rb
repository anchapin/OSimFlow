# *******************************************************************************
# OpenStudio(R), Copyright (c) Alliance for Sustainable Energy, LLC.
# See also https://openstudio.net/license
# *******************************************************************************

# start the measure
class SetLightingLoadsByLPD < OpenStudio::Measure::ModelMeasure
  # define the name that a user will see
  def name
    return 'Set Lighting Loads by LPD'
  end

  # human readable description
  def description
    return 'Set the lighting power density (W/ft^2) in the to a specified value for all spaces that have lights.'
  end

  # human readable description of modeling approach
  def modeler_description
    return ''
  end

  # helper to make it easier to do unit conversions on the fly.  The definition be called through this measure.
  def unit_helper(number, from_unit_string, to_unit_string)
    return OpenStudio.convert(OpenStudio::Quantity.new(number, OpenStudio.createUnit(from_unit_string).get), OpenStudio.createUnit(to_unit_string).get).get.value
  end

  # short def to make numbers pretty (converts 4125001.25641 to 4,125,001.26 or 4,125,001). The definition be called through this measure
  # round to 0 or 2)
  def neat_numbers(number, roundto = 2)
    if roundto == 2
      number = format '%.2f', number
    else
      number = number.round
    end
    # regex to add commas
    number.to_s.reverse.gsub(/([0-9]{3}(?=([0-9])))/, '\\1,').reverse
  end

  def get_light_definition_parameters(light_definition)
    # get design calculation method
    calc_method = light_definition.designLevelCalculationMethod

    # get original values: lightingLevel
    lighting_level = nil
    if light_definition.lightingLevel.is_initialized
      lighting_level = light_definition.lightingLevel.get
    end

    # get original values: wattsperSpaceFloorArea
    watts_per_space_floor_area = nil
    if light_definition.wattsperSpaceFloorArea.is_initialized
      watts_per_space_floor_area = light_definition.wattsperSpaceFloorArea.get
    end

    # get original values: wattsperPerson
    watts_per_person = nil
    if light_definition.wattsperPerson.is_initialized
      watts_per_person = light_definition.wattsperPerson.get
    end

    # get original fractional values
    fraction_radiant = light_definition.fractionRadiant
    fraction_visible = light_definition.fractionVisible

    return calc_method, lighting_level, watts_per_space_floor_area, watts_per_person, fraction_radiant, fraction_visible
  end

  # define the arguments that the user will input
  def arguments(_model)
    args = OpenStudio::Measure::OSArgumentVector.new

    # make an argument LPD
    lpd = OpenStudio::Measure::OSArgument.makeDoubleArgument('lpd', true)
    lpd.setDisplayName('Lighting Power Density (W/ft^2)')
    lpd.setDefaultValue(1.0)
    args << lpd

    return args
  end

  # define what happens when the measure is run
  def run(model, runner, user_arguments)
    super(model, runner, user_arguments)

    if !ENV['SAVE_INTERMEDIATE'].nil?
      model.save('7.1.before_SetLightingLoadsByLPD.osm')
    end

    # use the built-in error checking
    if !runner.validateUserArguments(arguments(model), user_arguments)
      return false
    end

    # assign the user inputs to variables
    lpd = runner.getDoubleArgumentValue('lpd', user_arguments)

    # check the lpd for reasonableness
    if (lpd < 0) || (lpd > 50)
      runner.registerError("A Lighting Power Density of #{lpd} W/ft^2 is above the measure limit.")
      return false
    elsif lpd > 21
      runner.registerWarning("A Lighting Power Density of #{lpd} W/ft^2 is abnormally high.")
    end

    # unit conversion of lpd from IP units (W/ft^2) to SI units (W/m^2)
    lpd_si = OpenStudio.convert(lpd, 'W/ft^2', 'W/m^2').get

    # get all lighting definitions
    lighting_definitions_summary = {}
    model.getSpaceTypes.each do |spacetype|
      # skip if not used in model
      next if spacetype.spaces.empty?

      # get floor area
      floor_area = spacetype.floorArea

      # get number of people
      number_of_people = spacetype.getNumberOfPeople(floor_area)

      # get lights from space type
      lights = spacetype.lights
      # get light definition
      lights.each do |light|
        light_definition = light.lightsDefinition
        light_definition_name = light_definition.name.to_s
        unless lighting_definitions_summary.key?(light_definition_name)
          lighting_definitions_summary[light_definition_name] = {}
          lighting_definitions_summary[light_definition_name]['floor_area'] = floor_area
          lighting_definitions_summary[light_definition_name]['number_of_people'] = number_of_people
          lighting_definitions_summary[light_definition_name]['object'] = light_definition
        end
      end
    end
    count_lighting_definition_total = lighting_definitions_summary.size
    runner.registerInitialCondition("The building has #{count_lighting_definition_total} lighting definition(s).")

    # initialize variables
    count_lighting_definition_modified = 0
    lighting_definitions_summary.each do |_light_definition_name, entries|
      runner.registerInfo('### ===========================================================================')
      runner.registerInfo("### light_definition_name = #{entries['object'].name}")

      # get existing lighting parameters
      calc_method_old, lighting_level_old, watts_per_space_floor_area_old, watts_per_person_old, fraction_radiant_old, fraction_visible_old = get_light_definition_parameters(entries['object'])

      runner.registerInfo('### ORIGINAL -------------------------------------------------------------------')
      runner.registerInfo("### calc_method_old = #{calc_method_old}")
      runner.registerInfo("### lighting_level_old = #{lighting_level_old}")
      runner.registerInfo("### watts_per_space_floor_area_old = #{watts_per_space_floor_area_old}")
      runner.registerInfo("### watts_per_person_old = #{watts_per_person_old}")
      runner.registerInfo("### fraction_radiant_old = #{fraction_radiant_old}")
      runner.registerInfo("### fraction_visible_old = #{fraction_visible_old}")

      # override lighting power with user-defined LPD
      if calc_method_old == 'Watts/Area'
        entries['object'].setWattsperSpaceFloorArea(lpd_si)
      else
        entries['object'].setDesignLevelCalculationMethod('Watts/Area', entries['floor_area'], entries['number_of_people'])
        entries['object'].setWattsperSpaceFloorArea(lpd_si)
      end
      count_lighting_definition_modified += 1

      # get updated lighting parameters
      calc_method_new, lighting_level_new, watts_per_space_floor_area_new, watts_per_person_new, fraction_radiant_new, fraction_visible_new = get_light_definition_parameters(entries['object'])

      runner.registerInfo('### REVISED -------------------------------------------------------------------')
      runner.registerInfo("### calc_method_new = #{calc_method_new}")
      runner.registerInfo("### lighting_level_new = #{lighting_level_new}")
      runner.registerInfo("### watts_per_space_floor_area_new = #{watts_per_space_floor_area_new}")
      runner.registerInfo("### watts_per_person_new = #{watts_per_person_new}")
      runner.registerInfo("### fraction_radiant_new = #{fraction_radiant_new}")
      runner.registerInfo("### fraction_visible_new = #{fraction_visible_new}")

      # raise error if parameters other than watts_per_space_floor_area changed
      if lighting_level_old != lighting_level_new
        runner.registerWarning('unexpected lighting parameter changed with this measure: lighting_level')
      end
      if watts_per_person_old != watts_per_person_new
        runner.registerWarning('unexpected lighting parameter changed with this measure: watts_per_person')
      end
      if fraction_radiant_old != fraction_radiant_new
        runner.registerError('unexpected lighting parameter changed with this measure: fraction_radiant')
        return false
      end
      if fraction_visible_old != fraction_visible_new
        runner.registerError('unexpected lighting parameter changed with this measure: fraction_visible_new')
        return false
      end

      # raise error if lighting_level or watts_per_person is defined
      unless (watts_per_person_new.nil?) & (lighting_level_new.nil?)
        runner.registerError("lighting power definition besides watts_per_space_floor_area defined in the final model: lighting_level = #{lighting_level_new}, watts_per_person = #{watts_per_person_new}")
        return false
      end
    end

    # report final condition
    runner.registerFinalCondition("Out of #{count_lighting_definition_total} total lighting definition(s), #{count_lighting_definition_modified} lighting definition(s) are modified with the user-input lighting power density of #{lpd} W/ft^2 (#{lpd_si.round(3)} W/m^2)")

    if !ENV['SAVE_INTERMEDIATE'].nil?
      model.save('7.2.after_SetLightingLoadsByLPD.osm')
    end

    return true
  end
end

# this allows the measure to be used by the application
SetLightingLoadsByLPD.new.registerWithApplication
