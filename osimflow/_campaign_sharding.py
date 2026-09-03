"""Sample sharding for Campaign (issue #1462 extraction).

This module extracts shard partitioning from the Campaign class:
the ``shard_count`` / ``shard_index`` modulo partitioning and the
``shard_start`` / ``shard_end`` range slicing (the ``--shard-*``
CLI flags), plus the shard-derived labels used by the samples
manifest and provenance writers.

Mirrors the ``_campaign_cost_tracker.py`` collaborator pattern:
:class:`CampaignSharding` is constructed with the campaign config
and exposes pure functions over the sample list.
"""

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from .config import CampaignConfig

if TYPE_CHECKING:
    from .campaign import SampleSpec

log = logging.getLogger("osimflow.campaign")


class CampaignSharding:
    """Owns shard selection and shard labels for a Campaign."""

    def __init__(self, cfg: CampaignConfig) -> None:
        self._cfg = cfg

    def label(self) -> str | None:
        """Return the shard label for this campaign, or ``None``."""
        if self._cfg.shard_count is not None and self._cfg.shard_index is not None:
            return f"part-{self._cfg.shard_index}-of-{self._cfg.shard_count}"
        if self._cfg.shard_start is not None and self._cfg.shard_end is not None:
            return f"range-{self._cfg.shard_start}-{self._cfg.shard_end}"
        return None

    def samples_manifest_path(self) -> Path:
        """Path of the (shard-aware) samples manifest for this campaign."""
        label = self.label()
        if label is None:
            return self._cfg.samples_file
        return self._cfg.work_dir / f"samples.{label}.json"

    def apply_sharding(
        self,
        samples: list["SampleSpec"],
        *,
        generation: int,
    ) -> list["SampleSpec"]:
        """Return only samples assigned to this shard (if sharding configured)."""
        if self._cfg.shard_count is not None and self._cfg.shard_index is not None:
            shard_count = self._cfg.shard_count
            shard_index = self._cfg.shard_index
            selected = [s for idx, s in enumerate(samples) if idx % shard_count == shard_index]
            log.info(
                "sharding(partition): generation=%d selected %d/%d samples (index=%d count=%d)",
                generation,
                len(selected),
                len(samples),
                shard_index,
                shard_count,
            )
            return selected
        if self._cfg.shard_start is not None and self._cfg.shard_end is not None:
            start = self._cfg.shard_start
            end = self._cfg.shard_end
            selected = samples[start:end]
            log.info(
                "sharding(range): generation=%d selected %d/%d samples (start=%d end=%d)",
                generation,
                len(selected),
                len(samples),
                start,
                end,
            )
            return selected
        return samples
