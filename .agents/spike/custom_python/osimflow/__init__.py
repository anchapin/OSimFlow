"""osimflow — community-driven parametric OpenStudio simulation campaigns.

This package is the spike implementation of the custom-Python driver
proposed in `.agents/results/result-architecture.md`. See the parent ADR
(`.agents/results/architecture/0001-workflow-framework.md`) for rationale.
"""
# Imports are kept local to avoid a circular dependency: `campaign` and
# `config` reference each other, and `executors` is only needed when
# the user actually instantiates one. Importing everything eagerly
# would force every consumer (including the test suite that only needs
# the cache module) to pull in `submitit`.

__all__ = [
    "SQLiteCache",
    "CacheKey",
    "Campaign",
    "CampaignConfig",
    "load_config",
    "BaseExecutor",
    "LocalExecutor",
    "SlurmExecutor",
    "AWSBatchExecutor",
]


def __getattr__(name):
    """Lazy attribute access so `from osimflow import X` doesn't fail at
    import time for X whose module has heavy deps."""
    if name in ("SQLiteCache", "CacheKey"):
        from .cache import SQLiteCache, CacheKey
        return {"SQLiteCache": SQLiteCache, "CacheKey": CacheKey}[name]
    if name in ("Campaign",):
        from .campaign import Campaign
        return Campaign
    if name in ("CampaignConfig", "load_config"):
        from .config import CampaignConfig, load_config
        return {"CampaignConfig": CampaignConfig, "load_config": load_config}[name]
    if name in ("BaseExecutor", "LocalExecutor", "SlurmExecutor", "AWSBatchExecutor"):
        from . import executors
        return getattr(executors, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

