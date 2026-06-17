"""osimflow — community-driven parametric OpenStudio simulation campaigns.

Foundation selected via the architecture decision in
`.agents/results/architecture/0001-workflow-framework.md` and validated
in `.agents/results/decision-verdict.md`. This is the canonical public
API; everything else is an implementation detail.
"""

from .alerting import AlertManager, build_alert_manager
from .algorithms import AlgorithmRegistry, BaseAlgorithm, LHSAlgorithm
from .algorithms.doe_analysis import DOEAnalysis
from .algorithms.halton import HaltonAlgorithm
from .algorithms.sobol import SobolAlgorithm
from .cache import CacheKey, CacheStats, SQLiteCache
from .campaign import Campaign, QuotaExceededError
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
from .config import CampaignConfig, ResourceQuota, coerce_variable_type, load_config
from .cost_tracking import CampaignCostSummary, CostEstimate, CostTracker
from .data_point_manager import DataPoint, DataPointManager, DataPointStatus
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
from .measures import (
    AmbiguousVariableError,
    BCLMeasureError,
    DiscoveredMeasure,
    MeasureArgument,
    MeasureRegistry,
    MeasureRegistryError,
    UnmappedVariableError,
)
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
    detect_climate_zone_from_stat,
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
    "DOEAnalysis",
    "SobolAlgorithm",
    "HaltonAlgorithm",
    "CacheKey",
    "CacheStats",
    "SQLiteCache",
    "Campaign",
    "CampaignConfig",
    "ResourceQuota",
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
    "QuotaExceededError",
    "ValidationError",
    # Version detection
    "VersionDetectionError",
    "detect_openstudio_version",
    "get_compatible_container_tag",
    "verify_version_compatibility",
    # Alerting
    "AlertManager",
    "build_alert_manager",
    # Cost tracking
    "CostEstimate",
    "CostTracker",
    "CampaignCostSummary",
    # Data point lifecycle management (#418, #419, #420)
    "DataPoint",
    "DataPointManager",
    "DataPointStatus",
    # EPW validation
    "EPWValidationError",
    "EPWDownloadError",
    "detect_climate_zone_from_stat",
    "discover_epw_files",
    "download_epw",
    "validate_all_epw_files",
    "validate_epw",
    "validate_epw_header",
    # Logging
    "get_logger",
    "setup_logging",
    # Storage
    "ResultStorage",
    "LocalStorage",
    "S3Storage",
    "GCSStorage",
    "AzureBlobStorage",
    "ResultStorageUploader",
    "build_result_storage",
    # Task queue
    "TaskQueue",
    "DaskTaskQueue",
    "NoOpTaskQueue",
    "TaskHandle",
    "TaskQueueStatus",
    "build_task_queue",
    # Document store
    "DocumentStore",
    "DocumentStoreError",
    "DocumentNotFoundError",
    "DuplicateDocumentError",
    "SQLiteDocumentStore",
    "build_document_store",
    # Measure registry (issue #532)
    "MeasureRegistry",
    "MeasureArgument",
    "DiscoveredMeasure",
    "MeasureRegistryError",
    "UnmappedVariableError",
    "AmbiguousVariableError",
]

setup_logging()
