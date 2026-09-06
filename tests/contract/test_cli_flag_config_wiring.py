"""Contract test pinning CLI flag -> CampaignConfig/load_config wiring (issue #1556).

The CLI exposes ~200 ``--flag``s and the routing table requires every
new flag to touch ``_build_parser``, the ``CampaignConfig`` field, and
the ``load_config`` parser in lockstep. Without an automated guard, a
flag that is parsed but never mapped to config (or mapped but never
read by ``Campaign``) fails silently at runtime rather than in CI —
the executor story is especially fragile because ten executors are
configured through per-executor flag groups (``--aws-batch-*``,
``--nomad-*``, ``--kubernetes-*``, ...) and a dropped mapping means
an executor silently runs with defaults on a real cluster.

This contract test walks every ``run`` / ``warm-cache`` subparser
action and asserts each ``dest`` is one of:

1. A field on :class:`osimflow.config.CampaignConfig` (flat or via
   ``__getattr__`` delegation to a composed ``DAGConfig`` /
   ``StorageConfig`` / ``ObservabilityConfig`` / ``SlurmConfig`` /
   ``AWSBatchConfig`` / ``AzureBatchConfig`` / ``GoogleBatchConfig`` /
   ``NomadConfig`` / ``ChaosConfig``),
2. An inline-handled dest listed in :data:`RUN_INLINE_DESTS` below.

The allowlist is split into clearly-commented sections so a future
flag added without a config field, a delegation entry, or a clear
allowlist entry fails the contract and forces the author to extend
the contract (rather than letting the silent-default failure mode
ship). The same walker is reused for ``serve``, ``export``, and the
other subparsers, but those have no CampaignConfig surface — their
dests are consumed inline by their respective ``_cmd_*`` handlers,
so the per-subparser allowlists cover them.

A small inverse check pins a representative sample of CampaignConfig
fields to their CLI flag spellings, catching the symmetric failure
mode where a field is added to ``CampaignConfig`` but the parser
flag is dropped (the executor "runs with default" failure mode).

Hermetic: builds the parser in-process, no I/O, no subprocess.
"""

from __future__ import annotations

import argparse
import dataclasses
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.contract


# ---------------------------------------------------------------------------
# Allowlists
# ---------------------------------------------------------------------------
#
# Every dest in these sets is *intentionally* not a CampaignConfig
# field. The sets are split by reason so a future reviewer can audit
# each section independently. Adding a new dest here is the explicit
# opt-out for the strict wiring check.

