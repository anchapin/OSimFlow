"""osimflow — community-driven parametric OpenStudio simulation campaigns.

Foundation selected via the architecture decision in
`.agents/results/architecture/0001-workflow-framework.md` and validated
in `.agents/results/decision-verdict.md`. This is the canonical public
API; everything else is an implementation detail.
"""

from .alerting import AlertManager, build_alert_manager
from .algorithms import AlgorithmRegistry, BaseAlgorithm, LHSAlgorithm
from .algorithms.halton import HaltonAlgorithm
from .algorithms.sobol import SobolAlgorithm
from .cache import CacheKey, CacheStats, SQLiteCache
from .campaign import Campaign
from .chaos import (
    ChaosEngine,
    ChaosResult,
    ChaosScenario,
    CPUSpikeInjector,
    FaultInjector,
    FaultType,
    KillSwitchInjector,
    MemoryPressureInjector,
    NetworkDelayInjector,
    run_chaos_scenario,
)
from .config import CampaignConfig, coerce_variable_type, load_config
from .cost_tracking import CampaignCostSummary, CostEstimate, CostTracker
from .distributed_cache import DistributedCache, build_cache
from .distributed_jobqueue import DistributedJobQueue, build_job_queue
from .document_store import (
    DocumentNotFoundError,
    DocumentStore,
    DocumentStoreError,
    DuplicateDocumentError,
    SQLiteDocumentStore,
    build_document_store,
)
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
    new_trace_id,
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
from .taskqueue import (
    DaskTaskQueue,
    NoOpTaskQueue,
    TaskHandle,
    TaskQueue,
    TaskQueueStatus,
    build_task_queue,
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
    "CacheStats",
    "SQLiteCache",
    "Campaign",
    "CampaignConfig",
    "coerce_variable_type",
    "load_config",
    "DistributedCache",
    "build_cache",
    "DistributedJobQueue",
    "build_job_queue",
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
    "new_trace_id",
    "ParetoFront",
    "ParetoSolution",
    "CampaignRegistry",
    "CampaignRecord",
    "SevereEnergyPlusError",
    "ValidationError",
    # Version detection (from origin/main)
    "VersionDetectionError",
    "detect_openstudio_version",
    "get_compatible_container_tag",
    "verify_version_compatibility",
    # Alerting (from origin/main)
    "AlertManager",
    "build_alert_manager",
    # Cost tracking (from this PR)
    "CostEstimate",
    "CostTracker",
    "CampaignCostSummary",
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
    "TaskQueue",
    "DaskTaskQueue",
    "NoOpTaskQueue",
    "TaskHandle",
    "TaskQueueStatus",
    "build_task_queue",
    "DocumentStore",
    "DocumentStoreError",
    "DocumentNotFoundError",
    "DuplicateDocumentError",
    "SQLiteDocumentStore",
    "build_document_store",
]

setup_logging()
