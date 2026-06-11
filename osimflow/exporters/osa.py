"""OpenStudio Analysis (OSA) export converter.

Exports an OSimFlow campaign configuration to a PAT-compatible
``analysis.json`` file so that users can transfer their study
definitions back to the OpenStudio Parametric Analysis Tool or
share them as ``.osa`` archives.

This is the reverse of :mod:`osimflow.importers.osa`.

Distribution mapping (reverse of the import table)
---------------------------------------------------

=================  ====================  ============================
OSimFlow name      OSA distribution      Parameters
=================  ====================  ============================
uniform            uniform               minimum, maximum
normal             normal                mean, stddev
lognormal          lognormal             mean, stddev
triangular         triangular            minimum, maximum, mode (optional)
discrete           discrete              values
categorical        categorical           values
beta               uniform*              min, max (lossy)
gamma              uniform*              min, max (lossy)
exponential        uniform*              min, max (lossy)
=================  ====================  ============================

``Beta``, ``gamma``, and ``exponential`` distributions do not have direct OSA
equivalents.  They are exported as ``uniform`` spanning the same
min/max range with a warning logged.  Callers that need exact fidelity
should use the round-trip ``variables.yml`` directly.
"""

import json
import logging
import zipfile
from pathlib import Path
from typing import Any

import yaml

from osimflow.config import CampaignConfig

log = logging.getLogger("osimflow.exporters.osa")

# Reverse of the import ``OSA_DISTRIBUTION_MAP``.  Maps OSimFlow
# distribution names to their OSA equivalents.
_OSIMFLOW_TO_OSA: dict[str, str] = {
    "uniform": "uniform",
    "normal": "normal",
    "lognormal": "lognormal",
    "triangular": "triangular",
    "discrete": "discrete",
    "categorical": "categorical",
}

# Distributions that do not have a direct OSA equivalent — exported
# as ``uniform`` with a warning.
_LOSSY_DISTRIBUTIONS: frozenset[str] = frozenset({"beta", "gamma", "exponential"})

# Reverse of the import ``_OSA_ALGORITHM_MAP``.  Maps OSimFlow
# algorithm names to their OSA algorithm type strings.
_OSIMFLOW_ALGO_TO_OSA: dict[str, str] = {
    "lhs": "lhs",
    "sobol": "sobol",
    "halton": "doe",
    "morris": "morris",
    "fast99": "fast99",
}

# Distribution-specific parameter mappers: OSimFlow keys → OSA keys.
_PARAM_MAPS: dict[str, dict[str, str]] = {
    "uniform": {"min": "minimum", "max": "maximum"},
    "normal": {"mean": "mean", "sigma": "stddev"},
    "lognormal": {"mean": "mean", "sigma": "stddev"},
    "triangular": {"min": "minimum", "max": "maximum", "mode": "mode"},
}


# Placeholder note embedded in analysis.json when no seed .osm is found.
_SEED_MISSING_NOTE = (
    "NOTE: No seed model (.osm) was found in template_sim_package. "
    "Add a seed.osm to the .osa archive before importing into PAT."
)