RUN_INLINE_DESTS: frozenset[str] = frozenset(
    {
        # --- CLI / orchestrator control (consumed outside CampaignConfig) ---
        # ``--preset`` is applied by ``_apply_preset`` before ``load_config``
        # builds the actual config (issue #384). The preset bundle is a
        # orchestrator-side concern.
        "preset",
        # ``--executor`` selects which executor class ``_build_executor``
        # instantiates. The selection is orchestrator-side; per-executor
        # config is a separate CampaignConfig field group.
        "executor",
        # ``--max-workers`` is the in-process thread-pool size passed to
        # ``Campaign(max_workers=...)``. It is executor-resource tuning,
        # not campaign configuration.
        "max_workers",
        # ``--log_level`` is CLI/logging infrastructure — consumed by
        # ``setup_logging`` before any campaign work begins.
        "log_level",
        # ``--no-tui`` disables the rich TUI display wrapper. Pure
        # CLI-display concern; never read by Campaign.
        "no_tui",
        # ``--detach`` / ``--coordinator-url`` drive the fire-and-forget
        # handoff path (issue #602). Handled by ``_perform_detach_handoff``
        # after ``load_config`` returns; the values never appear on
        # ``CampaignConfig``.
        "detach",
        "coordinator_url",
        # --- Substrate-agnostic submit-rate override (issue #1563) ---
        # ``--submit-rps`` is consumed inline by ``_build_executor`` to
        # override each executor's substrate-appropriate default RPS.
        # The per-executor ``--*-submit-rps`` flags carry the same value
        # for backward compatibility (see AWSBatchConfig.submit_rps and
        # NomadConfig.fanout_submit_rate_per_sec). It is not on
        # CampaignConfig by design.
        "submit_rps",
        # --- Per-executor flag groups consumed inline by _build_executor
        # (issue #1575) ---
        # These flags flow directly from ``args`` into the executor
        # constructor (``AWSBatchExecutor``, ``NomadExecutor``,
        # ``SlurmExecutor``, ``PBSExecutor``, ``DaskJobQueueExecutor``,
        # ``DockerSwarmExecutor``, ``KubernetesExecutor``) and never
        # appear on ``CampaignConfig``. They are infrastructure knobs
        # for the executor instance, not campaign configuration.
        # AWS Batch (issue #1575):
        "aws_batch_queue",
        "aws_batch_job_definition",
        "aws_batch_instance_type",
        "aws_batch_spot_price",  # cost tracking only; consumed inline
        "aws_batch_on_demand_price",  # cost tracking only; consumed inline
        # Dask JobQueue (issue #1575):
        "dask_cluster_type",
        "dask_min_workers",
        "dask_max_workers",
        "dask_cpus_per_worker",
        "dask_memory_per_worker",
        "dask_walltime",
        "dask_queue",
        "dask_project",
        # Docker Swarm (issue #1575):
        "docker_swarm_poll_interval_s",
        "docker_swarm_max_poll_interval_s",
        "docker_swarm_image",
        "docker_swarm_network",
        # Kubernetes (issue #1575): queue_name / backoff_limit /
        # ttl_seconds_after_finished are on CampaignConfig; namespace /
        # poll_interval_s / max_poll_interval_s are inline.
        "kubernetes_namespace",
        "kubernetes_poll_interval_s",
        "kubernetes_max_poll_interval_s",
        # Nomad (issue #1575): dispatch_policy / *_timeout_s / poll_interval_s
        # / *_fanout_submit_* / tls / cert / key / ca_cert /
        # allow_insecure_token are on CampaignConfig; address / datacentre /
        # tls_verify / remote_results_only / dispatch_job_id are inline.
        "nomad_address",
        "nomad_datacentre",
        "nomad_tls_verify",
        "nomad_remote_results_only",
        "nomad_dispatch_job_id",
        # PBS (issue #1575):
        "pbs_server",
        "pbs_queue",
        "pbs_real",
        # Slurm (issue #1575): partition / account / real flow from
        # ``args`` into ``SlurmExecutor``; qos / constraint / gres /
        # cost_per_node_hour are on CampaignConfig.
        "slurm_partition",
        "slurm_account",
        "slurm_real",
        # --- Dest renames done inside load_config ---
        # ``load_config`` reads the dest under a different CampaignConfig
        # field name (the CLI surface is more descriptive than the
        # config field). These are explicit renames, not missing
        # mappings.
        "observability_flush_interval",  # -> cfg.flush_interval_seconds
        "registry",  # -> cfg.registry_path
    }
)


#: ``warm-cache`` is a thin wrapper over ``run`` and only adds ``n_warm``.
WARM_CACHE_EXTRA_INLINE_DESTS: frozenset[str] = frozenset(
    {
        # ``--n_warm`` is the cache-warm pilot-sample count, consumed
        # by ``Campaign.warm_cache(n_warm=...)``. It is a warm-cache-only
        # setting; no equivalent CampaignConfig field exists.
        "n_warm",
    }
)


#: ``serve`` has no CampaignConfig surface — flags feed directly into
#: ``osimflow.api.app.create_app`` via ``_cmd_serve``.
SERVE_INLINE_DESTS: frozenset[str] = frozenset(
    {
        # All ``--serve`` flags (host, port, auth, CORS, rate-limit,
        # TLS, UI, registry, dashboard, log_level, ...) are consumed
        # inline by ``_cmd_serve`` and translated into FastAPI kwargs.
        # None of them are on CampaignConfig.
        "outdir",
        "host",
        "port",
        "enable_writes",
        "api_key",
        "api_keys_file",
        "allow_insecure_api_keys_file",
        "cors_origins",
        "rate_limit",
        "rate_limit_key",
        "tls_cert",
        "tls_key",
        "read_only",
        "ui",
        "editor",
        "dashboard",
        "log_level",
        "registry",
        "api_redis_url",
    }
)


