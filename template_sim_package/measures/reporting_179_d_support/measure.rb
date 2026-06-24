# *******************************************************************************
# OpenStudio(R), Copyright (c) Alliance for Sustainable Energy, LLC.
# See also https://openstudio.net/license
# *******************************************************************************

# see the URL below for information on how to write OpenStuido measures
# http://openstudio.nrel.gov/openstudio-measure-writing-guide

# see the URL below for access to C++ documentation on mondel objects (click on "model" in the main window to view model objects)
# http://openstudio.nrel.gov/sites/openstudio.nrel.gov/files/nv_data/cpp_documentation_it/model/html/namespaces.html

require 'openstudio-standards'

# start the measure
class Reporting179DSupport < OpenStudio::Measure::ModelMeasure
  # define the name that a user will see, this method may be deprecated as
  # the display name in PAT comes from the name field in measure.xml
  def name
    return 'reporting 179D support'
  end

  # human readable description
  def description
    return ''
  end

  # human readable description of modeling approach
  def modeler_description
    return 'Note that this is a ModelMeasure, not a ReportingMeasure even though it acts like one. It will run a sizing run for you to retrieve some capacities from the SQL'
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

  def get_window_props_from_subsurfaces(window_property, subsurfaces, surface_type, window_type, sqlFile)
    if window_property[surface_type].nil?
      window_property[surface_type] = {}
    end

    subsurfaces.each do |subsurface|
      subsurface_name = subsurface.name.to_s
      if subsurface.subSurfaceType == window_type
        # get SHGC
        var_val_query = "SELECT Value FROM TabularDataWithStrings WHERE ReportName = 'EnvelopeSummary' AND ReportForString = 'Entire Facility' AND TableName = 'Exterior Fenestration' AND RowName = '#{subsurface_name.upcase}' AND ColumnName = 'Glass SHGC'"
        val = sqlFile.execAndReturnFirstDouble(var_val_query)
        window_shgc = val.to_f.round(3)
        # get U-value
        var_val_query = "SELECT Value FROM TabularDataWithStrings WHERE ReportName = 'EnvelopeSummary' AND ReportForString = 'Entire Facility' AND TableName = 'Exterior Fenestration' AND RowName = '#{subsurface_name.upcase}' AND ColumnName = 'Glass U-Factor' AND Units = 'W/m2-K'"
        val = sqlFile.execAndReturnFirstDouble(var_val_query)
        window_u_value = val.to_f.round(3)
        # get opening area
        var_val_query = "SELECT Value FROM TabularDataWithStrings WHERE ReportName = 'EnvelopeSummary' AND ReportForString = 'Entire Facility' AND TableName = 'Exterior Fenestration' AND RowName = '#{subsurface_name.upcase}' AND ColumnName = 'Area of Multiplied Openings' AND Units = 'm2'"
        val = sqlFile.execAndReturnFirstDouble(var_val_query)
        window_area = val.to_f.round(3)
        # initialize hash
        if window_property[surface_type][window_type].nil?
          window_property[surface_type][window_type] = {}
        end
        if window_property[surface_type][window_type].nil?
          window_property[surface_type][window_type] = {}
        end
        if window_property[surface_type][window_type][subsurface_name].nil?
          window_property[surface_type][window_type][subsurface_name] = {}
        end
        # add window information to hash
        window_property[surface_type][window_type][subsurface_name]['area_m2'] = window_area
        window_property[surface_type][window_type][subsurface_name]['shgc'] = window_shgc
        window_property[surface_type][window_type][subsurface_name]['u_value'] = window_u_value
      end
    end
    return window_property
  end

  # helper method to access report variable data
  def sql_get_report_variable_data_double(runner, sql, object, variable_name)
    value = 0.0
    var_data_id_query = "SELECT ReportVariableDataDictionaryIndex FROM ReportVariableDataDictionary WHERE VariableName = '#{variable_name}' AND ReportingFrequency = 'Run Period' AND KeyValue = '#{object.name.get.to_s.upcase}'"
    var_data_id = sql.execAndReturnFirstDouble(var_data_id_query)
    if var_data_id.is_initialized
      var_val_query = "SELECT VariableValue FROM ReportVariableData WHERE ReportVariableDataDictionaryIndex = '#{var_data_id.get}'"
      val = sql.execAndReturnFirstDouble(var_val_query)
      if val.is_initialized
        value = val.get
      else
        runner.registerWarning("'#{variable_name}' not available for #{object.iddObjectType} '#{object.name}'.")
      end
    else
      runner.registerWarning("'#{variable_name}' not available for #{object.iddObjectType} '#{object.name}'.")
    end
    return value
  end

  # helper method for extracting AHRI ratings
  def ahri_rating_extraction_coil(runner, model, ahri_rating_map_coil, std)
    ahri_rating_map_coil.each do |table_name, coils_and_metrics|
      label = nil
      if table_name.downcase.include?('heating')
        label = 'heating'
      elsif table_name.downcase.include?('cooling')
        label = 'cooling'
      else
        runner.registerError("cannot get label from ahri rating map: #{table_name}")
        return false
      end
      coils_and_metrics['metrics'].each do |metric|
        # initialize variables
        ahri_rating_weighted_sum = 0.0
        capacity_btu_per_hr_total = 0.0
        ahri_rating_weighted_sum = 0.0
        metric_name_revised = metric.gsub(' ', '_').downcase

        coils_and_metrics['coils'].sort.each do |coil|
          coil_design_cop = 0.0
          capacity_w = 0.0

          # get design airflow depending on coil type
          rated_air_flow_rate_m_3_per_s = 0.0
          if coil.to_CoilCoolingDXSingleSpeed.is_initialized
            coil = coil.to_CoilCoolingDXSingleSpeed.get
            if coil.ratedAirFlowRate.is_initialized
              rated_air_flow_rate_m_3_per_s = coil.ratedAirFlowRate.get
            elsif coil.autosizedRatedAirFlowRate.is_initialized
              rated_air_flow_rate_m_3_per_s = coil.autosizedRatedAirFlowRate.get
            else
              runner.registerWarning("Cooling coil rated airflow not available for coil '#{coil.name}'.")
            end
            if coil.ratedTotalCoolingCapacity.is_initialized
              capacity_w = coil.ratedTotalCoolingCapacity.get
            elsif coil.autosizedRatedTotalCoolingCapacity.is_initialized
              capacity_w = coil.autosizedRatedTotalCoolingCapacity.get
            else
              runner.registerWarning("Cooling coil capacity not available for coil '#{coil.name}'.")
            end
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
            if coil.ratedHighSpeedAirFlowRate.is_initialized
              rated_air_flow_rate_m_3_per_s = coil.ratedHighSpeedAirFlowRate.get
            elsif coil.autosizedRatedHighSpeedAirFlowRate.is_initialized
              rated_air_flow_rate_m_3_per_s = coil.autosizedRatedHighSpeedAirFlowRate.get
            else
              runner.registerWarning("Cooling coil rated airflow not available for coil '#{coil.name}'.")
            end
            if coil.ratedHighSpeedTotalCoolingCapacity.is_initialized
              capacity_w = coil.ratedHighSpeedTotalCoolingCapacity.get
            elsif coil.autosizedRatedHighSpeedTotalCoolingCapacity.is_initialized
              capacity_w = coil.autosizedRatedHighSpeedTotalCoolingCapacity.get
            else
              runner.registerWarning("Cooling coil capacity not available for coil '#{coil.name}'.")
            end
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
            temp_rated_air_flow_rate_m_3_per_s = 0.0
            coil.stages.each do |stage|
              if stage.ratedAirFlowRate.is_initialized
                temp_rated_air_flow_rate_m_3_per_s = stage.ratedAirFlowRate.get
              elsif stage.autosizedRatedAirFlowRate.is_initialized
                temp_rated_air_flow_rate_m_3_per_s = stage.autosizedRatedAirFlowRate.get
              else
                runner.registerWarning("Cooling coil rated airflow not available for coil stage '#{stage.name}'.")
              end
              if stage.grossRatedTotalCoolingCapacity.is_initialized
                temp_capacity_w = stage.grossRatedTotalCoolingCapacity.get
              elsif stage.autosizedGrossRatedTotalCoolingCapacity.is_initialized
                temp_capacity_w = stage.autosizedGrossRatedTotalCoolingCapacity.get
              else
                runner.registerWarning("Cooling coil capacity not available for coil stage '#{stage.name}'.")
              end
              temp_coil_design_cop = stage.grossRatedCoolingCOP
              coil_design_cop = temp_coil_design_cop if temp_capacity_w >= capacity_w
              capacity_w = temp_capacity_w if temp_capacity_w > capacity_w
              rated_air_flow_rate_m_3_per_s = temp_rated_air_flow_rate_m_3_per_s if temp_rated_air_flow_rate_m_3_per_s > rated_air_flow_rate_m_3_per_s
            end
          elsif coil.to_CoilCoolingDXVariableSpeed.is_initialized
            coil = coil.to_CoilCoolingDXVariableSpeed.get
            coil.speeds.each do |speed|
              temp_capacity_w = speed.referenceUnitGrossRatedTotalCoolingCapacity
              temp_rated_air_flow_rate_m_3_per_s = speed.referenceUnitRatedAirFlowRate
              temp_coil_design_cop = speed.referenceUnitGrossRatedCoolingCOP
              coil_design_cop = temp_coil_design_cop if temp_capacity_w >= capacity_w
              capacity_w = temp_capacity_w if temp_capacity_w > capacity_w
              rated_air_flow_rate_m_3_per_s = temp_rated_air_flow_rate_m_3_per_s if temp_rated_air_flow_rate_m_3_per_s > rated_air_flow_rate_m_3_per_s
            end
          elsif coil.to_CoilHeatingDXSingleSpeed.is_initialized
            coil = coil.to_CoilHeatingDXSingleSpeed.get
            if coil.ratedAirFlowRate.is_initialized
              rated_air_flow_rate_m_3_per_s = coil.ratedAirFlowRate.get
            elsif coil.autosizedRatedAirFlowRate.is_initialized
              rated_air_flow_rate_m_3_per_s = coil.autosizedRatedAirFlowRate.get
            else
              runner.registerWarning("Heating coil rated airflow not available for coil '#{coil.name}'.")
            end
            if coil.ratedTotalHeatingCapacity.is_initialized
              capacity_w = coil.ratedTotalHeatingCapacity.get
            elsif coil.autosizedRatedTotalHeatingCapacity.is_initialized
              capacity_w = coil.autosizedRatedTotalHeatingCapacity.get
            else
              runner.registerWarning("Heating coil capacity not available for coil '#{coil.name}'.")
            end
            coil_design_cop = coil.ratedCOP
          elsif coil.to_CoilHeatingDXMultiSpeed.is_initialized
            coil = coil.to_CoilHeatingDXMultiSpeed.get
            coil.stages.each do |stage|
              if stage.ratedAirFlowRate.is_initialized
                temp_rated_air_flow_rate_m_3_per_s = stage.ratedAirFlowRate.get
              elsif stage.autosizedRatedAirFlowRate.is_initialized
                temp_rated_air_flow_rate_m_3_per_s = stage.autosizedRatedAirFlowRate.get
              else
                runner.registerWarning("Heating coil rated airflow not available for coil stage '#{stage.name}'.")
              end
              if stage.grossRatedHeatingCapacity.is_initialized
                temp_capacity_w = stage.grossRatedHeatingCapacity.get
              elsif stage.autosizedGrossRatedHeatingCapacity.is_initialized
                temp_capacity_w = stage.autosizedGrossRatedHeatingCapacity.get
              else
                runner.registerWarning("Heating coil capacity not available for coil stage '#{stage.name}'.")
              end
              temp_coil_design_cop = stage.grossRatedHeatingCOP
              coil_design_cop = temp_coil_design_cop if temp_capacity_w >= capacity_w
              capacity_w = temp_capacity_w if temp_capacity_w > capacity_w
              rated_air_flow_rate_m_3_per_s = temp_rated_air_flow_rate_m_3_per_s if temp_rated_air_flow_rate_m_3_per_s > rated_air_flow_rate_m_3_per_s
            end
          elsif coil.to_CoilHeatingDXVariableSpeed.is_initialized
            coil = coil.to_CoilHeatingDXVariableSpeed.get
            coil.speeds.each do |speed|
              temp_capacity_w = speed.referenceUnitGrossRatedHeatingCapacity
              temp_rated_air_flow_rate_m_3_per_s = speed.referenceUnitRatedAirFlowRate
              temp_coil_design_cop = speed.referenceUnitGrossRatedHeatingCOP
              coil_design_cop = temp_coil_design_cop if temp_capacity_w >= capacity_w
              capacity_w = temp_capacity_w if temp_capacity_w > capacity_w
              rated_air_flow_rate_m_3_per_s = temp_rated_air_flow_rate_m_3_per_s if temp_rated_air_flow_rate_m_3_per_s > rated_air_flow_rate_m_3_per_s
            end
          else
            runner.registerWarning('Design airflow for DX heating/cooling coil unavailable because of unrecognized coil type.')
          end

          capacity_btu_per_hr = OpenStudio.convert(capacity_w, 'W', 'Btu/hr').get

          # get ahri rating
          case metric
          when 'SEER'
            if capacity_btu_per_hr < 65000
              # 0.4 had cop_to_seer_cooling_no_fan(cop)->seer; in 0.8.2 it
              # was renamed cop_no_fan_to_seer(cop)->seer. The previous
              # call here was seer_to_cop_no_fan, which is the *inverse*
              # function in 0.8.2 (seer->cop), giving SEER values an order
              # of magnitude too low (e.g. 1.369 instead of ~14.5).
              ahri_rating_value = std.cop_no_fan_to_seer(coil_design_cop)
              if ahri_rating_value.is_a?(Complex)
                runner.registerWarning("Discriminant is negative in cop_no_fan_to_seer(), no real solution exists for COP:#{coil_design_cop}.")
                ahri_rating_value = 0
              end
              capacity_btu_per_hr_total += capacity_btu_per_hr
              ahri_rating_weighted_sum += ahri_rating_value * capacity_btu_per_hr
            end
          when 'EER'
            if capacity_btu_per_hr >= 65000
              ahri_rating_value = std.cop_no_fan_to_eer(coil_design_cop, capacity_w)
              capacity_btu_per_hr_total += capacity_btu_per_hr
              ahri_rating_weighted_sum += ahri_rating_value * capacity_btu_per_hr
            end
          when 'HSPF' # inverse of this: https://github.com/NREL/openstudio-standards/blob/7fd7a7ba32a3acf34d63720806e1f7e02708425b/lib/openstudio-standards/prototypes/common/objects/Prototype.utilities.rb#L322

            if capacity_btu_per_hr < 65000
              ahri_rating_value = cop_wo_fan_to_hspf(runner, coil_design_cop)
              capacity_btu_per_hr_total += capacity_btu_per_hr
              ahri_rating_weighted_sum += ahri_rating_value * capacity_btu_per_hr
            end

          when 'COP at 47' # inverse of this: https://github.com/NREL/openstudio-standards/blob/7fd7a7ba32a3acf34d63720806e1f7e02708425b/lib/openstudio-standards/prototypes/common/objects/Prototype.utilities.rb#L308

            if capacity_btu_per_hr >= 65000
              ahri_rating_value = cop_wo_fan_to_cop_heating(coil_design_cop, capacity_btu_per_hr)
              capacity_btu_per_hr_total += capacity_btu_per_hr
              ahri_rating_weighted_sum += ahri_rating_value * capacity_btu_per_hr
            end

          end
        end
        ahri_rating_weighted_average = capacity_btu_per_hr_total > 0.0 ? ahri_rating_weighted_sum / capacity_btu_per_hr_total : 0.0
        runner.registerValue("in_hvac_dx_#{label}_ahri_#{metric_name_revised}_proposed", ahri_rating_weighted_average.round(3))
      end
    end
  end

  # inverse of this: https://github.com/NREL/openstudio-standards/blob/11eec2c1a2842af4c86eb981288b3527702b78e7/lib/openstudio-standards/prototypes/common/objects/Prototype.utilities.rb#L318
  def cop_wo_fan_to_hspf(runner, cop)
    a = -0.0296
    b = 0.7134
    c = -cop
    discriminant = (b**2) - (4 * a * c)
    if discriminant < 0
      runner.registerWarning("Discriminant is negative in cop_wo_fan_to_hspf(), no real solution exists for COP:#{cop}.")
      return 0
    end

    hspf1 = (-b + Math.sqrt(discriminant)) / (2 * a)
    hspf2 = (-b - Math.sqrt(discriminant)) / (2 * a)

    # Return the positive solution (as HSPF should be positive)
    return hspf1 > 0 ? hspf1 : hspf2
  end

  # inverse of this: https://github.com/NREL/openstudio-standards/blob/11eec2c1a2842af4c86eb981288b3527702b78e7/lib/openstudio-standards/prototypes/common/objects/Prototype.utilities.rb#L293
  def cop_wo_fan_to_cop_heating(cop, cap_btu_per_hr)
    a = 1.48E-7
    b = 1.062

    cop_w_fan_at_47_f = cop / ((a * cap_btu_per_hr) + b)

    return cop_w_fan_at_47_f
  end

  # helper method for extracting AHRI ratings
  def ahri_rating_extraction_chiller(runner, ahri_rating_map_chiller, sqlFile)
    ahri_rating_map_chiller.each do |table_name, chillers_and_metrics|
      chillers_and_metrics['metric_unit_pairs'].each do |metric, unit|
        # initialize variables
        ahri_rating_weighted_sum = 0.0
        chiller_total_capacity_w = 0.0
        metric_name_revised = metric.gsub(' ', '_').downcase

        chillers_and_metrics['chillers'].sort.each do |chiller|
          rowname = chiller.name.to_s.upcase
          ahri_rating = 0.0
          capacity_w = 0.0

          # get chiller capacity
          if chiller.referenceCapacity.is_initialized
            capacity_w = chiller.referenceCapacity.get
          elsif chiller.autosizedReferenceCapacity.is_initialized
            capacity_w = chiller.autosizedReferenceCapacity.get
          else
            runner.registerWarning("Chiller capacity not available for chiller '#{chiller.name}'.")
          end

          # get ahri rating
          var_val_query = "SELECT Value FROM TabularDataWithStrings WHERE ReportName = 'EquipmentSummary' AND ReportForString = 'Entire Facility' AND TableName = '#{table_name}' AND RowName = '#{rowname}' AND ColumnName = '#{metric}' AND Units = '#{unit}'"
          val = sqlFile.execAndReturnFirstDouble(var_val_query)
          if val.is_initialized
            ahri_rating = val.get
          else
            runner.registerWarning("#{metric_name_revised} not available")
          end

          # calc weighted metrics
          chiller_total_capacity_w += capacity_w
          ahri_rating_weighted_sum += ahri_rating * capacity_w
        end
        ahri_rating_weighted_average = chiller_total_capacity_w > 0.0 ? ahri_rating_weighted_sum / chiller_total_capacity_w : 0.0
        runner.registerValue("in_hvac_chiller_capacity_weighted_ahri_#{metric_name_revised}_proposed", ahri_rating_weighted_average.round(3))
      end
    end
  end

  # define the arguments that the user will input
  def arguments(_model)
    args = OpenStudio::Measure::OSArgumentVector.new

    # make an argument for disable_upstream_arg
    disable_upstream_arg = OpenStudio::Measure::OSArgument.makeBoolArgument('disable_upstream_arg', true)
    disable_upstream_arg.setDisplayName('Disable unstream argument search?')
    disable_upstream_arg.setDefaultValue(false)
    args << disable_upstream_arg

    # get hvac type from hvac upgrade measure instead of from create_typical measure
    hvac_type_from_upgrade = OpenStudio::Measure::OSArgument.makeBoolArgument('hvac_type_from_upgrade', true)
    hvac_type_from_upgrade.setDisplayName('Get hvac type from upgrade measure?')
    hvac_type_from_upgrade.setDefaultValue(false)
    args << hvac_type_from_upgrade

    return args
  end

  # define what happens when the measure is run
  def run(model, runner, user_arguments)
    super(model, runner, user_arguments)

    if !ENV['SAVE_INTERMEDIATE'].nil?
      model.save('18.1.before_reporting_179_d_support.osm')
    end

    # use the built-in error checking
    return false unless runner.validateUserArguments(arguments(model), user_arguments)

    ############################################################################################
    # initial condition
    ############################################################################################
    outputVariables = model.getOutputVariables
    meters = model.getOutputMeters
    runner.registerInitialCondition("The model started with #{meters.size} meter objects and #{outputVariables.size} output variable objects.")

    ############################################################################################
    # puts'### get arguments')
    ############################################################################################
    # use the built-in error checking
    if !runner.validateUserArguments(arguments(model), user_arguments)
      return false
    end

    disable_upstream_arg = runner.getBoolArgumentValue('disable_upstream_arg', user_arguments)
    hvac_type_from_upgrade = runner.getBoolArgumentValue('hvac_type_from_upgrade', user_arguments)

    ############################################################################################
    # puts('### load standard')
    ############################################################################################
    std = Standard.build('ComStock 90.1-2013')

    parent_dir = File.dirname(Dir.pwd)
    look_for_dir = 'hardsize_model'
    look_for_sr = 'hardsize_model_SR'
    measure_run_dirs = Dir.glob(File.join(parent_dir, "*#{look_for_dir}*/#{look_for_sr}*/"))
    sqlpath = measure_run_dirs.find do |dir|
      sql = Dir.glob(File.join(dir, 'run/eplusout.sql'))
      sql.any?
    end
    if sqlpath
      sqlpath = File.join(sqlpath, 'run/eplusout.sql')
      runner.registerInfo("### extract existing sizing run sql from previous step: #{sqlpath}")
    else
      ############################################################################################
      runner.registerInfo('### sizing run sql does not exist - do sizing run to generate sql')
      ############################################################################################
      # Run a sizing run to determine equipment capacities and flow rates
      if std.model_run_sizing_run(model, "#{Dir.pwd}/SR") == false
        runner.registerError('Sizing run for Hardsize model failed, cannot hard-size model.')
        runner.registerInfo('Sizing run for Hardsize model failed, cannot hard-size model.')
        return false
      end
      sqlpath = "#{Dir.pwd}/SR/run/eplusout.sql"
    end
    # puts("--- sqlpath = #{sqlpath}")
    sqlFile = OpenStudio::SqlFile.new(sqlpath)

    ############################################################################################
    # get parameters from upstream workflow
    ############################################################################################
    if disable_upstream_arg == false
      # hvac system type
      if hvac_type_from_upgrade
        in_hvac_system_type_proposed = runner.getPastStepValuesForName('system_type_hvac_upgrade').values.first
      else
        in_hvac_system_type_proposed = runner.getPastStepValuesForName('system_type').values.first
      end
      runner.registerValue('in_hvac_system_type_proposed', in_hvac_system_type_proposed) unless in_hvac_system_type_proposed.nil?
    end

    ############################################################################################
    # register values before PRM measure replaces parameters
    ############################################################################################
    in_interior_lighting_lpd_w_per_m_2_proposed = model.getBuilding.lightingPowerPerFloorArea
    runner.registerValue('in_interior_lighting_lpd_w_per_m_2_proposed', in_interior_lighting_lpd_w_per_m_2_proposed.round(3), 'W/m^2')

    # calculate exterior surface properties
    in_roof_absorptance_times_area = 0
    in_roof_ua_si = 0.0
    in_roof_area_m_2 = 0.0
    in_ext_wall_ua_si = 0.0
    in_exterior_wall_area_m_2_proposed = 0.0
    window_property = {}
    model.getSpaces.sort.each do |space|
      space.surfaces.each do |surface|
        next if surface.outsideBoundaryCondition != 'Outdoors'

        case surface.surfaceType.to_s
        when 'RoofCeiling'
          surface_absorptance = surface.exteriorVisibleAbsorptance.is_initialized ? surface.exteriorVisibleAbsorptance.get : 0.0
          surface_u_value_si = surface.uFactor.is_initialized ? surface.uFactor.get : 0.0
          surface_area_m_2 = surface.netArea
          surface_ua_si = surface_u_value_si * surface_area_m_2
          in_roof_absorptance_times_area += surface_absorptance * surface_area_m_2
          in_roof_ua_si += surface_ua_si
          in_roof_area_m_2 += surface_area_m_2
          window_property = get_window_props_from_subsurfaces(window_property, surface.subSurfaces, 'RoofCeiling', 'FixedWindow', sqlFile)
        when 'Wall'
          surface_u_value_si = surface.uFactor.is_initialized ? surface.uFactor.get : 0.0
          surface_area_m_2 = surface.netArea
          surface_ua_si = surface_u_value_si * surface_area_m_2
          in_ext_wall_ua_si += surface_ua_si
          in_exterior_wall_area_m_2_proposed += surface_area_m_2
          window_property = get_window_props_from_subsurfaces(window_property, surface.subSurfaces, 'Wall', 'FixedWindow', sqlFile)
        end
      end
    end

    # average roof U-value
    if in_roof_area_m_2 > 0
      in_average_roof_u_value_si_proposed = in_roof_ua_si / in_roof_area_m_2
      runner.registerValue('in_average_roof_u_value_si_proposed', in_average_roof_u_value_si_proposed.round(5))
    else
      runner.registerWarning('Roof area is zero. Cannot calculate average U-value.')
    end

    # average roof absorptance
    if in_roof_area_m_2 > 0
      in_average_roof_absorptance_proposed = in_roof_absorptance_times_area / in_roof_area_m_2
      runner.registerValue('in_average_roof_absorptance_proposed', in_average_roof_absorptance_proposed.round(5))
    else
      runner.registerWarning('Roof area is zero. Cannot calculate average absorptance.')
    end

    # average wall U-value
    if in_exterior_wall_area_m_2_proposed > 0
      in_average_ext_wall_u_value_si_proposed = in_ext_wall_ua_si / in_exterior_wall_area_m_2_proposed
      runner.registerValue('in_exterior_wall_area_m_2_proposed', in_exterior_wall_area_m_2_proposed.round(2))
      runner.registerValue('in_average_ext_wall_u_value_si_proposed', in_average_ext_wall_u_value_si_proposed.round(5), 'W/m^2*K')
    else
      runner.registerWarning('Exterior wall area is zero. Cannot calculate average U-value.')
    end

    # total window area
    in_window_area_m_2_proposed = 0
    var_val_query = "SELECT Value FROM TabularDataWithStrings WHERE ReportName = 'EnvelopeSummary' AND ReportForString = 'Entire Facility' AND TableName = 'Exterior Fenestration' AND RowName = 'Total or Average' AND ColumnName = 'Area of Multiplied Openings' AND Units = 'm2'"
    val = sqlFile.execAndReturnFirstDouble(var_val_query)
    if val.is_initialized
      in_window_area_m_2_proposed = val.get
      runner.registerValue('in_window_area_m_2_proposed', in_window_area_m_2_proposed.round(2))
    else
      runner.registerWarning('Overall window area not available.')
    end

    # Average window U-value
    var_val_query = "SELECT Value FROM TabularDataWithStrings WHERE ReportName = 'EnvelopeSummary' AND ReportForString = 'Entire Facility' AND TableName = 'Exterior Fenestration' AND RowName = 'Total or Average' AND ColumnName = 'Glass U-Factor' AND Units = 'W/m2-K'"
    val = sqlFile.execAndReturnFirstDouble(var_val_query)
    if val.is_initialized
      in_window_u_value_w_per_m_2_k_overall_proposed = val.get
      runner.registerValue('in_window_u_value_w_per_m_2_k_overall_proposed', in_window_u_value_w_per_m_2_k_overall_proposed)
    else
      runner.registerWarning('Overall average window U-value not available.')
    end

    # Average window SHGC
    var_val_query = "SELECT Value FROM TabularDataWithStrings WHERE ReportName = 'EnvelopeSummary' AND ReportForString = 'Entire Facility' AND TableName = 'Exterior Fenestration' AND RowName = 'Total or Average' AND ColumnName = 'Glass SHGC'"
    val = sqlFile.execAndReturnFirstDouble(var_val_query)
    if val.is_initialized
      in_window_shgc_overall_proposed = val.get
      runner.registerValue('in_window_shgc_overall_proposed', in_window_shgc_overall_proposed.round(3))
    else
      runner.registerWarning('Overall average window SHGC not available.')
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
      unless window_property[surface_type].empty?
        window_property[surface_type]['FixedWindow'].each do |_surface_name, properties|
          total_area += properties['area_m2']
          weighted_sum_shgc += properties['shgc'] * properties['area_m2']
          weighted_sum_u_value += properties['u_value'] * properties['area_m2']
        end
        weighted_shgc = (weighted_sum_shgc / total_area).round(3)
        weighted_u_value = (weighted_sum_u_value / total_area).round(3)
        runner.registerValue("in_window_shgc_#{window_label_hash[surface_type]}_proposed", weighted_shgc.round(3))
        runner.registerValue("in_window_u_value_w_per_m_2_k_#{window_label_hash[surface_type]}_proposed", weighted_u_value.round(5))
      end
    end

    # building skylight
    var_val_query = "SELECT Value FROM TabularDataWithStrings WHERE ReportName = 'InputVerificationandResultsSummary' AND ReportForString = 'Entire Facility' AND TableName = 'Skylight-Roof Ratio' AND RowName = 'Skylight-Roof Ratio' AND ColumnName = 'Total'"
    val = sqlFile.execAndReturnFirstDouble(var_val_query)
    if val.is_initialized
      in_window_wwr_skylight_proposed = val.get / 100.0
      runner.registerValue('in_window_wwr_skylight_proposed', in_window_wwr_skylight_proposed.round(3))
    else
      runner.registerWarning('Overall window to wall ratio for skylight not available.')
    end

    # building window to wall ratio
    in_window_wwr_overall_proposed = in_window_area_m_2_proposed / (in_exterior_wall_area_m_2_proposed + in_window_area_m_2_proposed)
    runner.registerValue('in_window_wwr_overall_proposed', in_window_wwr_overall_proposed.round(3))

    # Handle fuel output variables that changed in EnergyPlus version 9.4 (Openstudio version >= 3.1)
    elec = 'Electric'
    gas = 'Gas'
    if model.version > OpenStudio::VersionString.new('3.0.1')
      elec = 'Electricity'
      gas = 'NaturalGas'
    end

    # DX cooling coils properties
    dx_cooling_capacity_weighted_design_cop = 0.0
    dx_cooling_airflow_weighted_design_cop = 0.0
    dx_cooling_total_capacity_w = 0.0
    dx_cooling_total_rated_airflow_m_3_per_s = 0.0
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

        # capacity and cop, use cop at highest capacity
        temp_capacity_w = 0.0
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

      # add to weighted load cop
      dx_cooling_capacity_weighted_design_cop += coil_design_cop * capacity_w
      dx_cooling_airflow_weighted_design_cop += rated_air_flow_rate_m_3_per_s * coil_design_cop

      # cooling coil info logging
      runner.registerInfo("Cooling coil '#{coil.name}' has design capacity #{capacity_w.round(2)} W, design cop #{coil_design_cop.round(2)}.")
    end
    in_hvac_dx_cooling_cop_design_capacity_weighted_proposed = dx_cooling_total_capacity_w > 0.0 ? dx_cooling_capacity_weighted_design_cop / dx_cooling_total_capacity_w : 0.0
    in_hvac_dx_cooling_cop_airflow_weighted = dx_cooling_total_rated_airflow_m_3_per_s > 0.0 ? dx_cooling_airflow_weighted_design_cop / dx_cooling_total_rated_airflow_m_3_per_s : 0.0
    runner.registerValue('in_hvac_dx_cooling_cop_design_capacity_weighted_proposed', in_hvac_dx_cooling_cop_design_capacity_weighted_proposed.round(3))
    runner.registerValue('in_hvac_dx_cooling_cop_airflow_weighted_proposed', in_hvac_dx_cooling_cop_airflow_weighted.round(3))

    # DX heating coil capacity, load, and efficiences, including supplemental coils
    dx_heating_capacity_weighted_design_cop = 0.0
    dx_heating_airflow_weighted_design_cop = 0.0
    dx_heating_total_capacity_w = 0.0
    dx_heating_total_rated_airflow_m_3_per_s = 0.0
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
        temp_capacity_w = 0.0
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
    in_hvac_dx_heating_cop_design_capacity_weighted = dx_heating_total_capacity_w > 0.0 ? dx_heating_capacity_weighted_design_cop / dx_heating_total_capacity_w : 0.0
    in_hvac_dx_heating_cop_airflow_weighted = dx_heating_total_rated_airflow_m_3_per_s > 0.0 ? dx_heating_airflow_weighted_design_cop / dx_heating_total_rated_airflow_m_3_per_s : 0.0
    runner.registerValue('in_hvac_dx_heating_cop_design_capacity_weighted_proposed', in_hvac_dx_heating_cop_design_capacity_weighted.round(3))
    runner.registerValue('in_hvac_dx_heating_cop_airflow_weighted_proposed', in_hvac_dx_heating_cop_airflow_weighted.round(3))

    # AHRI ratings (from EnergyPlus calculation)
    ahri_rating_map_coil = {
      'DX Cooling Coils' => {
        'coils' => dx_cooling_coils,
        'metrics' => ['SEER', 'EER'],
      },
      'DX Heating Coils' => {
        'coils' => dx_heating_coils,
        'metrics' => ['HSPF', 'COP at 47'],
      },
    }
    ahri_rating_map_chiller = {
      'Central Plant' => {
        'chillers' => model.getChillerElectricEIRs,
        'metric_unit_pairs' => {
          'IPLV in SI Units' => 'W/W',
        },
      },
    }
    ahri_rating_extraction_coil(runner, model, ahri_rating_map_coil, std)
    ahri_rating_extraction_chiller(runner, ahri_rating_map_chiller, sqlFile)

    # service water heating fuel type
    water_heaters = []
    in_swh_fuel_type = []
    model.getWaterHeaterMixeds.each { |wh| water_heaters << wh }
    model.getWaterHeaterStratifieds.each { |wh| water_heaters << wh }
    water_heaters.each do |wh|
      wh_fuel_type = wh.heaterFuelType
      case wh_fuel_type.downcase
      when /electric/
        fuel = elec
        in_swh_fuel_type << fuel
      when /gas/
        fuel = gas
        in_swh_fuel_type << fuel
      else
        fuel = wh_fuel_type
        in_swh_fuel_type << fuel
      end
    end
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
    runner.registerValue('in_swh_ua_w_per_k_weighted_avg_proposed', in_swh_ua_w_per_k_weighted_avg.round(5))
    runner.registerValue('in_swh_burner_efficiency_weighted_avg_proposed', in_swh_burner_efficiency_weighted_avg.round(3))

    # infiltration
    in_infiltration_ach_proposed = model.getBuilding.infiltrationDesignAirChangesPerHour
    runner.registerValue('in_infiltration_ach_proposed', in_infiltration_ach_proposed.round(3), '1/h')

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
    runner.registerValue('in_hvac_controls_design_cooling_supply_air_temperature_proposed', in_hvac_controls_design_cooling_supply_air_temperature.round(1))
    runner.registerValue('in_hvac_controls_design_heating_supply_air_temperature_proposed', in_hvac_controls_design_heating_supply_air_temperature.round(1))

    # zone HVAC properties
    zone_hvac_total_design_max_flow_m_3_s = 0.0
    zone_hvac_fan_weighted_efficiency = 0.0
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
          elsif zone_hvac.supplyFan.get.to_FanConstantVolume.is_initialized
            supply_fan = zone_hvac.supplyFan.get.to_FanConstantVolume.get
            fan_efficiency = supply_fan.fanTotalEfficiency
            max_design_flow = supply_fan.maximumFlowRate.get
          elsif zone_hvac.supplyFan.get.to_FanVariableVolume.is_initialized
            supply_fan = zone_hvac.supplyFan.get.to_FanVariableVolume.get
            fan_efficiency = supply_fan.fanTotalEfficiency
            max_design_flow = supply_fan.maximumFlowRate.get
          end
        else
          if zone_hvac.supplyAirFan.to_FanOnOff.is_initialized
            supply_fan = zone_hvac.supplyAirFan.to_FanOnOff.get
            fan_efficiency = supply_fan.fanTotalEfficiency
            max_design_flow = supply_fan.maximumFlowRate.get
          elsif zone_hvac.supplyAirFan.to_FanConstantVolume.is_initialized
            supply_fan = zone_hvac.supplyAirFan.to_FanConstantVolume.get
            fan_efficiency = supply_fan.fanTotalEfficiency
            max_design_flow = supply_fan.maximumFlowRate.get
          elsif zone_hvac.supplyAirFan.to_FanVariableVolume.is_initialized
            supply_fan = zone_hvac.supplyAirFan.to_FanVariableVolume.get
            fan_efficiency = supply_fan.fanTotalEfficiency
            max_design_flow = supply_fan.maximumFlowRate.get
          end
        end
        zone_hvac_total_design_max_flow_m_3_s += max_design_flow
        zone_hvac_fan_weighted_efficiency += fan_efficiency * max_design_flow
      end
    end
    in_hvac_zone_fan_max_design_flow_weighted_efficiency_proposed = zone_hvac_total_design_max_flow_m_3_s > 0.0 ? zone_hvac_fan_weighted_efficiency / zone_hvac_total_design_max_flow_m_3_s : 0.0
    runner.registerValue('in_hvac_zone_fan_max_design_flow_weighted_efficiency_proposed', in_hvac_zone_fan_max_design_flow_weighted_efficiency_proposed.round(5))

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
    runner.registerValue('in_hvac_controls_vav_terminal_min_cfm_per_sqft_proposed', in_hvac_controls_vav_terminal_min_cfm_per_sqft.round(5))

    # Fraction of building area with different air loop features
    building_zone_area_m2 = 0.0
    model.getThermalZones.sort.each do |zone|
      building_zone_area_m2 += zone.floorArea
    end
    building_area_with_heat_recovery_m2 = 0.0
    building_area_with_dcv_m2 = 0.0
    model.getAirLoopHVACs.sort.each do |air_loop_hvac|
      # fraction with heat recovery
      has_heat_recovery = std.air_loop_hvac_energy_recovery?(air_loop_hvac)

      # fraction with demand controlled ventilation
      has_dcv = false
      oa_system = air_loop_hvac.airLoopHVACOutdoorAirSystem
      if oa_system.is_initialized
        oa_system = oa_system.get
        controller_oa = oa_system.getControllerOutdoorAir
        controller_mv = controller_oa.controllerMechanicalVentilation
        has_dcv = controller_mv.demandControlledVentilation
      end

      # air loop area
      air_loop_area_m2 = 0.0
      air_loop_hvac.thermalZones.sort.each do |zone|
        air_loop_area_m2 += zone.floorArea
      end

      building_area_with_heat_recovery_m2 += air_loop_area_m2 if has_heat_recovery
      building_area_with_dcv_m2 += air_loop_area_m2 if has_dcv
    end
    building_area_fraction_with_heat_recovery_proposed = building_area_with_heat_recovery_m2 / building_zone_area_m2
    runner.registerValue('in_hvac_area_fraction_with_heat_recovery_proposed', building_area_fraction_with_heat_recovery_proposed.round(3))
    building_area_fraction_with_dcv_proposed = building_area_with_dcv_m2 / building_zone_area_m2
    runner.registerValue('in_hvac_area_fraction_with_dcv_proposed', building_area_fraction_with_dcv_proposed.round(3))

    # fully conditioned floor area: zones that are both heated and cooled.
    # Uses OpenstudioStandards::ThermalZone helpers which validate setpoint values
    # (heating max > 41F, cooling min < 91F), handle radiant equipment and staged
    # thermostats, and explicitly exclude heating-only zones. Zone multipliers are applied.
    in_floor_area_fully_conditioned_m_2_proposed = 0.0
    model.getThermalZones.each do |zone|
      heated = OpenstudioStandards::ThermalZone.thermal_zone_heated?(zone)
      cooled = OpenstudioStandards::ThermalZone.thermal_zone_cooled?(zone)
      next unless heated && cooled

      in_floor_area_fully_conditioned_m_2_proposed += zone.floorArea * zone.multiplier.to_f
    end
    runner.registerValue('in_floor_area_fully_conditioned_m_2_proposed', in_floor_area_fully_conditioned_m_2_proposed.round(2), 'm^2')

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
    in_hvac_chiller_capacity_weighted_design_efficiency_proposed = chiller_total_capacity_w > 0.0 ? chiller_capacity_weighted_design_cop / chiller_total_capacity_w : 0.0
    runner.registerValue('in_hvac_chiller_capacity_weighted_design_efficiency_proposed', in_hvac_chiller_capacity_weighted_design_efficiency_proposed.round(3))

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
    in_hvac_boiler_capacity_weighted_design_efficiency_proposed = boiler_total_capacity_w > 0.0 ? boiler_capacity_weighted_design_efficiency / boiler_total_capacity_w : 0.0
    runner.registerValue('in_hvac_boiler_capacity_weighted_design_efficiency_proposed', in_hvac_boiler_capacity_weighted_design_efficiency_proposed.round(3))

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
    in_hvac_furnace_eff_design_capacity_weighted_proposed = gas_coil_total_capacity_w > 0.0 ? gas_coil_capacity_weighted_efficiency / gas_coil_total_capacity_w : 0.0
    runner.registerValue('in_hvac_furnace_eff_design_capacity_weighted_proposed', in_hvac_furnace_eff_design_capacity_weighted_proposed.round(3))

    # ground floor insulation
    in_ground_rvalue_ip_sum = 0
    in_ground_ffactor_si_sum = 0
    in_ground_area_sum = 0
    model.getSpaces.sort.each do |space|
      space.surfaces.each do |surface|
        next if surface.surfaceType.to_s != 'Floor'

        planarsurface = surface.to_PlanarSurface.get
        construction = planarsurface.construction.get
        next unless construction.iddObjectType.to_s.include?('OS_Construction_FfactorGroundFloor')

        ffactorgroundconstruction = surface.construction.get.to_FFactorGroundFloorConstruction.get
        next if ffactorgroundconstruction.perimeterExposed < 0.0001

        calc1 = ffactorgroundconstruction.fFactor * ffactorgroundconstruction.perimeterExposed / ffactorgroundconstruction.area # W/m-K * m / m^2 = W/m^2-K
        calc2 = 1 / calc1 # m^2-K/W
        calc3 = calc2 * 5.678261 # ft^2-R-hr/Btu
        r_value_ip = calc3
        in_ground_rvalue_ip_sum += r_value_ip * ffactorgroundconstruction.area
        in_ground_ffactor_si_sum += ffactorgroundconstruction.fFactor * ffactorgroundconstruction.area
        in_ground_area_sum += ffactorgroundconstruction.area
      end
    end
    in_ground_rvalue_ip = in_ground_area_sum > 0.0 ? in_ground_rvalue_ip_sum / in_ground_area_sum : 0.0
    in_ground_ffactor_si = in_ground_area_sum > 0.0 ? in_ground_ffactor_si_sum / in_ground_area_sum : 0.0
    in_ground_ffactor_ip = in_ground_ffactor_si / 3.28084 / 1.8 * 3.41
    runner.registerValue('in_ground_rvalue_ip_proposed', in_ground_rvalue_ip.round(5))
    runner.registerValue('in_ground_ffactor_ip_proposed', in_ground_ffactor_ip.round(5))

    # Air system properties
    air_system_total_oa_mass_flow_kg_s = 0.0
    air_system_total_mass_flow_kg_s = 0.0
    air_system_weighted_fan_efficiency = 0.0
    in_hvac_controls_design_supply_air_flow_total_m_3_per_s = 0.0
    in_hvac_controls_design_outdoor_air_supply_flow_total = 0.0
    in_hvac_controls_controller_outdoor_air_minimum_outdoor_air_flow_rate_total = 0.0
    in_hvac_controls_controller_outdoor_air_maximum_outdoor_air_flow_rate_total = 0.0
    in_hvac_controls_total_floor_area_served_by_airloop = 0.0
    in_hvac_controls_total_floor_area_served_by_dcv = 0.0
    in_hvac_controls_num_airloop = 0
    in_hvac_controls_num_airloop_served_by_dcv = 0
    in_hvac_controls_herv_effectiveness_latent_heating = 0.0
    in_hvac_controls_herv_effectiveness_latent_cooling = 0.0
    in_hvac_controls_herv_effectiveness_sensible_heating = 0.0
    in_hvac_controls_herv_effectiveness_sensible_cooling = 0.0
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
      # TODO: lots of unused, also just use nil and not an abritrary float
      fan_efficiency = 999
      design_flow = 999
      oa_design_flow_rate = 0
      herv_effectiveness_latent_heating = 999
      herv_effectiveness_latent_cooling = 999
      herv_effectiveness_sensible_heating = 999
      herv_effectiveness_sensible_cooling = 999

      # get design air flow
      if air_loop_hvac.autosizedDesignSupplyAirFlowRate.is_initialized
        design_flow = air_loop_hvac.autosizedDesignSupplyAirFlowRate.get
      elsif air_loop_hvac.designSupplyAirFlowRate.is_initialized
        design_flow = air_loop_hvac.designSupplyAirFlowRate.get
      else
        runner.registerWarning("Cannot get design supply flow rate from air loop hvac '#{air_loop_hvac.name}'.")
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
            # herv_effectiveness
            herv_effectiveness_latent_heating = herv.latentEffectivenessat100HeatingAirFlow
            herv_effectiveness_latent_cooling = herv.latentEffectivenessat100CoolingAirFlow
            herv_effectiveness_sensible_heating = herv.sensibleEffectivenessat100HeatingAirFlow
            herv_effectiveness_sensible_cooling = herv.sensibleEffectivenessat100CoolingAirFlow
          end
        end
      end

      # get fan metrics
      # TODO: you're missing FanSystemModel aren't you?
      supply_fan = air_loop_hvac.supplyFan
      if supply_fan.is_initialized
        supply_fan = supply_fan.get
        if supply_fan.to_FanOnOff.is_initialized
          supply_fan = supply_fan.to_FanOnOff.get
          fan_efficiency = supply_fan.fanTotalEfficiency
        elsif supply_fan.to_FanConstantVolume.is_initialized
          supply_fan = supply_fan.to_FanConstantVolume.get
          fan_efficiency = supply_fan.fanTotalEfficiency
        elsif supply_fan.to_FanVariableVolume.is_initialized
          supply_fan = supply_fan.to_FanVariableVolume.get
          fan_efficiency = supply_fan.fanTotalEfficiency
        else
          runner.registerWarning("Supply Fan type not recognized for air loop hvac '#{air_loop_hvac.name}'.")
        end
      else
        runner.registerWarning("Supply Fan not available for air loop hvac '#{air_loop_hvac.name}'.") unless std.air_loop_hvac_unitary_system?(air_loop_hvac)
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

      # add to weighted
      air_system_total_mass_flow_kg_s += air_loop_mass_flow_rate_kg_s
      air_system_total_oa_mass_flow_kg_s += air_loop_oa_mass_flow_rate_kg_s
      air_system_weighted_fan_efficiency += fan_efficiency * design_flow
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

    # calculate economizer variables
    if economizer_statistics.empty?
      runner.registerInfo('No economizer present in the model.')
      runner.registerValue('in_hvac_controls_economizer_control_type', 'NoEconomizer')
    else
      economizer_type_hash = economizer_statistics.group_by { |e| e[:economizer_type] }
      economizer_type_areas = economizer_type_hash.map { |x, y| [x, y.inject(0) { |sum, i| sum + i[:air_loop_mass_flow_rate_kg_s] }] }
      largest_economizer_type = economizer_type_areas.max_by { |_k, v| v }
      runner.registerValue('in_hvac_controls_economizer_control_type', largest_economizer_type[0])
    end
    temperature_limited_hash = economizer_statistics.reject { |e| e[:economizer_high_limit_temperature_c].nil? }
    enthalpy_limited_hash = economizer_statistics.reject { |e| e[:economizer_high_limit_enthalpy_j_per_kg].nil? }
    if temperature_limited_hash.empty?
      in_hvac_controls_economizer_high_limit_shutoff_t_c = -999
    else
      in_hvac_controls_economizer_high_limit_shutoff_t_c = 0.0
      weighted_economizer_high_limit_temperature_c_flow_rate_kg_s = 0.0
      temperature_limited_hash.each do |e|
        weighted_economizer_high_limit_temperature_c_flow_rate_kg_s += e[:air_loop_mass_flow_rate_kg_s]
        in_hvac_controls_economizer_high_limit_shutoff_t_c += e[:economizer_high_limit_temperature_c] * e[:air_loop_mass_flow_rate_kg_s]
      end
      in_hvac_controls_economizer_high_limit_shutoff_t_c /= weighted_economizer_high_limit_temperature_c_flow_rate_kg_s
    end
    if enthalpy_limited_hash.empty?
      in_hvac_controls_economizer_high_limit_shutoff_h_j_per_kg = -999
    else
      in_hvac_controls_economizer_high_limit_shutoff_h_j_per_kg = 0.0
      weighted_economizer_high_limit_enthalpy_j_per_flow_rate_kg_s = 0.0
      enthalpy_limited_hash.each do |e|
        weighted_economizer_high_limit_enthalpy_j_per_flow_rate_kg_s += e[:air_loop_mass_flow_rate_kg_s]
        in_hvac_controls_economizer_high_limit_shutoff_h_j_per_kg += e[:economizer_high_limit_enthalpy_j_per_kg] * e[:air_loop_mass_flow_rate_kg_s]
      end
      in_hvac_controls_economizer_high_limit_shutoff_h_j_per_kg /= weighted_economizer_high_limit_enthalpy_j_per_flow_rate_kg_s
    end
    in_hvac_central_fan_max_design_flow_weighted_efficiency = in_hvac_controls_design_supply_air_flow_total_m_3_per_s > 0.0 ? air_system_weighted_fan_efficiency / in_hvac_controls_design_supply_air_flow_total_m_3_per_s : 0.0
    in_hvac_controls_herv_effectiveness_latent_heating = in_hvac_controls_design_supply_air_flow_total_m_3_per_s > 0.0 ? in_hvac_controls_herv_effectiveness_latent_heating / in_hvac_controls_design_supply_air_flow_total_m_3_per_s : 0.0
    in_hvac_controls_herv_effectiveness_latent_cooling = in_hvac_controls_design_supply_air_flow_total_m_3_per_s > 0.0 ? in_hvac_controls_herv_effectiveness_latent_cooling / in_hvac_controls_design_supply_air_flow_total_m_3_per_s : 0.0
    in_hvac_controls_herv_effectiveness_sensible_heating = in_hvac_controls_design_supply_air_flow_total_m_3_per_s > 0.0 ? in_hvac_controls_herv_effectiveness_sensible_heating / in_hvac_controls_design_supply_air_flow_total_m_3_per_s : 0.0
    in_hvac_controls_herv_effectiveness_sensible_cooling = in_hvac_controls_design_supply_air_flow_total_m_3_per_s > 0.0 ? in_hvac_controls_herv_effectiveness_sensible_cooling / in_hvac_controls_design_supply_air_flow_total_m_3_per_s : 0.0
    runner.registerValue('in_hvac_central_fan_max_design_flow_weighted_efficiency_proposed', in_hvac_central_fan_max_design_flow_weighted_efficiency.round(3))
    runner.registerValue('in_hvac_controls_design_supply_air_flow_total_m_3_per_s_proposed', in_hvac_controls_design_supply_air_flow_total_m_3_per_s.round(5))
    runner.registerValue('in_hvac_controls_design_outdoor_air_supply_flow_total_proposed', in_hvac_controls_design_outdoor_air_supply_flow_total.round(5))
    runner.registerValue('in_hvac_controls_herv_effectiveness_latent_heating_proposed', in_hvac_controls_herv_effectiveness_latent_heating.round(3))
    runner.registerValue('in_hvac_controls_herv_effectiveness_latent_cooling_proposed', in_hvac_controls_herv_effectiveness_latent_cooling.round(3))
    runner.registerValue('in_hvac_controls_herv_effectiveness_sensible_heating_proposed', in_hvac_controls_herv_effectiveness_sensible_heating.round(3))
    runner.registerValue('in_hvac_controls_herv_effectiveness_sensible_cooling_proposed', in_hvac_controls_herv_effectiveness_sensible_cooling.round(3))
    runner.registerValue('in_hvac_controls_economizer_high_limit_shutoff_t_c_proposed', in_hvac_controls_economizer_high_limit_shutoff_t_c.round(3))
    runner.registerValue('in_hvac_controls_economizer_high_limit_shutoff_h_j_per_kg_proposed', in_hvac_controls_economizer_high_limit_shutoff_h_j_per_kg.round(3))

    in_hvac_controls_designspecification_outdoorair_total_flow_rate = 0.0
    model.getThermalZones.each do |zone|
      in_hvac_controls_designspecification_outdoorair_total_flow_rate += OpenstudioStandards::ThermalZone.thermal_zone_get_outdoor_airflow_rate(zone) * zone.multiplier.to_f
    end
    runner.registerValue('in_hvac_controls_designspecification_outdoorair_total_flow_rate_proposed', in_hvac_controls_designspecification_outdoorair_total_flow_rate.round(5), 'm^3/s')
    runner.registerValue('in_hvac_controls_total_floor_area_served_by_airloop_proposed', in_hvac_controls_total_floor_area_served_by_airloop.round(5), 'm^2')
    runner.registerValue('in_hvac_controls_total_floor_area_served_by_dcv_proposed', in_hvac_controls_total_floor_area_served_by_dcv.round(5), 'm^2')
    runner.registerValue('in_hvac_controls_num_airloop_proposed', in_hvac_controls_num_airloop)
    runner.registerValue('in_hvac_controls_num_airloop_served_by_dcv_proposed', in_hvac_controls_num_airloop_served_by_dcv)
    runner.registerValue('in_hvac_controls_controller_outdoor_air_minimum_outdoor_air_flow_rate_total_proposed', in_hvac_controls_controller_outdoor_air_minimum_outdoor_air_flow_rate_total.round(5), 'm^3/s')
    runner.registerValue('in_hvac_controls_controller_outdoor_air_maximum_outdoor_air_flow_rate_total_proposed', in_hvac_controls_controller_outdoor_air_maximum_outdoor_air_flow_rate_total.round(5), 'm^3/s')

    ############################################################################################
    # add output variables
    ############################################################################################

    # define a list of output variables and reporting frequency
    list_ovs = {
      'Air System Outdoor Air Flow Fraction' => 'runperiod',
      'Air System Mixed Air Mass Flow Rate' => 'runperiod',
      'Boiler Heating Energy' => 'runperiod',
      'Boiler Electricity Energy' => 'runperiod',
      'Boiler NaturalGas Energy' => 'runperiod',
      'Chiller Evaporator Cooling Energy' => 'runperiod',
      'Chiller COP' => 'runperiod',
      'Cooling Coil Total Cooling Energy' => 'runperiod',
      'Cooling Coil Electricity Energy' => 'runperiod',
      'Cooling Coil NaturalGas Energy' => 'runperiod',
      'Heating Coil Heating Energy' => 'runperiod',
      'Heating Coil Electricity Energy' => 'runperiod',
      'Heating Coil NaturalGas Energy' => 'runperiod',
      'Heating Coil Defrost Electricity Energy' => 'runperiod',
      'Heating Coil Defrost NaturalGas Energy' => 'runperiod',
      'Water Use Connections Hot Water Volume' => 'runperiod',
      'Site Outdoor Air Drybulb Temperature' => 'Hourly',
      'Site Outdoor Air Relative Humidity' => 'Hourly',
    }

    list_ovs.each do |variable_name, reporting_frequency|
      key_value = '*'
      outputVariable = OpenStudio::Model::OutputVariable.new(variable_name, model)
      outputVariable.setReportingFrequency(reporting_frequency)
      outputVariable.setKeyValue(key_value)
      runner.registerInfo("Adding output variable for #{outputVariable.variableName} reporting #{reporting_frequency}.")
      runner.registerInfo("Key value for variable is #{outputVariable.keyValue}.")
    end

    ############################################################################################
    # add meters
    ############################################################################################

    # define a list of enduse meters
    list_enduses = [
      'Heating:NaturalGas',
      'Heating:Electricity',
      'Cooling:NaturalGas',
      'Cooling:Electricity',
      'Fans:Electricity',
      'Pumps:Electricity',
      'InteriorLights:Electricity',
      'WaterSystems:Electricity',
      'WaterSystems:NaturalGas',
      'Booster:WaterSystems:Electricity'
    ]
    reporting_frequency = 'monthly'

    # flag to add meter
    add_flag = true

    # OpenStudio doesn't seemt to like two meters of the same name, even if they have different reporting frequencies.
    count_new = 0
    count_mod = 0
    list_enduses.each do |meter_name|
      runner.registerInfo("Working on creating a meter for #{meter_name}")
      meters.each do |meter|
        next unless meter.name == meter_name

        runner.registerWarning("A meter named #{meter_name} already exists. One will not be added to the model.")
        if meter.reportingFrequency != reporting_frequency
          meter.setReportingFrequency(reporting_frequency)
          runner.registerInfo("Changing reporting frequency of existing meter to #{reporting_frequency}.")
          count_mod += 1
        end
        add_flag = false
      end

      next unless add_flag

      meter = OpenStudio::Model::OutputMeter.new(model)
      meter.setName(meter_name)
      meter.setReportingFrequency(reporting_frequency)
      runner.registerInfo("Adding meter for #{meter.name} reporting #{reporting_frequency}")
      count_new += 1
    end

    ############################################################################################
    # final condition
    ############################################################################################
    # reporting final condition of model
    outputVariables = model.getOutputVariables
    runner.registerFinalCondition("The model finished with #{count_new} meters (new), #{count_mod} meters (modified), and #{outputVariables.size} new output variable objects.")

    if !ENV['SAVE_INTERMEDIATE'].nil?
      model.save('18.2.after_reporting_179_d_support.osm')
    end

    true
  end
end

# this allows the measure to be use by the application
Reporting179DSupport.new.registerWithApplication
