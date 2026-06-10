"""osimflow — community-driven parametric OpenStudio simulation campaigns.

Foundation selected via the architecture decision in
`.agents/results/architecture/0001-workflow-framework.md` and validated
in `.agents/results/decision-verdict.md`. This is the canonical public
API; everything else is an implementation detail.
"""

from .algorithms import AlgorithmRegistry, BaseAlgorithm, LHSAlgorithm
from .cache import CacheKey, SQLiteCache
from .campaign import Campaign
from .config import CampaignConfig, load_config
from .executors import (
    AWSBatchExecutor,
    BaseExecutor,
    LocalExecutor,
    NomadExecutor,
    SlurmExecutor,
)
from .monitoring import RunTrace, StepTrace
from .weather import (
    EPWDownloadError,
    EPWValidationError,
    discover_epw_files,
    download_epw,
    validate_all_epw_files,
    validate_epw,
    validate_epw_header,
)

__all__ = [
    "AlgorithmRegistry",
    "BaseAlgorithm",
    "LHSAlgorithm",
    "CacheKey",
    "SQLiteCache",
    "Campaign",
    "CampaignConfig",
    "load_config",
    "BaseExecutor",
    "LocalExecutor",
    "SlurmExecutor",
    "AWSBatchExecutor",
    "NomadExecutor",
    "RunTrace",
    "StepTrace",
    "EPWValidationError",
    "EPWDownloadError",
    "discover_epw_files",
    "download_epw",
    "validate_all_epw_files",
    "validate_epw",
    "validate_epw_header",
]
