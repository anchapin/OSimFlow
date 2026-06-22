"""osimflow — community-driven parametric OpenStudio simulation campaigns.

Foundation selected via the architecture decision in
`.agents/results/architecture/0001-workflow-framework.md` and validated
in `.agents/results/decision-verdict.md`. This is the canonical public
API; everything else is an implementation detail.
"""

from typing import TYPE_CHECKING

from ._campaign_observability import ObservabilityManager
from .alerting import AlertManager, build_alert_manager
from .algorithms import AlgorithmRegistry, BaseAlgorithm, LHSAlgorithm
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
from .cross_run_aggregator import CrossRunAggregator
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
    DaskJobQueueExecutor,
    DockerSwarmExecutor,
    GoogleBatchExecutor,
    KubernetesExecutor,
    LocalExecutor,
    NomadExecutor,
    PBSExecutor,
    SlurmExecutor,
)
from .executors.base import BaseExecutor, Handle
from .handoff_record import (
    HANDOFF_RECORD_NAME,
    IDEMPOTENCY_KEY_HEADER,
    HandoffRecord,
    NoHandoffRecordError,
    handoff_record_exists,
    read_handoff_record,
    write_handoff_record,
)
from .jobqueue import JobQueue
from .logging import get_logger, setup_logging
from .measures import (
    AmbiguousVariableError,
    DiscoveredMeasure,
    MeasureArgument,
    MeasureRegistry,
    MeasureRegistryError,
    UnmappedVariableError,
)
from .monitoring import RunTrace, StepTrace
from .notify import (
    EmailNotifyBackend,
    NotifyBackend,
    NullNotifyBackend,
    SNSNotifyBackend,
    WebhookNotifyBackend,
    build_notify_backend,
)
from .observability import (
    NullBackend,
    ObservabilityBackend,
    PrometheusBackend,
    new_trace_id,
)
from .pareto import ParetoFront, ParetoSolution
from .registry import CampaignRecord, CampaignRegistry
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

if TYPE_CHECKING:
    from .algorithms.calibration import CalibrationAlgorithm
    from .algorithms.custom import CustomDOEAlgorithm
    from .algorithms.da import DualAnnealingAlgorithm
    from .algorithms.de import DifferentialEvolutionAlgorithm
    from .algorithms.diag import DiagAlgorithm
    from .algorithms.doe_analysis import DOEAnalysis
    from .algorithms.factorial import FullFactorialAlgorithm, GridSamplingAlgorithm
    from .algorithms.fast99 import FAST99Algorithm
    from .algorithms.ga import GeneticAlgorithm
    from .algorithms.gaisl import IslandModelGAAlgorithm
    from .algorithms.halton import HaltonAlgorithm
    from .algorithms.morris import MorrisAlgorithm
    from .algorithms.nsga2 import NSGA2Algorithm
    from .algorithms.pso import PSOAlgorithm
    from .algorithms.random_sampling import RandomSamplingAlgorithm
    from .algorithms.repeat_all import RepeatAllAlgorithm
    from .algorithms.rgenoud import RgenoudAlgorithm
    from .algorithms.sequential_search import SequentialSearchAlgorithm
    from .algorithms.sobol import SobolAlgorithm
    from .algorithms.spea2 import SPEA2Algorithm
    from .algorithms.uq import UncertaintyQuantification
    from .executors import AWSBatchExecutor
    from .executors.azure_batch_executor import AzureBatchExecutor
    from .executors.dask_jobqueue_executor import DaskJobQueueExecutor
    from .executors.docker_swarm_executor import DockerSwarmExecutor
    from .executors.google_batch_executor import GoogleBatchExecutor
    from .executors.kubernetes_executor import KubernetesExecutor
    from .executors.pbs_executor import PBSExecutor
    from .observability import CloudWatchBackend, OpenTelemetryBackend
    from .storage import (
        AzureBlobStorage,
        GCSStorage,
        S3ArtifactStorage,
        S3Storage,
    )

__all__ = [
    "AlgorithmRegistry",
    "BaseAlgorithm",
    "LHSAlgorithm",
    "Campaign",
    "QuotaExceededError",
    "CacheKey",
    "CacheStats",
    "SQLiteCache",
    "CrossRunAggregator",
    "CampaignConfig",
    "ResourceQuota",
    "coerce_variable_type",
    "load_config",
    "DistributedCache",
    "build_cache",
    "DistributedJobQueue",
    "build_job_queue",
    "BaseExecutor",
    "Handle",
    "AWSBatchExecutor",
    "AzureBatchExecutor",
    "DaskJobQueueExecutor",
    "DockerSwarmExecutor",
    "GoogleBatchExecutor",
    "KubernetesExecutor",
    "LocalExecutor",
    "NomadExecutor",
    "PBSExecutor",
    "SlurmExecutor",
    "JobQueue",
    # Coordinator handoff record (issue #630, Epic #624)
    "HANDOFF_RECORD_NAME",
    "IDEMPOTENCY_KEY_HEADER",
    "HandoffRecord",
    "NoHandoffRecordError",
    "handoff_record_exists",
    "read_handoff_record",
    "write_handoff_record",
    "RunTrace",
    "StepTrace",
    "ObservabilityBackend",
    "NullBackend",
    "PrometheusBackend",
    "new_trace_id",
    "ObservabilityManager",
    "ParetoFront",
    "ParetoSolution",
    "CampaignRegistry",
    "CampaignRecord",
    "SevereEnergyPlusError",
    "ValidationError",
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
    "S3ArtifactStorage",
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
    # Notify
    "NotifyBackend",
    "NullNotifyBackend",
    "EmailNotifyBackend",
    "SNSNotifyBackend",
    "WebhookNotifyBackend",
    "build_notify_backend",
    # Version detection
    "VersionDetectionError",
    "detect_openstudio_version",
    "get_compatible_container_tag",
    "verify_version_compatibility",
]

