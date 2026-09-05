"""Executor abstraction for OSimFlow campaigns.

A `BaseExecutor` is a thin wrapper that takes a Python callable and runs it
on some compute substrate (local thread, Slurm job, AWS Batch task, etc.)
and returns a handle. The handle exposes:

  * `.result(timeout=None)` — block until done, return the callable's return
    value or re-raise.
  * `.job_id` — a substrate-specific identifier (Slurm job ID, Batch task ARN,
    thread name, etc.) for log correlation.
  * `.done()` — non-blocking check.

This mirrors `submitit.Future`-style ergonomics intentionally: it is the
mental model the team will use, and the SlurmExecutor returns real
`submitit.Future` objects directly.

Since issue #1463 this package init holds only the shared surface —
the :class:`ExecutorRegistry`, entry-point plug-in discovery, per-step
resource defaults, and the re-export of every executor implementation
from its own module (``local_executor``, ``slurm_executor``,
``aws_batch_executor``, ``nomad_executor``, ``azure_batch_executor``,
``google_batch_executor``, ``kubernetes_executor``, ``pbs_executor``,
``dask_jobqueue_executor``, ``docker_swarm_executor``) — so
``from osimflow.executors import <Name>`` keeps working unchanged.

Issue #1574 separated the testing patch surface from this package:
private helpers (``_AWSBatchHandle``, ...),
the ``time`` / ``random`` stdlib modules, and the transport helpers now
live on :mod:`osimflow.testing.patch_targets` — tests patch through
that module instead of this one. The bare ``import time`` / ``import
random`` ``# noqa: F401`` lines are gone; private helper re-exports
are gone too. A :pep:`562` ``__getattr__`` deprecation shim on this
package keeps third-party plug-ins that still import a private name
working (with a :class:`DeprecationWarning`) until they migrate.

Health-check registration (issue #1024, #1463) now lives entirely on the
``osimflow.health`` side: that module registers at import time and
re-binds at ``run_health_checks()`` entry. The executors package never
imports ``osimflow.health`` — the dependency is one-directional.
"""

import logging
import warnings
from collections.abc import Callable
from importlib import import_module
from importlib.metadata import entry_points
from typing import TYPE_CHECKING, Any, cast

from osimflow.executor_configs.base import ExecutorArgumentHook
from osimflow.executors.aws_batch_executor import AWSBatchExecutor
from osimflow.executors.azure_batch_executor import AzureBatchExecutor as AzureBatchExecutor
from osimflow.executors.base import (
    _EXECUTOR_HEALTH_CHECKS,
    _EXECUTOR_REGISTRY,
    BaseExecutor,
    Handle,
    SubmitRequest,
)
from osimflow.executors.dask_jobqueue_executor import DaskJobQueueExecutor as DaskJobQueueExecutor
from osimflow.executors.docker_swarm_executor import DockerSwarmExecutor as DockerSwarmExecutor
from osimflow.executors.google_batch_executor import GoogleBatchExecutor as GoogleBatchExecutor
from osimflow.executors.kubernetes_executor import KubernetesExecutor as KubernetesExecutor
from osimflow.executors.local_executor import LocalExecutor, run_subprocess
from osimflow.executors.nomad_executor import NomadExecutor
from osimflow.executors.pbs_executor import PBSExecutor as PBSExecutor
from osimflow.executors.slurm_executor import SlurmExecutor
from osimflow.executors.transport import (
    coerce_transport_mode as coerce_transport_mode,
    materialize_object_storage_result as materialize_object_storage_result,
    resolve_result_for_callback as resolve_result_for_callback,
    validate_transport_mode as validate_transport_mode,
)

if TYPE_CHECKING:
    from osimflow.health import CheckResult

log = logging.getLogger("osimflow.executors")

#: Entry-point group for third-party executor plug-ins (issue #432).
EXECUTOR_ENTRY_POINT_GROUP = "osimflow.executors"

