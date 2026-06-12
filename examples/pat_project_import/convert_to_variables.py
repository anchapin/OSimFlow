#!/usr/bin/env python3
"""
convert_to_variables.py

Convert OpenStudio PAT (OpenStudio-Server Analysis GUI) analysis.json
export to OSimFlow variables.yml format.

Usage:
    python convert_to_variables.py path/to/analysis.json [-o output.yml]

Example:
    python convert_to_variables.py my_project/analysis.json -o variables.yml
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Any


DISTRIBUTION_MAP = {
    "uniform": "uniform",
    "normal": "normal",
    "lognormal": "lognormal",
    "triangular": "triangular",
    "discrete": "discrete",
    "categorical": "categorical",
}


def extract_distribution(attrs: list[dict]) -> tuple[str, dict]:
    """Extract distribution type and parameters from attributes."""
    dist_name = "uniform"
    dist_params = {}

    attr_map = {a.get("name", ""): a.get("value") for a in attrs}

    # Check for explicit distribution attribute
    if "distribution" in attr_map:
        dist_name = attr_map["distribution"].lower()
    elif "variable_type" in attr_map:
        # Infer from variable type
        var_type = attr_map["variable_type"]
        if var_type == "string" or var_map.get("discrete_values"):
            dist_name = "discrete"

    # Extract distribution parameters
    if dist_name in ("uniform", "triangular"):
        # Parameters come from the main variable dict
        pass
    elif dist_name == "normal":
        if "mean" in attr_map:
            dist_params["mean"] = attr_map["mean"]
        if "std" in attr_map:
            dist_params["std"] = attr_map["std"]
    elif dist_name == "lognormal":
        if "mean" in attr_map:
            dist_params["mean"] = attr_map["mean"]
        if "std" in attr_map:
            dist_params["std"] = attr_map["std"]

    return dist_name, dist_params


def convert_analysis_to_variables(analysis: dict) -> dict:
    """Convert PAT analysis.json to OSimFlow variables.yml structure."""
    variables = []

    # Handle different analysis.json formats
    if "problem" in analysis:
        # Newer format with 'problem' key
        design_vars = analysis["problem"].get("design_variables", [])
        objectives = analysis["problem"].get("objective_functions", [])
    elif "variables" in analysis:
        # Legacy format with 'variables' key
        design_vars = analysis["variables"]
        objectives = analysis.get("objective_functions", [])
    else:
        design_vars = []
        objectives = []

    # Convert design variables
    for var in design_vars:
        name = var.get("name", var.get("display_name", "unnamed"))
        attrs = var.get("attributes", [])

        dist_name, dist_params = extract_distribution(attrs)

        var_entry = {
            "name": name,
            "distribution": dist_name,
        }

        # Add range parameters
        if "minimum" in var:
            var_entry["min"] = var["minimum"]
        if "maximum" in var:
            var_entry["max"] = var["maximum"]
        if "mean" in var:
            var_entry["mean"] = var["mean"]
        if "mode" in var:
            var_entry["mode"] = var["mode"]
        if "std" in var:
            var_entry["std"] = var["std"]

        # Add discrete values
        if dist_name == "discrete":
            if "discrete_values" in var:
                var_entry["values"] = var["discrete_values"]
            elif "values" in var:
                var_entry["values"] = var["values"]

        # Add categorical values
        if dist_name == "categorical":
            if "categories" in var:
                var_entry["categories"] = var["categories"]

        # Add description
        if "display_name" in var:
            var_entry["description"] = var["display_name"]

        variables.append(var_entry)

    # Build output structure
    result = {"variables": variables}

    # Add objectives if present
    if objectives:
        obj_list = []
        for obj in objectives:
            obj_entry = {
                "name": obj.get("name", "unnamed"),
                "direction": obj.get("objective", "minimize"),
            }
            obj_list.append(obj_entry)
        result["objectives"] = obj_list

    return result


def main():
    parser = argparse.ArgumentParser(
        description="Convert PAT analysis.json to OSimFlow variables.yml"
    )
    parser.add_argument(
        "input",
        type=Path,
        help="Path to PAT analysis.json file",
    )
    parser.add_argument(
        "-o", "--output",
        type=Path,
        default=None,
        help="Output path (default: variables.yml in same directory)",
    )
    parser.add_argument(
        "--pretty",
        action="store_true",
        default=True,
        help="Pretty-print YAML output",
    )

    args = parser.parse_args()

    if not args.input.exists():
        print(f"Error: File not found: {args.input}", file=sys.stderr)
        sys.exit(1)

    # Load analysis.json
    with open(args.input) as f:
        analysis = json.load(f)

    # Convert
    variables = convert_analysis_to_variables(analysis)

    # Determine output path
    if args.output:
        output_path = args.output
    else:
        output_path = args.input.parent / "variables.yml"

    # Write output
    import yaml
    with open(output_path, "w") as f:
        yaml.dump(variables, f, default_flow_style=False, sort_keys=False)

    print(f"Converted: {args.input}")
    print(f"Output: {output_path}")
    print(f"Variables: {len(variables.get('variables', []))}")


if __name__ == "__main__":
    main()