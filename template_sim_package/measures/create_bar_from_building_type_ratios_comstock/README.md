

###### (Automatically generated documentation)

# Create Bar From Building Type Ratios

## Description
Creates one or more rectangular building elements based on space type ratios of selected mix of building types, along with user arguments that describe the desired geometry characteristics.

## Modeler Description
The building floor area can be described as a footprint size or as a total building area. The shape can be described by its aspect ratio or can be defined as a set width. Because this measure contains both DOE and DEER inputs, care needs to be taken to choose a template compatable with the selected building types. See readme document for additional guidance.

## Measure Type
ModelMeasure

## Taxonomy


## Arguments


### Primary Building Type

**Name:** bldg_type_a,
**Type:** Choice,
**Units:** ,
**Required:** true,
**Model Dependent:** false

### Primary Building Subtype

**Name:** bldg_subtype_a,
**Type:** Choice,
**Units:** ,
**Required:** true,
**Model Dependent:** false

### Primary Building Type Number of Units
Number of units argument not currently used by this measure
**Name:** bldg_type_a_num_units,
**Type:** Integer,
**Units:** ,
**Required:** true,
**Model Dependent:** false

### Building Type B

**Name:** bldg_type_b,
**Type:** Choice,
**Units:** ,
**Required:** true,
**Model Dependent:** false

### Building Subtype B

**Name:** bldg_subtype_b,
**Type:** Choice,
**Units:** ,
**Required:** true,
**Model Dependent:** false

### Building Type B Fraction of Building Floor Area

**Name:** bldg_type_b_fract_bldg_area,
**Type:** Double,
**Units:** ,
**Required:** true,
**Model Dependent:** false

### Building Type B Number of Units
Number of units argument not currently used by this measure
**Name:** bldg_type_b_num_units,
**Type:** Integer,
**Units:** ,
**Required:** true,
**Model Dependent:** false

### Building Type C

**Name:** bldg_type_c,
**Type:** Choice,
**Units:** ,
**Required:** true,
**Model Dependent:** false

### Building Subtype C

**Name:** bldg_subtype_c,
**Type:** Choice,
**Units:** ,
**Required:** true,
**Model Dependent:** false

### Building Type C Fraction of Building Floor Area

**Name:** bldg_type_c_fract_bldg_area,
**Type:** Double,
**Units:** ,
**Required:** true,
**Model Dependent:** false

### Building Type C Number of Units
Number of units argument not currently used by this measure
**Name:** bldg_type_c_num_units,
**Type:** Integer,
**Units:** ,
**Required:** true,
**Model Dependent:** false

### Building Type D

**Name:** bldg_type_d,
**Type:** Choice,
**Units:** ,
**Required:** true,
**Model Dependent:** false

### Building Subtype D

**Name:** bldg_subtype_d,
**Type:** Choice,
**Units:** ,
**Required:** true,
**Model Dependent:** false

### Building Type D Fraction of Building Floor Area

**Name:** bldg_type_d_fract_bldg_area,
**Type:** Double,
**Units:** ,
**Required:** true,
**Model Dependent:** false

### Building Type D Number of Units
Number of units argument not currently used by this measure
**Name:** bldg_type_d_num_units,
**Type:** Integer,
**Units:** ,
**Required:** true,
**Model Dependent:** false

### Single Floor Area
Non-zero value will fix the single floor area, overriding a user entry for Total Building Floor Area
**Name:** single_floor_area,
**Type:** Double,
**Units:** ft^2,
**Required:** true,
**Model Dependent:** false

### Total Building Floor Area

**Name:** total_bldg_floor_area,
**Type:** Double,
**Units:** ft^2,
**Required:** true,
**Model Dependent:** false

### Typical Floor to Floor Height
Selecting a typical floor height of 0 will trigger a smart building type default.
**Name:** floor_height,
**Type:** Double,
**Units:** ft,
**Required:** true,
**Model Dependent:** false

### Enable Custom Height Bar Application
This is argument value is only relevant when smart default floor to floor height is used for a building type that has spaces with custom heights.
**Name:** custom_height_bar,
**Type:** Boolean,
**Units:** ,
**Required:** true,
**Model Dependent:** false

### Number of Stories Above Grade

**Name:** num_stories_above_grade,
**Type:** Double,
**Units:** ,
**Required:** true,
**Model Dependent:** false

### Number of Stories Below Grade