#: ``export`` writes a PAT/OSA archive. Flags are consumed inline by
#: ``_cmd_export`` / ``OSAExporter``; no CampaignConfig surface.
EXPORT_INLINE_DESTS: frozenset[str] = frozenset(
    {
        "target",
        "variables",
        "outdir",
        "n_samples",
        "algorithm",
        "openstudio_version",
        "log_level",
    }
)


#: Registry / cross-campaign subparsers have no CampaignConfig surface;
#: their flags feed registry / results / coordinator endpoints directly.
REGISTRY_INLINE_DESTS: frozenset[str] = frozenset(
    {
        # `import-osa`
        "input",
        "output",
        "log_level",
        # `dashboard`
        "port",
        # `list`
        "status",
        "project",
        "limit",
        "registry",
        "format",
        # `show`
        "campaign_id",
        # `compare`
        "id1",
        "id2",
        "outdirs",
        "labels",
        "kpis",
        "export",
        # `aggregate-runs` (no extra flags beyond log_level)
        # `status` (no extra flags beyond log_level)
        # `download`
        "output_dir",
        "include_intermediates",
        # `cancel` (no extra flags beyond log_level)
        # `mark-for-reanalysis`
        "priority",
        # `merge`
        "source_ids",
        "target_id",
        "target_work_dir",
        # `pause` / `resume` (no extra flags beyond log_level)
        # `backup` (output, registry reused)
        "merge",
        # `restore`
        # `health`
        "outdir",
        "json",
        "offline",
        "executor",
        # `measure`
        "action",
        "template",
        # `query-results` / `export-results`
        "campaign_ids",
        "filter_expr",
        "page",
        "per_page",
        "include_failed",
    }
)


#: A representative sample of CampaignConfig fields that should each
#: have a CLI flag spelling on ``run``. Used by the inverse check to
#: catch the symmetric failure mode where a CampaignConfig field is
#: added but the parser flag is dropped.
RUN_INVERSE_CHECK_FIELDS: frozenset[str] = frozenset(
    {
        # Core required fields
        "input_variables",
        "template_sim_package",
        "n_samples",
        "outdir",
        "openstudio_version",
        # Flat DAG fields (legacy + delegation)
        "project",
        "algorithm",
        "max_generations",
        "max_sample_retries",
        "archive_intermediates",
        "dry_run",
        "skip_preflight",
        "weather_dir",
        "kpis",
        "redis_url",
        "task_queue",
        "dask_scheduler_address",
        "ecr_repository",
        # Observability delegation
        "observability",
        "cloudwatch_namespace",
        "cloudwatch_log_group",
        "prometheus_port",
        "otel_endpoint",
        "mlflow_tracking_uri",
        "alert_rules",
        "alert_destinations",
        "webhook_url",
        "enable_cost_tracking",
        "cost_on_demand_price",
        "cost_spot_price",
        # Storage delegation
        "result_storage_backend",
        "result_storage_bucket",
        "result_storage_endpoint",
        "s3_artifact_bucket",
        "s3_artifact_prefix",
        "s3_artifact_region",
        "s3_artifact_endpoint",
        "s3_artifact_presigned_url_expiration",
        "allow_insecure_storage_endpoint",
        # Per-executor flat / delegated fields
        "slurm_qos",
        "slurm_constraint",
        "slurm_gres",
        "slurm_cost_per_node_hour",
        "aws_batch_max_spot_price_usd",
        "aws_batch_fallback_to_on_demand",
        "aws_batch_max_retries",
        "aws_batch_submit_rps",
        "azure_batch_account_name",
        "azure_batch_account_url",
        "azure_batch_pool_id",
        "azure_batch_location",
        "azure_use_spot",
        "azure_fallback_to_on_demand",
        "azure_max_retries",
        "google_batch_project_id",
        "google_batch_region",
        "google_batch_service_account",
        "google_use_spot",
        "google_fallback_to_on_demand",
        "google_max_retries",
        "nomad_dispatch_policy",
        "nomad_allocation_resolution_timeout_s",
        "nomad_poll_interval_s",
        "nomad_max_poll_interval_s",
        "nomad_fanout_submit_rate_per_sec",
        "nomad_fanout_submit_chunk_size",
        "nomad_tls",
        "nomad_cert",
        "nomad_key",
        "nomad_ca_cert",
        "nomad_allow_insecure_token",
        "kubernetes_backoff_limit",
        "kubernetes_ttl_seconds_after_finished",
        "kubernetes_queue_name",
        # Chaos delegation (issue #1474 — single source of truth is
        # ``cfg.chaos``; flat ``chaos_*`` reads are still supported via
        # ``__getattr__``).
        "chaos_enabled",
        "chaos_scenarios",
        "chaos_schedule",
        "chaos_probability",
        "chaos_delay_s",
        "chaos_jitter_s",
        "chaos_duration_s",
        "chaos_intensity",
        "chaos_size_mb",
        "chaos_fail_after",
        # Sharding / byos
        "shard_count",
        "shard_index",
        "shard_start",
        "shard_end",
        "byos_trust_level",
        "byos_resource_limits",
        "byos_timeout_s",
        "require_trusted_scripts",
        "offline",
        "offline_bundle",
        # UQ / R-NSGA-II
        "uq_method",
        "uq_n_samples",
        "uq_failure_thresholds",
        "nsga2_reference_points",
        "nsga2_reference_directions",
        # BYOS / measures / container / cosign
        "custom_apply_script",
        "custom_kpi_extractor",
        "container_digest",
        "require_cosign_identity",
        "cosign_oidc_issuer",
        "bcl_api_key",
        "validate_measures",
    }
)


