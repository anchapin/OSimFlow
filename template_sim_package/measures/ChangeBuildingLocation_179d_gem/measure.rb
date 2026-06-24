# *******************************************************************************
# OpenStudio(R), Copyright (c) Alliance for Sustainable Energy, LLC.
# See also https://openstudio.net/license
# *******************************************************************************

# Authors : Nicholas Long, David Goldwasser
# Simple measure to load the EPW file and DDY file

# load OpenStudio measure libraries from openstudio-extension gem
require 'openstudio-standards'

class ChangeBuildingLocation179D < OpenStudio::Measure::ModelMeasure
  Dir["#{__dir__}/resources/*.rb"].each { |file| require file }

  # define the name that a user will see, this method may be deprecated as
  # the display name in PAT comes from the name field in measure.xml
  def name
    'ChangeBuildingLocation'
  end

  # define the arguments that the user will input
  def arguments(_model)
    args = OpenStudio::Measure::OSArgumentVector.new

    weather_file_name = OpenStudio::Measure::OSArgument.makeStringArgument('weather_file_name', true)
    weather_file_name.setDisplayName('Weather File Name')
    weather_file_name.setDescription('Name of the weather file to change to. This is the filename with the extension (e.g. NewWeather.epw). Optionally this can inclucde the full file path, but for most use cases should just be file name.')
    weather_file_name.setDefaultValue('USA_UT_Salt.Lake.City.Intl.AP.725720_TMY3.epw')
    args << weather_file_name

    # # make choice argument for climate zone
    # choices = OpenStudio::StringVector.new
    # choices << "Lookup From EPW File"

    # climate_zone = OpenStudio::Measure::OSArgument.makeChoiceArgument("climate_zone", get_climate_zones(false, "Lookup From EPW File"), true)
    # climate_zone.setDisplayName("Climate Zone.")
    # climate_zone.setDefaultValue("Lookup From EPW file")
    # args << climate_zone

    set_year = OpenStudio::Measure::OSArgument.makeIntegerArgument('set_year', true)
    set_year.setDisplayName('Set Calendar Year')
    set_year.setDefaultValue 0
    set_year.setDescription('This will impact the day of the week the simulation starts on. An input value of 0 will leave the year un-altered')
    args << set_year

    # make an argument for use_upstream_args
    use_upstream_args = OpenStudio::Measure::OSArgument.makeBoolArgument('use_upstream_args', true)
    use_upstream_args.setDisplayName('Use Upstream Argument Values')
    use_upstream_args.setDescription('When true this will look for arguments or registerValues in upstream measures that match arguments from this measure, and will use the value from the upstream measure in place of what is entered for this measure.')
    use_upstream_args.setDefaultValue(true)
    args << use_upstream_args

    # make choice argument for climate zone
    choices = OpenStudio::StringVector.new
    choices << 'Do Nothing'
    choices << 'TMY3,AMY'
    choices << 'AMY,TMY3'
    epw_gsub = OpenStudio::Measure::OSArgument.makeChoiceArgument('epw_gsub', choices, true)
    epw_gsub.setDisplayName('Find and replace option from existing weather file name.')
    epw_gsub.setDescription('This will override what is entered in weather file name or from upstream measures, unless Do Nothing is selected.')
    epw_gsub.setDefaultValue('Do Nothing')
    args << epw_gsub

    args
  end

  # Define what happens when the measure is run
  def run(model, runner, user_arguments)
    super(model, runner, user_arguments)

    if !ENV['SAVE_INTERMEDIATE'].nil?
      model.save('2.1.before_ChangeBuildingLocation_179d_gem.osm')
    end

    # assign the user inputs to variables
    args = runner.getArgumentValues(arguments(model), user_arguments)
    # Keys are symbols, turn them into strings
    args.transform_keys!(&:to_s)
    if !args then return false end

    args['climate_zone'] = 'Lookup From EPW file'

    # lookup and replace argument values from upstream measures
    if args['use_upstream_args'] == true
      args.each do |arg, _value|
        next if arg == 'use_upstream_args' # this argument should not be changed

        value_from_osw = runner.getPastStepValuesForName(arg)
        value_from_osw = value_from_osw.collect { |k, v| { measure_name: k, value: v } }.first if !value_from_osw.empty?

        if !value_from_osw.empty?
          runner.registerInfo("Replacing argument named #{arg} from current measure with a value of #{value_from_osw[:value]} from #{value_from_osw[:measure_name]}.")
          new_val = value_from_osw[:value]
          # TODO: - make code to handle non strings more robust. check_upstream_measure_for_arg coudl pass bakc the argument type
          case arg
          when 'total_bldg_floor_area'
            args[arg] = new_val.to_f
          when 'num_stories_above_grade'
            args[arg] = new_val.to_f
          when 'zipcode'
            args[arg] = new_val.to_i
          else
            args[arg] = new_val
          end
        end
      end
    end

    # create initial condition
    if model.getWeatherFile.city == ''
      runner.registerInitialCondition("No weather file is set. The model has #{model.getDesignDays.size} design day objects")
    else
      runner.registerInitialCondition("The initial weather file is #{model.getWeatherFile.city} and the model has #{model.getDesignDays.size} design day objects")
    end

    # load standards
    std = Standard.build('90.1-2004') # template does not matter

    # use gsub if requested
    if args['epw_gsub'] != 'Do Nothing'
      # get the orig weather file from OSM
      file_name = model.getWeatherFile.url.get.split('/').last
      runner.registerInfo(file_name)

      if model.getWeatherFile.file.is_initialized
        orig_epw = model.getWeatherFile.url.get.split('/').last
        gsub_array = args['epw_gsub'].split(',')
        # updated line below so it doesn't matter what the user argument is, it always modifies what was in the seed OSM
        args['weather_file_name'] = orig_epw.gsub(gsub_array[0], gsub_array[1])
        runner.registerInfo("Changing target weather file from #{orig_epw} to #{args['weather_file_name']}.")
      end
    end

    # find weather file, checking both the location specified in the osw
    # and the path used by ComStock meta-measure
    # TODO: revise or delete. On the BEM-to-surrogate repo, this  ../../../weather is not a folder either (../../weather is)
    comstock_weather_file = File.absolute_path(File.join(Dir.pwd, '../../../weather', args['weather_file_name']))
    osw_weather_file = runner.workflow.findFile(args['weather_file_name'])
    if File.file? comstock_weather_file
      weather_file = comstock_weather_file
      runner.registerInfo("Found weather_file from ComStock WeatherFile: #{weather_file}")
    elsif osw_weather_file.is_initialized
      weather_file = osw_weather_file.get.to_s
      runner.registerInfo("Found weather_file from workflow.FindFile: #{weather_file}")
    elsif args['climate_zone'] # using openstudio-standard epw based on climatezone
      epw_file_name = std.model_get_climate_zone_weather_file_map[args['climate_zone']]
      weather_file = std.model_get_weather_file(epw_file_name)
      runner.registerInfo('Found weather_file from climate_zone os-standards')
    else
      runner.registerError("Did not find #{args['weather_file_name']} in paths described in OSW file or in default ComStock workflow location of #{comstock_weather_file}.")
      return false
    end

    # Parse the EPW manually because OpenStudio can't handle multiyear weather files (or DATA PERIODS with YEARS)
    epw_file = OpenStudio::Weather::Epw.load(weather_file)

    weather_file = model.getWeatherFile
    weather_file.setCity(epw_file.city)
    weather_file.setStateProvinceRegion(epw_file.state)
    weather_file.setCountry(epw_file.country)
    weather_file.setDataSource(epw_file.data_type)
    weather_file.setWMONumber(epw_file.wmo.to_s)
    weather_file.setLatitude(epw_file.lat)
    weather_file.setLongitude(epw_file.lon)
    weather_file.setTimeZone(epw_file.gmt)
    weather_file.setElevation(epw_file.elevation)
    weather_file.setString(10, epw_file.filename)

    weather_name = "#{epw_file.city}_#{epw_file.state}_#{epw_file.country}"
    weather_lat = epw_file.lat
    weather_lon = epw_file.lon
    weather_time = epw_file.gmt
    weather_elev = epw_file.elevation

    # Add or update site data
    site = model.getSite
    site.setName(weather_name)
    site.setLatitude(weather_lat)
    site.setLongitude(weather_lon)
    site.setTimeZone(weather_time)
    site.setElevation(weather_elev)

    runner.registerInfo("city is #{epw_file.city}. State is #{epw_file.state}")

    # actual year of start date
    if args['set_year'].to_i > 0
      model.getYearDescription.setCalendarYear(args['set_year'].to_i)
      runner.registerInfo("Changing Calendar Year to #{args['set_year']},")
    end

    # Add SiteWaterMainsTemperature -- via parsing of STAT file.
    stat_file = "#{File.join(File.dirname(epw_file.filename), File.basename(epw_file.filename, '.*'))}.stat"
    unless File.exist? stat_file
      runner.registerInfo 'Could not find STAT file by filename, looking in the directory'
      stat_files = Dir["#{File.dirname(epw_file.filename)}/*.stat"]
      if stat_files.size > 1
        runner.registerError('More than one stat file in the EPW directory')
        return false
      end
      if stat_files.empty?
        runner.registerError('Cound not find the stat file in the EPW directory')
        return false
      end

      runner.registerInfo "Using STAT file: #{stat_files.first}"
      stat_file = stat_files.first
    end
    unless stat_file
      runner.registerError 'Could not find stat file'
      return false
    end

    stat_model = EnergyPlus::StatFile.new(stat_file)
    water_temp = model.getSiteWaterMainsTemperature
    water_temp.setAnnualAverageOutdoorAirTemperature(stat_model.mean_dry_bulb)
    water_temp.setMaximumDifferenceInMonthlyAverageOutdoorAirTemperatures(stat_model.delta_dry_bulb)
    runner.registerInfo("mean dry bulb is #{stat_model.mean_dry_bulb}")

    # Remove all the Design Day objects that are in the file
    model.getObjectsByType('OS:SizingPeriod:DesignDay'.to_IddObjectType).each(&:remove)

    # find the ddy files
    ddy_file = "#{File.join(File.dirname(epw_file.filename), File.basename(epw_file.filename, '.*'))}.ddy"
    puts "ddy_file:#{ddy_file}"
    unless File.exist? ddy_file
      ddy_files = Dir["#{File.dirname(epw_file.filename)}/*.ddy"]
      if ddy_files.size > 1
        runner.registerError('More than one ddy file in the EPW directory')
        return false
      end
      if ddy_files.empty?
        runner.registerError('could not find the ddy file in the EPW directory')
        return false
      end

      ddy_file = ddy_files.first
    end

    unless ddy_file
      runner.registerError "Could not find DDY file for #{ddy_file}"
      return false
    end

    ddy_model = OpenStudio::EnergyPlus.loadAndTranslateIdf(ddy_file).get

    # Warn if no design days are present in the ddy file
    if ddy_model.getDesignDays.empty?
      runner.registerWarning('No design days were found in the ddy file.')
    end

    ddy_model.getDesignDays.sort.each do |d|
      # grab only the ones that matter
      ddy_list = [
        /Htg 99.6. Condns DB/, # Annual heating 99.6%
        /Clg .4. Condns WB=>MDB/, # Annual humidity (for cooling towers and evap coolers)
        /Clg .4. Condns DB=>MWB/, # Annual cooling
        /August .4. Condns DB=>MCWB/, # Monthly cooling DB=>MCWB (to handle solar-gain-driven cooling)
        /September .4. Condns DB=>MCWB/,
        /October .4. Condns DB=>MCWB/
      ]
      ddy_list.each do |ddy_name_regex|
        if d.name.get.to_s.match?(ddy_name_regex)
          runner.registerInfo("Adding object #{d.name}")

          # add the object to the existing model
          model.addObject(d.clone)
          break
        end
      end
    end

    # Warn if no design days were added
    if model.getDesignDays.empty?
      runner.registerWarning('No design days were added to the model.')
    end

    # Set climate zone
    climateZones = model.getClimateZones

    # ASHRAE 169-2013
    ashrae_cz_year = 2013
    climate_zones = {
      'USA_HI_Honolulu.Intl.AP.911820_TMY3.epw' => '1A',
      'USA_FL_Tampa.Intl.AP.722110_TMY3.epw' => '2A',
      'USA_AZ_Tucson.Intl.AP.722740_TMY3.epw' => '2B',
      'USA_GA_Atlanta-Hartsfield-Jackson.Intl.AP.722190_TMY3.epw' => '3A',
      'USA_TX_El.Paso.Intl.AP.722700_TMY3.epw' => '3B',
      'USA_CA_San.Jose-Mineta.Intl.AP.724945_TMY3.epw' => '3C',
      'USA_MD_Baltimore-Washington.Intl.AP.724060_TMY3.epw' => '4A',
      'USA_NM_Albuquerque.Intl.AP.723650_TMY3.epw' => '4B',
      'USA_WA_Seattle-Tacoma.Intl.AP.727930_TMY3.epw' => '4C',
      'USA_IL_Chicago-OHare.Intl.AP.725300_TMY3.epw' => '5A',
      'USA_UT_Salt.Lake.City.Intl.AP.725720_TMY3.epw' => '5B',
      'USA_WA_Port.Angeles-Fairchild.Intl.AP.727885_TMY3.epw' => '5C',
      'USA_MN_Rochester.Intl.AP.726440_TMY3.epw' => '6A',
      'USA_MT_Helena.Rgnl.AP.727720_TMY3.epw' => '6B',
      'USA_MN_Duluth.Intl.AP.727450_TMY3.epw' => '7A',
      'USA_AK_Fairbanks.Intl.AP.702610_TMY3.epw' => '8A',
    }

    # include key to empty array if the key is included in weather file input
    # this is to include a case where user adds not only weather file name but also path to the weather file
    climate_zone = ''
    found_keys = []
    climate_zones.each_key do |key|
      if args['weather_file_name'].include?(key)
        found_keys << key
      end
    end

    # get climate zone from hash
    if found_keys.size == 1
      climate_zone = climate_zones[found_keys[0]]
    else
      runner.registerError("cannot find the correct weather file from this string: #{found_keys}.")
      return false
    end

    climateZones.setClimateZone(OpenStudio::Model::ClimateZones.ashraeInstitutionName, ashrae_cz_year, climate_zone)
    runner.registerInfo("Setting ASHRAE Climate Zone to #{climate_zone}")

    # report time zone for use in results.csv
    runner.registerValue('climate_zone', "ASHRAE 169-2013-#{climate_zone}")

    # add final condition
    runner.registerFinalCondition("The final weather file is #{model.getWeatherFile.city} and the model has #{model.getDesignDays.size} design day objects.")

    if !ENV['SAVE_INTERMEDIATE'].nil?
      model.save('2.2.after_ChangeBuildingLocation_179d_gem.osm')
    end

    true
  end
end

# This allows the measure to be use by the application
ChangeBuildingLocation179D.new.registerWithApplication
