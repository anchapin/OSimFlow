# *******************************************************************************
# OpenStudio(R), Copyright (c) Alliance for Sustainable Energy, LLC.
# See also https://openstudio.net/license
# *******************************************************************************

# see the URL below for information on how to write OpenStudio measures
# http://nrel.github.io/OpenStudio-user-documentation/reference/measure_writing_guide/

require 'erb'
require 'openstudio-standards'
require 'pathname'
(Pathname.new(__dir__) / 'resources/179d_standards').glob('*.rb').sort_by { |p| p.basename.to_s.size }.each { |f| require f.sub_ext('').to_s } unless defined?(ACM179dASHRAE9012007)

# start the measure
class Reporting179D < OpenStudio::Measure::ReportingMeasure
  # human readable name
  def name
    'reporting 179D'
  end

  # human readable description
  def description
    'tbd'
  end

  # human readable description of modeling approach
  def modeler_description
    'Report measure'
  end

  # define the arguments that the user will input
  def arguments(_model)
    # report measure does not require any user arguments, return an empty list
    args = OpenStudio::Measure::OSArgumentVector.new

    # make an argument for disable_upstream_arg
    disable_upstream_arg = OpenStudio::Measure::OSArgument.makeBoolArgument('disable_upstream_arg', true)
    disable_upstream_arg.setDisplayName('Disable unstream argument search?')
    disable_upstream_arg.setDefaultValue(false)
    args << disable_upstream_arg

    # make an argument for adding warning/severe-error counts
    add_warning_severe_counts = OpenStudio::Measure::OSArgument.makeBoolArgument('add_warning_severe_counts', true)
    add_warning_severe_counts.setDisplayName('Add warning/severe-error counts?')
    add_warning_severe_counts.setDefaultValue(false)
    args << add_warning_severe_counts

    return args
  end

  # optional outputs to be displayed in PAT
  def outputs
    OpenStudio::Measure::OSOutputVector.new
  end

  # extract results from sql file
  def report_sim_output(runner, name, vals, os_units, desired_units, percent_of_val = 1.0)
    total_val = 0.0
    vals.each do |val|
      next if val.empty?

      total_val += val.get * percent_of_val
    end
    if os_units.nil? || desired_units.nil? || os_units == desired_units
      val_in_units = total_val
    else
      val_in_units = OpenStudio.convert(total_val, os_units, desired_units).get
    end
    runner.registerValue(name, val_in_units)
    runner.registerInfo("Registering #{val_in_units.round(2)} for #{name}.")

    return val_in_units
  end

  # calculating percentile
  def percentile(values, percentile)
    values_sorted = values.sort
    k = ((percentile * (values_sorted.length - 1)) + 1).floor - 1
    f = ((percentile * (values_sorted.length - 1)) + 1).modulo(1)

    return values_sorted[k] + (f * (values_sorted[k + 1] - values_sorted[k]))
  end

  # calculate swh energy factor for gas water heater
  def calculate_ef_ng(ua_btu_h_per_F, q_btu_h)
    burner_efficiency = 0.8
    thermal_efficiency = 0.82
    c1 = 67.5
    c2 = 0.0005840268652
    p_on = q_btu_h / burner_efficiency
    re = ((p_on * thermal_efficiency) - (ua_btu_h_per_F * c1)) / p_on
    energy_factor = 1 / ((ua_btu_h_per_F * c1 * (c2 - (1 / re / p_on))) + (1 / re))
    return energy_factor
  end

  # calculate swh energy factor for elec water heater
  def calculate_ef_elec(ua_btu_per_h_per_f)
    energy_factor = 1.0 / ((ua_btu_per_h_per_f * 24.0 * 67.5 / 41_094.0) + 1.0)
    return energy_factor
  end

  # helper method to access report variable data
  def sql_get_report_variable_data_double(runner, sql, object, variable_name)
    value = 0.0
    if object.respond_to?(:name)
      var_data_id_query = "SELECT ReportVariableDataDictionaryIndex FROM ReportVariableDataDictionary WHERE VariableName = '#{variable_name}' AND ReportingFrequency = 'Run Period' AND KeyValue = '#{object.name.get.to_s.upcase}'"
    else
      var_data_id_query = "SELECT ReportVariableDataDictionaryIndex FROM ReportVariableDataDictionary WHERE VariableName = '#{variable_name}' AND ReportingFrequency = 'Run Period' AND KeyValue = '#{object.upcase}'"
    end
    var_data_id = sql.execAndReturnFirstDouble(var_data_id_query)
    if var_data_id.is_initialized
      var_val_query = "SELECT VariableValue FROM ReportVariableData WHERE ReportVariableDataDictionaryIndex = '#{var_data_id.get}'"
      val = sql.execAndReturnFirstDouble(var_val_query)
      if val.is_initialized
        value = val.get
      elsif object.respond_to?(:name)
        runner.registerWarning("'#{variable_name}' not available for #{object.iddObjectType} '#{object.name}'.")
      else
        runner.registerWarning("'#{variable_name}' not available for #{object}'.")
      end
    elsif object.respond_to?(:name)
      runner.registerWarning("'#{variable_name}' not available for #{object.iddObjectType} '#{object.name}'.")
    else
      runner.registerWarning("'#{variable_name}' not available for #{object}'.")
    end
    return value
  end

  # Get the Total Ventilation [m3] from the sql file
  # @param sqlFile [OpenStudio::SqlFile] the sql file to query
  # @param runner [OpenStudio::Measure::OSRunner] the runner to report errors
  # @return [Float] the total outdoor air volume for the facility
  # @raise [RuntimeError] if the query fails or returns no results
  def facility_total_runtime_outdoor_air_volume(sqlFile:, runner: nil)
    # NOTE: there's a "Total Facility" line in the table, but it's rounded
    # So we get the zone values and sum them up
    query = <<~SQL
      SELECT Value FROM TabularDataWithStrings
        WHERE ReportName="OutdoorAirDetails"
          AND TableName="Total Outdoor Air by Zone"
          AND ColumnName="Total Ventilation"
          AND RowName<>"Total Facility"
          AND Units="m3";
    SQL

    vec_ = sqlFile.execAndReturnVectorOfDouble(query)
    unless vec_.is_initialized
      msg = "facility_total_runtime_outdoor_air_volume: Couldn't get vector for query: #{query}"
      if runner
        runner.registerError(msg)
      end
      raise "Couldn't get value for query: #{query_zone}" unless vec_.is_initialized
    end

    return vec_.get.sum
  end

  # Collect the first meaningful failure message from run.log or eplusout.err.
  # Returns an empty string when the simulation completed successfully or when
  # neither file is found.
  #
  # Priority order:
  #   1. run.log: "[openstudio.model.Model] The run did not finish and had following errors: ..."
  #      — the most specific, human-readable summary written by openstudio-standards.
  #   2. eplusout.err: EnergyPlus "** Severe ** / ** Fatal **" lines when terminated.
  #   3. run.log: generic "[timestamp FATAL]" / "<Fatal>" lines as a last resort.
  #
  # @param runner [OpenStudio::Measure::OSRunner]
  # @param eplusout_err_path [String, nil] override path to eplusout.err (used in tests)
  # @param run_log_path [String, nil] override path to run.log (used in tests)
  def collect_simulation_failure_message(runner, eplusout_err_path: nil, run_log_path: nil)
    # Resolve file paths once, using is_initialized (OpenStudio::OptionalPath is NOT a Ruby String/nil).
    unless run_log_path
      lf = runner.workflow.findFile('run/run.log')
      run_log_path = lf.is_initialized ? lf.get.to_s : nil
    end
    unless eplusout_err_path
      ef = runner.workflow.findFile('run/eplusout.err')
      eplusout_err_path = ef.is_initialized ? ef.get.to_s : nil
    end

    # 1. run.log: "[openstudio.model.Model] The run did not finish and had following errors:"
    #    — most specific message, written by openstudio-standards when a sizing run crashes.
    #    Collects that line plus all subsequent consecutive ERROR lines (OSRunner, OSWorkflow)
    #    so the full failure context is preserved in one message.
    if run_log_path && File.exist?(run_log_path)
      found_model_error = false
      collected = []
      first_error_line = nil
      File.foreach(run_log_path, encoding: 'UTF-8', invalid: :replace, undef: :replace) do |line|
        if found_model_error
          break unless line =~ /^\[[\d:.]+ ERROR\]/

          collected << line.sub(/^\[[\d:.]+ \w+\]\s*/, '').strip
        else
          if line =~ /\[openstudio\.model\.Model\].*did not finish.*errors:/i
            found_model_error = true
            collected << line.sub(/^\[[\d:.]+ \w+\]\s*/, '').strip
          elsif first_error_line.nil? && line =~ /^\[[\d:.]+ ERROR\]/
            first_error_line = line.sub(/^\[[\d:.]+ \w+\]\s*/, '').strip
          end
        end
      end
      return collected.join("\n") unless collected.empty?
      return first_error_line unless first_error_line.nil?
    end

    # 2. eplusout.err: extract the first Fatal/Severe line when EnergyPlus terminated.
    if eplusout_err_path && File.exist?(eplusout_err_path)
      lines = File.readlines(eplusout_err_path, encoding: 'UTF-8', invalid: :replace, undef: :replace)
      if lines.any? { |l| l.include?('EnergyPlus Terminated') }
        fatal_lines = lines.grep(/\*\* (Fatal|Severe)\s+\*\*/i)
        return fatal_lines.empty? ? 'EnergyPlus Terminated--Fatal Error Detected' : fatal_lines.first.gsub(/\s*\*+\s*/, ' ').strip
      end
    end

    # 3. run.log: generic FATAL / <Fatal> lines as a last resort.
    if run_log_path && File.exist?(run_log_path)
      File.foreach(run_log_path, encoding: 'UTF-8', invalid: :replace, undef: :replace) do |line|
        return line.strip if line =~ /\bFATAL\b/i
      end
    end

    ''
  end

  # return a vector of IdfObject's to request EnergyPlus objects needed by the run method
  # Warning: Do not change the name of this method to be snake_case. The method must be lowerCamelCase.
  def energyPlusOutputRequests(runner, user_arguments)
    super(runner, user_arguments)

    result = OpenStudio::IdfObjectVector.new
    result << OpenStudio::IdfObject.load('Output:Variable,*,Site Outdoor Air Drybulb Temperature,Hourly;').get

    # Get model
    model = runner.lastOpenStudioModel
    if model.empty?
      runner.registerError('Cannot find last model in energyPlusOutputRequests, cannot request outputs for HVAC equipment.')
      return false
    end
    model = model.get

    # Handle fuel output variables that changed in EnergyPlus version 9.4 (Openstudio version >= 3.1)
    elec = 'Electric'
    # gas = 'Gas'
    # fuel_oil = 'FuelOil#2'
    if model.version > OpenStudio::VersionString.new('3.0.1')
      elec = 'Electricity'
      # gas = 'NaturalGas'
      # fuel_oil = 'FuelOilNo2'
    end

    # request service water heating use
    result << OpenStudio::IdfObject.load('Output:Variable,*,Water Use Connections Hot Water Volume,RunPeriod;').get

    # request coil and fan energy use for HVAC equipment
    # result << OpenStudio::IdfObject.load('Output:Variable,*,Chiller COP,RunPeriod;').get
    # result << OpenStudio::IdfObject.load('Output:Variable,*,Chiller Evaporator Cooling Energy,RunPeriod;').get #J
    # result << OpenStudio::IdfObject.load('Output:Variable,*,Boiler Heating Energy,RunPeriod;').get #J
    # result << OpenStudio::IdfObject.load("Output:Variable,*,Boiler #{elec} Energy,RunPeriod;").get #J
    # result << OpenStudio::IdfObject.load("Output:Variable,*,Boiler #{gas} Energy,RunPeriod;").get #J
    # result << OpenStudio::IdfObject.load("Output:Variable,*,Boiler #{fuel_oil} Energy,RunPeriod;").get #J
    # result << OpenStudio::IdfObject.load("Output:Variable,*,Boiler Propane Energy,RunPeriod;").get #J
    # result << OpenStudio::IdfObject.load("Output:Variable,*,Heat Pump #{elec} Energy,RunPeriod;").get #J
    # result << OpenStudio::IdfObject.load('Output:Variable,*,Heat Pump Load Side Heat Transfer Energy,RunPeriod;').get #J
    # result << OpenStudio::IdfObject.load('Output:Variable,*,Heat Pump Source Side Inlet Temperature,RunPeriod;').get #C
    # result << OpenStudio::IdfObject.load('Output:Variable,*,Fluid Heat Exchanger Loop Supply Side Inlet Temperature,RunPeriod;').get #C
    # result << OpenStudio::IdfObject.load('Output:Variable,*,Fluid Heat Exchanger Loop Supply Side Outlet Temperature,RunPeriod;').get #C
    # result << OpenStudio::IdfObject.load('Output:Variable,*,Fluid Heat Exchanger Loop Demand Side Inlet Temperature,RunPeriod;').get #C
    # result << OpenStudio::IdfObject.load('Output:Variable,*,Fluid Heat Exchanger Loop Demand Side Outlet Temperature,RunPeriod;').get #C
    # result << OpenStudio::IdfObject.load("Output:Variable,*,Fluid Heat Exchanger Heat Transfer Energy,RunPeriod;").get # J
    result << OpenStudio::IdfObject.load("Output:Variable,*,Cooling Coil #{elec} Energy,RunPeriod;").get # J
    # result << OpenStudio::IdfObject.load("Output:Variable,*,Heating Coil #{elec} Energy,RunPeriod;").get # J
    # result << OpenStudio::IdfObject.load("Output:Variable,*,Heating Coil #{gas} Energy,RunPeriod;").get # J
    # result << OpenStudio::IdfObject.load("Output:Variable,*,Heating Coil Defrost #{elec} Energy,RunPeriod;").get # J
    # result << OpenStudio::IdfObject.load('Output:Variable,*,Heating Coil Heating Energy,RunPeriod;').get # J
    result << OpenStudio::IdfObject.load('Output:Variable,*,Cooling Coil Total Cooling Energy,RunPeriod;').get # J
    # result << OpenStudio::IdfObject.load('Output:Variable,*,Zone VRF Air Terminal Total Heating Energy,RunPeriod;').get # J
    # result << OpenStudio::IdfObject.load('Output:Variable,*,Zone VRF Air Terminal Total Cooling Energy,RunPeriod;').get # J
    # result << OpenStudio::IdfObject.load('Output:Variable,*,VRF Heat Pump Cooling Electricity Energy,RunPeriod;').get # J
    # result << OpenStudio::IdfObject.load('Output:Variable,*,VRF Heat Pump Heating Electricity Energy,RunPeriod;').get # J
    # result << OpenStudio::IdfObject.load('Output:Variable,*,VRF Heat Pump Defrost Electricity Energy,RunPeriod;').get # J
    # result << OpenStudio::IdfObject.load('Output:Variable,*,VRF Heat Pump Crankcase Heater Electricity Energy,RunPeriod;').get # J
    # result << OpenStudio::IdfObject.load('Output:Variable,*,VRF Heat Pump Heat Recovery Energy,RunPeriod;').get # J
    result << OpenStudio::IdfObject.load('Output:Variable,*,Air System Outdoor Air Mass Flow Rate,RunPeriod;').get
    result << OpenStudio::IdfObject.load('Output:Variable,*,Air System Mixed Air Mass Flow Rate,RunPeriod;').get # kg/s
    result << OpenStudio::IdfObject.load('Output:Variable,*,Air System Outdoor Air Economizer Status,RunPeriod;').get
    result << OpenStudio::IdfObject.load('Output:Variable,*,System Node Mass Flow Rate,RunPeriod;').get # kg/s, needed for zone HVAC OA node flow tracking
    # result << OpenStudio::IdfObject.load("Output:Variable,*,Water Heater #{elec} Energy,RunPeriod;").get # J
    # result << OpenStudio::IdfObject.load("Output:Variable,*,Water Heater #{gas} Energy,RunPeriod;").get # J
    # result << OpenStudio::IdfObject.load("Output:Variable,*,Water Heater #{fuel_oil} Energy,RunPeriod;").get # J
    # result << OpenStudio::IdfObject.load("Output:Variable,*,Water Heater Propane Energy,RunPeriod;").get # J
    # result << OpenStudio::IdfObject.load('Output:Variable,*,Water Heater Heating Energy,RunPeriod;').get # J
    # result << OpenStudio::IdfObject.load('Output:Variable,*,Water Heater Unmet Demand Heat Transfer Energy,RunPeriod;').get # J
    # if model.version > OpenStudio::VersionString.new('3.3.0')
    #   result << OpenStudio::IdfObject.load('Output:Variable,*,Cooling Coil Total Water Heating Energy,RunPeriod;').get # J
    #   result << OpenStudio::IdfObject.load('Output:Variable,*,Cooling Coil Water Heating Electricity Energy,RunPeriod;').get # J
    # else
    #   result << OpenStudio::IdfObject.load('Output:Variable,*,Heating Coil Total Water Heating Energy,RunPeriod;').get # J
    #   result << OpenStudio::IdfObject.load('Output:Variable,*,Heating Coil Water Heating Electricity Energy,RunPeriod;').get # J
    # end

    # result << OpenStudio::IdfObject.load("Output:Variable,*,Fan #{elec} Energy,RunPeriod;").get # J
    # result << OpenStudio::IdfObject.load("Output:Variable,*,Humidifier #{elec} Energy,RunPeriod;").get # J
    # result << OpenStudio::IdfObject.load("Output:Variable,*,Evaporative Cooler #{elec} Energy,RunPeriod;").get # J
    # result << OpenStudio::IdfObject.load('Output:Variable,*,Baseboard Hot Water Energy,RunPeriod;').get # J
    # result << OpenStudio::IdfObject.load("Output:Variable,*,Baseboard #{elec} Energy,RunPeriod;").get # J

    result << OpenStudio::IdfObject.load('Output:Variable,*,Zone People Occupant Count,Timestep;').get
    result << OpenStudio::IdfObject.load('Output:Variable,*,Zone Heating Setpoint Not Met Time,Timestep;').get
    result << OpenStudio::IdfObject.load('Output:Variable,*,Zone Cooling Setpoint Not Met Time,Timestep,Timestep;').get

    return result
  end

  # define what happens when the measure is run
  def run(runner, user_arguments)
    super(runner, user_arguments)

    # -------------------------------------------------------------------
    # Register simulation failure message FIRST — before any early returns — so
    # it appears in results.csv when EnergyPlus ran but crashed (eplusout.err exists).
    # NOTE: if the workflow fails before EnergyPlus runs, this reporting measure
    # is never invoked at all, so pre-EnergyPlus failures cannot be captured here.
    # -------------------------------------------------------------------
    simulation_failed_message = begin
      collect_simulation_failure_message(runner)
    rescue StandardError => e
      "collect_simulation_failure_message raised: #{e.class}: #{e.message}"
    end
    runner.registerValue('simulation_failed_message', simulation_failed_message)
    runner.registerInfo("simulation_failed_message: #{simulation_failed_message}")

    # -------------------------------------------------------------------
    # puts'### get last model')
    # -------------------------------------------------------------------
    model = runner.lastOpenStudioModel
    if model.empty?
      runner.registerError('Cannot find last model.')
      return false
    end
    model = model.get

    # -------------------------------------------------------------------
    # puts'### use built-in error checking')
    # -------------------------------------------------------------------
    return false unless runner.validateUserArguments(arguments(model), user_arguments)

    # -------------------------------------------------------------------
    # puts'### get arguments')
    # -------------------------------------------------------------------
    # use the built-in error checking
    if !runner.validateUserArguments(arguments(model), user_arguments)
      return false
    end

    disable_upstream_arg = runner.getBoolArgumentValue('disable_upstream_arg', user_arguments)
    add_warning_severe_counts = runner.getBoolArgumentValue('add_warning_severe_counts', user_arguments)

    # -------------------------------------------------------------------
    # puts'### load standard')
    # -------------------------------------------------------------------
    std = Standard.build('179D 90.1-2007')

    # -------------------------------------------------------------------
    # puts'### get sql file from the model')
    # -------------------------------------------------------------------
    sqlFile = runner.lastEnergyPlusSqlFile
    if sqlFile.empty?
      runner.registerError('Cannot find last sql file.')
      return false
    end
    sqlFile = sqlFile.get
    runner.registerInfo("Sql file not empty: #{sqlFile.path}")
    model.setSqlFile(sqlFile)

    # -------------------------------------------------------------------
    # puts'### get enduse consumptions from sql file')
    # reference: https://openstudio-sdk-documentation.s3.amazonaws.com/cpp/OpenStudio-2.7.0-doc/utilities/html/classopenstudio_1_1_sql_file.html
    # -------------------------------------------------------------------
    out_total_electricity_gj = sqlFile.electricityTotalEndUses.to_f
    out_total_electricity_heating_gj = sqlFile.electricityHeating.to_f
    out_total_electricity_cooling_gj = sqlFile.electricityCooling.to_f
    out_total_electricity_fan_gj = sqlFile.electricityFans.to_f
    out_total_electricity_pump_gj = sqlFile.electricityPumps.to_f
    out_total_electricity_heatrejection_gj = sqlFile.electricityHeatRejection.to_f
    out_total_electricity_humidification_gj = sqlFile.electricityHumidification.to_f
    out_total_electricity_heatrecovery_gj = sqlFile.electricityHeatRecovery.to_f
    out_total_electricity_lighting_interior_gj = sqlFile.electricityInteriorLighting.to_f
    out_total_electricity_watersystem_gj = sqlFile.electricityWaterSystems.to_f
    out_total_gas_gj = sqlFile.naturalGasTotalEndUses.to_f
    out_total_gas_heating_gj = sqlFile.naturalGasHeating.to_f
    out_total_gas_cooling_gj = sqlFile.naturalGasCooling.to_f
    out_total_gas_fan_gj = sqlFile.naturalGasFans.to_f
    out_total_gas_pump_gj = sqlFile.naturalGasPumps.to_f
    out_total_gas_heatrejection_gj = sqlFile.naturalGasHeatRejection.to_f
    out_total_gas_humidification_gj = sqlFile.naturalGasHumidification.to_f
    out_total_gas_heatrecovery_gj = sqlFile.naturalGasHeatRecovery.to_f
    out_total_gas_lighting_interior_gj = sqlFile.naturalGasInteriorLighting.to_f
    out_total_gas_watersystem_gj = sqlFile.naturalGasWaterSystems.to_f

    # -------------------------------------------------------------------
    # puts'### initialize 179D related metrics')
    # -------------------------------------------------------------------
    out_total_electricity_179_d_gj = 0 # in GJ
    out_total_gas_179_d_gj = 0 # in GJ

    # -------------------------------------------------------------------
    # puts'### register relevant values: weather parameters')
    # -------------------------------------------------------------------

    # get the weather file run period (as opposed to design day run period)
    ann_env_pd = nil
    sqlFile.availableEnvPeriods.each do |env_pd|
      env_type = sqlFile.environmentType(env_pd)
      if env_type.is_initialized && (env_type.get == OpenStudio::EnvironmentType.new('WeatherRunPeriod'))
        ann_env_pd = env_pd
      end
    end
    if ann_env_pd == false
      runner.registerError('Cannot find a weather runperiod. Make sure you ran an annual simulation, not just the design days.')
      return false
    end

    # -------------------------------------------------------------------
    # puts'### register relevant values: building parameters')
    # reference: https://openstudio-sdk-documentation.s3.amazonaws.com/cpp/OpenStudio-3.4.0-doc/model/html/classopenstudio_1_1model_1_1_building.html
    # reference: https://github.com/NREL/comstock-internal/blob/develop/measures/comstock_sensitivity_reports/measure.rb
    # -------------------------------------------------------------------
    # floor area
    in_floor_area_m_2 = model.getBuilding.floorArea.round(0)
    runner.registerValue('in_floor_area_m_2', in_floor_area_m_2, 'm^2')

    # fully conditioned floor area: zones that are both heated and cooled.
    # Uses OpenstudioStandards::ThermalZone helpers which validate setpoint values
    # (heating max > 41F, cooling min < 91F), handle radiant equipment and staged
    # thermostats, and explicitly exclude heating-only zones. Zone multipliers are applied.
    in_floor_area_fully_conditioned_m_2 = 0.0
    model.getThermalZones.each do |zone|
      heated = OpenstudioStandards::ThermalZone.thermal_zone_heated?(zone)
      cooled = OpenstudioStandards::ThermalZone.thermal_zone_cooled?(zone)
      next unless heated && cooled

      in_floor_area_fully_conditioned_m_2 += zone.floorArea * zone.multiplier.to_f
    end
    runner.registerValue('in_floor_area_fully_conditioned_m_2', in_floor_area_fully_conditioned_m_2.round(2), 'm^2')

    # Collect non-fully-conditioned zones (heated-only, cooled-only, or unconditioned).
    # Used later to subtract their lighting contribution from the 179D electricity total.
    non_fc_zones = model.getThermalZones.reject do |zone|
      OpenstudioStandards::ThermalZone.thermal_zone_heated?(zone) &&
        OpenstudioStandards::ThermalZone.thermal_zone_cooled?(zone)
    end
    runner.registerInfo("Non-FC zones (#{non_fc_zones.size}): #{non_fc_zones.map(&:nameString).join(', ')}")

    # Validate that lighting schedule shapes are consistent between FC and non-FC zones.
    # The proportional-wattage correction used for 179D lighting is only accurate when
    # FC and non-FC zones share similar schedule shapes (i.e., the same fraction of
    # installed watts is running at any given hour).
    if non_fc_zones.any?
      fc_zones = model.getThermalZones.select do |zone|
        OpenstudioStandards::ThermalZone.thermal_zone_heated?(zone) &&
          OpenstudioStandards::ThermalZone.thermal_zone_cooled?(zone)
      end

      fc_light_schedules = fc_zones
                           .flat_map(&:spaces)
                           .flat_map(&:lights)
                           .filter_map { |l| l.schedule.is_initialized ? l.schedule.get.nameString : nil }
                           .uniq.sort
      non_fc_light_schedules = non_fc_zones
                               .flat_map(&:spaces)
                               .flat_map(&:lights)
                               .filter_map { |l| l.schedule.is_initialized ? l.schedule.get.nameString : nil }
                               .uniq.sort

      runner.registerInfo("Lighting schedules in FC zones (#{fc_light_schedules.size}): #{fc_light_schedules.join(', ')}")
      runner.registerInfo("Lighting schedules in non-FC zones (#{non_fc_light_schedules.size}): #{non_fc_light_schedules.join(', ')}")

      exclusive_to_non_fc = non_fc_light_schedules - fc_light_schedules
      if exclusive_to_non_fc.any?
        runner.registerError(
          "Non-FC zones use lighting schedules not present in FC zones: #{exclusive_to_non_fc.join(', ')}. " \
          'The proportional-wattage correction for 179D interior lighting may be inaccurate.'
        )
      else
        runner.registerInfo('Non-FC zone lighting schedules are a subset of FC zone schedules. Proportional-wattage correction is valid.')
      end
    end

    # fully conditioned floor area from simulation results via SqlFile.
    # model.getBuilding.conditionedFloorArea queries the EnergyPlus output directly
    # and is only available after the model has a SqlFile attached.
    conditioned_floor_area_sql = model.getBuilding.conditionedFloorArea
    if conditioned_floor_area_sql.is_initialized
      runner.registerValue('in_floor_area_fully_conditioned_m_2_sql', conditioned_floor_area_sql.get.round(2), 'm^2')
    else
      runner.registerWarning('conditionedFloorArea from SqlFile is not available; skipping in_floor_area_fully_conditioned_m_2_sql.')
    end

    # exteriorSurfaceArea = vertical wall (including windows) + roof area
    in_exterior_wall_window_roof_area_m_2 = model.getBuilding.exteriorSurfaceArea.round(3)
    runner.registerValue('in_exterior_wall_window_roof_area_m_2', in_exterior_wall_window_roof_area_m_2, 'm^2')

    # exteriorWallArea = vertical wall (including windows)
    in_exterior_wall_window_area_m_2 = model.getBuilding.exteriorWallArea.round(3)
    runner.registerValue('in_exterior_wall_window_area_m_2', in_exterior_wall_window_area_m_2, 'm^2')

    # air volume
    in_air_volume_m_3 = model.getBuilding.airVolume
    in_air_volume_ft_3 = (in_air_volume_m_3 * 35.3147).round(2)
    runner.registerValue('in_air_volume_ft_3', in_air_volume_ft_3, 'm^2')

    # LPD
    in_interior_lighting_lpd_w_per_m_2 = model.getBuilding.lightingPowerPerFloorArea.round(3)
    runner.registerValue('in_interior_lighting_lpd_w_per_m_2', in_interior_lighting_lpd_w_per_m_2, 'W/m^2')

    # infiltration
    in_infiltration_ach = model.getBuilding.infiltrationDesignAirChangesPerHour.round(3)
    runner.registerValue('in_infiltration_ach', in_infiltration_ach, '1/h')

    # infiltration
    in_infiltration_m_3_per_sec_m_2 = model.getBuilding.infiltrationDesignFlowPerExteriorWallArea.round(6)
    runner.registerValue('in_infiltration_m_3_per_sec_m_2', in_infiltration_m_3_per_sec_m_2)

    # calculate exterior surface properties
    in_roof_absorptance_times_area = 0
    in_roof_ua_si = 0.0
    in_roof_area_m_2 = 0.0
    in_ext_wall_ua_si = 0.0
    in_exterior_wall_area_m_2 = 0.0

    accepted_window_types = ['window', 'skylight']
    kept_subsurfaces = model.getSubSurfaces.select do |ss|
      accepted_window_types.any? { |x| ss.subSurfaceType.downcase.include?(x) }
    end
    window_properties = kept_subsurfaces.map do |ss|
      std.sub_surface_get_window_property(ss)
    end

    groupby_keys = ['surface_type', 'window_type', 'name']
    window_property = window_properties.group_by { |x| x['surface_type'] }.transform_values do |v1|
      v1.group_by { |x| x['window_type'] }.transform_values do |v2|
        v2.group_by { |x| x['name'] }.transform_values do |v3|
          v3.map { |h| h.except(*groupby_keys) }[0]
        end
      end
    end

    # Get roof/wall/ground properties
    in_roof_absorptance_times_area = 0.0
    in_roof_ua_si = 0.0
    in_roof_area_m_2 = 0.0
    in_ext_wall_ua_si = 0.0
    in_exterior_wall_area_m_2 = 0.0
    in_ground_rvalue_ip_sum = 0.0
    in_ground_ffactor_si_sum = 0.0
    in_ground_area_sum = 0.0

    model.getSpaces.sort.each do |space|
      space.surfaces.each do |surface|
        surface_type = surface.surfaceType.to_s

        if surface.outsideBoundaryCondition == 'Outdoors'
          case surface_type
          when 'RoofCeiling'
            absorptance = surface.exteriorVisibleAbsorptance.is_initialized ? surface.exteriorVisibleAbsorptance.get : 0.0
            u_value_si = surface.uFactor.is_initialized ? surface.uFactor.get : 0.0
            area_m_2 = surface.netArea
            ua_si = u_value_si * area_m_2

            in_roof_absorptance_times_area += absorptance * area_m_2
            in_roof_ua_si += ua_si
            in_roof_area_m_2 += area_m_2

          when 'Wall'
            u_value_si = surface.uFactor.is_initialized ? surface.uFactor.get : 0.0
            area_m_2 = surface.netArea
            ua_si = u_value_si * area_m_2

            in_ext_wall_ua_si += ua_si
            in_exterior_wall_area_m_2 += area_m_2
          end
        end

        if surface_type == 'Floor'
          planarsurface = surface.to_PlanarSurface
          next unless planarsurface.is_initialized

          construction = planarsurface.get.construction
          next unless construction.is_initialized && construction.get.iddObjectType.to_s.include?('OS_Construction_FfactorGroundFloor')

          ff_construction = construction.get.to_FFactorGroundFloorConstruction
          next unless ff_construction.is_initialized && ff_construction.get.perimeterExposed >= 0.0001

          ff = ff_construction.get
          area = ff.area
          f_factor = ff.fFactor
          perim = ff.perimeterExposed

          ff_si = f_factor * perim / area # W/m2-K
          r_si = 1.0 / ff_si              # m2-K/W
          r_ip = r_si * 5.678261          # ft2-h-F/Btu

          in_ground_rvalue_ip_sum += r_ip * area
          in_ground_ffactor_si_sum += f_factor * area
          in_ground_area_sum += area
        end
      end
    end

    # Final ground metrics
    in_ground_rvalue_ip = in_ground_area_sum > 0.0 ? in_ground_rvalue_ip_sum / in_ground_area_sum : 0.0
    in_ground_ffactor_si = in_ground_area_sum > 0.0 ? in_ground_ffactor_si_sum / in_ground_area_sum : 0.0
    in_ground_ffactor_ip = in_ground_ffactor_si / 3.28084 / 1.8 * 3.41
    runner.registerValue('in_ground_rvalue_ip', in_ground_rvalue_ip.round(5))
    runner.registerValue('in_ground_ffactor_ip', in_ground_ffactor_ip.round(5))

    # average roof U-value
    if in_roof_area_m_2 > 0
      in_average_roof_u_value_si = in_roof_ua_si / in_roof_area_m_2
      runner.registerValue('in_roof_area_m_2', in_roof_area_m_2.round(2), 'm^2')
      runner.registerValue('in_average_roof_u_value_si', in_average_roof_u_value_si.round(5))
    else
      runner.registerWarning('Roof area is zero. Cannot calculate average U-value.')
    end

    # average roof absorptance
    if in_roof_area_m_2 > 0
      in_average_roof_absorptance = in_roof_absorptance_times_area / in_roof_area_m_2
      runner.registerValue('in_average_roof_absorptance', in_average_roof_absorptance.round(5))
    else
      runner.registerWarning('Roof area is zero. Cannot calculate average absorptance.')
    end

    # average wall U-value
    if in_exterior_wall_area_m_2 > 0
      in_average_ext_wall_u_value_si = in_ext_wall_ua_si / in_exterior_wall_area_m_2
      runner.registerValue('in_exterior_wall_area_m_2', in_exterior_wall_area_m_2.round(5), 'm^2')
      runner.registerValue('in_average_ext_wall_u_value_si', in_average_ext_wall_u_value_si.round(5), 'W/m^2*K')
    else
      runner.registerWarning('Exterior wall area is zero. Cannot calculate average U-value.')
    end

    # total window area
    var_val_query = "SELECT Value FROM TabularDataWithStrings WHERE ReportName = 'EnvelopeSummary' AND ReportForString = 'Entire Facility' AND TableName = 'Exterior Fenestration' AND RowName = 'Total or Average' AND ColumnName = 'Area of Multiplied Openings' AND Units = 'm2'"
    val = sqlFile.execAndReturnFirstDouble(var_val_query)
    if val.is_initialized
      in_window_area_m_2 = val.get
      runner.registerValue('in_window_area_m_2', in_window_area_m_2.round(5))
    else
      runner.registerWarning('Overall window area not available.')
    end

    # Average window U-value
    var_val_query = "SELECT Value FROM TabularDataWithStrings WHERE ReportName = 'EnvelopeSummary' AND ReportForString = 'Entire Facility' AND TableName = 'Exterior Fenestration' AND RowName = 'Total or Average' AND ColumnName = 'Glass U-Factor' AND Units = 'W/m2-K'"
    val = sqlFile.execAndReturnFirstDouble(var_val_query)
    if val.is_initialized
      in_window_u_value_w_per_m_2_k_overall = val.get
      runner.registerValue('in_window_u_value_w_per_m_2_k_overall', in_window_u_value_w_per_m_2_k_overall.round(5))
    else
      runner.registerWarning('Overall average window U-value not available.')
    end

    # Average window SHGC
    var_val_query = "SELECT Value FROM TabularDataWithStrings WHERE ReportName = 'EnvelopeSummary' AND ReportForString = 'Entire Facility' AND TableName = 'Exterior Fenestration' AND RowName = 'Total or Average' AND ColumnName = 'Glass SHGC'"
    val = sqlFile.execAndReturnFirstDouble(var_val_query)
    if val.is_initialized
      in_window_shgc_overall = val.get
      runner.registerValue('in_window_shgc_overall', in_window_shgc_overall.round(5))
    else
      runner.registerWarning('Overall average window SHGC not available.')
    end

    # Average window VLT
    var_val_query = "SELECT Value FROM TabularDataWithStrings WHERE ReportName = 'EnvelopeSummary' AND ReportForString = 'Entire Facility' AND TableName = 'Exterior Fenestration' AND RowName = 'Total or Average' AND ColumnName = 'Glass Visible Transmittance'"
    val = sqlFile.execAndReturnFirstDouble(var_val_query)
    if val.is_initialized
      in_window_vlt = val.get
      runner.registerValue('in_window_vlt', in_window_vlt.round(5))
    else
      runner.registerWarning('Overall average window VLT not available.')
    end

    # window properties
    window_label_hash = {
      'RoofCeiling' => 'skylight',
      'Wall' => 'verticalwall',
    }
    ['RoofCeiling', 'Wall'].each do |surface_type|
      total_area = 0
      weighted_sum_shgc = 0
      weighted_sum_u_value = 0
      unless window_property[surface_type].nil? || window_property[surface_type].empty?
        window_property[surface_type]['FixedWindow'].each do |_surface_name, properties|
          total_area += properties['area_m2']
          weighted_sum_shgc += properties['shgc'] * properties['area_m2']
          weighted_sum_u_value += properties['u_value'] * properties['area_m2']
        end
        weighted_shgc = (weighted_sum_shgc / total_area).round(3)
        weighted_u_value = (weighted_sum_u_value / total_area).round(3)
        runner.registerValue("in_window_shgc_#{window_label_hash[surface_type]}", weighted_shgc.round(5))
        runner.registerValue("in_window_u_value_w_per_m_2_k_#{window_label_hash[surface_type]}", weighted_u_value.round(5))
      end
    end

    # building window to wall ratio
    var_val_query = "SELECT Value FROM TabularDataWithStrings WHERE ReportName = 'InputVerificationandResultsSummary' AND ReportForString = 'Entire Facility' AND TableName = 'Window-Wall Ratio' AND RowName = 'Gross Window-Wall Ratio' AND ColumnName = 'Total'"
    val = sqlFile.execAndReturnFirstDouble(var_val_query)
    if val.is_initialized
      in_window_wwr_overall = val.get / 100.0
      runner.registerValue('in_window_wwr_overall', in_window_wwr_overall)
    else
      runner.registerWarning('Overall window to wall ratio not available.')
    end

    # building skylight
    var_val_query = "SELECT Value FROM TabularDataWithStrings WHERE ReportName = 'InputVerificationandResultsSummary' AND ReportForString = 'Entire Facility' AND TableName = 'Skylight-Roof Ratio' AND RowName = 'Skylight-Roof Ratio' AND ColumnName = 'Total'"
    val = sqlFile.execAndReturnFirstDouble(var_val_query)
    if val.is_initialized
      in_window_wwr_skylight = val.get / 100.0
      runner.registerValue('in_window_wwr_skylight', in_window_wwr_skylight.round(5))
    else
      runner.registerWarning('Overall window to wall ratio for skylight not available.')
    end

    # get design SAT stpt
    design_sat_stpt_cooling_weighted = 0.0
    design_sat_stpt_heating_weighted = 0.0
    count = 0.0
    model.getSizingZones.each do |sizingzone|
      count += 1
      design_sat_stpt_cooling_temp = sizingzone.zoneCoolingDesignSupplyAirTemperature
      design_sat_stpt_heating_temp = sizingzone.zoneHeatingDesignSupplyAirTemperature
      design_sat_stpt_cooling_weighted += design_sat_stpt_cooling_temp
      design_sat_stpt_heating_weighted += design_sat_stpt_heating_temp
    end
    in_hvac_controls_design_cooling_supply_air_temperature = count > 0.0 ? design_sat_stpt_cooling_weighted / count : 0.0
    in_hvac_controls_design_heating_supply_air_temperature = count > 0.0 ? design_sat_stpt_heating_weighted / count : 0.0
    runner.registerValue('in_hvac_controls_design_cooling_supply_air_temperature', in_hvac_controls_design_cooling_supply_air_temperature.round(1))
    runner.registerValue('in_hvac_controls_design_heating_supply_air_temperature', in_hvac_controls_design_heating_supply_air_temperature.round(1))

    # Calculate fraction of building area with different air loop features
    building_zone_area_m2 = model.getThermalZones.sort.sum(&:floorArea)

    number_of_air_loops = 0.0
    number_of_air_loops_with_dcv = 0.0
    number_of_air_loops_with_economizer = 0.0
    number_of_air_loops_with_heat_recovery = 0.0

    building_area_with_dcv_m2 = 0.0
    building_area_with_economizer_m2 = 0.0
    building_area_with_heat_recovery_m2 = 0.0
    building_area_with_motorized_oa_damper_m2 = 0.0
    building_area_with_mz_vav_optimization_m2 = 0.0
    building_area_with_supply_air_temperature_reset_m2 = 0.0

    air_density_kg_per_m_3 = 1.2 # kg/m3

    air_system_total_oa_mass_flow_kg_s = 0.0
    air_system_total_mass_flow_kg_s = 0.0
    air_system_weighted_fan_efficiency = 0.0
    air_system_total_fan_power = 0.0

    in_hvac_controls_design_supply_air_flow_total_m_3_per_s = 0.0
    in_hvac_controls_design_outdoor_air_supply_flow_total = 0.0
    in_hvac_controls_herv_effectiveness_latent_heating = 0.0
    in_hvac_controls_herv_effectiveness_latent_cooling = 0.0
    in_hvac_controls_herv_effectiveness_sensible_heating = 0.0
    in_hvac_controls_herv_effectiveness_sensible_cooling = 0.0

    economizer_statistics = []

    model.getAirLoopHVACs.sort.each do |air_loop_hvac|
      has_economizer = false
      has_dcv = false
      has_mz_vav_optimization = false
      has_supply_air_temp_reset = false
      has_motorized_oa_damper = false

      # fraction with heat recovery
      has_heat_recovery = std.air_loop_hvac_energy_recovery?(air_loop_hvac)

      # fraction with DCV and economizer
      if air_loop_hvac.airLoopHVACOutdoorAirSystem.is_initialized
        oa_system = air_loop_hvac.airLoopHVACOutdoorAirSystem.get
        controller_oa = oa_system.getControllerOutdoorAir
        economizer_type = controller_oa.getEconomizerControlType
        controller_mv = controller_oa.controllerMechanicalVentilation

        has_economizer = true unless economizer_type == 'NoEconomizer'
        has_dcv = true if controller_mv.demandControlledVentilation == true

        if controller_oa.minimumOutdoorAirSchedule.is_initialized
          min_oa_sch = controller_oa.minimumOutdoorAirSchedule.get
          has_motorized_oa_damper = true unless min_oa_sch == model.alwaysOnDiscreteSchedule
        end

        if std.air_loop_hvac_multizone_vav_system?(air_loop_hvac)
          oa_method = controller_mv.systemOutdoorAirMethod
          has_mz_vav_optimization = true if oa_method.include?('VentilationRateProcedure')
        end
      end

      # SAT reset
      oa_node = air_loop_hvac.supplyOutletNode
      oa_node.setpointManagers.each do |spm|
        if spm.to_SetpointManagerWarmest.is_initialized
          has_supply_air_temp_reset = true
        end
      end

      # air loop area
      air_loop_area_m2 = 0.0
      air_loop_hvac.thermalZones.sort.each do |zone|
        air_loop_area_m2 += zone.floorArea
      end

      number_of_air_loops += 1.0
      number_of_air_loops_with_dcv += 1.0 if has_dcv
      number_of_air_loops_with_economizer += 1.0 if has_economizer
      number_of_air_loops_with_heat_recovery += 1.0 if has_heat_recovery

      building_area_with_dcv_m2 += air_loop_area_m2 if has_dcv
      building_area_with_economizer_m2 += air_loop_area_m2 if has_economizer
      building_area_with_heat_recovery_m2 += air_loop_area_m2 if has_heat_recovery
      building_area_with_motorized_oa_damper_m2 += air_loop_area_m2 if has_motorized_oa_damper
      building_area_with_mz_vav_optimization_m2 += air_loop_area_m2 if has_mz_vav_optimization
      building_area_with_supply_air_temperature_reset_m2 += air_loop_area_m2 if has_supply_air_temp_reset
    end

    # Air system properties
    in_hvac_controls_controller_outdoor_air_minimum_outdoor_air_flow_rate_total = 0.0
    in_hvac_controls_controller_outdoor_air_maximum_outdoor_air_flow_rate_total = 0.0
    in_hvac_controls_total_floor_area_served_by_airloop = 0.0
    in_hvac_controls_total_floor_area_served_by_dcv = 0.0
    in_hvac_controls_num_airloop = 0
    in_hvac_controls_num_airloop_served_by_dcv = 0
    economizer_statistics = []
    model.getAirLoopHVACs.sort.each do |air_loop_hvac|
      in_hvac_controls_num_airloop += 1
      in_hvac_controls_total_floor_area_served_by_airloop += air_loop_hvac.thermalZones.sum do |zone|
        zone.spaces.select(&:partofTotalFloorArea).sum(&:floorArea)
      end
      # get Air System Outdoor Air Mass Flow Rate
      air_loop_oa_mass_flow_rate_kg_s = sql_get_report_variable_data_double(runner, sqlFile, air_loop_hvac, 'Air System Outdoor Air Mass Flow Rate')

      # get Air System Mixed Air Mass Flow Rate
      air_loop_mass_flow_rate_kg_s = sql_get_report_variable_data_double(runner, sqlFile, air_loop_hvac, 'Air System Mixed Air Mass Flow Rate')

      # initialize parameters
      fan_efficiency = 0
      fan_power = 0
      design_flow = 0
      oa_design_flow_rate = 0
      herv_effectiveness_latent_heating = 0
      herv_effectiveness_latent_cooling = 0
      herv_effectiveness_sensible_heating = 0
      herv_effectiveness_sensible_cooling = 0

      # get design air flow
      if air_loop_hvac.autosizedDesignSupplyAirFlowRate.is_initialized
        design_flow = air_loop_hvac.autosizedDesignSupplyAirFlowRate.get
      elsif air_loop_hvac.designSupplyAirFlowRate.is_initialized
        design_flow = air_loop_hvac.designSupplyAirFlowRate.get
      else
        runner.registerError("Cannot get design supply flow rate from air loop hvac '#{air_loop_hvac.nameString}'.")
        return false
      end

      # get oa design air flow
      if air_loop_hvac.airLoopHVACOutdoorAirSystem.is_initialized
        sizing_system = air_loop_hvac.sizingSystem
        if sizing_system.designOutdoorAirFlowRate.is_initialized
          oa_design_flow_rate = sizing_system.designOutdoorAirFlowRate.get
        else
          runner.registerError("Cannot get OA design supply flow rate from air loop hvac '#{air_loop_hvac.nameString}'.")
          return false
        end

        air_loop_hvac_oasys = air_loop_hvac.airLoopHVACOutdoorAirSystem.get
        controller_oa = air_loop_hvac_oasys.getControllerOutdoorAir
        controller_mv = controller_oa.controllerMechanicalVentilation

        oa_min_flow_rate = 0.0
        if controller_oa.autosizedMinimumOutdoorAirFlowRate.is_initialized
          oa_min_flow_rate = controller_oa.autosizedMinimumOutdoorAirFlowRate.get
        elsif controller_oa.minimumOutdoorAirFlowRate.is_initialized
          oa_min_flow_rate = controller_oa.minimumOutdoorAirFlowRate.get
        elsif controller_oa.isMinimumOutdoorAirFlowRateAutosized && controller_mv.demandControlledVentilation
          # OS SDK FT will write this as 0.0, hence why it's not retrievable from the SQL
          oa_min_flow_rate = 0.0
        else
          runner.registerError("Cannot get OA min supply flow rate from air loop hvac '#{air_loop_hvac.name}'.")
          return false
        end
        in_hvac_controls_controller_outdoor_air_minimum_outdoor_air_flow_rate_total += oa_min_flow_rate

        oa_max_flow_rate = 0.0
        if controller_oa.autosizedMaximumOutdoorAirFlowRate.is_initialized
          oa_max_flow_rate = controller_oa.autosizedMaximumOutdoorAirFlowRate.get
        elsif controller_oa.maximumOutdoorAirFlowRate.is_initialized
          oa_max_flow_rate = controller_oa.maximumOutdoorAirFlowRate.get
        else
          runner.registerError("Cannot get OA max supply flow rate from air loop hvac '#{air_loop_hvac.name}'.")
          return false
        end
        in_hvac_controls_controller_outdoor_air_maximum_outdoor_air_flow_rate_total += oa_max_flow_rate

        if controller_mv.demandControlledVentilation
          in_hvac_controls_num_airloop_served_by_dcv += 1
          air_loop_hvac.thermalZones.each do |zone|
            zone.spaces.each do |space|
              next unless space.partofTotalFloorArea

              dsoa = space.designSpecificationOutdoorAir
              next if dsoa.empty?

              dsoa = dsoa.get

              next if dsoa.outdoorAirFlowperPerson == 0

              in_hvac_controls_total_floor_area_served_by_dcv += space.floorArea
            end
          end
        end

        # H/ERV effectiveness
        # Get the outdoor air system from the air loop
        air_loop_hvac_oasys = air_loop_hvac.airLoopHVACOutdoorAirSystem.get
        air_loop_hvac_oasys.oaComponents.each do |oa_comp|
          if oa_comp.to_HeatExchangerAirToAirSensibleAndLatent.is_initialized
            herv = oa_comp.to_HeatExchangerAirToAirSensibleAndLatent.get
            herv_effectiveness_latent_heating = herv.latentEffectivenessat100HeatingAirFlow
            herv_effectiveness_latent_cooling = herv.latentEffectivenessat100CoolingAirFlow
            herv_effectiveness_sensible_heating = herv.sensibleEffectivenessat100HeatingAirFlow
            herv_effectiveness_sensible_cooling = herv.sensibleEffectivenessat100CoolingAirFlow
          end
        end
      end

      # get fan metrics
      supply_fan = air_loop_hvac.supplyFan
      if supply_fan.is_initialized
        supply_fan = supply_fan.get

        if supply_fan.to_FanOnOff.is_initialized
          supply_fan = supply_fan.to_FanOnOff.get
          fan_efficiency = supply_fan.fanTotalEfficiency
          mass_flow_rate_kg_per_s = supply_fan.maximumFlowRate.get
          pressure_rise_pa = supply_fan.pressureRise
          fan_power = (mass_flow_rate_kg_per_s * pressure_rise_pa) / (fan_efficiency * air_density_kg_per_m_3)

        elsif supply_fan.to_FanConstantVolume.is_initialized
          supply_fan = supply_fan.to_FanConstantVolume.get
          fan_efficiency = supply_fan.fanTotalEfficiency
          mass_flow_rate_kg_per_s = supply_fan.maximumFlowRate.get
          pressure_rise_pa = supply_fan.pressureRise
          fan_power = (mass_flow_rate_kg_per_s * pressure_rise_pa) / (fan_efficiency * air_density_kg_per_m_3)

        elsif supply_fan.to_FanVariableVolume.is_initialized
          supply_fan = supply_fan.to_FanVariableVolume.get
          fan_efficiency = supply_fan.fanTotalEfficiency
          c1 = supply_fan.fanPowerCoefficient1.get
          c2 = supply_fan.fanPowerCoefficient2.get
          c3 = supply_fan.fanPowerCoefficient3.get
          c4 = supply_fan.fanPowerCoefficient4.get
          c5 = supply_fan.fanPowerCoefficient5.get
          plr = c1 + (c2 * 1) + (c3 * (1**2)) + (c4 * (1**3)) + (c5 * (1**4)) # assuming flow fraction as 1
          mass_flow_rate_kg_per_s = supply_fan.maximumFlowRate.get
          pressure_rise_pa = supply_fan.pressureRise
          fan_power = plr * (mass_flow_rate_kg_per_s * pressure_rise_pa) / (fan_efficiency * air_density_kg_per_m_3)

        else
          runner.registerWarning("Supply Fan type not recognized for air loop hvac '#{air_loop_hvac.name}'.")
        end
      else
        unless std.air_loop_hvac_unitary_system?(air_loop_hvac)
          runner.registerWarning("Supply Fan not available for air loop hvac '#{air_loop_hvac.name}'.")
        end
      end

      # record economizer details
      if air_loop_hvac.airLoopHVACOutdoorAirSystem.is_initialized
        oa_system = air_loop_hvac.airLoopHVACOutdoorAirSystem.get
        controller_oa = oa_system.getControllerOutdoorAir
        economizer_type = controller_oa.getEconomizerControlType
      else
        economizer_type = 'NoEconomizer'
      end

      economizer_high_limit_temperature_c = nil
      economizer_high_limit_enthalpy_j_per_kg = nil

      case economizer_type
      when 'NoEconomizer'
        # no action
      when 'FixedDryBulb', 'FixedEnthalpy', 'DifferentialDryBulb', 'DifferentialEnthalpy'
        if controller_oa.getEconomizerMaximumLimitDryBulbTemperature.is_initialized
          economizer_high_limit_temperature_c = controller_oa.getEconomizerMaximumLimitDryBulbTemperature.get
        end
        if controller_oa.getEconomizerMaximumLimitEnthalpy.is_initialized
          economizer_high_limit_enthalpy_j_per_kg = controller_oa.getEconomizerMaximumLimitEnthalpy.get
        end
      else
        runner.registerWarning("Economizer type '#{economizer_type}' not supported by output measure.")
      end

      # record economizer statistics
      unless economizer_type == 'NoEconomizer'
        economizer_statistics << {
          air_loop_mass_flow_rate_kg_s: design_flow,
          economizer_type:,
          economizer_high_limit_temperature_c:,
          economizer_high_limit_enthalpy_j_per_kg:,
        }
      end

      # add to weighted sums
      air_system_total_mass_flow_kg_s += air_loop_mass_flow_rate_kg_s
      air_system_total_oa_mass_flow_kg_s += air_loop_oa_mass_flow_rate_kg_s
      air_system_weighted_fan_efficiency += fan_efficiency * design_flow
      air_system_total_fan_power += fan_power
      in_hvac_controls_design_supply_air_flow_total_m_3_per_s += design_flow
      in_hvac_controls_design_outdoor_air_supply_flow_total += oa_design_flow_rate
      in_hvac_controls_herv_effectiveness_latent_heating += herv_effectiveness_latent_heating * design_flow
      in_hvac_controls_herv_effectiveness_latent_cooling += herv_effectiveness_latent_cooling * design_flow
      in_hvac_controls_herv_effectiveness_sensible_heating += herv_effectiveness_sensible_heating * design_flow
      in_hvac_controls_herv_effectiveness_sensible_cooling += herv_effectiveness_sensible_cooling * design_flow
    end

    # Loop through zone hvac equipment and add to outdoor air flow rates
    model.getThermalZones.sort.each do |zone|
      zone.equipment.each do |zone_equipment|
        zone_hvac = nil

        # Handle zone HVAC equipment OA flow rates
        handled = false
        obj_types_to_check = [
          :to_ZoneHVACPackagedTerminalAirConditioner,
          :to_ZoneHVACPackagedTerminalHeatPump,
          :to_ZoneHVACWaterToAirHeatPump,
          :to_ZoneHVACFourPipeFanCoil,
          :to_ZoneHVACTerminalUnitVariableRefrigerantFlow
        ]
        obj_types_to_check.each do |meth|
          if zone_equipment.respond_to?(meth) && zone_equipment.send(meth).is_initialized
            zone_hvac = zone_equipment.send(meth).get

            # Special case for Fan Coil units
            if meth == :to_ZoneHVACFourPipeFanCoil
              oa_rate = zone_hvac.maximumOutdoorAirFlowRate
            else
              oa_rate = zone_hvac.outdoorAirFlowRateDuringCoolingOperation
            end

            if oa_rate.is_initialized
              in_hvac_controls_design_outdoor_air_supply_flow_total += oa_rate.get
            elsif meth == :to_ZoneHVACFourPipeFanCoil &&
                  zone_hvac.respond_to?(:isMaximumOutdoorAirFlowRateAutosized) &&
                  zone_hvac.isMaximumOutdoorAirFlowRateAutosized
              # FanCoil autosized fallback: maximumOutdoorAirFlowRate is unset when autosized
              if zone_hvac.respond_to?(:autosizedMaximumOutdoorAirFlowRate)
                autosized_oa_rate = zone_hvac.autosizedMaximumOutdoorAirFlowRate
                if autosized_oa_rate.is_initialized
                  in_hvac_controls_design_outdoor_air_supply_flow_total += autosized_oa_rate.get
                end
              end
            elsif zone_hvac.respond_to?(:autosizedCoolingOutdoorAirFlowRate)
              autosized_oa_rate = zone_hvac.autosizedCoolingOutdoorAirFlowRate
              if autosized_oa_rate.is_initialized
                in_hvac_controls_design_outdoor_air_supply_flow_total += autosized_oa_rate.get
              end
            else
              runner.registerError("Cannot get outdoor air flow rate for Zone '#{zone.nameString}' from zone hvac '#{zone_hvac.nameString}'.")
              return false
            end
            handled = true
            break
          end
        end

        unless handled
          if zone_equipment.to_ZoneVentilationDesignFlowRate.is_initialized
            zone_hvac = zone_equipment.to_ZoneVentilationDesignFlowRate.get
            zone = zone_hvac.thermalZone.get
            area = zone.floorArea
            volume = zone.airVolume
            people = zone.numberOfPeople
            method = zone_hvac.designFlowRateCalculationMethod
            flow_rate = case method
            when 'Flow/Area'
              zone_hvac.flowRateperZoneFloorArea.to_f * area
            when 'Flow/Person'
              zone_hvac.flowRateperPerson.to_f * people
            when 'AirChanges/Hour'
              zone_hvac.airChangesperHour.to_f * volume / 3600.0
            when 'Flow/Zone'
              zone_hvac.designFlowRate.to_f
            else
              runner.registerWarning("Unknown ventilation method: #{method}")
              0.0
                        end
            in_hvac_controls_design_outdoor_air_supply_flow_total += flow_rate
          elsif zone_equipment.to_FanZoneExhaust.is_initialized
            runner.registerWarning("Zone HVAC FanZoneExhaust equipment type is not supported by output measure for Zone '#{zone.nameString}': #{zone_equipment.briefDescription}. Skipping. ")
          elsif zone_equipment.to_ZoneHVACBaseboardConvectiveElectric.is_initialized
            runner.registerWarning("Zone HVAC ZoneHVACBaseboardConvectiveElectric equipment type is not supported by output measure for Zone '#{zone.nameString}': #{zone_equipment.briefDescription}. Skipping.")
          elsif zone_equipment.to_ZoneHVACUnitHeater.is_initialized
            runner.registerWarning("Zone HVAC equipment type unit heater is not supported by output measure for Zone '#{zone.nameString}': #{zone_equipment.briefDescription}. Skipping.")
          elsif zone_equipment.to_AirLoopHVACUnitarySystem.is_initialized
            runner.registerWarning("Zone HVAC equipment type air loop HVAC unitary system is not supported by output measure for Zone '#{zone.nameString}': #{zone_equipment.briefDescription}. Skipping.")
          elsif zone_equipment.to_HVACComponent.get.airLoopHVAC.is_initialized
            runner.registerWarning("Zone HVAC equipment type air loop component is not supported by output measure for Zone '#{zone.nameString}': #{zone_equipment.briefDescription}. Skipping.")
          else
            runner.registerError("Zone HVAC equipment type not recognized or unsupported for Zone '#{zone.nameString}': #{zone_equipment.briefDescription}. Please add it.")
            return false
          end
        end
      end
    end
    building_area_fraction_with_dcv = building_area_with_dcv_m2 / building_zone_area_m2
    building_area_fraction_with_economizer = building_area_with_economizer_m2 / building_zone_area_m2
    building_area_fraction_with_heat_recovery = building_area_with_heat_recovery_m2 / building_zone_area_m2
    building_area_fraction_with_motorized_oa_damper = building_area_with_motorized_oa_damper_m2 / building_zone_area_m2
    building_area_fraction_with_mz_vav_optimization = building_area_with_mz_vav_optimization_m2 / building_zone_area_m2
    building_area_fraction_with_supply_air_temperature_reset = building_area_with_supply_air_temperature_reset_m2 / building_zone_area_m2

    runner.registerValue('in_hvac_area_fraction_with_dcv', building_area_fraction_with_dcv.round(5))
    runner.registerValue('in_hvac_area_fraction_with_economizer', building_area_fraction_with_economizer.round(5))
    runner.registerValue('in_hvac_area_fraction_with_heat_recovery', building_area_fraction_with_heat_recovery.round(5))
    runner.registerValue('in_hvac_area_fraction_with_motorized_oa_damper', building_area_fraction_with_motorized_oa_damper.round(5))
    runner.registerValue('in_hvac_area_fraction_with_mz_vav_optimization', building_area_fraction_with_mz_vav_optimization.round(5))
    runner.registerValue('in_hvac_area_fraction_with_supply_air_temperature_reset', building_area_fraction_with_supply_air_temperature_reset.round(5))

    # calculate economizer variables
    if economizer_statistics.empty?
      runner.registerInfo('No economizer present in the model.')
      runner.registerValue('in_hvac_controls_economizer_control_type', 'NoEconomizer')
    else
      economizer_type_hash = economizer_statistics.group_by { |ec| ec[:economizer_type] }
      economizer_type_areas = economizer_type_hash.map { |x, y| [x, y.inject(0) { |sum, i| sum + i[:air_loop_mass_flow_rate_kg_s] }] }
      largest_economizer_type = economizer_type_areas.max_by { |_k, v| v }
      runner.registerValue('in_hvac_controls_economizer_control_type', largest_economizer_type[0])
    end
    temperature_limited_hash = economizer_statistics.reject { |ec| ec[:economizer_high_limit_temperature_c].nil? }
    enthalpy_limited_hash = economizer_statistics.reject { |ec| ec[:economizer_high_limit_enthalpy_j_per_kg].nil? }
    if temperature_limited_hash.empty?
      in_hvac_controls_economizer_high_limit_shutoff_t_c = -999
    else
      in_hvac_controls_economizer_high_limit_shutoff_t_c = 0.0
      weighted_economizer_high_limit_temperature_c_flow_rate_kg_s = 0.0
      temperature_limited_hash.each do |ec|
        weighted_economizer_high_limit_temperature_c_flow_rate_kg_s += ec[:air_loop_mass_flow_rate_kg_s]
        in_hvac_controls_economizer_high_limit_shutoff_t_c += ec[:economizer_high_limit_temperature_c] * ec[:air_loop_mass_flow_rate_kg_s]
      end
      in_hvac_controls_economizer_high_limit_shutoff_t_c /= weighted_economizer_high_limit_temperature_c_flow_rate_kg_s
    end
    if enthalpy_limited_hash.empty?
      in_hvac_controls_economizer_high_limit_shutoff_h_j_per_kg = -999
    else
      in_hvac_controls_economizer_high_limit_shutoff_h_j_per_kg = 0.0
      weighted_economizer_high_limit_enthalpy_j_per_flow_rate_kg_s = 0.0
      enthalpy_limited_hash.each do |ec|
        weighted_economizer_high_limit_enthalpy_j_per_flow_rate_kg_s += ec[:air_loop_mass_flow_rate_kg_s]
        in_hvac_controls_economizer_high_limit_shutoff_h_j_per_kg += ec[:economizer_high_limit_enthalpy_j_per_kg] * ec[:air_loop_mass_flow_rate_kg_s]
      end
      in_hvac_controls_economizer_high_limit_shutoff_h_j_per_kg /= weighted_economizer_high_limit_enthalpy_j_per_flow_rate_kg_s
    end
    in_hvac_central_fan_max_design_flow_weighted_efficiency = in_hvac_controls_design_supply_air_flow_total_m_3_per_s > 0.0 ? air_system_weighted_fan_efficiency / in_hvac_controls_design_supply_air_flow_total_m_3_per_s : 0.0
    in_hvac_controls_herv_effectiveness_latent_heating = in_hvac_controls_design_supply_air_flow_total_m_3_per_s > 0.0 ? in_hvac_controls_herv_effectiveness_latent_heating / in_hvac_controls_design_supply_air_flow_total_m_3_per_s : 0.0
    in_hvac_controls_herv_effectiveness_latent_cooling = in_hvac_controls_design_supply_air_flow_total_m_3_per_s > 0.0 ? in_hvac_controls_herv_effectiveness_latent_cooling / in_hvac_controls_design_supply_air_flow_total_m_3_per_s : 0.0
    in_hvac_controls_herv_effectiveness_sensible_heating = in_hvac_controls_design_supply_air_flow_total_m_3_per_s > 0.0 ? in_hvac_controls_herv_effectiveness_sensible_heating / in_hvac_controls_design_supply_air_flow_total_m_3_per_s : 0.0
    in_hvac_controls_herv_effectiveness_sensible_cooling = in_hvac_controls_design_supply_air_flow_total_m_3_per_s > 0.0 ? in_hvac_controls_herv_effectiveness_sensible_cooling / in_hvac_controls_design_supply_air_flow_total_m_3_per_s : 0.0
    runner.registerValue('in_hvac_central_fan_max_design_flow_weighted_efficiency', in_hvac_central_fan_max_design_flow_weighted_efficiency.round(5))
    runner.registerValue('in_hvac_central_fan_total_power_w', air_system_total_fan_power.round(5))
    runner.registerValue('in_hvac_controls_design_outdoor_air_supply_flow_total', in_hvac_controls_design_outdoor_air_supply_flow_total.round(5))
    runner.registerValue('in_hvac_controls_herv_effectiveness_latent_heating', in_hvac_controls_herv_effectiveness_latent_heating.round(5))
    runner.registerValue('in_hvac_controls_herv_effectiveness_latent_cooling', in_hvac_controls_herv_effectiveness_latent_cooling.round(5))
    runner.registerValue('in_hvac_controls_herv_effectiveness_sensible_heating', in_hvac_controls_herv_effectiveness_sensible_heating.round(5))
    runner.registerValue('in_hvac_controls_herv_effectiveness_sensible_cooling', in_hvac_controls_herv_effectiveness_sensible_cooling.round(5))
    runner.registerValue('in_hvac_controls_economizer_high_limit_shutoff_t_c', in_hvac_controls_economizer_high_limit_shutoff_t_c.round(5))
    runner.registerValue('in_hvac_controls_economizer_high_limit_shutoff_h_j_per_kg', in_hvac_controls_economizer_high_limit_shutoff_h_j_per_kg.round(5))

    in_hvac_controls_designspecification_outdoorair_total_flow_rate = 0.0
    model.getThermalZones.each do |zone|
      in_hvac_controls_designspecification_outdoorair_total_flow_rate += OpenstudioStandards::ThermalZone.thermal_zone_get_outdoor_airflow_rate(zone) * zone.multiplier.to_f
    end
    runner.registerValue('in_hvac_controls_designspecification_outdoorair_total_flow_rate', in_hvac_controls_designspecification_outdoorair_total_flow_rate.round(5), 'm^3/s')
    runner.registerValue('in_hvac_controls_total_floor_area_served_by_airloop', in_hvac_controls_total_floor_area_served_by_airloop.round(5), 'm^2')
    runner.registerValue('in_hvac_controls_total_floor_area_served_by_dcv', in_hvac_controls_total_floor_area_served_by_dcv.round(5), 'm^2')
    runner.registerValue('in_hvac_controls_num_airloop', in_hvac_controls_num_airloop)
    runner.registerValue('in_hvac_controls_num_airloop_served_by_dcv', in_hvac_controls_num_airloop_served_by_dcv)
    runner.registerValue('in_hvac_controls_controller_outdoor_air_minimum_outdoor_air_flow_rate_total', in_hvac_controls_controller_outdoor_air_minimum_outdoor_air_flow_rate_total.round(5), 'm^3/s')
    runner.registerValue('in_hvac_controls_controller_outdoor_air_maximum_outdoor_air_flow_rate_total', in_hvac_controls_controller_outdoor_air_maximum_outdoor_air_flow_rate_total.round(5), 'm^3/s')

    runner.registerValue('out_facility_total_runtime_outdoor_air_volume', facility_total_runtime_outdoor_air_volume(sqlFile:, runner:), 'm^3')

    # zone HVAC properties
    zone_hvac_total_design_max_flow_m_3_s = 0.0
    zone_hvac_fan_weighted_efficiency = 0.0
    zone_hvac_fan_total_power = 0.0
    zone_hvac_total_mass_flow_kg_s = 0.0
    zone_hvac_total_oa_mass_flow_kg_s = 0.0
    model.getZoneHVACComponents.sort.each do |zone_hvac_component|
      # Convert this to the actual class type
      has_fan = true
      is_unitary = false
      if zone_hvac_component.to_AirLoopHVACUnitarySystem.is_initialized
        zone_hvac =  zone_hvac_component.to_AirLoopHVACUnitarySystem.get
        is_unitary = true
      elsif zone_hvac_component.to_ZoneHVACFourPipeFanCoil.is_initialized
        zone_hvac =  zone_hvac_component.to_ZoneHVACFourPipeFanCoil.get
      elsif zone_hvac_component.to_ZoneHVACUnitHeater.is_initialized
        zone_hvac =  zone_hvac_component.to_ZoneHVACUnitHeater.get
      elsif zone_hvac_component.to_ZoneHVACUnitVentilator.is_initialized
        zone_hvac =  zone_hvac_component.to_ZoneHVACUnitVentilator.get
      elsif zone_hvac_component.to_ZoneHVACPackagedTerminalAirConditioner.is_initialized
        zone_hvac =  zone_hvac_component.to_ZoneHVACPackagedTerminalAirConditioner.get
      elsif zone_hvac_component.to_ZoneHVACPackagedTerminalHeatPump.is_initialized
        zone_hvac =  zone_hvac_component.to_ZoneHVACPackagedTerminalHeatPump.get
      elsif zone_hvac_component.to_ZoneHVACTerminalUnitVariableRefrigerantFlow.is_initialized
        zone_hvac =  zone_hvac_component.to_ZoneHVACTerminalUnitVariableRefrigerantFlow.get
      elsif zone_hvac_component.to_ZoneHVACWaterToAirHeatPump.is_initialized
        zone_hvac =  zone_hvac_component.to_ZoneHVACWaterToAirHeatPump.get
      elsif zone_hvac_component.to_ZoneHVACEnergyRecoveryVentilator.is_initialized
        zone_hvac = zone_hvac_component.to_ZoneHVACEnergyRecoveryVentilator.get
      elsif zone_hvac_component.to_ZoneHVACBaseboardConvectiveElectric.is_initialized
        zone_hvac = zone_hvac_component.to_ZoneHVACBaseboardConvectiveElectric.get
        has_fan = false
      elsif zone_hvac_component.to_ZoneHVACBaseboardConvectiveWater.is_initialized
        zone_hvac = zone_hvac_component.to_ZoneHVACBaseboardConvectiveWater.get
        has_fan = false
      elsif zone_hvac_component.to_ZoneHVACBaseboardRadiantConvectiveElectric.is_initialized
        zone_hvac = zone_hvac_component.to_ZoneHVACBaseboardRadiantConvectiveElectric.get
        has_fan = false
      elsif zone_hvac_component.to_ZoneHVACBaseboardRadiantConvectiveWater.is_initialized
        zone_hvac = zone_hvac_component.to_ZoneHVACBaseboardRadiantConvectiveWater.get
        has_fan = false
      else
        runner.registerWarning("Zone HVAC equipment '#{zone_hvac_component.name}' type is not supported in this reporting measure.")
        next
      end

      # Get fan properties
      if has_fan
        if is_unitary
          if zone_hvac.supplyFan.get.to_FanOnOff.is_initialized
            supply_fan = zone_hvac.supplyFan.get.to_FanOnOff.get
            fan_efficiency = supply_fan.fanTotalEfficiency
            max_design_flow = supply_fan.maximumFlowRate.get
            pressure_rise_pa = supply_fan.pressureRise
            fan_total_eff = supply_fan.fanTotalEfficiency
            fan_power = (max_design_flow * pressure_rise_pa) / (fan_total_eff * air_density_kg_per_m_3)
          elsif zone_hvac.supplyFan.get.to_FanConstantVolume.is_initialized
            supply_fan = zone_hvac.supplyFan.get.to_FanConstantVolume.get
            fan_efficiency = supply_fan.fanTotalEfficiency
            max_design_flow = supply_fan.maximumFlowRate.get
            pressure_rise_pa = supply_fan.pressureRise
            fan_total_eff = supply_fan.fanTotalEfficiency
            fan_power = (max_design_flow * pressure_rise_pa) / (fan_total_eff * air_density_kg_per_m_3)
          elsif zone_hvac.supplyFan.get.to_FanVariableVolume.is_initialized
            supply_fan = zone_hvac.supplyFan.get.to_FanVariableVolume.get
            fan_efficiency = supply_fan.fanTotalEfficiency
            max_design_flow = supply_fan.maximumFlowRate.get
            c1 = supply_fan.fanPowerCoefficient1.get
            c2 = supply_fan.fanPowerCoefficient2.get
            c3 = supply_fan.fanPowerCoefficient3.get
            c4 = supply_fan.fanPowerCoefficient4.get
            c5 = supply_fan.fanPowerCoefficient5.get
            plr = c1 + (c2 * 1) + (c3 * (1**2)) + (c4 * (1**3)) + (c5 * (1**4)) # assuming flow fraction as 1
            pressure_rise_pa = supply_fan.pressureRise
            fan_total_eff = supply_fan.fanTotalEfficiency
            fan_power = plr * (max_design_flow * pressure_rise_pa) / (fan_total_eff * air_density_kg_per_m_3)
          end
        else
          if zone_hvac.supplyAirFan.to_FanOnOff.is_initialized
            supply_fan = zone_hvac.supplyAirFan.to_FanOnOff.get
            fan_efficiency = supply_fan.fanTotalEfficiency
            max_design_flow = supply_fan.maximumFlowRate.get
            pressure_rise_pa = supply_fan.pressureRise
            fan_total_eff = supply_fan.fanTotalEfficiency
            fan_power = (max_design_flow * pressure_rise_pa) / (fan_total_eff * air_density_kg_per_m_3)
          elsif zone_hvac.supplyAirFan.to_FanConstantVolume.is_initialized
            supply_fan = zone_hvac.supplyAirFan.to_FanConstantVolume.get
            fan_efficiency = supply_fan.fanTotalEfficiency
            max_design_flow = supply_fan.maximumFlowRate.get
            pressure_rise_pa = supply_fan.pressureRise
            fan_total_eff = supply_fan.fanTotalEfficiency
            fan_power = (max_design_flow * pressure_rise_pa) / (fan_total_eff * air_density_kg_per_m_3)
          elsif zone_hvac.supplyAirFan.to_FanVariableVolume.is_initialized
            supply_fan = zone_hvac.supplyAirFan.to_FanVariableVolume.get
            fan_efficiency = supply_fan.fanTotalEfficiency
            max_design_flow = supply_fan.maximumFlowRate.get
            c1 = supply_fan.fanPowerCoefficient1.get
            c2 = supply_fan.fanPowerCoefficient2.get
            c3 = supply_fan.fanPowerCoefficient3.get
            c4 = supply_fan.fanPowerCoefficient4.get
            c5 = supply_fan.fanPowerCoefficient5.get
            plr = (c1 + (c2 * 1) + (c3 * (1**2)) + (c4 * 1)) ^ (3 + (c5 * (1**4))) # assuming flow fraction as 1
            pressure_rise_pa = supply_fan.pressureRise
            fan_total_eff = supply_fan.fanTotalEfficiency
            fan_power = plr * (max_design_flow * pressure_rise_pa) / (fan_total_eff * air_density_kg_per_m_3)
          end
        end

        zone_hvac_total_design_max_flow_m_3_s += max_design_flow
        zone_hvac_fan_weighted_efficiency += fan_efficiency * max_design_flow
        zone_hvac_fan_total_power += fan_power
      end

      # cast zone_hvac_component down to its child object
      obj_type = zone_hvac_component.iddObjectType.valueName
      obj_type_name = obj_type.gsub('OS_', '').gsub('_', '')
      method_name = "to_#{obj_type_name}"
      if zone_hvac_component.respond_to?(method_name)
        actual_zone_hvac = zone_hvac_component.method(method_name).call
        if !actual_zone_hvac.empty?
          actual_zone_hvac = actual_zone_hvac.get
        end
      end
      # Get OA properties
      oa_node_exists = false
      next if actual_zone_hvac.airLoopHVAC.is_initialized || !actual_zone_hvac.respond_to?(:supplyAirFan)
      # Skip zone HVAC in zones already served by a DOAS or other air loop — OA is
      # fully accounted for by the air system path above; the zone unit is conditioning-only.
      next if zone_hvac_component.thermalZone.is_initialized && zone_hvac_component.thermalZone.get.airLoopHVAC.is_initialized

      base_obj_name = actual_zone_hvac.name.get
      outlet_node = actual_zone_hvac.outletNode.get
      zone_equip_mass_flow_rate_kg_s = sql_get_report_variable_data_double(runner, sqlFile, outlet_node, 'System Node Mass Flow Rate')
      if actual_zone_hvac.respond_to?(:outdoorAirMixerName)
        # PTAC, PTHP, FCU, UnitVentilator — have outdoorAirMixerName in OpenStudio Ruby API
        oa_node_exists = true
        oa_node = "#{base_obj_name} OA Node"
      elsif zone_hvac_component.to_ZoneHVACWaterToAirHeatPump.is_initialized
        # WSHP — outdoorAirMixerName is NOT exposed in OpenStudio Ruby; use same OA node naming pattern
        oa_node_exists = true
        oa_node = "#{base_obj_name} OA Node"
      elsif actual_zone_hvac.respond_to?(:vrfSystem)
        oa_node_exists = true
        oa_node = "#{base_obj_name} Outdoor Air Node"
      end

      if oa_node_exists
        zone_equip_oa_mass_flow_rate_kg_s = sql_get_report_variable_data_double(runner, sqlFile, oa_node, 'System Node Mass Flow Rate')
      else
        zone_equip_oa_mass_flow_rate_kg_s = 0.0
      end
      zone_hvac_total_mass_flow_kg_s += zone_equip_mass_flow_rate_kg_s
      zone_hvac_total_oa_mass_flow_kg_s += zone_equip_oa_mass_flow_rate_kg_s
    end
    in_hvac_zone_fan_max_design_flow_weighted_efficiency = zone_hvac_total_design_max_flow_m_3_s > 0.0 ? zone_hvac_fan_weighted_efficiency / zone_hvac_total_design_max_flow_m_3_s : 0.0
    zone_hvac_average_outdoor_air_fraction = zone_hvac_total_mass_flow_kg_s > 0.0 ? zone_hvac_total_oa_mass_flow_kg_s / zone_hvac_total_mass_flow_kg_s : 0.0
    total_building_avg_mass_flow_rate_kg_s = zone_hvac_total_mass_flow_kg_s + air_system_total_mass_flow_kg_s
    total_building_avg_oa_mass_flow_rate_kg_s = zone_hvac_total_oa_mass_flow_kg_s + air_system_total_oa_mass_flow_kg_s
    total_building_avg_oa_fraction = total_building_avg_mass_flow_rate_kg_s > 0.0 ? total_building_avg_oa_mass_flow_rate_kg_s / total_building_avg_mass_flow_rate_kg_s : 0.0
    in_hvac_controls_design_supply_air_flow_total_m_3_per_s += zone_hvac_total_design_max_flow_m_3_s
    runner.registerValue('in_average_outdoor_air_fraction', total_building_avg_oa_fraction.round(3))
    runner.registerValue('in_hvac_zone_hvac_average_outdoor_air_fraction', zone_hvac_average_outdoor_air_fraction.round(3))
    runner.registerValue('in_hvac_total_building_average_outdoor_air_fraction', total_building_avg_oa_fraction.round(3))
    runner.registerValue('in_hvac_zone_fan_max_design_flow_weighted_efficiency', in_hvac_zone_fan_max_design_flow_weighted_efficiency.round(3))
    runner.registerValue('in_hvac_zone_fan_total_power_w', zone_hvac_fan_total_power.round(3))
    runner.registerValue('in_hvac_controls_design_supply_air_flow_total_m_3_per_s', in_hvac_controls_design_supply_air_flow_total_m_3_per_s.round(5))

    # VAV terminal properties
    vav_terminal_covering_area_sqft_weighted = 0.0
    vav_terminal_min_cfm_per_sqft_weighted = 0.0

    vav_terminal_types = [
      # Supported
      'OS_AirTerminal_SingleDuct_VAV_NoReheat',
      'OS_AirTerminal_SingleDuct_VAV_Reheat',
      # Unsupported, will raise error if found
      'OS_AirTerminal_SingleDuct_VAV_HeatAndCool_NoReheat',
      'OS_AirTerminal_SingleDuct_VAV_HeatAndCool_Reheat',
      'OS_AirTerminal_SingleDuct_VAV_Reheat_VariableSpeedFan',
      'OS_AirTerminal_DualDuct_VAV',
      'OS_AirTerminal_DualDuct_VAV_OutdoorAir'
    ]

    model.getThermalZones.each do |zone|
      vav_terminal_min_flow_m_3_per_sec = nil
      vav_terminal_covering_area_m_2 = zone.floorArea

      next if zone.airLoopHVACTerminal.empty?

      airloophvacterminal = zone.airLoopHVACTerminal.get
      airloophvacterminal_type = airloophvacterminal.iddObjectType.valueName

      next if !vav_terminal_types.include?(airloophvacterminal_type)

      case airloophvacterminal_type
      when 'OS_AirTerminal_SingleDuct_VAV_NoReheat'
        terminal = airloophvacterminal.to_AirTerminalSingleDuctVAVNoReheat.get
        if terminal.autosizedFixedMinimumAirFlowRate.is_initialized
          vav_terminal_min_flow_m_3_per_sec = terminal.autosizedFixedMinimumAirFlowRate.get
        elsif terminal.fixedMinimumAirFlowRate.is_initialized
          vav_terminal_min_flow_m_3_per_sec = terminal.fixedMinimumAirFlowRate.get
        else
          raise "cannot find fixed min flow for vav terminal: #{terminal.name}"
        end
      when 'OS_AirTerminal_SingleDuct_VAV_Reheat'
        terminal = airloophvacterminal.to_AirTerminalSingleDuctVAVReheat.get
        if terminal.autosizedFixedMinimumAirFlowRate.is_initialized
          vav_terminal_min_flow_m_3_per_sec = terminal.autosizedFixedMinimumAirFlowRate.get
        elsif terminal.fixedMinimumAirFlowRate.is_initialized
          vav_terminal_min_flow_m_3_per_sec = terminal.fixedMinimumAirFlowRate.get
        else
          raise "cannot find fixed min flow for vav terminal: #{terminal.name}"
        end
      else
        raise "unsupported vav terminal type: #{airloophvacterminal.briefDescription} for zone '#{zone.nameString}'."
      end

      vav_terminal_min_flow_cfm = OpenStudio.convert(vav_terminal_min_flow_m_3_per_sec, 'm^3/s', 'cfm').get
      vav_terminal_covering_area_sqft = OpenStudio.convert(vav_terminal_covering_area_m_2, 'm^2', 'ft^2').get
      vav_terminal_min_cfm_per_sqft = vav_terminal_min_flow_cfm / vav_terminal_covering_area_sqft
      vav_terminal_min_cfm_per_sqft_weighted += vav_terminal_min_cfm_per_sqft * vav_terminal_covering_area_sqft
      vav_terminal_covering_area_sqft_weighted += vav_terminal_covering_area_sqft
    end
    in_hvac_controls_vav_terminal_min_cfm_per_sqft = vav_terminal_covering_area_sqft_weighted > 0.0 ? vav_terminal_min_cfm_per_sqft_weighted / vav_terminal_covering_area_sqft_weighted : 0.0
    runner.registerValue('in_hvac_controls_vav_terminal_min_cfm_per_sqft', in_hvac_controls_vav_terminal_min_cfm_per_sqft.round(5))

    # Handle fuel output variables that changed in EnergyPlus version 9.4 (Openstudio version >= 3.1)
    elec = 'Electric'
    gas = 'Gas'
    if model.version > OpenStudio::VersionString.new('3.0.1')
      elec = 'Electricity'
      gas = 'NaturalGas'
    end

    # Chiller properties
    chiller_total_capacity_w = 0.0
    chiller_capacity_weighted_design_cop = 0.0
    model.getChillerElectricEIRs.each do |chiller|
      # get chiller capacity
      if chiller.referenceCapacity.is_initialized
        capacity_w = chiller.referenceCapacity.get
      elsif chiller.autosizedReferenceCapacity.is_initialized
        capacity_w = chiller.autosizedReferenceCapacity.get
      else
        runner.registerWarning("Chiller capacity not available for chiller '#{chiller.name}'.")
      end
      chiller_total_capacity_w += capacity_w

      # get chiller design cop
      chiller_design_cop = chiller.referenceCOP

      # add to weighted load cop
      chiller_capacity_weighted_design_cop += capacity_w * chiller_design_cop
    end
    in_hvac_chiller_capacity_weighted_design_efficiency = chiller_total_capacity_w > 0.0 ? chiller_capacity_weighted_design_cop / chiller_total_capacity_w : 0.0
    runner.registerValue('in_hvac_chiller_capacity_weighted_design_efficiency', in_hvac_chiller_capacity_weighted_design_efficiency.round(5))
    in_hvac_chiller_capacity_tons = OpenStudio.convert(chiller_total_capacity_w, 'W', 'ton').get
    runner.registerValue('in_hvac_chiller_capacity_tons', in_hvac_chiller_capacity_tons.round(5))

    # Boiler properties
    boiler_capacity_weighted_design_efficiency = 0.0
    boiler_total_capacity_w = 0.0
    model.getBoilerHotWaters.each do |boiler|
      # get boiler capacity
      capacity_w = 0.0
      if boiler.nominalCapacity.is_initialized
        capacity_w = boiler.nominalCapacity.get
      elsif boiler.autosizedNominalCapacity.is_initialized
        capacity_w = boiler.autosizedNominalCapacity.get
      else
        runner.registerWarning("Boiler capacity not available for boiler '#{boiler.name}'.")
      end
      boiler_design_efficiency = boiler.nominalThermalEfficiency
      boiler_total_capacity_w += capacity_w
      boiler_capacity_weighted_design_efficiency += capacity_w * boiler_design_efficiency
    end
    in_hvac_boiler_capacity_weighted_design_efficiency = boiler_total_capacity_w > 0.0 ? boiler_capacity_weighted_design_efficiency / boiler_total_capacity_w : 0.0
    runner.registerValue('in_hvac_boiler_capacity_weighted_design_efficiency', in_hvac_boiler_capacity_weighted_design_efficiency.round(5))

    # DX cooling coils properties
    dx_cooling_capacity_weighted_design_cop = 0.0
    dx_cooling_airflow_weighted_design_cop = 0.0
    dx_cooling_total_capacity_w = 0.0
    dx_cooling_total_rated_airflow_m_3_per_s = 0.0
    dx_cooling_count = 0
    dx_cooling_coils = []
    model.getCoilCoolingDXSingleSpeeds.each { |c| dx_cooling_coils << c }
    model.getCoilCoolingDXTwoSpeeds.each { |c| dx_cooling_coils << c }
    model.getCoilCoolingDXMultiSpeeds.each { |c| dx_cooling_coils << c }
    model.getCoilCoolingDXVariableSpeeds.each { |c| dx_cooling_coils << c }
    dx_cooling_coils.each do |coil|
      # get dx cooling capacity and cop
      capacity_w = 0.0
      rated_air_flow_rate_m_3_per_s = 0.0
      coil_design_cop = 0.0
      if coil.to_CoilCoolingDXSingleSpeed.is_initialized
        coil = coil.to_CoilCoolingDXSingleSpeed.get
        dx_cooling_count += 1

        # capacity
        if coil.ratedTotalCoolingCapacity.is_initialized
          capacity_w = coil.ratedTotalCoolingCapacity.get
        elsif coil.autosizedRatedTotalCoolingCapacity.is_initialized
          capacity_w = coil.autosizedRatedTotalCoolingCapacity.get
        else
          runner.registerWarning("Cooling coil capacity not available for coil '#{coil.name}'.")
        end

        # rated air flow rate
        if coil.ratedAirFlowRate.is_initialized
          rated_air_flow_rate_m_3_per_s = coil.ratedAirFlowRate.get
        elsif coil.autosizedRatedAirFlowRate.is_initialized
          rated_air_flow_rate_m_3_per_s = coil.autosizedRatedAirFlowRate.get
        else
          runner.registerWarning("Cooling coil rated airflow not available for coil '#{coil.name}'.")
        end

        # cop
        if model.version > OpenStudio::VersionString.new('3.4.0')
          coil_design_cop = coil.ratedCOP
        else
          if coil.ratedCOP.is_initialized
            coil_design_cop = coil.ratedCOP.get
          else
            runner.registerWarning("'Rated COP' not available for DX coil '#{coil.name}'.")
          end
        end
      elsif coil.to_CoilCoolingDXTwoSpeed.is_initialized
        coil = coil.to_CoilCoolingDXTwoSpeed.get
        dx_cooling_count += 1

        # capacity
        if coil.ratedHighSpeedTotalCoolingCapacity.is_initialized
          capacity_w = coil.ratedHighSpeedTotalCoolingCapacity.get
        elsif coil.autosizedRatedHighSpeedTotalCoolingCapacity.is_initialized
          capacity_w = coil.autosizedRatedHighSpeedTotalCoolingCapacity.get
        else
          runner.registerWarning("Cooling coil capacity not available for coil '#{coil.name}'.")
        end

        # rated air flow rate
        if coil.ratedHighSpeedAirFlowRate.is_initialized
          rated_air_flow_rate_m_3_per_s = coil.ratedHighSpeedAirFlowRate.get
        elsif coil.autosizedRatedHighSpeedAirFlowRate.is_initialized
          rated_air_flow_rate_m_3_per_s = coil.autosizedRatedHighSpeedAirFlowRate.get
        else
          runner.registerWarning("Cooling coil rated airflow not available for coil '#{coil.name}'.")
        end

        # cop, use high speed cop
        if model.version > OpenStudio::VersionString.new('3.4.0')
          coil_design_cop = coil.ratedHighSpeedCOP
        else
          if coil.ratedHighSpeedCOP.is_initialized
            coil_design_cop = coil.ratedHighSpeedCOP.get
          else
            runner.registerWarning("'Rated High Speed COP' not available for DX coil '#{coil.name}'.")
          end
        end

      elsif coil.to_CoilCoolingDXMultiSpeed.is_initialized
        coil = coil.to_CoilCoolingDXMultiSpeed.get
        dx_cooling_count += 1

        # capacity and cop, use cop at highest capacity
        temp_capacity_w = 0.0
        temp_rated_air_flow_rate_m_3_per_s = 0.0
        coil.stages.each do |stage|
          if stage.grossRatedTotalCoolingCapacity.is_initialized
            temp_capacity_w = stage.grossRatedTotalCoolingCapacity.get
          elsif stage.autosizedGrossRatedTotalCoolingCapacity.is_initialized
            temp_capacity_w = stage.autosizedGrossRatedTotalCoolingCapacity.get
          else
            runner.registerWarning("Cooling coil capacity not available for coil stage '#{stage.name}'.")
          end

          if stage.ratedAirFlowRate.is_initialized
            temp_rated_air_flow_rate_m_3_per_s = stage.ratedAirFlowRate.get
          elsif stage.autosizedRatedAirFlowRate.is_initialized
            temp_rated_air_flow_rate_m_3_per_s = stage.autosizedRatedAirFlowRate.get
          else
            runner.registerWarning("Cooling coil rated airflow not available for coil stage '#{stage.name}'.")
          end

          # update cop if highest capacity
          temp_coil_design_cop = stage.grossRatedCoolingCOP
          coil_design_cop = temp_coil_design_cop if temp_capacity_w >= capacity_w

          # update if highest capacity
          capacity_w = temp_capacity_w if temp_capacity_w > capacity_w
          rated_air_flow_rate_m_3_per_s = temp_rated_air_flow_rate_m_3_per_s if temp_rated_air_flow_rate_m_3_per_s > rated_air_flow_rate_m_3_per_s
        end
      elsif coil.to_CoilCoolingDXVariableSpeed.is_initialized
        coil = coil.to_CoilCoolingDXVariableSpeed.get
        dx_cooling_count += 1

        # capacity and cop, use cop at highest capacity
        temp_capacity_w = 0.0
        temp_rated_air_flow_rate_m_3_per_s = 0.0
        coil.speeds.each do |speed|
          temp_capacity_w = speed.referenceUnitGrossRatedTotalCoolingCapacity
          temp_rated_air_flow_rate_m_3_per_s = speed.referenceUnitRatedAirFlowRate

          # update cop if highest capacity
          temp_coil_design_cop = speed.referenceUnitGrossRatedCoolingCOP
          coil_design_cop = temp_coil_design_cop if temp_capacity_w >= capacity_w

          # update if highest capacity
          capacity_w = temp_capacity_w if temp_capacity_w > capacity_w
          rated_air_flow_rate_m_3_per_s = temp_rated_air_flow_rate_m_3_per_s if temp_rated_air_flow_rate_m_3_per_s > rated_air_flow_rate_m_3_per_s
        end
      else
        runner.registerWarning('Design capacity is only available for DX cooling coil types CoilCoolingDXSingleSpeed, CoilCoolingDXTwoSpeed, CoilCoolingDXMultiSpeed, CoilCoolingDXVariableSpeed.')
      end
      dx_cooling_total_capacity_w += capacity_w
      dx_cooling_total_rated_airflow_m_3_per_s += rated_air_flow_rate_m_3_per_s

      # get Cooling Coil Total Cooling Energy
      coil_cooling_energy_j = 0.0
      var_data_id_query = "SELECT ReportVariableDataDictionaryIndex FROM ReportVariableDataDictionary WHERE VariableName = 'Cooling Coil Total Cooling Energy' AND ReportingFrequency = 'Run Period' AND KeyValue = '#{coil.name.get.to_s.upcase}'"
      var_data_id = sqlFile.execAndReturnFirstDouble(var_data_id_query)
      if var_data_id.is_initialized
        var_val_query = "SELECT VariableValue FROM ReportVariableData WHERE ReportVariableDataDictionaryIndex = '#{var_data_id.get}'"
        val = sqlFile.execAndReturnFirstDouble(var_val_query)
        if val.is_initialized
          coil_cooling_energy_j = val.get
        else
          runner.registerWarning("'Coil Cooling Total Cooling Energy' not available for DX coil '#{coil.name}'.")
        end
      else
        runner.registerWarning("'Coil Cooling Total Cooling Energy' not available for DX coil '#{coil.name}'.")
      end

      # get Cooling Coil Electric Energy
      coil_electric_energy_j = 0.0
      var_data_id_query = "SELECT ReportVariableDataDictionaryIndex FROM ReportVariableDataDictionary WHERE VariableName = 'Cooling Coil #{elec} Energy' AND ReportingFrequency = 'Run Period' AND KeyValue = '#{coil.name.get.to_s.upcase}'"
      var_data_id = sqlFile.execAndReturnFirstDouble(var_data_id_query)
      if var_data_id.is_initialized
        var_val_query = "SELECT VariableValue FROM ReportVariableData WHERE ReportVariableDataDictionaryIndex = '#{var_data_id.get}'"
        val = sqlFile.execAndReturnFirstDouble(var_val_query)
        if val.is_initialized
          coil_electric_energy_j = val.get
        else
          runner.registerWarning("'Cooling Coil #{elec} Energy' value not available for DX coil '#{coil.name}'.")
        end
      else
        runner.registerWarning("'Cooling Coil #{elec} Energy' data index not available for DX coil '#{coil.name}'.")
      end

      # add to weighted load cop
      coil_annual_cop = coil_cooling_energy_j > 0.0 ? coil_cooling_energy_j / coil_electric_energy_j : 0
      dx_cooling_capacity_weighted_design_cop += capacity_w * coil_design_cop
      dx_cooling_airflow_weighted_design_cop += rated_air_flow_rate_m_3_per_s * coil_design_cop

      # cooling coil info logging
      runner.registerInfo("Cooling coil '#{coil.name}' has design capacity #{capacity_w.round(2)} W, design cop #{coil_design_cop.round(2)}, and annual weighted cop #{coil_annual_cop.round(2)}.")
    end
    in_hvac_dx_cooling_capacity_tons = OpenStudio.convert(dx_cooling_total_capacity_w, 'W', 'ton').get
    in_hvac_dx_cooling_cop_design_capacity_weighted = dx_cooling_total_capacity_w > 0.0 ? dx_cooling_capacity_weighted_design_cop / dx_cooling_total_capacity_w : 0.0
    in_hvac_dx_cooling_cop_airflow_weighted = dx_cooling_total_rated_airflow_m_3_per_s > 0.0 ? dx_cooling_airflow_weighted_design_cop / dx_cooling_total_rated_airflow_m_3_per_s : 0.0
    in_hvac_dx_cooling_coil_count = dx_cooling_count
    runner.registerValue('in_hvac_dx_cooling_capacity_tons', in_hvac_dx_cooling_capacity_tons.round(3))
    runner.registerValue('in_hvac_dx_cooling_total_rated_airflow_m_3_per_s', dx_cooling_total_rated_airflow_m_3_per_s.round(5))
    runner.registerValue('in_hvac_dx_cooling_cop_design_capacity_weighted', in_hvac_dx_cooling_cop_design_capacity_weighted.round(3))
    runner.registerValue('in_hvac_dx_cooling_cop_airflow_weighted', in_hvac_dx_cooling_cop_airflow_weighted.round(3))
    runner.registerValue('in_hvac_dx_cooling_coil_count', in_hvac_dx_cooling_coil_count)

    # DX heating coil capacity, load, and efficiences, including supplemental coils
    dx_heating_capacity_weighted_design_cop = 0.0
    dx_heating_airflow_weighted_design_cop = 0.0
    dx_heating_total_capacity_w = 0.0
    dx_heating_total_rated_airflow_m_3_per_s = 0.0
    dx_heating_count = 0
    dx_heating_coils = []
    model.getCoilHeatingDXSingleSpeeds.each { |c| dx_heating_coils << c }
    model.getCoilHeatingDXMultiSpeeds.each { |c| dx_heating_coils << c }
    model.getCoilHeatingDXVariableSpeeds.each { |c| dx_heating_coils << c }
    dx_heating_coils.each do |coil|
      # get coil rated capacity and cop
      capacity_w = 0.0
      rated_air_flow_rate_m_3_per_s = 0.0
      coil_design_cop = 0.0
      if coil.to_CoilHeatingDXSingleSpeed.is_initialized
        coil = coil.to_CoilHeatingDXSingleSpeed.get
        dx_heating_count += 1

        if coil.ratedTotalHeatingCapacity.is_initialized
          capacity_w = coil.ratedTotalHeatingCapacity.get
        elsif coil.autosizedRatedTotalHeatingCapacity.is_initialized
          capacity_w = coil.autosizedRatedTotalHeatingCapacity.get
        else
          runner.registerWarning("Heating coil capacity not available for coil '#{coil.name}'.")
        end

        if coil.ratedAirFlowRate.is_initialized
          rated_air_flow_rate_m_3_per_s = coil.ratedAirFlowRate.get
        elsif coil.autosizedRatedAirFlowRate.is_initialized
          rated_air_flow_rate_m_3_per_s = coil.autosizedRatedAirFlowRate.get
        else
          runner.registerWarning("Heating coil rated airflow not available for coil '#{coil.name}'.")
        end

        # get rated cop and cop at lower temperatures
        coil_design_cop = coil.ratedCOP

      elsif coil.to_CoilHeatingDXMultiSpeed.is_initialized
        coil = coil.to_CoilHeatingDXMultiSpeed.get
        dx_heating_count += 1

        temp_capacity_w = 0.0
        temp_rated_air_flow_rate_m_3_per_s = 0.0
        coil.stages.each do |stage|
          if stage.grossRatedHeatingCapacity.is_initialized
            temp_capacity_w = stage.grossRatedHeatingCapacity.get
          elsif stage.autosizedGrossRatedHeatingCapacity.is_initialized
            temp_capacity_w = stage.autosizedGrossRatedHeatingCapacity.get
          else
            runner.registerWarning("Heating coil capacity not available for coil stage '#{stage.name}'.")
          end

          if stage.ratedAirFlowRate.is_initialized
            temp_rated_air_flow_rate_m_3_per_s = stage.ratedAirFlowRate.get
          elsif stage.autosizedRatedAirFlowRate.is_initialized
            temp_rated_air_flow_rate_m_3_per_s = stage.autosizedRatedAirFlowRate.get
          else
            runner.registerWarning("Heating coil rated airflow not available for coil stage '#{stage.name}'.")
          end

          # pick cop at highest capacity
          temp_coil_design_cop = stage.grossRatedHeatingCOP
          coil_design_cop = temp_coil_design_cop if temp_capacity_w >= capacity_w

          # update if highest capacity
          capacity_w = temp_capacity_w if temp_capacity_w > capacity_w
          rated_air_flow_rate_m_3_per_s = temp_rated_air_flow_rate_m_3_per_s if temp_rated_air_flow_rate_m_3_per_s > rated_air_flow_rate_m_3_per_s
        end
      elsif coil.to_CoilHeatingDXVariableSpeed.is_initialized
        coil = coil.to_CoilHeatingDXVariableSpeed.get
        dx_heating_count += 1

        coil.speeds.each do |speed|
          temp_capacity_w = speed.referenceUnitGrossRatedHeatingCapacity
          temp_rated_air_flow_rate_m_3_per_s = speed.referenceUnitRatedAirFlowRate

          # get cop and cop at lower temperatures
          # pick cop at highest capacity
          temp_coil_design_cop = speed.referenceUnitGrossRatedHeatingCOP
          coil_design_cop = temp_coil_design_cop if temp_capacity_w >= capacity_w

          # update if highest capacity
          capacity_w = temp_capacity_w if temp_capacity_w > capacity_w
          rated_air_flow_rate_m_3_per_s = temp_rated_air_flow_rate_m_3_per_s if temp_rated_air_flow_rate_m_3_per_s > rated_air_flow_rate_m_3_per_s
        end
      else
        runner.registerWarning('Design COP and capacity for DX heating coil unavailable because of unrecognized coil type.')
      end
      dx_heating_total_capacity_w += capacity_w
      dx_heating_total_rated_airflow_m_3_per_s += rated_air_flow_rate_m_3_per_s
      dx_heating_capacity_weighted_design_cop += capacity_w * coil_design_cop
      dx_heating_airflow_weighted_design_cop += rated_air_flow_rate_m_3_per_s * coil_design_cop
    end
    # report out
    in_hvac_dx_heating_capacity_kbtuh = OpenStudio.convert(dx_heating_total_capacity_w, 'W', 'kBtu/h').get
    in_hvac_dx_heating_cop_design_capacity_weighted = dx_heating_total_capacity_w > 0.0 ? dx_heating_capacity_weighted_design_cop / dx_heating_total_capacity_w : 0.0
    in_hvac_dx_heating_cop_airflow_weighted = dx_heating_total_rated_airflow_m_3_per_s > 0.0 ? dx_heating_airflow_weighted_design_cop / dx_heating_total_rated_airflow_m_3_per_s : 0.0
    in_hvac_dx_heating_coil_count = dx_heating_count
    runner.registerValue('in_hvac_dx_heating_capacity_kbtuh', in_hvac_dx_heating_capacity_kbtuh.round(5))
    runner.registerValue('in_hvac_dx_heating_cop_design_capacity_weighted', in_hvac_dx_heating_cop_design_capacity_weighted.round(3))
    runner.registerValue('in_hvac_dx_heating_cop_airflow_weighted', in_hvac_dx_heating_cop_airflow_weighted.round(3))
    runner.registerValue('in_hvac_dx_heating_coil_count', in_hvac_dx_heating_coil_count)

    # AHRI rated COPs - cooling
    dx_unit_cooling_capacity_w = OpenStudio.convert(in_hvac_dx_cooling_capacity_tons, 'ton', 'W').get
    dx_unit_power_based_on_energyplus_w = in_hvac_dx_cooling_cop_airflow_weighted > 0.0 ? dx_unit_cooling_capacity_w / in_hvac_dx_cooling_cop_airflow_weighted : 0.0
    dx_unit_power_based_on_ahri_central = dx_unit_power_based_on_energyplus_w > 0.0 ? dx_unit_power_based_on_energyplus_w + air_system_total_fan_power : 0.0
    dx_unit_power_based_on_ahri_terminal = dx_unit_power_based_on_energyplus_w > 0.0 ? dx_unit_power_based_on_energyplus_w + zone_hvac_fan_total_power : 0.0
    in_hvac_dx_cooling_ahri_rated_cop_central = dx_unit_power_based_on_ahri_central > 0.0 ? (dx_unit_cooling_capacity_w / dx_unit_power_based_on_ahri_central) : 0.0
    in_hvac_dx_cooling_ahri_rated_cop_terminal = dx_unit_power_based_on_ahri_terminal > 0.0 ? (dx_unit_cooling_capacity_w / dx_unit_power_based_on_ahri_terminal) : 0.0
    runner.registerValue('in_hvac_dx_cooling_ahri_rated_cop_central', in_hvac_dx_cooling_ahri_rated_cop_central.round(3))
    runner.registerValue('in_hvac_dx_cooling_ahri_rated_cop_terminal', in_hvac_dx_cooling_ahri_rated_cop_terminal.round(3))

    # AHRI rated COPs - heating
    dx_unit_heating_capacity_w = OpenStudio.convert(in_hvac_dx_heating_capacity_kbtuh, 'kBtu/h', 'W').get
    dx_unit_power_based_on_energyplus_w = in_hvac_dx_heating_cop_airflow_weighted > 0.0 ? dx_unit_heating_capacity_w / in_hvac_dx_heating_cop_airflow_weighted : 0.0
    dx_unit_power_based_on_ahri_central = dx_unit_power_based_on_energyplus_w > 0.0 ? dx_unit_power_based_on_energyplus_w + air_system_total_fan_power : 0.0
    dx_unit_power_based_on_ahri_terminal = dx_unit_power_based_on_energyplus_w > 0.0 ? dx_unit_power_based_on_energyplus_w + zone_hvac_fan_total_power : 0.0
    in_hvac_dx_heating_ahri_rated_cop_central = dx_unit_power_based_on_ahri_central > 0.0 ? (dx_unit_heating_capacity_w / dx_unit_power_based_on_ahri_central) : 0.0
    in_hvac_dx_heating_ahri_rated_cop_terminal = dx_unit_power_based_on_ahri_terminal > 0.0 ? (dx_unit_heating_capacity_w / dx_unit_power_based_on_ahri_terminal) : 0.0
    runner.registerValue('in_hvac_dx_heating_ahri_rated_cop_central', in_hvac_dx_heating_ahri_rated_cop_central.round(3))
    runner.registerValue('in_hvac_dx_heating_ahri_rated_cop_terminal', in_hvac_dx_heating_ahri_rated_cop_terminal.round(3))

    # Average gas coil efficiency
    gas_coil_capacity_weighted_efficiency = 0.0
    gas_coil_total_capacity_w = 0.0
    model.getCoilHeatingGass.sort.each do |coil|
      # get gas coil capacity
      capacity_w = 0.0
      if coil.nominalCapacity.is_initialized
        capacity_w = coil.nominalCapacity.get
      elsif coil.autosizedNominalCapacity.is_initialized
        capacity_w = coil.autosizedNominalCapacity.get
      else
        runner.registerWarning("Gas heating coil capacity not available for '#{coil.name}'.")
      end
      gas_coil_total_capacity_w += capacity_w
      gas_coil_capacity_weighted_efficiency += capacity_w * coil.gasBurnerEfficiency
    end
    in_hvac_furnace_eff_design_capacity_weighted = gas_coil_total_capacity_w > 0.0 ? gas_coil_capacity_weighted_efficiency / gas_coil_total_capacity_w : 0.0
    runner.registerValue('in_hvac_furnace_eff_design_capacity_weighted', in_hvac_furnace_eff_design_capacity_weighted.round(5))

    # Service water heating hot water use
    in_swh_annual_hot_water_m_3 = 0
    model.getWaterUseConnectionss.each do |water_use_connection|
      var_data_id_query = "SELECT ReportVariableDataDictionaryIndex FROM ReportVariableDataDictionary WHERE VariableName = 'Water Use Connections Hot Water Volume' AND ReportingFrequency = 'Run Period' AND KeyValue = '#{water_use_connection.name.get.to_s.upcase}'"
      var_data_id = sqlFile.execAndReturnFirstDouble(var_data_id_query)
      if var_data_id.is_initialized
        var_val_query = "SELECT VariableValue FROM ReportVariableData WHERE ReportVariableDataDictionaryIndex = '#{var_data_id.get}'"
        in_swh_annual_hot_water_m_3 += sqlFile.execAndReturnFirstDouble(var_val_query).get
      else
        runner.registerWarning("'Water Use Connections Hot Water Volume' not available for water use connection '#{water_use_connection.name}'.")
      end
    end
    runner.registerValue('in_swh_annual_hot_water_m_3', in_swh_annual_hot_water_m_3.round(3), 'm^3')

    # water heater storage tank volume
    water_heater_total_volume_gal = 0.0
    heat_pump_water_heater_tanks = []
    heat_pump_water_heaters = []
    model.getWaterHeaterHeatPumps.each { |wh| heat_pump_water_heaters << wh }
    model.getWaterHeaterHeatPumpWrappedCondensers.each { |wh| heat_pump_water_heaters << wh }
    # loop through heat pump water heaters and report out variables
    heat_pump_water_heaters.sort.each do |hpwh|
      tank = hpwh.tank
      if tank.to_WaterHeaterMixed.is_initialized
        tank = tank.to_WaterHeaterMixed.get
      elsif tank.to_WaterHeaterStratified.is_initialized
        tank = tank.to_WaterHeaterStratified.get
      end
      heat_pump_water_heater_tanks << tank.name.to_s
    end
    water_heaters = model.getWaterHeaterMixeds + model.getWaterHeaterStratifieds
    in_swh_fuel_type = []
    water_heaters.sort.each do |wh|
      wh_name = wh.name.to_s

      # Skip tanks that are part of heat pump water heaters
      next if heat_pump_water_heater_tanks.include?(wh_name)

      # Calculate and accumulate tank volume
      volume_m3 = wh.tankVolume.is_initialized ? wh.tankVolume.get : 0.0
      volume_gal = OpenStudio.convert(volume_m3, 'm^3', 'gal').get.round(3)
      water_heater_total_volume_gal += volume_gal

      # Identify and collect fuel type
      case wh.heaterFuelType.downcase
      when /electric/
        in_swh_fuel_type << elec
      when /gas/
        in_swh_fuel_type << gas
      else
        in_swh_fuel_type << wh.heaterFuelType
      end
    end
    water_heater_total_volume_gal = water_heater_total_volume_gal.round(1)
    runner.registerValue('in_swh_water_heater_total_volume_gal', water_heater_total_volume_gal.round(5))

    in_swh_fuel_type_unique = in_swh_fuel_type.uniq
    if in_swh_fuel_type_unique.size == 1
      in_swh_fuel_type = in_swh_fuel_type[0]
    else
      in_swh_fuel_type = "dual fuel: #{in_swh_fuel_type_unique}"
    end
    runner.registerValue('in_swh_fuel_type', in_swh_fuel_type)

    # service water heating performance metric
    swh_capacity_w_sum = 0
    in_swh_ua_w_per_k_avg_weighted_sum = 0
    in_swh_te_avg_weighted_sum = 0
    model.getWaterHeaterMixeds.each do |swh|
      # get water heater capacity
      if swh.heaterMaximumCapacity.is_initialized
        swh_capacity_w = swh.heaterMaximumCapacity.get
      elsif swh.autosizedHeaterMaximumCapacity.is_initialized
        swh_capacity_w = swh.autosizedHeaterMaximumCapacity.get
      else
        runner.registerError("Capacity not available for water heater '#{swh.name}' after sizing run.")
        return false
      end
      swh_capacity_w_sum += swh_capacity_w

      if swh.additionalProperties.getFeatureAsInteger('component_quantity').is_initialized
        comp_qty = swh.additionalProperties.getFeatureAsInteger('component_quantity').get
        if comp_qty > 1
          runner.registerInfo("Water heater '#{swh.name}' with capacity #{swh_capacity_w.round} W is representing #{comp_qty} water heaters.")
        end
      end

      if in_swh_fuel_type == gas
        # get parameters to calculate energy factor
        ua_w_per_k = swh.onCycleLossCoefficienttoAmbientTemperature.get
        thermal_efficiency = swh.heaterThermalEfficiency.get

        runner.registerInfo("Exsting water heater '#{swh.name}' has Thermal Efficiency = #{thermal_efficiency} and UA = #{ua_w_per_k} W/K")
        in_swh_ua_w_per_k_avg_weighted_sum += ua_w_per_k * swh_capacity_w
        in_swh_te_avg_weighted_sum += thermal_efficiency * swh_capacity_w
      elsif in_swh_fuel_type == elec
        # get parameters to calculate EF
        ua_w_per_k = OpenStudio.convert(swh.onCycleLossCoefficienttoAmbientTemperature.get, 'W/K', 'Btu/h*R').get
        thermal_efficiency = swh.heaterThermalEfficiency.get

        runner.registerInfo("Exsting water heater '#{swh.name}' has Thermal Efficiency = #{thermal_efficiency} and UA = #{ua_w_per_k} W/K")
        in_swh_ua_w_per_k_avg_weighted_sum += ua_w_per_k * swh_capacity_w
        in_swh_te_avg_weighted_sum += thermal_efficiency * swh_capacity_w
      end
    end
    in_swh_ua_w_per_k_weighted_avg = swh_capacity_w_sum > 0.0 ? in_swh_ua_w_per_k_avg_weighted_sum / swh_capacity_w_sum : 0.0
    in_swh_burner_efficiency_weighted_avg = swh_capacity_w_sum > 0.0 ? in_swh_te_avg_weighted_sum / swh_capacity_w_sum : 0.0
    runner.registerValue('in_swh_ua_w_per_k_weighted_avg', in_swh_ua_w_per_k_weighted_avg.round(5))
    runner.registerValue('in_swh_burner_efficiency_weighted_avg', in_swh_burner_efficiency_weighted_avg.round(3))

    # -------------------------------------------------------------------
    # puts'### register values from upstream: building parameters')
    # -------------------------------------------------------------------

    if disable_upstream_arg == false
      # primary building type
      in_primary_bldg_type = std.model_get_primary_building_type(model, remap_office: true, remap_retail: true)
      runner.registerValue('in_primary_bldg_type', in_primary_bldg_type)

      # HVAC system details
      in_hvac_system_type_prm = runner.getPastStepValuesForName('prm_baseline_system_type').values.first
      runner.registerValue('in_hvac_system_type_prm', in_hvac_system_type_prm) unless in_hvac_system_type_prm.nil?

      # aspect ratio
      in_ns_to_ew_ratio = runner.getPastStepValuesForName('ns_to_ew_ratio').values.first
      runner.registerValue('in_ns_to_ew_ratio', in_ns_to_ew_ratio.to_f.round(5)) unless in_ns_to_ew_ratio.nil?

      # number of stories above grade
      in_num_stories_above_grade = runner.getPastStepValuesForName('num_stories_above_grade').values.first
      runner.registerValue('in_num_stories_above_grade', in_num_stories_above_grade) unless in_num_stories_above_grade.nil?

      # climate zone
      in_weather_climate_zone = runner.getPastStepValuesForName('climate_zone').values.first
      runner.registerValue('in_weather_climate_zone', in_weather_climate_zone) unless in_weather_climate_zone.nil?
    end

    # -------------------------------------------------------------------
    # puts'### register relevant values: simulation results')
    # -------------------------------------------------------------------
    # unmet hours
    # These two are the "During Heating / During Cooling" (including Unoccupied hours)
    _out_hours_heating_setpoint_not_met = report_sim_output(runner, 'out_hours_heating_setpoint_not_met', [sqlFile.hoursHeatingSetpointNotMet], nil, nil)
    _out_hours_cooling_setpoint_not_met = report_sim_output(runner, 'out_hours_cooling_setpoint_not_met', [sqlFile.hoursCoolingSetpointNotMet], nil, nil)

    # During occupied heating/cooling
    query_htg_occ = "SELECT Value FROM TabularDataWithStrings WHERE (ReportName='SystemSummary') AND (ReportForString='Entire Facility') AND (TableName='Time Setpoint Not Met') AND (RowName = 'Facility') AND (ColumnName='During Occupied Heating') AND (Units = 'hr')"
    unmet_htg_occ = sqlFile.execAndReturnFirstDouble(query_htg_occ).get
    runner.registerValue('out_hours_heating_setpoint_not_met_when_occupied', unmet_htg_occ.round(5))
    runner.registerInfo("Time Setpoint Not Met During Occupied Heating: #{unmet_htg_occ.round(2)}.")

    query_clg_occ = "SELECT Value FROM TabularDataWithStrings WHERE (ReportName='SystemSummary') AND (ReportForString='Entire Facility') AND (TableName='Time Setpoint Not Met') AND (RowName = 'Facility') AND (ColumnName='During Occupied Cooling') AND (Units = 'hr')"
    unmet_clg_occ = sqlFile.execAndReturnFirstDouble(query_clg_occ).get
    runner.registerValue('out_hours_cooling_setpoint_not_met_when_occupied', unmet_clg_occ.round(5))
    runner.registerInfo("Time Setpoint Not Met During Occupied Cooling: #{unmet_clg_occ.round(2)}.")

    if unmet_htg_occ > 300 || unmet_clg_occ > 300
      runner.registerWarning('More than 300 unmet hours during occupied, which exceeds the threshold for ASHRAE 90.1 Appendix G')
    end

    # Threshold unmet hours
    raise unless sqlFile.availableReportingFrequencies(ann_env_pd).include?('Zone Timestep')

    n_tzs = model.getThermalZones.size
    raise unless sqlFile.availableKeyValues(ann_env_pd, 'Zone Timestep', 'Zone Heating Setpoint Not Met Time').size == n_tzs
    raise unless sqlFile.availableKeyValues(ann_env_pd, 'Zone Timestep', 'Zone Cooling Setpoint Not Met Time').size == n_tzs

    runner.registerValue('in_thermal_zone_count', n_tzs)

    # Figure out size of the array
    unmet_heating = sqlFile.timeSeries(ann_env_pd, 'Timestep', 'Zone Heating Setpoint Not Met Time', model.getThermalZones.first.nameString.upcase).get
    # Create a bool area, initialized to false
    facility_unmet_heating = [false] * unmet_heating.values.size
    facility_unmet_cooling = [false] * unmet_heating.values.size # OpenStudio::Vector.new(unmet_heating.values.size)

    # Minimum people in the zone to count as occupied
    abs_threshold = 1.0
    # zone_hvac_unoccupied_threshold in Standards.ZoneHVACComponent is 0.15 and in PRM it's 10%
    threshold_occ_pct = 0.10 # If the occ schedule is < 10%, we assume it's still really unoccupied

    offending_zones_heating = {}
    offending_zones_cooling = {}

    timestep_hour = 1.0 / model.getTimestep.numberOfTimestepsPerHour

    model.getThermalZones.each do |z|
      upcase_zone_name = z.nameString.upcase

      occupancy = sqlFile.timeSeries(ann_env_pd, 'Timestep', 'Zone People Occupant Count', upcase_zone_name)

      next if occupancy.empty?

      occupancy = occupancy.get

      unmet_heating = sqlFile.timeSeries(ann_env_pd, 'Timestep', 'Zone Heating Setpoint Not Met Time', upcase_zone_name)
      raise if unmet_heating.empty?

      unmet_heating = unmet_heating.get.values

      unmet_cooling = sqlFile.timeSeries(ann_env_pd, 'Timestep', 'Zone Cooling Setpoint Not Met Time', upcase_zone_name)
      raise if unmet_cooling.empty?

      unmet_cooling = unmet_cooling.get.values

      threshold = [threshold_occ_pct * z.numberOfPeople, abs_threshold].max

      this_unmet_heating = 0
      this_unmet_cooling = 0
      occupancy.values.each_with_index do |occ, i|
        next if occ < threshold

        if unmet_heating[i] > 0.0
          facility_unmet_heating[i] = true
          this_unmet_heating += timestep_hour
        end
        if unmet_cooling[i] > 0.0
          facility_unmet_cooling[i] = true
          this_unmet_cooling += timestep_hour
        end
      end
      if this_unmet_heating >= 300
        offending_zones_heating[z.nameString] = this_unmet_heating
      end
      if this_unmet_cooling >= 300
        offending_zones_cooling[z.nameString] = this_unmet_cooling
      end
    end

    unless offending_zones_heating.empty?
      runner.registerValue('offending_zones_heating', offending_zones_heating.to_json)
    end
    unless offending_zones_cooling.empty?
      runner.registerValue('offending_zones_cooling', offending_zones_cooling.to_json)
    end

    timestep_hour = 1.0 / model.getTimestep.numberOfTimestepsPerHour

    n_unmet_heating_thresholded = facility_unmet_heating.count(true) * timestep_hour
    runner.registerValue('out_hours_heating_setpoint_not_met_when_occupied_with_more_than_10_percent_occ', n_unmet_heating_thresholded.round(3))
    runner.registerInfo("Time Setpoint Not Met During Occupied Heating (> 10%): #{n_unmet_heating_thresholded.round(2)}.")

    n_unmet_cooling_thresholded = facility_unmet_cooling.count(true) * timestep_hour
    runner.registerValue('out_hours_cooling_setpoint_not_met_when_occupied_with_more_than_10_percent_occ', n_unmet_cooling_thresholded.round(3))
    runner.registerInfo("Time Setpoint Not Met During Occupied Cooling (> 10%): #{n_unmet_cooling_thresholded.round(2)}.")

    # total electricity consumption for building
    runner.registerInfo("Total Electricity for Building: #{out_total_electricity_gj} GJ")
    out_site_electricity_eui_kwh_per_m_2 = OpenStudio.convert(out_total_electricity_gj, 'GJ', 'kWh').get / in_floor_area_m_2
    runner.registerValue('out_site_electricity_eui_kwh_per_m_2', out_site_electricity_eui_kwh_per_m_2.round(3), 'kWh/m2')

    # total electricity consumption for HVAC (heating)
    runner.registerInfo("Total Electricity for Heating: #{out_total_electricity_heating_gj} GJ")
    runner.registerValue('out_total_electricity_heating_gj', out_total_electricity_heating_gj.round(3), 'GJ')
    out_total_electricity_179_d_gj += out_total_electricity_heating_gj

    # total electricity consumption for HVAC (cooling)
    runner.registerInfo("Total Electricity for Cooling: #{out_total_electricity_cooling_gj} GJ")
    runner.registerValue('out_total_electricity_cooling_gj', out_total_electricity_cooling_gj.round(3), 'GJ')
    out_total_electricity_179_d_gj += out_total_electricity_cooling_gj

    # total electricity consumption for HVAC (fan)
    runner.registerInfo("Total Electricity for Fan: #{out_total_electricity_fan_gj} GJ")
    runner.registerValue('out_total_electricity_fan_gj', out_total_electricity_fan_gj.round(3), 'GJ')
    out_total_electricity_179_d_gj += out_total_electricity_fan_gj

    # total electricity consumption for HVAC (pump)
    runner.registerInfo("Total Electricity for Pump: #{out_total_electricity_pump_gj} GJ")
    runner.registerValue('out_total_electricity_pump_gj', out_total_electricity_pump_gj.round(3), 'GJ')
    out_total_electricity_179_d_gj += out_total_electricity_pump_gj

    # total electricity consumption for HVAC (heat rejection)
    runner.registerInfo("Total Electricity for Heat Rejection: #{out_total_electricity_heatrejection_gj} GJ")
    runner.registerValue('out_total_electricity_heatrejection_gj', out_total_electricity_heatrejection_gj.round(3), 'GJ')
    out_total_electricity_179_d_gj += out_total_electricity_heatrejection_gj

    # total electricity consumption for HVAC (humidification)
    runner.registerInfo("Total Electricity for Humidification: #{out_total_electricity_humidification_gj} GJ")
    runner.registerValue('out_total_electricity_humidification_gj', out_total_electricity_humidification_gj.round(3), 'GJ')
    out_total_electricity_179_d_gj += out_total_electricity_humidification_gj

    # total electricity consumption for HVAC (heat recovery)
    runner.registerInfo("Total Electricity for Heat Recovery: #{out_total_electricity_heatrecovery_gj} GJ")
    runner.registerValue('out_total_electricity_heatrecovery_gj', out_total_electricity_heatrecovery_gj.round(3), 'GJ')
    out_total_electricity_179_d_gj += out_total_electricity_heatrecovery_gj

    # total electricity consumption for interior lighting
    # Subtract the proportional share belonging to non-fully-conditioned zones.
    # The correction uses each zone's installed lighting wattage (space.lightingPower)
    # as a proxy for its share of the annual lighting electricity.  This is valid when
    # FC and non-FC zones use similar lighting schedule shapes (validated above).
    if non_fc_zones.any?
      non_fc_lighting_w = non_fc_zones.sum { |z| z.spaces.sum(&:lightingPower) }
      total_lighting_w = model.getBuilding.lightingPower
      if total_lighting_w > 0 && non_fc_lighting_w > 0
        non_fc_lighting_fraction = non_fc_lighting_w / total_lighting_w
        lighting_non_fc_gj = out_total_electricity_lighting_interior_gj * non_fc_lighting_fraction
        runner.registerInfo(
          "Non-FC lighting: #{non_fc_lighting_w.round(1)} W of #{total_lighting_w.round(1)} W total " \
          "(fraction=#{non_fc_lighting_fraction.round(4)}); subtracting #{lighting_non_fc_gj.round(3)} GJ from 179D interior lighting."
        )
        out_total_electricity_lighting_interior_gj -= lighting_non_fc_gj
      end
    end
    runner.registerInfo("Total Electricity for Interior Lighting (FC zones only): #{out_total_electricity_lighting_interior_gj} GJ")
    runner.registerValue('out_total_electricity_lighting_interior_gj', out_total_electricity_lighting_interior_gj.round(3), 'GJ')
    out_total_electricity_179_d_gj += out_total_electricity_lighting_interior_gj

    # total electricity consumption for water system
    runner.registerInfo("Total Electricity for Water System: #{out_total_electricity_watersystem_gj} GJ")
    runner.registerValue('out_total_electricity_watersystem_gj', out_total_electricity_watersystem_gj.round(3), 'GJ')
    out_total_electricity_179_d_gj += out_total_electricity_watersystem_gj

    # total electricity consumption for 179D
    runner.registerInfo("Total Electricity for 179D: #{out_total_electricity_179_d_gj} GJ")
    runner.registerValue('out_total_electricity_179_d_gj', out_total_electricity_179_d_gj.round(3), 'GJ')

    # total gas consumption for building
    runner.registerInfo("Total Gas for Building: #{out_total_gas_gj} GJ")
    out_site_gas_eui_kwh_per_m_2 = OpenStudio.convert(out_total_gas_gj, 'GJ', 'kWh').get / in_floor_area_m_2
    runner.registerValue('out_site_gas_eui_kwh_per_m_2', out_site_gas_eui_kwh_per_m_2.round(3), 'kWh/m2')

    # total gas consumption for HVAC (heating)
    runner.registerInfo("Total Gas for Heating: #{out_total_gas_heating_gj} GJ")
    runner.registerValue('out_total_gas_heating_gj', out_total_gas_heating_gj.round(3), 'GJ')
    out_total_gas_179_d_gj += out_total_gas_heating_gj

    # total gas consumption for HVAC (cooling)
    runner.registerInfo("Total Gas for Cooling: #{out_total_gas_cooling_gj} GJ")
    out_total_gas_179_d_gj += out_total_gas_cooling_gj

    # total gas consumption for HVAC (fan)
    runner.registerInfo("Total Gas for Fan: #{out_total_gas_fan_gj} GJ")
    out_total_gas_179_d_gj += out_total_gas_fan_gj

    # total gas consumption for HVAC (pump)
    runner.registerInfo("Total Gas for Pump: #{out_total_gas_pump_gj} GJ")
    out_total_gas_179_d_gj += out_total_gas_pump_gj

    # total gas consumption for HVAC (heat rejection)
    runner.registerInfo("Total Gas for Heat Rejection: #{out_total_gas_heatrejection_gj} GJ")
    out_total_gas_179_d_gj += out_total_gas_heatrejection_gj

    # total gas consumption for HVAC (humidification)
    runner.registerInfo("Total Gas for Humidification: #{out_total_gas_humidification_gj} GJ")
    out_total_gas_179_d_gj += out_total_gas_humidification_gj

    # total gas consumption for HVAC (heat recovery)
    runner.registerInfo("Total Gas for Heat Recovery: #{out_total_gas_heatrecovery_gj} GJ")
    out_total_gas_179_d_gj += out_total_gas_heatrecovery_gj

    # total gas consumption for interior lighting
    runner.registerInfo("Total Gas for Interior Lighting: #{out_total_gas_lighting_interior_gj} GJ")
    out_total_gas_179_d_gj += out_total_gas_lighting_interior_gj

    # total gas consumption for water system
    runner.registerInfo("Total Gas for Water System: #{out_total_gas_watersystem_gj} GJ")
    runner.registerValue('out_total_gas_watersystem_gj', out_total_gas_watersystem_gj.round(3), 'GJ')
    out_total_gas_179_d_gj += out_total_gas_watersystem_gj

    # total gas consumption for water system
    runner.registerInfo("Total Gas for 179D: #{out_total_gas_179_d_gj} GJ")
    runner.registerValue('out_total_gas_179_d_gj', out_total_gas_179_d_gj.round(3), 'GJ')

    # -------------------------------------------------------------------
    # puts'### close the sql file')
    # -------------------------------------------------------------------
    sqlFile.close

    # -------------------------------------------------------------------
    # Read and parse eplusout.err for warning/severe error counts
    # -------------------------------------------------------------------
    if add_warning_severe_counts
      # get eplusout.err file path
      err_file_class = runner.workflow.findFile('run/eplusout.err')
      err_file_path = nil
      if err_file_class.is_initialized
        err_file_path = err_file_class.get.to_s
      else
        runner.registerError('cannot find eplusout.err file')
        return false
      end

      # check if file exists
      if File.exist?(err_file_path)
        # initialize variables
        warmup_warnings = 0
        warmup_severe = 0
        sizing_warnings = 0
        sizing_severe = 0
        sim_warnings = 0
        sim_severe = 0
        simhvac_max_iter_count = 0

        # loop through each line and extract warning/severe counts
        lines = File.readlines(err_file_path)
        lines.each_with_index do |line, index|
          case line
          # counts during warmup
          when /Warmup Error Summary.*?(\d+) Warning; (\d+) Severe/
            warmup_warnings = Regexp.last_match(1).to_i
            warmup_severe = Regexp.last_match(2).to_i
          # counts during sizing
          when /Sizing Error Summary.*?(\d+) Warning; (\d+) Severe/
            sizing_warnings = Regexp.last_match(1).to_i
            sizing_severe = Regexp.last_match(2).to_i
          # counts during simulation
          when /Completed Successfully-- (\d+) Warning; (\d+) Severe/
            sim_warnings = Regexp.last_match(1).to_i
            sim_severe = Regexp.last_match(2).to_i
          # counts for other specifics: SimHVAC max iteration exceeding
          when /\*+\s+\*\* Warning \*\* SimHVAC: Exceeding Maximum iterations/
            1.upto(3) do |i|
              next_line = lines[index + i]
              break unless next_line

              if next_line =~ /\*+\s+\*\*   ~~~   \*\*   This error occurred (\d+) total times;/
                simhvac_max_iter_count = Regexp.last_match(1).to_i
                break
              end
            end
          end
        end

        # Register values
        runner.registerValue('out_eplusout_warmup_warning_count', warmup_warnings)
        runner.registerValue('out_eplusout_warmup_severe_count', warmup_severe)
        runner.registerValue('out_eplusout_sizing_warning_count', sizing_warnings)
        runner.registerValue('out_eplusout_sizing_severe_count', sizing_severe)
        runner.registerValue('out_eplusout_sim_warning_count', sim_warnings)
        runner.registerValue('out_eplusout_sim_warning_count_simhvac_max_iter', simhvac_max_iter_count)
        runner.registerValue('out_eplusout_sim_severe_count', sim_severe)
      else
        runner.registerWarning("eplusout.err file not found in #{File.dirname(__FILE__)}")
      end
    end

    true
  end
end

# register the measure to be used by the application
Reporting179D.new.registerWithApplication
