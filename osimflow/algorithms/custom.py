"""Custom DOE pattern loader (issue #406).

``CustomDOEAlgorithm`` loads user-provided sample points from a CSV
file or accepts a Python callable that generates samples at runtime.
Column headers in the CSV must match variable names defined in
``variables.yml``.

Configuration in ``variables.yml``::

    algorithm:
      type: custom
      samples_file: /path/to/samples.csv   # CSV with column headers = variable names

    variables:
      - name: wall_area
        distribution: uniform
        min: 100
        max: 500
      - name: window_shading
        distribution: uniform
        min: 0.0
        max: 1.0

Single-shot: ``is_iterative()`` returns ``False``,
``is_converged()`` always returns ``True``.
"""

import csv
import json
import logging
from pathlib import Path
from typing import Any

from osimflow.algorithms import (
    BaseAlgorithm,
    _normalise_var_list,
    _write_empty_samples,
)

log = logging.getLogger("osimflow.algorithms.custom")


class CustomDOEAlgorithm(BaseAlgorithm):
    """Custom DOE pattern loader.

    Supports two modes:

    ``file`` mode
        Loads pre-computed sample points from a CSV file.  The CSV
        must have column headers matching the variable names declared
        in ``variables.yml``.  Rows are read in order until ``n_samples``
        is reached; if the file has fewer rows than requested an error
        is raised.

    ``function`` mode
        Accepts a Python callable (path to a module:function string)
        that returns a list of sample dicts.  The callable receives
        ``(variables, n_samples, seed, outdir)`` and must return a list
        of ``{"sample_id": str, "values": {var_name: value, ...}}``
        dicts.  Implemented via the BYOS pattern.
    """

    def generate_samples(
        self,
        variables: dict[str, Any],
        n_samples: int,
        seed: int | None,
        outdir: Path,
    ) -> Path:
        outdir.mkdir(parents=True, exist_ok=True)
        samples_path = outdir / "samples.json"

        algo_config: dict[str, Any] = variables.get("algorithm", {})
        if not isinstance(algo_config, dict):
            return _write_empty_samples(samples_path)

        if algo_config.get("type") != "custom":
            return _write_empty_samples(samples_path)

        samples_file = algo_config.get("samples_file")
        samples_function = algo_config.get("samples_function")

        if samples_file:
            return self._generate_from_file(
                variables, n_samples, outdir, Path(samples_file), samples_path
            )
        elif samples_function:
            return self._generate_from_function(
                variables, n_samples, seed, outdir, samples_function, samples_path
            )
        else:
            log.error(
                "CustomDOEAlgorithm requires either 'samples_file' or 'samples_function' "
                "in the algorithm config section of variables.yml"
            )
            return _write_empty_samples(samples_path)

    def _generate_from_file(
        self,
        variables: dict[str, Any],
        n_samples: int,
        outdir: Path,
        file_path: Path,
        samples_path: Path,
    ) -> Path:
        var_list = _normalise_var_list(variables.get("variables", []))
        if not var_list:
            return _write_empty_samples(samples_path)

        expected_names = {v["name"] for v in var_list}

        if not file_path.exists():
            raise FileNotFoundError(f"CustomDOEAlgorithm: samples_file not found: {file_path}")

        try:
            with file_path.open(newline="", encoding="utf-8") as fh:
                reader = csv.DictReader(fh)
                fieldnames = reader.fieldnames or []
                actual_columns = {c.strip() for c in fieldnames}

                missing = expected_names - actual_columns
                if missing:
                    raise ValueError(
                        f"CustomDOEAlgorithm: samples_file '{file_path}' is missing "
                        f"columns for variable(s): {sorted(missing)}. "
                        f"Found columns: {sorted(actual_columns)}"
                    )

                samples: list[dict[str, Any]] = []
                for i, row in enumerate(reader):
                    if i >= n_samples:
                        break
                    values: dict[str, Any] = {}
                    for var_def in var_list:
                        var_name = var_def["name"]
                        raw = row.get(var_name, "").strip()
                        if raw == "":
                            raise ValueError(
                                f"CustomDOEAlgorithm: empty value for '{var_name}' "
                                f"in row {i + 1} of '{file_path}'"
                            )
                        try:
                            values[var_name] = float(raw)
                        except ValueError:
                            values[var_name] = raw
                    samples.append({"sample_id": f"{i + 1:04d}", "values": values})

                if len(samples) < n_samples:
                    raise ValueError(
                        f"CustomDOEAlgorithm: samples_file '{file_path}' has only "
                        f"{len(samples)} row(s) but {n_samples} were requested. "
                        f"Add more rows or reduce n_samples."
                    )

        except (OSError, csv.Error) as exc:
            raise RuntimeError("CustomDOEAlgorithm: failed to read CSV file") from exc

        samples_path.write_text(json.dumps({"samples": samples}, indent=2))
        log.info(
            "CustomDOEAlgorithm: loaded %d samples from '%s'",
            len(samples),
            file_path,
        )
        return samples_path

    def _generate_from_function(
        self,
        variables: dict[str, Any],
        n_samples: int,
        seed: int | None,
        outdir: Path,
        function_spec: str,
        samples_path: Path,
    ) -> Path:
        try:
            module_path, func_name = function_spec.rsplit(":", 1)
        except ValueError:
            raise ValueError(  # noqa: B904
                f"CustomDOEAlgorithm: 'samples_function' must be in "
                f"'module:function' format, got: {function_spec!r}"
            )

        try:
            from importlib import import_module  # noqa: PLC0415

            mod = import_module(module_path)
            func = getattr(mod, func_name)
        except Exception as exc:
            raise RuntimeError("CustomDOEAlgorithm: failed to import callable") from exc

        if not callable(func):
            raise TypeError(f"CustomDOEAlgorithm: '{function_spec}' is not callable")

        try:
            raw_samples = func(variables=variables, n_samples=n_samples, seed=seed, outdir=outdir)
        except TypeError:
            raw_samples = func(variables, n_samples, seed, outdir)

        if not isinstance(raw_samples, list):
            raise TypeError(
                f"CustomDOEAlgorithm: function '{function_spec}' must return "
                f"list[dict], got {type(raw_samples).__name__}"
            )

        var_list = _normalise_var_list(variables.get("variables", []))
        expected_names = {v["name"] for v in var_list}

        samples: list[dict[str, Any]] = []
        for i, item in enumerate(raw_samples[:n_samples]):
            if not isinstance(item, dict):
                raise TypeError(
                    f"CustomDOEAlgorithm: function '{function_spec}' returned "
                    f"a list containing {type(item).__name__} at index {i}, "
                    f"expected dict with 'sample_id' and 'values' keys"
                )
            sample_id = item.get("sample_id", f"{i + 1:04d}")
            values = item.get("values", {})
            if not isinstance(values, dict):
                raise TypeError(
                    f"CustomDOEAlgorithm: function '{function_spec}' returned "
                    f"dict with non-dict 'values' at index {i}"
                )
            extra_keys = set(values.keys()) - expected_names
            if var_list and extra_keys:
                log.warning(
                    "CustomDOEAlgorithm: function '%s' produced values for "
                    "unknown variable(s) %s — ignoring extra keys",
                    function_spec,
                    sorted(extra_keys),
                )
                values = {k: v for k, v in values.items() if k in expected_names}
            samples.append({"sample_id": sample_id, "values": values})

        samples_path.write_text(json.dumps({"samples": samples}, indent=2))
        log.info(
            "CustomDOEAlgorithm: generated %d samples via '%s'",
            len(samples),
            function_spec,
        )
        return samples_path

    def observe(self, history: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Single-shot: return the samples from the last iteration."""
        if not history:
            return []
        last = history[-1].get("samples", [])
        return list(last)

    def is_converged(self, history: list[dict[str, Any]]) -> bool:
        """Single-shot algorithms are always converged."""
        return True

    def name(self) -> str:
        return "custom"

    def is_iterative(self) -> bool:
        return False
