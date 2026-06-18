# ComStock(TM), Copyright (c) 2020 Alliance for Sustainable Energy, LLC. All rights reserved.
# See top level LICENSE.txt file for license terms.

# *******************************************************************************
# OpenStudio(R), Copyright (c) Alliance for Sustainable Energy, LLC.
# See also https://openstudio.net/license
# *******************************************************************************

require 'openstudio'
require 'openstudio-standards'
require 'json'

# start the measure
class HardsizeModel < OpenStudio::Measure::ModelMeasure
  # human readable name
  def name
    # Measure name should be the title case of the class name.
    return 'Hardsize Model'
  end

  # human readable description
  def description
    return 'Sets the HVAC capacities and flow rates in the model.'
  end

  # human readable description of modeling approach
  def modeler_description
    return 'Runs a sizing run and applies EnerygyPlus autosized values into the model.'
  end

  # define the arguments that the user will input
  def arguments(_model)
    args = OpenStudio::Measure::OSArgumentVector.new

    # Daylight Savings Time
    apply_hardsize = OpenStudio::Measure::OSArgument.makeBoolArgument('apply_hardsize', true)
    apply_hardsize.setDisplayName('Hardsize model')
    apply_hardsize.setDescription('Set to true to hardsize model HVAC, set to false to leave model autosized')
    apply_hardsize.setDefaultValue(true)
    args << apply_hardsize

    return args
  end

  # define what happens when the measure is run
  def run(model, runner, user_arguments)
    super(model, runner, user_arguments)

    if !ENV['SAVE_INTERMEDIATE'].nil?
      model.save('17.1.before_hardsize_model.osm')
    end

    # use the built-in error checking
    if !runner.validateUserArguments(arguments(model), user_arguments)
      return false
    end

    # patch work: get coil spec summary for multi-speed coils from the original model
    coil_summary_original = {}
    model.getCoilCoolingDXMultiSpeeds.sort.each do |msc|
      coil_summary_original[msc.name.get.to_s] = {}
      msc.stages.sort.each_with_index do |msc_stage, i|
        capacity_w = msc_stage.grossRatedTotalCoolingCapacity.get
        airflow_m_3_per_s = msc_stage.ratedAirFlowRate.get
        cfm_per_ton = OpenStudio.convert(OpenStudio.convert(airflow_m_3_per_s / capacity_w, 'm^3/s', 'cfm').get, 'ton', 'W').get
        coil_summary_original[msc.name.get.to_s][i + 1] = {}
        coil_summary_original[msc.name.get.to_s][i + 1]['capacity_w'] = capacity_w
        coil_summary_original[msc.name.get.to_s][i + 1]['airflow_m_3_per_s'] = airflow_m_3_per_s
        coil_summary_original[msc.name.get.to_s][i + 1]['cfm_per_ton'] = cfm_per_ton
        # puts("### stage #{i}: capacity_w = #{capacity_w.round(0)} | airflow_m_3_per_s = #{airflow_m_3_per_s.round(3)} | cfm_per_ton = #{cfm_per_ton.round(3)}")
      end
    end
    model.getCoilHeatingDXMultiSpeeds.sort.each do |msc|
      coil_summary_original[msc.name.get.to_s] = {}
      msc.stages.sort.each_with_index do |msc_stage, i|
        capacity_w = msc_stage.grossRatedHeatingCapacity.get
        airflow_m_3_per_s = msc_stage.ratedAirFlowRate.get
        cfm_per_ton = OpenStudio.convert(OpenStudio.convert(airflow_m_3_per_s / capacity_w, 'm^3/s', 'cfm').get, 'ton', 'W').get
        coil_summary_original[msc.name.get.to_s][i + 1] = {}
        coil_summary_original[msc.name.get.to_s][i + 1]['capacity_w'] = capacity_w
        coil_summary_original[msc.name.get.to_s][i + 1]['airflow_m_3_per_s'] = airflow_m_3_per_s
        coil_summary_original[msc.name.get.to_s][i + 1]['cfm_per_ton'] = cfm_per_ton
        # puts("### stage #{i}: capacity_w = #{capacity_w.round(0)} | airflow_m_3_per_s = #{airflow_m_3_per_s.round(3)} | cfm_per_ton = #{cfm_per_ton.round(3)}")
      end
    end
    # File.write("#{Dir.pwd}/coil_summary_hardsize_before.json", JSON.pretty_generate(coil_summary_original))

    # Assign the user inputs to variables
    apply_hardsize = runner.getBoolArgumentValue('apply_hardsize', user_arguments)

    unless apply_hardsize
      runner.registerAsNotApplicable("Leaving model autosized per argument: apply_hardsize = #{apply_hardsize}")
      return true
    end

    reset_log
    standard = Standard.build('ComStock DOE Ref Pre-1980') # Actual standard doesn't matter

    # Collect equipment capacities and flow rates that are hard-sized by OpenStudio-Standards.
    # These fields need to keep the hard-sized values and not be replaced with the
    # autosized values determined by EnergyPlus.
    # The eventual goal is to have OpenStudio-Standards rely entirely on EnergyPlus autosizing,
    # such that all of this code can be removed.

    # TODO: remove this after feature https://github.com/NREL/openstudio-standards/issues/1391 is implemented
    # Get the terminal minimum damper positions and preserve them after the hard-sizing
    # because damper position is hard-sized by openstudio-standards, not autosized
    # Min OA flow rate at these damper positions is also hard-sized.
    vav_damper_posits = {}
    vav_max_rht_fracs = {}
    model.getAirTerminalSingleDuctVAVReheats.each do |term|
      if term.zoneMinimumAirFlowInputMethod == 'Constant' && !term.isConstantMinimumAirFlowFractionAutosized
        vav_damper_posits[term] = term.constantMinimumAirFlowFraction.get
      end
      unless term.isMaximumFlowFractionDuringReheatAutosized
        vav_max_rht_fracs[term] = term.maximumFlowFractionDuringReheat.get
      end
    end

    # SizingSystem::applySizingValues will override any hardsized values
    # because it grabs them from another object's field that could be autosized
    sizing_system_hardsized_values = {}

    model.getSizingSystems.each do |sizing_system|
      sizing_system_hardsized = {}
      unless sizing_system.isDesignOutdoorAirFlowRateAutosized
        sizing_system_hardsized[:design_outdoor_air_flow_rate] = sizing_system.designOutdoorAirFlowRate.get
      end
      unless sizing_system.isCoolingDesignCapacityAutosized
        sizing_system_hardsized[:cooling_design_capacity] = sizing_system.coolingDesignCapacity.get
      end
      unless sizing_system.isHeatingDesignCapacityAutosized
        sizing_system_hardsized[:heating_design_capacity] = sizing_system.heatingDesignCapacity.get
      end
      unless sizing_system.isCentralHeatingMaximumSystemAirFlowRatioAutosized
        sizing_system_hardsized[:central_heating_maximum_system_air_flow_ratio] = sizing_system.centralHeatingMaximumSystemAirFlowRatio.get
      end
      unless sizing_system.isOccupantDiversityAutosized
        sizing_system_hardsized[:occupant_diversity] = sizing_system.occupantDiversity.get
      end

      sizing_system_hardsized_values[sizing_system] = sizing_system_hardsized
    end

    # Run a sizing run to determine equipment capacities and flow rates
    if standard.model_run_sizing_run(model, "#{Dir.pwd}/hardsize_model_SR") == false
      runner.registerError('Sizing run for Hardsize model failed, cannot hard-size model.')
      puts('Sizing run for Hardsize model failed, cannot hard-size model.')
      return false
    end

    # Apply the capacities and flow rates from the sizing run to the model
    runner.registerInfo('Hard-sizing HVAC equipment to capacities and flows used to set efficiencies and controls.')
    model.applySizingValues

    # patch work: apply coil spec summary for multi-speed coils to the model after applySizingValues
    # this only affects models using multispeed coils (e.g., HPRTU measure)
    # sizing and applying sizing values multiple times cause
    # ratedEvaporativeCondenserPumpPowerConsumption (for cooling coil only), rated capacity, and rated airflow to get weird values
    # replace multispeed cooling coil sizing values (rated capacity and rated airflow) to original values
    model.getCoilCoolingDXMultiSpeeds.sort.each do |msc|
      msc.stages.sort.each_with_index do |msc_stage, i|
        coil_name = msc.name.get.to_s # Convert to string to match the hash keys
        coil_data = coil_summary_original[coil_name]

        if coil_data.nil?
          runner.registerError("No original spec found for #{msc.name}")
          return false
        end

        state_num = i + 1
        stage_data = coil_data[state_num]

        if stage_data.nil?
          runner.registerError("No stage data found for stage #{i + 1} in #{msc.name}")
          return false
        end

        msc_stage.setGrossRatedTotalCoolingCapacity(stage_data['capacity_w'])
        msc_stage.setRatedAirFlowRate(stage_data['airflow_m_3_per_s'])
        msc_stage.setRatedEvaporativeCondenserPumpPowerConsumption(0)
      end
    end
    # replace multispeed heating coil sizing values (rated capacity and rated airflow) to original values
    model.getCoilHeatingDXMultiSpeeds.sort.each do |msc|
      msc.stages.sort.each_with_index do |msc_stage, i|
        coil_name = msc.name.get.to_s # Convert to string to match the hash keys
        coil_data = coil_summary_original[coil_name]

        if coil_data.nil?
          runner.registerError("No original spec found for #{msc.name}")
          return false
        end

        state_num = i + 1
        stage_data = coil_data[state_num]

        if stage_data.nil?
          runner.registerError("No stage data found for stage #{i + 1} in #{msc.name}")
          return false
        end

        msc_stage.setGrossRatedHeatingCapacity(stage_data['capacity_w'])
        msc_stage.setRatedAirFlowRate(stage_data['airflow_m_3_per_s'])
      end
    end

    # Reset some fields to the previously-collected hard-sized values

    # TODO: remove once this functionality is added to the OpenStudio C++ for hard sizing UnitarySystems
    model.getAirLoopHVACUnitarySystems.each do |unitary|
      unitary.setSupplyAirFlowRateMethodDuringCoolingOperation('SupplyAirFlowRate')
      unitary.setSupplyAirFlowRateMethodDuringHeatingOperation('SupplyAirFlowRate')
    end

    # TODO: remove once this functionality is added to the OpenStudio C++ for hard sizing Sizing:System
    # autosizedDesignOutdoorAirFlowRate is getting the COntroller OA MAXimum Outdoor Air Flow Rate
    # Please read: https://github.com/NREL/openstudio-bem-to-surrogate-gem/issues/293#issuecomment-3022784437
    # https://github.com/NREL/OpenStudio/issues/5442
    model.getSizingSystems.each do |sizing_system|
      # next if sizing_system.isDesignOutdoorAirFlowRateAutosized

      sizing_system_hardsized = sizing_system_hardsized_values[sizing_system]

      raise if sizing_system_hardsized.nil?

      air_loop_hvac = sizing_system.airLoopHVAC

      if !sizing_system_hardsized[:cooling_design_capacity].nil?
        runner.registerInfo("For #{air_loop_hvac.nameString}, restoring previously hardsized cooling design capacity of #{sizing_system_hardsized[:cooling_design_capacity]} W")
        sizing_system.setCoolingDesignCapacity(sizing_system_hardsized[:cooling_design_capacity])
      end

      if !sizing_system_hardsized[:heating_design_capacity].nil?
        runner.registerInfo("For #{air_loop_hvac.nameString}, restoring previously hardsized heating design capacity of #{sizing_system_hardsized[:heating_design_capacity]} W")
        sizing_system.setHeatingDesignCapacity(sizing_system_hardsized[:heating_design_capacity])
      end

      if !sizing_system_hardsized[:central_heating_maximum_system_air_flow_ratio].nil?
        runner.registerInfo("For #{air_loop_hvac.nameString}, restoring previously hardsized central heating maximum system air flow ratio of #{sizing_system_hardsized[:central_heating_maximum_system_air_flow_ratio]}")
        sizing_system.setCentralHeatingMaximumSystemAirFlowRatio(sizing_system_hardsized[:central_heating_maximum_system_air_flow_ratio])
      end

      if !sizing_system_hardsized[:occupant_diversity].nil?
        runner.registerInfo("For #{air_loop_hvac.nameString}, restoring previously hardsized occupant diversity of #{sizing_system_hardsized[:occupant_diversity]}")
        sizing_system.setOccupantDiversity(sizing_system_hardsized[:occupant_diversity])
      end

      system_oa_method = sizing_system.systemOutdoorAirMethod

      if sizing_system_hardsized[:design_outdoor_air_flow_rate].nil?
        air_loop_hvac_oasys = air_loop_hvac.airLoopHVACOutdoorAirSystem.get
        controller_oa = air_loop_hvac_oasys.getControllerOutdoorAir
        controller_mv = controller_oa.controllerMechanicalVentilation
        oa_design_flow_rate = nil
        if controller_mv.demandControlledVentilation
          # minimumOutdoorAirFlowRate is zero...
          # If this is a VAV System, the Sizing:System already has a hardsized
          # Design Outdoor Air Flow Rate, so we hit the ladder above anyways
          if !system_oa_method.casecmp('zonesum').zero?
            runner.registerError("For #{air_loop_hvac.nameString}, expected System Outdoor Air Method to be 'ZoneSum' not '#{system_oa_method}'")
            return false
          end
          oa_design_flow_rate = 0.0
          air_loop_hvac.thermalZones.each do |zone|
            oa_design_flow_rate += OpenstudioStandards::ThermalZone.thermal_zone_get_outdoor_airflow_rate(zone) * zone.multiplier.to_f
          end
          runner.registerInfo("For #{air_loop_hvac.nameString}, DCV is enabled, calculated minimum outdoor air flow rate of #{oa_design_flow_rate} m^3/s from DSOAs of thermal zones")
        elsif controller_oa.minimumOutdoorAirFlowRate.is_initialized
          # This should really just be the case if DCV is enabled anyways
          oa_design_flow_rate = controller_oa.minimumOutdoorAirFlowRate.get
          if oa_design_flow_rate < 0.0001
            # Fall back anyways
            runner.registerWarning("For #{air_loop_hvac.nameString}, expected minimum outdoor air flow rate to be greater than 0.0001 m^3/s, but got #{oa_design_flow_rate} m^3/s")
            oa_design_flow_rate = 0.0
            air_loop_hvac.thermalZones.each do |zone|
              oa_design_flow_rate += OpenstudioStandards::ThermalZone.thermal_zone_get_outdoor_airflow_rate(zone) * zone.multiplier.to_f
            end
            runner.registerInfo("For #{air_loop_hvac.nameString}, DCV is enabled, calculated minimum outdoor air flow rate of #{oa_design_flow_rate} m^3/s from DSOAs of thermal zones")
          else
            runner.registerInfo("For #{air_loop_hvac.nameString}, using minimum outdoor air flow rate of #{oa_design_flow_rate} m^3/s from Controller Outdoor Air")
          end

        elsif controller_oa.autosizedMimumOutdoorAirFlowRate.is_initialized
          # Get the E+ calculated value
          oa_design_flow_rate = controller_oa.autosizedMimumOutdoorAirFlowRate.get
          runner.registerInfo("For #{air_loop_hvac.nameString}, using Autosized minimum outdoor air flow rate of #{oa_design_flow_rate} m^3/s from Controller Outdoor Air")
        else
          runner.registerError("No minimum outdoor air flow rate found for sizing system in #{air_loop_hvac.nameString}")
          return false
        end
      else
        oa_design_flow_rate = sizing_system_hardsized[:design_outdoor_air_flow_rate]
        runner.registerInfo("For #{air_loop_hvac.nameString}, restoring previously hardsized design outdoor air flow rate of #{oa_design_flow_rate} m^3/s")
      end
      if oa_design_flow_rate.nil?
        runner.registerError("No outdoor air design flow rate found for sizing system in #{air_loop_hvac.nameString}")
        return false
      end

      sizing_system.setDesignOutdoorAirFlowRate(oa_design_flow_rate)

      sizing_system.setSystemOutdoorAirMethod('ZoneSum')
    end

    # TODO: remove this after feature https://github.com/NREL/openstudio-standards/issues/1391 is implemented
    # Re-apply hardsized VAV damper positions
    model.getAirTerminalSingleDuctVAVReheats.each do |term|
      if vav_damper_posits.key?(term)
        term.setConstantMinimumAirFlowFraction(vav_damper_posits[term])
      end
      if vav_max_rht_fracs.key?(term)
        term.setMaximumFlowFractionDuringReheat(vav_max_rht_fracs[term])
      end

      # TODO: remove once this functionality is added to the OpenStudio C++ for hard sizing
      if term.damperHeatingAction == 'Normal'
        term.autosizeMaximumFlowFractionDuringReheat
        term.autosizeMaximumFlowPerZoneFloorAreaDuringReheat
      end
    end

    if !ENV['SAVE_INTERMEDIATE'].nil?
      model.save('17.2.after_hardsize_model.osm')
    end

    return true
  end
end

# register the measure to be used by the application
HardsizeModel.new.registerWithApplication