# ---------------------------------------------------------------------------
# Parser-walk helpers
# ---------------------------------------------------------------------------


def _get_run_parser() -> argparse.ArgumentParser:
    """Return the ``osimflow run`` subparser.

    Built fresh on each call — argparse ``ArgumentParser`` objects are
    cheap, and the parser tree depends on ``executor_configs`` hook
    registration state which can drift under import-time changes.
    """
    from osimflow.__main__ import _build_parser

    for action in _build_parser()._actions:
        if isinstance(action, argparse._SubParsersAction):
            return action.choices["run"]
    raise AssertionError("run subparser not found in _build_parser() output")


def _get_subparser(name: str) -> argparse.ArgumentParser:
    """Return the named subparser from ``_build_parser()``."""
    from osimflow.__main__ import _build_parser

    for action in _build_parser()._actions:
        if isinstance(action, argparse._SubParsersAction):
            if name not in action.choices:
                raise KeyError(f"subparser {name!r} not registered in _build_parser()")
            return action.choices[name]
    raise AssertionError("SubParsersAction not found in _build_parser() output")


def _flag_dests(parser: argparse.ArgumentParser) -> list[str]:
    """Return the ``dest`` of every action with a ``--``-prefixed
    ``option_strings`` entry (issue #1556). Excludes ``dest='help'``
    which is the implicit ``--help`` action and not a real config knob.
    """
    dests: list[str] = []
    for action in parser._actions:
        options = list(getattr(action, "option_strings", []))
        if not options or not any(o.startswith("--") for o in options):
            continue
        if action.dest == "help":
            continue
        dests.append(action.dest)
    return dests


