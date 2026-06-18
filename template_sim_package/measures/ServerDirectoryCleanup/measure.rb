# *******************************************************************************
# OpenStudio(R), Copyright (c) Alliance for Sustainable Energy, LLC.
# See also https://openstudio.net/license
# *******************************************************************************

# start the measure
class ServerDirectoryCleanup < OpenStudio::Measure::ReportingMeasure
  # define the name that a user will see, this method may be deprecated as
  # the display name in PAT comes from the name field in measure.xml
  def name
    'Server Directory Cleanup'
  end

  # file types that can be removed
  def self.file_types
    # key is arg name value is string for run method
    file_types = {}
    file_types['sql'] = '*.sql'
    file_types['eso'] = '*.eso'
    file_types['audit'] = '*.audit'
    file_types['osm'] = '*.osm'
    file_types['idf'] = '*.idf'
    file_types['bnd'] = '*.bnd'
    file_types['eio'] = '*.eio'
    file_types['shd'] = '*.shd'
    file_types['mdd'] = '*.mdd'
    file_types['rdd'] = '*.rdd'
    file_types['csv'] = '*.csv'
    file_types['Sizing Run Directories'] = 'Sizing Run Directories'

    return file_types
  end

  # define the arguments that the user will input
  def arguments(_model = nil)
    args = OpenStudio::Measure::OSArgumentVector.new

    ServerDirectoryCleanup.file_types.each do |file_type, _|
      arg = OpenStudio::Measure::OSArgument.makeBoolArgument(file_type, true)
      arg.setDisplayName("Remove '#{file_type}' files from run directory")
      if file_type == 'Sizing Run Directories'
        arg.setDefaultValue(false)
      else
        arg.setDefaultValue(true)
      end

      args << arg
    end

    args
  end

  # define what happens when the measure is run
  def run(runner, user_arguments)
    super(runner, user_arguments)

    # use the built-in error checking
    unless runner.validateUserArguments(arguments, user_arguments)
      false
    end

    # assign the user inputs to variables
    args = ServerDirectoryCleanup.file_types.to_h { |k, _| [k, runner.getBoolArgumentValue(k, user_arguments)] }
    initial_string = 'The following files were in the local run directory prior to the execution of this measure: '
    list_of_files = Dir.children('./..')
    initial_string = "#{initial_string + list_of_files.join(', ')}."
    runner.registerInitialCondition(initial_string)

    # TODO: - code to remove sizing runs is not functional yet
    # delete run directories
    ServerDirectoryCleanup.file_types.each do |k, v|
      next if !args[k]

      if v == 'Sizing Run Directories'

        Dir.glob('./../**/output').select { |e| File.directory? e }.each do |f|
          runner.registerInfo("Removing #{f} directory.")
          FileUtils.rm_f Dir.glob("#{f}/*")
          FileUtils.remove_dir(f, true)
        end
        # sometimes SizingRun seems to be used instead of output
        Dir.glob('./../**/SizingRun').select { |e| File.directory? e }.each do |f|
          runner.registerInfo("Removing #{f} directory.")
          FileUtils.rm_f Dir.glob("#{f}/*")
          FileUtils.remove_dir(f, true)
        end

      else
        Dir.glob("./../#{v}").each do |f|
          File.delete(f)
          runner.registerInfo("Deleted #{f} from the run directory.") if !File.exist?(f)
        end
      end
    end

    final_string = 'The following files were in the local run directory following to the execution of this measure: '
    list_of_files = Dir.children('./..')
    final_string = "#{final_string + list_of_files.join(', ')}."
    runner.registerFinalCondition(final_string)

    true
  end
end

# this allows the measure to be use by the application
ServerDirectoryCleanup.new.registerWithApplication
