#!/usr/bin/env ruby
# frozen_string_literal: true

# Generate a minimal valid OpenStudio model (.osm) for E2E testing.
#
# Usage: ruby generate_seed_model.rb <output_path>
#
# Creates a 1-zone shoebox office model with:
#   - Minimal geometry (10m x 5m x 3m)
#   - Ideal loads HVAC
#   - All required construction/material objects
#   - A dummy thermostat schedule
#
# This must be run inside the nrel/openstudio container (or anywhere
# the openstudio Ruby bindings are available).

require "openstudio"

output_path = ARGV[0] || "model.osm"

model = OpenStudio::Model::Model.new

# --- Version ---
# (already present in blank model)

# --- Building ---
building = model.getBuilding
building.setName("Shoebox Office")

# --- Thermal Zone ---
zone = OpenStudio::Model::ThermalZone.new(model)
zone.setName("Core Zone")

# --- Space ---
space = OpenStudio::Model::Space.new(model)
space.setName("Core Space")
space.setThermalZone(zone)

# --- Geometry: 10m x 5m x 3m box ---
# Origin at (0,0,0), vertices going counterclockwise looking down.
width  = 10.0
depth  = 5.0
height = 3.0

# Floor
floor_vertices = [
  OpenStudio::Point3d.new(0, 0, 0),
  OpenStudio::Point3d.new(width, 0, 0),
  OpenStudio::Point3d.new(width, depth, 0),
  OpenStudio::Point3d.new(0, depth, 0),
]

# --- Materials ---
concrete = OpenStudio::Model::StandardOpaqueMaterial.new(model)
concrete.setName("Concrete 150mm")
concrete.setRoughness("MediumSmooth")
concrete.setThickness(0.15)
concrete.setConductivity(1.73)
concrete.setDensity(2243)
concrete.setSpecificHeat(837)

insulation = OpenStudio::Model::StandardOpaqueMaterial.new(model)
insulation.setName("Rigid Insulation 50mm")
insulation.setRoughness("MediumSmooth")
insulation.setThickness(0.05)
insulation.setConductivity(0.04)
insulation.setDensity(30.0)
insulation.setSpecificHeat(1400)

gypsum = OpenStudio::Model::StandardOpaqueMaterial.new(model)
gypsum.setName("Gypsum Board 12mm")
gypsum.setRoughness("Smooth")
gypsum.setThickness(0.012)
gypsum.setConductivity(0.16)
gypsum.setDensity(800)
gypsum.setSpecificHeat(1090)

# --- Constructions ---
ext_wall_layers = OpenStudio::Model::MaterialVector.new
ext_wall_layers << gypsum
ext_wall_layers << insulation
ext_wall_layers << concrete
ext_wall = OpenStudio::Model::Construction.new(model)
ext_wall.setLayers(ext_wall_layers)
ext_wall.setName("Ext Wall")

roof_layers = OpenStudio::Model::MaterialVector.new
roof_layers << insulation
roof_layers << concrete
roof = OpenStudio::Model::Construction.new(model)
roof.setLayers(roof_layers)
roof.setName("Roof")

floor_layers = OpenStudio::Model::MaterialVector.new
floor_layers << concrete
floor_constr = OpenStudio::Model::Construction.new(model)
floor_constr.setLayers(floor_layers)
floor_constr.setName("Floor Slab")

# Simple glazing for window
glazing = OpenStudio::Model::SimpleGlazing.new(model)
glazing.setName("Simple Glazing")
glazing.setUFactor(3.0)
glazing.setSolarHeatGainCoefficient(0.4)

# --- Create floor surface ---
floor_surf = OpenStudio::Model::Surface.new(floor_vertices, model)
floor_surf.setName("Floor")
floor_surf.setSurfaceType("Floor")
floor_surf.setSpace(space)
floor_surf.setConstruction(floor_constr)

# --- Create roof (ceiling) surface ---
roof_vertices = [
  OpenStudio::Point3d.new(0, 0, height),
  OpenStudio::Point3d.new(0, depth, height),
  OpenStudio::Point3d.new(width, depth, height),
  OpenStudio::Point3d.new(width, 0, height),
]
roof_surf = OpenStudio::Model::Surface.new(roof_vertices, model)
roof_surf.setName("Roof")
roof_surf.setSurfaceType("RoofCeiling")
roof_surf.setSpace(space)
roof_surf.setConstruction(roof)

# --- Create wall surfaces ---
# North wall (no window)
north_verts = [
  OpenStudio::Point3d.new(0, depth, 0),
  OpenStudio::Point3d.new(0, depth, height),
  OpenStudio::Point3d.new(width, depth, height),
  OpenStudio::Point3d.new(width, depth, 0),
]
north_wall = OpenStudio::Model::Surface.new(north_verts, model)
north_wall.setName("North Wall")
north_wall.setSurfaceType("Wall")
north_wall.setSpace(space)
north_wall.setConstruction(ext_wall)

