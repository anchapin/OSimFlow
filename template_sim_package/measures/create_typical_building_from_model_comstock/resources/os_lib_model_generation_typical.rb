# *******************************************************************************
# OpenStudio(R), Copyright (c) Alliance for Sustainable Energy, LLC.
# See also https://openstudio.net/license
# *******************************************************************************

module OsLib_ModelGeneration_Typical
    def reenable_ptac_pthp_outdoor_air(zones, runner)
      zones.each do |zone|
        zone.equipment.sort.each do |equip|
          ptac = equip.to_ZoneHVACPackagedTerminalAirConditioner
          if ptac.is_initialized
            ptac = ptac.get
            ptac.autosizeOutdoorAirFlowRateDuringCoolingOperation
            ptac.autosizeOutdoorAirFlowRateDuringHeatingOperation
            ptac.autosizeOutdoorAirFlowRateWhenNoCoolingorHeatingisNeeded
            runner.registerInfo("Re-enabled outdoor air on #{ptac.name}")
          end

          pthp = equip.to_ZoneHVACPackagedTerminalHeatPump
          next unless pthp.is_initialized

          pthp = pthp.get
          pthp.autosizeOutdoorAirFlowRateDuringCoolingOperation
          pthp.autosizeOutdoorAirFlowRateDuringHeatingOperation
          pthp.autosizeOutdoorAirFlowRateWhenNoCoolingorHeatingisNeeded
          runner.registerInfo("Re-enabled outdoor air on #{pthp.name}")
        end
      end
    end

    # create typical building from model
    # creates a complete energy model from model with defined geometry and standards space type assignments
    #
    # @param template [String] standard template
    # @param climate_zone [String] ASHRAE climate zone, e.g. 'ASHRAE 169-2013-4A'.
    # @param add_hvac [Boolean] Add HVAC systems to the model
    # @param hvac_system_type [String] HVAC system type
    # @param hvac_delivery_type [String] HVAC delivery type, how the system delivers heating or cooling to zones.
    #   Options are 'Forced Air' or 'Hydronic'.
    # @param heating_fuel [String] The primary HVAC heating fuel type.
    #   Options are 'Electricity', 'NaturalGas', 'DistrictHeating', 'DistrictHeatingWater', 'DistrictHeatingSteam', 'DistrictAmbient'
    # @param service_water_heating_fuel [String] The primary service water heating fuel type.
    #   Options are 'Inferred', 'Electricity', 'NaturalGas', 'DistrictHeating', 'DistrictHeatingWater', 'DistrictHeatingSteam', 'HeatPump'
    # @param cooling_fuel [String] The primary HVAC cooling fuel type
    #   Options are 'Electricity', 'DistrictCooling', 'DistrictAmbient'
    # @param kitchen_makeup [String] Source of makeup air for kitchen exhaust
    #   Options are 'None', 'Adjacent'
    # @param exterior_lighting_zone [String] The exterior lighting zone for exterior lighting allowance.
    #   Options are '0 - Undeveloped Areas Parks', '1 - Developed Areas Parks', '2 - Neighborhood', '3 - All Other Areas', '4 - High Activity'
    # @param add_constructions [Boolean] Create and apply default construction set
    # @param wall_construction_type [String] wall construction type.
    #  Options are 'Inferred', 'Mass', 'Metal Building', 'WoodFramed', 'SteelFramed'
    # @param add_space_type_loads [Boolean] Populate existing standards space types in the model with internal loads
    # @param add_daylighting_controls [Boolean] Add daylighting controls
    # @param add_infiltration [Boolean] Adds infiltration to the model based on cosntruction
    # @param add_elevators [Boolean] Apply elevators directly to a space in the model instead of to a space type
    # @param add_internal_mass [Boolean] Add internal mass to each space
    # @param add_exterior_lights [Boolean] Add exterior lightings objects to parking, canopies, and facades
    # @param onsite_parking_fraction [Double] Fraction of allowable exterior parking lighting applied. Set to 0 to add no parking lighting.
    # @param add_exhaust [Boolean] Add exhaust fans to the models. Primarly kitchen exhaust fans.
    # @param add_swh [Boolean] Add service water heating supply and demand objects
    # @param add_thermostat [Boolean] Add thermostats to thermal zones based on the standards space type
    # @param add_refrigeration [Boolean] Add refrigerated cases and walkin refrigeration
    # @param refrigeration_template [String] The refrigeration technology level, either 'old', 'new', or 'advanced'
    # @param modify_wkdy_op_hrs [Boolean] Modify the default weekday hours of operation
    # @param wkdy_op_hrs_start_time [Double] Weekday operating hours start time. Enter as a fractional value, e.g. 5:15pm is 17.25. Only used if modify_wkdy_op_hrs is true.
    # @param wkdy_op_hrs_duration [Double] Weekday operating hours duration from start time. Enter as a fractional value, e.g. 5:15pm is 17.25. Only used if modify_wkdy_op_hrs is true.
    # @param modify_wknd_op_hrs [Boolean] Modify the default weekend hours of operation
    # @param wknd_op_hrs_start_time [Double] Weekend operation hours start time. Enter as a fractional value, e.g. 5:15pm is 17.25. Only used if modify_wknd_op_hrs is true.
    # @param wknd_op_hrs_duration [Double] Weekend operating hours duration from start time. Enter as a fractional value, e.g. 5:15pm is 17.25. Only used if modify_wknd_op_hrs is true.
    # @param hoo_var_method [String] hours of operation variable method. Options are 'hours' or 'fractional'.
    # @param enable_dst [Boolean] Enable daylight savings
    # @param unmet_hours_tolerance_r [Double] Thermostat setpoint tolerance for unmet hours in degrees Rankine
    # @param remove_objects [Boolean] Clean model of non-geometry objects. Only removes the same objects types as those added to the model.
    # @param user_hvac_mapping [Hash] Hash defining a mapping of system types to zones.
    #   Structure is:
    #     ['systems'][N]['system_type'] = 'MY_CBECS_HVAC_TYPE' as defined in lib/openstudio-standards/hvac/cbecs_hvac.rb
    #     ['systems'][N]['thermal_zones'] = ['Zone 1', 'Zone 2', ...]
    # @return [Boolean] returns true if successful, false if not
    def typical_building_from_model(
      model,
      runner,
      template,
      climate_zone: 'Lookup From Model',
      add_hvac: true,
      hvac_system_type: 'Inferred',
      hvac_delivery_type: 'Forced Air',
      heating_fuel: 'NaturalGas',
      service_water_heating_fuel: 'NaturalGas',
      cooling_fuel: 'Electricity',
      kitchen_makeup: 'Adjacent',
      exterior_lighting_zone: '3 - All Other Areas',
      add_constructions: true,
      wall_construction_type: 'Inferred',
      add_space_type_loads: true,
      add_daylighting_controls: true,
      add_infiltration: true,
      add_elevators: true,
      add_internal_mass: true,
      add_exterior_lights: true,
      onsite_parking_fraction: 1.0,
      add_exhaust: true,
      add_swh: true,
      add_thermostat: true,
      add_refrigeration: true,
      refrigeration_template: 'new',
      modify_wkdy_op_hrs: false,
      wkdy_op_hrs_start_time: 8.0,
      wkdy_op_hrs_duration: 8.0,
      modify_wknd_op_hrs: false,
      wknd_op_hrs_start_time: 8.0,
      wknd_op_hrs_duration: 8.0,
      hoo_var_method: 'hours',
      enable_dst: true,
      unmet_hours_tolerance_r: 1.0,
      remove_objects: true,
      user_hvac_mapping: nil,
      sizing_run_directory: nil
    )
      # sizing run directory
      sizing_run_directory = Dir.pwd if sizing_run_directory.nil?

      # report initial condition of model
      initial_object_size = model.getModelObjects.size
      runner.registerInitialCondition("The building started with #{initial_object_size} objects.")

      # open channel to log messages
      reset_log

      # create a new standard class
      standard = Standard.build(template)

      # validate climate zone
      if climate_zone == 'Lookup From Model' || climate_zone.nil?
        climate_zone = standard.model_get_building_properties(model)['climate_zone']
        runner.registerInfo("Using climate zone #{climate_zone} from model")
      else
        runner.registerInfo("Using climate zone #{climate_zone} from user arguments")
      end
      if climate_zone == ''
        runner.registerError('Could not determine climate zone from measure arguments or model.')
        return false
      end

      # validate weekday hours of operation
      wkdy_op_hrs_start_time_hr = nil
      wkdy_op_hrs_start_time_min = nil
      wkdy_op_hrs_duration_hr = nil
      wkdy_op_hrs_duration_min = nil
      if modify_wkdy_op_hrs
        # weekday start time hr
        wkdy_op_hrs_start_time_hr = wkdy_op_hrs_start_time.floor
        if wkdy_op_hrs_start_time_hr < 0 || wkdy_op_hrs_start_time_hr > 24
          runner.registerError("Weekday operating hours start time hrs must be between 0 and 24.  #{wkdy_op_hrs_start_time} was entered.")
          return false
        end

        # weekday start time min
        wkdy_op_hrs_start_time_min = (60.0 * (wkdy_op_hrs_start_time - wkdy_op_hrs_start_time.floor)).floor
        if wkdy_op_hrs_start_time_min < 0 || wkdy_op_hrs_start_time_min > 59
          runner.registerError("Weekday operating hours start time mins must be between 0 and 59.  #{wkdy_op_hrs_start_time} was entered.")
          return false
        end

        # weekday duration hr
        wkdy_op_hrs_duration_hr = wkdy_op_hrs_duration.floor
        if wkdy_op_hrs_duration_hr < 0 || wkdy_op_hrs_duration_hr > 24
          runner.registerError("Weekday operating hours duration hrs must be between 0 and 24.  #{wkdy_op_hrs_duration} was entered.")
          return false
        end

        # weekday duration min
        wkdy_op_hrs_duration_min = (60.0 * (wkdy_op_hrs_duration - wkdy_op_hrs_duration.floor)).floor
        if wkdy_op_hrs_duration_min < 0 || wkdy_op_hrs_duration_min > 59
          runner.registerError("Weekday operating hours duration mins must be between 0 and 59.  #{wkdy_op_hrs_duration} was entered.")
          return false
        end

        # check that weekday start time plus duration does not exceed 24 hrs
        if (wkdy_op_hrs_start_time_hr + wkdy_op_hrs_duration_hr + ((wkdy_op_hrs_start_time_min + wkdy_op_hrs_duration_min) / 60.0)) > 24.0
          runner.registerInfo("Weekday start time of #{wkdy_op_hrs_start_time} plus duration of #{wkdy_op_hrs_duration} is more than 24 hrs, hours of operation overlap midnight.")
        end
      end

      # validate weekend hours of operation
      wknd_op_hrs_start_time_hr = nil
      wknd_op_hrs_start_time_min = nil
      wknd_op_hrs_duration_hr = nil
      wknd_op_hrs_duration_min = nil
      if modify_wknd_op_hrs
        # weekend start time hr
        wknd_op_hrs_start_time_hr = wknd_op_hrs_start_time.floor
        if wknd_op_hrs_start_time_hr < 0 || wknd_op_hrs_start_time_hr > 24
          runner.registerError("Weekend operating hours start time hrs must be between 0 and 24.  #{wknd_op_hrs_start_time} was entered.")
          return false
        end

        # weekend start time min
        wknd_op_hrs_start_time_min = (60.0 * (wknd_op_hrs_start_time - wknd_op_hrs_start_time.floor)).floor
        if wknd_op_hrs_start_time_min < 0 || wknd_op_hrs_start_time_min > 59
          runner.registerError("Weekend operating hours start time mins must be between 0 and 59.  #{wknd_op_hrs_start_time} was entered.")
          return false
        end

        # weekend duration hr
        wknd_op_hrs_duration_hr = wknd_op_hrs_duration.floor
        if wknd_op_hrs_duration_hr < 0 || wknd_op_hrs_duration_hr > 24
          runner.registerError("Weekend operating hours duration hrs must be between 0 and 24.  #{wknd_op_hrs_duration} was entered.")
          return false
        end

        # weekend duration min
        wknd_op_hrs_duration_min = (60.0 * (wknd_op_hrs_duration - wknd_op_hrs_duration.floor)).floor
        if wknd_op_hrs_duration_min < 0 || wknd_op_hrs_duration_min > 59
          runner.registerError("Weekend operating hours duration min smust be between 0 and 59.  #{wknd_op_hrs_duration} was entered.")
          return false
        end

        # check that weekend start time plus duration does not exceed 24 hrs
        if (wknd_op_hrs_start_time_hr + wknd_op_hrs_duration_hr + ((wknd_op_hrs_start_time_min + wknd_op_hrs_duration_min) / 60.0)) > 24.0
          runner.registerInfo("Weekend start time of #{wknd_op_hrs_start} plus duration of #{wknd_op_hrs_duration} is more than 24 hrs, hours of operation overlap midnight.")
        end
      end

      # validate unmet hours tolerance
      if unmet_hours_tolerance_r < 0
        runner.registerError('unmet_hours_tolerance_r must be greater than or equal to 0 Rankine.')
        return false
      elsif unmet_hours_tolerance_r > 5.0
        runner.registerError('unmet_hours_tolerance_r must be less than or equal to 5 Rankine.')
        return false
      end

      # make sure daylight savings is turned on up prior to any sizing runs being done.
      if enable_dst
        start_date = '2nd Sunday in March'
        end_date = '1st Sunday in November'

        runperiodctrl_daylightsaving = model.getRunPeriodControlDaylightSavingTime
        runperiodctrl_daylightsaving.setStartDate(start_date)
        runperiodctrl_daylightsaving.setEndDate(end_date)
      end

      # add internal loads to space types
      if add_space_type_loads

        # remove internal loads
        if remove_objects
          model.getSpaceLoads.sort.each do |instance|
            # most prototype building types model exterior elevators with name Elevator
            next if instance.name.to_s.include?('Elevator')
            next if instance.to_InternalMass.is_initialized
            next if instance.to_WaterUseEquipment.is_initialized

            instance.remove
          end
          model.getDesignSpecificationOutdoorAirs.each(&:remove)
          model.getDefaultScheduleSets.each(&:remove)
        end

        model.getSpaceTypes.sort.each do |space_type|
          set_people = true
          set_lights = true
          set_electric_equipment = true
          set_gas_equipment = true
          set_ventilation = true
          test = standard.space_type_apply_internal_loads(space_type, set_people, set_lights, set_electric_equipment, set_gas_equipment, set_ventilation)
          if test == false
            runner.registerWarning("Could not add loads for #{space_type.name}. Not expected for #{template}")
            next
          end

          # apply internal load schedules
          # the last bool test it to make thermostat schedules. They are now added in HVAC section instead of here
          make_thermostat = false
          standard.space_type_apply_internal_load_schedules(space_type, set_people, set_lights, set_electric_equipment, set_gas_equipment, set_ventilation, make_thermostat)

          # extend space type name to include the template. Consider this as well for load defs
          space_type.setName("#{space_type.name} - #{template}")
          runner.registerInfo("Adding loads to space type named #{space_type.name}")
        end

        # warn if spaces in model without space type
        spaces_without_space_types = []
        model.getSpaces.sort.each do |space|
          next if space.spaceType.is_initialized

          spaces_without_space_types << space
        end
        if !spaces_without_space_types.empty?
          runner.registerWarning("#{spaces_without_space_types.size} spaces do not have space types assigned, and wont' receive internal loads from standards space type lookups.")
        end
      end

      # identify primary building type (used for construction, and ideally HVAC as well)
      building_types = {}
      model.getSpaceTypes.sort.each do |space_type|
        # populate hash of building types
        if space_type.standardsBuildingType.is_initialized
          bldg_type = space_type.standardsBuildingType.get
          if building_types.key?(bldg_type)
            building_types[bldg_type] += space_type.floorArea
          else
            building_types[bldg_type] = space_type.floorArea
          end
        else
          runner.registerWarning("Can't identify building type for #{space_type.name}")
        end
      end
      # @todo this fails if no space types, or maybe just no space types with standards
      primary_bldg_type = building_types.key(building_types.values.max)
      # Used for some lookups in the standards gem
      lookup_building_type = standard.model_get_lookup_name(primary_bldg_type)
      model.getBuilding.setStandardsBuildingType(primary_bldg_type)

      # make construction set and apply to building
      if add_constructions

        # remove default construction sets
        if remove_objects
          model.getDefaultConstructionSets.each(&:remove)
        end

        # @todo allow building type and space type specific constructions set selection.
        if ['SmallHotel', 'LargeHotel', 'MidriseApartment', 'HighriseApartment'].include?(primary_bldg_type)
          is_residential = 'Yes'
          occ_type = 'Residential'
        else
          is_residential = 'No'
          occ_type = 'Nonresidential'
        end

        # set FC factor constructions before adding other constructions
        standard.model_set_below_grade_wall_constructions(model, lookup_building_type, climate_zone)
        standard.model_set_floor_constructions(model, lookup_building_type, climate_zone)
        if model.getFFactorGroundFloorConstructions.empty?
          runner.registerInfo('Unable to determine FC factor value to use. Using default ground construction instead.')
        else
          runner.registerInfo('Set FC factor constructions for slab and below grade walls.')
        end

        # adjust F factor constructions to avoid simulation errors
        model.getFFactorGroundFloorConstructions.each do |cons|
          # Rfilm_in = 0.135, Rfilm_out = 0.03, Rcons for 6" heavy concrete = 0.15m / 1.95 W/mK, 0.001 minimum resistance of Rfic resistive layer
          if cons.area <= (0.135 + 0.03 + (0.15 / 1.95) + 0.001) * cons.perimeterExposed * cons.fFactor
            # set minimum Rfic to ~ R1 = 0.18 m^2K/W
            new_area = 0.422 * cons.perimeterExposed * cons.fFactor
            runner.registerInfo("F-factor fictitious resistance for #{cons.name.get} with Area=#{cons.area.round(2)}, Exposed Perimeter=#{cons.perimeterExposed.round(2)}, and F-factor=#{cons.fFactor.round(2)} will result in a negative value and a failed simulation. Construction area is adjusted to be #{new_area.round(2)} m2.")
            cons.setArea(new_area)
          end
        end

        bldg_def_const_set = standard.model_add_construction_set(model, climate_zone, lookup_building_type, nil, is_residential)
        if bldg_def_const_set.is_initialized
          bldg_def_const_set = bldg_def_const_set.get
          if is_residential == 'Yes'
            bldg_def_const_set.setName("Res #{bldg_def_const_set.name}")
          end
          model.getBuilding.setDefaultConstructionSet(bldg_def_const_set)
          runner.registerInfo("Adding default construction set named #{bldg_def_const_set.name}")
        else
          runner.registerError("Could not create default construction set for the building type #{lookup_building_type} in climate zone #{climate_zone}.")
          log_messages_to_runner(runner, debug = true)
          return false
        end

        # Replace the construction of exterior walls with user-specified wall construction type
        unless wall_construction_type == 'Inferred'
          # Check that a default exterior construction set is defined
          if bldg_def_const_set.defaultExteriorSurfaceConstructions.empty?
            runner.registerError('Default construction set has no default exterior surface constructions.')
            log_messages_to_runner(runner, debug = true)
            return false
          end
          ext_surf_consts = bldg_def_const_set.defaultExteriorSurfaceConstructions.get

          # Check that a default exterior wall is defined
          if ext_surf_consts.wallConstruction.empty?
            runner.registerError('Default construction set has no default exterior wall construction.')
            log_messages_to_runner(runner, debug = true)
            return false
          end
          old_construction = ext_surf_consts.wallConstruction.get
          standards_info = old_construction.standardsInformation

          # Get the old wall construction type
          if standards_info.standardsConstructionType.empty?
            old_wall_construction_type = 'Not defined'
          else
            old_wall_construction_type = standards_info.standardsConstructionType.get
          end

          # Modify the default wall construction if different from measure input
          if old_wall_construction_type == wall_construction_type
            # Don't modify if the default matches the user-specified wall construction type
            runner.registerInfo("Exterior wall construction type #{wall_construction_type} is the default for this building type.")
          else
            climate_zone_set = standard.model_find_climate_zone_set(model, climate_zone)
            new_construction = standard.model_find_and_add_construction(model,
                                                                        climate_zone_set,
                                                                        'ExteriorWall',
                                                                        wall_construction_type,
                                                                        occ_type)
            ext_surf_consts.setWallConstruction(new_construction)
            runner.registerInfo("Set exterior wall construction to #{new_construction.name}, replacing building type default #{old_construction.name}.")
          end
        end

        # Replace the construction of any outdoor-facing "AtticFloor" surfaces
        # with the "ExteriorRoof" - "IEAD" construction for the specific climate zone and template.
        # This prevents creation of buildings where the DOE Prototype building construction set
        # assumes an attic but the supplied geometry used does not have an attic.
        new_construction = nil
        climate_zone_set = standard.model_find_climate_zone_set(model, climate_zone)
        model.getSurfaces.sort.each do |surf|
          next unless surf.outsideBoundaryCondition == 'Outdoors'
          next unless surf.surfaceType == 'RoofCeiling'
          next if surf.construction.empty?

          construction = surf.construction.get
          standards_info = construction.standardsInformation
          next if standards_info.intendedSurfaceType.empty?
          next unless standards_info.intendedSurfaceType.get == 'AtticFloor'

          if new_construction.nil?
            new_construction = standard.model_find_and_add_construction(model,
                                                                        climate_zone_set,
                                                                        'ExteriorRoof',
                                                                        'IEAD',
                                                                        occ_type)
          end
          surf.setConstruction(new_construction)
          runner.registerInfo("Changed the construction for #{surf.name} from #{construction.name} to #{new_construction.name} to avoid outdoor-facing attic floor constructions in buildings with no attic space.")
        end

        # address any adiabatic surfaces that don't have hard assigned constructions
        model.getSurfaces.sort.each do |surface|
          next if surface.outsideBoundaryCondition != 'Adiabatic'
          next if surface.construction.is_initialized

          surface.setAdjacentSurface(surface)
          surface.setConstruction(surface.construction.get)
          surface.setOutsideBoundaryCondition('Adiabatic')
        end

        # set ground temperatures from DOE prototype buildings
        OpenstudioStandards::Weather.model_set_ground_temperatures(model, climate_zone: climate_zone)
      end

      # add infiltration
      if add_infiltration
        if remove_objects
          model.getSpaceInfiltrationDesignFlowRates.each(&:remove)
          model.getSpaceInfiltrationEffectiveLeakageAreas.each(&:remove)
          model.getSpaceInfiltrationFlowCoefficients.each(&:remove)
        end

        # use NIST method for determining infiltration
        # this sets a default always on; schedules are adjusted later if HVAC is added
        OpenstudioStandards::Infiltration.model_set_nist_infiltration(model,
                                                                      airtightness_value: standard.default_airtightness,
                                                                      air_barrier: standard.default_air_barrier)
      end

      # add elevators (returns ElectricEquipment object)
      if add_elevators

        # remove elevators as spaceLoads or exteriorLights
        model.getSpaceLoads.sort.each do |instance|
          next if !instance.name.to_s.include?('Elevator') # most prototype building types model exterior elevators with name Elevator

          instance.remove
        end
        model.getExteriorLightss.sort.each do |ext_light|
          next if !ext_light.name.to_s.include?('Fuel equipment') # some prototype building types model exterior elevators by this name

          ext_light.remove
        end

        elevators = standard.model_add_elevators(model)
        if elevators.nil?
          runner.registerInfo('No elevators added to the building.')
        else
          elevator_def = elevators.electricEquipmentDefinition
          design_level = elevator_def.designLevel.get
          runner.registerInfo("Adding #{elevators.multiplier.round(1)} elevators each with power of #{OpenStudio.toNeatString(design_level, 0, true)} (W), plus lights and fans.")
          elevator_def.setFractionLatent(0.0)
          elevator_def.setFractionRadiant(0.0)
          elevator_def.setFractionLost(1.0)
        end
      end

      # add exterior lights (returns a hash where key is lighting type and value is exteriorLights object)
      if add_exterior_lights

        if remove_objects
          model.getExteriorLightss.sort.each do |ext_light|
            next if ext_light.name.to_s.include?('Fuel equipment') # some prototype building types model exterior elevators by this name

            ext_light.remove
          end
        end
        exterior_lights = OpenstudioStandards::ExteriorLighting.model_create_typical_exterior_lighting(model,
                                                                                                       lighting_generation: 'default',
                                                                                                       lighting_zone: exterior_lighting_zone.chars[0].to_i,
                                                                                                       onsite_parking_fraction: onsite_parking_fraction)
        exterior_lights.each do |v|
          runner.registerInfo("Adding Exterior Lights named #{v.exteriorLightsDefinition.name} with design level of #{v.exteriorLightsDefinition.designLevel} * #{OpenStudio.toNeatString(v.multiplier, 0, true)}.")
        end
      end

      # add service water heating demand and supply
      if add_swh

        # remove water use equipment and water use connections
        if remove_objects
          # @todo remove plant loops used for service water heating
          model.getWaterUseEquipments.each(&:remove)
          model.getWaterUseConnectionss.each(&:remove)
          # Also remove stale Pipe:Indoor / Pipe:Outdoor objects and their
          # named "Piping Air Velocity"/"Piping Air Temp" schedules. Without
          # this cleanup the upstream OpenstudioStandards SWH path finds the
          # fixture's existing schedules by name+value match in
          # create_constant_schedule_ruleset and returns them with their old
          # ScheduleTypeLimits intact. Pipe:Indoor#setAmbientAirVelocitySchedule
          # then rejects the attachment with "incompatible ScheduleTypeLimits"
          # leaving the schedule field empty, which EnergyPlus rejects with
          # "Invalid Ambient Air Velocity Schedule Name=" -> Fatal exit.
          model.getPipeIndoors.each(&:remove)
          model.getPipeOutdoors.each(&:remove)
          model.getScheduleRulesets.each do |sch|
            n = sch.name.to_s
            sch.remove if n.include?('Piping Air Velocity') || n.include?('Piping Air Temp')
          end
        end

        # Infer the SWH type
        if service_water_heating_fuel == 'Inferred'
          if heating_fuel == 'NaturalGas' || heating_fuel.include?('DistrictHeating')
            # If building has gas service, probably uses natural gas for SWH
            service_water_heating_fuel = 'NaturalGas'
          elsif heating_fuel == 'Electricity'
            # If building is doing space heating with electricity, probably used for SWH
            service_water_heating_fuel = 'Electricity'
          elsif heating_fuel == 'DistrictAmbient'
            # If building has district ambient loop, it is fancy and probably uses HPs for SWH
            service_water_heating_fuel = 'HeatPump'
          else
            # Use inferences built into OpenStudio Standards for each building and space type
            service_water_heating_fuel = nil
          end
        end

        # NOTE: 179D Override
        # This is using typical values from https://github.com/NREL/openstudio-standards/blob/95b8178956b8e16533aabd2bd3aaddb31dcdd153/lib/openstudio-standards/service_water_heating/data/typical_water_use_equipment.json#L405-L420
        # Not what we want!
        if standard.respond_to?(:model_add_typical_swh)
          typical_swh = standard.model_add_typical_swh(model, water_heater_fuel: service_water_heating_fuel)
        else
          typical_swh = OpenstudioStandards::ServiceWaterHeating.create_typical_service_water_heating(model, water_heating_fuel: service_water_heating_fuel)
          raise "Shouldn't happen" if template == '179D 90.1-2007'
        end
        midrise_swh_loops = []
        stripmall_swh_loops = []
        typical_swh.each do |loop|
          if loop.name.get.include?('MidriseApartment')
            midrise_swh_loops << loop
          elsif loop.name.get.include?('RetailStripmall')
            stripmall_swh_loops << loop
          else
            water_use_connections = []
            loop.demandComponents.each do |component|
              next if !component.to_WaterUseConnections.is_initialized

              water_use_connections << component
            end
            runner.registerInfo("Adding #{loop.name} to the building. It has #{water_use_connections.size} water use connections.")
          end
        end
        if !midrise_swh_loops.empty?
          runner.registerInfo("Adding #{midrise_swh_loops.size} MidriseApartment service water heating loops.")
        end
        if !stripmall_swh_loops.empty?
          runner.registerInfo("Adding #{stripmall_swh_loops.size} RetailStripmall service water heating loops.")
        end

        # @todo this is no longer part of the upstream ComStock / openstudio-standards create_typical but it makes EUI
        # diffs for adding it back in
        # Modify Pipe:Indoor objects to have an ambient temperature that's always
        # higher than the warmest Site:WaterMainsTemperature found in the year
        # TODO remove once this EnergyPlus issue is closed: https://github.com/NREL/EnergyPlus/issues/9650
        water_temp = model.getSiteWaterMainsTemperature
        mean_c = water_temp.annualAverageOutdoorAirTemperature.get
        max_delta_c = water_temp.maximumDifferenceInMonthlyAverageOutdoorAirTemperatures.get
        max_temp_c = mean_c + (0.5 * max_delta_c) + 3.0 # To ensure higher than mains temp
        max_temp_c = max_temp_c.round(1)
        max_temp_f = OpenStudio.convert(max_temp_c, 'C', 'F').get
        max_temp_f = max_temp_f.round(1)
        pipe_indoor_temp_sch = OpenStudio::Model::ScheduleConstant.new(model)
        pipe_indoor_temp_sch.setName("Temporary Pipe Indoor Ambient Temp #{max_temp_f}F")
        pipe_indoor_temp_sch.setValue(max_temp_c)
        model.getPipeIndoors.each do |heat_loss_pipe|
          # @todo schedule type registry error for this setter
          # heat_loss_pipe.setAmbientTemperatureSchedule(pipe_indoor_temp_sch)
          heat_loss_pipe.setPointer(7, pipe_indoor_temp_sch.handle)
        end
        runner.registerWarning("Set Pipe:Indoor ambient temp schedules to #{max_temp_f}F to avoid E+ issue 9650, remove once fixed.")
      end

      # add_daylighting_controls
      if add_daylighting_controls
        # remove add_daylighting_controls objects
        if remove_objects
          model.getDaylightingControls.each(&:remove)
        end

        # add daylight controls, need to perform a sizing run for 2010
        if (template == '90.1-2010' || template == 'ComStock 90.1-2010') && (standard.model_run_sizing_run(model, "#{sizing_run_directory}/SRvt") == false)
          log_messages_to_runner(runner, debug = true)
          return false
        end

        standard.model_add_daylighting_controls(model)
      end

      # add refrigeration
      if add_refrigeration

        # remove refrigeration equipment
        if remove_objects
          model.getRefrigerationSystems.each(&:remove)
          model.getRefrigerationCases.each(&:remove)
          model.getRefrigerationWalkIns.each(&:remove)
          model.getRefrigerationCompressorRacks.each(&:remove)
          model.getRefrigerationCompressors.each(&:remove)
        end

        # Add refrigerated cases and walkins
        OpenstudioStandards::Refrigeration.create_typical_refrigeration(model, template: refrigeration_template)
      end

      # @todo add slab modeling and slab insulation
      # @todo fuel customization for cooking and laundry
      # works by switching some fraction of electric loads to gas if requested (assuming base load is electric)

      # add thermostats
      if add_thermostat

        # remove thermostats
        if remove_objects
          model.getThermostatSetpointDualSetpoints.each(&:remove)
        end

        model.getSpaceTypes.sort.each do |space_type|
          # create thermostat schedules
          # skip un-recognized space types
          next if standard.space_type_get_standards_data(space_type).empty?

          # the last bool test it to make thermostat schedules. They are added to the model but not assigned
          standard.space_type_apply_internal_load_schedules(space_type, false, false, false, false, false, true)

          # identify thermal thermostat and apply to zones (apply_internal_load_schedules names )
          model.getThermostatSetpointDualSetpoints.sort.each do |thermostat|
            next if thermostat.name.to_s != "#{space_type.name} Thermostat"
            next if !thermostat.coolingSetpointTemperatureSchedule.is_initialized
            next if !thermostat.heatingSetpointTemperatureSchedule.is_initialized

            runner.registerInfo("Assigning #{thermostat.name} to thermal zones with #{space_type.name} assigned.")
            space_type.spaces.sort.each do |space|
              next if !space.thermalZone.is_initialized

              space.thermalZone.get.setThermostatSetpointDualSetpoint(thermostat)
            end
          end
        end
      end

      # add internal mass
      if add_internal_mass

        if remove_objects
          model.getSpaceLoads.sort.each do |instance|
            next unless instance.to_InternalMass.is_initialized

            instance.remove
          end
        end

        # add internal mass to conditioned spaces; needs to happen after thermostats are applied
        standard.model_add_internal_mass(model, primary_bldg_type)
      end

      # add hvac system
      if add_hvac

        # remove HVAC objects
        if remove_objects
          standard.model_remove_prm_hvac(model)
        end

        # If user does not map HVAC types to zones with a JSON file, run conventional approach to HVAC assignment
        if user_hvac_mapping.nil?
          case hvac_system_type
          when 'Inferred'

            # Get the hvac delivery type enum
            hvac_delivery = case hvac_delivery_type
                            when 'Forced Air'
                              'air'
                            when 'Hydronic'
                              'hydronic'
                            end

            # Group the zones by occupancy type.  Only split out non-dominant groups if their total area exceeds the limit.
            min_area_m2 = OpenStudio.convert(20_000, 'ft^2', 'm^2').get
            sys_groups = OpenstudioStandards::Geometry.model_group_thermal_zones_by_occupancy_type(model, min_area_m2: min_area_m2)

            # For each group, infer the HVAC system type.
            sys_groups.each do |sys_group|
              # Infer the primary system type
              sys_type, central_htg_fuel, zone_htg_fuel, clg_fuel = standard.model_typical_hvac_system_type(model,
                                                                                                            climate_zone,
                                                                                                            sys_group['type'],
                                                                                                            hvac_delivery,
                                                                                                            heating_fuel,
                                                                                                            cooling_fuel,
                                                                                                            OpenStudio.convert(sys_group['area_ft2'], 'ft^2', 'm^2').get,
                                                                                                            sys_group['stories'])

              # Infer the secondary system type for multizone systems
              sec_sys_type = case sys_type
                             when 'PVAV Reheat', 'VAV Reheat'
                               'PSZ-AC'
                             when 'PVAV PFP Boxes', 'VAV PFP Boxes'
                               'PSZ-HP'
                             else
                               sys_type # same as primary system type
                             end

              # group zones
              story_zone_lists = OpenstudioStandards::Geometry.model_group_thermal_zones_by_building_story(model, sys_group['zones'])

              # On each story, add the primary system to the primary zones
              # and add the secondary system to any zones that are different.
              story_zone_lists.each do |story_group|
                # Differentiate primary and secondary zones, based on
                # operating hours and internal loads (same as 90.1 PRM)
                pri_sec_zone_lists = standard.model_differentiate_primary_secondary_thermal_zones(model, story_group)
                system_zones = pri_sec_zone_lists['primary']

                # if the primary system type is PTAC, filter to cooled zones to prevent sizing error if no cooling
                if sys_type == 'PTAC'
                  heated_and_cooled_zones = system_zones.select { |zone| OpenstudioStandards::ThermalZone.thermal_zone_heated?(zone) && OpenstudioStandards::ThermalZone.thermal_zone_cooled?(zone) }
                  cooled_only_zones = system_zones.select { |zone| !OpenstudioStandards::ThermalZone.thermal_zone_heated?(zone) && OpenstudioStandards::ThermalZone.thermal_zone_cooled?(zone) }
                  system_zones = heated_and_cooled_zones + cooled_only_zones
                end

                # Add the primary system to the primary zones
                unless standard.model_add_hvac_system(model, sys_type, central_htg_fuel, zone_htg_fuel, clg_fuel, system_zones)
                  runner.registerError("HVAC system type '#{sys_type}' not recognized. Check input system type argument against Model.hvac.rb for valid hvac system type names.")
                  return false
                end

                # Add the secondary system to the secondary zones (if any)
                if !pri_sec_zone_lists['secondary'].empty?
                  system_zones = pri_sec_zone_lists['secondary']
                  if (sec_sys_type == 'PTAC') || (sec_sys_type == 'PSZ-AC')
                    heated_and_cooled_zones = system_zones.select { |zone| OpenstudioStandards::ThermalZone.thermal_zone_heated?(zone) && OpenstudioStandards::ThermalZone.thermal_zone_cooled?(zone) }
                    cooled_only_zones = system_zones.select { |zone| !OpenstudioStandards::ThermalZone.thermal_zone_heated?(zone) && OpenstudioStandards::ThermalZone.thermal_zone_cooled?(zone) }
                    system_zones = heated_and_cooled_zones + cooled_only_zones
                  end
                  unless standard.model_add_hvac_system(model, sec_sys_type, central_htg_fuel, zone_htg_fuel, clg_fuel, system_zones)
                    runner.registerError("HVAC system type '#{sys_type}' not recognized. Check input system type argument against Model.hvac.rb for valid hvac system type names.")
                    return false
                  end
                end
              end
            end

          else
            # HVAC system_type specified
            # Group the zones by occupancy type.  Only split out non-dominant groups if their total area exceeds the limit.
            min_area_m2 = OpenStudio.convert(20_000, 'ft^2', 'm^2').get
            sys_groups = OpenstudioStandards::Geometry.model_group_thermal_zones_by_occupancy_type(model, min_area_m2: min_area_m2)
            sys_groups.each do |sys_group|
              # group zones
              story_zone_groups = OpenstudioStandards::Geometry.model_group_thermal_zones_by_building_story(model, sys_group['zones'])

              # Add the user specified HVAC system for each story.
              # Single-zone systems will get one per zone.
              story_zone_groups.each do |zones|
                unless OpenstudioStandards::HVAC.add_cbecs_hvac_system(model, standard, hvac_system_type, zones)
                  runner.registerError("HVAC system type '#{hvac_system_type}' not recognized. Check input system type argument against cbecs_hvac.rb in the HVAC module for valid HVAC system type names.")
                  return false
                end
                # openstudio-standards PR #1791 set zone_equipment_ventilation: false for all
                # PTAC/PTHP cases in add_cbecs_hvac_system (zeros out OA flow rates).
                # For new-construction models, PTACs should ventilate through the unit.
                # Re-enable by autosizing the three OA flow rate fields on each PTAC/PTHP.
                if hvac_system_type.start_with?('PTAC', 'PTHP')
                  reenable_ptac_pthp_outdoor_air(zones, runner)
                end
              end
            end
          end
        else
          # If user specified a mapping of HVAC systems to zones
          user_hvac_mapping['systems'].each do |system_hash|
            hvac_system_type = system_hash['system_type']
            zone_names = system_hash['thermal_zones']

            # Get OS:ThermalZone objects
            zones = zone_names.map do |zone_name|
              model.getThermalZoneByName(zone_name).get
            end

            puts "Adding #{hvac_system_type} to #{zone_names.join(', ')}"

            unless OpenstudioStandards::HVAC.add_cbecs_hvac_system(model, standard, hvac_system_type, zones)
              runner.registerError("HVAC system type '#{hvac_system_type}' not recognized. Check input system type argument against cbecs_hvac.rb in the HVAC module for valid HVAC system type names.")
              return false
            end
            # Re-enable outdoor air on PTAC/PTHP (disabled by openstudio-standards PR #1791)
            if hvac_system_type.start_with?('PTAC', 'PTHP')
              reenable_ptac_pthp_outdoor_air(zones, runner)
            end
          end
        end
      end

      # hours of operation
      if modify_wkdy_op_hrs || modify_wknd_op_hrs
        # Infer the current hours of operation schedule for the building
        op_sch = OpenstudioStandards::Schedules.model_infer_hours_of_operation_building(model)

        # Convert existing schedules in the model to parametric schedules based on current hours of operation
        runner.registerInfo("Generating parametric schedules from ruleset schedules using #{hoo_var_method} variable method for hours of operation formula.")
        OpenstudioStandards::Schedules.model_setup_parametric_schedules(model, hoo_var_method: hoo_var_method)

        # Create start and end times from start time and duration supplied
        wkdy_start_time = nil
        wkdy_end_time = nil
        wknd_start_time = nil
        wknd_end_time = nil
        # weekdays
        if modify_wkdy_op_hrs
          wkdy_start_time = OpenStudio::Time.new(0, wkdy_op_hrs_start_time_hr, wkdy_op_hrs_start_time_min, 0)
          wkdy_end_time = wkdy_start_time + OpenStudio::Time.new(0, wkdy_op_hrs_duration_hr, wkdy_op_hrs_duration_min, 0)
        end
        # weekends
        if modify_wknd_op_hrs
          wknd_start_time = OpenStudio::Time.new(0, wknd_op_hrs_start_time_hr, wknd_op_hrs_start_time_min, 0)
          wknd_end_time = wknd_start_time + OpenStudio::Time.new(0, wknd_op_hrs_duration_hr, wknd_op_hrs_duration_min, 0)
        end

        # Modify hours of operation, using weekdays values for all weekdays and weekend values for Saturday and Sunday
        OpenstudioStandards::Schedules.schedule_ruleset_set_hours_of_operation(op_sch,
                                                                               wkdy_start_time: wkdy_start_time,
                                                                               wkdy_end_time: wkdy_end_time,
                                                                               sat_start_time: wknd_start_time,
                                                                               sat_end_time: wknd_end_time,
                                                                               sun_start_time: wknd_start_time,
                                                                               sun_end_time: wknd_end_time)

        # Apply new operating hours to parametric schedules to make schedules in model reflect modified hours of operation
        parametric_schedules = OpenstudioStandards::Schedules.model_apply_parametric_schedules(model, error_on_out_of_order: false)
        runner.registerInfo("Updated #{parametric_schedules.size} schedules with new hours of operation.")
      end

      # add_exhaust
      if add_exhaust

        # remove exhaust objects
        if remove_objects
          model.getFanZoneExhausts.each(&:remove)
        end

        method_str = "model_add_exhaust"
        source_loc = standard.method(method_str.to_sym).source_location
        if template == '179D 90.1-2007'
          raise "#{kitchen_makeup}, #{method_str} location: #{source_loc.join(':')}" unless source_loc[0].include?('179d_ashrae_90_1_2007.Model.rb')
        end
        zone_exhaust_fans = standard.model_add_exhaust(model, makeup_source: kitchen_makeup)
        zone_exhaust_fans.each do |zone_exhaust_fan|
          max_flow_rate_ip = OpenStudio.convert(zone_exhaust_fan.maximumFlowRate.get, 'm^3/s', 'cfm').get
          runner.registerInfo("Adding #{OpenStudio.toNeatString(max_flow_rate_ip, 0, true)} (cfm) of exhaust to #{zone_exhaust_fan.thermalZone.get.name}")
        end
      end

      # OpenstudioStandards::Infiltration.model_set_nist_infiltration_schedules will remove the Makeup infil we added!
      if add_infiltration
        spi_infos = model.getSpaceInfiltrationDesignFlowRates.map { |spi| {'name':  spi.nameString, 'space': spi.space.get, 'schedule': spi.schedule.get, 'flow_rate': spi.designFlowRate.get } }
      end

      # set hvac controls and efficiencies (this should be last model articulation element)
      if add_hvac
        # set additional properties for building
        props = model.getBuilding.additionalProperties
        props.setFeature('hvac_system_type', hvac_system_type)

        case hvac_system_type
        when 'Ideal Air Loads'

        else
          # Set the heating and cooling sizing parameters
          standard.model_apply_prm_sizing_parameters(model)

          # Perform a sizing run
          if standard.model_run_sizing_run(model, "#{sizing_run_directory}/SR1") == false
            log_messages_to_runner(runner, debug = true)
            return false
          end

          # If there are any multizone systems, reset damper positions
          # to achieve a 60% ventilation effectiveness minimum for the system
          # following the ventilation rate procedure from 62.1
          standard.model_apply_multizone_vav_outdoor_air_sizing(model)

          # Apply the prototype HVAC assumptions
          standard.model_apply_prototype_hvac_assumptions(model, primary_bldg_type, climate_zone)

          # Apply the HVAC efficiency standard
          # NOTE: 179D override
          if template == '179D 90.1-2007'
            standard.model_apply_hvac_efficiency_standard(model, climate_zone, baseline_179d = false)
          else
            standard.model_apply_hvac_efficiency_standard(model, climate_zone)
          end
        end

        # adjust infiltration schedules
        if add_infiltration
          OpenstudioStandards::Infiltration.model_set_nist_infiltration_schedules(model)
        end
      end

      if add_infiltration
        # Recreate the Makeup infil
        spi_infos.each do |spi_info|
          spi = OpenStudio::Model::SpaceInfiltrationDesignFlowRate.new(model)
          spi.setName(spi_info[:name])
          spi.setSpace(spi_info[:space])
          spi.setSchedule(spi_info[:schedule])
          spi.setDesignFlowRate(spi_info[:flow_rate])
        end
      end

      # set unmet hours tolerance
      unmet_hrs_tol_k = OpenStudio.convert(unmet_hours_tolerance_r, 'R', 'K').get
      tolerances = model.getOutputControlReportingTolerances
      tolerances.setToleranceforTimeHeatingSetpointNotMet(unmet_hrs_tol_k)
      tolerances.setToleranceforTimeCoolingSetpointNotMet(unmet_hrs_tol_k)

      # remove everything but spaces, zones, and stub space types (extend as needed for additional objects, may make bool arg for this)
      if remove_objects
        model.purgeUnusedResourceObjects
        objects_after_cleanup = initial_object_size - model.getModelObjects.size
        if objects_after_cleanup > 0
          runner.registerInfo("Removing #{objects_after_cleanup} objects from model")
        end
      end

      # change night cycling control to "Thermostat" cycling and increase thermostat tolerance to 1.99999
      # @todo: disabled for now, I expect it would change EUI
      if false
        manager_night_cycles = model.getAvailabilityManagerNightCycles
        runner.registerInfo("Changing thermostat tolerance to 1.99999 for #{manager_night_cycles.size} night cycle manager objects.")
        manager_night_cycles.each do |night_cycle|
          night_cycle.setThermostatTolerance(1.9999)
          night_cycle.setCyclingRunTimeControlType('Thermostat')
        end
      end

      # disable HVAC Sizing Simulation for Sizing Periods, not used for the type of PlantLoop sizing used in ComStock
      if model.version >= OpenStudio::VersionString.new('3.0.0')
        sim_control = model.getSimulationControl
        sim_control.setDoHVACSizingSimulationforSizingPeriodsNoFail(false)
      end

      # report final condition of model
      runner.registerFinalCondition("The building finished with #{model.getModelObjects.size} objects.")

      # log messages to info messages
      log_messages_to_runner(runner, debug = false)

      return true
    end
end
