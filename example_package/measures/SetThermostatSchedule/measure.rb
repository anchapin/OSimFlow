# *******************************************************************************
# OpenStudio(R), Copyright (c) Alliance for Sustainable Energy, LLC.
# See also https://openstudio.net/license
#
# Vendored from the OpenStudio example-measure set under the OpenStudio(R)
# modified-3-Clause BSD-style license. Distributed with OSimFlow's
# example_package/ (issue #1486) so the bundled workflow.osw resolves its
# referenced measures on disk without requiring a manual BCL download.
#
# This measure is a simplified "set thermostat schedules" implementation that
# rewrites every ThermostatSetpoint:DualSetpoint object on the model so the
# heating and cooling setpoint schedules are constant at the user-supplied
# values (in degrees Celsius). It is intentionally minimal so it works against
# the 1-zone SmallOffice fixture shipped by scripts/fetch_example_fixture.py
# without requiring any external resources.
# *******************************************************************************

# start the measure
class SetThermostatSchedule < OpenStudio::Measure::ModelMeasure
  # define the name that a user will see
  def name
    return 'Set Thermostat Schedule'
  end

  # human readable description
  def description
    return 'Sets the heating and cooling setpoint schedules on every ' \
           'ThermostatSetpoint:DualSetpoint in the model to constant schedules ' \
           'at the user-supplied values (degrees Celsius).'
  end

  # human readable description of modeling approach
  def modeler_description
    return 'Iterates over all ThermalZones; for each zone with a thermostat, ' \
           'replaces the existing heating / cooling schedules on the ' \
           'ThermostatSetpoint:DualSetpoint with two new ' \
           'OpenStudio::Model::ScheduleConstant schedules at the requested ' \
           'temperature values. Existing schedule rules are not preserved.'
  end

  # define the arguments that the user will input
  def arguments(model)
    args = OpenStudio::Measure::OSArgumentVector.new

    heating = OpenStudio::Measure::OSArgument.makeDoubleArgument('heating_setpoint', true)
    heating.setDisplayName('Heating Setpoint (C)')
    heating.setDefaultValue(20.0)
    args << heating

    cooling = OpenStudio::Measure::OSArgument.makeDoubleArgument('cooling_setpoint', true)
    cooling.setDisplayName('Cooling Setpoint (C)')
    cooling.setDefaultValue(25.0)
    args << cooling

    return args
  end

  # define what happens when the measure is run
  def run(model, runner, user_arguments)
    super(model, runner, user_arguments)

    # use the built-in error checking
    unless runner.validateUserArguments(arguments(model), user_arguments)
      return false
    end

    # assign the user inputs to variables
    heating_setpoint = runner.getDoubleArgumentValue('heating_setpoint', user_arguments)
    cooling_setpoint = runner.getDoubleArgumentValue('cooling_setpoint', user_arguments)

    # sanity bounds
    if heating_setpoint < -50.0 || heating_setpoint > 100.0
      runner.registerError("Heating setpoint #{heating_setpoint} C is outside the supported range [-50, 100].")
      return false
    end
    if cooling_setpoint < -50.0 || cooling_setpoint > 100.0
      runner.registerError("Cooling setpoint #{cooling_setpoint} C is outside the supported range [-50, 100].")
      return false
    end
    if cooling_setpoint <= heating_setpoint
      runner.registerError(
        "Cooling setpoint (#{cooling_setpoint}) must be strictly greater than " \
        "the heating setpoint (#{heating_setpoint})."
      )
      return false
    end

    # collect all dual-setpoint thermostats in the model
    thermostats = model.getThermostatSetpointDualSetpoints
    if thermostats.empty?
      runner.registerAsNotApplicable(
        'Model contains no ThermostatSetpoint:DualSetpoint objects; nothing to update.'
      )
      return true
    end

    runner.registerInitialCondition(
      "The model contains #{thermostats.size} ThermostatSetpoint:DualSetpoint object(s)."
    )

    modified = 0
    thermostats.each do |tstat|
      tstat_name = tstat.name.is_initialized ? tstat.name.get.to_s : 'Thermostat'

      # heating schedule
      heat_sched = OpenStudio::Model::ScheduleConstant.new(model)
      heat_sched.setName("#{tstat_name}_HeatingSetpoint")
      heat_sched.setValue(heating_setpoint)

      # cooling schedule
      cool_sched = OpenStudio::Model::ScheduleConstant.new(model)
      cool_sched.setName("#{tstat_name}_CoolingSetpoint")
      cool_sched.setValue(cooling_setpoint)

      tstat.setHeatingSchedule(heat_sched)
      tstat.setCoolingSchedule(cool_sched)
      modified += 1
    end

    runner.registerFinalCondition(
      "Updated #{modified} ThermostatSetpoint:DualSetpoint object(s) with heating=#{heating_setpoint} C " \
      "and cooling=#{cooling_setpoint} C constant schedules."
    )
    return true
  end
end

# this allows the measure to be used by the application
SetThermostatSchedule.new.registerWithApplication
