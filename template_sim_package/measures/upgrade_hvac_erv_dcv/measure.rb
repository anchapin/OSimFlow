# ComStock, Copyright (c) 2023 Alliance for Sustainable Energy, LLC. All rights reserved.
# See top level LICENSE.txt file for license terms.

# *******************************************************************************
# OpenStudio(R), Copyright (c) 2008-2018, Alliance for Sustainable Energy, LLC.
# All rights reserved.
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#
# (1) Redistributions of source code must retain the above copyright notice,
# this list of conditions and the following disclaimer.
#
# (2) Redistributions in binary form must reproduce the above copyright notice,
# this list of conditions and the following disclaimer in the documentation
# and/or other materials provided with the distribution.
#
# (3) Neither the name of the copyright holder nor the names of any contributors
# may be used to endorse or promote products derived from this software without
# specific prior written permission from the respective party.
#
# (4) Other than as required in clauses (1) and (2), distributions in any form
# of modifications or other derivative works may not use the "OpenStudio"
# trademark, "OS", "os", or any other confusingly similar designation without
# specific prior written permission from Alliance for Sustainable Energy, LLC.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDER(S) AND ANY CONTRIBUTORS
# "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO,
# THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
# ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER(S), ANY CONTRIBUTORS, THE
# UNITED STATES GOVERNMENT, OR THE UNITED STATES DEPARTMENT OF ENERGY, NOR ANY OF
# THEIR EMPLOYEES, BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL,
# EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT
# OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
# INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT,
# STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY
# OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
# *******************************************************************************

# dependencies
require 'openstudio-standards'

