# This class holds methods that apply a 179D-flavored version of the ASHRAE
# 90.1-PRM-2019 (Appendix G) Performance Rating Method.
#
# Architecture (see instructions/PRM-2019-MIGRATION-MASTER-PLAN.md §4):
# - The proposed model is *built* with the 90.1-2019 prototype standard
#   (ASHRAE9012019), which ships the building data (space types, schedules,
#   constructions). PRM-2019 ships only rule data, so it cannot build.
# - This subclass *judges / transforms* that model under Appendix G. It inherits
#   the modern App-G algorithm from ASHRAE901PRM2019 and re-introduces the
#   historical `baseline_179d` on/off switch via two entry points:
#     * model_create_prm_any_baseline_building   -> baseline pass (super + 179D post-steps)
#     * model_create_179d_proposed_normalization -> proposed pass (infer+tag, neutral knobs only)
#
# The two non-negotiable rules (master plan §2):
#   Rule 1 — claimed knobs (envelope, LPD, HVAC efficiency) MUST differ between
#            baseline and proposed; never call model_create_prm_proposed_building.
#   Rule 2 — non-claimed knobs (infiltration) MUST be identical in both passes.
#
# @ref [References::ASHRAE901PRM2019]
class ACM179dASHRAE901PRM2019 < ASHRAE901PRM2019
  register_standard '179D 90.1-2019'
  attr_reader :template

  # NOTE on @template: we deliberately DO NOT override @template here. The parent
  # ASHRAE901PRM2019#initialize sets it to '90.1-PRM-2019', which is the key under
  # which ALL the vanilla PRM-2019 data is stored (prm_baseline_hvac, lpd, wwr,
  # swh, ...). Setting @template to a distinct '179D 90.1-2019' breaks every gem
  # data lookup -- e.g. model_prm_baseline_system_type raises
  # "Could not find baseline HVAC type for: 179D 90.1-2019-...". The 179D identity
  # comes from the registered factory name '179D 90.1-2019' + the method overrides
  # below, NOT from a distinct @template. (Dry-run lesson L10.) If Phase 3 adds
  # 179D data overlays, follow the 2007-fork pattern and rewrite each overlay row's
  # 'template' field to '90.1-PRM-2019' so it merges under the same key.

  # Loads the openstudio-standards dataset for this standard.
  #
  # Starts from the vanilla 90.1-PRM-2019 rule tables (via super). Phase 3
  # (reactive fixes) will layer reconciled 179D overlays here only where the
  # Phase 2 triage justifies it.
  #
  # @param data_directories [Array<String>] extra standards-data search paths
  # @return [Hash] the standards data hash
  def load_standards_database(data_directories = [])
    super(data_directories)
    # Phase 3 (reactive): layer on reconciled 179D overlays as needed.
  end

  # Baseline pass (apply_baseline: true). Inherits the full vanilla App-G
  # baseline rebuild from ASHRAE901PRM2019 via `super`, then runs 179D-specific
  # post-steps. The post-step hook is intentionally empty for Phase 1; Phase 3
  # PRs fill it in as triage justifies (master plan §6 / PR-SCOPES Group D).
  #
  # Signature mirrors the gem's 13-argument
  # ASHRAE901PRM2019#model_create_prm_any_baseline_building.
  def model_create_prm_any_baseline_building(*args)
    # L16 fix: stamp a valid PRM standards_space_type on each space type BEFORE
    # `super` applies baseline lighting. The 90.1-2019 prototype space-type names
    # (e.g. "WholeBuilding - Sm Office") are not valid PRM lpd_space_type keys, so
    # the gem's interior-lighting lookup misses AND removes the lights -> zero
    # baseline lighting. Stamping a PRM-valid name lets the LPD resolve to code-min.
    prepare_space_types_for_prm_lighting(args.first)
    super
    apply_179d_baseline_post_steps(args.first)
    true
  end

  # Proposed pass (apply_proposed: true). Hand-assembled normalization that KEEPS
  # the real, as-built HVAC + LPD + equipment efficiency and only adjusts the
  # neutral, non-claimed knobs to baseline-grade values. It mirrors the gem
  # baseline's post-rebuild apply sequence (Standards.Model.rb ~L399-507) on the
  # RETAINED HVAC, with the Gotcha #2 infer+tag plumbing up front.
  #
  # Deliberately SKIPPED (claimed -> must differ, §2 Rule 1):
  #   * envelope (WWR/skylight/constructions), lighting/LPD, SWH rebuild
  #   * HVAC rebuild (model_add_prm_baseline_system)
  #   * model_apply_hvac_efficiency_standard  (keep as-built efficiency, decision L6)
  # Applied identically to baseline (neutral / Rule 2): infiltration; and on the
  # retained HVAC: sizing temps/params, controls (SAT/economizer), VAV dampers,
  # multizone OA sizing, fan power, plant pump/count, DCV.
  #
  # @param model [OpenStudio::Model::Model] the proposed model (real HVAC retained)
  # @param climate_zone [String] e.g. 'ASHRAE 169-2013-2A'
  # @param hvac_building_type [String] valid prm_hvac_bldg_type (drives tag inference)
  # @param sizing_run_dir [String] directory for the proposed-pass sizing runs
  # @param debug [Boolean]
  # @return [Boolean] true on success, false if a sizing run fails
  def model_create_179d_proposed_normalization(model, climate_zone, hvac_building_type = 'other nonresidential', sizing_run_dir = Dir.pwd, debug = false)
    # Gotcha #2: stamp inferred baseline_system_type on retained HVAC so the
    # vanilla PRM-2019 apply methods (unguarded .get) don't crash.
    ensure_baseline_system_type_tags(model, climate_zone, hvac_building_type)

    # Capture, on the RETAINED HVAC, the characteristics the gem baseline records
    # BEFORE its rebuild and that the apply methods below read back via unguarded
    # .get. Without this, air_loop_hvac_apply_prm_baseline_fan_power raises
    # "Optional not initialized" on the missing per-zone supply/return/relief_fan_w.
    model_identify_non_mechanically_cooled_systems(model)
    if model_get_fan_power_breakdown
      model.getAirLoopHVACs.sort.each do |air_loop|
        supply_fan_w = air_loop_hvac_get_supply_fan_power(air_loop)
        return_fan_w = air_loop_hvac_get_return_fan_power(air_loop)
        relief_fan_w = air_loop_hvac_get_relief_fan_power(air_loop)
        air_loop.thermalZones.sort.each do |zone|
          zone.additionalProperties.setFeature('supply_fan_w', supply_fan_w.to_f)
          zone.additionalProperties.setFeature('return_fan_w', return_fan_w.to_f)
          zone.additionalProperties.setFeature('relief_fan_w', relief_fan_w.to_f)
        end
      end
    end

    # Evaluate DCV requirements (tags zones) before the DCV apply below.
    model_evaluate_dcv_requirements(model)

    # Rule 2: infiltration is neutral -> apply the SAME PRM infiltration the
    # baseline applies so it cancels in baseline - proposed.
    model_apply_standard_infiltration(model, infiltration_rate: prm_building_envelope_infiltration_rate)

    # --- Sizing settings / SAT (neutral) ---
    model.getThermalZones.each { |zone| thermal_zone_apply_prm_baseline_supply_temperatures(zone) }
    model.getAirLoopHVACs.each { |air_loop| air_loop_hvac_apply_prm_sizing_temperatures(air_loop) }
    model_apply_prm_baseline_sizing_schedule(model)
    model_apply_prm_sizing_parameters(model)

    # --- Controls: SAT reset, economizers (neutral) ---
    model.getAirLoopHVACs.sort.each { |air_loop| air_loop_hvac_apply_prm_baseline_controls(air_loop, climate_zone) }
    model.getPlantLoops.sort.each do |plant_loop|
      next if plant_loop_swh_loop?(plant_loop)

      plant_loop_apply_prm_baseline_temperatures(plant_loop)
    end

    # Sizing run #1
    return false unless model_run_sizing_run(model, "#{sizing_run_dir}/PROP-SR1")

    # --- Dampers + multizone OA sizing (neutral) ---
    model.getAirLoopHVACs.sort.each { |air_loop| air_loop_hvac_apply_minimum_vav_damper_positions(air_loop, false) }
    model_apply_multizone_vav_outdoor_air_sizing(model)

    # --- Fan power (neutral) ---
    model.getAirLoopHVACs.sort.each { |air_loop| air_loop_hvac_apply_prm_baseline_fan_power(air_loop) }
    model.getZoneHVACComponents.sort.each { |zone_hvac| zone_hvac_component_apply_prm_baseline_fan_power(zone_hvac) }

    # --- Plant loop counts (only affects retained real plant loops) ---
    model.getPlantLoops.sort.each do |plant_loop|
      next if plant_loop_swh_loop?(plant_loop)

      plant_loop_apply_prm_number_of_boilers(plant_loop)
      plant_loop_apply_prm_number_of_chillers(plant_loop)
    end
    model.getPlantLoops.sort.each do |plant_loop|
      next if plant_loop_swh_loop?(plant_loop)

      plant_loop_apply_prm_number_of_cooling_towers(plant_loop)
    end

    # Sizing run #2
    return false unless model_run_sizing_run(model, "#{sizing_run_dir}/PROP-SR2")

    # --- Pump power / control (neutral) ---
    model.getPlantLoops.sort.each do |plant_loop|
      next if plant_loop_swh_loop?(plant_loop)

      plant_loop_apply_prm_baseline_pump_power(plant_loop)
      plant_loop_apply_prm_baseline_pumping_type(plant_loop)
    end

    # NOTE: model_apply_hvac_efficiency_standard is deliberately SKIPPED here ->
    # the proposed model keeps its as-built equipment efficiency (claimed, §2 Rule 1).

    # --- DCV (neutral) ---
    model_set_baseline_demand_control_ventilation(model, climate_zone)

    # --- Final refinement + cleanup (same tail as the baseline) ---
    model_refine_size_dependent_values(model, sizing_run_dir)
    model_temp_fix_ems_references(model)
    model_remove_unused_resource_objects(model)
    model_add_reporting_tolerances(model)

    true
  end

  private

  # 179D-specific steps run after the vanilla App-G baseline rebuild (`super`).
  # Empty for Phase 1 by design — reactive fixes are added per Phase 2 triage.
  #
  # @param model [OpenStudio::Model::Model] the rebuilt baseline model
  # @return [void]
  def apply_179d_baseline_post_steps(model); end

  # Map from 90.1-2019 prototype standardsSpaceType -> valid PRM-2019
  # lpd_space_type (PNNL BEM-for-PRM list). Extend as building types are
  # validated; entries whose key already equals a valid PRM lpd_space_type pass
  # through untouched via the data-driven check below.
  # SME-reviewed 2026-06-12; per-row rationale captured in the PR-04 review
  # comment on the pull request. Generic names (Office/Corridor/Lobby/Restroom/
  # Kitchen/Mechanical) are shared across building types; the "- all other"
  # variants are used as the cross-type default unless a type-specific category
  # is clearly better. "- whole building" variants are preferred for Office,
  # Retail, Library, and Strip mall, matching the convention used across our
  # other modeling.
  PROTOTYPE_TO_PRM_LPD_SPACE_TYPE = {
    # whole-building offices
    'WholeBuilding - Sm Office' => 'office - whole building',
    'WholeBuilding - Md Office' => 'office - whole building',
    'WholeBuilding - Lg Office' => 'office - whole building',
    # school space-by-space (PrimarySchool / SecondarySchool share these names)
    'Classroom' => 'classroom/lecture/training - preschool to 12th',
    'Corridor' => 'corridor - all other',
    'Cafeteria' => 'dining - cafeteria/fast food',
    'ComputerRoom' => 'computer room',
    'Gym' => 'gymnasium playing area',
    'Kitchen' => 'kitchen',
    'Library' => 'library - whole building',
    'Lobby' => 'lobby - all other',
    'Auditorium' => 'audience seating - auditorium',
    'Mechanical' => 'electrical/mechanical',
    'Office' => 'office - whole building',
    'Restroom' => 'restroom - all other',
    'Stair' => 'stairwell',
    'PublicRestroom' => 'restroom - all other',
    'Storage' => 'storage 50 to 1000 sf - all other',
    # warehouse storage (DOE Warehouse Bulk/Fine zones)
    'Bulk' => 'warehouse - bulk storage',
    'Fine' => 'warehouse - fine storage',
    # retail (RetailStripmall whole-building + RetailStandalone space-by-space)
    'Strip mall - type 1' => 'retail - whole building',
    'Strip mall - type 2' => 'retail - whole building',
    'Strip mall - type 3' => 'retail - whole building',
    'Retail' => 'retail - whole building',
    'Point_of_Sale' => 'sales',
    'Back_Space' => 'storage 50 to 1000 sf - all other',
    'Entry' => 'lobby - all other',
    # hotel (SmallHotel)
    'GuestRoom123Occ' => 'guest room',
    'GuestRoom123Vac' => 'guest room',
    'GuestLounge' => 'lobby - hotel',
    'StaffLounge' => 'lounge/breakroom - all other',
    'Exercise' => 'exercise center - whole building',
    'Meeting' => 'conference/meeting/multipurpose',
    'Laundry' => 'laundry/washing',
    'ElevatorCore' => 'elevator core',
    'Elec/MechRoom' => 'electrical/mechanical'
  }.freeze

  # L16 fix: ensure every space type carries a PRM-valid standards_space_type so
  # the baseline interior-lighting LPD lookup resolves (otherwise the gem removes
  # the lights -> zero baseline lighting). Leaves already-valid names untouched
  # and warns on unmapped names (those still fall through to the gem's behavior).
  #
  # @param model [OpenStudio::Model::Model] the model about to be baselined
  # @return [void]
  def prepare_space_types_for_prm_lighting(model)
    valid_lpd = (standards_data['prm_interior_lighting'] || [])
                .map { |row| row['lpd_space_type'] }.compact.uniq
    if valid_lpd.empty?
      # Safety net: a future openstudio-standards version may rename the table key
      # (e.g. to 'prm_lpd' or 'interior_lighting_prm'). With no entries we lose the
      # "already PRM-valid, leave alone" shortcut and the L16 regression -- zero
      # baseline lighting -- would silently come back. Warn loudly so the rename
      # surfaces in the log instead of as a wrong savings number.
      OpenStudio.logFree(OpenStudio::Warn, 'openstudio.standards.SpaceType',
                         "179D L16: standards_data['prm_interior_lighting'] is empty -- the gem table key may have been renamed. " \
                         'Baseline lighting may regress; verify the table name in openstudio-standards.')
    end
    model.getSpaceTypes.each do |space_type|
      next unless space_type.standardsSpaceType.is_initialized

      current = space_type.standardsSpaceType.get
      next if valid_lpd.include?(current) # already PRM-valid

      mapped = PROTOTYPE_TO_PRM_LPD_SPACE_TYPE[current]
      if mapped.nil?
        OpenStudio.logFree(OpenStudio::Warn, 'openstudio.standards.SpaceType',
                           "179D L16: no PRM lpd_space_type mapping for '#{current}'; baseline lighting may be zero. Add it to PROTOTYPE_TO_PRM_LPD_SPACE_TYPE.")
        next
      end
      space_type.setStandardsSpaceType(mapped)
      OpenStudio.logFree(OpenStudio::Info, 'openstudio.standards.SpaceType',
                         "179D L16: stamped standardsSpaceType '#{current}' -> '#{mapped}' for PRM baseline lighting.")
    end
  end

  # Gotcha #2 plumbing: stamp the canonical `baseline_system_type` tag on every
  # retained air loop / thermal zone of the proposed model before the vanilla
  # PRM-2019 apply methods read it with unguarded `.get`. Uses a single
  # building-level inference (master plan §5 "use the predominant condition");
  # per-zone-group inference can be added reactively if a model needs it.
  #
  # @param model [OpenStudio::Model::Model] the proposed model
  # @param climate_zone [String] e.g. 'ASHRAE 169-2013-2A'
  # @param hvac_building_type [String] valid prm_hvac_bldg_type
  # @return [void]
  def ensure_baseline_system_type_tags(model, climate_zone, hvac_building_type)
    sys = select_baseline_system_type(hvac_building_type, model, climate_zone)
    model.getAirLoopHVACs.each { |air_loop| air_loop.additionalProperties.setFeature('baseline_system_type', sys) }
    model.getThermalZones.each { |zone| zone.additionalProperties.setFeature('baseline_system_type', sys) }
    OpenStudio.logFree(OpenStudio::Info, 'openstudio.standards.Model',
                       "179D proposed normalization: tagged retained HVAC with inferred baseline_system_type='#{sys}'.")
  end

  # Appendix G G3.1.1 baseline-system selection (master plan §5 pseudocode).
  # Returns the canonical `baseline_system_type` tag string. "warm" = CZ 0,1,2,3A.
  #
  # @return [String] one of PTAC/PTHP/PSZ_AC/PSZ_HP/PVAV_Reheat/PVAV_PFP_Boxes/
  #   VAV_Reheat/VAV_PFP_Boxes/Gas_Furnace/Electric_Furnace/SZ_CV_HW/SZ_CV_ER
  def select_baseline_system_type(hvac_building_type, model, climate_zone)
    cz = climate_zone.to_s.split('-')[-1].to_s.upcase # e.g. '2A'
    warm = %w[0A 0B 1A 1B 2A 2B 3A].include?(cz)
    area_ft2 = OpenStudio.convert(model.getBuilding.floorArea, 'm^2', 'ft^2').get
    floors = [model.getBuildingStorys.size, 1].max

    case hvac_building_type
    when 'residential'
      warm ? 'PTHP' : 'PTAC'
    when 'public assembly'
      area_ft2 < 120_000 ? (warm ? 'PSZ_HP' : 'PSZ_AC') : (warm ? 'SZ_CV_ER' : 'SZ_CV_HW')
    when 'heated-only storage'
      warm ? 'Electric_Furnace' : 'Gas_Furnace'
    when 'retail'
      warm ? 'PSZ_HP' : 'PSZ_AC'
    when 'hospital'
      area_ft2 > 150_000 ? 'VAV_Reheat' : 'PVAV_Reheat'
    else # 'other nonresidential' (offices, schools, restaurants, outpatient)
      if floors <= 3 && area_ft2 < 25_000
        warm ? 'PSZ_HP' : 'PSZ_AC'
      elsif (floors.between?(4, 5) && area_ft2 < 25_000) || (floors <= 5 && area_ft2.between?(25_000, 150_000))
        warm ? 'PVAV_PFP_Boxes' : 'PVAV_Reheat'
      else
        warm ? 'VAV_PFP_Boxes' : 'VAV_Reheat'
      end
    end
  end
end
