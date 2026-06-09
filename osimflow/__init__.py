"""osimflow — community-driven parametric OpenStudio simulation campaigns.

Foundation selected via the architecture decision in
`.agents/results/architecture/0001-workflow-framework.md` and validated
in `.agents/results/decision-verdict.md`. This is the canonical public
API; everything else is an implementation detail.
"""
from .cache import CacheKey, SQLiteCache
from .campaign import Campaign
from .config import CampaignConfig, load_config
from .executors import (
    AWSBatchExecutor,
    BaseExecutor,
    LocalExecutor,
    SlurmExecutor,
)
from .monitoring import RunTrace, StepTrace

__all__ = [
    "CacheKey",
    "SQLiteCache",
    "Campaign",
    "CampaignConfig",
    "load_config",
    "BaseExecutor",
    "LocalExecutor",
    "SlurmExecutor",
    "AWSBatchExecutor",
    "RunTrace",
    "StepTrace",
]
