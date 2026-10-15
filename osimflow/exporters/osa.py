"""OpenStudio Analysis (OSA) export converter.

Exports an OSimFlow campaign configuration to a PAT-compatible
``analysis.json`` file so that users can transfer their study
definitions back to the OpenStudio Parametric Analysis Tool or
share them as ``.osa`` archives.

This is the reverse of :mod:`osimflow.importers.osa`.

Distribution mapping (reverse of the import table)
---------------------------------------------------

================  ====================  ============================  ==============
OSimFlow name      OSA distribution      Parameters                    Notes
================  ====================  ============================  ==============
uniform            uniform               minimum, maximum
normal             normal                mean, stddev
lognormal          lognormal             mean, stddev
triangular         triangular            minimum, maximum, mode (opt.)
discrete           discrete              values
categorical        categorical           values
categorical+pivot  pivot                 values                        pivot: true
static             (no distribution)     default_value only            locked/fixed
beta               uniform*              min, max (lossy)
gamma              uniform*              min, max (lossy)
exponential        uniform*              min, max (lossy)
================  ====================  ============================  ==============

Variable type mapping
---------------------

====================  =====================  ============================
OSimFlow field        OSA variable_type      Notes
====================  =====================  ============================
variable_type=variable variable              (default, omitted)
variable_type=argument argument              measure argument
pivot=true             pivot                  categorical with pivot flag
====================  =====================  ============================

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
from osimflow.weather import find_weather_file

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
    "halton": "halton",
    "morris": "morris",
    "fast99": "fast99",
    "full_factorial": "doe",
    "grid": "grid",
    "random": "random",
    "repeat_all": "repeat_all",
    "nsga2": "nsga_nrel",
    "spea2": "spea_nrel",
    "pso": "pso",
    "de": "de",
    "ga": "ga",
    "gaisl": "gaisl",
    "rgenoud": "rgenoud",
}

# Distribution-specific parameter mappers: OSimFlow keys → OSA keys.
_PARAM_MAPS: dict[str, dict[str, str]] = {
    "uniform": {"min": "minimum", "max": "maximum"},
    "normal": {"mean": "mean", "sigma": "stddev"},
    "lognormal": {"mean": "mean", "sigma": "stddev"},
    "triangular": {"min": "minimum", "max": "maximum", "mode": "mode"},
}


# Sub-directories (relative to template_sim_package) searched for the seed model.
_SEED_SEARCH_DIRS = ("", "seeds", "seed", "files", "models")


# Placeholder note embedded in analysis.json when no seed .osm is found.
_SEED_MISSING_NOTE = (
    "NOTE: No seed model (.osm) was found in template_sim_package. "
    "Add a seed.osm to the .osa archive before importing into PAT."
)


class _PackageFiles:
    """Seed/weather files resolved from a template simulation package."""

    def __init__(self) -> None:
        self.seed_path: Path | None = None
        self.seed_arcname: str | None = None
        self.seed_from_osw = False
        self.weather_rel: str | None = None


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

        ``analysis.json`` and (when a seed is packed) ``seed.osm`` are
        reserved names: same-named files in ``template_sim_package`` are
        skipped so the archive never has duplicate members.

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

        # Step 1: build analysis.json in memory (no staging directory to leak).
        analysis_data = self._build_analysis(config)

        # Step 2: check for a seed .osm in template_sim_package.
        files = self._resolve_package_files(config)
        seed_path = files.seed_path
        has_seed = seed_path is not None
        tsp = config.template_sim_package

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
                    # Only the resolved seed is special (packed as seed.osm);
                    # other .osm files (files/, measures/<m>/tests/) are kept.
                    if seed_path is not None and f.resolve() == seed_path.resolve():
                        continue
                    arcname = f.relative_to(tsp).as_posix()
                    # Generated entries are reserved; a colliding source file
                    # (stale analysis.json, stray root seed.osm) is skipped.
                    if arcname == "analysis.json" or (
                        arcname == "seed.osm" and has_seed
                    ):
                        log.warning(
                            "Skipping %s: archive name is reserved for generated content",
                            arcname,
                        )
                        continue
                    if arcname == "workflow.osw" and files.seed_path is not None:
                        zf.writestr(arcname, self._osw_with_seed(f))
                        continue
                    zf.write(f, arcname)
                    log.debug("Packed %s → %s", f, arcname)

        log.info("Packed .osa archive: %s", osa_path)
        return osa_path

    @staticmethod
    def _osw_with_seed(osw_path: Path) -> str:
        """Return ``workflow.osw`` text with ``seed_file`` pointing at the packed ``seed.osm``."""
        try:
            data = json.loads(osw_path.read_text(encoding="utf-8"))
        except ValueError:
            return osw_path.read_text(encoding="utf-8")
        if not isinstance(data, dict):
            return osw_path.read_text(encoding="utf-8")
        data["seed_file"] = "seed.osm"
        return json.dumps(data, indent=2) + "\n"

    def _build_analysis(self, config: CampaignConfig) -> dict[str, Any]:
        """Build the full analysis.json structure."""
        algorithm_type = self._translate_algorithm(config.algorithm)
        yml_data = self._load_variables_yml(config)
        algorithm: dict[str, Any] = {
            "type": algorithm_type,
            "number_of_samples": config.n_samples,
        }
        seed = self._resolve_seed(yml_data)
        if seed is not None:
            algorithm["seed"] = seed
        analysis: dict[str, Any] = {
            "display_name": config.outdir.name,
            "algorithm": dict(algorithm),
            "problem": {
                "algorithm": dict(algorithm),
                "variables": self._build_variables(config),
                "workflow": self._build_workflow(config),
                "file_format_version": 1,
            },
        }
        outputs = self._resolve_outputs(config, yml_data)
        if outputs:
            analysis["output_variables"] = outputs
        files = self._resolve_package_files(config)
        if files.seed_arcname:
            analysis["seed"] = {"file_name": files.seed_arcname}
        if files.weather_rel:
            analysis["weather_file"] = {"file_name": files.weather_rel}
        return {
            "analysis": analysis,
            "server": {
                "base_oscli_version": config.openstudio_version or "3.10.0",
            },
        }

    @staticmethod
    def _build_workflow(config: CampaignConfig) -> list[dict[str, Any]]:
        """Ordered ``problem.workflow`` steps from ``template_sim_package/workflow.osw``.

        Each step carries ``measure_dir_name``, the per-step ``name`` (when the
        OSW names it; needed for multi-instance measures), static ``arguments``
        and an (empty) ``variables`` list — the shape ``OSAImporter`` reads.
        """
        tsp = config.template_sim_package
        osw_path = Path(tsp) / "workflow.osw" if tsp else None
        if osw_path is None or not osw_path.is_file():
            return []
        try:
            osw = json.loads(osw_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            log.warning("Could not parse %s; exporting empty workflow", osw_path)
            return []
        raw_steps = osw.get("steps") if isinstance(osw, dict) else None
        workflow: list[dict[str, Any]] = []
        for step in raw_steps if isinstance(raw_steps, list) else []:
            if not isinstance(step, dict):
                continue
            m_dir = step.get("measure_dir_name")
            if not isinstance(m_dir, str) or not m_dir.strip():
                continue
            m_dir = Path(m_dir.strip()).name
            args = step.get("arguments")
            entry: dict[str, Any] = {
                "measure_dir_name": m_dir,
                "name": m_dir,
                "arguments": dict(args) if isinstance(args, dict) else {},
                "variables": [],
            }
            name = step.get("name")
            if isinstance(name, str) and name.strip():
                entry["name"] = name.strip()
            workflow.append(entry)
        return workflow

    @staticmethod
    def _load_variables_yml(config: CampaignConfig) -> dict[str, Any]:
        path = Path(config.input_variables)
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError):
            return {}
        return data if isinstance(data, dict) else {}

    @staticmethod
    def _resolve_seed(yml_data: dict[str, Any]) -> int | None:
        """Integer seed from top-level ``seed`` or ``_metadata.seed`` (else ``None``)."""
        raw = yml_data.get("seed")
        if raw is None:
            meta = yml_data.get("_metadata")
            raw = meta.get("seed") if isinstance(meta, dict) else None
        if isinstance(raw, bool) or not isinstance(raw, int):
            return None
        return raw

    @staticmethod
    def _resolve_outputs(config: CampaignConfig, yml_data: dict[str, Any]) -> list[dict[str, Any]]:
        """``output_variables`` from ``_metadata.outputs`` (preferred) or ``config.kpis``."""
        meta = yml_data.get("_metadata")
        raw = meta.get("outputs") if isinstance(meta, dict) else None
        outputs: list[dict[str, Any]] = []
        for item in raw if isinstance(raw, list) else []:
            if isinstance(item, dict):
                outputs.append(dict(item))
            elif isinstance(item, str) and item.strip():
                outputs.append({"name": item.strip()})
        if not outputs:
            outputs = [{"name": k} for k in (config.kpis or [])]
        return outputs

    @staticmethod
    def _fallback_seed(tsp: Path) -> Path | None:
        """First ``.osm`` in the seed search dirs, else any outside ``measures/``."""
        for sub in _SEED_SEARCH_DIRS:
            base = tsp / sub
            if base.is_dir():
                found = next((c for c in sorted(base.glob("*.osm")) if c.is_file()), None)
                if found is not None:
                    return found
        for candidate in sorted(tsp.rglob("*.osm")):
            if candidate.is_file() and "measures" not in candidate.relative_to(tsp).parts[:-1]:
                return candidate
        return None

    @staticmethod
    def _resolve_package_files(config: CampaignConfig) -> "_PackageFiles":
        """Locate the seed ``.osm`` and weather file of ``template_sim_package``.

        ``workflow.osw`` ``seed_file`` / ``weather_file`` are honored first
        (searching the package root, ``seeds/``, ``files/``, ``weather/`` and the
        in-package ``file_paths``); a lone top-level ``.osm`` is the fallback seed.
        """
        tsp = config.template_sim_package
        result = _PackageFiles()
        if not tsp or not Path(tsp).is_dir():
            return result
        tsp = Path(tsp)
        osw: dict[str, Any] = {}
        osw_path = tsp / "workflow.osw"
        if osw_path.is_file():
            try:
                loaded = json.loads(osw_path.read_text(encoding="utf-8"))
                osw = loaded if isinstance(loaded, dict) else {}
            except (OSError, ValueError):
                osw = {}

        root = tsp.resolve()
        extra = [p for p in osw.get("file_paths", []) if isinstance(p, str)]
        seed_name = osw.get("seed_file")
        if isinstance(seed_name, str) and seed_name.strip():
            for sub in (*_SEED_SEARCH_DIRS, *extra):
                cand = (tsp / sub / seed_name).resolve()
                if cand.is_file() and cand.is_relative_to(root):
                    result.seed_path = cand
                    result.seed_from_osw = True
                    break
        if result.seed_path is None:
            result.seed_path = OSAExporter._fallback_seed(tsp)
        if result.seed_path is not None:
            result.seed_arcname = "seed.osm"

        weather_name = osw.get("weather_file")
        if isinstance(weather_name, str) and weather_name.strip():
            found = find_weather_file(tsp, weather_name.strip(), config.weather_dir)
            if found is not None and found.resolve().is_relative_to(root):
                result.weather_rel = str(found.resolve().relative_to(root))
        return result

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
        except Exception as exc:  # noqa: BLE001 - variables.yml parse/IO errors are logged and export proceeds with empty variables
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
        is_pivot = var.get("pivot") is True

        # Determine OSA variable_type from OSimFlow fields.
        osa_variable_type: str = var.get("variable_type", "variable")
        if is_pivot:
            osa_variable_type = "pivot"

        # --- Handle static distribution ---
        if distribution_name == "static":
            osa_var: dict[str, Any] = {
                "name": name,
                "variable_type": osa_variable_type,
            }
            # Static variables carry a default_value but no distribution block.
            static_value = var.get("value")
            if static_value is not None:
                osa_var["default_value"] = static_value
            # Still carry measure reference if present.
            measure_arg = var.get("measure_argument")
            if measure_arg and isinstance(measure_arg, str) and "." in measure_arg:
                parts = measure_arg.split(".", 1)
                osa_var["measure"] = {
                    "name": parts[0],
                    "display_name": parts[0],
                    "argument": parts[1],
                }
            display_name = var.get("display_name")
            if display_name:
                osa_var["display_name"] = display_name
            return osa_var

        # --- Handle pivot distribution ---
        if is_pivot:
            values = var.get("values", [])
            osa_dist: dict[str, Any] = {"type": "pivot", "values": values}
            mapping = var.get("mapping")
            if mapping and isinstance(mapping, dict):
                osa_dist["mapping"] = mapping
        else:
            osa_dist = self._convert_distribution(distribution_name, var)

        osa_var = {
            "name": name,
            "variable_type": osa_variable_type,
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
                "name": parts[0],
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
            weights = self._pmf_weights(values, var.get("pmf"))
            if weights is not None:
                dist["weights"] = weights
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
    def _pmf_weights(values: list[Any], pmf: Any) -> list[float] | None:
        """Ordered ``weights`` list for *values* from a ``{value: weight}`` pmf."""
        if not isinstance(pmf, dict) or not values:
            return None
        weights: list[float] = []
        for v in values:
            w = pmf.get(v, pmf.get(str(v)))
            if w is None:
                log.warning("pmf has no weight for value %r; omitting weights", v)
                return None
            weights.append(float(w))
        return weights

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
