"""osimflow — community-driven parametric OpenStudio simulation campaigns.

Foundation selected via the architecture decision in
`.agents/results/architecture/0001-workflow-framework.md` and validated
in `.agents/results/decision-verdict.md`. This is the canonical public
API; everything else is an implementation detail.
"""

from .algorithms import AlgorithmRegistry, BaseAlgorithm, LHSAlgorithm
from .algorithms.halton import HaltonAlgorithm
from .algorithms.sobol import SobolAlgorithm
from .cache import CacheKey, RedisCache, SQLiteCache
from .campaign import Campaign
from .config import CampaignConfig, load_config
from .executors import (
    AWSBatchExecutor,
    AzureBatchExecutor,
    BaseExecutor,
    DaskJobQueueExecutor,
    GoogleBatchExecutor,
    KubernetesExecutor,
    LocalExecutor,
    NomadExecutor,
    PBSExecutor,
    SlurmExecutor,
)
from .jobqueue import JobQueue
from .logging import get_logger, setup_logging
from .monitoring import RunTrace, StepTrace
from .observability import (
    CloudWatchBackend,
    NullBackend,
    ObservabilityBackend,
    OpenTelemetryBackend,
    PrometheusBackend,
)
from .pareto import ParetoFront, ParetoSolution
from .registry import CampaignRecord, CampaignRegistry
from .storage import (
    AzureBlobStorage,
    GCSStorage,
    LocalStorage,
    ResultStorage,
    ResultStorageUploader,
    S3Storage,
    build_result_storage,
)
from .validation import ValidationError
from .weather import (
    EPWDownloadError,
    EPWValidationError,
    discover_epw_files,
    download_epw,
    validate_all_epw_files,
    validate_epw,
    validate_epw_header,
)
from .work import SevereEnergyPlusError

__all__ = [
    "AlgorithmRegistry",
    "BaseAlgorithm",
    "LHSAlgorithm",
    "SobolAlgorithm",
    "HaltonAlgorithm",
    "CacheKey",
    "SQLiteCache",
    "Campaign",
    "CampaignConfig",
    "load_config",
    "BaseExecutor",
    "LocalExecutor",
    "SlurmExecutor",
    "AWSBatchExecutor",
    "AzureBatchExecutor",
    "DaskJobQueueExecutor",
    "GoogleBatchExecutor",
    "KubernetesExecutor",
    "NomadExecutor",
    "PBSExecutor",
    "JobQueue",
    "RunTrace",
    "StepTrace",
    "ObservabilityBackend",
    "NullBackend",
    "CloudWatchBackend",
    "PrometheusBackend",
    "OpenTelemetryBackend",
    "ParetoFront",
    "ParetoSolution",
    "CampaignRegistry",
    "CampaignRecord",
    "SevereEnergyPlusError",
    "ValidationError",
    "EPWValidationError",
    "EPWDownloadError",
    "discover_epw_files",
    "download_epw",
    "validate_all_epw_files",
    "validate_epw",
    "validate_epw_header",
    "get_logger",
    "setup_logging",
    "ResultStorage",
    "LocalStorage",
    "S3Storage",
    "GCSStorage",
    "AzureBlobStorage",
    "ResultStorageUploader",
    "build_result_storage",
]

setup_logging()
