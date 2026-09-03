"""Artifact / provenance writing for Campaign (issue #1462 extraction).

Extracted from ``osimflow.campaign``: the manifest writers introduced
by issue #277 (``campaign_meta.json``, ``provenance.json``,
``artifact_manifest.json``), the per-sample intermediate archiver, and
the campaign-inputs archiver used by ``--archive_intermediates``.
"""

import hashlib
import json
import logging
import platform
import shutil
import time
from pathlib import Path
from typing import Any

import yaml

from .config import CampaignConfig
from .json_utils import safe_json_dumps
from .monitoring import RunTrace

log = logging.getLogger("osimflow.campaign")


def _osimflow_version() -> str:
    """Return the installed OSimFlow version, or 'unknown'."""
    try:
        from importlib.metadata import version  # noqa: PLC0415

        return version("osimflow")
    except Exception:
        return "unknown"


class CampaignArtifactWriter:
    """Owns campaign metadata / provenance / artifact-manifest writing."""

    def __init__(self, cfg: CampaignConfig, trace: RunTrace) -> None:
        self._cfg = cfg
        self._trace = trace

    # ------------------------------------------------------------------
    # Manifest writers (issue #277)
    # ------------------------------------------------------------------
    def write_campaign_meta(
        self,
        executor_name: str,
        shard_label: str | None,
    ) -> None:
        """Write ``campaign_meta.json`` to outdir at campaign start.

        Captures the campaign configuration in a queryable JSON form so
        downstream tools (dashboards, comparators, auditors) can inspect
        a campaign without parsing CLI args or run.json.

        The file is overwritten on each run so re-runs produce the
        latest configuration snapshot.
        """
        # Build input_variables summary from variables.yml.
        variable_summary: list[dict[str, object]] = []
        try:
            raw: Any = yaml.safe_load(self._cfg.input_variables.read_text())
            if isinstance(raw, dict):
                for var in raw.get("variables", []):
                    if isinstance(var, dict) and "name" in var:
                        entry: dict[str, object] = {
                            "name": var["name"],
                            "distribution": var.get("distribution", "unknown"),
                        }
                        for key in ("min", "max", "mean", "sigma", "mode", "steps"):
                            if key in var:
                                entry[key] = var[key]
                        variable_summary.append(entry)
        except Exception as exc:
            log.warning("could not parse variables.yml for campaign_meta: %s", exc, exc_info=True)

        meta: dict[str, object] = {
            "campaign_id": self._trace.campaign_id,
            "algorithm": self._cfg.algorithm,
            "n_samples": self._cfg.n_samples,
            "shard": {
                "count": self._cfg.shard_count,
                "index": self._cfg.shard_index,
                "start": self._cfg.shard_start,
                "end": self._cfg.shard_end,
                "label": shard_label,
            },
            "openstudio_version": self._cfg.openstudio_version,
            "executor_type": executor_name,
            "input_variables": {
                "path": str(self._cfg.input_variables),
                "variables": variable_summary,
            },
            "template_sim_package": str(self._cfg.template_sim_package),
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "osimflow_version": _osimflow_version(),
        }
        out_path = self._cfg.outdir / "campaign_meta.json"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(meta, indent=2, default=str))
        log.info("wrote campaign metadata to %s", out_path)

    def write_provenance(
        self,
        code_hashes: dict[str, str],
        cache_stats: Any,
        latest_samples_file: Path,
        shard_label: str | None,
    ) -> None:
        """Write ``provenance.json`` to outdir at campaign completion.

        Captures the full sampling details, code hashes used for cache
        invalidation, and runtime environment information for
        reproducibility auditing.
        """
        # Read samples.json if it exists for seed/algorithm details.
        sampling_details: dict[str, object] = {
            "algorithm": self._cfg.algorithm,
            "n_samples": self._cfg.n_samples,
            "max_generations": self._cfg.max_generations,
        }
        samples_file = latest_samples_file
        if samples_file.exists():
            try:
                samples_data = json.loads(samples_file.read_text())
                # Capture the sample IDs so provenance is self-describing.
                sampling_details["sample_ids"] = [
                    s.get("sample_id", f"unknown_{i}")
                    for i, s in enumerate(samples_data.get("samples", []))
                ]
                sampling_details["n_actual_samples"] = len(samples_data.get("samples", []))
            except Exception as exc:
                log.warning("could not read samples.json for provenance: %s", exc, exc_info=True)

        provenance: dict[str, object] = {
            "campaign_id": self._trace.campaign_id,
            "sampling": sampling_details,
            "shard": {
                "count": self._cfg.shard_count,
                "index": self._cfg.shard_index,
                "start": self._cfg.shard_start,
                "end": self._cfg.shard_end,
                "label": shard_label,
                "samples_file": str(samples_file),
            },
            "code_hashes": code_hashes,
            "environment": {
                "osimflow_version": _osimflow_version(),
                "python_version": platform.python_version(),
                "platform": platform.platform(),
                "python_implementation": platform.python_implementation(),
            },
            "cache_stats": cache_stats,
            "completed_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        }
        out_path = self._cfg.outdir / "provenance.json"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        safe_json_dumps(provenance, out_path, default=str, indent=2)
        log.info("wrote provenance to %s", out_path)

    def write_artifact_manifest(self) -> None:
        """Write ``artifact_manifest.json`` to outdir after aggregation.

        Scans the outdir for all output files and records their paths,
        sizes, and SHA-256 checksums grouped by category (results, plots,
        logs, intermediates).
        """
        artifacts: list[dict[str, object]] = []

        # Maps (path_prefix, is_prefix_match) -> category for common cases.
        prefix_map = {
            "plots": "plots",
            "work/sim": "intermediates",
            "work/apply": "intermediates",
        }
        ext_map = {
            ".png": "plots",
            ".pdf": "plots",
            ".svg": "plots",
            ".csv": "results",
            ".parquet": "results",
            ".log": "logs",
            ".sqlite": "cache",
        }

        def _categorise(path: Path) -> str:
            """Assign a category based on file location/extension."""
            rel = str(path.relative_to(self._cfg.outdir))
            # Check prefix-based categories first.
            for prefix, cat in prefix_map.items():
                if rel.startswith(prefix):
                    return cat
            # Check extension-based categories.
            suffix = path.suffix
            if suffix in ext_map:
                return ext_map[suffix]
            # JSON files: distinguish by name.
            if suffix == ".json":
                if "run.json" in rel:
                    return "logs"
                if any(x in rel for x in ("campaign_meta", "provenance", "artifact_manifest")):
                    return "metadata"
                return "results"
            return "other"

        for f in sorted(self._cfg.outdir.rglob("*")):
            if not f.is_file():
                continue
            try:
                rel_path = str(f.relative_to(self._cfg.outdir))
            except ValueError:
                continue  # skip files outside outdir
            category = _categorise(f)
            # Compute checksum for files that are not the manifest itself.
            sha256 = (
                hashlib.sha256(f.read_bytes()).hexdigest()
                if "artifact_manifest" not in rel_path
                else ""
            )
            artifacts.append(
                {
                    "path": rel_path,
                    "size_bytes": f.stat().st_size,
                    "checksum_sha256": sha256,
                    "category": category,
                }
            )

        manifest: dict[str, object] = {
            "campaign_id": self._trace.campaign_id,
            "artifacts": artifacts,
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        }
        out_path = self._cfg.outdir / "artifact_manifest.json"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        safe_json_dumps(manifest, out_path, default=str, indent=2)
        log.info("wrote artifact manifest to %s (%d files)", out_path, len(artifacts))

    # ------------------------------------------------------------------
    # Intermediate archiving
    # ------------------------------------------------------------------
    @staticmethod
    def archive_sample_artifacts(src: Path, dst: Path, patterns: list[str]) -> None:
        """Copy files matching *patterns* from *src* into *dst*.

        Creates *dst* (with parents) and copies each file whose name
        matches one of the glob *patterns*.  Uses ``shutil.copy2`` so
        timestamps are preserved (cross-substrate robustness: works on
        local, NFS, and any substrate that exposes a POSIX filesystem).
        """
        dst.mkdir(parents=True, exist_ok=True)
        for pattern in patterns:
            for f in src.glob(pattern):
                if f.is_file():
                    shutil.copy2(f, dst / f.name)
                    log.debug("archived %s -> %s", f, dst / f.name)

    def maybe_archive_inputs(self) -> None:
        """Archive campaign inputs when ``cfg.archive_intermediates`` is set."""
        if not self._cfg.archive_intermediates:
            return
        inputs_archive = self._cfg.outdir / "archive" / "inputs"
        inputs_archive.mkdir(parents=True, exist_ok=True)
        pkg_dst = inputs_archive / self._cfg.template_sim_package.name
        if pkg_dst.exists():
            shutil.rmtree(pkg_dst)
        shutil.copytree(self._cfg.template_sim_package, pkg_dst)
        log.info("archived template_sim_package -> %s", pkg_dst)
        shutil.copy2(self._cfg.input_variables, inputs_archive / self._cfg.input_variables.name)
        log.info("archived input_variables -> %s", inputs_archive / self._cfg.input_variables.name)
