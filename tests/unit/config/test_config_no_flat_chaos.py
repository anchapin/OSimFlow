"""Regression guard for issue #1474.

Acceptance criterion from the issue body: ``CampaignConfig`` must have
no field beginning with ``chaos_`` outside the composed ``cfg.chaos``
object. The flat ``chaos_*`` shadow fields were removed and the single
``ChaosConfig`` object is the source of truth. Legacy flat reads still
work via ``__getattr__`` delegation (see ``config.py``), but no field
with that prefix must exist on the dataclass itself.
"""

from __future__ import annotations

import dataclasses

from osimflow.config import CampaignConfig, ChaosConfig


def test_no_chaos_prefixed_fields_on_campaign_config() -> None:
    """No dataclass field on CampaignConfig starts with ``chaos_``.

    The composed ``chaos: ChaosConfig`` attribute is the single source of
    truth — issue #1474 removed every flat ``chaos_*`` shadow field.
    """
    field_names = {f.name for f in dataclasses.fields(CampaignConfig)}
    chaos_prefixed = sorted(n for n in field_names if n.startswith("chaos_"))
    assert chaos_prefixed == [], (
        f"CampaignConfig still carries flat chaos_* fields: {chaos_prefixed}"
        f" — issue #1474 requires these to live inside cfg.chaos only."
    )


def test_chaos_field_present_and_default_factory() -> None:
    """``chaos`` is the sole chaos source-of-truth field, defaulted to ChaosConfig()."""
    field_names = {f.name for f in dataclasses.fields(CampaignConfig)}
    assert "chaos" in field_names

    # Build a minimal CampaignConfig and verify cfg.chaos is a ChaosConfig
    # populated by the default factory (no flat fields driving it).
    cfg = CampaignConfig(
        input_variables=Path("/tmp/v.yml"),
        template_sim_package=Path("/tmp/pkg"),
        n_samples=1,
        outdir=Path("/tmp/out"),
        openstudio_version="3.11.0",
    )
    assert isinstance(cfg.chaos, ChaosConfig)
    assert cfg.chaos.enabled is False
    assert cfg.chaos.scenarios == []
    assert cfg.chaos.schedule == "none"


def test_legacy_flat_chaos_reads_still_delegate() -> None:
    """Flat reads like ``cfg.chaos_enabled`` still resolve via __getattr__.

    Issue #1474 keeps backward-compat reads working so older callers
    that haven't migrated to ``cfg.chaos.<attr>`` don't break.
    """
    cfg = CampaignConfig(
        input_variables=Path("/tmp/v.yml"),
        template_sim_package=Path("/tmp/pkg"),
        n_samples=1,
        outdir=Path("/tmp/out"),
        openstudio_version="3.11.0",
        chaos=ChaosConfig(
            enabled=True,
            scenarios=["network_delay"],
            schedule="per_sample",
            delay_s=0.2,
            jitter_s=0.05,
            fail_after=3,
        ),
    )
    # Delegated reads
    assert cfg.chaos_enabled is True
    assert cfg.chaos_scenarios == ["network_delay"]
    assert cfg.chaos_schedule == "per_sample"
    assert cfg.chaos_delay_s == 0.2
    assert cfg.chaos_jitter_s == 0.05
    assert cfg.chaos_fail_after == 3
    # And the composed object reads
    assert cfg.chaos.enabled is True
    assert cfg.chaos.delay_s == 0.2


# Path is imported at module bottom because the local imports in the
# CampaignConfig signature already pulled it in transitively; we use
# it in the test bodies above.
from pathlib import Path  # noqa: E402  (intentional late import for the helper)
