

###### (Automatically generated documentation)

# HVAC ERV DCV

## Description
Adds ERV or DCV.

## Modeler Description
ERV: Heat/energy recovery added based on climate zone. Energy recovery added to ASHRAE 'humid' climates, heat recovery added to all others. Effectiveness is based on Ventacity system. Additional fan static pressure is added as wheel power to capture impact of bypass. DCV: Add demand control ventilation to variable volume HVAC systems. Requires that the design specification outdoor air objects have some part of the ventilation be specified as per person. Also requires that if zone hvac equipment is present, it takes load priority over the ventilation system.

## Measure Type
ModelMeasure

## Taxonomy


## Arguments


### ERV or DCV:
Select whether to add an exhaust air energy or heat recovery system (ERV) or demand control ventilation (DCV).
**Name:** erv_or_dcv,
**Type:** Choice,
**Units:** ,
**Required:** true,
**Model Dependent:** false




