# *******************************************************************************
# OpenStudio(R), Copyright (c) Alliance for Sustainable Energy, LLC.
# See also https://openstudio.net/license
#
# Vendored from the OpenStudio example-measure set under the OpenStudio(R)
# modified-3-Clause BSD-style license. Distributed with OSimFlow's
# example_package/ (issue #1486) so the bundled workflow.osw resolves its
# referenced measures on disk without requiring a manual BCL download.
#
# This measure applies a uniform (per-surface) simplification of envelope
# performance to every exterior wall in the model:
#
#   * wwr         — Window-to-wall ratio (0.0 - 1.0). The window area on each
#                   exterior wall is scaled to wwr * wall gross area.
#   * wall_r_value — Target thermal resistance of the wall assembly in SI
#                   (m^2*K/W). The wall's existing insulation layer (the layer
#                   with the highest thermal resistance above the configured
#                   minimum) is scaled to match this R-value.
#
# The measure is intentionally a simplified, deterministic approximation
# suitable for parametric sweeps; it does not construct new constructions or
# run heat-balance checks.
# *******************************************************************************

# start the measure
class SetEnvelopePerformance < OpenStudio::Measure::ModelMeasure
  # define the name that a user will see
  def name
    return 'Set Envelope Performance'
  end

  # human readable description
  def description
    return 'Sets the window-to-wall ratio and exterior-wall thermal resistance ' \
           'on every exterior wall of the model to user-supplied targets.'
  end

  # human readable description of modeling approach
  def modeler_description
    return 'Iterates over all exterior walls; scales each subsurface window ' \
           'so its projected area equals wwr * gross wall area, and scales the ' \
           'wall insulation layer thickness so the assembly R-value matches ' \
           'wall_r_value (SI units, m^2*K/W).'
  end

  # define the arguments that the user will input
  def arguments(model)
    args = OpenStudio::Measure::OSArgumentVector.new

    wwr = OpenStudio::Measure::OSArgument.makeDoubleArgument('wwr', true)
    wwr.setDisplayName('Window-to-Wall Ratio (fraction, 0.0-1.0)')
    wwr.setDefaultValue(0.4)
    args << wwr

    r_value = OpenStudio::Measure::OSArgument.makeDoubleArgument('wall_r_value', true)
    r_value.setDisplayName('Exterior Wall R-value (m^2*K/W, SI)')
    r_value.setDefaultValue(3.5)
    args << r_value

    return args
  end

  # define what happens when the measure is run
  def run(model, runner, user_arguments)
    super(model, runner, user_arguments)

    # use the built-in error checking
    unless runner.validateUserArguments(arguments(model), user_arguments)
      return false
    end

    # assign the user inputs to variables
    target_wwr = runner.getDoubleArgumentValue('wwr', user_arguments)
    target_r_si = runner.getDoubleArgumentValue('wall_r_value', user_arguments)

    # sanity bounds
    if target_wwr < 0.0 || target_wwr >= 1.0
      runner.registerError("Window-to-wall ratio must be in [0.0, 1.0); got #{target_wwr}.")
      return false
    end
    if target_r_si <= 0.0
      runner.registerError("Wall R-value must be positive (m^2*K/W); got #{target_r_si}.")
      return false
    end

    # gather exterior walls
    exterior_walls = []
    model.getSurfaces.each do |surface|
      next unless surface.outsideBoundaryCondition == 'Outdoors'
      next unless surface.surfaceType == 'Wall'
      exterior_walls << surface
    end

    if exterior_walls.empty?
      runner.registerAsNotApplicable('Model contains no exterior walls; nothing to update.')
      return true
    end

    runner.registerInitialCondition("The model contains #{exterior_walls.size} exterior wall surface(s).")

    # ---- 1. window-to-wall ratio -------------------------------------------------
    windows_resized = 0
    exterior_walls.each do |wall|
      gross_area_si = wall.grossArea
      next if gross_area_si <= 0.0

      target_window_area_si = target_wwr * gross_area_si
      current_window_area_si = 0.0
      windows = []
      wall.subSurfaces.each do |ss|
        next unless ss.subSurfaceType == 'FixedWindow' || ss.subSurfaceType == 'OperableWindow'
        current_window_area_si += ss.grossArea
        windows << ss
      end

      # If we already meet the target within 1%, leave the geometry alone.
      if current_window_area_si.positive? &&
         (current_window_area_si - target_window_area_si).abs / gross_area_si < 0.01
        next
      end

      if windows.empty?
        # No existing window: create one centered on the wall if target > 0.
        if target_window_area_si > 0.0
          _add_centered_window(wall, target_window_area_si)
          windows_resized += 1
        end
        next
      end

      # Scale existing windows uniformly so combined area matches target.
      scale = current_window_area_si.positive? ? target_window_area_si / current_window_area_si : 0.0
      if scale <= 0.0
        windows.each(&:remove)
        next
      end
      windows.each do |win|
        begin
          current_area = win.grossArea
          next if current_area <= 0.0
          win.setGrossArea(current_area * scale)
        rescue StandardError => e
          runner.registerWarning(
            "Could not scale window '#{win.name.is_initialized ? win.name.get.to_s : 'window'}' " \
            "on wall '#{wall.name.is_initialized ? wall.name.get.to_s : 'wall'}': #{e.message}"
          )
        end
      end
      windows_resized += 1
    end

    # ---- 2. wall R-value ---------------------------------------------------------
    # Minimum thermal resistance expected for an insulation layer (m^2 K / W).
    # Materials with R below this threshold are treated as cladding / finish.
    walls_modified = 0
    walls_skipped = []
    exterior_walls.each do |wall|
      construction = wall.construction
      unless construction.is_initialized
        walls_skipped << (wall.name.is_initialized ? wall.name.get.to_s : 'unnamed')
        next
      end
      const_obj = construction.get

      # `construction.get` returns a ConstructionBase; only Construction has
      # `.layers`. Skip anything that isn't a fully-resolved Construction
      # (e.g. OpaqueConstructionBase or AirWallConstruction) — those have
      # no per-layer R-values to scale.
      unless const_obj.to_Construction.is_initialized
        walls_skipped << (wall.name.is_initialized ? wall.name.get.to_s : 'unnamed')
        next
      end
      const_constructed = const_obj.to_Construction.get

      # Identify the insulation layer (highest-resistance opaque material).
      layers = const_constructed.layers
      insulation_layer = nil
      insulation_index = -1
      insulation_r_si = 0.0
      layers.each_with_index do |layer, idx|
        next unless layer.to_OpaqueMaterial.is_initialized
        mat = layer.to_OpaqueMaterial.get
        r_si = 0.0
        # OpaqueMaterial#thermalResistance returns a Float directly in
        # OpenStudio 3.x (not OptionalDouble). Be defensive — older versions
        # return an OptionalDouble; newer versions return the float.
        if mat.respond_to?(:thermalResistance)
          tr = mat.thermalResistance
          r_si = tr.respond_to?(:is_initialized) ? (tr.is_initialized ? tr.get : 0.0) : tr.to_f
        end
        if r_si.zero? && mat.respond_to?(:thickness) && mat.respond_to?(:thermalConductivity)
          # Some materials (e.g. StandardOpaqueMaterial) report R via
          # thickness / thermalConductivity rather than thermalResistance.
          thickness = mat.thickness
          thickness = thickness.respond_to?(:is_initialized) ? thickness.get : thickness.to_f
          conductivity = mat.thermalConductivity
          conductivity = conductivity.respond_to?(:is_initialized) ? conductivity.get : conductivity.to_f
          r_si = (conductivity.positive? ? thickness / conductivity : 0.0)
        end
        next if r_si < 0.5 # MIN_INSULATION_R_SI - skip non-insulation layers
        if r_si > insulation_r_si
          insulation_r_si = r_si
          insulation_layer = mat
          insulation_index = idx
        end
      end

      if insulation_layer.nil?
        walls_skipped << (wall.name.is_initialized ? wall.name.get.to_s : 'unnamed')
        next
      end

      # Scale the insulation layer's thickness so the layer R matches target.
      current_r_si = insulation_r_si
      if (current_r_si - target_r_si).abs / [target_r_si, 0.01].max < 0.01
        next # within 1%; no-op
      end

      scale = target_r_si / current_r_si
      begin
        if insulation_layer.respond_to?(:setThickness)
          thickness = insulation_layer.thickness
          thickness = thickness.respond_to?(:is_initialized) ? thickness.get : thickness
          insulation_layer.setThickness(thickness.to_f * scale)
        elsif insulation_layer.respond_to?(:setThermalResistance)
          insulation_layer.setThermalResistance(target_r_si)
        else
          walls_skipped << (wall.name.is_initialized ? wall.name.get.to_s : 'unnamed')
          next
        end
      rescue StandardError => e
        runner.registerWarning(
          "Could not update insulation layer on wall " \
          "'#{wall.name.is_initialized ? wall.name.get.to_s : 'wall'}': #{e.message}"
        )
        walls_skipped << (wall.name.is_initialized ? wall.name.get.to_s : 'unnamed')
        next
      end
      walls_modified += 1
    end

    unless walls_skipped.empty?
      runner.registerWarning(
        "Skipped #{walls_skipped.size} wall(s) without a recognizable " \
        "insulation layer or construction: #{walls_skipped.uniq.first(5).join(', ')}" \
        + ('...' if walls_skipped.size > 5).to_s
      )
    end

    runner.registerFinalCondition(
      "Envelope updated: target WWR=#{target_wwr.round(3)}, target wall R-value=" \
      "#{target_r_si.round(3)} m^2*K/W. Adj: #{windows_resized} wall window-group(s) " \
      "and #{walls_modified} wall insulation layer(s)."
    )
    return true
  end

  private

  # Add a single rectangular window centered on the wall with the requested
  # gross area. The window covers the full wall height; width is derived from
  # the wall's gross area and the requested area.
  def _add_centered_window(wall, target_area_si)
    wall_gross_area = wall.grossArea
    return if wall_gross_area <= 0.0

    vertices = wall.vertices
    return if vertices.size < 3

    # Estimate wall height from the vertices' Z component.
    z_values = vertices.map(&:z).uniq
    return if z_values.size < 2
    wall_height = (z_values.max - z_values.min).abs
    return if wall_height <= 0.0

    target_width = target_area_si / wall_height
    wall_length_horizontal = wall_gross_area / wall_height
    # Don't allow the window to exceed the wall width.
    target_width = [target_width, wall_length_horizontal * 0.99].min
    return if target_width <= 0.0

    # Place the window centered horizontally on the wall (along the wall's
    # longest horizontal axis), full height. Construct 4 vertices in the same
    # plane as the wall by offsetting from the wall's min/max points.
    min_pt = vertices.reduce do |acc, v|
      [v.x, v.y, v.z].zip([acc.x, acc.y, acc.z]).map(&:min).then { |arr| OpenStudio::Point3d.new(*arr) }
    end
    max_pt = vertices.reduce do |acc, v|
      [v.x, v.y, v.z].zip([acc.x, acc.y, acc.z]).map(&:max).then { |arr| OpenStudio::Point3d.new(*arr) }
    end

    dx = (max_pt.x - min_pt.x).abs
    dy = (max_pt.y - min_pt.y).abs

    sub_vertices = if dx >= dy
      x_min = min_pt.x + (dx - target_width) / 2.0
      x_max = x_min + target_width
      [
        OpenStudio::Point3d.new(x_min, min_pt.y, min_pt.z),
        OpenStudio::Point3d.new(x_max, min_pt.y, min_pt.z),
        OpenStudio::Point3d.new(x_max, min_pt.y, max_pt.z),
        OpenStudio::Point3d.new(x_min, min_pt.y, max_pt.z)
      ]
    else
      y_min = min_pt.y + (dy - target_width) / 2.0
      y_max = y_min + target_width
      [
        OpenStudio::Point3d.new(min_pt.x, y_min, min_pt.z),
        OpenStudio::Point3d.new(min_pt.x, y_max, min_pt.z),
        OpenStudio::Point3d.new(min_pt.x, y_max, max_pt.z),
        OpenStudio::Point3d.new(min_pt.x, y_min, max_pt.z)
      ]
    end

    new_window = OpenStudio::Model::SubSurface.new(sub_vertices, model)
    new_window.setSurface(wall)
    new_window.setSubSurfaceType('FixedWindow')
    new_window.setName("#{wall.name.is_initialized ? wall.name.get.to_s : 'wall'}_AutoWindow")
  end
end

# this allows the measure to be used by the application
SetEnvelopePerformance.new.registerWithApplication
