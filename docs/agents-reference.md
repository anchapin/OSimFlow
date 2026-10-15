# AGENTS.md reference: CLI flags and directory map

<!-- docs-skip -->
> Moved out of `AGENTS.md` (issue #1893) to keep the always-loaded rules file small.
> `tools/check_agents_contract.py` checks each section below **in its own scope**:
> flags must be listed under "CLI flags", files/symbols/config modules under
> "Directory map", DAG steps under `AGENTS.md` §2. A mention elsewhere does not count.

## CLI flags (compact, alphabetical, grouped)

The contract checker *derives* the flag list from the code (issue
#1575): it builds the CLI parser — which calls every per-executor
`add_arguments(parser_group)` hook registered from
`osimflow/executor_configs/` — walks its actions, and requires each
registered `--flag` to appear somewhere in this document (textual
`add_argument` scan of `osimflow/__main__.py` +
`osimflow/executor_configs/*.py` as fallback). Executor-specific flags
physically live in their executor's config module, not in
`osimflow/__main__.py`; everything else is registered in
`osimflow/__main__.py:_build_parser`. Listed in run-subcommand groups
(or "global" if used outside `run`):

- **Executor + parallelism:** `--algorithm`,
  `--aws-batch-allow-long-lived-credentials`, `--aws-batch-instance-type`, `--aws-batch-job-definition`,
  `--aws-batch-max-retries`, `--aws-batch-on-demand-job-definition`,
  `--aws-batch-on-demand-queue`, `--aws-batch-max-spot-price-usd`,
  `--aws-batch-on-demand-price`, `--aws-batch-payload-secret-arn`,
`--aws-batch-queue`, `--aws-batch-spot-price`,
  `--aws-batch-submit-rps`, `--aws-batch-fallback-to-on-demand`, `--submit-rps`,
  `--azure-batch-account-name`, `--azure-batch-account-url`, `--azure-batch-location`,
  `--azure-batch-payload-secret-id`,
  `--azure-batch-pool-id`, `--azure-fallback-to-on-demand`,
  `--azure-max-retries`, `--azure-use-spot`, `--coordinator-url`,
  `--container-digest`, `--cosign-oidc-issuer`, `--dask-cluster-type`, `--dask-cpus-per-worker`,
  `--dask-max-workers`, `--dask-memory-per-worker`,
  `--dask-min-workers`, `--dask-project`, `--dask-queue`,
  `--dask-scheduler-address`, `--dask-walltime`, `--detach`, `--detach-s3`,
  `--docker-swarm-image`, `--docker-swarm-max-poll-interval-s`,
  `--docker-swarm-network`, `--docker-swarm-payload-secret`, `--docker-swarm-poll-interval-s`,
  `--ecr-repository`, `--enable-cost-tracking`, `--executor`,
  `--export`, `--google-batch-project-id`, `--google-batch-region`,
  `--google-batch-service-account`, `--google-batch-payload-secret-name`, `--google-fallback-to-on-demand`,
  `--google-max-retries`, `--google-use-spot`,
  `--kubernetes-backoff-limit`, `--kubernetes-max-poll-interval-s`,
  `--kubernetes-namespace`, `--kubernetes-poll-interval-s`,
  `--kubernetes-queue-name`, `--kubernetes-payload-secret-ref`, `--kubernetes-ttl-seconds-after-finished`,
  `--max-generations`, `--max-sample-retries`, `--max-workers`,
  `--mlflow_tracking_uri`,   `--nomad-address`, `--nomad-allocation-resolution-timeout-s`,
  `--nomad-allow-insecure-token`, `--nomad-ca-cert`, `--nomad-cert`, `--nomad-datacentre`,
  `--nomad-dispatch-job-id`, `--nomad-dispatch-policy`, `--nomad-fanout-submit-chunk-size`,
  `--nomad-fanout-submit-rate-per-sec`, `--nomad-key`,
  `--nomad-max-poll-interval-s`,
  `--nomad-vault-secret-key`, `--nomad-vault-secret-path`, `--nomad-poll-interval-s`,
  `--nomad-remote-results-only`, `--nomad-tls`, `--nomad-tls-verify`,
  `--nsga2-reference-directions`, `--nsga2-reference-points`,
  `--pbs-queue`, `--pbs-real`, `--pbs-server`, `--prebuilt-workflow`
  (issue #1812 — execute the existing OSW package as supplied: no
  `--input_variables` needed, no model mutation / OpenStudio bindings,
  non-empty variables rejected; remote executors reject custom
  apply/extract hooks before submission — `AWSBatchExecutor.validate_work_fn`),
  `--preset`,
  `--preset` accepts one of the named bundles defined in
  `osimflow/cli/parser.py` (`PRESETS`) (issue #384): `local-quick`,
  `local-large`, `slurm-hpc`, `slurm-gpu`, `aws-batch-cloud`,
  `sensitivity-morris`, `sensitivity-fast99`, `optimization-de`,
  `optimization-nsga2`. Individual flags override preset values;
  see `docs/user-guide.md` for the full reference table.
  `--redis-url`, `--s3-artifact-bucket`, `--s3-artifact-endpoint`,
  `--s3-artifact-prefix`, `--s3-artifact-presigned-url-expiration`,
  `--s3-artifact-region`, `--shard-count`, `--shard-end`,
  `--shard-index`, `--shard-start`, `--slurm-account`,
  `--slurm-constraint`, `--slurm-cost-per-node-hour`,
  `--slurm-gres`, `--slurm-partition`, `--slurm-qos`, `--slurm-real`,
  `--task-queue`, `--uq-failure-threshold`, `--uq-method`,
  `--uq-n-samples`.

- **Inputs / outputs:** `--allow-insecure-storage-endpoint`, `--continue-on-error`,
  `--manifest`, `--root`, `--submit-lock-timeout-s`, `--allow-partial`,
  `--from-s3`, `--prefix`,
  `--archive_intermediates`,
  `--bcl-api-key`, `--dry-run`, `--finalize-script`,
  `--finalize-script-timeout` (issue #1685 — wall-clock
  timeout in seconds for the `--finalize-script` subprocess;
  on expiry the child process group is SIGKILLed and a warning
  is logged, but the finalize hook returns normally so
  `Campaign.run()`'s `finally` block can still rewrite `run.json`,
  fire the webhook, and `cache.close()`. Default: 600 s),
  `--init-script`, `--init-script-timeout` (issue #1685 —
  wall-clock timeout in seconds for the `--init-script`
  subprocess; on expiry the child process group is SIGKILLed
  and the campaign aborts with `CampaignError` before any
  step runs. Default: 600 s),
  `--input_variables`, `--kpis`, `--log_level`,
  `--n_samples`, `--no-tui`, `--offline`, `--offline-bundle`,
  `--openstudio_version`, `--outdir`, `--result-storage-backend`,
  `--result-storage-bucket`, `--result-storage-endpoint`,
  `--sample`, `--skip-preflight`, `--template_sim_package`,
  `--track-costs`, `--validate-measures`,
  `--weather_dir`.

- **BYOS:** `--byos-resource-limits`, `--byos-timeout-s`,
  `--byos-trust-level`,
  `--sample-await-timeout-s` (orchestrator-side per-step await
  deadline in seconds; issue #1566 — when set, the campaign passes
  it directly to ``handle.result(timeout=...)`` so a wedged
  substrate becomes a ``TimeoutError`` through the existing
  per-sample failure-recording path instead of parking
  ``_await_one`` indefinitely. When unset, the deadline is
  ``max(time_min_for_step * 60, byos_timeout_s)`` so the
  orchestrator-side bound accommodates the longest expected
  legitimate work. ``None`` (default) preserves the
  pre-#1566 bare-``handle.result()`` semantics.).,
  `--custom_apply_script`, `--custom_kpi_extractor`,
  `--require-cosign-identity`, `--require-trusted-scripts`.

- **Resilience (chaos fault injection, issue #1013):**
  `--chaos-delay-s`, `--chaos-duration-s`, `--chaos-enabled`,
  `--chaos-fail-after`, `--chaos-intensity` (0.0–1.0),
  `--chaos-jitter-s` (must not exceed `--chaos-delay-s`),
  `--chaos-probability`, `--chaos-scenarios`, `--chaos-schedule`, `--chaos-size-mb`. Off by default; the Campaign wires the
  registered scenarios into ``ChaosEngine`` at the configured
  schedule (``before_step`` / ``after_step`` / ``per_sample``)
  and records every invocation under ``run.json.chaos_invocations``.

- **Observability / cost / alerts:** `--alert-destinations`,
  `--alert-rules`, `--api-keys-file`, `--cloudwatch-log-group`,
  `--cloudwatch-namespace`, `--cost-on-demand-price`,
  `--cost-spot-price`, `--log-aggregation-url`, `--observability`,
  `--observability-flush-interval`,
  `--otel-endpoint`, `--prometheus-port`, `--rate-limit-key`,
  `--resource-quota`, `--webhook-url`.

- **Subcommand flags (non-`run`):** `serve` —
  `--allow-insecure-api-keys-file`,
  `--api-key`, `--api-keys-file`, `--api-redis-url`, `--cors-origins`,
  `--dashboard`, `--editor`, `--enable-writes`, `--host`, `--port`,
  `--rate-limit`, `--rate-limit-key`,
  `--rate-limit-trust-x-forwarded-for` (issue #1683 — opt-in
  trust gate that honors ``X-Forwarded-For`` only when the immediate
  upstream is in ``--trusted-proxies``; defaults off so a client
  cannot rotate a spoofed XFF value to obtain a fresh rate-limit
  bucket),
  `--trusted-proxies` (issue #1683 — comma-separated list of
  trusted-proxy CIDR blocks / IPs / hostnames used by the XFF
  trust gate; required whenever
  ``--rate-limit-trust-x-forwarded-for`` is set, otherwise the
  API refuses to start fail-closed),
  `--read-only`,
  `--read-write`, `--registry`, `--tls-cert`, `--tls-key`, `--ui`;
  `export` — `--algorithm`, `--format`, `--limit`, `--n_samples`,
  `--openstudio_version`, `--outdir`, `--target`;
  `list` — `--format`, `--limit`, `--project`, `--registry`, `--status`;
  `status` / `download` — `--include-intermediates`,
  `--output-dir`; `backup` / `restore` — `--merge`, `--output`,
  `--registry`;   `health` — `--json`, `--offline`, `--redis-url`;
  `mark-for-reanalysis` — `--priority`;
  `merge` — `--source-ids`, `--target-id`, `--target-work-dir`;
  `import-osa` — `--output`, `--package-output`, `--template-package`;
  `measure` / `list-measures` — `--filter`, `--project`,
  `--template`, `--variables`;
  `aggregate-runs` / `compare` / `query-results` / `export-results` —
  `--campaign-ids`, `--include-failed`,
  `--no-include-failed`, `--labels`, `--outdirs`, `--page`,
  `--per-page`;
  `warm-cache` — `--n_warm`.

---

## Directory map (contract-checked)

Files listed here are required to appear in `AGENTS.md` by the
contract checker (§0.3). When you add a public file, mention its
name in this section.

### `osimflow/` core

- `osimflow/__init__.py` — public API surface (`__all__`).
- `osimflow/__main__.py` — `argparse` CLI entry point (`osimflow run ...`):
  keeps `main`, the `run` path (`_perform_detach_handoff`, `_cmd_resume`,
  `_cmd_warm_cache`) and re-exports every name moved into `osimflow/cli/`
  so historical `osimflow.__main__.<name>` patch/import seams keep working
  (issue #1890).
- `osimflow/cli/__init__.py` — CLI package marker (issue #1890).
- `osimflow/cli/parser.py` — `_build_parser`, `PRESETS`, `_apply_preset`
  and every per-subcommand `_add_*_args` registration.
- `osimflow/cli/executor.py` — `_build_executor`, `_byos_audit_logger`,
  `_warn_if_mutable_tag`.
- `osimflow/cli/serve.py` — `serve` / `dashboard` subcommands.
- `osimflow/cli/io.py` — `import-osa` / `export`.
- `osimflow/cli/s3.py` — S3 result-store helpers (`--from-s3`, `--detach-s3`).
- `osimflow/cli/registry.py` — `list` / `show` / `compare` /
  `aggregate-runs` / `backup` / `restore`.
- `osimflow/cli/status.py` — `status` / `download`.
- `osimflow/cli/coordinator.py` — Coordinator handoff status/download helpers.
- `osimflow/cli/lifecycle.py` — `run-batches`, `mark-for-reanalysis`, `merge`,
  `health`, `cancel`, `pause`, run-invocation record/replay.
- `osimflow/cli/results.py` — `measure` / `query-results` / `export-results`.
- `osimflow/campaign/` — subpackage (issue #1891): `__init__.py` holds
  `Campaign`, and the former `_campaign_*.py` collaborators live beside it
  as `campaign/<name>.py`. The old private paths are `sys.modules`
  aliases of them kept for back-compat (patch seams and imports at the old
  paths still work, and `osimflow/campaign/code_hashes.py` resolves the
  package root as `.parent.parent`): `osimflow/_campaign_analysis.py`, `osimflow/_campaign_artifacts.py`, `osimflow/_campaign_baseline.py`, `osimflow/_campaign_chaos.py`, `osimflow/_campaign_code_hashes.py`, `osimflow/_campaign_cost_tracker.py`, `osimflow/_campaign_dag.py`, `osimflow/_campaign_epw.py`, `osimflow/_campaign_fanout.py`, `osimflow/_campaign_helpers.py`, `osimflow/_campaign_hooks.py`, `osimflow/_campaign_kpis.py`, `osimflow/_campaign_lifecycle.py`, `osimflow/_campaign_observability.py`, `osimflow/_campaign_optimization.py`, `osimflow/_campaign_quota.py`, `osimflow/_campaign_sample_trace.py`, `osimflow/_campaign_sharding.py`, `osimflow/_campaign_types.py`.
- `osimflow/campaign/dag.py`, `osimflow/campaign/helpers.py` — pieces
  split out of `Campaign` (issue #1890): the DAG step descriptors
  (`StepInputs`, `StepOutputs`, `DAGStep`, `SimResult`, `SampleDict`, the
  per-step `_sig_*` / `_out_*` helpers) and small helpers
  (`_scoped_dry_run_env`, `_make_mlflow_param_view`,
  `_package_content_digest`, `cast_samples`, `cast_variables`,
  `cast_aggregate_result`, `cast_plot_paths`). `campaign/__init__.py` re-exports
  every moved name and keeps the `_STEP_DEPENDENCIES` table (tests patch
  it there).
- `osimflow/campaign/__init__.py` — `Campaign` orchestrator +
  `CONTAINER_SUBSTRATE_EXECUTORS` (issue #1536: executors whose
  substrate pulls the OpenStudio image; `run()` fails loudly when
  the digest is unresolved and no `--container-digest` was given) +
  `CampaignError` +
  `CampaignAbortError` + `QuotaExceededError` + the 7-step DAG.
- `osimflow/_config_quota.py`, `osimflow/_config_coerce.py`,
  `osimflow/_config_sections.py` — leaf pieces split out of `config.py`
  (issue #1890): `ResourceQuota` + `_parse_resource_quota`, variable type
  coercion (`coerce_variable_type`, `_coerce_variables_yml_file`), and the
  focused dataclasses (`DAGConfig`, `StorageConfig`,
  `ObservabilityConfig`, `ChaosConfig`). `config.py` re-exports every
  moved name; they log under the `osimflow.config` logger.
- `osimflow/config.py` — the campaign-config composer: `CampaignConfig`
  + the focused subsystem dataclasses it composes (`ObservabilityConfig`,
  `DAGConfig`, `StorageConfig`) + `ResourceQuota` +
  `coerce_variable_type` + `load_config`. Since issue #1575 the
  per-executor dataclasses (`LocalConfig`, `SlurmConfig`,
  `AWSBatchConfig`, `AzureBatchConfig`, `GoogleBatchConfig`,
  `NomadConfig`) are *defined* in `osimflow/executor_configs/` and
  re-exported here, so `from osimflow.config import SlurmConfig` (and
  the `osimflow` top-level re-export) keep working.
- `osimflow/osw_results.py` — measure-reported values and failure
  messages from `out.osw` / `run.log` (issue #1871): `parse_out_osw`,
  `extract_failure_message_from_osw`,
  `collect_failure_message_from_run_log`, `failure_message`,
  `MEASURE_RESULTS_TOKEN` (`--kpis`
  opt-in token), `SIMULATION_FAILED_KEY`, `OUT_OSW_NAME`,
  `RUN_LOG_NAME`. `work._publish_run_artifacts` copies both files into
  the per-sample sim dir (success or failure); `extract_kpis` includes the
  `<measure_dir_name>.<attribute>` KPIs by default for built-in extraction
  (`kpis=None`) and filters them when `--kpis` is set (issue #2028);
  `aggregate_results.extract_failure` uses the message when
  `eplusout.sql` is missing.
- `osimflow/_work_retry.py`, `osimflow/_work_env.py`,
  `osimflow/_work_heartbeat.py`, `osimflow/_work_osm_mutations.py`,
  `osimflow/_work_stub.py`, `osimflow/_work_artifacts.py` — leaf helpers
  split out of `work.py` (issue #1890): failure classification + retry
  (`SevereEnergyPlusError`, `TransientError`, `classify_failure`,
  `run_with_retry`), subprocess env sanitising (`_sanitize_env`), heartbeat
  / container health, `.osm`/`.osw` mutation helpers, stub `eplusout.sql`
  writer, and run-artifact helpers. `work.py` re-exports every moved name
  and keeps the callers of patched seams (`run_subprocess`, `log`,
  `_is_stub_mode`, …), so `monkeypatch`/`mock.patch` on `osimflow.work.*`
  still takes effect. They are in the `code_hashes["work"]` import closure.
- `osimflow/work.py` — per-step work functions + `BYOS` contract
  (`default_apply_parameters`, `run_openstudio_sim`, `extract_kpis`,
  `aggregate_results`, `generate_plots`,
  `SevereEnergyPlusError`).  Since issue #1869 `default_apply_parameters` writes
  OSW measure arguments and the `__epw_file__` weather target straight
  into `workflow.osw` (no OpenStudio bindings / `model.osm` needed), so
  `APPLY_PARAMETERS` sweeps measure-only OSW packages on `aws_batch`; the
  `custom` algorithm may emit per-sample `weather_file` / `seed_model`
  overrides (`docs/aws-batch-worker-runtime.md`).  `aggregate_results` produces
  `aggregated_results.csv` + `aggregated_results.parquet` (both
  always written) + `failed_simulations.csv` in the campaign
  ``outdir``.
- `osimflow/byos_contract.py` — single source of truth for the BYOS
  function-signature contract (`_BYOS_CONTRACT` + `ByosContractEntry`,
  issue #1061).  Both ``osimflow.byos`` and the inline subprocess
  runner read from this module; the inline runner is generated.
- `osimflow/_byos_runner_generated.py` — generated by
  `tools/_generate_byos_runner.py` from
  `osimflow.byos_contract._BYOS_CONTRACT`.  Holds
  `_SUBPROCESS_RUNNER` (the inline script passed to
  `subprocess.Popen(['python', '-c', ...])`) and a snapshot of the
  contract for the subprocess.  Regenerate via `make contract` or
  pre-commit.
- `osimflow/cache.py` — `SQLiteCache` + `CacheKey` + `CacheStats`
  + `digest_pinned_image_ref` (issue #1536: converts any digested
  form — cache-key, direct, or bare — into a pullable
  `<repo>@sha256:` ref; `None` for the `unresolved` sentinel).
- `osimflow/_sqlite_store.py` — shared SQLite access primitive (issue
  #1564): `connect()` (WAL + `busy_timeout=5000` +
  `synchronous=NORMAL` + `locking_mode=NORMAL` +
  `check_same_thread=False` + `row_factory` + `timeout=10.0`),
  `per_pid_path()` (issue #993 pid-suffixed file scheme),
  `encode_value` / `decode_value` (canonical JSON encode/decode with
  safe fallback), `with_retries()` (exponential-backoff retry on
  `OperationalError`), `transaction()` commit/rollback context
  manager, `StoreConfig` dataclass.  Used by `cache.py`,
  `document_store.py` (SQLiteDocumentStore), `registry.py`,
  `results_db.py`, `event_log.py`, `distributed_cache.py`; each
  store still owns its own persistent connection / threading lock /
  cache-specific PRAGMA tweaks.
- `osimflow/distributed_cache.py` — `DistributedCache` +
  `build_cache` + `campaign_state_namespace` (Redis-backed;
  pid-private local SQLite files in distributed mode). One of four
  Redis-backed planes that share a single `--redis-url`; the scoped
  decision is single-instance Redis (issue #1562 / ADR-0004), with
  campaign-restart-by-replay as the recovery story.
- `osimflow/circuit_breaker.py` — `CircuitBreaker` +
  `CircuitOpenError` + `set_on_transition_callback()`
  (closed/open/half-open; guards the Redis
  data plane in `DistributedCache` and `RedisDocumentStore`
  against persistent outages, issue #1111; `_consecutive_failures`
  is reset to 0 on a failed ``half_open`` → ``open`` transition,
  issue #1379; in ``half_open`` a lock-protected
  `_half_open_in_flight` flag admits exactly one probe per
  cycle and rejects every other concurrent caller until the
  admitted probe resolves via `record_success` /
  `record_failure`, issue #1569 — fan-out threads no longer
  race past the cooldown boundary and burn per-call socket
  timeouts on a still-down Redis).
- `osimflow/queues/` — subpackage (issue #1891) grouping the three ADR-0005
  queue modules by role: `dispatch.py` (work dispatch, was `taskqueue.py`),
  `journal.py` (crash-recovery journal, was `jobqueue.py`) and
  `control_plane.py` (control-plane broadcast, was
  `distributed_jobqueue.py`). `osimflow/taskqueue.py`,
  `osimflow/jobqueue.py` and `osimflow/distributed_jobqueue.py` are
  `sys.modules` aliases of them kept for back-compat (patch seams and
  imports at the old paths still work).
- `osimflow/queues/control_plane.py` — `DistributedJobQueue` +
  `build_job_queue` (Redis pub/sub wrapper).  Carries its own
  `CircuitBreaker(name=f"jobqueue:{campaign_id}")` (issue #1397) so
  persistent Redis outages fail-fast at the publish boundary instead of
  burning the 5 s socket timeout on every job state transition; closes
  the control-plane sibling of issue #1111. Single-instance Redis is
  the supported topology (issue #1562 / ADR-0004).
- `osimflow/storage/` — subpackage (issue #1891) grouping the storage
  modules; `osimflow/storage/__init__.py` holds the former `storage.py`
  content, and `osimflow/_storage_endpoint.py`, `osimflow/_storage_retry.py`,
  `osimflow/_storage_base.py`, `osimflow/_storage_azure.py` are
  `sys.modules` aliases of `osimflow/storage/endpoint.py`,
  `osimflow/storage/retry.py`, `osimflow/storage/base.py`,
  `osimflow/storage/azure.py` kept for back-compat (patch seams and imports at
  the old paths still work).
- `osimflow/storage/endpoint.py`, `osimflow/storage/retry.py`,
  `osimflow/storage/base.py`, `osimflow/storage/azure.py` — pieces split out of the storage package
  (issue #1890): endpoint validation (`_validate_storage_endpoint`),
  transient-error classifier, the `ResultStorage` ABC + `LocalStorage`,
  and `AzureBlobStorage`. The package `__init__` re-exports every moved name and
  keeps `_retry_transient_storage` (patch seam), `S3Storage`, `GCSStorage`, `S3ArtifactStorage`,
  `ResultStorageUploader` and `build_result_storage`; they log under the
  `osimflow.storage` logger.
- `osimflow/storage/__init__.py` — `ResultStorage` ABC (`put_if_absent`: atomic
  create-if-absent, `S3Storage` uses `If-None-Match: *`, base fails closed —
  issue #1881) + `LocalStorage`,
  `S3Storage`, `GCSStorage`, `AzureBlobStorage`,
  `S3ArtifactStorage`, `ResultStorageUploader`,
  `build_result_storage`.  `_validate_storage_endpoint`
  enforces `https://` for non-loopback
  `--result-storage-endpoint` /
  `--s3-artifact-endpoint` URLs unless
  `--allow-insecure-storage-endpoint` is set (issue #1386);
  mirrors `osimflow.distributed_cache.validate_redis_url`.
- `osimflow/queues/dispatch.py` — `ProducerQueue` ABC (fan-out / push) +
  `ConsumerQueue` ABC (fan-in / pull) + `DaskTaskQueue`
  (implements both), `NoOpTaskQueue` (implements both),
  `TaskHandle` (a subclass of the executor `Handle` since
  issue #1543, so `Campaign` fan-out references one handle type),
  `TaskQueueStatus`, `build_task_queue`. Work-dispatch role only —
  distinct from the `queues/journal.py` crash-recovery journal and the
  `queues/control_plane.py` control-plane broadcast (ADR-0005).
- `osimflow/document_store.py` — `DocumentStore` ABC,
  `DocumentStoreError`, `DocumentNotFoundError`,
  `DuplicateDocumentError`, `SQLiteDocumentStore`,
  `RedisDocumentStore`, `build_document_store`
  (issue #1014; dispatch is by `redis_url` / `namespace` mirroring
  `build_cache`). The `RedisDocumentStore` is the source-of-truth
  control plane for the four-plane Redis design (issue #1562 /
  ADR-0004): a Redis outage fails loud rather than silently diverges,
  and recovery is via `osimflow run --outdir <same>` cache replay
  (see `docs/user-guide.md` §7.7).
- `osimflow/_document_store_base.py`, `osimflow/_document_store_sqlite.py`
  — issue #1890 split of `document_store.py`: the base module holds
  `DocumentStore` ABC, the error classes, `apply_update_operators` and
  `_document_matches`; the sqlite module holds `SQLiteDocumentStore` and
  `_build_where_clause`. `osimflow/document_store.py` keeps
  `RedisDocumentStore`, `_get_redis_sync` (test patch seam),
  `build_document_store`, and re-exports every historical name.
- `osimflow/queues/journal.py` — filesystem-based `JobQueue`
  (crash recovery).
- `osimflow/observability/` — subpackage (issue #1891) holding
  `__init__.py` (the backends below), `monitoring.py` and
  `mlflow_hook.py`; `osimflow/monitoring.py` and
  `osimflow/mlflow_hook.py` are `sys.modules` aliases of them kept for
  back-compat (patch seams and imports at the old paths still work).
- `osimflow/observability/monitoring.py` — `RunTrace` (includes
  `chaos_schedule`, `circuit_breaker_states`, `alerts_fired`,
  `accounting_errors`) + `StepTrace` + `record_alert()` +
  `record_accounting_error()` (issue #1674 — fan-out drain
  accounting-failure counter surfaced in `run.json`); writes
  `run.json`.
- `osimflow/observability/__init__.py` — `ObservabilityBackend` ABC +
  `NullBackend`, `CloudWatchBackend`, `PrometheusBackend`, `OpenTelemetryBackend`
  + `new_trace_id` + `record_circuit_breaker_event`.
- `osimflow/campaign/observability.py` — `ObservabilityManager`
  wrapping backend lifecycle from `Campaign`.
- `osimflow/campaign/cost_tracker.py` — internal cost wiring
  used by `Campaign`.
- `osimflow/campaign/quota.py` — `CampaignQuotaGuard` +
  `QuotaExceededError` (defined here, re-exported from
  `osimflow.campaign`): quota enforcement extracted from `Campaign`
  (issue #1462) — start-quota fail-fast, mid-campaign hard-limit
  checks, `max_concurrent_samples` fan-out bounding.
- `osimflow/campaign/chaos.py` — `CampaignChaosWiring` +
  `build_default_chaos_engine`: chaos-engine selection/wiring and the
  schedule-aware `maybe_inject` hook extracted from `Campaign`
  (issue #1462; engine wiring originally issue #1013).
- `osimflow/campaign/sharding.py` — `CampaignSharding`: shard
  selection (`shard_count`/`shard_index` partition + `shard_start`/
  `shard_end` range) and shard labels, extracted from `Campaign`
  (issue #1462).
- `osimflow/campaign/code_hashes.py` — `compute_code_hashes` +
  `code_hash_with_byos` + the AST-based transitive import-closure
  machinery (issues #1021/#1011/#1446) extracted from `Campaign`
  (issue #1462). `Campaign._compute_code_hashes` delegates here and
  still works unbound (`Campaign._compute_code_hashes(stub)` test
  path); `_byos_file_hash` / `_combine_code_hash` /
  `_transitive_import_closure` are re-exported from
  `osimflow.campaign`.
- `osimflow/campaign/lifecycle.py` — `CampaignLifecycle` +
  `CancelSignal` (the explicit `request_cancel` / `request_pause` /
  `request_resume` protocol Campaign satisfies structurally — issue
  #1542 killed the former `Campaign` back-reference) +
  `CancelRegistry` / `cancel_registry` singleton + `handle_signal` +
  `CampaignPauseRequested` (issue #1537 — the dedicated soft-pause
  control-flow signal, distinct from `KeyboardInterrupt`-driven
  cancellation; keeps `run.json` status `paused` with no
  `finished_at` and no job cancellation so `osimflow resume`
  works): cancellation (sticky flag + flock-protected `.stop`
  polling), soft pause/resume, SIGINT/SIGTERM handler bookkeeping
  extracted from `Campaign` (issue #1462). `_CancelRegistry` /
  `_cancel_registry` are re-exported from `osimflow.campaign`.
  No `_campaign_*.py` module imports or type-references `Campaign`
  (issue #1542 acceptance criterion; the documented unbound
  `Campaign._compute_code_hashes(stub)` test path is preserved via
  duck typing).
- `osimflow/campaign/fanout.py` — the fan-out wait loop extracted
  from `Campaign` (issue #1542): `FanoutDeps` (explicit
  call-time dependency bundle — job queue, sample-state,
  sample-trace/checkpoint + alert + cancel/pause callbacks; no
  Campaign back-reference) + `mark_sample_failed` (issue #1570
  failure-accounting path) + `compute_await_deadline` (issue #1566)
  + `submit_and_await_all` (issue #286 concurrent wait core with
  the #443/#1567 recovery loop, #1539 abort propagation, and
  #553/#1537 pause honouring).  Issue #1538 added the bounded
  cancellation path: a poll-cadence wait loop (cancel/pause are
  detectable even when every handle is parked), an early substrate
  kill via the optional `FanoutDeps.cancel_active_jobs` callback
  (wired to `Campaign._cancel_active_jobs`), and
  `_shutdown_fanout_pool_bounded` (`shutdown(wait=False,
  cancel_futures=True)` + a time-bounded join on the
  `osimflow-fanout` threads) so a cancelled campaign always
  returns to write `run.json` instead of hanging on the pool
  context exit.  Standalone unit tests:
  `tests/unit/campaign/test_campaign_fanout.py`; `Campaign` keeps thin
  call-time delegating methods (`_submit_and_await_all` /
  `_mark_sample_failed` / `_compute_await_deadline` +
  `_fanout_deps`) so the historical test seams
  (`patch.object(campaign, "_job_queue")` & co.) keep working.
- `osimflow/campaign/analysis.py` — `CampaignAnalysisMixin`
  (issue #1542): `step_compute_sensitivity_indices` (Sobol, issue
  #346) and `step_compute_uq_indices` (issue #530) extracted from
  `Campaign`; `Campaign` inherits the mixin so the step-method
  surface and the data-driven DAG dispatcher are unchanged.
- `osimflow/campaign/optimization.py` — `CampaignOptimizationMixin`
  (issue #1679): the GA / NSGA-II / DE / SPEA2 generational
  optimization loop (`_run_one_generation`, issue #270 feedback
  loop with the data-driven step dispatcher of #1392), the
  per-generation best-objective extraction
  (`_extract_best_objective`), and the per-generation Pareto
  front persistence (`_persist_pareto_front`, issue #141)
  extracted from `Campaign`; `Campaign` inherits the mixin so
  the historical method surface
  (`campaign._run_one_generation(...)` &
  `campaign._persist_pareto_front(...)` &
  `campaign._extract_best_objective(...)`) is unchanged.
- `osimflow/campaign/runner.py` — `CampaignRunMixin` (issue #1890):
  `Campaign.run` and its run-path helpers (`_abort_run_path_cancel`,
  `_run_dry_run`, `_run_single_sample`, `_run_full_campaign`,
  `_finalize_full_campaign`, `_maybe_inject_chaos`,
  `_maybe_archive_inputs`, `_maybe_fire_webhook`) moved verbatim out
  of `Campaign`; module-level names resolve through the
  `osimflow.campaign` namespace at call time so `mock.patch` seams
  keep working.
- `osimflow/campaign/steps.py` — `CampaignStepsMixin` (issue #1890):
  the `step_*` DAG step bodies (`step_generate_samples`,
  `step_preflight_run_model`, `step_apply_parameters`,
  `step_run_openstudio_sim`, `step_aggregate_results`,
  `step_generate_plots`), the detached-submit helpers and
  `warm_cache`, moved verbatim out of `Campaign` with the same
  call-time `osimflow.campaign` namespace lookup.
- `osimflow/campaign/kpis.py` — `CampaignKpisMixin` (issue
  #1679): the `EXTRACT_KPIS` per-sample fan-out step
  (`step_extract_kpis` — issue #1082 cache key, the #286/#1533/
  #1566 bounded-submit-and-await chunking, the #847/#1180
  failure-recording path) and the worker direct-to-storage
  push helper (`_publish_sample_results`, issue #625) extracted
  from `Campaign`; `Campaign` inherits the mixin so the
  historical method surface
  (`campaign.step_extract_kpis(...)` &
  `campaign._publish_sample_results(...)`) is unchanged and
  the data-driven dispatcher
  (``getattr(self, step_info.method)``) still resolves the
  method.
- `osimflow/campaign/types.py` — `SampleSpec` + `VariableSpec`
  TypedDicts (issue #1542), re-exported from
  `osimflow.campaign` so existing imports keep working.
- `osimflow/campaign/artifacts.py` — `CampaignArtifactWriter`:
  `campaign_meta.json` / `provenance.json` /
  `artifact_manifest.json` writers (issue #277) and intermediate/
  input archiving, extracted from `Campaign` (issue #1462).
- `osimflow/campaign/epw.py` — `CampaignEpwResolver`:
  variables.yml loading, `epw_file` target resolution (issue #55),
  and pre-flight EPW existence/format validation (issue #63),
  extracted from `Campaign` (issue #1462).
- `osimflow/campaign/hooks.py` — init/finalize shell-hook execution
  (issue #108), hook env construction, and the completion webhook
  (issue #283), extracted from `Campaign` (issue #1462).
- `osimflow/campaign/baseline.py` — baseline KPI comparison
  (issue #64) extracted from `Campaign` (issue #1462).
- `osimflow/campaign/sample_trace.py` —
  `CampaignSampleTraceRecorder` + `CampaignAbortError`: per-sample
  `SampleTrace` assembly, per-sample trace-ID minting (issue #436),
  campaign cost-total accumulation (issue #126), and incremental
  checkpointing with the consecutive-failure abort (issue #739;
  `CampaignAbortError` crosses the concurrent fan-out thread
  boundary — issue #1539), extracted from `Campaign` (issue #1462).
- `osimflow/logging.py` — `JSONFormatter` + `RotatingFileHandler`
  + `get_logger`, `setup_logging`, `LogAggregator`.
- `osimflow/registry/` — subpackage (issue #1891) holding
  `__init__.py` (the registry below), `results_db.py`,
  `results_query.py` and `cross_run_aggregator.py`;
  `osimflow/results_db.py`, `osimflow/results_query.py` and
  `osimflow/cross_run_aggregator.py` are `sys.modules` aliases of them
  kept for back-compat (patch seams and imports at the old paths still
  work).
- `osimflow/registry/__init__.py` — `CampaignRegistry` + `CampaignRecord`
  (powers `list`/`show`/`compare`/`backup`/`restore`).
- `osimflow/pareto.py` — `ParetoFront` + `ParetoSolution`.
- `osimflow/measures.py` — `MeasureRegistry` + `MeasureArgument` +
  `DiscoveredMeasure` + `MeasureRegistryError` +
  `UnmappedVariableError` + `AmbiguousVariableError`.
- `osimflow/measure_resolver.py`, `osimflow/measure_versioning.py`
  — measure internals.
- `osimflow/weather.py` — `discover_epw_files`, `download_epw`,
  `validate_epw`, `validate_epw_header`,
  `validate_all_epw_files`, `detect_climate_zone_from_stat`,
  `EPWValidationError`, `EPWDownloadError`.
- `osimflow/version_detection.py` — `VersionDetectionError`,
  `detect_openstudio_version`,
  `get_compatible_container_tag`,
  `verify_version_compatibility`.
- `osimflow/_health_types.py`, `osimflow/_health_executors.py`,
  `osimflow/_health_redis.py` — pieces split out of `health.py` (issue
  #1890): `CheckStatus` / `CheckCategory` / `CheckResult` /
  `HealthReport`, the per-executor substrate checks (`_check_local`,
  `_check_slurm`, … + `_check_task_payload_signing`), and the Redis
  deployment-mode check. `health.py` re-exports every moved name and keeps
  the dispatch table, `run_health_checks`, and the formatters; they log
  under the `osimflow.health` logger.
- `osimflow/health.py` — `osimflow health` subcommand
  (`CheckResult`, `CheckStatus`, `CheckCategory`, `HealthReport`,
  `run_health_checks`). One `_check_<executor>()` per
  `ExecutorRegistry` executor (issue #1024); each returns
  `INFORMATIONAL` by default, promoted to `CRITICAL` when
  `--executor <name>` is passed. Since issue #1463 the health-check
  registration lives on this module's side (`_register_executor_health_checks`
  at import); `osimflow.executors` never imports this module. The
  Redis deployment-mode probe `_check_redis_deployment_mode`
  (issue #1562 / ADR-0004) reports `single` / `sentinel` / `cluster` /
  `unknown` and the Redis version + replication role for `--redis-url`
  when the operator runs `osimflow health --redis-url <URL>`; the
  probe SKIPs cleanly when no URL is set.
- `osimflow/notifications/` — subpackage (issue #1891) holding
  `alerting.py` and `notify.py`; `osimflow/alerting.py` and
  `osimflow/notify.py` are `sys.modules` aliases of them kept for
  back-compat (patch seams and imports at the old paths still work).
- `osimflow/notifications/alerting.py` — `AlertManager`
  (`on_alert` callback for `RunTrace.record_alert` wiring),
  `build_alert_manager`.
- `osimflow/notifications/notify.py` — `NotifyBackend` ABC +
  `EmailNotifyBackend`, `NullNotifyBackend`, `SNSNotifyBackend`,
  `WebhookNotifyBackend` + `build_notify_backend`.
- `osimflow/chaos.py` — `ChaosEngine` + `ChaosResult` +
  `FaultType` + `ChaosScenario` + `FaultInjector` ABC + `CPUSpikeInjector`,
  `MemoryPressureInjector`, `NetworkDelayInjector`,
  `KillSwitchSimulator`, `run_chaos_scenario`.
- `osimflow/cost_tracking.py` — `CostEstimate`, `CostTracker`,
  `CampaignCostSummary`.
- `osimflow/cosign.py` — container image signature verification
  (issue #1385): `CosignVerificationError` +
  `build_cosign_image_ref` + `verify_image_signature` +
  `write_cosign_receipt` + `triangulate_image_ref` (issue #1536:
  resolves a mutable-tag ref to its digest-pinned form via
  `cosign triangulate` BEFORE verification — closing the
  verify-tag/pull-tag TOCTOU) + `DEFAULT_COSIGN_OIDC_ISSUER`.
  When `--require-cosign-identity` is set, `Campaign` init shells
  out to `cosign verify` (keyless sigstore) against the OpenStudio
  image ref and refuses to run on failure — a cache hit must never
  silently consume a substituted image.  The receipt records the
  verified digest and (when triangulated) the source tag.
- `osimflow/data_point_manager.py` — `DataPoint`,
  `DataPointManager`, `DataPointStatus`.
- `osimflow/registry/cross_run_aggregator.py` — `CrossRunAggregator`.
- `osimflow/registry/results_query.py` — cross-campaign results query/export
  helpers (issue #1699): `query_results_cli` (powers
  ``osimflow query-results``), `export_results_cli` (powers
  ``osimflow export-results``), `load_aggregated_results`, and
  `apply_filter`. Pure stdlib + pandas — no `fastapi` import at
  module scope, so the CLI subcommands work on installs without
  the optional `[api]` extra. `osimflow/api/results_query.py`
  imports `apply_filter` / `load_aggregated_results` from here for
  its route handlers; do not import the CLI helpers from the api
  module (use this module directly).
- `osimflow/server_csv.py` — openstudio-server `download_data.csv`
  layout export (issue #1872): `build_server_csv_frame`,
  `combine_server_frames`, `SIMULATION_FAILED_COL`. Powers
  `osimflow export-results --format openstudio-server-csv`; columns
  `name,_id,status,status_message`, then `<measure>.<arg>` variables,
  outputs, and `simulation_failed_message` last.
- `osimflow/s3_campaign.py` — S3-backed detached AWS Batch campaigns
  (issue #1873): `S3CampaignStore`, `S3Handoff`, `new_handoff`,
  `S3CampaignError`, `CampaignDetached`, `COMPLETE_MARKER`
  (`_OSIMFLOW_COMPLETE`, uploaded last by `remote_runner` into each
  sample result dir) + `FAILED_MARKER` (`_OSIMFLOW_FAILED`, issue
  #1878: a worker whose step raised uploads `out.osw` / `run.log` /
  stdout+stderr logs / size-capped `eplusout.err` best-effort, then this
  marker; a Batch retry attempt overwrites it with `RETRYING_STATE`;
  `S3CampaignStore.sample_states` treats either marker as terminal
  (unless `retrying`) and a success marker wins; `AWSBatchHandle` also
  materializes those files on the controller when a job fails).
  `run --detach-s3` writes `<campaign>/_handoff.json`
  + `samples.json` to the bucket and exits; `status|list|download
  --from-s3` (with `--prefix`, `--allow-partial`) read S3 state, and
  `download` finalizes locally (KPI extract + aggregate), skipping
  already-downloaded samples. `download_data.csv` is no longer written by
  default (`S3CampaignStore.download(write_server_csv=True)` opts in; CLI
  users run `osimflow export-results --format openstudio-server-csv`).
  Issue #1881 makes the submit crash/concurrency-safe: `SubmitClaim`
  (generation-numbered `<campaign>/_claims/NNNNNN.json`, created via
  `ResultStorage.put_if_absent`, 15-min renewable lease; `claim_submission`
  / `renew_claim` / `finish_claim` / `release_claim` on `S3CampaignStore`
  with outcomes `CLAIM_ACQUIRED` / `CLAIM_ALREADY_SUBMITTED` /
  `CLAIM_IN_PROGRESS`), per-sample `<campaign>/_jobs/<sample>.json` job
  records (`record_job` / `recorded_jobs`), and `batch_job_name`
  (deterministic `osimflow-<campaign>-<sample>`). `Campaign` claims before
  the first submit; `AWSBatchExecutor._do_submit(job_name=,
  reuse_existing_job=)` dedupes a resumed submit via `_find_existing_job`
  (`list_jobs` name filter, FAILED ignored) because Batch has no client token.
- `osimflow/batches.py` — sequential multi-batch driver (issue #1874):
  `run_batches`, `load_manifest`, `build_run_argv`, `batch_campaign_name`,
  `submit_lock`, `BatchResult`, `BatchManifestError`, `SubmitLockTimeout`.
  Powers `osimflow run-batches --manifest M [--root DIR] [--continue-on-error]
  [--submit-lock-timeout-s N] [--dry-run]`: each batch runs as its own
  `osimflow run` campaign in `<root>/Batch<id>_<name>` under an exclusive
  flock on `<root>/.submit.lock`; summary in `<root>/batches_summary.json`.
- `osimflow/handoff_record.py` — `HandoffRecord` +
  `NoHandoffRecordError` + `IDEMPOTENCY_KEY_HEADER` +
  `HANDOFF_RECORD_NAME` + `read_handoff_record`,
  `write_handoff_record`, `handoff_record_exists` (for
  `--detach` / Coordinator).
- `osimflow/remote_runner.py` — stdlib
  `python -m osimflow.remote_runner` worker for Nomad /
  Kubernetes Jobs (decodes `OSIMFLOW_TASK_PAYLOAD`, pushes
  artifacts to object storage).
- `osimflow/input_staging.py` — controller-to-worker input staging
  for executors with no shared filesystem (issue #1809; AWS Batch):
  `InputStager` (uploads each payload `Path` as content-addressed
  blobs + a SHA-256 manifest under `_inputs/` and replaces it with a
  tagged `staged_input`/`staged_output` reference — the manifest digest
  rides inside the HMAC-signed `OSIMFLOW_TASK_PAYLOAD`),
  `spill_task_payload` / `fetch_spilled_payload` (oversized payloads — AWS Batch caps `containerOverrides` at 8192 B — are uploaded content-addressed under `_inputs/payloads/` and replaced by a signed SHA-256 pointer that `remote_runner` resolves),
  `WorkerPathRemapper` (verified materialization into worker scratch,
  scratch<->controller path mapping so result keys line up with
  `materialize_object_storage_result`), `result_upload_plan`,
  `InputStagingError` (every failure is explicit — never a fallback to
  controller paths). `BaseExecutor._stage_task_inputs` is the shared
  entry used by `AWSBatchExecutor._do_submit`; `remote_runner` verifies
  the transport signature before fetching inputs.
- `osimflow/task_payload_hmac.py` — HMAC-SHA256 signing/verification
  for `OSIMFLOW_TASK_PAYLOAD` (issue #1177):
  `sign_task_payload`, `verify_task_payload`,
  `resolve_payload_secret`, `build_signature_env` +
  `TASK_PAYLOAD_ENV` / `TASK_PAYLOAD_SIG_ENV` /
  `TASK_PAYLOAD_SECRET_ENV` constants and the Nomad meta-key
  mirrors. `KubernetesExecutor` / `NomadExecutor` sign at
  submission; `remote_runner` verifies (via
  `hmac.compare_digest`, fail-closed) before decoding. Secret
  comes from the `OSIMFLOW_TASK_PAYLOAD_SECRET` env var (no CLI
  flag — avoids new public surface).  Since issue #1549 a second
  HMAC covers the result-transport settings:
  `build_transport_signature_env` +
  `canonical_result_transport_settings` +
  `RESULT_TRANSPORT_SIG_ENV` / `RESULT_TRANSPORT_SIG_META_KEY` /
  `RESULT_TRANSPORT_SIGNED_FIELDS`.  Every transport-carrying
  executor (AWS/Azure/Google Batch, Kubernetes, Nomad, Docker
  Swarm) emits `OSIMFLOW_RESULT_TRANSPORT_SIG` next to the
  `OSIMFLOW_RESULT_*` env vars; `remote_runner` verifies it
  fail-closed before constructing the storage backend, and the
  `allow_insecure_storage_endpoint` escape hatch is honored at
  the worker only when it authenticated as part of the signed
  settings.
- `osimflow/apply_params.py`, `osimflow/aggregation.py`,
  `osimflow/audit.py`, `osimflow/byos.py`,
  `osimflow/event_log.py`, `osimflow/json_utils.py`,
  `osimflow/registry/results_db.py`,
  `osimflow/validation.py` (`ValidationError`) — internal
  supporting modules.
- `osimflow/webhook.py` — `WebhookClient` (campaign-completion
  callback POSTs with retry/backoff) + `WebhookSSRFError` +
  `WebhookDeliveryError`.  SSRF validation (issues #1175, #1671):
  hostnames resolved via `socket.getaddrinfo` with fail-closed
  semantics on resolution failure, `_BLOCKED_NETWORKS` covering
  loopback / RFC1918 / CGNAT (100.64.0.0/10) / link-local /
  metadata ranges (IPv4-mapped IPv6 unwrapped), and every redirect
  hop re-validated (scheme + host) by `_SSRFValidatingRedirectHandler`
  before being followed.
- `osimflow/manifest.py` — per-sample manifest construction,
  atomic publishing, and the Coordinator status client
  (`report_sample_completion`); hosts
  `_validate_coordinator_url` — the https gate for
  `--coordinator-url` / `OSIMFLOW_COORDINATOR_URL` (issue #1550;
  loopback-exempt, override reuses
  `--allow-insecure-storage-endpoint`).
- `osimflow/offline_bundle.py` — fail-closed verification of
  `--offline-bundle` assets against the bundle's
  `bundle_manifest.json` SHA-256 digests (issue #1640):
  `verify_offline_bundle()` + `OfflineBundleError` +
  `MANIFEST_FILENAME`. Invoked from the `osimflow run` CLI path
  right after `load_config` — before any bundle asset (pip wheel /
  docker tar / EPW) is consumed; mismatch or missing manifest
  aborts with exit 1.
- `osimflow/errors.py` — single package root
  (`OSimFlowError`) + intermediate mixins
  (`OSimFlowRuntimeError`, `OSimFlowValueError`) so every
  module-level exception outside `client.py` / `api/`
  inherits from `OSimFlowError` while keeping its historic
  `RuntimeError` / `ValueError` catch behaviour (issue #1484).
- `osimflow/client.py` — typed Python client for the `[api]`
  REST surface. **Canonical** (sole) REST client; the deprecated
  `osimflow_client/` shim was removed (issue #1927).
- `osimflow/observability/mlflow_hook.py` — optional MLflow integration
  (lazy-imports `mlflow`).
- `osimflow/tui.py` — optional `rich`-based terminal UI.
- `osimflow/_eval_safe.py`, `osimflow/_subprocess_utils.py` —
  internal helpers.  `_subprocess_utils.run_subprocess` (the
  per-sample log-capture helper shared by `work.py` and the
  executors package) spawns its child via `Popen` and registers it
  in a thread-keyed registry so `terminate_active_subprocesses()`
  can SIGTERM every in-flight work subprocess during graceful
  shutdown (issue #1538 — the local substrate's kill API, invoked
  by `LocalExecutor.cancel()`).
- `osimflow/py.typed` — PEP 561 marker.

### `osimflow/_work_scripts/`

CLI scripts invoked by the work layer: `generate_lhs.py`,
`apply_params_to_model.py`, `extract_kpis.py`,
`aggregate_results.py`, `generate_plots.py`,
`excel_to_variables.py`. Mirrored 1:1 by `bin/*.py` shims.

### `osimflow/algorithms/`

- `__init__.py` — `BaseAlgorithm` ABC, `AlgorithmRegistry`,
  `LHSAlgorithm`, `discover_plugins()` (entry point
  `osimflow.algorithms`).
- `sobol.py`, `halton.py` — Sobol / Halton quasi-random
  (`scipy.stats.qmc`).
- `de.py`, `da.py` — `DifferentialEvolutionAlgorithm`,
  `DualAnnealingAlgorithm` (scipy).
- `ga.py` — `GeneticAlgorithm` (DEAP; `[ga]` extra).
- `nsga2.py`, `pso.py`, `spea2.py` — multi-objective
  (pymoo; `[optimization]`).
- `gaisl.py` — island-model parallel GA.
- `rgenoud.py` — hybrid GA + BFGS local search.
- `morris.py`, `fast99.py` — Morris / FAST99 sensitivity
  (SALib; `[sensitivity]`).
- `factorial.py` — `FullFactorialAlgorithm` +
  `GridSamplingAlgorithm`.
- `random_sampling.py` — pure Monte Carlo.
- `sequential_search.py` — deterministic sweep + adaptive
  sampling.
- `calibration.py` — BM25-based utility-bill calibration.
- `custom.py` — `CustomDOEAlgorithm` (CSV or Python callable).
- `qdiscrete.py` — inverse-CDF discrete sampling
  (`DoE.base::qdiscrete`).
- `repeat_all.py` — `RepeatAllAlgorithm`.
- `diag.py` — `DiagAlgorithm` (OAT, mirrors
  openstudio-server's `diag.rb`).
- `doe_analysis.py` — `DOEAnalysis` (main effects,
  interactions, ANOVA).
- `uq.py` — Monte Carlo uncertainty propagation + failure
  probability.

### `osimflow/executor_configs/` (contract-checked)

Per-executor configuration home (issue #1575). One module per
`ExecutorRegistry` key, each owning that executor's `XConfig`
dataclass (where one exists) plus an
`add_arguments(parser_group)` hook that registers the executor's
flags on the `run` / `warm-cache` subparser — the exact flags,
defaults, and help that used to live in `osimflow/__main__.py`.
`osimflow.config` imports/re-exports every `XConfig`;
`osimflow.__main__._add_run_args` calls `add_executor_arguments`
instead of hand-coding the executor argparse tree. The package is a
leaf (stdlib-only imports) so `osimflow.config` never pulls executor
SDKs into its import graph.

- `__init__.py` — re-exports the `XConfig` classes and the registry
  functions; registers all ten built-in hooks.
- `base.py` — `ExecutorArgumentHook` type + the reload-stable
  `_EXECUTOR_ARGUMENT_HOOKS` registry (issue #1463 anchoring pattern)
  + `register_executor_arguments` + `iter_executor_argument_hooks` +
  `add_executor_arguments`. `ExecutorRegistry.register_arguments` /
  `iter_argument_hooks` (in `osimflow/executors/__init__.py`)
  delegate here; `ExecutorRegistry.discover_plugins` auto-registers a
  plug-in class's `add_arguments` staticmethod under its entry-point
  name, so third-party executors get first-class CLI configuration.
- `aws_batch.py` — `AWSBatchConfig` + the `--aws-batch-*` and
  `--ecr-repository` flags.
- `azure_batch.py` — `AzureBatchConfig` + the `--azure-*` flags.
- `dask_jobqueue.py` — the `--dask-*` cluster flags (`--task-queue`
  and `--dask-scheduler-address` stay in `__main__.py`: they configure
  the campaign-level task queue, not the executor).
- `docker_swarm.py` — the `--docker-swarm-*` flags.
- `google_batch.py` — `GoogleBatchConfig` + the `--google-*` flags.
- `kubernetes.py` — the `--kubernetes-*` flags (native Job controls
  stay flat on `CampaignConfig`, issue #997).
- `local.py` — `LocalConfig` + `--max-workers`.
- `nomad.py` — `NomadConfig` + the `--nomad-*` flags.
- `pbs.py` — the `--pbs-*` flags.
- `slurm.py` — `SlurmConfig` + the `--slurm-*` flags.
- `factory.py` — the shared `build_executor(name, **kwargs)` factory
  (issue #1681) that maps an `ExecutorRegistry` name to constructor
  kwargs. Both `osimflow.__main__._build_executor` (CLI) and
  `osimflow.api.campaigns._build_executor_from_request` (REST) call
  into this module so the two surfaces cannot drift. Per-executor
  `kwargs_for_executor(**kwargs)` hooks live next to each
  `add_arguments` hook in the per-executor modules above and are
  registered in `osimflow/executor_configs/__init__.py` next to the
  argument hooks. Plug-in executors without a `kwargs_for_executor`
  hook fall back to forwarding `**kwargs` directly to the constructor.

### `osimflow/executors/` (contract-checked)

Since issue #1463 `__init__.py` holds only the shared surface —
`ExecutorRegistry` (`register_health_check(name, fn)` attaches a
per-executor health check, issue #1024; `iter_health_checks()`
feeds `osimflow.health.run_health_checks`) plus `discover_plugins()`
via entry point `osimflow.executors`, the per-step resource defaults
(`DEFAULT_STEP_RESOURCES` / `get_step_resources`), and the re-export
of every executor so `from osimflow.executors import X` keeps working.
The registry's state dicts are anchored in `base.py`
(`_EXECUTOR_REGISTRY` / `_EXECUTOR_HEALTH_CHECKS`) so they survive
`importlib.reload`; health-check registration is bound from the
`osimflow.health` side only (one-directional: health imports
executors, never the reverse).

**Issue #1574** removed the test patch seams from this package: the
bare ``import time`` / ``import random`` ``# noqa: F401`` lines and
private helper re-exports (``_AWSBatchHandle``, ``_TokenBucketRateLimiter``,
``_SpotPriceCache``, ``_retry_nomad_request``, ``_NOMAD_RETRY_*``,
``_slugify_job_name``, ``_apply_slurm_params``, ...) are gone from
``__init__.py``. Tests now patch through
``osimflow.testing.patch_targets`` (which re-exports those helpers and
the ``time`` / ``random`` stdlib modules). A :pep:`562` ``__getattr__``
deprecation shim keeps third-party plug-ins that still import a
private name from this package working, with a one-shot
``DeprecationWarning`` pointing at the new surface. The contract
checker enforces the new rule by requiring every non-underscore
top-level executor symbol to be named in this section; private names
are explicitly skipped.

`base.py` defines `BaseExecutor` + `Handle` + `SubmitRequest` + the
shared `PollingHandle` poll-retry-fallback state machine with
`PollOutcome` + `poll_until_terminal` + `retry_with_backoff`
(issue #1464 — owns the terminal-poll loop, #1465 deadline, jittered
backoff, retry accounting, and fallback-to-on-demand transition;
`_AzureBatchHandle` and `_GoogleBatchHandle` subclass it, supplying
substrate hooks; executor-author guidance is in
`docs/DEVELOPMENT.md` §7, "Subclassing `PollingHandle` for polling
executors"). Issue #1538 added substrate job cancellation:
`Handle.cancel() -> bool` is the idempotent, never-raising kill
affordance (the base implementation duck-types the backing future's
`cancel()` — covers LocalExecutor's `concurrent.futures.Future`,
SlurmExecutor/PBS-style submitit `Job.cancel` (`scancel` / debug
SIGINT), and Dask futures); `PollingHandle.cancel()` routes through
the `_cancel_job()` substrate hook instead; `BaseExecutor.submit`
registers every issued handle in a lazily-initialised live-handle
registry (`_register_handle`, pruned past
`_LIVE_HANDLE_SWEEP_THRESHOLD` completed entries) so
`BaseExecutor.cancel()` — invoked by
`CampaignLifecycle.cancel_active_jobs()` on every graceful-shutdown
path — sweeps each live handle exactly once (per-handle failures are
logged, never raised; a second sweep is a no-op). Per-substrate kill
APIs: LocalExecutor = `terminate_active_subprocesses()` (SIGTERM the
`run_subprocess` Popen registry) + queued-future cancel; Slurm =
submitit `Job.cancel()` (`scancel`); PBS = `qdel`;
AWS Batch = `batch.terminate_job(jobId=...)`; Azure Batch =
`BatchClient.terminate_task(job_id, task_id)`; Google Batch =
`BatchServiceClient.delete_job(name=...)`; Kubernetes =
`delete_namespaced_job(..., propagation_policy="Foreground")`;
Nomad = `POST /v1/allocation/<id>/stop` (`_NomadClient.stop_allocation`);
Docker Swarm = `services.get(name).remove()`;
Dask-JobQueue = dask `Future.cancel()`.
`transport.py` is the executor-agnostic
result-reference contract (`coerce_transport_mode`,
`validate_transport_mode`, `encode_transport_value`,
`decode_transport_value`, `local_path_to_storage_key`,
`resolve_result_for_callback`, `materialize_object_storage_result`,
`resolve_and_materialize`)
and, since issue #1541, exports the frozen `ResultTransportConfig`
value object (mode + backend + bucket + prefix + endpoint +
`presigned_url_expiration_s`) plus `resolve_transport_argument`:
`Campaign` constructs the config once via
`ResultTransportConfig.from_campaign_config` and threads it as the
single `transport=` parameter through `BaseExecutor.submit()` →
`_do_submit()` → every remote `Handle` (`PollingHandle._transport`),
replacing the historic five loose `result_transport_mode` /
`result_storage_*` kwargs. `submit()` still accepts those five as a
deprecated shim (folded via `resolve_transport_argument`; passing
both forms raises), and `SubmitRequest` exposes them as read-through
properties — the remote-runner `OSIMFLOW_RESULT_*` env-var contract
is unchanged.
`_rate_limiter.py` is the shared `TokenBucketRateLimiter` (issue
#1563): a thread-safe token-bucket used by every executor via
`BaseExecutor.submit`'s template-method acquire (replaces the
pre-#1563 AWS-private `_TokenBucketRateLimiter` and Nomad's ad-hoc
`fanout_submit_rate_per_sec` pacing hook). Every executor
subclass declares a substrate-appropriate `default_submit_rps`
(`LocalExecutor` = `inf`, `SlurmExecutor` / `PBSExecutor` = 100,
`AWSBatchExecutor` / `AzureBatchExecutor` / `GoogleBatchExecutor` =
10, `DockerSwarmExecutor` = 20, `NomadExecutor` /
`KubernetesExecutor` = 5, `DaskJobQueueExecutor` = 50); the CLI
`--submit-rps <float>` flag overrides the executor's default.
All ten executors each have their own file:
`local_executor.py` (`LocalExecutor` + `run_subprocess`),
`slurm_executor.py` (`SlurmExecutor`),
`aws_batch_executor.py` (`AWSBatchExecutor`),
`nomad_executor.py` (`NomadExecutor`),
`azure_batch_executor.py` (`AzureBatchExecutor`),
`dask_jobqueue_executor.py` (`DaskJobQueueExecutor`; Dask
JobQueue with Slurm/PBS/K8s schedulers — distinct from the
submitit-based `SlurmExecutor`), `docker_swarm_executor.py`
(`DockerSwarmExecutor`), `google_batch_executor.py`
(`GoogleBatchExecutor`), `kubernetes_executor.py`
(`KubernetesExecutor` — each Job runs
`python -m osimflow.remote_runner`;
`OSIMFLOW_TASK_PAYLOAD` carries the step call,
`OSIMFLOW_RESULT_*` carries the transport contract;
`security_context_strict=True` constructor flag, issue #1383,
emits `runAsNonRoot`, `readOnlyRootFilesystem`,
`allowPrivilegeEscalation: false`, `capabilities.drop: ["ALL"]`
plus `automountServiceAccountToken: false`),
`pbs_executor.py` (`PBSExecutor`, submitit).

### `osimflow/api/` (optional, `[api]` extra)

`__init__.py` (`create_app`), `app.py` (FastAPI factory),
`events.py` (SSE + stop endpoints), `auth.py`, `campaigns.py`,
`coordinator.py`, `dashboard.py`, `files.py`, `measures.py`,
`pat_compat.py`, `results_query.py`, `results_viewer.py`,
`schemas.py`, `timeseries.py`, `variable_designer.py`,
`variables.py`, plus `static/` and `templates/` UI assets.

### `osimflow/viz/`

`dashboard.py` — Streamlit dashboard (`[viz]` extra).

### `osimflow/importers/`, `osimflow/exporters/`

`osimflow/importers/osa.py` (`parse_osa`, `parse_analysis_json`,
`osa_to_variables_yml`, `stage_osa_template_package`);
`osimflow/exporters/osa.py` (`OSAExporter`,
`pack_osa` — PAT `.osa` archive).

### `osimflow/testing/`

Reusable test harness for third-party executor plug-in authors
(issue #1478). `ExecutorConformanceSuite` is a pytest-friendly
mixin class; `run_executor_conformance` is the non-pytest runner
that returns a `ConformanceReport`. Covers the `submit()` → `Handle`
lifecycle, result-reference handling via `transport.py`,
resource-directive propagation, and health-check registration;
the opt-in `three_sample_stub_campaign` check runs the full
Campaign via a stub-mode 3-sample mini-campaign. Importable as
`from osimflow.testing import ExecutorConformanceSuite`.

Reusable test harness for third-party algorithm plug-in authors
(issue #1565). `AlgorithmConformanceSuite` mirrors the executor
suite shape (pytest mixin + non-pytest runner + factory attribute)
and verifies the `BaseAlgorithm` sampling contract: `n_samples`
exactness, seed determinism, variable-name/order stability, dtype
coercion via `coerce_variable_type`, and cache-key reproducibility.
The default `variables_spec` covers uniform + normal + discrete
int so the suite exercises `coerce_variable_type` end-to-end;
plug-ins whose algorithms cannot represent one of those (e.g.
Sobol via SALib) override `variables_spec` / `sample_counts` on
the subclass. Importable as
`from osimflow.testing import AlgorithmConformanceSuite`.
`patch_targets.py` (issue #1574) is the explicit testing surface
that replaces ``osimflow.executors.time`` / ``osimflow.executors.random``
and the old private helper re-exports; tests patch sleep / jitter /
helper calls through ``osimflow.testing.patch_targets.time``,
``osimflow.testing.patch_targets.random``, and the private helpers
re-exported from that module (``_AWSBatchHandle``, ``_TokenBucketRateLimiter``,
``_SpotPriceCache``, ``_retry_nomad_request``, ``_NOMAD_RETRY_*``,
``_slugify_job_name``, ``_apply_slurm_params``, ...).

### Top-level

- `bin/` — backward-compatible shims over `_work_scripts/`:
  `generate_lhs.py`, `apply_params_to_model.py`,
  `extract_kpis.py`, `aggregate_results.py`, `generate_plots.py`,
  `excel_to_variables.py`. ~25 lines each, re-exporting from
  `_work_scripts/`. Do not add logic here.
- `scripts/` — CLI utilities:
  `e2e_gate.py` (real-E2E workflow preflight + JUnit
  executed-test verification, issue #1813),
  `resolve_openstudio_deb.py` (picks the OpenStudio Ubuntu `.deb`
  release asset for `openstudio-cli-e2e.yml`, issue #1813),
  `fetch_example_fixture.py` (downloads a real `.osm`+`.epw` into the
  gitignored `tests/fixtures/real/`; never touches `example_package/`,
  issue #1832), `generate_openapi.py` (regenerates
  `docs/openapi.json`), `bundle_offline.py` (for
  `--offline-bundle`), `migrate_from_mongodb.py`,
   `apply_branch_protection.sh` (post-merge
  settings-as-code for `main`), `setup_nomad_vm.sh`,
   `sweep-stale-branches.sh` (dry-run stale-branch sweep;
   `--apply` deletes proven-merged; `--include-orphaned` opts into
   abandoned branches; `--worktree` scans stale local worktrees;
   see issue #1003).
- `tools/` — repo-internal check scripts:
  `check_forbidden_files.py` (pre-commit hook rejecting tracked
  `eplusout.*`/`*.osm`/`*.osw`/`*.sqlite`/`*.job`/`data_point.zip` outside
  an allowlist, issue #1894),
    `check_agents_contract.py` (§0.3), `check_docs_sync.py`
  (docs path resolution), `_generate_byos_runner.py`
  (regenerates `osimflow/_byos_runner_generated.py` from
  `osimflow/byos_contract.py`; issue #1061).
- `user_scripts/` — user-supplied "Bring Your Own Script"
  overrides (see `user_scripts/README.md`). Loaded via
  `importlib.util` and signature-validated with
  `inspect.signature`.
- `example_package/` — tiny model + variables for local
  smoke tests and executor integration tests.
- `osimflow-deploy/` — cloud-deployment recipes sub-monorepo
  (independent `osimflow-deploy-v` tag prefix, CODEOWNERS
  for IaC). Links to actual IaC in `infra/`; does not
  duplicate it (the sole Helm chart is `infra/kubernetes/`; compose
  files live at the repo root; notebooks live in `examples/notebooks/`;
  issue #1887).
- `docker/osimflow-worker/` — AWS Batch worker runtime image (issue
  #1810): `Dockerfile` layers Python 3.12 + `osimflow[aws]` onto
  `nrel/openstudio`, no ENTRYPOINT, non-root, `/scratch`; `smoke_test.py`
  is the in-container smoke test. The job definition's image is
  authoritative (`AWSBatchExecutor._validate_job_definition_image`);
  `OSIMFLOW_CONTAINER` does not change the launched image. See
  `docs/aws-batch-worker-runtime.md`.
- `infra/aws/terraform/` — Terraform module for AWS Batch
  (VPC, S3, IAM, compute env, job queue, job definition
  using `nrel/openstudio`). IAM in `iam.tf`, job
  definition in `job-definition.tf`, ECR + lifecycle in
  `ecr.tf`. CI runs `terraform validate` on `infra/` path
  changes.
- `infra/aws/scripts/sync-openstudio-to-ecr.sh` — ECR mirror
  script (exponential-backoff retry, multi-region).
- `infra/aws/scripts/bootstrap-terraform-backend.sh` — idempotent
  creation of the Terraform remote-state S3 bucket + DynamoDB lock table.
- `infra/nomad/examples/ha/` — native host-OS Nomad HA
  cluster recipe (3-server Raft, ACL bootstrap, mTLS).
  `infra/nomad/acl/policies/` for agent/worker policies.
  Tokens in `acl/tokens/` are git-ignored.
- `docs/` — `OSimFlow.md` (PRD), `DEVELOPMENT.md` (the
  day-to-day guide — read for depth), `CONTRIBUTING.md`,
  `GOVERNANCE.md`, `api.md`, `branch-protection.md`,
  `benchmarks.md`, `user-guide.md`,
  `substrate-coverage.md` (real-E2E matrix — issue #1020),
  plus per-feature guides (`aws-batch-terraform.md`,
  `nomad-production.md`, `kubernetes-deployment.md`,
  `container-image-strategy.md`, `observability.md`,
  `distributed-cache.md`, `openstudio-image-distribution.md`,
  `measure-runner-guide.md`, `packaging-measures.md`, etc.).
- `.agents/results/` — ADRs and the framework-decision
  verdict.
- `tests/contract/` — contract tests run by pre-commit and
  `make test-fast` (fast subset since issue #1624: the recursive
  `test_coverage_gate` is `full`-marked and only runs when explicitly
  selected with `-m full`).
- `tests/unit/`, `tests/integration/`, `tests/benchmarks/`
  — pytest trees. The real-substrate companion tests in
  `tests/integration/test_real_<substrate>_campaign.py`
  (`test_aws_batch_real.py`, `test_azure_batch_real.py`,
  `test_google_batch_real.py`, `test_kubernetes_executor_real.py`,
  `test_slurm_real_cluster.py`,
  `test_real_openstudio_campaign.py`,
  `test_real_nomad_ha_campaign.py`,
  `test_real_pbs_campaign.py`,
  `test_real_dask_campaign.py`,
  `test_real_docker_swarm_campaign.py`)
  follow the 3-sample mini-campaign pattern and are skip-gated by
  env var (see `docs/substrate-coverage.md` for the full matrix
  and `docs/DEVELOPMENT.md` §4 for the per-test skip-gate knobs).
  Issue #1020.

---