# South wall (no window)
south_verts = [
  OpenStudio::Point3d.new(width, 0, 0),
  OpenStudio::Point3d.new(width, 0, height),
  OpenStudio::Point3d.new(0, 0, height),
  OpenStudio::Point3d.new(0, 0, 0),
]
south_wall = OpenStudio::Model::Surface.new(south_verts, model)
south_wall.setName("South Wall")
south_wall.setSurfaceType("Wall")
south_wall.setSpace(space)
south_wall.setConstruction(ext_wall)

# East wall
east_verts = [
  OpenStudio::Point3d.new(width, depth, 0),
  OpenStudio::Point3d.new(width, depth, height),
  OpenStudio::Point3d.new(width, 0, height),
  OpenStudio::Point3d.new(width, 0, 0),
]
east_wall = OpenStudio::Model::Surface.new(east_verts, model)
east_wall.setName("East Wall")
east_wall.setSurfaceType("Wall")
east_wall.setSpace(space)
east_wall.setConstruction(ext_wall)

# West wall
west_verts = [
  OpenStudio::Point3d.new(0, 0, 0),
  OpenStudio::Point3d.new(0, 0, height),
  OpenStudio::Point3d.new(0, depth, height),
  OpenStudio::Point3d.new(0, depth, 0),
]
west_wall = OpenStudio::Model::Surface.new(west_verts, model)
west_wall.setName("West Wall")
west_wall.setSurfaceType("Wall")
west_wall.setSpace(space)
west_wall.setConstruction(ext_wall)

# --- Thermostat & HVAC ---
# Always-on schedule for thermostat
always_on = OpenStudio::Model::ScheduleConstant.new(model)
always_on.setName("Always On")
always_on.setValue(1.0)

htg_sch = OpenStudio::Model::ScheduleConstant.new(model)
htg_sch.setName("Heating Setpoint 20C")
htg_sch.setValue(20.0)

clg_sch = OpenStudio::Model::ScheduleConstant.new(model)
clg_sch.setName("Cooling Setpoint 25C")
clg_sch.setValue(25.0)

thermostat = OpenStudio::Model::ThermostatSetpointDualSetpoint.new(model)
thermostat.setName("Main Thermostat")
thermostat.setHeatingSetpointTemperatureSchedule(htg_sch)
thermostat.setCoolingSetpointTemperatureSchedule(clg_sch)
zone.setThermostat(thermostat)

# Ideal loads
ideal = OpenStudio::Model::ZoneHVACIdealLoadsAirSystem.new(model)
ideal.setName("Ideal Loads")
ideal.addToThermalZone(zone)

# --- Internal loads ---
# People
people_def = OpenStudio::Model::PeopleDefinition.new(model)
people_def.setName("People Def")
people_def.setNumberOfPeopleCalculationMethod("People")
people_def.setNumberOfPeople(2)
people = OpenStudio::Model::People.new(people_def)
people.setName("People")
people.setSpace(space)
people.setNumberOfPeopleSchedule(always_on)

# Lights
lights_def = OpenStudio::Model::LightsDefinition.new(model)
lights_def.setName("Lights Def")
lights_def.setDesignLevelCalculationMethod("WattsperFloorArea")
lights_def.setWattsperFloorArea(10.0)
lights = OpenStudio::Model::Lights.new(lights_def)
lights.setName("Lights")
lights.setSpace(space)
lights.setSchedule(always_on)

# Electric equipment
equip_def = OpenStudio::Model::ElectricEquipmentDefinition.new(model)
equip_def.setName("Equip Def")
equip_def.setDesignLevelCalculationMethod("WattsperFloorArea")
equip_def.setWattsperFloorArea(5.0)
equip = OpenStudio::Model::ElectricEquipment.new(equip_def)
equip.setName("Equipment")
equip.setSpace(equip)
equip.setSchedule(always_on)

# Infiltration
infiltration = OpenStudio::Model::SpaceInfiltrationDesignFlowRate.new(model)
infiltration.setName("Infiltration")
infiltration.setSpace(space)
infiltration.setDesignFlowRateCalculationMethod("FlowPerExteriorArea")
infiltration.setFlowPerExteriorArea(0.0003)
infiltration.setSchedule(always_on)

# --- Save ---
path = OpenStudio::Path.new(output_path)
model.save(path, true)

puts "Generated real .osm at: #{output_path}"
puts "Model objects: #{model.objects.size}"