**Name:** num_stories_below_grade,
**Type:** Integer,
**Units:** ,
**Required:** true,
**Model Dependent:** false

### Building Rotation
Set Building Rotation off of North (positive value is clockwise). Rotation applied after geometry generation. Values greater than +/- 45 will result in aspect ratio and party wall orientations that do not match cardinal directions of the inputs.
**Name:** building_rotation,
**Type:** Double,
**Units:** Degrees,
**Required:** true,
**Model Dependent:** false

### Target Standard

**Name:** template,
**Type:** Choice,
**Units:** ,
**Required:** true,
**Model Dependent:** false

### Ratio of North/South Facade Length Relative to East/West Facade Length
Selecting an aspect ratio of 0 will trigger a smart building type default. Aspect ratios less than one are not recommended for sliced bar geometry, instead rotate building and use a greater than 1 aspect ratio.
**Name:** ns_to_ew_ratio,
**Type:** Double,
**Units:** ,
**Required:** true,
**Model Dependent:** false

### Perimeter Multiplier
Selecting a value of 0 will trigger a smart building type default. This represents a multiplier for the building perimeter relative to the perimeter of a rectangular building that meets the area and aspect ratio inputs. Other than the smart default of 0.0 this argument should have a value of 1.0 or higher and is only applicable Multiple Space Types - Individual Stories Sliced division method.
**Name:** perim_mult,
**Type:** Double,
**Units:** ,
**Required:** true,
**Model Dependent:** false

### Bar Width
Non-zero value will fix the building width, overriding user entry for Perimeter Multiplier. NS/EW Aspect Ratio may be limited based on target width.
**Name:** bar_width,
**Type:** Double,
**Units:** ft,
**Required:** true,
**Model Dependent:** false

### Bar Separation Distance Multiplier
Multiplier of separation between bar elements relative to building height.
**Name:** bar_sep_dist_mult,
**Type:** Double,
**Units:** ,
**Required:** true,
**Model Dependent:** false

### Window to Wall Ratio
Selecting a window to wall ratio of 0 will trigger a smart building type default.
**Name:** wwr,
**Type:** Double,
**Units:** ,
**Required:** true,
**Model Dependent:** false

### Fraction of Exterior Wall Area with Adjacent Structure
This will impact how many above grade exterior walls are modeled with adiabatic boundary condition.
**Name:** party_wall_fraction,
**Type:** Double,
**Units:** ,
**Required:** true,
**Model Dependent:** false

### Number of North facing stories with party wall
This will impact how many above grade exterior north walls are modeled with adiabatic boundary condition. If this is less than the number of above grade stoes, upper flor will reamin exterior
**Name:** party_wall_stories_north,
**Type:** Integer,
**Units:** ,
**Required:** true,
**Model Dependent:** false

### Number of South facing stories with party wall
This will impact how many above grade exterior south walls are modeled with adiabatic boundary condition. If this is less than the number of above grade stoes, upper flor will reamin exterior
**Name:** party_wall_stories_south,
**Type:** Integer,
**Units:** ,
**Required:** true,
**Model Dependent:** false

### Number of East facing stories with party wall
This will impact how many above grade exterior east walls are modeled with adiabatic boundary condition. If this is less than the number of above grade stoes, upper flor will reamin exterior
**Name:** party_wall_stories_east,
**Type:** Integer,
**Units:** ,
**Required:** true,
**Model Dependent:** false

### Number of West facing stories with party wall
This will impact how many above grade exterior west walls are modeled with adiabatic boundary condition. If this is less than the number of above grade stoes, upper flor will reamin exterior
**Name:** party_wall_stories_west,
**Type:** Integer,
**Units:** ,
**Required:** true,
**Model Dependent:** false

### Neighbor height specification method
Absolute will use heights specified by cardinal direction. Relative will use height offset.
**Name:** neighbor_height_method,
**Type:** Choice,
**Units:** ,
**Required:** true,
**Model Dependent:** false

### Height of this building relative to neighboring buildings relative
Negative number means neighbors are taller than this building, Positive number means neighbors are shorter than this building.
**Name:** building_height_relative_to_neighbors,
**Type:** Double,
**Units:** ft,
**Required:** true,
**Model Dependent:** false

