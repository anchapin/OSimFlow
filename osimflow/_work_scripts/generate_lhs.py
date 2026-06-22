#!/usr/bin/env python3
"""generate_lhs.py — Latin Hypercube Sampling for OSimFlow.

Reads `variables.yml` and emits N parameter sets as JSON. See docs/OSimFlow.md
§4.2 (PROCESS_GENERATE_LHS_SAMPLES) for the contract.

Supports conditional/dependent sampling: variables whose distribution depends
on the value of another variable (e.g. cooling efficiency constrained by HVAC
system type). Conditional variables are resolved in dependency order after
independent variables are sampled.
"""

import argparse
import json
import logging
import math
import sys
from collections import deque
from pathlib import Path
from typing import Any

import scipy.stats
import scipy.stats.qmc
import yaml

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("generate_lhs")

SUPPORTED_DISTRIBUTIONS = (
    "uniform",
    "lognormal",
    "normal",
    "triangular",
    "beta",
    "gamma",
    "exponential",
    "discrete",
    "categorical",
    "conditional",
)


def _apply_distribution(u: float, dist: str, params: dict[str, Any]) -> float | dict[str, Any]:
    """Map a unit-sample value *u* ∈ [0, 1] through the named distribution PPF.

    Parameters
    ----------
    u : float
        Uniform sample in [0, 1] from the LHS engine.
    dist : str
        Distribution name (e.g. ``"uniform"``, ``"normal"``).
    params : dict
        Distribution-specific parameters read from ``variables.yml``.

    Returns
    -------
    float | dict[str, Any]
        The transformed sample value.  Continuous distributions return a
        ``float``.  ``discrete`` returns the raw value from the list.  ``categorical``
        returns a ``dict`` with ``label``, ``index``, and optionally ``mapping`` keys.

    Raises
    ------
    ValueError
        If *dist* is not one of :data:`SUPPORTED_DISTRIBUTIONS`.
    KeyError
        If a required parameter is missing from *params*.
    """
    if dist == "uniform":
        min_val = params["min"]
        max_val = params["max"]
        return float(min_val + u * (max_val - min_val))

    if dist == "lognormal":
        mean = params["mean"]
        sigma = params["sigma"]
        return float(scipy.stats.lognorm.ppf(u, s=sigma, scale=math.exp(mean)))

    if dist == "normal":
        mean = params["mean"]
        sigma = params["sigma"]
        return float(scipy.stats.norm.ppf(u, loc=mean, scale=sigma))

    if dist == "triangular":
        left = params["min"]
        right = params["max"]
        # scipy triang c-parameter is the normalised peak position.
        # OSimFlow does not expose a peak/mode parameter, so we default
        # to a symmetric triangle (c = 0.5).
        mode = params.get("mode")
        if mode is not None:
            c = (mode - left) / (right - left)
        else:
            c = 0.5
        return float(scipy.stats.triang.ppf(u, c, loc=left, scale=right - left))

    if dist == "beta":
        alpha = params["alpha"]
        beta_val = params["beta"]
        loc = params.get("loc", 0.0)
        scale = params.get("scale", 1.0)
        return float(scipy.stats.beta.ppf(u, a=alpha, b=beta_val, loc=loc, scale=scale))

    if dist == "gamma":
        alpha = params["alpha"]
        loc = params.get("loc", 0.0)
        scale = params.get("scale", 1.0)
        return float(scipy.stats.gamma.ppf(u, a=alpha, loc=loc, scale=scale))

    if dist == "exponential":
        rate = params["rate"]
        return float(scipy.stats.expon.ppf(u, scale=1 / rate))

    if dist == "discrete":
        raw_values = params.get("values")
        if not raw_values or not isinstance(raw_values, list):
            raise ValueError("discrete distribution requires a non-empty 'values' list")
        values: list[Any] = raw_values
        idx = min(int(u * len(values)), len(values) - 1)
        result: float | dict[str, Any] = values[idx]
        return result

    if dist == "categorical":
        raw_values = params.get("values")
        if not raw_values or not isinstance(raw_values, list):
            raise ValueError("categorical distribution requires a non-empty 'values' list")
        values = raw_values
        raw_mapping = params.get("mapping", {})
        idx = min(int(u * len(values)), len(values) - 1)
        label: str = str(values[idx])
        # Return structured output: label + resolved mapping (if any).
        if raw_mapping and isinstance(raw_mapping, dict):
            mapping: dict[str, Any] = raw_mapping
            resolved: Any = mapping.get(label)
            return {"label": label, "index": idx, "mapping": resolved}
        return {"label": label, "index": idx}

    raise ValueError(
        f"unsupported distribution {dist!r}; choose from {', '.join(SUPPORTED_DISTRIBUTIONS)}"
    )


def _resolve_label(value: Any) -> str:
    """Extract the string label from a potentially structured categorical value.

    If *value* is a ``dict`` with a ``"label"`` key (categorical output), return
    the label. Otherwise stringify the value.
    """
    if isinstance(value, dict) and "label" in value:
        return str(value["label"])
    return str(value)


