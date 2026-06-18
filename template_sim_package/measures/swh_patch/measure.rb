# ComStock(TM), Copyright (c) 2020 Alliance for Sustainable Energy, LLC. All rights reserved.
# See top level LICENSE.txt file for license terms.

# *******************************************************************************
# OpenStudio(R), Copyright (c) Alliance for Sustainable Energy, LLC.
# See also https://openstudio.net/license
# *******************************************************************************

require 'openstudio-standards'

# start the measure
class SwhPatch < OpenStudio::Measure::ModelMeasure
  # human readable name
  def name
    return 'swh_patch'
  end

  # human readable description
  def description
    return ''
  end

  # human readable description of modeling approach
  def modeler_description
    return ''
  end

  # define the arguments that the user will input
  def arguments(_model)
    args = OpenStudio::Measure::OSArgumentVector.new

    # make an argument for water heater capacity ratio
    capacity_btu_per_hr_ratio = OpenStudio::Measure::OSArgument.makeDoubleArgument('capacity_btu_per_hr_ratio', true)
    capacity_btu_per_hr_ratio.setDisplayName('water heater capacity ratio (range: 0 to 1)')
    capacity_btu_per_hr_ratio.setDefaultValue(0.5)
    args << capacity_btu_per_hr_ratio

    first_hour_rating_ratio = OpenStudio::Measure::OSArgument.makeDoubleArgument('first_hour_rating_ratio', true)
    first_hour_rating_ratio.setDisplayName('first hour rating ratio (range: 0 to 1)')
    first_hour_rating_ratio.setDefaultValue(0.5)
    args << first_hour_rating_ratio

    use_upstream_arg = OpenStudio::Measure::OSArgument.makeBoolArgument('use_upstream_arg', true)
    use_upstream_arg.setDisplayName('Use upstream arguments')
    use_upstream_arg.setDescription('Use upstream arguments instead of input arguments defined in this measure?')
    use_upstream_arg.setDefaultValue(true)
    args << use_upstream_arg

    return args
  end

  def self.binning_capacity_btu_per_hr(water_storage_vol_nameplate_gal, capacity_btu_per_hr_ratio)
    if (water_storage_vol_nameplate_gal >= 25) && (water_storage_vol_nameplate_gal < 35)
      capacity_btu_per_hr = 27000 + (9000 * capacity_btu_per_hr_ratio)
    elsif (water_storage_vol_nameplate_gal >= 35) && (water_storage_vol_nameplate_gal < 45)
      capacity_btu_per_hr = 30000 + (20000 * capacity_btu_per_hr_ratio)
    elsif (water_storage_vol_nameplate_gal >= 45) && (water_storage_vol_nameplate_gal < 60)
      capacity_btu_per_hr = 34000 + (31000 * capacity_btu_per_hr_ratio)
    elsif (water_storage_vol_nameplate_gal >= 60) && (water_storage_vol_nameplate_gal < 77)
      capacity_btu_per_hr = 75000 + (125000 * capacity_btu_per_hr_ratio)
    elsif (water_storage_vol_nameplate_gal >= 77) && (water_storage_vol_nameplate_gal < 95)
      capacity_btu_per_hr = 120000 + (190000 * capacity_btu_per_hr_ratio)
    else
      # (water_storage_vol_nameplate_gal >= 95) && (water_storage_vol_nameplate_gal <= 120)
      capacity_btu_per_hr = 150000 + (250000 * capacity_btu_per_hr_ratio)
    end
    capacity_btu_per_hr
  end

  def self.binning_first_hour_rating(water_storage_vol_nameplate_gal, first_hour_rating_ratio)
    if (water_storage_vol_nameplate_gal >= 25) && (water_storage_vol_nameplate_gal < 35)
      first_hour_rating = 46 + (23 * first_hour_rating_ratio)
    elsif (water_storage_vol_nameplate_gal >= 35) && (water_storage_vol_nameplate_gal < 45)
      first_hour_rating = 51 + (40 * first_hour_rating_ratio)
    else
      # (water_storage_vol_nameplate_gal >= 45) && (water_storage_vol_nameplate_gal <= 60)
      first_hour_rating = 65 + (54 * first_hour_rating_ratio)
    end
    first_hour_rating
  end

  def self.water_heater_classification(capacity_btu_per_hr)
    if capacity_btu_per_hr > 75000
      water_heater_type = 'commercial'
    else
      water_heater_type = 'residential'
    end
    water_heater_type
  end

  def self.energy_factor_calculation(water_storage_vol_nameplate_gal)
    energy_factor = 0.62 - (0.0019 * water_storage_vol_nameplate_gal)

    return energy_factor
  end

  def self.standby_loss_btu_per_hr_calculation(eta_burner_final, capacity_btu_per_hr, water_storage_vol_nameplate_gal)
    standby_loss_btu_per_hr = eta_burner_final * ((capacity_btu_per_hr / 800) + (110 * Math.sqrt(water_storage_vol_nameplate_gal)))

    return standby_loss_btu_per_hr
  end

  # define what happens when the measure is run
  def run(model, runner, user_arguments)
    super(model, runner, user_arguments)

    if !ENV['SAVE_INTERMEDIATE'].nil?
      model.save('21.1.before_swh_patch.osm')
    end

    ########################################################
    # use the built-in error checking
    ########################################################
    if !runner.validateUserArguments(arguments(model), user_arguments)
      return false
    end

    ########################################################
    # initialize parameters
    ########################################################
    no_water_heater_in_the_model = true
    recovery_efficiency = 0.0

    ########################################################
    # assign the user inputs to variables
    ########################################################
    capacity_btu_per_hr_ratio = runner.getDoubleArgumentValue('capacity_btu_per_hr_ratio', user_arguments)
    first_hour_rating_ratio = runner.getDoubleArgumentValue('first_hour_rating_ratio', user_arguments)
    use_upstream_arg = runner.getBoolArgumentValue('use_upstream_arg', user_arguments)

    ########################################################
    # override input arguments if using upstream values
    ########################################################
    if use_upstream_arg
      capacity_btu_per_hr_ratio = runner.getPastStepValuesForName('capacity_btu_per_hr_ratio').values.first.to_f
      first_hour_rating_ratio = runner.getPastStepValuesForName('first_hour_rating_ratio').values.first.to_f
    end

    ########################################################
    # check min/max values of inputs
    ########################################################
    if (capacity_btu_per_hr_ratio < 0) && (capacity_btu_per_hr_ratio > 1)
      runner.registerError("Value of input arg is not within 0 and 1: capacity_btu_per_hr_ratio = #{capacity_btu_per_hr_ratio}")
      return false
    end
    if (first_hour_rating_ratio < 0) && (first_hour_rating_ratio > 1)
      runner.registerError("Value of input arg is not within 0 and 1: first_hour_rating_ratio = #{first_hour_rating_ratio}")
      return false
    end

    ########################################################
    # set limits
    ########################################################
    upper_limit_eta_burner_com = 0.99
    upper_limit_uef_res = 0.9 # based on AHRI db (residential gas storage type as of 11/3/2023)
    upper_limit_re_res = 0.97 # based on AHRI db (residential gas storage type as of 11/3/2023)

    ########################################################
    # loop through water heaters in model
    ########################################################
    model.getWaterHeaterMixeds.each do |wh|
      no_water_heater_in_the_model = false

      #------------------------------------------------------#
      # check water heater is gas storage type
      #------------------------------------------------------#
      unless wh.heaterFuelType.include?('Gas')
        runner.registerInfo("Skipping water heater '#{wh.name}'; fuel type is not gas.")
        next
      end
      unless wh.onCycleLossCoefficienttoAmbientTemperature.get > 0 || wh.offCycleLossCoefficienttoAmbientTemperature.get > 0
        runner.registerInfo("Skipping water heater '#{wh.name}'; water heater is already instantaneous tankless.")
        next
      end

      #------------------------------------------------------#
      # get nameplate water heater storage volume
      #------------------------------------------------------#
      water_storage_vol_nameplate_gal = OpenStudio.convert(wh.tankVolume.get, 'm^3', 'gal').get # m^3 to gal
      runner.registerInfo("Processing water heater (#{wh.name}): water heater storage volume (name plate) = #{water_storage_vol_nameplate_gal}")

      #------------------------------------------------------#
      # classify water heater based on name plate volume
      #------------------------------------------------------#
      # define 90.1-2007 parameters
      # Ony gas for now

      #------------------------------------------------------#
      # set calc heater capacity (residential <= 75000 Btu/hr, commercial > 75000 Btu/hr)
      #------------------------------------------------------#
      capacity_btu_per_hr = SwhPatch.binning_capacity_btu_per_hr(water_storage_vol_nameplate_gal, capacity_btu_per_hr_ratio)

      runner.registerInfo("Setting water heater (#{wh.name}): water heater maximum capacity (capacity_btu_per_hr = #{capacity_btu_per_hr})")
      wh.setHeaterMaximumCapacity(OpenStudio.convert(capacity_btu_per_hr, 'Btu/hr', 'W').get) # Btu/hr to W

      ua_btu_per_hr_r = nil
      eta_burner_final = nil

      if capacity_btu_per_hr > 75000 # Btu/h
        eta_burner_final = 0.8
        standby_loss_btu_per_hr = SwhPatch.standby_loss_btu_per_hr_calculation(eta_burner_final, capacity_btu_per_hr, water_storage_vol_nameplate_gal)
        ua_btu_per_hr_r = standby_loss_btu_per_hr / 70 # dividing by 70F. Standby Loss (SL): The average hourly energy, expressed in Btu per hour, required to maintain the stored water temperature based on a 70F temperature differential between stored water and ambient room temperature.
        ua_w_per_k = OpenStudio.convert(ua_btu_per_hr_r, 'Btu/hr*R', 'W/K').get # Btu/hr-R to W/K, 1 Btu/hr = 0.29307107 W, 1 R = 0.555556 K
      else

        energy_factor = SwhPatch.energy_factor_calculation(water_storage_vol_nameplate_gal) # From Table 7.8 90.1-2007 Standards

        # puts wh.heaterMaximumCapacity
        #------------------------------------------------------#
        # get/calc final heat loss coefficient (ua_btu_per_hr_r) and burner efficiency (eta_burner_final) based on water heater type
        #------------------------------------------------------#
        # references:
        # https://www.nrel.gov/docs/fy21osti/71633.pdf
        # https://github.com/NREL/OpenStudio-HPXML/blob/2e750ede8de83b1e489df36a883c71acb984e736/HPXMLtoOpenStudio/resources/waterheater.rb#L1455
        # https://nrel.sharepoint.com/:x:/r/sites/179d/Shared%20Documents/General/references/ 231103_product_specs_residential_ahri_db_aosmith.xlsx?d=wf6d7e5132a994c3087933a29fc93af7e&csf=1&web=1&e=mdscxf
        # https://github.com/NREL/OpenStudio-HPXML/blob/3e68033aabe677c69268aa1fe9ae9d2c25bb4600/HPXMLtoOpenStudio/resources/waterheater.rb#L1292C16-L1292C76

        # define constant properties
        density = 8.2938 # lb/gal
        cp = 1.0007 # Btu/lb-F
        t_in = 58.0 # F
        t_env = 67.5 # F
        t = 125.0 # F

        ef = energy_factor
        uef = (ef - 0.0711) / 0.9066 # inverting formula above to calculate uef
        if ef >= 0.75
          recovery_efficiency = (0.561 * ef) + 0.439
        else
          recovery_efficiency = (0.252 * ef) + 0.608
        end

        first_hour_rating = SwhPatch.binning_first_hour_rating(water_storage_vol_nameplate_gal, first_hour_rating_ratio)

        if (first_hour_rating >= 0) && (first_hour_rating < 18)
          volume_drawn = 10.0 # gal
        elsif (first_hour_rating >= 18) && (first_hour_rating < 51)
          volume_drawn = 38.0 # gal
        elsif (first_hour_rating >= 51) && (first_hour_rating < 75)
          volume_drawn = 55.0 # gal
        elsif (first_hour_rating >= 75) && (first_hour_rating <= 130)
          volume_drawn = 84.0 # gal
        else
          runner.registerError('first_hour_rating is beyond modeling range (< 130)')
          return false
        end

        # calc ua_final and eta_burner_final
        draw_mass = volume_drawn * density # lb
        q_load = draw_mass * cp * (t - t_in) # Btu/day
        ua_btu_per_hr_r = ((recovery_efficiency / uef) - 1.0) / (((t - t_env) * (24.0 / q_load)) - ((t - t_env) / (capacity_btu_per_hr * uef))) # Btu/hr-F
        eta_burner_final = recovery_efficiency + ((ua_btu_per_hr_r * (t - t_env)) / capacity_btu_per_hr) # conversion efficiency is slightly larger than recovery efficiency
        ua_w_per_k = OpenStudio.convert(ua_btu_per_hr_r, 'Btu/hr*R', 'W/K').get # Btu/hr-R to W/K, 1 Btu/hr = 0.29307107 W, 1 R = 0.555556 K
        eta_burner_final = eta_burner_final.round(4)
      end

      #------------------------------------------------------#
      # check final calculated values
      #------------------------------------------------------#
      if ua_btu_per_hr_r.nil? || eta_burner_final.nil?
        runner.registerError("Processing water heater (#{wh.name}): calculation faild. check ua_btu_per_hr_r (#{ua_w_per_k}) and eta_burner_final (#{eta_burner_final}) values/calculations.")
        return false
      end

      #------------------------------------------------------#
      # check final value limits
      #------------------------------------------------------#
      if capacity_btu_per_hr > 75000 # Btu/h
        unless eta_burner_final <= upper_limit_eta_burner_com
          runner.registerError("Processing water heater (#{wh.name}): water heater parameters are outside of the allowed ranges: eta_burner_final = #{eta_burner_final}")
          return false
        end
      else
        unless (uef <= upper_limit_uef_res) && (recovery_efficiency <= upper_limit_re_res)
          runner.registerError("Processing water heater (#{wh.name}): water heater parameters are outside of the allowed ranges: uef = #{uef}, recovery_efficiency = #{recovery_efficiency}")
          return false
        end
      end
      unless (eta_burner_final < 1.0) && (eta_burner_final >= 0.0)
        runner.registerError("Processing water heater (#{wh.name}): water heater parameters are outside of the allowed ranges: eta_burner_final = #{eta_burner_final}")
        return false
      end
      unless ua_btu_per_hr_r > 0
        runner.registerError("Processing water heater (#{wh.name}): water heater parameters are outside of the allowed ranges: ua_btu_per_hr_r = #{ua_btu_per_hr_r}")
        return false
      end

      #------------------------------------------------------#
      # replace exiting values with new values
      #------------------------------------------------------#
      runner.registerInfo("Processing water heater (#{wh.name}): burner efficiency (original) = #{wh.heaterThermalEfficiency.get.to_f.round(4)}")
      wh.setHeaterThermalEfficiency(eta_burner_final)
      runner.registerInfo("Processing water heater (#{wh.name}): burner efficiency (new) = #{wh.heaterThermalEfficiency.get.to_f}")
      runner.registerInfo("Processing water heater (#{wh.name}): replacing heat loss coefficient (original): off-cycle = #{wh.offCycleLossCoefficienttoAmbientTemperature.get.to_f.round(4)}, on-cycle = #{wh.onCycleLossCoefficienttoAmbientTemperature.get.to_f.round(4)}")
      wh.setOnCycleLossCoefficienttoAmbientTemperature(ua_w_per_k)
      wh.setOffCycleLossCoefficienttoAmbientTemperature(ua_w_per_k)
      runner.registerInfo("Processing water heater (#{wh.name}): replacing heat loss coefficient (new): off-cycle = #{wh.offCycleLossCoefficienttoAmbientTemperature.get.to_f}, on-cycle = #{wh.onCycleLossCoefficienttoAmbientTemperature.get.to_f}")
    end

    ########################################################
    # raise error if there is no water heater in model
    ########################################################
    if no_water_heater_in_the_model
      runner.registerInfo('No water heaters present in the model so skipping this measure.')
    end

    if !ENV['SAVE_INTERMEDIATE'].nil?
      model.save('21.2.after_swh_patch.osm')
    end

    true
  end
end

# register the measure to be used by the application
SwhPatch.new.registerWithApplication