### Height of neighboring building to the north
If greater than zero, adds a shade the width of the building with the specified height to the north of the building. Only used if Neighbor height specification method is individual. Use party walls for attached buildings. Neighbors rotate with building.
**Name:** neighbor_height_north,
**Type:** Double,
**Units:** ft,
**Required:** true,
**Model Dependent:** false

### Height of neighboring building to the south
If greater than zero, adds a shade the width of the building with the specified height to the south of the building. Only used if Neighbor height specification method is individual. Use party walls for attached buildings. Neighbors rotate with building.
**Name:** neighbor_height_south,
**Type:** Double,
**Units:** ft,
**Required:** true,
**Model Dependent:** false

### Height of neighboring building to the east
If greater than zero, adds a shade the width of the building with the specified height to the east of the building. Only used if Neighbor height specification method is individual. Use party walls for attached buildings. Neighbors rotate with building.
**Name:** neighbor_height_east,
**Type:** Double,
**Units:** ft,
**Required:** true,
**Model Dependent:** false

### Height of neighboring building to the west
If greater than zero, adds a shade the width of the building with the specified height to the west of the building. Only used if Neighbor height specification method is individual. Use party walls for attached buildings. Neighbors rotate with building.
**Name:** neighbor_height_west,
**Type:** Double,
**Units:** ft,
**Required:** true,
**Model Dependent:** false

### Distance of neighboring building to the north
If greater than zero, puts the shade at a specified distance to the north of the building.
**Name:** neighbor_offset_north,
**Type:** Double,
**Units:** ft,
**Required:** true,
**Model Dependent:** false

### Distance of neighboring building to the south
If greater than zero, puts the shade at a specified distance to the south of the building.
**Name:** neighbor_offset_south,
**Type:** Double,
**Units:** ft,
**Required:** true,
**Model Dependent:** false

### Distance of neighboring building to the east
If greater than zero, puts the shade at a specified distance to the east of the building.
**Name:** neighbor_offset_east,
**Type:** Double,
**Units:** ft,
**Required:** true,
**Model Dependent:** false

### Distance of neighboring building to the west
If greater than zero, puts the shade at a specified distance to the west of the building.
**Name:** neighbor_offset_west,
**Type:** Double,
**Units:** ft,
**Required:** true,
**Model Dependent:** false

### Is the Bottom Story Exposed to Ground
This should be true unless you are modeling a partial building which doesn't include the lowest story. The bottom story floor will have an adiabatic boundary condition when false.
**Name:** bottom_story_ground_exposed_floor,
**Type:** Boolean,
**Units:** ,
**Required:** true,
**Model Dependent:** false

### Is the Top Story an Exterior Roof
This should be true unless you are modeling a partial building which doesn't include the highest story. The top story ceiling will have an adiabatic boundary condition when false.
**Name:** top_story_exterior_exposed_roof,
**Type:** Boolean,
**Units:** ,
**Required:** true,
**Model Dependent:** false

### Calculation Method for Story Multiplier

**Name:** story_multiplier,
**Type:** Choice,
**Units:** ,
**Required:** true,
**Model Dependent:** false

### Make Mid Story Floor Surfaces Adibatic
If set to true, this will skip surface intersection and make mid story floors and celings adiabiatc, not just at multiplied gaps.
**Name:** make_mid_story_surfaces_adiabatic,
**Type:** Boolean,
**Units:** ,
**Required:** true,
**Model Dependent:** false

### Division Method for Bar Space Types
To use perimeter multiplier greater than 1 selected Multiple Space Types - Individual Stories Sliced.
**Name:** bar_division_method,
**Type:** Choice,
**Units:** ,
**Required:** true,
**Model Dependent:** false

### Double Loaded Corridor
Add double loaded corridor for building types that have a defined circulation space type, to the selected space types.
**Name:** double_loaded_corridor,
**Type:** Choice,
**Units:** ,
**Required:** true,
**Model Dependent:** false

### Choose Space Type Sorting Method

**Name:** space_type_sort_logic,
**Type:** Choice,
**Units:** ,
**Required:** true,
**Model Dependent:** false

### Use Upstream Argument Values
When true this will look for arguments or registerValues in upstream measures that match arguments from this measure, and will use the value from the upstream measure in place of what is entered for this measure.
**Name:** use_upstream_args,
**Type:** Boolean,
**Units:** ,
**Required:** true,
**Model Dependent:** false

### Climate Zone.

**Name:** climate_zone,
**Type:** Choice,
**Units:** ,
**Required:** true,
**Model Dependent:** false





