"""EPW (weather-file) helpers for Campaign (issue #1462 extraction).

Extracted from ``osimflow.campaign``: the ``epw_file``-target
resolution and pre-flight validation introduced by issue #55 and
hardened by issue #63 (format validation).  All state comes from the
:class:`~osimflow.config.CampaignConfig`; Campaign keeps thin
delegating methods (tests call ``campaign._load_variable_defs()``,
``campaign._resolve_epw_targets(...)``, etc. directly).
"""

import logging
from pathlib import Path
from typing import Any

import yaml

from .apply_params import EPW_FILE_KEY
from .config import CampaignConfig
from .weather import EPWValidationError, validate_all_epw_files, validate_epw

log = logging.getLogger("osimflow.campaign")


class CampaignEpwResolver:
    """Owns variables.yml loading and epw_file target resolution/validation."""

    def __init__(self, cfg: CampaignConfig) -> None:
        self._cfg = cfg

    def load_variable_defs(self) -> list[dict[str, Any]]:
        """Load variable definitions from ``cfg.input_variables`` (variables.yml).

        Returns the raw ``variables`` list so the Campaign can inspect
        ``target`` and ``mapping`` metadata that the LHS generator does
        not propagate to the sample dicts.
        """
        try:
            raw: Any = yaml.safe_load(self._cfg.input_variables.read_text())
        except Exception as exc:
            log.error("Failed to load variables.yml: %s", exc)
            raise
        if not isinstance(raw, dict):
            return []
        variables: Any = raw.get("variables", [])
        if not isinstance(variables, list):
            return []
        return variables

    @staticmethod
    def collect_epw_mappings(
        variable_defs: list[dict[str, Any]],
    ) -> list[tuple[str, dict[str, Any]]]:
        """Collect (variable_name, mapping_dict) for all epw_file targets."""
        result: list[tuple[str, dict[str, Any]]] = []
        for var in variable_defs:
            if var.get("target") != "epw_file":
                continue
            mapping = var.get("mapping")
            if mapping and isinstance(mapping, dict):
                result.append((var["name"], mapping))
        return result

    def preflight_validate_epw_files(self, variable_defs: list[dict[str, Any]]) -> None:
        """Pre-flight check: verify all mapped .epw files exist and are valid.

        For every variable with ``target: epw_file`` and a ``mapping``
        dict, verify each mapped value is a file that exists inside
        the ``template_sim_package`` directory.  Fail fast with a
        clear error message listing every missing file.

        Additionally, validates the EPW format of each referenced file
        (header line starts with ``LOCATION``) so that malformed weather
        files are caught before any simulations start (issue #63).

        After validating the explicitly-referenced files, also validates
        all ``.epw`` files found in the ``weather/`` subdirectory of the
        template package (configurable via ``cfg.weather_dir``).

        Raises:
            FileNotFoundError: one or more mapped .epw files are missing.
            EPWValidationError: one or more .epw files fail format validation.
        """
        template_dir = self._cfg.template_sim_package
        epw_mappings = self.collect_epw_mappings(variable_defs)

        # Phase 1: check existence of all mapped .epw files.
        self._check_epw_existence(epw_mappings, template_dir)

        # Phase 2: validate EPW format for all referenced + discovered files.
        self._check_epw_format(epw_mappings, template_dir)

    @staticmethod
    def _check_epw_existence(
        epw_mappings: list[tuple[str, dict[str, Any]]],
        template_dir: Path,
    ) -> None:
        """Raise FileNotFoundError if any mapped .epw file is missing."""
        missing: list[str] = []
        for var_name, mapping in epw_mappings:
            for cat_value, epw_rel_path in mapping.items():
                epw_abs = template_dir / str(epw_rel_path)
                if not epw_abs.is_file():
                    missing.append(
                        f"  variable={var_name!r} value={cat_value!r} -> {epw_abs} (missing)"
                    )
        if missing:
            raise FileNotFoundError(
                "PRE-FLIGHT EPW VALIDATION FAILED: the following mapped "
                ".epw files were not found in template_sim_package="
                f"{template_dir}:\n" + "\n".join(missing)
            )

    def _check_epw_format(
        self,
        epw_mappings: list[tuple[str, dict[str, Any]]],
        template_dir: Path,
    ) -> None:
        """Raise EPWValidationError if any .epw file has invalid format."""
        format_errors: list[str] = []
        for _var_name, mapping in epw_mappings:
            for _cat_value, epw_rel_path in mapping.items():
                epw_abs = template_dir / str(epw_rel_path)
                try:
                    validate_epw(epw_abs)
                except EPWValidationError as exc:
                    format_errors.append(f"  {exc}")

        # Also validate all EPW files in the weather subdirectory (issue #63).
        try:
            validate_all_epw_files(template_dir, self._cfg.weather_dir)
        except EPWValidationError as exc:
            format_errors.append(f"  {exc}")

        if format_errors:
            raise EPWValidationError(
                "PRE-FLIGHT EPW FORMAT VALIDATION FAILED: the following "
                ".epw files have invalid format:\n" + "\n".join(format_errors)
            )

    def resolve_epw_targets(
        self,
        params: dict[str, object],
        variable_defs: list[dict[str, Any]],
        weather_file_override: str | None = None,
    ) -> dict[str, object]:
        """Resolve ``epw_file`` targets in a sample's parameter dict.

        For each variable with ``target: epw_file``, look up the
        parameter value in the variable's ``mapping`` dict and inject
        the resolved .epw path under :data:`EPW_FILE_KEY`
        (``"__epw_file__"``) into a copy of *params*.

        Categorical variables produce structured dicts (``{"label": ...,
        "index": ...}``); this method extracts the ``label`` for
        mapping lookups so the downstream resolution works transparently.

        If no ``epw_file`` targets exist, returns *params* unchanged.

        Raises:
            ValueError: a parameter value is not in the variable's mapping.
        """
        # GAP-009: per-sample weather_file override takes precedence over
        # campaign-level epw_file target resolution.
        if weather_file_override:
            resolved = dict(params)
            resolved[EPW_FILE_KEY] = str(weather_file_override)
            log.debug(
                "resolved epw_file (GAP-009 override): %s",
                weather_file_override,
            )
            return resolved

        epw_vars = [v for v in variable_defs if v.get("target") == "epw_file"]
        if not epw_vars:
            return params
        resolved = dict(params)
        for var in epw_vars:
            name = var["name"]
            mapping = var.get("mapping", {})
            raw_value = params.get(name)
            if raw_value is None:
                continue
            # Categorical variables produce structured dicts; extract the label.
            if isinstance(raw_value, dict) and "label" in raw_value:
                value = raw_value["label"]
            else:
                value = raw_value
            epw_path = mapping.get(value)
            if epw_path is None:
                raise ValueError(
                    f"Parameter {name!r} has value {value!r} which is "
                    f"not in the epw_file mapping. Available keys: "
                    f"{sorted(mapping.keys())}"
                )
            resolved[EPW_FILE_KEY] = str(epw_path)
            log.debug(
                "resolved epw_file: %s=%s -> %s",
                name,
                value,
                epw_path,
            )
        return resolved