class OSAExporter:
    """Export campaign state to PAT-compatible analysis.json format.

    Supports two workflows:

    1. :meth:`export` — write ``analysis.json`` only.
    2. :meth:`pack_osa` — produce a complete ``.osa`` ZIP archive
       containing ``analysis.json``, seed model, measures, and weather
       files.

    Usage::

        exporter = OSAExporter()
        path = exporter.export(config, outdir)
        # — or —
        osa_path = exporter.pack_osa(config, outdir)
    """

    def export(self, config: CampaignConfig, outdir: Path) -> Path:
        """Export campaign configuration to ``analysis.json``.

        Parameters
        ----------
        config
            The campaign configuration to export.
        outdir
            Directory where ``analysis.json`` will be written.

        Returns
        -------
        Path
            Absolute path to the exported file.
        """
        analysis = self._build_analysis(config)
        outdir = Path(outdir)
        outdir.mkdir(parents=True, exist_ok=True)
        outpath = outdir / "analysis.json"
        outpath.write_text(json.dumps(analysis, indent=2) + "\n", encoding="utf-8")
        log.info("Exported analysis.json to %s", outpath)
        return outpath

    def pack_osa(self, config: CampaignConfig, outdir: Path) -> Path:
        """Package campaign into a ``.osa`` ZIP archive.

        The archive layout matches the openstudio-analysis-gem structure:

        - ``analysis.json`` (always present)
        - ``seed.osm`` (if a ``.osm`` file exists in
          ``template_sim_package``)
        - ``measures/`` (contents of ``template_sim_package/measures/``)
        - ``weather/`` (contents of ``template_sim_package/weather/``)

        If no ``.osm`` file is found, a ``_seed_missing_note`` key is
        added to the ``analysis.json`` inside the archive.  The export
        never fails for missing files.

        Parameters
        ----------
        config
            The campaign configuration to package.
        outdir
            Directory where the ``.osa`` file will be written.

        Returns
        -------
        Path
            Absolute path to the ``.osa`` ZIP archive.
        """
        outdir = Path(outdir)
        outdir.mkdir(parents=True, exist_ok=True)

        # Step 1: export analysis.json to a temporary location.
        analysis_path = self.export(config, outdir / "_osa_staging")
        analysis_data = json.loads(analysis_path.read_text(encoding="utf-8"))

        # Step 2: check for a seed .osm in template_sim_package.
        has_seed = False
        seed_path: Path | None = None
        tsp = config.template_sim_package
        if tsp and tsp.is_dir():
            for candidate in sorted(tsp.rglob("*.osm")):
                if candidate.is_file():
                    has_seed = True
                    seed_path = candidate
                    break

        if not has_seed:
            log.warning(
                "No seed model (.osm) found in %s; "
                "the .osa archive will lack a seed model. "
                "Add one before importing into PAT.",
                tsp,
            )
            analysis_data["_seed_missing_note"] = _SEED_MISSING_NOTE

        # Step 3: build the .osa ZIP.
        osa_path = outdir / f"{config.outdir.name}.osa"
        with zipfile.ZipFile(osa_path, "w", zipfile.ZIP_DEFLATED) as zf:
            # Always write analysis.json.
            zf.writestr("analysis.json", json.dumps(analysis_data, indent=2) + "\n")

            # Include seed model.
            if has_seed and seed_path is not None:
                zf.write(seed_path, "seed.osm")
                log.info("Packed seed model: %s → seed.osm", seed_path.name)

            # Include template_sim_package contents (measures/, weather/, etc.)
            if tsp and tsp.is_dir():
                for f in tsp.rglob("*"):
                    if not f.is_file():
                        continue
                    # Skip .osm (already handled as seed.osm).
                    if f.suffix.lower() == ".osm":
                        continue
                    arcname = str(f.relative_to(tsp))
                    zf.write(f, arcname)
                    log.debug("Packed %s → %s", f, arcname)

        log.info("Packed .osa archive: %s", osa_path)
        return osa_path

    def _build_analysis(self, config: CampaignConfig) -> dict[str, Any]:
        """Build the full analysis.json structure."""
        algorithm_type = self._translate_algorithm(config.algorithm)
        return {
            "analysis": {
                "display_name": config.outdir.name,
                "algorithm": {
                    "type": algorithm_type,
                    "number_of_samples": config.n_samples,
                },
                "problem": {
                    "algorithm": {
                        "type": algorithm_type,
                        "number_of_samples": config.n_samples,
                    },
                    "variables": self._build_variables(config),
                    "workflows": [],
                    "file_format_version": 1,
                },
            },
            "server": {
                "base_oscli_version": config.openstudio_version or "3.11.0",
            },
        }

    def _translate_algorithm(self, algorithm_name: str) -> str:
        """Translate an OSimFlow algorithm name to an OSA algorithm type.

        Parameters
        ----------
        algorithm_name
            The OSimFlow algorithm name (e.g. ``"lhs"``, ``"sobol"``).

        Returns
        -------
        str
            The OSA algorithm type string.  Defaults to ``"lhs"`` if the
            algorithm is not in the translation table.
        """
        osa_name = _OSIMFLOW_ALGO_TO_OSA.get(algorithm_name)
        if osa_name is None:
            log.warning(
                "Unknown algorithm %r; falling back to 'lhs' in OSA export",
                algorithm_name,
            )
            return "lhs"
        return osa_name

    def _build_variables(self, config: CampaignConfig) -> list[dict[str, Any]]:
        """Build the OSA variable list from the campaign's variables.yml.

        Reads the ``variables.yml`` referenced by ``config.input_variables``
        and converts each variable to the OSA format.

        Returns
        -------
        list[dict]
            A list of OSA variable objects suitable for the ``problem``
            section of analysis.json.
        """
        variables_path = Path(config.input_variables)
        if not variables_path.exists():
            log.warning("variables.yml not found at %s; exporting empty variables", variables_path)
            return []

        try:
            with variables_path.open(encoding="utf-8") as f:
                yml_data = yaml.safe_load(f)
        except Exception as exc:
            log.warning("Could not parse %s: %s; exporting empty variables", variables_path, exc)
            return []

        if not isinstance(yml_data, dict):
            return []

        raw_variables = yml_data.get("variables", [])
        if not isinstance(raw_variables, list):
            return []

        result: list[dict[str, Any]] = []
        for var in raw_variables:
            if not isinstance(var, dict):
                continue
            osa_var = self._convert_variable(var)
            if osa_var is not None:
                result.append(osa_var)

        return result

    def _convert_variable(self, var: dict[str, Any]) -> dict[str, Any] | None:
        """Convert a single OSimFlow variable entry to OSA format.

        Parameters
        ----------
        var
            A single variable dict from ``variables.yml``.

        Returns
        -------
        dict or None
            The OSA variable object, or ``None`` if the variable cannot
            be converted (e.g. missing name).
        """
        name = var.get("name")
        if not name:
            return None

        distribution_name = var.get("distribution", "uniform")
        osa_dist = self._convert_distribution(distribution_name, var)

        osa_var: dict[str, Any] = {
            "name": name,
            "variable_type": "variable",
            "distribution": osa_dist,
        }

        # Carry over display_name if present.
        display_name = var.get("display_name")
        if display_name:
            osa_var["display_name"] = display_name

        # Carry over measure_argument as measure reference.
        measure_arg = var.get("measure_argument")
        if measure_arg and isinstance(measure_arg, str) and "." in measure_arg:
            parts = measure_arg.split(".", 1)
            osa_var["measure"] = {
                "display_name": parts[0],
                "argument": parts[1],
            }

        return osa_var

    def _convert_distribution(self, dist_name: str, var: dict[str, Any]) -> dict[str, Any]:
        """Convert an OSimFlow distribution to OSA format.

        Parameters
        ----------
        dist_name
            The OSimFlow distribution name.
        var
            The full variable dict (used to extract parameters).

        Returns
        -------
        dict
            An OSA distribution object.
        """
        # Handle lossy conversions (beta, gamma, exponential → uniform).
        if dist_name in _LOSSY_DISTRIBUTIONS:
            log.warning(
                "Distribution %r has no direct OSA equivalent; exporting as uniform. Variable: %s",
                dist_name,
                var.get("name", "<unknown>"),
            )
            return self._build_uniform_from_var(var)

        # Direct mapping.
        osa_type = _OSIMFLOW_TO_OSA.get(dist_name, "uniform")

        if osa_type in ("discrete", "categorical"):
            values = var.get("values", [])
            dist: dict[str, Any] = {"type": osa_type, "values": values}
            mapping = var.get("mapping")
            if mapping and isinstance(mapping, dict):
                dist["mapping"] = mapping
            return dist

        # Continuous distributions — map parameter names.
        param_map = _PARAM_MAPS.get(dist_name, {})
        result: dict[str, Any] = {"type": osa_type}
        for osimflow_key, osa_key in param_map.items():
            value = var.get(osimflow_key)
            if value is not None:
                result[osa_key] = value
        return result

    @staticmethod
    def _build_uniform_from_var(var: dict[str, Any]) -> dict[str, Any]:
        """Build a fallback uniform distribution from a variable that lacks
        a direct OSA mapping.

        Uses ``min``/``max`` if present, otherwise falls back to wide
        defaults (0, 1).
        """
        minimum = var.get("min", var.get("loc", 0))
        maximum = var.get("max", var.get("scale", 1))
        # For gamma/exponential with no explicit min/max, derive a range.
        if "min" not in var and "max" not in var:
            # Use loc as minimum, loc+scale*4 as a reasonable upper bound.
            loc = float(var.get("loc", 0))
            scale = float(var.get("scale", 1))
            minimum = loc
            maximum = loc + scale * 4
        return {"type": "uniform", "minimum": float(minimum), "maximum": float(maximum)}