# ---------------------------------------------------------------------------
# Deprecation shim for previously-re-exported private helpers (issue #1574)
# ---------------------------------------------------------------------------
# Before issue #1574 this package re-exported every ``_``-prefixed helper
# from the per-executor modules so that tests could patch through this
# namespace and so that AGENTS.md's contract-checked mention of those
# names became load-bearing public API. The new testing surface
# (:mod:`osimflow.testing.patch_targets`) is the supported way to reach
# those helpers from tests.
#
# To avoid silently breaking any third-party plug-in that took a
# dependency on the old re-exports, :pep:`562` ``__getattr__`` looks
# each private name up in its defining module and emits a
# :class:`DeprecationWarning` so plug-in authors see the migration path
# the first time the name is touched. The warning is module-scoped so a
# single import only fires once per process (issue #1115 pattern).
_DEPRECATED_PRIVATE_NAMES: dict[str, str] = {
    # AWS Batch (osimflow.executors.aws_batch_executor)
    "_AWSBatchHandle": "osimflow.executors.aws_batch_executor",
    "_SpotPriceCache": "osimflow.executors.aws_batch_executor",
    "_TokenBucketRateLimiter": (
        "osimflow.executors._rate_limiter.TokenBucketRateLimiter "
        "(issue #1563 — moved out of aws_batch_executor)"
    ),
    "_aws_error_code": "osimflow.executors.aws_batch_executor",
    # Nomad (osimflow.executors.nomad_executor)
    "_NOMAD_RETRY_CAP_S": "osimflow.executors.nomad_executor",
    "_NOMAD_RETRY_INITIAL_DELAY_S": "osimflow.executors.nomad_executor",
    "_NOMAD_RETRY_MAX_ATTEMPTS": "osimflow.executors.nomad_executor",
    "_NOMAD_RETRYABLE_HTTP_CODES": "osimflow.executors.nomad_executor",
    "_NomadClient": "osimflow.executors.nomad_executor",
    "_NomadHandle": "osimflow.executors.nomad_executor",
    "_retry_nomad_request": "osimflow.executors.nomad_executor",
    "_slugify_job_name": "osimflow.executors.nomad_executor",
    "_nomad_error_code": "osimflow.executors.nomad_executor",
    # Slurm (osimflow.executors.slurm_executor)
    "_apply_slurm_params": "osimflow.executors.slurm_executor",
}
_DEPRECATION_EMITTED: set[str] = set()


def __getattr__(name: str) -> Any:
    """PEP 562 lazy lookup with deprecation warnings (issue #1574).

    Resolves previously-re-exported private helpers on demand. Each
    name is looked up in its defining module the first time it is
    accessed; a :class:`DeprecationWarning` is emitted exactly once per
    process so the caller learns about the migration path without being
    drowned in repeat warnings.
    """
    module_name = _DEPRECATED_PRIVATE_NAMES.get(name)
    if module_name is not None:
        if name not in _DEPRECATION_EMITTED:
            _DEPRECATION_EMITTED.add(name)
            warnings.warn(
                (
                    f"Importing {name!r} from osimflow.executors is deprecated "
                    f"(issue #1574) and will be removed in a future release. "
                    f"Import it from {module_name} directly, or use "
                    f"osimflow.testing.patch_targets for test patching."
                ),
                DeprecationWarning,
                stacklevel=2,
            )
        return getattr(import_module(module_name), name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "AWSBatchExecutor",
    "AzureBatchExecutor",
    "BaseExecutor",
    "DaskJobQueueExecutor",
    "DockerSwarmExecutor",
    "ExecutorRegistry",
    "GoogleBatchExecutor",
    "Handle",
    "KubernetesExecutor",
    "LocalExecutor",
    "NomadExecutor",
    "PBSExecutor",
    "SlurmExecutor",
    "SubmitRequest",
]


# ---------------------------------------------------------------------------
# Per-step resource defaults (issue #39)
# ---------------------------------------------------------------------------
# Sensible defaults for each DAG step. Used by BaseExecutor.submit() when
# no explicit overrides are passed. See docs/resource-allocation.md for
# the rationale and tuning guidance.
#
# Keys match the step names used in osimflow/campaign.py.
# Values are dicts with {cpus, memory_mb, time_min}.
DEFAULT_STEP_RESOURCES: dict[str, dict[str, int]] = {
    "GENERATE_LHS_SAMPLES": {"cpus": 1, "memory_mb": 2048, "time_min": 5},
    "APPLY_PARAMETERS": {"cpus": 1, "memory_mb": 512, "time_min": 10},
    "RUN_OPENSTUDIO_SIM": {"cpus": 4, "memory_mb": 8192, "time_min": 240},
    "EXTRACT_KPIS": {"cpus": 1, "memory_mb": 2048, "time_min": 10},
    "AGGREGATE_RESULTS": {"cpus": 2, "memory_mb": 4096, "time_min": 15},
    "GENERATE_BASIC_PLOTS": {"cpus": 1, "memory_mb": 2048, "time_min": 10},
}


def get_step_resources(step_name: str) -> dict[str, int]:
    """Return resource defaults for a DAG step.

    Falls back to ``{"cpus": 1, "memory_mb": 1024, "time_min": 60}``
    when *step_name* is not in :data:`DEFAULT_STEP_RESOURCES`.
    """
    return DEFAULT_STEP_RESOURCES.get(
        step_name,
        {"cpus": 1, "memory_mb": 1024, "time_min": 60},
    )  # ======================================================================


