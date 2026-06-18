# *******************************************************************************
# OpenStudio(R), Copyright (c) Alliance for Sustainable Energy, LLC.
# See also https://openstudio.net/license
# *******************************************************************************

# see the URL below for information on how to write OpenStudio measures
# http://nrel.github.io/OpenStudio-user-documentation/reference/measure_writing_guide/

# start the measure
class SetHeatingCoolingCopEfficiency < OpenStudio::Measure::ModelMeasure
  require 'openstudio-standards'

  # human readable name
  def name
    return 'Set Heating Efficiency'
  end

  # human readable description
  def description
    return 'Set Heating Efficiency'
  end

  # human readable description of modeling approach
  def modeler_description
    return 'Change the HVAC components to make their properties match the selected template using the OpenStudio Standards methods.  Will replace the existing properties where present.'
  end

  # define the arguments that the user will input
  def arguments(_model)
    args = OpenStudio::Measure::OSArgumentVector.new

    cooling_cop = OpenStudio::Measure::OSArgument.makeDoubleArgument('cooling_cop', true)
    cooling_cop.setDisplayName('Cooling DX Coil COP')
    cooling_cop.setDefaultValue(3.7)
    args << cooling_cop

    heating_cop = OpenStudio::Measure::OSArgument.makeDoubleArgument('heating_cop', true)
    heating_cop.setDisplayName('Heating DX Coil COP')
    heating_cop.setDefaultValue(3.7)
    args << heating_cop

    efficiency = OpenStudio::Measure::OSArgument.makeDoubleArgument('efficiency', true)
    efficiency.setDisplayName('Heating Gas Coil Efficiency')
    efficiency.setDefaultValue('0.8')
    args << efficiency

    boiler_efficiency = OpenStudio::Measure::OSArgument.makeDoubleArgument('boiler_efficiency', true)
    boiler_efficiency.setDisplayName('Boiler Thermal Efficiency')
    boiler_efficiency.setDefaultValue('0.8')
    args << boiler_efficiency

    chiller_cop = OpenStudio::Measure::OSArgument.makeDoubleArgument('chiller_cop', true)
    chiller_cop.setDisplayName('Chiller Reference COP')
    chiller_cop.setDefaultValue('5.3')
    args << chiller_cop

    return args
  end

  # define what happens when the measure is run
  def run(model, runner, user_arguments)
    super(model, runner, user_arguments)

    if !ENV['SAVE_INTERMEDIATE'].nil?
      model.save('10.1.before_set_heating_cooling_cop_efficiency.osm')
    end

    # use the built-in error checking
    if !runner.validateUserArguments(arguments(model), user_arguments)
      return false
    end

    cooling_cop = runner.getDoubleArgumentValue('cooling_cop', user_arguments)
    heating_cop = runner.getDoubleArgumentValue('heating_cop', user_arguments)
    efficiency = runner.getDoubleArgumentValue('efficiency', user_arguments)
    boiler_efficiency = runner.getDoubleArgumentValue('boiler_efficiency', user_arguments)
    chiller_cop = runner.getDoubleArgumentValue('chiller_cop', user_arguments)

    system_type_value = runner.getPastStepValuesForName('system_type')
    if system_type_value.empty?
    #  runner.registerError('system_type is empty!')
    else
      system_type = system_type_value.first[1]
      runner.registerInfo("system_type is #{system_type}")
    end

    ########################################################
    # check min/max values of inputs
    ########################################################
    if (cooling_cop < 1) || (cooling_cop > 8)
      runner.registerError("Value of input arg is not within 2 and 4.2: cooling_cop = #{cooling_cop}")
      return false
    end
    if (heating_cop < 1) || (heating_cop > 8)
      runner.registerError("Value of input arg is not within 2.8 and 3.9: heating_cop = #{heating_cop}")
      return false
    end
    if (efficiency < 0.5) || (efficiency > 1)
      runner.registerError("Value of input arg is not within 0.76 and 1: efficiency = #{efficiency}")
      return false
    end
    if (boiler_efficiency < 0.5) || (boiler_efficiency > 1)
      runner.registerError("Value of input arg is not within 0.76 and 1: boiler_efficiency = #{boiler_efficiency}")
      return false
    end
    if (chiller_cop < 1) || (chiller_cop > 8)
      runner.registerError("Value of input arg is not within 0.76 and 1: chiller_cop = #{chiller_cop}")
      return false
    end

    # fail if any other systems in the model
    if !model.getCoilCoolingDXMultiSpeeds.empty?
      runner.registerError('Model contains CoilCoolingDXMultiSpeeds objects. Operation failed.')
      return false
    end
    if !model.getCoilCoolingDXVariableSpeeds.empty?
      runner.registerError('Model contains CoilCoolingDXVariableSpeeds objects. Operation failed.')
      return false
    end
    if !model.getCoilHeatingDXMultiSpeeds.empty?
      runner.registerError('Model contains CoilHeatingDXMultiSpeeds objects. Operation failed.')
      return false
    end
    if !model.getCoilHeatingDXVariableSpeeds.empty?
      runner.registerError('Model contains CoilHeatingDXVariableSpeeds objects. Operation failed.')
      return false
    end

    # This all comes from https://github.com/NREL/openstudio-standards/blob/179D/lib/openstudio-standards/standards/Standards.Model.rb#L2265
    # which is def model_apply_hvac_efficiency_standard(model, climate_zone, apply_controls: true, sql_db_vars_map: nil, necb_ref_hp: false)
    #
    # #-------- Cooling systems ------------
    #
    model.getCoilCoolingDXSingleSpeeds.sort.each do |obj|
      obj.setRatedCOP(cooling_cop.to_f)
      runner.registerInfo("Modified CoilCoolingDXSingleSpeeds: #{obj.name} to COP: #{cooling_cop.to_f}.")
    end

    model.getCoilCoolingDXTwoSpeeds.sort.each do |obj|
      obj.setRatedLowSpeedCOP(cooling_cop.to_f)
      runner.registerInfo("Modified getCoilCoolingDXTwoSpeeds: #{obj.name} to LowSpeedCOP: #{cooling_cop.to_f}.")
      obj.setRatedHighSpeedCOP(cooling_cop.to_f)
      runner.registerInfo("Modified getCoilCoolingDXTwoSpeeds: #{obj.name} to HighSpeedCOP: #{cooling_cop.to_f}.")
    end

    # #-------- Heating systems ------------
    # # Unitary HPs
    # # set DX HP coils before DX clg coils because when DX HP coils need to first
    # # pull the capacities of their paried DX clg coils, and this does not work
    # # correctly if the DX clg coil efficiencies have been set because they are renamed.
    # The Standards method sets curves as well, which we are not doing here, just updating the COP
    # model.getCoilHeatingDXSingleSpeeds.sort.each { |obj| sql_db_vars_map = coil_heating_dx_single_speed_apply_efficiency_and_curves(obj, sql_db_vars_map, necb_ref_hp) }
    # https://github.com/NREL/openstudio-standards/blob/179D/lib/openstudio-standards/standards/Standards.Model.rb#L2301
    # Standards
    # https://github.com/NREL/openstudio-standards/blob/179D/lib/openstudio-standards/standards/Standards.CoilHeatingDXSingleSpeed.rb#L186
    # OR PRM
    # https://github.com/NREL/openstudio-standards/blob/179D/lib/openstudio-standards/standards/ashrae_90_1_prm/ashrae_90_1_prm.CoilHeatingDXSingleSpeed.rb#L177
    model.getCoilHeatingDXSingleSpeeds.sort.each do |obj|
      obj.setRatedCOP(heating_cop.to_f)
      runner.registerInfo("Modified CoilHeatingDXSingleSpeeds: #{obj.name} to COP: #{heating_cop.to_f}.")
    end

    # Gas Coils
    # The Standards method sets curves as well, which we are not doing here, just updating the efficiency
    # model.getCoilHeatingGass.sort.each { |obj| coil_heating_gas_apply_efficiency_and_curves(obj) }
    # https://github.com/NREL/openstudio-standards/blob/179D/lib/openstudio-standards/standards/Standards.Model.rb#L2338
    # Standards
    # https://github.com/NREL/openstudio-standards/blob/179D/lib/openstudio-standards/standards/Standards.CoilHeatingGas.rb#L8
    # OR PRM
    # https://github.com/NREL/openstudio-standards/blob/179D/lib/openstudio-standards/standards/ashrae_90_1_prm/ashrae_90_1_prm.CoilHeatingGas.rb#L111
    # above returns the PRM minimal efficiency and sets it with the below
    # https://github.com/NREL/openstudio-standards/blob/179D/lib/openstudio-standards/standards/ashrae_90_1_prm/ashrae_90_1_prm.CoilHeatingGas.rb#L116
    model.getCoilHeatingGass.sort.each do |obj|
      obj.setGasBurnerEfficiency(efficiency.to_f)
      runner.registerInfo("Modified GasCoil: #{obj.name} to efficiency: #{efficiency.to_f}.")
    end

    # Boilers
    model.getBoilerHotWaters.sort.each do |obj|
      obj.setNominalThermalEfficiency(boiler_efficiency.to_f)
      runner.registerInfo("Modified Boiler: #{obj.name} to boiler_efficiency: #{boiler_efficiency.to_f}.")
    end
    # Chillers
    model.getChillerElectricEIRs.sort.each do |obj|
      obj.setReferenceCOP(chiller_cop.to_f)
      runner.registerInfo("Modified Chiller: #{obj.name} to chiller_cop: #{chiller_cop.to_f}.")
    end

    if !ENV['SAVE_INTERMEDIATE'].nil?
      model.save('10.2.after_set_heating_cooling_cop_efficiency.osm')
    end

    true
  end
end

# register the measure to be used by the application
SetHeatingCoolingCopEfficiency.new.registerWithApplication
