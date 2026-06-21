"""osimflow — community-driven parametric OpenStudio simulation campaigns.

Foundation selected via the architecture decision in
`.agents/results/architecture/0001-workflow-framework.md` and validated
in `.agents/results/decision-verdict.md`. This is the canonical public
API; everything else is an implementation detail.
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass

# Lazy loading map: public name -> (module_path, attribute_name)
_LAZY_IMPORTS: dict[str, tuple[str, str]] = {
    # Alerting
    "AlertManager": ("osimflow.alerting", "AlertManager"),
    "build_alert_manager": ("osimflow.alerting", "build_alert_manager"),
    # Algorithms
    "AlgorithmRegistry": ("osimflow.algorithms", "AlgorithmRegistry"),
    "BaseAlgorithm": ("osimflow.algorithms", "BaseAlgorithm"),
    "LHSAlgorithm": ("osimflow.algorithms", "LHSAlgorithm"),
    "DOEAnalysis": ("osimflow.algorithms.doe_analysis", "DOEAnalysis"),
    "HaltonAlgorithm": ("osimflow.algorithms.halton", "HaltonAlgorithm"),
    "SobolAlgorithm": ("osimflow.algorithms.sobol", "SobolAlgorithm"),
    # Cache
    "CacheKey": ("osimflow.cache", "CacheKey"),
    "CacheStats": ("osimflow.cache", "CacheStats"),
    "SQLiteCache": ("osimflow.cache", "SQLiteCache"),
    # Campaign
    "Campaign": ("osimflow.campaign", "Campaign"),
    "QuotaExceededError": ("osimflow.campaign", "QuotaExceededError"),
    # Chaos
    "ChaosEngine": ("osimflow.chaos", "ChaosEngine"),
    "ChaosResult": ("osimflow.chaos", "ChaosResult"),
    "ChaosScenario": ("osimflow.chaos", "ChaosScenario"),
    "CPUSpikeInjector": ("osimflow.chaos", "CPUSpikeInjector"),
    "FaultInjector": ("osimflow.chaos", "FaultInjector"),
    "FaultType": ("osimflow.chaos", "FaultType"),
    "KillSwitchInjector": ("osimflow.chaos", "KillSwitchInjector"),
    "MemoryPressureInjector": ("osimflow.chaos", "MemoryPressureInjector"),
    "NetworkDelayInjector": ("osimflow.chaos", "NetworkDelayInjector"),
    "run_chaos_scenario": ("osimflow.chaos", "run_chaos_scenario"),
    # Config
    "CampaignConfig": ("osimflow.config", "CampaignConfig"),
    "ResourceQuota": ("osimflow.config", "ResourceQuota"),
    "coerce_variable_type": ("osimflow.config", "coerce_variable_type"),
    "load_config": ("osimflow.config", "load_config"),
    # Cost tracking
    "CostEstimate": ("osimflow.cost_tracking", "CostEstimate"),
    "CostTracker": ("osimflow.cost_tracking", "CostTracker"),
    "CampaignCostSummary": ("osimflow.cost_tracking", "CampaignCostSummary"),
    # Cross-run aggregator
    "CrossRunAggregator": ("osimflow.cross_run_aggregator", "CrossRunAggregator"),
    # Data point lifecycle
    "DataPoint": ("osimflow.data_point_manager", "DataPoint"),
    "DataPointManager": ("osimflow.data_point_manager", "DataPointManager"),
    "DataPointStatus": ("osimflow.data_point_manager", "DataPointStatus"),
    # Distributed cache
    "DistributedCache": ("osimflow.distributed_cache", "DistributedCache"),
    "build_cache": ("osimflow.distributed_cache", "build_cache"),
    # Distributed job queue
    "DistributedJobQueue": ("osimflow.distributed_jobqueue", "DistributedJobQueue"),
    "build_job_queue": ("osimflow.distributed_jobqueue", "build_job_queue"),
    # Document store
    "DocumentNotFoundError": ("osimflow.document_store", "DocumentNotFoundError"),
    "DocumentStore": ("osimflow.document_store", "DocumentStore"),
    "DocumentStoreError": ("osimflow.document_store", "DocumentStoreError"),
    "DuplicateDocumentError": ("osimflow.document_store", "DuplicateDocumentError"),
    "SQLiteDocumentStore": ("osimflow.document_store", "SQLiteDocumentStore"),
    "build_document_store": ("osimflow.document_store", "build_document_store"),
    # Executors — import the module (has its own __init__.py) for backward compat
    "AWSBatchExecutor": ("osimflow.executors", "AWSBatchExecutor"),
    "AzureBatchExecutor": ("osimflow.executors", "AzureBatchExecutor"),
    "BaseExecutor": ("osimflow.executors", "BaseExecutor"),
    "DaskJobQueueExecutor": ("osimflow.executors", "DaskJobQueueExecutor"),
    "DockerSwarmExecutor": ("osimflow.executors", "DockerSwarmExecutor"),
    "GoogleBatchExecutor": ("osimflow.executors", "GoogleBatchExecutor"),
    "KubernetesExecutor": ("osimflow.executors", "KubernetesExecutor"),
    "LocalExecutor": ("osimflow.executors", "LocalExecutor"),
    "NomadExecutor": ("osimflow.executors", "NomadExecutor"),
    "PBSExecutor": ("osimflow.executors", "PBSExecutor"),
    "SlurmExecutor": ("osimflow.executors", "SlurmExecutor"),
    # Handoff record
    "HANDOFF_RECORD_NAME": ("osimflow.handoff_record", "HANDOFF_RECORD_NAME"),
    "IDEMPOTENCY_KEY_HEADER": ("osimflow.handoff_record", "IDEMPOTENCY_KEY_HEADER"),
    "HandoffRecord": ("osimflow.handoff_record", "HandoffRecord"),
    "NoHandoffRecordError": ("osimflow.handoff_record", "NoHandoffRecordError"),
    "handoff_record_exists": ("osimflow.handoff_record", "handoff_record_exists"),
    "read_handoff_record": ("osimflow.handoff_record", "read_handoff_record"),
    "write_handoff_record": ("osimflow.handoff_record", "write_handoff_record"),
    # Job queue
    "JobQueue": ("osimflow.jobqueue", "JobQueue"),
    # Logging
    "get_logger": ("osimflow.logging", "get_logger"),
    "setup_logging": ("osimflow.logging", "setup_logging"),
    # Measures
    "AmbiguousVariableError": ("osimflow.measures", "AmbiguousVariableError"),
    "DiscoveredMeasure": ("osimflow.measures", "DiscoveredMeasure"),
    "MeasureArgument": ("osimflow.measures", "MeasureArgument"),
    "MeasureRegistry": ("osimflow.measures", "MeasureRegistry"),
    "MeasureRegistryError": ("osimflow.measures", "MeasureRegistryError"),
    "UnmappedVariableError": ("osimflow.measures", "UnmappedVariableError"),
    # Monitoring
    "RunTrace": ("osimflow.monitoring", "RunTrace"),
    "StepTrace": ("osimflow.monitoring", "StepTrace"),
    # Notify
    "EmailNotifyBackend": ("osimflow.notify", "EmailNotifyBackend"),
    "NotifyBackend": ("osimflow.notify", "NotifyBackend"),
    "NullNotifyBackend": ("osimflow.notify", "NullNotifyBackend"),
    "SNSNotifyBackend": ("osimflow.notify", "SNSNotifyBackend"),
    "WebhookNotifyBackend": ("osimflow.notify", "WebhookNotifyBackend"),
    "build_notify_backend": ("osimflow.notify", "build_notify_backend"),
    # Observability
    "CloudWatchBackend": ("osimflow.observability", "CloudWatchBackend"),
    "NullBackend": ("osimflow.observability", "NullBackend"),
    "ObservabilityBackend": ("osimflow.observability", "ObservabilityBackend"),
    "OpenTelemetryBackend": ("osimflow.observability", "OpenTelemetryBackend"),
    "PrometheusBackend": ("osimflow.observability", "PrometheusBackend"),
    "new_trace_id": ("osimflow.observability", "new_trace_id"),
    # Pareto
    "ParetoFront": ("osimflow.pareto", "ParetoFront"),
    "ParetoSolution": ("osimflow.pareto", "ParetoSolution"),
    # Registry
    "CampaignRecord": ("osimflow.registry", "CampaignRecord"),
    "CampaignRegistry": ("osimflow.registry", "CampaignRegistry"),
    # Storage
    "AzureBlobStorage": ("osimflow.storage", "AzureBlobStorage"),
    "GCSStorage": ("osimflow.storage", "GCSStorage"),
    "LocalStorage": ("osimflow.storage", "LocalStorage"),
    "ResultStorage": ("osimflow.storage", "ResultStorage"),
    "ResultStorageUploader": ("osimflow.storage", "ResultStorageUploader"),
    "S3ArtifactStorage": ("osimflow.storage", "S3ArtifactStorage"),
    "S3Storage": ("osimflow.storage", "S3Storage"),
    "build_result_storage": ("osimflow.storage", "build_result_storage"),
    # Task queue
    "DaskTaskQueue": ("osimflow.taskqueue", "DaskTaskQueue"),
    "NoOpTaskQueue": ("osimflow.taskqueue", "NoOpTaskQueue"),
    "TaskHandle": ("osimflow.taskqueue", "TaskHandle"),
    "TaskQueue": ("osimflow.taskqueue", "TaskQueue"),
    "TaskQueueStatus": ("osimflow.taskqueue", "TaskQueueStatus"),
    "build_task_queue": ("osimflow.taskqueue", "build_task_queue"),
    # Validation
    "ValidationError": ("osimflow.validation", "ValidationError"),
    # Version detection
    "VersionDetectionError": ("osimflow.version_detection", "VersionDetectionError"),
    "detect_openstudio_version": ("osimflow.version_detection", "detect_openstudio_version"),
    "get_compatible_container_tag": ("osimflow.version_detection", "get_compatible_container_tag"),
    "verify_version_compatibility": ("osimflow.version_detection", "verify_version_compatibility"),
    # Weather
    "EPWDownloadError": ("osimflow.weather", "EPWDownloadError"),
    "EPWValidationError": ("osimflow.weather", "EPWValidationError"),
    "detect_climate_zone_from_stat": ("osimflow.weather", "detect_climate_zone_from_stat"),
    "discover_epw_files": ("osimflow.weather", "discover_epw_files"),
    "download_epw": ("osimflow.weather", "download_epw"),
    "validate_all_epw_files": ("osimflow.weather", "validate_all_epw_files"),
    "validate_epw": ("osimflow.weather", "validate_epw"),
    "validate_epw_header": ("osimflow.weather", "validate_epw_header"),
    # Work
    "SevereEnergyPlusError": ("osimflow.work", "SevereEnergyPlusError"),
}

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
    "LocalExecutor",
    "SlurmExecutor",
    "AWSBatchExecutor",
    "AzureBatchExecutor",
    "DaskJobQueueExecutor",
    "DockerSwarmExecutor",
    "GoogleBatchExecutor",
    "KubernetesExecutor",
    "NomadExecutor",
    "PBSExecutor",
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
    # Chaos (issue #652)
    "ChaosEngine",
    "ChaosResult",
    "ChaosScenario",
    "CPUSpikeInjector",
    "FaultInjector",
    "FaultType",
    "KillSwitchInjector",
    "MemoryPressureInjector",
    "NetworkDelayInjector",
    "run_chaos_scenario",
    "S3ArtifactStorage",
    # Notify
    "EmailNotifyBackend",
    "NotifyBackend",
    "NullNotifyBackend",
    "SNSNotifyBackend",
    "WebhookNotifyBackend",
    "build_notify_backend",
]


def __getattr__(name: str):
    """Lazy loading: import only when an attribute is accessed."""
    if name in _LAZY_IMPORTS:
        module_path, obj_name = _LAZY_IMPORTS[name]
        mod = __import__(module_path, fromlist=[obj_name])
        return getattr(mod, obj_name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
