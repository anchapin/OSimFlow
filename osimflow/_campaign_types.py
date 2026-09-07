"""Shared campaign type aliases (issue #1542).

``SampleSpec`` / ``VariableSpec`` moved here from
``osimflow.campaign`` so collaborator modules (e.g.
``_campaign_analysis``) can type against them without importing —
or TYPE_CHECKING-importing — the campaign god module.  They remain
re-exported from ``osimflow.campaign`` (and importable from there
in tests) unchanged.
"""

from typing import TypedDict

__all__ = ["SampleSpec", "VariableSpec"]


# Type aliases — these are the schemas of intermediate DAG outputs.
class SampleSpec(TypedDict, total=False):
    sample_id: str
    values: dict[str, object]
    # Per-sample override paths (GAP-009). When set on a sample, these
    # replace the campaign-level template_sim_package (seed_model) or
    # weather file (weather_file) for that sample only.
    seed_model: str
    weather_file: str


class VariableSpec(TypedDict, total=False):
    name: str
    distribution: str
    min: float
    max: float
    mean: float
    sigma: float