# Executor registry + entry-point plug-in discovery (issue #432)
# ======================================================================


class ExecutorRegistry:
    """Global registry that maps executor names to their classes.

    Mirrors the ``AlgorithmRegistry`` pattern: built-in executors are
    registered explicitly at import time, and third-party executors can be
    auto-discovered via ``entry_points`` by calling :meth:`discover_plugins`.

    The registry enables introspection (``list_available()``) and a uniform
    lookup path (``get(name)``) so that the CLI and Campaign can validate
    executor names without hard-coding a choices list.

    Health checks (issue #1024) are stored alongside each registered
    executor via :meth:`register_health_check`. The health module iterates
    the registry to dispatch one check per executor instead of hard-coding
    an executor list. The check functions themselves live in
    ``osimflow/health.py`` (kept there to avoid pulling the health module
    into every executor's import path). Since issue #1463 the registration
    call also lives on the ``osimflow.health`` side (at that module's
    import and at ``run_health_checks()`` entry), making the coupling
    strictly one-directional: health imports executors, never the
    reverse.

    Typical usage::

        cls = ExecutorRegistry.get("local")
        executor = cls(max_workers=4)

    For third-party executor packages, add this to ``pyproject.toml``::

        [project.entry-points."osimflow.executors"]
        my_exec = "my_package.executors:MyExecutor"

    Plugin executors that need CLI configuration should accept ``**kwargs``
    in their ``__init__`` and receive forwarded CLI arguments (issue #1275).
    Since issue #1575 they can also declare their own ``--flags``: define
    an ``add_arguments(parser_group)`` staticmethod on the class (it is
    auto-registered by :meth:`discover_plugins`) or call
    ``ExecutorRegistry.register_arguments`` /
    ``osimflow.executor_configs.register_executor_arguments`` directly.
    """

    # Anchored in ``base.py`` (issue #1463): class-level dict literals
    # are recreated whenever this module is re-executed
    # (``importlib.reload(osimflow.executors)``), which would silently
    # drop every registration — including the health checks bound from
    # ``osimflow.health``. Aliasing the module-level dicts in ``base.py``
    # (imported once, cached by the import system) keeps registry state
    # stable across reloads.
    _registry = _EXECUTOR_REGISTRY
    _health_checks = _EXECUTOR_HEALTH_CHECKS

    @classmethod
    def register(cls, name: str, executor_cls: type[BaseExecutor]) -> None:
        """Register *executor_cls* under *name*."""
        cls._registry[name] = executor_cls
        log.debug("registered executor %s -> %s", name, executor_cls.__qualname__)

    @classmethod
    def get(cls, name: str) -> type[BaseExecutor]:
        """Return the executor class registered under *name*.

        Raises
        ------
        ValueError
            If *name* is not registered, with a helpful message listing
            available executors.
        """
        if name not in cls._registry:
            available = ", ".join(sorted(cls._registry)) or "(none)"
            raise ValueError(f"unknown executor '{name}'. Available executors: {available}")
        return cls._registry[name]

    @classmethod
    def list_available(cls) -> list[str]:
        """Return the sorted list of registered executor names."""
        return sorted(cls._registry)

    @classmethod
    def register_health_check(cls, name: str, check_fn: "Callable[[], CheckResult]") -> None:
        """Register a health check for executor *name* (issue #1024).

        ``check_fn`` must be a zero-argument callable returning a
        ``CheckResult``. It runs in :meth:`osimflow.health.run_health_checks`
        for every registered executor.

        Raises
        ------
        ValueError
            If *name* is not a registered executor.
        """
        if name not in cls._registry:
            available = ", ".join(sorted(cls._registry)) or "(none)"
            raise ValueError(
                f"cannot register health check for '{name}': executor not registered. "
                f"Available executors: {available}"
            )
        cls._health_checks[name] = check_fn
        log.debug("registered health check for executor %s", name)

    @classmethod
    def get_health_check(cls, name: str) -> "Callable[[], CheckResult] | None":
        """Return the health check callable registered for *name*, or None."""
        return cls._health_checks.get(name)

    @classmethod
    def iter_health_checks(cls) -> "list[tuple[str, Callable[[], CheckResult]]]":
        """Return ``[(name, check_fn), ...]`` for every executor that has a check.

        Sorted by executor name for deterministic output. Executors
        registered without a health check are skipped — this lets us add
        a new executor before its check is in place without breaking the
        health subcommand (the regression test asserts coverage of all
        built-ins).
        """
        pairs: list[tuple[str, Callable[[], CheckResult]]] = []
        for name in sorted(cls._registry):
            check = cls._health_checks.get(name)
            if check is not None:
                pairs.append((name, check))
        return pairs

    @classmethod
    def clear_health_checks(cls) -> None:
        """Test helper: drop every registered health check.

        Production code never calls this. Tests use it to start from a
        clean slate when checking the registration loop in isolation.
        """
        cls._health_checks.clear()

    @classmethod
    def register_arguments(cls, name: str, hook: ExecutorArgumentHook) -> None:
        """Register a per-executor CLI argument hook (issue #1575).

        *hook* is an ``add_arguments(parser_group)`` callable that
        registers the executor's ``--flags`` on the ``run`` /
        ``warm-cache`` subparser.
        ``osimflow.__main__._add_run_args`` invokes every registered
        hook (via ``osimflow.executor_configs.add_executor_arguments``)
        instead of hand-coding the executor argparse tree, so a
        third-party executor's configuration finally has the same home
        as a built-in's. Mirrors :meth:`register` semantics —
        re-registering the same name overwrites the previous hook.

        The hook registry itself is anchored in
        ``osimflow/executor_configs/base.py`` (issue #1463 pattern) so it
        survives ``importlib.reload`` of either package ``__init__``.
        """
        from osimflow.executor_configs.base import (  # noqa: PLC0415
            register_executor_arguments,
        )

        register_executor_arguments(name, hook)

    @classmethod
    def iter_argument_hooks(cls) -> list[tuple[str, ExecutorArgumentHook]]:
        """Return ``[(name, hook), ...]`` sorted by executor name (issue #1575).

        Delegates to ``osimflow.executor_configs.iter_executor_argument_hooks``.
        """
        from osimflow.executor_configs.base import (  # noqa: PLC0415
            iter_executor_argument_hooks,
        )

        return iter_executor_argument_hooks()

    @classmethod
    def discover_plugins(cls) -> int:
        """Discover and auto-register executors from installed entry points.

        Scans the ``osimflow.executors`` entry point group and loads each
        entry point.  Loaded objects that are ``BaseExecutor`` subclasses are
        registered under the entry-point ``name``.

        The method is **safe** — if no plug-ins are found it silently returns
        ``0``.  Import or type errors for individual plug-ins are logged at
        ``WARNING`` level and skipped so a single broken plug-in never breaks
        the registry.

        Returns
        -------
        int
            The number of plug-ins successfully registered.
        """
        try:
            eps = list(entry_points(group=EXECUTOR_ENTRY_POINT_GROUP))
        except Exception:  # noqa: BLE001 — never crash on metadata issues
            return 0

        if not eps:
            return 0

        count = 0
        for ep in eps:
            try:
                obj = ep.load()
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "failed to load executor plug-in '%s' (%s): %s",
                    ep.name,
                    ep.value,
                    exc,
                )
                continue

            if not (isinstance(obj, type) and issubclass(obj, BaseExecutor)):
                log.warning(
                    "executor plug-in '%s' (%s) is not a BaseExecutor subclass — skipping",
                    ep.name,
                    ep.value,
                )
                continue

            cls.register(ep.name, obj)

            # Auto-register the plug-in's CLI argument hook when it
            # follows the ``add_arguments(parser_group)`` staticmethod
            # convention (issue #1575) — the same hook contract the
            # built-in executors use in ``osimflow/executor_configs/``.
            hook = getattr(obj, "add_arguments", None)
            if callable(hook):
                cls.register_arguments(ep.name, cast("ExecutorArgumentHook", hook))

            log.info("discovered executor plug-in '%s' -> %s", ep.name, ep.value)
            count += 1

        return count


# ======================================================================
# Register built-in executors
# ======================================================================
ExecutorRegistry.register("local", LocalExecutor)
ExecutorRegistry.register("slurm", SlurmExecutor)
ExecutorRegistry.register("aws_batch", AWSBatchExecutor)
ExecutorRegistry.register("nomad", NomadExecutor)
ExecutorRegistry.register("azure_batch", AzureBatchExecutor)
ExecutorRegistry.register("google_batch", GoogleBatchExecutor)
ExecutorRegistry.register("kubernetes", KubernetesExecutor)
ExecutorRegistry.register("pbs", PBSExecutor)
ExecutorRegistry.register("dask_jobqueue", DaskJobQueueExecutor)
ExecutorRegistry.register("docker_swarm", DockerSwarmExecutor)

# Discover third-party executor plug-ins (no-op when none installed).
ExecutorRegistry.discover_plugins()  # NOTE (issue #1463): the deferred import back-edge to osimflow.health that
# used to live here is gone. Health checks are registered from the
# osimflow.health side (at that module's import and at run_health_checks()
# entry) against the base.py-anchored registry dicts, which survive reloads
# of this module. osimflow.executors must never import osimflow.health.
