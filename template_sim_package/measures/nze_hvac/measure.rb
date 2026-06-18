# frozen_string_literal: true

# *******************************************************************************
# OpenStudio(R), Copyright (c) Alliance for Sustainable Energy, LLC.
# See also https://openstudio.net/license
# *******************************************************************************
require 'openstudio-standards'

# Drop the auto-defaulted 'condenser_type' from the chiller search criteria so
# entries in chillers.json whose condenser_type is null (e.g. NREL ZNE Ready 2017)
# can match; otherwise the lookup silently fails and SDK default curves/COP stay.
# Applies to every chiller-containing HVAC choice in this measure: for AirCooled
# chillers openstudio-standards injects condenser_type='WithCondenser' (which this
# patch strips); for WaterCooled chillers condenser_type is never added to the
# criteria upstream, so the delete is a safe no-op and lookups still disambiguate
# by compressor_type.
#
# NOTE: implemented with Module#prepend (not `alias` + redef) because the
# OpenStudio measure tester used in CI (`openstudio measure -r ...`) loads this
# file more than once. A second load of an `alias`-based patch would re-point
# the saved original at the already-patched method and the new `def` would then
# recurse into itself => SystemStackError / LocalJumpError. `Module#prepend`
# with the same module is a no-op on subsequent loads, so this is safe.
module ChillerSearchCriteriaDropCondenserTypePatch
  def chiller_electric_eir_find_search_criteria(chiller_electric_eir)
    criteria = super
    criteria.delete('condenser_type')
    criteria
  end
end
Standard.prepend(ChillerSearchCriteriaDropCondenserTypePatch) unless Standard.include?(ChillerSearchCriteriaDropCondenserTypePatch)