# start the measure
class HVACERVDCV < OpenStudio::Measure::ModelMeasure
  # human readable name
  def name
    return 'HVAC ERV DCV'
  end

  # human readable description
  def description
    return 'Adds ERV or DCV.'
  end

  # human readable description of modeling approach
  def modeler_description
    return "ERV: Heat/energy recovery added based on climate zone. Energy recovery added to ASHRAE 'humid' climates, heat recovery added to all others. Effectiveness is based on Ventacity system. Additional fan static pressure is added as wheel power to capture impact of bypass. DCV: Add demand control ventilation to variable volume HVAC systems. Requires that the design specification outdoor air objects have some part of the ventilation be specified as per person. Also requires that if zone hvac equipment is present, it takes load priority over the ventilation system."
  end

  # define the arguments that the user will input
  def arguments(_model)
    args = OpenStudio::Measure::OSArgumentVector.new

    erv_or_dcv_choices = OpenStudio::StringVector.new
    erv_or_dcv_choices << 'ERV'
    erv_or_dcv_choices << 'DCV'
    erv_or_dcv_choices << 'skip both'

    erv_or_dcv = OpenStudio::Measure::OSArgument.makeChoiceArgument('erv_or_dcv', erv_or_dcv_choices, true)
    erv_or_dcv.setDisplayName('ERV or DCV:')
    erv_or_dcv.setDescription('Select whether to add an exhaust air energy or heat recovery system (ERV) or demand control ventilation (DCV).')
    erv_or_dcv.setDefaultValue('skip both')
    args << erv_or_dcv

    return args
  end

  def model_get_climate_zone(model)
    climate_zone = ''
    model.getClimateZones.climateZones.each do |cz|
      case cz.institution
   when 'ASHRAE'
     next if cz.value == '' # Skip blank ASHRAE climate zones put in by OpenStudio Application

     if cz.value == '7' || cz.value == '8'
       climate_zone = "ASHRAE 169-2013-#{cz.value}A"
     else
       climate_zone = "ASHRAE 169-2013-#{cz.value}"
     end
      when 'CEC'
        # Skip blank ASHRAE climate zones put in by OpenStudio Application
        if cz.value == ''
          next
        end

        climate_zone = "CEC T24-CEC#{cz.value}"
      end
    end
    return climate_zone
  end

  # define what happens when the measure is run
  def run(model, runner, user_arguments)
    super(model, runner, user_arguments)

    if !ENV['SAVE_INTERMEDIATE'].nil?
      model.save('before_upgrade_erv_or_dcv.osm')
    end

    # use the built-in error checking
    if !runner.validateUserArguments(arguments(model), user_arguments)
      return false
    end

    # assign user inputs
    erv_or_dcv = runner.getStringArgumentValue('erv_or_dcv', user_arguments)

    # build standard to access methods
    template = 'ComStock 90.1-2019'
    std = Standard.build(template)

    case erv_or_dcv
    when 'skip both'
      runner.registerInitialCondition('Skipping both ERV and DCV upgrades based on user input.')
      runner.registerFinalCondition('Skipped both ERV and DCV upgrades based on user input.')
      return true

    when 'ERV'
      runner.registerInfo('Applying ERV (HVAC Exhaust Air Energy or Heat Recovery) upgrade.')
      # applicability
      building_types_to_exclude = [
        # "Rtl",
        # "Rt3",
        # "RtS",
        'RFF',
        'RSD',
        # "RetailStandalone",
        # "RetailStripmall",
        'QuickServiceRestaurant',
        'FullServiceRestaurant'
      ]
      thermal_zone_names_to_exclude = [
        'Kitchen',
        'kitchen',
        'KITCHEN',
        'Dining',
        'dining',
        'DINING'
      ]

      # check building-type applicability
      building_types_to_exclude = building_types_to_exclude.map(&:downcase)
      if model.getBuilding.standardsBuildingType.is_initialized
        model_building_type = model.getBuilding.standardsBuildingType.get
      else
        runner.registerError('Building type not found.')
        return false
      end
      if building_types_to_exclude.include?(model_building_type.downcase)
        runner.registerError("Building type '#{model_building_type}' is not applicable to this measure.")
        return false
      end

      # check
      applicable_air_loops = []
      no_oa_air_loops = 0
      na_space_type_air_loops = 0
      hx_initial = 0
      run_sizing = false
      model.getAirLoopHVACs.each do |air_loop_hvac|
        # check for outdoor air
        oa_sys = air_loop_hvac.airLoopHVACOutdoorAirSystem
        no_oa_air_loops += 1 unless oa_sys.is_initialized
        next unless oa_sys.is_initialized

        # check to see if HX already exists
        has_hx = std.air_loop_hvac_energy_recovery?(air_loop_hvac)
        hx_initial += 1 if has_hx
        # check to see if airloop includes only non applicable thermal zones
        airloop_applicable_thermal_zones = []
        air_loop_hvac.thermalZones.each do |thermal_zone|
          if thermal_zone_names_to_exclude.none? { |word| thermal_zone.name.to_s.include?(word) }
            airloop_applicable_thermal_zones << thermal_zone
          end
        end
        (na_space_type_air_loops += 1) if airloop_applicable_thermal_zones.empty?
        next if airloop_applicable_thermal_zones.empty?
        # skip airloop if HX already exists
        next if has_hx

        # skip if evaporative cooling
        evap = false
        air_loop_hvac.supplyComponents.each do |comp|
          next unless comp.to_EvaporativeCoolerDirectResearchSpecial.is_initialized || comp.to_EvaporativeCoolerIndirectResearchSpecial.is_initialized

          evap = true
        end
        next if evap == true

        # add airloop to applicable list
        applicable_air_loops << air_loop_hvac
        # run sizing if any airloop does not have sizing data
        next if run_sizing

        oa_sys = oa_sys.get
        oa_controller = oa_sys.getControllerOutdoorAir
        if !oa_controller.maximumOutdoorAirFlowRate.is_initialized && !oa_controller.autosizedMaximumOutdoorAirFlowRate.is_initialized
          run_sizing = true
          puts('Performing sizing run....')
        end
      end

      # report initial condition of model
      runner.registerInitialCondition("ERV upgrade selected by user. The model started with #{model.getAirLoopHVACs.size} air loops, of which #{no_oa_air_loops} have no outdoor air, #{na_space_type_air_loops} have no applicable space types, and #{hx_initial} already have heat exchangers. #{applicable_air_loops.size} air loop(s) are applicable for adding an ERV/HRV.")

      if applicable_air_loops.empty?
        runner.registerError('Model contains no air loops that have outdoor already but do not already contain a heat exchanger.')
        return false
      end

      if run_sizing
        runner.registerInfo('Air loop outdoor air flow rates not sized. Running sizing run.')
        if std.model_run_sizing_run(model, "#{Dir.pwd}/SizingRun") == false
          runner.registerError('Sizing run failed. See errors in sizing run directory or this measure')
          return false
        end
      end

      # get climate full string and classification (i.e. "5A")
      climate_zone = model_get_climate_zone(model)
      climate_zone_classification = climate_zone.split('-')[-1]

      # DOAS temperature supply settings - colder cooling discharge air for humid climates
      _doas_dat_clg_c, _doas_dat_htg_c, doas_type =
        if ['1A', '2A', '3A', '4A', '5A', '6A', '7', '7A', '8', '8A'].include?(climate_zone_classification)
          [12.7778, 19.4444, 'ERV']
        else
          [15.5556, 19.4444, 'HRV']
        end

      # apply ERVs to applicable air loops in model
      hx_added = 0
      hx_cfm_added = 0
      fan_power_added = 0
      air_loops_affected = 0
      applicable_air_loops.each do |air_loop_hvac|
        oa_sys = air_loop_hvac.airLoopHVACOutdoorAirSystem
        std.air_loop_hvac_apply_energy_recovery_ventilator(air_loop_hvac, climate_zone)
        hx_added += 1
        # set heat exchanger efficiency levels
        # get outdoor airflow (which is used for sizing)
        oa_sys = oa_sys.get
        oa_sys.getControllerOutdoorAir
        air_loop_hvac.sizingSystem
        # get design outdoor air flow rate
        # this is used to estimate wheel "fan" power
        # loop through thermal zones
        oa_flow_m3_per_s = 0
        air_loop_hvac.thermalZones.each do |thermal_zone|
          space = thermal_zone.spaces[0]

          # get zone area
          fa = thermal_zone.floorArea * thermal_zone.multiplier

          # get zone volume
          vol = thermal_zone.airVolume * thermal_zone.multiplier

          # get zone design people
          num_people = thermal_zone.numberOfPeople * thermal_zone.multiplier

          if space.designSpecificationOutdoorAir.is_initialized
            dsn_spec_oa = space.designSpecificationOutdoorAir.get

            # add floor area component
            oa_area = dsn_spec_oa.outdoorAirFlowperFloorArea
            oa_flow_m3_per_s += oa_area * fa

            # add per person component
            oa_person = dsn_spec_oa.outdoorAirFlowperPerson
            oa_flow_m3_per_s += oa_person * num_people

            # add air change component
            oa_ach = dsn_spec_oa.outdoorAirFlowAirChangesperHour
            oa_flow_m3_per_s += (oa_ach * vol) / 60
          end
        end
        hx_cfm_added += OpenStudio.convert(oa_flow_m3_per_s, 'm^3/s', 'cfm').get

        # get HX object and set efficiency and controls
        oa_sys.oaComponents.each do |oa_comp|
          if oa_comp.to_HeatExchangerAirToAirSensibleAndLatent.is_initialized
            hx = oa_comp.to_HeatExchangerAirToAirSensibleAndLatent.get
            # set controls
            hx.setSupplyAirOutletTemperatureControl(true)
            hx.setFrostControlType('MinimumExhaustTemperature')
            hx.setThresholdTemperature(1.66667) # 35F, from E+ recommendation
            hx.setHeatExchangerType('Rotary') # rotary is used for fan power modulation when bypass is active. Only affects supply temp control with bypass.
            # add setpoint manager to control recovery
            # Add a setpoint manager OA pretreat to control the ERV
            spm_oa_pretreat = OpenStudio::Model::SetpointManagerOutdoorAirPretreat.new(air_loop_hvac.model)
            spm_oa_pretreat.setMinimumSetpointTemperature(-99.0)
            spm_oa_pretreat.setMaximumSetpointTemperature(99.0)
            spm_oa_pretreat.setMinimumSetpointHumidityRatio(0.00001)
            spm_oa_pretreat.setMaximumSetpointHumidityRatio(1.0)
            # Reference setpoint node and mixed air stream node are outlet node of the OA system
            mixed_air_node = oa_sys.mixedAirModelObject.get.to_Node.get
            spm_oa_pretreat.setReferenceSetpointNode(mixed_air_node)
            spm_oa_pretreat.setMixedAirStreamNode(mixed_air_node)
            # Outdoor air node is the outboard OA node of the OA system
            spm_oa_pretreat.setOutdoorAirStreamNode(oa_sys.outboardOANode.get)
            # Return air node is the inlet node of the OA system
            return_air_node = oa_sys.returnAirModelObject.get.to_Node.get
            spm_oa_pretreat.setReturnAirStreamNode(return_air_node)
            # Attach to the outlet of the HX
            hx_outlet = hx.primaryAirOutletModelObject.get.to_Node.get
            spm_oa_pretreat.addToNode(hx_outlet)

            # set parameters for ERV
            case doas_type
            when 'ERV'
              # set efficiencies; assumed 90% airflow returned to unit
              hx.setSensibleEffectivenessat100HeatingAirFlow(0.75 * 0.90)
              hx.setSensibleEffectivenessat75HeatingAirFlow(0.78 * 0.90)
              hx.setLatentEffectivenessat100HeatingAirFlow(0.61 * 0.90)
              hx.setLatentEffectivenessat75HeatingAirFlow(0.68 * 0.90)
              hx.setSensibleEffectivenessat100CoolingAirFlow(0.75 * 0.90)
              hx.setSensibleEffectivenessat75CoolingAirFlow(0.78 * 0.90)
              hx.setLatentEffectivenessat100CoolingAirFlow(0.55 * 0.90)
              hx.setLatentEffectivenessat75CoolingAirFlow(0.60 * 0.90)
            # set parameters for HRV
            when 'HRV'
              # set efficiencies; assumed 90% airflow returned to unit
              hx.setSensibleEffectivenessat100HeatingAirFlow(0.84 * 0.90)
              hx.setSensibleEffectivenessat75HeatingAirFlow(0.86 * 0.90)
              hx.setLatentEffectivenessat100HeatingAirFlow(0)
              hx.setLatentEffectivenessat75HeatingAirFlow(0)
              hx.setSensibleEffectivenessat100CoolingAirFlow(0.83 * 0.90)
              hx.setSensibleEffectivenessat75CoolingAirFlow(0.84 * 0.90)
              hx.setLatentEffectivenessat100CoolingAirFlow(0)
              hx.setLatentEffectivenessat75CoolingAirFlow(0)
            end

            # fan efficiency ranges from 40-60% (Energy Modeling Guide for Very High Efficiency DOAS Final Report)
            default_fan_efficiency = 0.55
            power = (oa_flow_m3_per_s * 174.188 / default_fan_efficiency) + ((oa_flow_m3_per_s * 0.9 * 124.42) / default_fan_efficiency)
            fan_power_added += power
            hx.setNominalElectricPower(power)
          end
        end
        air_loops_affected += 1
      end

      # report final condition of model
      runner.registerValue('hvac_number_of_loops_affected', air_loops_affected)
      runner.registerFinalCondition("Added #{hx_added} heat exchangers to air loops with #{hx_cfm_added.round(1)} total cfm and #{fan_power_added.round} watts added as rotary wheel power to account for added static pressure. The ASHRAE climate zone of the model is #{climate_zone_classification}, so an #{doas_type} is the recovery type added to applicable air loops.")

    when 'DCV'
      runner.registerInfo('Applying DCV (HVAC Demand Control Ventilation) upgrade.')
      # applicability
      if model.getBuilding.name.to_s.include?('hotel') || model.getBuilding.name.to_s.include?('Hotel') || model.getBuilding.name.to_s.include?('Htl') || model.getBuilding.name.to_s.include?('Mtl')
        runner.registerError("Model building type '#{model.getBuilding.name}' is a hotel and not eligible for DCV. This measure is not applicable.")
        return false
      elsif ((model.getBuilding.name.to_s.include?('restaurant') || model.getBuilding.name.to_s.include?('Restaurant') || model.getBuilding.name.to_s.include?('RSD') || model.getBuilding.name.to_s.include?('RFF'))) && !(model.getBuilding.name.to_s.include?('Strip') || model.getBuilding.name.to_s.include?('strip'))
        runner.registerError("Model building type '#{model.getBuilding.name}' is a restaurant and not eligible for DCV. This measure is not applicable.")
        return false
      elsif model.getBuilding.name.to_s.include?('apartment') || model.getBuilding.name.to_s.include?('Apartment') || model.getBuilding.name.to_s.include?('MFm') # || model.getBuilding.name.to_s.include?('DMo') || model.getBuilding.name.to_s.include?('SFm')
        runner.registerError("Model building type '#{model.getBuilding.name}' is a multifamily and not eligible for DCV. This measure is not applicable.")
        return false
      end

      # list of space types where DCV will not be applied
      space_types_no_dcv = [
        'Kitchen',
        'kitchen',
        'PatRm',
        'PatRoom',
        'Lab',
        'Exam',
        'PatCorridor',
        'BioHazard',
        'Exam',
        'OR',
        'PreOp',
        'Soil Work',
        'Trauma',
        'Triage',
        'PhysTherapy',
        'Data Center',
        'CorridorStairway',
        'Corridor',
        'Mechanical',
        'Restroom',
        'Entry',
        'Dining',
        'IT_Room',
        'LockerRoom',
        'Stair',
        'Toilet',
        'MechElecRoom'
      ]

      no_outdoor_air_loops = 0
      no_per_person_rates_loops = 0
      constant_volume_doas_loops = 0
      existing_dcv_loops = 0
      ervs = 0
      ineligible_space_types = 0
      selected_air_loops = []
      model.getAirLoopHVACs.each do |air_loop_hvac|
        # check for prevelance of OA system in air loop; skip if none
        oa_system = air_loop_hvac.airLoopHVACOutdoorAirSystem
        if oa_system.is_initialized
          oa_system = oa_system.get
        else
          no_outdoor_air_loops += 1
          runner.registerInfo("Air loop '#{air_loop_hvac.name}' does not have outdoor air and cannot have demand control ventilation.")
          next
        end

        # check if airloop is DOAS; skip if true
        sizing_system = air_loop_hvac.sizingSystem
        type_of_load = sizing_system.typeofLoadtoSizeOn
        if type_of_load == 'VentilationRequirement'
          constant_volume_doas_loops += 1
          runner.registerInfo("Air loop '#{air_loop_hvac.name}' is a constant volume DOAS system and cannot have demand control ventilation.")
          next
        end

        # Check for ERV. If the air loop has an ERV, air loop is not applicable for DCV measure.
        erv_components = []
        air_loop_hvac.oaComponents.each do |component|
          component_name = component.name.to_s
          next if component_name.include? 'Node'

          if component_name.include? 'ERV'
            erv_components << component
          end
        end

        if erv_components.any?
          runner.registerInfo("Air loop '#{air_loop_hvac.name}' has an ERV. DCV will not be applied.")
          ervs += 1
          # next
        end

        # check to see if airloop has existing DCV
        # TODO - if it does have DCV, check to see if all zones are getting DCV
        controller_oa = oa_system.getControllerOutdoorAir
        controller_mv = controller_oa.controllerMechanicalVentilation
        if controller_mv.demandControlledVentilation
          existing_dcv_loops += 1
          runner.registerInfo("Air loop '#{air_loop_hvac.name}' already has demand control ventilation enabled.")
          next
        end

        # check to see if airloop has applicable space types
        # these space types are often ventilation driven, or generally do not use ventilation rates per person
        # exclude these space types: kitchens, laboratories, patient care rooms
        # TODO - add functionality to add DCV to multizone systems to applicable zones only
        space_no_dcv = 0
        space_dcv = 0
        air_loop_hvac.thermalZones.sort.each do |zone|
          zone.spaces.each do |space|
            if space_types_no_dcv.any? { |i| space.spaceType.get.name.to_s.include? i }
              space_no_dcv += 1
            else
              space_dcv += 1
            end
          end
        end
        unless space_dcv >= 1
          runner.registerInfo("Air loop '#{air_loop_hvac.name}' serves only ineligible space types. DCV will not be applied.")
          ineligible_space_types += 1
          next
        end

        runner.registerInfo("Air loop '#{air_loop_hvac.name}' does not have existing demand control ventilation.  This measure will enable it.")
        selected_air_loops << air_loop_hvac
      end

      # report initial condition of model
      runner.registerInitialCondition("DCV upgrade selected by user. Out of #{model.getAirLoopHVACs.size} air loops, #{no_outdoor_air_loops} do not have outdoor air, #{no_per_person_rates_loops} have a zone without per-person OA rates, #{constant_volume_doas_loops} are constant volume DOAS systems, #{ervs} have ERVs, #{ineligible_space_types} serve ineligible space types, and #{existing_dcv_loops} already have demand control ventilation enabled, leaving #{selected_air_loops.size} eligible for demand control ventilation.")

      if selected_air_loops.empty?
        runner.registerFinalCondition('Model does not contain air loops eligible for enabling demand control ventilation. Skipping DCV upgrade.')
        return true
      end

      # enable DCV on selected air loops
      enabled_dcv = 0
      # total_cooling_capacity_w = 0
      # total_airflow_m3_s = 0
      selected_air_loops.each do |air_loop_hvac|
        air_loop_hvac.thermalZones.sort.each do |zone|
          zone.spaces.each do |space|
            dsn_oa = space.designSpecificationOutdoorAir
            next if dsn_oa.empty?

            dsn_oa = dsn_oa.get

            # set design specification outdoor air objects to sum
            dsn_oa.setOutdoorAirMethod('Sum')

            # Get the space properties
            floor_area = space.floorArea * space.multiplier
            number_of_people = space.numberOfPeople * space.multiplier
            people_per_m2 = space.peoplePerFloorArea

            # Sum up the total OA from all sources
            oa_for_people_per_m2 = people_per_m2 * dsn_oa.outdoorAirFlowperPerson
            oa_for_floor_area_per_m2 = dsn_oa.outdoorAirFlowperFloorArea
            tot_oa_per_m2 = oa_for_people_per_m2 + oa_for_floor_area_per_m2
            tot_oa_cfm_per_ft2 = OpenStudio.convert(OpenStudio.convert(tot_oa_per_m2, 'm^3/s', 'cfm').get, '1/m^2', '1/ft^2').get
            tot_oa_cfm = floor_area * tot_oa_cfm_per_ft2

            # if space is ineligible type, convert all OA to per-area to avoid DCV being applied
            if space_types_no_dcv.any? { |i| space.spaceType.get.name.to_s.include? i } && !dsn_oa.outdoorAirFlowperPerson.zero?
              runner.registerInfo("Space '#{space.name}' is an ineligible space type but is on an air loop that serves other DCV-eligible spaces. Converting all outdoor air to per-area.")
              dsn_oa.setOutdoorAirFlowperPerson(0.0)
              dsn_oa.setOutdoorAirFlowperFloorArea(tot_oa_per_m2)
              next
            end

            # if both per-area and per-person are present, does not need to be modified
            if !dsn_oa.outdoorAirFlowperPerson.zero? && !dsn_oa.outdoorAirFlowperFloorArea.zero?
              next

            # if both are zero, skip space
            elsif dsn_oa.outdoorAirFlowperPerson.zero? && dsn_oa.outdoorAirFlowperFloorArea.zero?
              runner.registerInfo("Space '#{space.name}' has 0 outdoor air per-person and per-area rates. DCV may be still be applied to this air loop, but it will not function on this space.")
              next

            # if per-person or per-area values are zero, set to 10 cfm / person and allocate the rest to per-area
            elsif dsn_oa.outdoorAirFlowperPerson.zero? || dsn_oa.outdoorAirFlowperFloorArea.zero?
              # puts "========Before Per Person========="
              # puts "Per-person", dsn_oa.outdoorAirFlowperPerson * people_per_m2
              # puts "Per-area", dsn_oa.outdoorAirFlowperFloorArea
              # puts "Total OA", tot_oa_per_m2

              if dsn_oa.outdoorAirFlowperPerson.zero?
                runner.registerInfo("Space '#{space.name}' per-person outdoor air rate is 0. Using a minimum of 10 cfm / person and assigning the remaining space outdoor air requirement to per-area.")
              elsif dsn_oa.outdoorAirFlowperFloorArea.zero?
                runner.registerInfo("Space '#{space.name}' per-area outdoor air rate is 0. Using a minimum of 10 cfm / person and assigning the remaining space outdoor air requirement to per-area.")
              end

              # default ventilation is 10 cfm / person
              per_person_ventilation_rate = OpenStudio.convert(10, 'ft^3/min', 'm^3/s').get

              # assign remaining oa to per-area
              new_oa_for_people_per_m2 = people_per_m2 * per_person_ventilation_rate
              new_oa_for_people_cfm_per_f2 = OpenStudio.convert(OpenStudio.convert(new_oa_for_people_per_m2, 'm^3/s', 'cfm').get, '1/m^2', '1/ft^2').get
              new_oa_for_people_cfm = number_of_people * new_oa_for_people_cfm_per_f2
              remaining_oa_per_m2 = tot_oa_per_m2 - new_oa_for_people_per_m2
              if remaining_oa_per_m2 <= 0
                runner.registerInfo("Space '#{space.name}' has #{number_of_people.round(1)} people which corresponds to a ventilation minimum requirement of #{new_oa_for_people_cfm.round(0)} cfm at 10 cfm / person, but total zone outdoor air is only #{tot_oa_cfm.round(0)} cfm. Setting all outdoor air as per-person.")
                per_person_ventilation_rate = tot_oa_per_m2 / people_per_m2
                dsn_oa.setOutdoorAirFlowperFloorArea(0.0)
              else
                oa_per_area_per_m2 = remaining_oa_per_m2
                dsn_oa.setOutdoorAirFlowperFloorArea(oa_per_area_per_m2)
              end
              dsn_oa.setOutdoorAirFlowperPerson(per_person_ventilation_rate)

              # puts "========After Per Person========="
              # puts "Per-person", dsn_oa.outdoorAirFlowperPerson * people_per_m2
              # puts "Per-area", dsn_oa.outdoorAirFlowperFloorArea
              # puts "Total OA", dsn_oa.outdoorAirFlowperPerson * people_per_m2 + dsn_oa.outdoorAirFlowperFloorArea
            end

            # zero-out the ACH, and flow requirements
            # dsn_oa.setOutdoorAirFlowAirChangesperHour(0.0)
            # dsn_oa.setOutdoorAirFlowRate(0.0)
          end
        end

        std.air_loop_hvac_enable_demand_control_ventilation(air_loop_hvac, '')
        enabled_dcv += 1
      end

      # report final condition of model
      runner.registerFinalCondition("Enabled DCV for #{enabled_dcv} air loops in the model.")

    else
      runner.registerError('Unrecognized input argument.')
      return false
    end

    if !ENV['SAVE_INTERMEDIATE'].nil?
      model.save('after_upgrade_erv_or_dcv.osm')
    end

    return true
  end
end

# register the measure to be used by the application
HVACERVDCV.new.registerWithApplication