def _validate_dependency_graph(variables: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Validate the dependency graph and return variables in topological order.

    Performs cycle detection via Kahn's algorithm and ensures every
    ``depends_on`` reference points to a defined variable.

    Parameters
    ----------
    variables : list[dict]
        Variable definitions from ``variables.yml``.

    Returns
    -------
    list[dict]
        Variables sorted in dependency order. Independent variables come first,
        followed by conditional variables whose parents have already been
        resolved.

    Raises
    ------
    ValueError
        If a circular dependency is detected or a ``depends_on`` target is
        missing.
    """
    var_by_name: dict[str, dict[str, Any]] = {v["name"]: v for v in variables}
    all_names = set(var_by_name.keys())

    in_degree: dict[str, int] = {name: 0 for name in all_names}
    children: dict[str, list[str]] = {name: [] for name in all_names}

    for v in variables:
        if v.get("distribution") == "conditional":
            parent = v.get("depends_on")
            if not parent:
                raise ValueError(
                    f"variable '{v['name']}' has distribution='conditional' but no 'depends_on' key"
                )
            if parent not in all_names:
                raise ValueError(
                    f"variable '{v['name']}' depends_on '{parent}', "
                    f"which is not defined in variables"
                )
            in_degree[v["name"]] += 1
            children[parent].append(v["name"])

    queue: deque[str] = deque(n for n, deg in in_degree.items() if deg == 0)
    ordered: list[str] = []

    while queue:
        name = queue.popleft()
        ordered.append(name)
        for child in children[name]:
            in_degree[child] -= 1
            if in_degree[child] == 0:
                queue.append(child)

    if len(ordered) != len(variables):
        remaining = all_names - set(ordered)
        raise ValueError(f"circular dependency detected among variables: {sorted(remaining)}")

    return [var_by_name[name] for name in ordered]


def _resolve_conditional(
    u: float,
    var: dict[str, Any],
    resolved: dict[str, Any],
) -> Any:
    """Resolve a conditional variable given the already-sampled *resolved* dict.

    Parameters
    ----------
    u : float
        Uniform sample in [0, 1] for this variable's LHS dimension.
    var : dict
        The conditional variable definition (must have ``depends_on`` and
        ``conditions``).
    resolved : dict
        Already-resolved variable values (parents must be present).

    Returns
    -------
    Any
        The sampled value from the matching conditional distribution.

    Raises
    ------
    ValueError
        If the parent value does not match any condition key.
    """
    parent_name = var["depends_on"]
    parent_value = resolved[parent_name]
    parent_key = _resolve_label(parent_value)

    conditions = var.get("conditions")
    if not conditions or not isinstance(conditions, dict):
        raise ValueError(
            f"conditional variable '{var['name']}' requires a non-empty "
            f"'conditions' dict mapping parent values to distributions"
        )

    if parent_key not in conditions:
        raise ValueError(
            f"conditional variable '{var['name']}': parent "
            f"'{parent_name}' value '{parent_key}' has no matching "
            f"condition key. Available keys: {sorted(conditions.keys())}"
        )

    sub_dist_spec = conditions[parent_key]
    sub_dist = sub_dist_spec["distribution"]
    return _apply_distribution(u, sub_dist, sub_dist_spec)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--variables_yml", required=True, type=Path)
    parser.add_argument("--n_samples", required=True, type=int)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()

    args.out.parent.mkdir(parents=True, exist_ok=True)

    if args.n_samples == 0:
        args.out.write_text(json.dumps({"n_samples": 0, "variables": [], "samples": []}, indent=2))
        return 0

    with args.variables_yml.open() as f:
        config = yaml.safe_load(f)
    variables = config.get("variables", [])
    if not variables:
        for i in range(args.n_samples):
            param_file = args.out.parent / f"{i + 1:04d}.params.json"
            param_file.write_text(json.dumps({}, indent=2))
        args.out.write_text(
            json.dumps(
                {
                    "n_samples": args.n_samples,
                    "variables": [],
                    "samples": [
                        {"sample_id": f"{i + 1:04d}", "values": {}} for i in range(args.n_samples)
                    ],
                },
                indent=2,
            )
        )
        return 0

    d = len(variables)
    ordered = _validate_dependency_graph(variables)
    var_index: dict[str, int] = {v["name"]: i for i, v in enumerate(variables)}

    sampler = scipy.stats.qmc.LatinHypercube(d=d, seed=0)
    lhs_samples = sampler.random(n=args.n_samples)

    samples = []
    for i in range(args.n_samples):
        values: dict[str, Any] = {}
        for v in ordered:
            name = v["name"]
            j = var_index[name]
            u = lhs_samples[i, j]
            dist = v.get("distribution")

            if dist == "conditional":
                values[name] = _resolve_conditional(u, v, values)
            else:
                values[name] = _apply_distribution(u, dist, v)

        samples.append({"sample_id": f"{i + 1:04d}", "values": values})

        # Write per-sample parameter files. For categorical variables with
        # structured output (label + mapping), flatten to a simple key-value
        # dict so downstream processes get plain values. The full structured
        # data is preserved in the main samples.json output.
        flat_values: dict[str, Any] = {}
        for k, v in values.items():
            if isinstance(v, dict) and "label" in v:
                flat_values[k] = v["label"]
            else:
                flat_values[k] = v
        param_file = args.out.parent / f"{i + 1:04d}.params.json"
        param_file.write_text(json.dumps(flat_values, indent=2))

    args.out.write_text(
        json.dumps(
            {
                "n_samples": args.n_samples,
                "variables": variables,
                "samples": samples,
            },
            indent=2,
        )
    )

    return 0


if __name__ == "__main__":
    sys.exit(main())