# Lazy-loaded: cloud/HPC executors
# Note: AWSBatchExecutor and NomadExecutor are defined in executors/__init__.py
# (not separate files). They CAN be accessed via __getattr__ but importing from
# .executors still loads all cloud modules. campaign.py also imports AWSBatchExecutor
# at the top level, so it can't be fully deferred without also fixing campaign.py.
# The other executors (Azure, Google, Dask, Docker, K8s, PBS) have their own files.
_LAZY_EXECUTORS = {
    "AWSBatchExecutor": ".executors",
    "AzureBatchExecutor": ".executors.azure_batch_executor",
    "GoogleBatchExecutor": ".executors.google_batch_executor",
    "DaskJobQueueExecutor": ".executors.dask_jobqueue_executor",
    "DockerSwarmExecutor": ".executors.docker_swarm_executor",
    "KubernetesExecutor": ".executors.kubernetes_executor",
    "NomadExecutor": ".executors",
    "PBSExecutor": ".executors.pbs_executor",
}

# Lazy-loaded: cloud observability backends
_LAZY_OBSERVABILITY = {
    "CloudWatchBackend": ".observability",
    "OpenTelemetryBackend": ".observability",
}

# Lazy-loaded: cloud storage backends
_LAZY_STORAGE = {
    "S3Storage": ".storage",
    "GCSStorage": ".storage",
    "AzureBlobStorage": ".storage",
    "S3ArtifactStorage": ".storage",
}

# Lazy-loaded: heavy optional algorithms
_LAZY_ALGORITHMS = {
    "DOEAnalysis": ".algorithms.doe_analysis",
    "SobolAlgorithm": ".algorithms.sobol",
    "HaltonAlgorithm": ".algorithms.halton",
    "DifferentialEvolutionAlgorithm": ".algorithms.de",
    "DualAnnealingAlgorithm": ".algorithms.da",
    "GeneticAlgorithm": ".algorithms.ga",
    "NSGA2Algorithm": ".algorithms.nsga2",
    "PSOAlgorithm": ".algorithms.pso",
    "MorrisAlgorithm": ".algorithms.morris",
    "FAST99Algorithm": ".algorithms.fast99",
    "RgenoudAlgorithm": ".algorithms.rgenoud",
    "SPEA2Algorithm": ".algorithms.spea2",
    "UncertaintyQuantification": ".algorithms.uq",
    "IslandModelGAAlgorithm": ".algorithms.gaisl",
    "FullFactorialAlgorithm": ".algorithms.factorial",
    "GridSamplingAlgorithm": ".algorithms.factorial",
    "RandomSamplingAlgorithm": ".algorithms.random_sampling",
    "SequentialSearchAlgorithm": ".algorithms.sequential_search",
    "CalibrationAlgorithm": ".algorithms.calibration",
    "CustomDOEAlgorithm": ".algorithms.custom",
    "RepeatAllAlgorithm": ".algorithms.repeat_all",
    "DiagAlgorithm": ".algorithms.diag",
}


def __getattr__(name: str) -> object:
    # Lazy-load heavy cloud/HPC executors
    if name in _LAZY_EXECUTORS:
        _lazy_mod = _LAZY_EXECUTORS[name]
        _cls = getattr(__import__(f"osimflow{_lazy_mod}", fromlist=[name]), name)  # noqa: PLC0415
        return _cls

    # Lazy-load cloud observability backends
    if name in _LAZY_OBSERVABILITY:
        _lazy_mod = _LAZY_OBSERVABILITY[name]
        _cls = getattr(__import__(f"osimflow{_lazy_mod}", fromlist=[name]), name)  # noqa: PLC0415
        return _cls

    # Lazy-load cloud storage backends
    if name in _LAZY_STORAGE:
        _lazy_mod = _LAZY_STORAGE[name]
        _cls = getattr(__import__(f"osimflow{_lazy_mod}", fromlist=[name]), name)  # noqa: PLC0415
        return _cls

    # Lazy-load heavy optional algorithms
    if name in _LAZY_ALGORITHMS:
        _lazy_mod = _LAZY_ALGORITHMS[name]
        _cls = getattr(__import__(f"osimflow{_lazy_mod}", fromlist=[name]), name)  # noqa: PLC0415
        return _cls

    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


setup_logging()
