# EnergyPlus SQL Output — Schema Guide for KPI Extraction

> **Audience:** BEM practitioners and energy modelers writing custom KPI
> extractors for OSimFlow campaigns. If you are writing a BYOS script
> (`--custom_kpi_extractor`), this is your reference for querying the
> `eplusout.sql` SQLite database that EnergyPlus produces at the end of
> every simulation run.

---

## Table of Contents

1. [Overview](#overview)
2. [Connecting to the Database](#connecting-to-the-database)
3. [Core Tables and BEM Use Cases](#core-tables-and-bem-use-cases)
4. [Copy-Paste-Ready SQL Queries](#copy-paste-ready-sql-queries)
   - [Total Site EUI](#1-total-site-eui)
   - [End-Use Breakdown](#2-end-use-breakdown)
   - [Unmet Hours](#3-unmet-hours)
   - [Peak Demand](#4-peak-demand)
   - [Total Floor Area](#5-total-floor-area)
   - [Zone Temperatures (Min/Max)](#6-zone-temperatures-minmax)
   - [Annual Fuel Consumption by Meter](#7-annual-fuel-consumption-by-meter)
   - [Zone-Level Heating and Cooling Loads](#8-zone-level-heating-and-cooling-loads)
5. [Table-by-Table Reference](#table-by-table-reference)
   - [TabularDataWithStrings](#tabulardatawithstrings)
   - [Zones](#zones)
   - [EnergyTransfers](#energytransfers)
   - [MeterData](#meterdata)
   - [ReportDataDictionary](#reportdatadictionary)
   - [ReportData](#reportdata)
6. [EnergyPlus Version Differences](#energyplus-version-differences)
7. [Writing a BYOS KPI Extractor](#writing-a-byos-kpi-extractor)
8. [Tips and Common Pitfalls](#tips-and-common-pitfalls)

---

## Overview

Every EnergyPlus simulation (invoked through `openstudio.cli run -w workflow.osw`)
produces an `eplusout.sql` file in the simulation working directory. This is a
standard SQLite database containing structured simulation results — far richer
and more queryable than the flat-text `eplusout.csv` or the HTML report tables.

OSimFlow campaigns produce one `eplusout.sql` per sample. The per-sample path is:

```
${outdir}/work/sim/<sample_id>/eplusout.sql
```

The default KPI extractor (`bin/extract_kpis.py`) reads EUI from this database.
When you need additional metrics — end-use breakdowns, unmet hours, peak demand,
zone temperatures — you write a **BYOS KPI extractor** (see
[`user_scripts/README.md`](../user_scripts/README.md)) that queries this same
database.

The schema has roughly **90–110 tables** depending on the EnergyPlus version. In
practice, most BEM workflows query **5–6 tables** for the metrics that matter.
This guide focuses on those.

---

## Connecting to the Database

From Python (the language used by BYOS extractors):

```python
import sqlite3
from pathlib import Path

sql_path = Path("eplusout.sql")  # or simulation_dir / "eplusout.sql"
conn = sqlite3.connect(sql_path)
cur = conn.cursor()

# Always close when done
conn.close()
```

From the command line (ad-hoc exploration):

```bash
sqlite3 eplusout.sql ".tables"
sqlite3 eplusout.sql ".schema TabularDataWithStrings"
sqlite3 eplusout.sql "SELECT * FROM Zones LIMIT 5;"
```

> **Tip:** Use `.headers on` and `.mode column` in the SQLite CLI for readable
> output when exploring interactively.

---

## Core Tables and BEM Use Cases

| Table Name | BEM Use Case | Key Columns |
|---|---|---|
| `TabularDataWithStrings` | EUI, end uses, peak demand, unmet hours, LEED summary, component sizing, system summaries | `ReportName`, `ReportForString`, `TableName`, `RowName`, `ColumnName`, `Units`, `Value` |
| `Zones` | Floor area per zone, zone counts, zone multipliers | `ZoneIndex`, `ZoneName`, `FloorArea`, `Volume`, `Multiplier` |
| `EnergyTransfers` | Zone-level heating and cooling loads, energy transfer between zones and systems | `ZoneName`, `TransferType`, `EnergyValue` |
| `MeterData` | Time-series energy consumption by fuel type and meter | `MeterIndex`, `MeterType`, `ReportVariableDataType`, `VariableIndex` |
| `ReportDataDictionary` | Lookup table for variable names, units, and reporting frequencies — used to decode `ReportData` | `ReportDataDictionaryIndex`, `Name`, `ReportingFrequency`, `Units` |
| `ReportData` | Time-series values (temperatures, loads, etc.) for any reported variable | `ReportDataDictionaryIndex`, `TimeIndex`, `Value` |
| `Time` | Maps `TimeIndex` to simulation date/time | `TimeIndex`, `Year`, `Month`, `Day`, `Hour`, `Minute`, `EnvironmentPeriodIndex` |
| `ZoneInfo` | Extended zone metadata (not available in all EP versions) | `ZoneIndex`, `ZoneName`, `RelDirection`, `RelNorth`, `OriginX`, `OriginY`, `OriginZ` |
| `Schedules` | Named schedule data | `ScheduleIndex`, `ScheduleName`, `ScheduleType` |
| `Materials` | Construction material properties | `MaterialIndex`, `MaterialName`, `Roughness`, `Conductivity`, `Density`, `SpecificHeat` |

### How the tables relate

The most common query pattern is:

1. **`TabularDataWithStrings`** — standalone; no joins needed. Each row is a
   single cell from a tabular report, identified by the combination of
   `ReportName`, `TableName`, `RowName`, and `ColumnName`.

2. **`ReportData` + `ReportDataDictionary` + `Time`** — join on
   `ReportDataDictionaryIndex` to decode variable names, and on `TimeIndex`
   to get human-readable timestamps. This is how you extract hourly zone
   temperatures, system loads, and meter readings.

3. **`MeterData` + `ReportDataDictionary`** — join on `VariableIndex` /
   `ReportDataDictionaryIndex` to get meter names and fuel types.

---

## Copy-Paste-Ready SQL Queries

All queries below are standalone SQLite. Copy them directly into your BYOS
extractor or test them with `sqlite3 eplusout.sql`.

### 1. Total Site EUI

Returns Energy Use Intensity in MJ/m² (divide by 3.6 for kWh/m²).

```sql
SELECT Value
FROM TabularDataWithStrings
WHERE ReportName      = 'InitializationSummary'
  AND ReportForString = 'Entire Facility'
  AND TableName       = 'Site and Source Energy'
  AND RowName         = 'Total Site Energy'
  AND ColumnName      = 'Energy Per Total Building Area'
  AND Units           = 'MJ/m2';
```

> **Note:** This is the same query used by the default `bin/extract_kpis.py`.

### 2. End-Use Breakdown

Returns annual energy by end use (heating, cooling, fans, lighting, etc.) in
GJ or kWh, depending on the report configuration.

```sql
SELECT RowName, ColumnName, Value, Units
FROM TabularDataWithStrings
WHERE ReportName      = 'AnnualBuildingUtilityPerformanceSummary'
  AND ReportForString = 'Entire Facility'
  AND TableName       = 'End Uses'
  AND Value           != '0.00'
ORDER BY RowName, ColumnName;
```

Typical rows returned:

| RowName | ColumnName | Value | Units |
|---|---|---|---|
| Heating | Electricity | 12.34 | GJ |
| Cooling | Electricity | 8.76 | GJ |
| Interior Lighting | Electricity | 5.21 | GJ |
| Fans | Electricity | 3.10 | GJ |

### 3. Unmet Hours

Returns the number of hours where zone temperatures were outside the
heating or cooling setpoints.

```sql
SELECT RowName, Value
FROM TabularDataWithStrings
WHERE ReportName      = 'SystemSummary'
  AND ReportForString = 'Entire Facility'
  AND TableName       = 'Time Setpoint Not Met'
ORDER BY RowName;
```

Each zone has rows for both **During Heating** and **During Cooling**:
- `RowName = 'ZONE_NAME During Heating'` — hours below the heating setpoint.
- `RowName = 'ZONE_NAME During Cooling'` — hours above the cooling setpoint.

The `Value` column contains the count of unmet hours. LEED compliance
typically requires each zone to have fewer than 300 unmet hours for each
category.

### 4. Peak Demand

Returns the building peak electricity demand in Watts.

```sql
SELECT Value, RowName, Units
FROM TabularDataWithStrings
WHERE ReportName      = 'DemandEndUseComponentsSummary'
  AND ReportForString = 'Entire Facility'
  AND TableName       = 'End Uses'
  AND ColumnName      = 'Electricity Maximum'
ORDER BY CAST(Value AS REAL) DESC
LIMIT 10;
```

> **Alternative** — to get the overall building peak demand timestamp:

```sql
SELECT RowName, Value, Units
FROM TabularDataWithStrings
WHERE ReportName      = 'DemandEndUseComponentsSummary'
  AND ReportForString = 'Entire Facility'
  AND TableName       = 'End Uses'
  AND ColumnName      = 'Electricity Maximum';
```

### 5. Total Floor Area

```sql
SELECT SUM(FloorArea) AS total_floor_area_m2
FROM Zones;
```

For the conditioned floor area only (excluding plenums and unconditioned
zones), filter by zone naming conventions or check the
`InitializationSummary`:

```sql
SELECT Value
FROM TabularDataWithStrings
WHERE ReportName      = 'InitializationSummary'
  AND ReportForString = 'Entire Facility'
  AND TableName       = 'Building Area'
  AND RowName         = 'Total Building Area'
  AND ColumnName      = 'Area';
```

### 6. Zone Temperatures (Min/Max)

This requires joining the time-series `ReportData` with the data dictionary
to find the zone air temperature variables:

```sql
SELECT
  rdd.Name           AS variable_name,
  MIN(rd.Value)      AS min_temp_C,
  MAX(rd.Value)      AS max_temp_C,
  AVG(rd.Value)      AS avg_temp_C
FROM ReportData rd
JOIN ReportDataDictionary rdd
  ON rd.ReportDataDictionaryIndex = rdd.ReportDataDictionaryIndex
WHERE rdd.Name LIKE 'Zone Mean Air Temperature'
GROUP BY rdd.Name
ORDER BY rdd.Name;
```

For **hourly** values for a specific zone:

```sql
SELECT
  t.Month,
  t.Day,
  t.Hour,
  rd.Value AS temperature_C
FROM ReportData rd
JOIN ReportDataDictionary rdd
  ON rd.ReportDataDictionaryIndex = rdd.ReportDataDictionaryIndex
JOIN Time t
  ON rd.TimeIndex = t.TimeIndex
WHERE rdd.Name = 'Zone Mean Air Temperature'
  AND rdd.Name LIKE '%YOUR_ZONE_NAME%'
ORDER BY t.Month, t.Day, t.Hour;
```

> **Tip:** The variable name format is `Zone Mean Air Temperature` (EP 22.x+)
> or may include the zone name in older versions. Check
> `ReportDataDictionary` for exact names:
> ```sql
> SELECT DISTINCT Name
> FROM ReportDataDictionary
> WHERE Name LIKE '%Temperature%';
> ```

### 7. Annual Fuel Consumption by Meter

```sql
SELECT
  rdd.Name    AS meter_name,
  SUM(rd.Value) AS annual_total,
  rdd.Units   AS units
FROM ReportData rd
JOIN ReportDataDictionary rdd
  ON rd.ReportDataDictionaryIndex = rdd.ReportDataDictionaryIndex
WHERE rdd.Name LIKE 'Electricity:Facility'
   OR rdd.Name LIKE 'NaturalGas:Facility'
GROUP BY rdd.Name, rdd.Units;
```

### 8. Zone-Level Heating and Cooling Loads

```sql
SELECT
  ZoneName,
  TransferType,
  EnergyValue
FROM EnergyTransfers
WHERE TransferType IN ('Heating', 'Cooling')
ORDER BY ZoneName, TransferType;
```

> **Note:** `EnergyTransfers` may not be populated in all EnergyPlus versions
> or configurations. If it returns empty, fall back to querying
> `TabularDataWithStrings` with `TableName = 'Zone Sensible Heating and Cooling'`.

---

## Table-by-Table Reference

### TabularDataWithStrings

The workhorse table. EnergyPlus writes all standard tabular reports to this
single flat table. Each row represents one cell in a report table, identified
by a composite key of report name, table name, row, and column.

| Column | Type | Description |
|---|---|---|
| `ReportName` | TEXT | Name of the EnergyPlus report (e.g., `AnnualBuildingUtilityPerformanceSummary`, `SystemSummary`, `InitializationSummary`). |
| `ReportForString` | TEXT | Scope — almost always `'Entire Facility'`. |
| `TableName` | TEXT | The table within the report (e.g., `'End Uses'`, `'Site and Source Energy'`, `'Time Setpoint Not Met'`). |
| `RowName` | TEXT | Row label (e.g., `'Heating'`, `'Total Site Energy'`, zone names). |
| `ColumnName` | TEXT | Column label (e.g., `'Electricity'`, `'Energy Per Total Building Area'`, `'Maximum'`). |
| `Units` | TEXT | Units string (e.g., `'GJ'`, `'MJ/m2'`, `'W'`, `'hr'`). May be empty for dimensionless values. |
| `Value` | TEXT | The cell value, stored as a **string** — cast with `CAST(Value AS REAL)` for numeric operations. |

**Common `ReportName` values:**

| ReportName | Content |
|---|---|
| `InitializationSummary` | Building area, zone counts, construction details |
| `AnnualBuildingUtilityPerformanceSummary` | End uses, EUI, peak demand, load profiles |
| `DemandEndUseComponentsSummary` | Peak demand breakdown by end use |
| `SystemSummary` | Unmet hours, equipment sizing, coil details |
| `ComponentSizingSummary` | Autosized component capacities |
| `LEEDsummary` | LEED EAc1 compliance metrics (if LEED output requested) |

### Zones

One row per thermal zone defined in the model.

| Column | Type | Description |
|---|---|---|
| `ZoneIndex` | INTEGER | Primary key (0-based). |
| `ZoneName` | TEXT | Zone name from the IDF/OSM. |
| `FloorArea` | REAL | Zone floor area in m². |
| `Volume` | REAL | Zone volume in m³. |
| `Multiplier` | REAL | Zone multiplier (1.0 for unique zones; >1 for repeated floors/zones). |
| `ZoneGroup` | TEXT | Zone group name (if assigned). |
| `CeilingHeight` | REAL | Ceiling height in m. |
| `ListofZoneGroups` | TEXT | Comma-separated group names. |

**Useful query — zone count and total area:**

```sql
SELECT
  COUNT(*)          AS n_zones,
  SUM(FloorArea)    AS total_area_m2,
  AVG(FloorArea)    AS avg_zone_area_m2
FROM Zones;
```

### EnergyTransfers

Energy transfer records between zones and HVAC systems. Availability depends
on the EnergyPlus version and output configuration.

| Column | Type | Description |
|---|---|---|
| `ZoneName` | TEXT | Zone identifier. |
| `TransferType` | TEXT | Type of transfer (`'Heating'`, `'Cooling'`, `'Ventilation'`, etc.). |
| `EnergyValue` | REAL | Energy transferred (Joules by default). |

### MeterData

Index table for energy meters. Each meter tracks a fuel type (electricity,
natural gas, etc.) at a specific scope (facility, building, zone).

| Column | Type | Description |
|---|---|---|
| `MeterIndex` | INTEGER | Primary key. |
| `MeterType` | TEXT | Fuel type (e.g., `'Electricity'`, `'NaturalGas'`, `'Propane'`). |
| `ReportVariableDataType` | TEXT | Data type (`'Summed'` for cumulative, `'Averaged'` for instantaneous). |
| `VariableIndex` | INTEGER | Foreign key into `ReportDataDictionary`. |
| `ResourceType` | TEXT | Broader category (`'Electricity'`, `'Gas'`, `'DistrictCooling'`, etc.). |
| `EndUseCategory` | TEXT | End use (e.g., `'InteriorLights'`, `'Heating'`, `'Cooling'`). |
| `Units` | TEXT | Units (typically `'J'` for energy meters). |

### ReportDataDictionary

Lookup table that decodes variable indices into human-readable names, units,
and reporting frequencies. Essential for any time-series query.

| Column | Type | Description |
|---|---|---|
| `ReportDataDictionaryIndex` | INTEGER | Primary key; referenced by `ReportData`. |
| `Name` | TEXT | Full variable name (e.g., `'Zone Mean Air Temperature'`, `'Electricity:Facility'`). |
| `ReportingFrequency` | TEXT | Time resolution (`'Hourly'`, `'Timestep'`, `'Daily'`, `'Monthly'`, `'RunPeriod'`). |
| `Units` | TEXT | Units (e.g., `'C'`, `'W'`, `'J'`, `'m3/s'`). |
| `Type` | TEXT | Data type hint (`'Real'`, `'Integer'`). |
| `KeyName` | TEXT | Object name (e.g., zone name, meter name). |

### ReportData

Time-series values. One row per variable per timestep. Always join with
`ReportDataDictionary` and `Time` for meaningful output.

| Column | Type | Description |
|---|---|---|
| `ReportDataDictionaryIndex` | INTEGER | Foreign key into `ReportDataDictionary`. |
| `TimeIndex` | INTEGER | Foreign key into `Time`. |
| `Value` | REAL | The reported value. |

> **Caution:** This table can be very large (millions of rows for hourly
> data across hundreds of variables). Always filter by
> `ReportDataDictionaryIndex` rather than scanning the full table.

### Time

Maps integer time indices to calendar date/time.

| Column | Type | Description |
|---|---|---|
| `TimeIndex` | INTEGER | Primary key. |
| `Year` | INTEGER | Simulation year. |
| `Month` | INTEGER | Month (1–12). |
| `Day` | INTEGER | Day of month (1–31). |
| `Hour` | INTEGER | Hour (1–24 in EnergyPlus convention). |
| `Minute` | INTEGER | Minute (0–59). |
| `EnvironmentPeriodIndex` | INTEGER | Links to `EnvironmentPeriods` (design day vs. run period). |

---

## EnergyPlus Version Differences

The `eplusout.sql` schema evolves between EnergyPlus releases. Here are the
major differences that affect BEM queries.

### EP 9.x (OpenStudio 3.x) vs. EP 22.x / 23.x

| Aspect | EP 9.x (9.2–9.6) | EP 22.x / 23.x (22.1–23.2) |
|---|---|---|
| **Total tables** | ~90 | ~100–110 |
| `TabularDataWithStrings` column `Value` | Present, stored as text | Same — no change |
| `ReportDataDictionary.Name` format | `'ZONE NAME:Zone Mean Air Temperature'` (prefixed with zone name) | `'Zone Mean Air Temperature'` — zone name moved to `KeyName` column |
| `EnergyTransfers` table | May be empty depending on output requests | More consistently populated |
| `ZoneInfo` table | Not available | Present with extended metadata |
| `Schedules` table | Basic | Expanded with schedule type classification |
| `EnvironmentPeriods` table | Present | Present; `EnvironmentName` format slightly different |
| LEED summary report | `'LEEDsummary'` | `'LEEDsummary'` (unchanged) |

### Key migration pattern

If you are querying zone-level time-series data and need to support both old
and new EnergyPlus versions, use this pattern:

```sql
-- Works in both EP 9.x and 22.x+
SELECT
  rdd.Name     AS variable_name,
  rdd.KeyName  AS zone_name,
  rd.Value     AS value,
  rdd.Units    AS units
FROM ReportData rd
JOIN ReportDataDictionary rdd
  ON rd.ReportDataDictionaryIndex = rdd.ReportDataDictionaryIndex
WHERE rdd.Name LIKE '%Mean Air Temperature%';
```

### OSimFlow version pinning

OSimFlow pins the EnergyPlus version via the container tag
(`--openstudio_version`). The mapping between OpenStudio versions and
EnergyPlus versions is:

| OpenStudio | EnergyPlus |
|---|---|
| 3.11.0 | 22.1 |
| 3.11.0 | 22.2 |
| 3.6.0 | 23.1 |
| 3.7.0 | 23.2 |

See [`docs/openstudio-image-distribution.md`](openstudio-image-distribution.md)
for the full supported version matrix.

---

## Writing a BYOS KPI Extractor

OSimFlow lets you provide a custom KPI extraction script via
`--custom_kpi_extractor user_scripts/my_kpis.py`. The script must define a
function named `extract_kpis` that receives a context dict and returns a
dict of KPI name → value pairs.

### Minimal example

```python
# user_scripts/detailed_kpis.py
"""Custom KPI extractor: EUI + end-use breakdown + unmet hours."""

from pathlib import Path
import sqlite3


def extract_kpis(ctx: dict) -> dict:
    simulation_dir = ctx["simulation_dir"]
    sample_id = ctx["sample_id"]
    sql_path = Path(simulation_dir) / "eplusout.sql"

    if not sql_path.exists():
        return {"error": "no eplusout.sql"}

    kpis = {}
    conn = sqlite3.connect(sql_path)
    try:
        cur = conn.cursor()

        # EUI (MJ/m² → kWh/m²)
        cur.execute("""
            SELECT Value FROM TabularDataWithStrings
            WHERE ReportName = 'InitializationSummary'
              AND TableName = 'Site and Source Energy'
              AND RowName = 'Total Site Energy'
              AND ColumnName = 'Energy Per Total Building Area'
              AND Units = 'MJ/m2'
        """)
        row = cur.fetchone()
        if row:
            kpis["eui_kwh_m2_yr"] = float(row[0]) / 3.6

        # Total unmet hours (heating + cooling, all zones)
        cur.execute("""
            SELECT SUM(CAST(Value AS REAL))
            FROM TabularDataWithStrings
            WHERE ReportName = 'SystemSummary'
              AND TableName = 'Time Setpoint Not Met'
        """)
        row = cur.fetchone()
        if row and row[0] is not None:
            kpis["total_unmet_hours"] = float(row[0])

        # Heating electricity (GJ)
        cur.execute("""
            SELECT Value FROM TabularDataWithStrings
            WHERE ReportName = 'AnnualBuildingUtilityPerformanceSummary'
              AND TableName = 'End Uses'
              AND RowName = 'Heating'
              AND ColumnName = 'Electricity'
        """)
        row = cur.fetchone()
        if row:
            kpis["heating_electricity_gj"] = float(row[0])

    finally:
        conn.close()

    return kpis
```

Then run:

```bash
osimflow run \
  --executor local \
  --custom_kpi_extractor user_scripts/detailed_kpis.py \
  --input_variables variables.yml \
  --template_sim_package ./example_package \
  --n_samples 10 \
  --outdir ./results
```

The returned dict is written to the per-sample KPI JSON alongside the
default extractor output. See [`user_scripts/README.md`](../user_scripts/README.md)
for the full BYOS interface specification.

> **Cross-reference:** The BYOS examples gallery (issue #49) will provide
> additional ready-to-use extractors for common BEM workflows. Check the
> `user_scripts/examples/` directory once that issue is resolved.

---

## Tips and Common Pitfalls

### `Value` is always text

The `TabularDataWithStrings.Value` column stores all values as **text strings**,
even for numeric data. Always cast:

```sql
-- Good
SELECT CAST(Value AS REAL) FROM TabularDataWithStrings WHERE ...;

-- Bad (string comparison gives wrong ordering)
SELECT Value FROM TabularDataWithStrings WHERE ... ORDER BY Value DESC;
```

### Report names are exact strings

The `ReportName`, `TableName`, `RowName`, and `ColumnName` fields are
case-sensitive and exact-match. A typo will silently return zero rows.
Use this diagnostic query to discover valid values:

```sql
-- List all available report names
SELECT DISTINCT ReportName FROM TabularDataWithStrings;

-- List all tables within a report
SELECT DISTINCT TableName
FROM TabularDataWithStrings
WHERE ReportName = 'AnnualBuildingUtilityPerformanceSummary';

-- List all rows within a table
SELECT DISTINCT RowName
FROM TabularDataWithStrings
WHERE ReportName = 'AnnualBuildingUtilityPerformanceSummary'
  AND TableName = 'End Uses';
```

### ReportData can be huge

For a typical building with 50 zones, hourly reporting, and 20+ output
variables, `ReportData` can exceed **5 million rows**. Always:

1. Filter by `ReportDataDictionaryIndex` (never scan the whole table).
2. Aggregate in SQL rather than loading all rows into Python.
3. Consider using `ReportingFrequency = 'Monthly'` or `'RunPeriod'` for
   summary metrics.

### Zero-value rows in TabularDataWithStrings

End-use queries often return `"0.00"` for fuel/end-use combinations that
don't exist in the model. Filter these out:

```sql
SELECT * FROM TabularDataWithStrings
WHERE ...
  AND Value != '0.00'
  AND Value != '0';
```

### Multiple run periods

If the model has design-day simulations and an annual run, the database
contains data for all periods. Use `EnvironmentPeriodIndex` in the `Time`
table to filter:

```sql
SELECT DISTINCT EnvironmentPeriodIndex, EnvironmentName
FROM EnvironmentPeriods;
```

Annual data is typically the largest `EnvironmentPeriodIndex` value.

### Floor area vs. conditioned floor area

`SUM(Zones.FloorArea)` gives the **total** modeled floor area, including
storage rooms, plenums, and unconditioned spaces. For conditioned-area EUI
denominators, prefer the value from `InitializationSummary`:

```sql
SELECT Value FROM TabularDataWithStrings
WHERE ReportName = 'InitializationSummary'
  AND TableName = 'Building Area'
  AND RowName = 'Net Conditioned Building Area';
```

### MJ/m² to kWh/m² conversion

EnergyPlus reports EUI in MJ/m². The conversion factor is **1 MJ = 1/3.6 kWh**
(or equivalently, divide by 3.6). The default extractor in
`bin/extract_kpis.py` performs this conversion:

```python
eui_kwh = eui_mj / 3.6
```

For kBtu/ft², multiply MJ/m² by 0.0888.

---

## Source References

| Component | Source File |
|---|---|
| Default KPI extractor | `bin/extract_kpis.py` |
| BYOS loader | `osimflow/byos.py` (planned) |
| SampleTrace schema | `osimflow/monitoring.py` |
| BYOS interface spec | `user_scripts/README.md` |
| PRD — KPI extraction | `docs/OSimFlow.md` §4.2 |
| OpenStudio version matrix | `docs/openstudio-image-distribution.md` |