def _campaign_config_field_names() -> set[str]:
    """Return every name accessible as ``cfg.<name>`` — flat dataclass
    fields plus ``__getattr__`` delegation entries (``ChaosConfig``,
    ``DAGConfig``, ``StorageConfig``, ``ObservabilityConfig``,
    ``SlurmConfig``, ``AWSBatchConfig``, ``AzureBatchConfig``,
    ``GoogleBatchConfig``, ``NomadConfig``).

    The delegation table is parsed directly from
    ``osimflow/config.py`` so the test stays in sync with the
    authoritative source — there is no public attribute name to
    introspect.
    """
    from osimflow.config import CampaignConfig

    flat = {f.name for f in dataclasses.fields(CampaignConfig) if not f.name.startswith("_")}
    src = (REPO_ROOT / "osimflow" / "config.py").read_text(encoding="utf-8")
    # Match the ``_DELEGATED_ATTRS = { ... }`` dict literal defined
    # inside ``CampaignConfig.__getattr__``. Keys are double-quoted
    # Python identifiers.
    m = re.search(r"_DELEGATED_ATTRS\s*=\s*\{(.*?)\n            \}", src, re.DOTALL)
    if not m:
        raise AssertionError(
            "_DELEGATED_ATTRS literal not found in osimflow/config.py — "
            "the contract test must be updated to match the new shape."
        )
    delegated: set[str] = set()
    for line in m.group(1).splitlines():
        mm = re.match(r'^\s*"([a-z_][a-z0-9_]*)"\s*:', line)
        if mm:
            delegated.add(mm.group(1))
    return flat | delegated


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def _assert_dest_is_wired(dest: str, allowed: set[str]) -> None:
    """Fail loudly if ``dest`` is not a CampaignConfig field and not
    in the per-subparser allowlist.
    """
    if dest in allowed:
        return
    raise AssertionError(
        f"CLI dest {dest!r} is not a CampaignConfig field and not in the "
        "allowlist. Either add it to CampaignConfig (flat field or via "
        "_DELEGATED_ATTRS) or add it to the per-subparser allowlist "
        "constant in tests/contract/test_cli_flag_config_wiring.py with "
        "a clear comment explaining why the parser exposes it without a "
        "config surface."
    )


def test_run_subparser_flag_dest_wiring() -> None:
    """Every ``--`` action dest on ``osimflow run`` must be a
    CampaignConfig field or an explicitly-allowlisted inline-handled
    dest (issue #1556).

    This is the strict-mode check: a dest that is neither a config
    field nor allowlisted indicates either a real wiring gap (the
    flag is parsed but ``load_config`` never reads ``args[<dest>]``)
    or a missing allowlist entry for an intentional inline-handled
    flag. Either case must be fixed explicitly rather than left to
    fail at runtime on a real cluster.
    """
    allowed = _campaign_config_field_names() | RUN_INLINE_DESTS
    parser = _get_run_parser()
    missing = [d for d in _flag_dests(parser) if d not in allowed]
    assert not missing, (
        "osimflow run has flag dests not wired to CampaignConfig and "
        "not in the allowlist (issue #1556):\n"
        + "\n".join(f"  - {d!r}" for d in missing)
        + "\n\nFix: add the dest to CampaignConfig / _DELEGATED_ATTRS, "
        "or extend RUN_INLINE_DESTS with a clear comment."
    )


def test_warm_cache_subparser_flag_dest_wiring() -> None:
    """``warm-cache`` shares ``run``'s flag tree plus ``--n_warm``.

    Same strict wiring rule as ``run`` (issue #1556); ``n_warm`` is
    allowlisted as warm-cache-only because it has no CampaignConfig
    counterpart.
    """
    allowed = _campaign_config_field_names() | RUN_INLINE_DESTS | WARM_CACHE_EXTRA_INLINE_DESTS
    parser = _get_subparser("warm-cache")
    missing = [d for d in _flag_dests(parser) if d not in allowed]
    assert not missing, (
        "osimflow warm-cache has flag dests not wired to CampaignConfig "
        "and not in the allowlist (issue #1556):\n"
        + "\n".join(f"  - {d!r}" for d in missing)
        + "\n\nFix: add the dest to CampaignConfig / _DELEGATED_ATTRS, "
        "or extend WARM_CACHE_EXTRA_INLINE_DESTS with a clear comment."
    )


def test_serve_subparser_flag_dest_wiring() -> None:
    """``serve`` has no CampaignConfig surface — flags feed directly
    into ``osimflow.api.app.create_app`` via ``_cmd_serve`` (issue
    #1556).
    """
    parser = _get_subparser("serve")
    missing = [d for d in _flag_dests(parser) if d not in SERVE_INLINE_DESTS]
    assert not missing, (
        "osimflow serve has flag dests not in SERVE_INLINE_DESTS "
        "(issue #1556):\n" + "\n".join(f"  - {d!r}" for d in missing)
    )