class NetZeroEnergyHvac < OpenStudio::Measure::ModelMeasure
  def name
    return 'net_zero_energy_hvac'
  end

  # human readable description
  def description
    return 'This measure replaces the existing HVAC system if any with the user selected HVAC system.  The user can select how to partition the system, applying it to the whole building, a system per building type, a system per building story, or automatically partition based on residential/non-residential occupany types and space loads.'
  end

  # human readable description of modeling approach
  def modeler_description
    return 'HVAC system creation logic uses [openstudio-standards](https://github.com/NREL/openstudio-standards) and efficiency values are defined in the openstudio-standards Standards spreadsheet under the *NREL ZNE Ready 2017* template.'
  end

  # -------------------------------------------------------------
  # methods for reverting back to OS 3.6.1
  # -------------------------------------------------------------
  # Determine if the space is a plenum.
  # Assume it is a plenum if it is a supply or return plenum for an AirLoop,
  # if it is not part of the total floor area,
  # or if the space type name contains the word plenum.
  #
  # @param space [OpenStudio::Model::Space] space object
  # return [Bool] returns true if plenum, false if not
  def space_plenum?(space)
    plenum_status = false

    # Check if it is designated
    # as not part of the building
    # floor area.  This method internally
    # also checks to see if the space's zone
    # is a supply or return plenum
    unless space.partofTotalFloorArea
      plenum_status = true
      return plenum_status
    end

    # @todo update to check if it has internal loads

    # Check if the space type name
    # contains the word plenum.
    space_type = space.spaceType
    if space_type.is_initialized
      space_type = space_type.get
      if space_type.name.get.to_s.downcase.include?('plenum')
        plenum_status = true
        return plenum_status
      end
      if space_type.standardsSpaceType.is_initialized && space_type.standardsSpaceType.get.downcase.include?('plenum')
        plenum_status = true
        return plenum_status
      end
    end

    return plenum_status
  end

  # Returns the min and max value for this schedule.
  # It doesn't evaluate design days only run-period conditions
  #
  # @author David Goldwasser, NREL.
  # @param schedule_ruleset [OpenStudio::Model::ScheduleRuleset] schedule ruleset object
  # @return [Hash] Hash has two keys, min and max.
  def schedule_ruleset_annual_min_max_value(schedule_ruleset)
    # gather profiles
    profiles = []
    profiles << schedule_ruleset.defaultDaySchedule
    rules = schedule_ruleset.scheduleRules
    rules.each do |rule|
      profiles << rule.daySchedule
    end

    # test profiles
    min = nil
    max = nil
    profiles.each do |profile|
      profile.values.each do |value| # rubocop:disable Style/HashEachMethods
        if min.nil?
          min = value
        else
          min = value if min > value
        end
        if max.nil?
          max = value
        else
          max = value if max < value
        end
      end
    end
    result = { 'min' => min, 'max' => max }

    return result
  end

  # Determine if the thermal zone is a plenum based on whether a majority of the spaces in the zone are plenums or not.
  #
  # @param thermal_zone [OpenStudio::Model::ThermalZone] thermal zone
  # @return [Bool] returns true if majority plenum, false if not
  def thermal_zone_plenum?(thermal_zone)
    plenum_status = false

    area_plenum = 0
    area_non_plenum = 0
    thermal_zone.spaces.each do |space|
      if space_plenum?(space)
        area_plenum += space.floorArea
      else
        area_non_plenum += space.floorArea
      end
    end

    # Majority
    if area_plenum > area_non_plenum
      plenum_status = true
    end

    return plenum_status
  end

  # Determines heating status.
  # If the zone has a thermostat with a maximum heating setpoint above 5C (41F), counts as heated.
  # Plenums are also assumed to be heated.
  #
  # @author Andrew Parker, Julien Marrec
  # @param thermal_zone [OpenStudio::Model::ThermalZone] thermal zone
  # @return [Bool] returns true if heated, false if not
  def thermal_zone_heated?(thermal_zone)
    temp_f = 41
    temp_c = OpenStudio.convert(temp_f, 'F', 'C').get

    htd = false

    # Consider plenum zones heated
    area_plenum = 0
    area_non_plenum = 0
    thermal_zone.spaces.each do |space|
      if space_plenum?(space)
        area_plenum += space.floorArea
      else
        area_non_plenum += space.floorArea
      end
    end

    # Majority
    if area_plenum > area_non_plenum
      htd = true
      return htd
    end

    # Check if the zone has radiant heating,
    # and if it does, get heating setpoint schedule
    # directly from the radiant system to check.
    thermal_zone.equipment.each do |equip|
      htg_sch = nil
      if equip.to_ZoneHVACHighTemperatureRadiant.is_initialized
        equip = equip.to_ZoneHVACHighTemperatureRadiant.get
        if equip.heatingSetpointTemperatureSchedule.is_initialized
          htg_sch = equip.heatingSetpointTemperatureSchedule.get
        end
      elsif equip.to_ZoneHVACLowTemperatureRadiantElectric.is_initialized
        equip = equip.to_ZoneHVACLowTemperatureRadiantElectric.get
        htg_sch = equip.heatingSetpointTemperatureSchedule
      elsif equip.to_ZoneHVACLowTempRadiantConstFlow.is_initialized
        equip = equip.to_ZoneHVACLowTempRadiantConstFlow.get
        htg_coil = equip.heatingCoil
        if htg_coil.to_CoilHeatingLowTempRadiantConstFlow.is_initialized
          htg_coil = htg_coil.to_CoilHeatingLowTempRadiantConstFlow.get
          if htg_coil.heatingHighControlTemperatureSchedule.is_initialized
            htg_sch = htg_coil.heatingHighControlTemperatureSchedule.get
          end
        end
      elsif equip.to_ZoneHVACLowTempRadiantVarFlow.is_initialized
        equip = equip.to_ZoneHVACLowTempRadiantVarFlow.get
        htg_coil = equip.heatingCoil
        if equip.model.version > OpenStudio::VersionString.new('3.1.0')
          if htg_coil.is_initialized
            htg_coil = htg_coil.get
          else
            htg_coil = nil
          end
        end
        if !htg_coil.nil? && htg_coil.to_CoilHeatingLowTempRadiantVarFlow.is_initialized
          htg_coil = htg_coil.to_CoilHeatingLowTempRadiantVarFlow.get
          if htg_coil.heatingControlTemperatureSchedule.is_initialized
            htg_sch = htg_coil.heatingControlTemperatureSchedule.get
          end
        end
      end
      # Move on if no heating schedule was found
      next if htg_sch.nil?

      # Get the setpoint from the schedule
      if htg_sch.to_ScheduleRuleset.is_initialized
        htg_sch = htg_sch.to_ScheduleRuleset.get
        max_c = schedule_ruleset_annual_min_max_value(htg_sch)['max']
        if max_c > temp_c
          htd = true
        end
      elsif htg_sch.to_ScheduleConstant.is_initialized
        htg_sch = htg_sch.to_ScheduleConstant.get
        max_c = schedule_constant_annual_min_max_value(htg_sch)['max']
        if max_c > temp_c
          htd = true
        end
      elsif htg_sch.to_ScheduleCompact.is_initialized
        htg_sch = htg_sch.to_ScheduleCompact.get
        max_c = schedule_compact_annual_min_max_value(htg_sch)['max']
        if max_c > temp_c
          htd = true
        end
      else
        OpenStudio.logFree(OpenStudio::Debug, 'openstudio.Standards.ThermalZone', "Zone #{thermal_zone.name} used an unknown schedule type for the heating setpoint; assuming heated.")
        htd = true
      end
    end

    # Unheated if no thermostat present
    if thermal_zone.thermostat.empty?
      return htd
    end

    # Check the heating setpoint
    tstat = thermal_zone.thermostat.get
    if tstat.to_ThermostatSetpointDualSetpoint
      tstat = tstat.to_ThermostatSetpointDualSetpoint.get
      htg_sch = tstat.getHeatingSchedule
      if htg_sch.is_initialized
        htg_sch = htg_sch.get
        if htg_sch.to_ScheduleRuleset.is_initialized
          htg_sch = htg_sch.to_ScheduleRuleset.get
          max_c = schedule_ruleset_annual_min_max_value(htg_sch)['max']
          if max_c > temp_c
            htd = true
          end
        elsif htg_sch.to_ScheduleConstant.is_initialized
          htg_sch = htg_sch.to_ScheduleConstant.get
          max_c = schedule_constant_annual_min_max_value(htg_sch)['max']
          if max_c > temp_c
            htd = true
          end
        elsif htg_sch.to_ScheduleCompact.is_initialized
          htg_sch = htg_sch.to_ScheduleCompact.get
          max_c = schedule_compact_annual_min_max_value(htg_sch)['max']
          if max_c > temp_c
            htd = true
          end
        else
          OpenStudio.logFree(OpenStudio::Debug, 'openstudio.Standards.ThermalZone', "Zone #{thermal_zone.name} used an unknown schedule type for the heating setpoint; assuming heated.")
          htd = true
        end
      end
    elsif tstat.to_ZoneControlThermostatStagedDualSetpoint
      tstat = tstat.to_ZoneControlThermostatStagedDualSetpoint.get
      htg_sch = tstat.heatingTemperatureSetpointSchedule
      if htg_sch.is_initialized
        htg_sch = htg_sch.get
        if htg_sch.to_ScheduleRuleset.is_initialized
          htg_sch = htg_sch.to_ScheduleRuleset.get
          max_c = schedule_ruleset_annual_min_max_value(htg_sch)['max']
          if max_c > temp_c
            htd = true
          end
        end
      end
    end

    return htd
  end

  # Determines cooling status.
  # If the zone has a thermostat with a minimum cooling setpoint below 33C (91F), counts as cooled.
  # Plenums are also assumed to be cooled.
  #
  # @author Andrew Parker, Julien Marrec
  # @param thermal_zone [OpenStudio::Model::ThermalZone] thermal zone
  # @return [Bool] returns true if cooled, false if not
  def thermal_zone_cooled?(thermal_zone)
    temp_f = 91
    temp_c = OpenStudio.convert(temp_f, 'F', 'C').get

    cld = false

    # Consider plenum zones cooled
    area_plenum = 0
    area_non_plenum = 0
    thermal_zone.spaces.each do |space|
      if space_plenum?(space)
        area_plenum += space.floorArea
      else
        area_non_plenum += space.floorArea
      end
    end

    # Majority
    if area_plenum > area_non_plenum
      cld = true
      return cld
    end

    # Check if the zone has radiant cooling,
    # and if it does, get cooling setpoint schedule
    # directly from the radiant system to check.
    thermal_zone.equipment.each do |equip|
      clg_sch = nil
      if equip.to_ZoneHVACLowTempRadiantConstFlow.is_initialized
        equip = equip.to_ZoneHVACLowTempRadiantConstFlow.get
        clg_coil = equip.coolingCoil
        if clg_coil.to_CoilCoolingLowTempRadiantConstFlow.is_initialized
          clg_coil = clg_coil.to_CoilCoolingLowTempRadiantConstFlow.get
          if clg_coil.coolingLowControlTemperatureSchedule.is_initialized
            clg_sch = clg_coil.coolingLowControlTemperatureSchedule.get
          end
        end
      elsif equip.to_ZoneHVACLowTempRadiantVarFlow.is_initialized
        equip = equip.to_ZoneHVACLowTempRadiantVarFlow.get
        clg_coil = equip.coolingCoil
        if equip.model.version > OpenStudio::VersionString.new('3.1.0')
          if clg_coil.is_initialized
            clg_coil = clg_coil.get
          else
            clg_coil = nil
          end
        end
        if !clg_coil.nil? && clg_coil.to_CoilCoolingLowTempRadiantVarFlow.is_initialized
          clg_coil = clg_coil.to_CoilCoolingLowTempRadiantVarFlow.get
          if clg_coil.coolingControlTemperatureSchedule.is_initialized
            clg_sch = clg_coil.coolingControlTemperatureSchedule.get
          end
        end
      end
      # Move on if no cooling schedule was found
      next if clg_sch.nil?

      # Get the setpoint from the schedule
      if clg_sch.to_ScheduleRuleset.is_initialized
        clg_sch = clg_sch.to_ScheduleRuleset.get
        min_c = schedule_ruleset_annual_min_max_value(clg_sch)['min']
        if min_c < temp_c
          cld = true
        end
      elsif clg_sch.to_ScheduleConstant.is_initialized
        clg_sch = clg_sch.to_ScheduleConstant.get
        min_c = schedule_constant_annual_min_max_value(clg_sch)['min']
        if min_c < temp_c
          cld = true
        end
      elsif clg_sch.to_ScheduleCompact.is_initialized
        clg_sch = clg_sch.to_ScheduleCompact.get
        min_c = schedule_compact_annual_min_max_value(clg_sch)['min']
        if min_c < temp_c
          cld = true
        end
      else
        OpenStudio.logFree(OpenStudio::Debug, 'openstudio.Standards.ThermalZone', "Zone #{thermal_zone.name} used an unknown schedule type for the cooling setpoint; assuming cooled.")
        cld = true
      end
    end

    # Unheated if no thermostat present
    if thermal_zone.thermostat.empty?
      return cld
    end

    # Check the cooling setpoint
    tstat = thermal_zone.thermostat.get
    if tstat.to_ThermostatSetpointDualSetpoint
      tstat = tstat.to_ThermostatSetpointDualSetpoint.get
      clg_sch = tstat.getCoolingSchedule
      if clg_sch.is_initialized
        clg_sch = clg_sch.get
        if clg_sch.to_ScheduleRuleset.is_initialized
          clg_sch = clg_sch.to_ScheduleRuleset.get
          min_c = schedule_ruleset_annual_min_max_value(clg_sch)['min']
          if min_c < temp_c
            cld = true
          end
        elsif clg_sch.to_ScheduleConstant.is_initialized
          clg_sch = clg_sch.to_ScheduleConstant.get
          min_c = schedule_constant_annual_min_max_value(clg_sch)['min']
          if min_c < temp_c
            cld = true
          end
        elsif clg_sch.to_ScheduleCompact.is_initialized
          clg_sch = clg_sch.to_ScheduleCompact.get
          min_c = schedule_compact_annual_min_max_value(clg_sch)['min']
          if min_c < temp_c
            cld = true
          end
        else
          OpenStudio.logFree(OpenStudio::Debug, 'openstudio.Standards.ThermalZone', "Zone #{thermal_zone.name} used an unknown schedule type for the cooling setpoint; assuming cooled.")
          cld = true
        end
      end
    elsif tstat.to_ZoneControlThermostatStagedDualSetpoint
      tstat = tstat.to_ZoneControlThermostatStagedDualSetpoint.get
      clg_sch = tstat.coolingTemperatureSetpointSchedule
      if clg_sch.is_initialized
        clg_sch = clg_sch.get
        if clg_sch.to_ScheduleRuleset.is_initialized
          clg_sch = clg_sch.to_ScheduleRuleset.get
          min_c = schedule_ruleset_annual_min_max_value(clg_sch)['min']
          if min_c < temp_c
            cld = true
          end
        end
      end
    elsif tstat.to_ThermostatSetpointSingleHeating
      cld = false
    end

    return cld
  end

  # Checks all spaces on this story that are part of the total floor area to see if they have the same multiplier.
  # If they do, assume that the multipliers are being used as a floor multiplier.
  #
  # @param building_story [OpenStudio::Model::BuildingStory] OpenStudio BuildingStory object
  # @return [Integer] return the floor multiplier for this story, returning 1 if no floor multiplier.
  def building_story_get_floor_multiplier(building_story)
    floor_multiplier = 1

    # Determine the multipliers for all spaces
    multipliers = []
    building_story.spaces.each do |space|
      # Ignore spaces that aren't part of the total floor area
      next unless space.partofTotalFloorArea

      multipliers << space.multiplier
    end

    # If there are no spaces on this story, assume
    # a multiplier of 1
    if multipliers.empty?
      return floor_multiplier
    end

    # Calculate the average multiplier and
    # then convert to integer.
    avg_multiplier = (multipliers.sum.to_f / multipliers.size).to_i

    # If the multiplier is greater than 1, report this
    if avg_multiplier > 1
      floor_multiplier = avg_multiplier
      OpenStudio.logFree(OpenStudio::Info, 'openstudio.standards.Geometry.Information', "Story #{building_story.name} has a multiplier of #{floor_multiplier}.")
    end

    return floor_multiplier
  end

  # Determine the number of stories spanned by the supplied thermal zones.
  # If all zones on one of the stories have an identical multiplier,
  # assume that the multiplier is a floor multiplier and increase the number of stories accordingly.
  # Stories do not have to be contiguous.
  #
  # @param thermal_zones [Array<OpenStudio::Model::ThermalZone>] An array of OpenStudio ThermalZone objects
  # @return [Integer] The number of stories spanned by the thermal zones
  def thermal_zones_get_number_of_stories_spanned(thermal_zones)
    # Get the story object for all zones
    stories = []
    thermal_zones.each do |zone|
      zone.spaces.each do |space|
        story = space.buildingStory
        next if story.empty?

        stories << story.get
      end
    end

    # Reduce down to the unique set of stories
    stories = stories.uniq

    # Tally up stories including multipliers
    num_stories = 0
    stories.each do |story|
      num_stories += building_story_get_floor_multiplier(story)
    end

    return num_stories
  end

  # Categorize zones by occupancy type and fuel type, where the types depend on the standard.
  #
  # @param model [OpenStudio::Model::Model] OpenStudio model object
  # @param custom [String] custom fuel type
  # @param applicable_zones [list of zone objects]
  # @return [Array<Hash>] an array of hashes, one for each zone,
  #   with the keys 'zone', 'type' (occ type), 'fuel', and 'area'
  def model_zones_with_occ_and_fuel_type(model, _custom, applicable_zones = nil)
    zones = []

    model.getThermalZones.sort.each do |zone|
      # Skip plenums
      if thermal_zone_plenum?(zone)
        OpenStudio.logFree(OpenStudio::Info, 'openstudio.standards.Model', "Zone #{zone.name} is a plenum.  It will not be assigned a baseline system.")
        next
      end

      # This is only used for the stable baseline (2016 and later)
      if !applicable_zones.nil? && !applicable_zones.include?(zone)
        # This zone is not part of the current hvac_building_type
        next
      end

      # Skip unconditioned zones
      heated = thermal_zone_heated?(zone)
      cooled = thermal_zone_cooled?(zone)
      if !heated && !cooled
        OpenStudio.logFree(OpenStudio::Info, 'openstudio.standards.Model', "Zone #{zone.name} is unconditioned.  It will not be assigned a baseline system.")
        next
      end

      zn_hash = {}

      # The zone object
      zn_hash['zone'] = zone

      # Floor area
      zn_hash['area'] = zone.floorArea

      # Occupancy type
      zn_hash['occ'] = thermal_zone_occupancy_type(zone)

      # Building type
      zn_hash['bldg_type'] = thermal_zone_building_type(zone)

      # Fuel type
      # for 2013 and prior, baseline fuel = proposed fuel
      # for 2016 and later, use fuel to identify zones with district energy
      zn_hash['fuel'] = thermal_zone_get_zone_fuels_for_occ_and_fuel_type(zone)

      zones << zn_hash
    end

    return zones
  end

  # Split all zones in the model into groups that are big enough to justify their own HVAC system type.
  # Similar to the logic from 90.1 Appendix G, but without regard to the fuel type of the existing HVAC system (because the model may not have one).
  #
  # @param model [OpenStudio::Model::Model] OpenStudio Model object
  # @param min_area_m2 [Double] the minimum area required to justify a different system type, default 20,000 ft^2
  # @return [Array<Hash>] an array of hashes of area information, with keys area_ft2, type, stories, and zones (an array of zones)
  def model_group_thermal_zones_by_occupancy_type(model, min_area_m2: 1858.0608)
    min_area_ft2 = OpenStudio.convert(min_area_m2, 'm^2', 'ft^2').get

    # Get occupancy type, fuel type, and area information for all zones, excluding unconditioned zones.
    # Occupancy types are:
    # Residential
    # NonResidential
    # Use 90.1-2010 so that retail and publicassembly are not split out
    # std = Standard.build('90.1-2019') # delete once space methods refactored
    zones = model_zones_with_occ_and_fuel_type(model, nil)

    # Ensure that there is at least one conditioned zone
    if zones.empty?
      OpenStudio.logFree(OpenStudio::Error, 'openstudio.prototype.Model', 'The building does not appear to have any conditioned zones. Make sure zones have thermostat with appropriate heating and cooling setpoint schedules.')
      return []
    end

    # Group the zones by occupancy type
    type_to_area = Hash.new { 0.0 }
    zones_grouped_by_occ = zones.group_by { |z| z['occ'] }

    # Determine the dominant occupancy type by area
    zones_grouped_by_occ.each do |occ_type, zns|
      zns.each do |zn|
        type_to_area[occ_type] += zn['area']
      end
    end
    dom_occ = type_to_area.sort_by { |_k, v| v }.reverse[0][0]

    # Get the dominant occupancy type group
    dom_occ_group = zones_grouped_by_occ[dom_occ]

    # Check the non-dominant occupancy type groups to see if they are big enough to trigger the occupancy exception.
    # If they are, leave the group standing alone.
    # If they are not, add the zones in that group back to the dominant occupancy type group.
    occ_groups = []
    zones_grouped_by_occ.each do |occ_type, zns|
      # Skip the dominant occupancy type
      next if occ_type == dom_occ

      # Add up the floor area of the group
      area_m2 = 0
      zns.each do |zn|
        area_m2 += zn['area']
      end
      area_ft2 = OpenStudio.convert(area_m2, 'm^2', 'ft^2').get

      # If the non-dominant group is big enough, preserve that group.
      if area_ft2 > min_area_ft2
        occ_groups << [occ_type, zns]
        OpenStudio.logFree(OpenStudio::Info, 'openstudio.standards.Model', "The portion of the building with an occupancy type of #{occ_type} is bigger than the minimum area of #{min_area_ft2.round} ft2.  It will be assigned a separate HVAC system type.")
        # Otherwise, add the zones back to the dominant group.
      else
        dom_occ_group += zns
      end
    end
    # Add the dominant occupancy group to the list
    occ_groups << [dom_occ, dom_occ_group]

    # Calculate the area for each of the final groups
    # and replace the zone hashes with an array of zone objects
    final_groups = []
    occ_groups.each do |occ_type, zns|
      # Sum the area and put all zones into an array
      area_m2 = 0.0
      gp_zns = []
      zns.each do |zn|
        area_m2 += zn['area']
        gp_zns << zn['zone']
      end
      area_ft2 = OpenStudio.convert(area_m2, 'm^2', 'ft^2').get

      # Determine the number of stories this group spans
      num_stories = thermal_zones_get_number_of_stories_spanned(gp_zns)

      # Create a hash representing this group
      group = {}
      group['area_ft2'] = area_ft2
      group['type'] = occ_type
      group['stories'] = num_stories
      group['zones'] = gp_zns
      final_groups << group

      # Report out the final grouping
      OpenStudio.logFree(OpenStudio::Info, 'openstudio.standards.Model', "Final system type group: occ = #{group['type']}, area = #{group['area_ft2'].round} ft2, num stories = #{group['stories']}, zones:")
      group['zones'].sort.each_slice(5) do |zone_list|
        zone_names = []
        zone_list.each do |zone|
          zone_names << zone.name.get.to_s
        end
        OpenStudio.logFree(OpenStudio::Info, 'openstudio.standards.Model', "--- #{zone_names.join(', ')}")
      end
    end

    return final_groups
  end

  # Group an array of zones into multiple arrays, one for each story in the building.
  # Zones with spaces on multiple stories will be assigned to only one of the stories.
  # Returns an empty array when the story doesn't contain any of the zones.
  #
  # @param model [OpenStudio::Model::Model] OpenStudio Model object
  # @param thermal_zones [Array<OpenStudio::Model::ThermalZone>] An array of OpenStudio ThermalZone objects
  # @return [Array<Array<OpenStudio::Model::ThermalZone>>] An array of arrays of OpenStudio ThermalZone objects
  def model_group_thermal_zones_by_building_story(model, thermal_zones)
    story_zone_lists = []
    zones_already_assigned = []
    model.getBuildingStorys.sort.each do |story|
      # Get all the spaces on this story
      spaces = story.spaces

      # Get all the thermal zones that serve these spaces
      all_zones_on_story = []
      spaces.each do |space|
        if space.thermalZone.is_initialized
          all_zones_on_story << space.thermalZone.get
        else
          OpenStudio.logFree(OpenStudio::Warn, 'openstudio.standards.Model', "Space #{space.name} has no thermal zone, it is not included in the simulation.")
        end
      end

      # Find thermal zones in the list that are on this story
      zones_on_story = []
      thermal_zones.each do |zone|
        if all_zones_on_story.include?(zone)
          # Skip thermal zones that were already assigned to a story.
          # This can happen if a zone has multiple spaces on multiple stories.
          # Stairwells and atriums are typical scenarios.
          next if zones_already_assigned.include?(zone)

          zones_on_story << zone
          zones_already_assigned << zone
        end
      end

      unless zones_on_story.empty?
        story_zone_lists << zones_on_story
      end
    end

    return story_zone_lists
  end

  # Split all zones in the model into groups that are big enough to justify their own HVAC system type.
  # Similar to the logic from 90.1 Appendix G, but without regard to the fuel type of the existing HVAC system (because the model may not have one).
  #
  # @param model [OpenStudio::Model::Model] OpenStudio Model object
  # @param min_area_m2 [Double] the minimum area required to justify a different system type, default 20,000 ft^2
  # @return [Array<Hash>] an array of hashes of area information, with keys area_ft2, type, stories, and zones (an array of zones)
  def model_group_thermal_zones_by_building_type(model, min_area_m2: 1858.0608)
    min_area_ft2 = OpenStudio.convert(min_area_m2, 'm^2', 'ft^2').get

    # Get occupancy type, building type, fuel type, and area information for all zones, excluding unconditioned zones
    # std = Standard.build('90.1-2019') # delete once space methods refactored
    zones = model_zones_with_occ_and_fuel_type(model, nil)

    # Ensure that there is at least one conditioned zone
    if zones.empty?
      OpenStudio.logFree(OpenStudio::Error, 'openstudio.prototype.Model', 'The building does not appear to have any conditioned zones. Make sure zones have thermostat with appropriate heating and cooling setpoint schedules.')
      return []
    end

    # Group the zones by building type
    type_to_area = Hash.new { 0.0 }
    zones_grouped_by_bldg_type = zones.group_by { |z| z['bldg_type'] }

    # Determine the dominant building type by area
    zones_grouped_by_bldg_type.each do |bldg_type, zns|
      zns.each do |zn|
        type_to_area[bldg_type] += zn['area']
      end
    end
    dom_bldg_type = type_to_area.sort_by { |_k, v| v }.reverse[0][0]

    # Get the dominant building type group
    dom_bldg_type_group = zones_grouped_by_bldg_type[dom_bldg_type]

    # Check the non-dominant building type groups to see if they are big enough to trigger the building exception.
    # If they are, leave the group standing alone.
    # If they are not, add the zones in that group back to the dominant building type group.
    bldg_type_groups = []
    zones_grouped_by_bldg_type.each do |bldg_type, zns|
      # Skip the dominant building type
      next if bldg_type == dom_bldg_type

      # Add up the floor area of the group
      area_m2 = 0
      zns.each do |zn|
        area_m2 += zn['area']
      end
      area_ft2 = OpenStudio.convert(area_m2, 'm^2', 'ft^2').get

      # If the non-dominant group is big enough, preserve that group.
      if area_ft2 > min_area_ft2
        bldg_type_groups << [bldg_type, zns]
        OpenStudio.logFree(OpenStudio::Info, 'openstudio.standards.Model', "The portion of the building with a building type of #{bldg_type} is bigger than the minimum area of #{min_area_ft2.round} ft2.  It will be assigned a separate HVAC system type.")
        # Otherwise, add the zones back to the dominant group.
      else
        dom_bldg_type_group += zns
      end
    end
    # Add the dominant building type group to the list
    bldg_type_groups << [dom_bldg_type, dom_bldg_type_group]

    # Calculate the area for each of the final groups
    # and replace the zone hashes with an array of zone objects
    final_groups = []
    bldg_type_groups.each do |bldg_type, zns|
      # Sum the area and put all zones into an array
      area_m2 = 0.0
      gp_zns = []
      zns.each do |zn|
        area_m2 += zn['area']
        gp_zns << zn['zone']
      end
      area_ft2 = OpenStudio.convert(area_m2, 'm^2', 'ft^2').get

      # Determine the number of stories this group spans
      num_stories = thermal_zones_get_number_of_stories_spanned(gp_zns)

      # Create a hash representing this group
      group = {}
      group['area_ft2'] = area_ft2
      group['type'] = bldg_type
      group['stories'] = num_stories
      group['zones'] = gp_zns
      final_groups << group

      # Report out the final grouping
      OpenStudio.logFree(OpenStudio::Info, 'openstudio.standards.Model', "Final system type group: bldg_type = #{group['type']}, area = #{group['area_ft2'].round} ft2, num stories = #{group['stories']}, zones:")
      group['zones'].sort.each_slice(5) do |zone_list|
        zone_names = []
        zone_list.each do |zone|
          zone_names << zone.name.get.to_s
        end
        OpenStudio.logFree(OpenStudio::Info, 'openstudio.standards.Model', "--- #{zone_names.join(', ')}")
      end
    end

    return final_groups
  end

  # Determine the thermal zone's occupancy type category.
  # Options are: residential, nonresidential
  #
  # @param thermal_zone [OpenStudio::Model::ThermalZone] thermal zone
  # @return [String] the occupancy type category
  # @todo Add public assembly building types
  def thermal_zone_occupancy_type(thermal_zone)
    occ_type = if thermal_zone_residential?(thermal_zone)
                 'residential'
               else
                 'nonresidential'
               end

    # OpenStudio::logFree(OpenStudio::Info, "openstudio.Standards.ThermalZone", "For #{self.name}, occupancy type = #{occ_type}.")

    return occ_type
  end

  # Determine if the thermal zone is residential based on the space type properties for the spaces in the zone.
  # If there are both residential and nonresidential spaces in the zone,
  # the result will be whichever type has more floor area.
  # In the event that they are equal, it will be assumed nonresidential.
  #
  # @param thermal_zone [OpenStudio::Model::ThermalZone] thermal zone
  # return [Bool] true if residential, false if nonresidential
  def thermal_zone_residential?(thermal_zone)
    # Determine the respective areas
    res_area_m2 = 0
    nonres_area_m2 = 0
    thermal_zone.spaces.each do |space|
      # Ignore space if not part of total area
      next unless space.partofTotalFloorArea

      if space_residential?(space)
        res_area_m2 += space.floorArea
      else
        nonres_area_m2 += space.floorArea
      end
    end

    # Determine which is larger
    is_res = false
    if res_area_m2 > nonres_area_m2
      is_res = true
    end

    return is_res
  end

  # Returns the building type that represents the majority of floor area
  #
  # @param thermal_zone [OpenStudio::Model::ThermalZone] thermal zone
  # @return [String] the building type
  def thermal_zone_building_type(thermal_zone)
    # determine areas of each building type
    building_type_areas = {}
    thermal_zone.spaces.each do |space|
      # ignore space if not part of total area
      next unless space.partofTotalFloorArea

      if space.spaceType.is_initialized
        space_type = space.spaceType.get
        if space_type.standardsBuildingType.is_initialized
          building_type = space_type.standardsBuildingType.get
          if building_type_areas[building_type].nil?
            building_type_areas[building_type] = space.floorArea
          else
            building_type_areas[building_type] += space.floorArea
          end
        end
      end
    end

    # return largest building type area
    building_type = building_type_areas.key(building_type_areas.values.max)

    if building_type.nil?
      OpenStudio.logFree(OpenStudio::Info, 'openstudio.Standards.ThermalZone', "Thermal zone #{thermal_zone.name} does not have standards building type.")
    end

    return building_type
  end

  # for 2013 and prior, baseline fuel = proposed fuel
  # @param themal_zone
  # @return [string] with applicable DistrictHeating and/or DistrictCooling
  def thermal_zone_get_zone_fuels_for_occ_and_fuel_type(zone)
    zone_fuels = thermal_zone_fossil_or_electric_type(zone, '')
    return zone_fuels
  end

  # Determine if the thermal zone's fuel type category.
  # Options are:
  #   fossil, electric, unconditioned
  # If a customization is passed, additional categories may be returned.
  # If 'Xcel Energy CO EDA', the type fossilandelectric is added.
  # DistrictHeating is considered a fossil fuel since it is typically created by natural gas boilers.
  #
  # @param thermal_zone [OpenStudio::Model::ThermalZone] thermal zone
  # @param custom [String] string for custom case statement
  # @return [String] the fuel type category
  def thermal_zone_fossil_or_electric_type(thermal_zone, custom)
    # error if HVACComponent heating fuels method is not available
    if thermal_zone.model.version < OpenStudio::VersionString.new('3.6.0')
      OpenStudio.logFree(OpenStudio::Error, 'openstudio.Standards.ThermalZone', 'Required HVACComponent methods .heatingFuelTypes and .coolingFuelTypes are not available in pre-OpenStudio 3.6.0 versions. Use a more recent version of OpenStudio.')
    end

    # Cooling fuels, for determining unconditioned zones
    htg_fuels = thermal_zone.heatingFuelTypes.map(&:valueName)
    clg_fuels = thermal_zone.coolingFuelTypes.map(&:valueName)
    fossil = OpenstudioStandards::ThermalZone.thermal_zone_fossil_heat?(thermal_zone)
    district = OpenstudioStandards::ThermalZone.thermal_zone_district_heat?(thermal_zone)
    electric = OpenstudioStandards::ThermalZone.thermal_zone_electric_heat?(thermal_zone)

    # Categorize
    fuel_type = nil
    if fossil || district
      # If uses any fossil, counts as fossil even if electric is present too
      fuel_type = 'fossil'
    elsif electric
      fuel_type = 'electric'
    elsif htg_fuels.empty? && clg_fuels.empty?
      fuel_type = 'unconditioned'
    else
      OpenStudio.logFree(OpenStudio::Warn, 'openstudio.Standards.ThermalZone', "For #{thermal_zone.name}, could not determine fuel type, assuming fossil.  Heating fuels = #{htg_fuels.join(', ')}; cooling fuels = #{clg_fuels.join(', ')}.")
      fuel_type = 'fossil'
    end

    # Customization for Xcel.
    # Likely useful for other utility
    # programs where fuel switching is important.
    # This is primarily for systems where Gas is
    # used at the central AHU and electric is
    # used at the terminals/zones.  Examples
    # include zone VRF/PTHP with gas-heated DOAS,
    # and gas VAV with electric reheat
    case custom
    when 'Xcel Energy CO EDA'
      if fossil && electric
        fuel_type = 'fossilandelectric'
      end
    end

    return fuel_type
  end

  # Determine if the space is residential based on the space type name assigned to the space.
  # For spaces with no space type, assume nonresidential.
  # For spaces that are plenums, base the decision on the space
  # type of the space below the largest floor in the plenum.
  # Matches residential for names including 'Apartment', 'GuestRoom', 'PatRoom', 'ResBedroom', 'ResLiving'
  #
  # @param space [OpenStudio::Model::Space] space object
  # return [Boolean] true if residential, false if nonresidential
  def space_residential?(space)
    is_res = false

    space_to_check = space

    # If this space is a plenum, check the space type
    # of the space below the largest floor in the space
    if space_plenum?(space)
      # Find the largest floor
      largest_floor_area = 0.0
      largest_surface = nil
      space.surfaces.each do |surface|
        next unless surface.surfaceType == 'Floor' && surface.outsideBoundaryCondition == 'Surface'

        if surface.grossArea > largest_floor_area
          largest_floor_area = surface.grossArea
          largest_surface = surface
        end
      end
      if largest_surface.nil?
        OpenStudio.logFree(OpenStudio::Warn, 'openstudio.standards.Space', "#{space.name} is a plenum, but could not find a floor with a space below it to determine if plenum should be  res or nonres.  Assuming nonresidential.")
        return is_res
      end
      # Get the space on the other side of this floor
      if largest_surface.adjacentSurface.is_initialized
        adj_surface = largest_surface.adjacentSurface.get
        if adj_surface.space.is_initialized
          space_to_check = adj_surface.space.get
        else
          OpenStudio.logFree(OpenStudio::Warn, 'openstudio.standards.Space', "#{space.name} is a plenum, but could not find a space attached to the largest floor's adjacent surface #{adj_surface.name} to determine if plenum should be res or nonres.  Assuming nonresidential.")
          return is_res
        end
      else
        OpenStudio.logFree(OpenStudio::Warn, 'openstudio.standards.Space', "#{space.name} is a plenum, but could not find a floor with a space below it to determine if plenum should be  res or nonres.  Assuming nonresidential.")
        return is_res
      end
    end

    space_type = space_to_check.spaceType

    if space_type.is_initialized
      space_type = space_type.get
      # @todo need an alternate way of determining residential without standards data
      res_types = [/\sApartment/, /GuestRoom/, /PatRoom/, /ResBedroom/, /ResLiving/]
      if res_types.any? { |match| space_type.name.get =~ match }
        is_res = true
      end
    else
      OpenStudio.logFree(OpenStudio::Warn, 'openstudio.standards.Space', "Could not find a space type for #{space_to_check.name}, assuming nonresidential.")
    end

    return is_res
  end

  # Log the info, warning, and error messages to a file.
  #
  # runner @param [file_path] The path to the log file
  # debug @param [Boolean] If true, include the debug messages in the log
  # @return [Array<String>] The array of messages, which can be used elsewhere.
  def log_messages_to_file(file_path, debug = false)
    messages = []

    File.open(file_path, 'w') do |file|
      $OPENSTUDIO_LOG.logMessages.each do |msg|
        # DLM: you can filter on log channel here for now
        if /openstudio.*/ =~ msg.logChannel # /openstudio\.model\..*/
          # Skip certain messages that are irrelevant/misleading
          next if msg.logMessage.include?('UseWeatherFile') || # 'UseWeatherFile' is not yet a supported option for YearDescription
                  msg.logMessage.include?('Skipping layer') || # Annoying/bogus "Skipping layer" warnings
                  msg.logChannel.include?('runmanager') || # RunManager messages
                  msg.logChannel.include?('setFileExtension') || # .ddy extension unexpected
                  msg.logChannel.include?('Translator') || # Forward translator and geometry translator
                  msg.logMessage.include?('Successive data points') || # Successive data points (2004-Jan-31 to 2001-Feb-01, ending on line 753) are greater than 1 day apart in EPW file
                  msg.logMessage.include?('has multiple parents') || # Bogus errors about curves having multiple parents
                  msg.logMessage.include?('does not have an Output') || # Warning from EMS translation
                  msg.logMessage.include?('Prior to OpenStudio 2.6.2, this field was returning a double, it now returns an Optional double') # Warning about OS API change

          # Report the message in the correct way
          if msg.logLevel == OpenStudio::Info
            s = "INFO  #{msg.logMessage}"
            file.puts(s)
            messages << s
          elsif msg.logLevel == OpenStudio::Warn
            s = "WARN  #{msg.logMessage}"
            file.puts(s)
            messages << s
          elsif msg.logLevel == OpenStudio::Error
            s = "ERROR #{msg.logMessage}"
            file.puts(s)
            messages << s
          elsif msg.logLevel == OpenStudio::Debug && debug
            s = "DEBUG #{msg.logMessage}"
            file.puts(s)
            messages << s
          end
        end
      end
    end

    return messages
  end
  # -------------------------------------------------------------

  def add_system_to_zones(model, runner, hvac_system_type, zones, standard,
                          doas_dcv: false)
    if doas_dcv
      doas_system_type = 'DOAS with DCV'
    else
      doas_system_type = 'DOAS'
    end

    # create HVAC system
    # use methods in openstudio-standards
    # Standard.model_add_hvac_system(model, system_type, main_heat_fuel, zone_heat_fuel, cool_fuel, zones)
    # can be combination systems or individual objects - depends on the type of system
    # todo - reenable fan_coil_capacity_control_method when major installer released with udpated standards gem from what shipped with 2.9.0
    case hvac_system_type.to_s
    when 'DOAS with fan coil chiller with boiler'
      standard.model_add_hvac_system(model, doas_system_type, 'NaturalGas', nil, 'Electricity', zones,
                                     hot_water_loop_type: 'LowTemperature',
                                     air_loop_heating_type: 'Water',
                                     air_loop_cooling_type: 'Water')
      standard.model_add_hvac_system(model, 'Fan Coil', 'NaturalGas', nil, 'Electricity', zones,
                                     hot_water_loop_type: 'LowTemperature',
                                     zone_equipment_ventilation: false)
      # fan_coil_capacity_control_method: 'VariableFanVariableFlow')
      chilled_water_loop = model.getPlantLoopByName('Chilled Water Loop').get
      condenser_water_loop = model.getPlantLoopByName('Condenser Water Loop').get
      standard.model_add_waterside_economizer(model, chilled_water_loop, condenser_water_loop,
                                              integrated: true)

    when 'DOAS with fan coil chiller with central air source heat pump'
      standard.model_add_hvac_system(model, doas_system_type, 'AirSourceHeatPump', nil, 'Electricity', zones,
                                     air_loop_heating_type: 'Water',
                                     air_loop_cooling_type: 'Water')
      standard.model_add_hvac_system(model, 'Fan Coil', 'AirSourceHeatPump', nil, 'Electricity', zones,
                                     zone_equipment_ventilation: false)
      # fan_coil_capacity_control_method: 'VariableFanVariableFlow')
      chilled_water_loop = model.getPlantLoopByName('Chilled Water Loop').get
      condenser_water_loop = model.getPlantLoopByName('Condenser Water Loop').get
      standard.model_add_waterside_economizer(model, chilled_water_loop, condenser_water_loop,
                                              integrated: true)

    when 'DOAS with fan coil air-cooled chiller with boiler'
      standard.model_add_hvac_system(model, doas_system_type, 'NaturalGas', nil, 'Electricity', zones,
                                     hot_water_loop_type: 'LowTemperature',
                                     chilled_water_loop_cooling_type: 'AirCooled',
                                     air_loop_heating_type: 'Water',
                                     air_loop_cooling_type: 'Water')
      standard.model_add_hvac_system(model, 'Fan Coil', 'NaturalGas', nil, 'Electricity', zones,
                                     hot_water_loop_type: 'LowTemperature',
                                     chilled_water_loop_cooling_type: 'AirCooled',
                                     zone_equipment_ventilation: false)
    # fan_coil_capacity_control_method: 'VariableFanVariableFlow')

    when 'DOAS with fan coil air-cooled chiller with central air source heat pump'
      standard.model_add_hvac_system(model, doas_system_type, 'AirSourceHeatPump', nil, 'Electricity', zones,
                                     chilled_water_loop_cooling_type: 'AirCooled',
                                     air_loop_heating_type: 'Water',
                                     air_loop_cooling_type: 'Water')
      standard.model_add_hvac_system(model, 'Fan Coil', 'AirSourceHeatPump', nil, 'Electricity', zones,
                                     chilled_water_loop_cooling_type: 'AirCooled',
                                     zone_equipment_ventilation: false)
    # fan_coil_capacity_control_method: 'VariableFanVariableFlow')

    # ventilation provided by zone fan coil unit in fan coil systems
    when 'Fan coil chiller with boiler'
      standard.model_add_hvac_system(model, 'Fan Coil', 'NaturalGas', nil, 'Electricity', zones,
                                     hot_water_loop_type: 'LowTemperature')
      # fan_coil_capacity_control_method: 'VariableFanVariableFlow')
      chilled_water_loop = model.getPlantLoopByName('Chilled Water Loop').get
      condenser_water_loop = model.getPlantLoopByName('Condenser Water Loop').get
      standard.model_add_waterside_economizer(model, chilled_water_loop, condenser_water_loop,
                                              integrated: true)

    when 'Fan coil chiller with central air source heat pump'
      standard.model_add_hvac_system(model, 'Fan Coil', 'AirSourceHeatPump', nil, 'Electricity', zones)
      # fan_coil_capacity_control_method: 'VariableFanVariableFlow')
      chilled_water_loop = model.getPlantLoopByName('Chilled Water Loop').get
      condenser_water_loop = model.getPlantLoopByName('Condenser Water Loop').get
      standard.model_add_waterside_economizer(model, chilled_water_loop, condenser_water_loop,
                                              integrated: true)

    when 'Fan coil air-cooled chiller with boiler'
      standard.model_add_hvac_system(model, 'Fan Coil', 'NaturalGas', nil, 'Electricity', zones,
                                     hot_water_loop_type: 'LowTemperature',
                                     chilled_water_loop_cooling_type: 'AirCooled')
    # fan_coil_capacity_control_method: 'VariableFanVariableFlow')

    when 'Fan coil air-cooled chiller with central air source heat pump'
      standard.model_add_hvac_system(model, 'Fan Coil', 'AirSourceHeatPump', nil, 'Electricity', zones,
                                     chilled_water_loop_cooling_type: 'AirCooled')
    # fan_coil_capacity_control_method: 'VariableFanVariableFlow')

    when 'DOAS with radiant slab chiller with boiler'
      standard.model_add_hvac_system(model, doas_system_type, 'NaturalGas', nil, 'Electricity', zones,
                                     hot_water_loop_type: 'LowTemperature',
                                     air_loop_heating_type: 'Water',
                                     air_loop_cooling_type: 'Water')
      standard.model_add_hvac_system(model, 'Radiant Slab', 'NaturalGas', nil, 'Electricity', zones,
                                     hot_water_loop_type: 'LowTemperature')
      chilled_water_loop = model.getPlantLoopByName('Chilled Water Loop').get
      condenser_water_loop = model.getPlantLoopByName('Condenser Water Loop').get
      standard.model_add_waterside_economizer(model, chilled_water_loop, condenser_water_loop,
                                              integrated: true)

    when 'DOAS with radiant slab chiller with central air source heat pump'
      standard.model_add_hvac_system(model, doas_system_type, 'AirSourceHeatPump', nil, 'Electricity', zones,
                                     air_loop_heating_type: 'Water',
                                     air_loop_cooling_type: 'Water')
      standard.model_add_hvac_system(model, 'Radiant Slab', 'AirSourceHeatPump', nil, 'Electricity', zones)
      chilled_water_loop = model.getPlantLoopByName('Chilled Water Loop').get
      condenser_water_loop = model.getPlantLoopByName('Condenser Water Loop').get
      standard.model_add_waterside_economizer(model, chilled_water_loop, condenser_water_loop,
                                              integrated: true)

    when 'DOAS with radiant slab air-cooled chiller with boiler'
      standard.model_add_hvac_system(model, doas_system_type, 'NaturalGas', nil, 'Electricity', zones,
                                     hot_water_loop_type: 'LowTemperature',
                                     chilled_water_loop_cooling_type: 'AirCooled',
                                     air_loop_heating_type: 'Water',
                                     air_loop_cooling_type: 'Water')
      standard.model_add_hvac_system(model, 'Radiant Slab', 'NaturalGas', nil, 'Electricity', zones,
                                     hot_water_loop_type: 'LowTemperature',
                                     chilled_water_loop_cooling_type: 'AirCooled')

    when 'DOAS with radiant slab air-cooled chiller with central air source heat pump'
      standard.model_add_hvac_system(model, doas_system_type, 'AirSourceHeatPump', nil, 'Electricity', zones,
                                     chilled_water_loop_cooling_type: 'AirCooled',
                                     air_loop_heating_type: 'Water',
                                     air_loop_cooling_type: 'Water')
      standard.model_add_hvac_system(model, 'Radiant Slab', 'AirSourceHeatPump', nil, 'Electricity', zones,
                                     chilled_water_loop_cooling_type: 'AirCooled')

    when 'DOAS with VRF'
      standard.model_add_hvac_system(model, doas_system_type, 'Electricity', nil, 'Electricity', zones,
                                     air_loop_heating_type: 'DX',
                                     air_loop_cooling_type: 'DX')
      standard.model_add_hvac_system(model, 'VRF', 'Electricity', nil, 'Electricity', zones,
                                     zone_equipment_ventilation: false)

    when 'VRF'
      standard.model_add_hvac_system(model, 'VRF', 'Electricity', nil, 'Electricity', zones)

    when 'DOAS with water source heat pumps cooling tower with boiler'
      standard.model_add_hvac_system(model, doas_system_type, 'NaturalGas', nil, 'Electricity', zones,
                                     hot_water_loop_type: 'LowTemperature')
      standard.model_add_hvac_system(model, 'Water Source Heat Pumps', 'NaturalGas', nil, 'Electricity', zones,
                                     hot_water_loop_type: 'LowTemperature',
                                     heat_pump_loop_cooling_type: 'CoolingTower',
                                     zone_equipment_ventilation: false)

    when 'DOAS with water source heat pumps with ground source heat pump'
      standard.model_add_hvac_system(model, doas_system_type, 'Electricity', nil, 'Electricity', zones,
                                     air_loop_heating_type: 'DX',
                                     air_loop_cooling_type: 'DX')
      standard.model_add_hvac_system(model, 'Ground Source Heat Pumps', 'Electricity', nil, 'Electricity', zones,
                                     zone_equipment_ventilation: false)

    when 'Water source heat pumps cooling tower with boiler'
      standard.model_add_hvac_system(model, 'Water Source Heat Pumps', 'NaturalGas', nil, 'Electricity', zones,
                                     hot_water_loop_type: 'LowTemperature',
                                     heat_pump_loop_cooling_type: 'CoolingTower')

    when 'Water source heat pumps with ground source heat pump'
      standard.model_add_hvac_system(model, 'Ground Source Heat Pumps', 'Electricity', nil, 'Electricity', zones)

    # PVAV systems by default use a DX coil for cooling
    when 'PVAV with gas boiler reheat'
      # NOTE: Was 'LowTemperature' (120F supply) but EnergyPlus's UA
      # root-finder cannot bracket reheat coil sizing for VAV/PVAV
      # reheat systems at 120F/20F under standards 0.8.2 -- it errors
      # with "Bad starting values for UA / Inadequate water side
      # capacity, increase design loop exit temperature and/or decrease
      # design loop delta T". 90.1 PRM baseline calls for 180F/50F
      # (HighTemperature) for HW loops feeding reheat coils anyway.
      standard.model_add_hvac_system(model, 'PVAV Reheat', 'NaturalGas', 'NaturalGas', 'Electricity', zones,
                                     hot_water_loop_type: 'HighTemperature')

    when 'PVAV with central air source heat pump reheat'
      standard.model_add_hvac_system(model, 'PVAV Reheat', 'AirSourceHeatPump', 'AirSourceHeatPump', 'Electricity', zones)

    when 'VAV chiller with gas boiler reheat'
      standard.model_add_hvac_system(model, 'VAV Reheat', 'NaturalGas', 'NaturalGas', 'Electricity', zones,
                                     hot_water_loop_type: 'HighTemperature')
      chilled_water_loop = model.getPlantLoopByName('Chilled Water Loop').get
      condenser_water_loop = model.getPlantLoopByName('Condenser Water Loop').get
      standard.model_add_waterside_economizer(model, chilled_water_loop, condenser_water_loop,
                                              integrated: true)

    when 'VAV chiller with central air source heat pump reheat'
      standard.model_add_hvac_system(model, 'VAV Reheat', 'AirSourceHeatPump', 'AirSourceHeatPump', 'Electricity', zones)
      chilled_water_loop = model.getPlantLoopByName('Chilled Water Loop').get
      condenser_water_loop = model.getPlantLoopByName('Condenser Water Loop').get
      standard.model_add_waterside_economizer(model, chilled_water_loop, condenser_water_loop,
                                              integrated: true)

    when 'VAV air-cooled chiller with gas boiler reheat'
      standard.model_add_hvac_system(model, 'VAV Reheat', 'NaturalGas', 'NaturalGas', 'Electricity', zones,
                                     hot_water_loop_type: 'HighTemperature',
                                     chilled_water_loop_cooling_type: 'AirCooled')

    when 'VAV air-cooled chiller with central air source heat pump reheat'
      standard.model_add_hvac_system(model, 'VAV Reheat', 'AirSourceHeatPump', 'AirSourceHeatPump', 'Electricity', zones,
                                     chilled_water_loop_cooling_type: 'AirCooled')

    when 'PSZ-HP'
      standard.model_add_hvac_system(model, 'PSZ-HP', 'Electricity', nil, 'Electricity', zones)
    else
      runner.registerError("HVAC System #{hvac_system_type} not recognized")
      return false
    end
    runner.registerInfo("Added HVAC System type #{hvac_system_type} to the model for #{zones.size} zones")
  end

  def arguments(_model)
    args = OpenStudio::Measure::OSArgumentVector.new

    # argument to remove existing hvac system
    remove_existing_hvac = OpenStudio::Measure::OSArgument.makeBoolArgument('remove_existing_hvac', true)
    remove_existing_hvac.setDisplayName('Remove existing HVAC?')
    remove_existing_hvac.setDefaultValue(false)
    args << remove_existing_hvac

    # argument for HVAC system type
    hvac_system_type_choices = OpenStudio::StringVector.new
    hvac_system_type_choices << 'DOAS with fan coil chiller with boiler'
    hvac_system_type_choices << 'DOAS with fan coil chiller with central air source heat pump'
    hvac_system_type_choices << 'DOAS with fan coil air-cooled chiller with boiler'
    hvac_system_type_choices << 'DOAS with fan coil air-cooled chiller with central air source heat pump'
    hvac_system_type_choices << 'Fan coil chiller with boiler'
    hvac_system_type_choices << 'Fan coil chiller with central air source heat pump'
    hvac_system_type_choices << 'Fan coil air-cooled chiller with boiler'
    hvac_system_type_choices << 'Fan coil air-cooled chiller with central air source heat pump'
    hvac_system_type_choices << 'DOAS with radiant slab chiller with boiler'
    hvac_system_type_choices << 'DOAS with radiant slab chiller with central air source heat pump'
    hvac_system_type_choices << 'DOAS with radiant slab air-cooled chiller with boiler'
    hvac_system_type_choices << 'DOAS with radiant slab air-cooled chiller with central air source heat pump'
    hvac_system_type_choices << 'DOAS with VRF'
    hvac_system_type_choices << 'VRF'
    hvac_system_type_choices << 'DOAS with water source heat pumps cooling tower with boiler'
    hvac_system_type_choices << 'DOAS with water source heat pumps with ground source heat pump'
    hvac_system_type_choices << 'Water source heat pumps cooling tower with boiler'
    hvac_system_type_choices << 'Water source heat pumps with ground source heat pump'
    hvac_system_type_choices << 'VAV chiller with gas boiler reheat'
    hvac_system_type_choices << 'VAV chiller with central air source heat pump reheat'
    hvac_system_type_choices << 'VAV air-cooled chiller with gas boiler reheat'
    hvac_system_type_choices << 'VAV air-cooled chiller with central air source heat pump reheat'
    hvac_system_type_choices << 'PVAV with gas boiler reheat'
    hvac_system_type_choices << 'PVAV with central air source heat pump reheat'

    hvac_system_type = OpenStudio::Measure::OSArgument.makeChoiceArgument('hvac_system_type', hvac_system_type_choices, true)
    hvac_system_type.setDisplayName('HVAC System Type:')
    hvac_system_type.setDescription('Details on HVAC system type in measure documentation.')
    hvac_system_type.setDefaultValue('DOAS with fan coil chiller with central air source heat pump')
    args << hvac_system_type

    # make the DOAS system have DCV controls
    doas_dcv = OpenStudio::Measure::OSArgument.makeBoolArgument('doas_dcv', true)
    doas_dcv.setDisplayName('DOAS capable of demand control ventilation?')
    doas_dcv.setDescription('If a DOAS system, this will make air terminals variable air volume instead of constant volume.')
    doas_dcv.setDefaultValue(false)
    args << doas_dcv

    # argument for how to partition HVAC system
    hvac_system_partition_choices = OpenStudio::StringVector.new
    hvac_system_partition_choices << 'Automatic Partition'
    hvac_system_partition_choices << 'Whole Building'
    hvac_system_partition_choices << 'One System Per Building Story'
    hvac_system_partition_choices << 'One System Per Building Type'

    hvac_system_partition = OpenStudio::Measure::OSArgument.makeChoiceArgument('hvac_system_partition', hvac_system_partition_choices, true)
    hvac_system_partition.setDisplayName('HVAC System Partition:')
    hvac_system_partition.setDescription('Automatic Partition will separate the HVAC system by residential/non-residential and if loads and schedules are substantially different.')
    hvac_system_partition.setDefaultValue('Automatic Partition')
    args << hvac_system_partition

    # @todo add an argument for ventilation schedule

    return args
  end

  def run(model, runner, user_arguments)
    super(model, runner, user_arguments)

    if !ENV['SAVE_INTERMEDIATE'].nil?
      model.save('16.1.before_nze_hvac.osm')
    end

    # use the built-in error checking
    if !runner.validateUserArguments(arguments(model), user_arguments)
      return false
    end

    # assign user inputs
    remove_existing_hvac = runner.getBoolArgumentValue('remove_existing_hvac', user_arguments)
    hvac_system_type = runner.getOptionalStringArgumentValue('hvac_system_type', user_arguments)
    doas_dcv = runner.getBoolArgumentValue('doas_dcv', user_arguments)
    hvac_system_partition = runner.getOptionalStringArgumentValue('hvac_system_partition', user_arguments)
    hvac_system_partition = hvac_system_partition.to_s

    # register HVAC type name for 179D
    runner.registerValue('system_type_hvac_upgrade', hvac_system_type.to_s)

    # standard to access methods in openstudio-standards
    std = Standard.build('NREL ZNE Ready 2017')

    # ensure standards building type is set
    unless model.getBuilding.standardsBuildingType.is_initialized
      dominant_building_type = std.model_get_standards_building_type(model)
      if dominant_building_type.nil?
        # use office building type if none in model
        model.getBuilding.setStandardsBuildingType('Office')
      else
        model.getBuilding.setStandardsBuildingType(dominant_building_type)
      end
    end

    # get the climate zone
    climate_zone_obj = model.getClimateZones.getClimateZone('ASHRAE', 2006)
    if climate_zone_obj.empty
      climate_zone_obj = model.getClimateZones.getClimateZone('ASHRAE', 2013)
    end

    if climate_zone_obj.empty
      runner.registerError('Please assign an ASHRAE climate zone to the model before running the measure.')
      return false
    else
      climate_zone = "ASHRAE 169-2006-#{climate_zone_obj.value}"
    end

    # remove existing hvac system from model
    if remove_existing_hvac
      runner.registerInfo('Removing existing HVAC systems from the model')
      std.remove_hvac(model)
    end

    # exclude plenum zones, zones without thermostats, and zones with no floor area
    conditioned_zones = []
    model.getThermalZones.each do |zone|
      next if thermal_zone_plenum?(zone)
      next if !thermal_zone_heated?(zone) && !thermal_zone_cooled?(zone)

      conditioned_zones << zone
    end

    # logic to partition thermal zones to be served by different HVAC systems
    case hvac_system_partition

      when 'Automatic Partition'
        # group zones by occupancy type (residential/nonresidential)
        # split non-dominant groups if their total area exceeds 20,000 ft2.
        sys_groups = model_group_thermal_zones_by_occupancy_type(model, min_area_m2: OpenStudio.convert(20000, 'ft^2', 'm^2').get)

        # assume secondary system type is PSZ-AC for VAV Reheat otherwise assume same hvac system type
        sec_sys_type = hvac_system_type # same as primary system type
        sec_sys_type = 'PSZ-HP' if (hvac_system_type.to_s == 'VAV Reheat') || (hvac_system_type.to_s == 'PVAV Reheat')

        sys_groups.each do |sys_group|
          # add the primary system to the primary zones and the secondary system to any zones that are different
          # differentiate primary and secondary zones based on operating hours and internal loads (same as 90.1 PRM)
          pri_sec_zone_lists = std.model_differentiate_primary_secondary_thermal_zones(model, sys_group['zones'])

          # add the primary system to the primary zones
          add_system_to_zones(model, runner, hvac_system_type, pri_sec_zone_lists['primary'], std, doas_dcv:)

          # add the secondary system to the secondary zones (if any)
          if !pri_sec_zone_lists['secondary'].empty?
            runner.registerInfo("Secondary system type is #{sec_sys_type}")
            add_system_to_zones(model, runner, sec_sys_type, pri_sec_zone_lists['secondary'], std, doas_dcv:)
          end
        end

      when 'Whole Building'
        add_system_to_zones(model, runner, hvac_system_type, conditioned_zones, std, doas_dcv:)

      when 'One System Per Building Story'
        story_groups = model_group_thermal_zones_by_building_story(model, conditioned_zones)
        story_groups.each do |story_zones|
          add_system_to_zones(model, runner, hvac_system_type, story_zones, std, doas_dcv:)
        end

      when 'One System Per Building Type'
        system_groups = model_group_thermal_zones_by_building_type(model, min_area_m2: 0.0)
        system_groups.each do |system_group|
          add_system_to_zones(model, runner, hvac_system_type, system_group['zones'], std, doas_dcv:)
        end

      else
        runner.registerError('Invalid HVAC system partition choice')
        return false
    end

    # check that weather file exists for a sizing run
    if !model.weatherFile.is_initialized
      runner.registerError('Weather file not set. Cannot perform sizing run.')
      return false
    end

    # ensure sizing OA method is aligned
    model.getControllerMechanicalVentilations.each do |controller|
      controller.setSystemOutdoorAirMethod('ZoneSum')
    end

    # logic to ensure variable, not cycling, pump operation for chillers
    model.getChillerElectricEIRs.each { |chiller| chiller.setChillerFlowMode('LeavingSetpointModulated') }

    # log the build messages and errors to a file before sizing run in case of failure
    log_messages_to_file("#{Dir.pwd}/openstudio-standards.log", true)

    # perform a sizing run to get equipment sizes for efficiency standards
    if std.model_run_sizing_run(model, "#{Dir.pwd}/SizingRun") == false
      runner.registerError("Unable to perform sizing run for hvac system #{hvac_system_type} for this model.  Check the openstudio-standards.log in this measure for more details.")
      log_messages_to_file("#{Dir.pwd}/openstudio-standards.log", true)
      return false
    end

    # apply the HVAC efficiency standards
    std.model_apply_hvac_efficiency_standard(model, climate_zone)

    # log the build messages and errors to a file
    log_messages_to_file("#{Dir.pwd}/openstudio-standards.log", true)

    runner.registerFinalCondition("Added system type #{hvac_system_type} to model.")

    if !ENV['SAVE_INTERMEDIATE'].nil?
      model.save('16.2.after_nze_hvac.osm')
    end

    return true
  end
end

# this allows the measure to be used by the application
NetZeroEnergyHvac.new.registerWithApplication
