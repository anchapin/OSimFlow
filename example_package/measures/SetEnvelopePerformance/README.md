

###### (Automatically generated documentation)

# Set Envelope Performance

## Description
Sets the window-to-wall ratio and exterior-wall thermal resistance on every exterior wall of the model to user-supplied targets.

## Modeler Description
Iterates over all exterior walls; scales each subsurface window so its projected area equals wwr * gross wall area, and scales the wall insulation layer thickness so the assembly R-value matches wall_r_value (SI units, m^2*K/W).

## Measure Type
ModelMeasure

## Taxonomy


## Arguments


### Window-to-Wall Ratio (fraction, 0.0-1.0)

**Name:** wwr,
**Type:** Double,
**Units:** ,
**Required:** true,
**Model Dependent:** false


### Exterior Wall R-value (m^2*K/W, SI)

**Name:** wall_r_value,
**Type:** Double,
**Units:** ,
**Required:** true,
**Model Dependent:** false