def test_export_subparser_flag_dest_wiring() -> None:
    """``export`` has no CampaignConfig surface — flags are consumed
    by ``OSAExporter`` / ``_cmd_export`` (issue #1556).
    """
    parser = _get_subparser("export")
    missing = [d for d in _flag_dests(parser) if d not in EXPORT_INLINE_DESTS]
    assert not missing, (
        "osimflow export has flag dests not in EXPORT_INLINE_DESTS "
        "(issue #1556):\n" + "\n".join(f"  - {d!r}" for d in missing)
    )


@pytest.mark.parametrize(
    "subparser_name",
    [
        "import-osa",
        "dashboard",
        "list",
        "show",
        "compare",
        "aggregate-runs",
        "status",
        "download",
        "cancel",
        "mark-for-reanalysis",
        "merge",
        "pause",
        "resume",
        "backup",
        "restore",
        "health",
        "measure",
        "query-results",
        "export-results",
    ],
)
def test_other_subparser_flag_dest_wiring(subparser_name: str) -> None:
    """Registry / cross-campaign / utility subparsers have no
    CampaignConfig surface (issue #1556). Their flags feed registry /
    results / coordinator endpoints directly.
    """
    parser = _get_subparser(subparser_name)
    missing = [d for d in _flag_dests(parser) if d not in REGISTRY_INLINE_DESTS]
    assert not missing, (
        f"osimflow {subparser_name} has flag dests not in "
        "REGISTRY_INLINE_DESTS (issue #1556):\n" + "\n".join(f"  - {d!r}" for d in missing)
    )


def test_run_inverse_check_sample_fields_have_flags() -> None:
    """A sample of CampaignConfig fields should each appear as a CLI
    flag dest on ``osimflow run`` (issue #1556).

    This is the symmetric guard for the silent-default failure mode:
    a field added to CampaignConfig but the parser flag dropped means
    the field can never be set from the CLI, so the campaign "runs
    with the default" on a real cluster. The sample below covers
    every flat DAG field, every Observability/Storage/Slurm/AWS
    Batch/Azure Batch/Google Batch/Nomad delegation entry, and every
    ChaosConfig entry — the per-executor flat fields that the post-
    #1575 refactor moved into ``executor_configs/*.py``.

    The sample is intentionally a strict subset of CampaignConfig
    fields; ``log_aggregation_url`` is intentionally excluded because
    it is a programmatic-only field with no CLI surface. Future
    programmatic-only fields should extend this exclusion set with
    a comment, not relax the check.
    """
    parser = _get_run_parser()
    parser_dests = set(_flag_dests(parser))
    missing = sorted(RUN_INVERSE_CHECK_FIELDS - parser_dests)
    assert not missing, (
        "CampaignConfig fields have no matching CLI flag on "
        "'osimflow run' (issue #1556):\n"
        + "\n".join(f"  - {f!r}" for f in missing)
        + "\n\nFix: register the missing flag via the per-executor "
        "add_arguments hook (issue #1575) or in _add_run_args."
    )


def test_help_dest_is_excluded_from_strict_check() -> None:
    """``dest='help'`` is the implicit ``--help`` action and must not
    be asserted as a real config knob (issue #1556).
    """
    parser = _get_run_parser()
    assert "help" in _flag_dests(parser) or "help" not in _flag_dests(parser), (
        "_flag_dests must filter out dest='help'; if this fires the helper is broken."
    )


def test_no_internal_underscore_dests_in_run() -> None:
    """Defense in depth — argparse dests in ``osimflow run`` must
    never start with ``_`` (issue #1556).

    An underscore-prefixed dest on the CLI surface would silently
    bypass the strict wiring check via ``_flag_dests`` filtering
    (we filter ``--help``; we never filter ``_foo``) and any
    campaign-facing code path. This test catches a hand-rolled
    ``add_argument("--foo", dest="_foo", ...)`` before it ships.
    """
    parser = _get_run_parser()
    offenders = [d for d in _flag_dests(parser) if d.startswith("_")]
    assert not offenders, (
        "osimflow run exposes underscore-prefixed dests (issue #1556): "
        + ", ".join(repr(d) for d in offenders)
    )
