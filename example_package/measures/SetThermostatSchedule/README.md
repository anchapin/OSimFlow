

###### (Automatically generated documentation)

# Set Thermostat Schedule

## Description
Sets the heating and cooling setpoint schedules on every ThermostatSetpoint:DualSetpoint in the model to constant schedules at the user-supplied values (degrees Celsius).

## Modeler Description
Iterates over all ThermalZones; for each zone with a thermostat, replaces the existing heating / cooling schedules on the ThermostatSetpoint:DualSetpoint with two new OpenStudio::Model::ScheduleConstant schedules at the requested temperature values. Existing schedule rules are not preserved.

## Measure Type
ModelMeasure

## Taxonomy


## Arguments


### Heating Setpoint (C)

**Name:** heating_setpoint,
**Type:** Double,
**Units:** ,
**Required:** true,
**Model Dependent:** false


### Cooling Setpoint (C)

**Name:** cooling_setpoint,
**Type:** Double,
**Units:** ,
**Required:** true,
**Model Dependent:** false






