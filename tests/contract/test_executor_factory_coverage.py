"""Contract test for the shared executor factory (issue #1681).

The pre-#1681 hand-rolled ``_build_executor_from_request`` in
``osimflow/api/campaigns.py`` silently drifted from the CLI's
``osimflow.__main__._build_executor`` — it omitted ``docker_swarm``
entirely, invented a second, divergent set of Slurm resource
defaults, and would silently drop every new executor or flag added
via the issue #1575 ``add_arguments`` hook regime. This contract test
asserts the new shared
:func:`osimflow.executor_configs.build_executor` factory stays in
lockstep with :class:`osimflow.executors.ExecutorRegistry` so the
two surfaces can never diverge again.

The contract lives in ``tests/contract/`` so ``make test-fast`` (the
pre-commit mirror) and the ``contract`` CI job enforce it on every PR.
"""

from __future__ import annotations

import importlib

import pytest

from osimflow.executor_configs import (
    build_executor,
    iter_executor_kwargs_builders,
    supported_executor_names,
)
from osimflow.executors import BaseExecutor, ExecutorRegistry

#: Substrate SDK module names whose presence is required to actually
#: *instantiate* each executor. The factory contract asserts the
#: builder / registry desync, but the actual ``__init__`` import paths
#: are substrate-specific — issue #1582 (azure-batch) and the docker
#: SDK live behind optional extras. Mapping mirrors the optional-
#: dependency blocks in ``pyproject.toml``.
_SUBSTRATE_SDKS: dict[str, tuple[str, ...]] = {
    "aws_batch": ("boto3", "botocore"),
    "azure_batch": ("azure.batch", "azure.identity"),
    "google_batch": ("google.cloud.batch_v1",),
    "kubernetes": ("kubernetes",),
    "docker_swarm": ("docker",),
    "slurm": ("submitit",),
}


def _substrate_sdk_available(name: str) -> bool:
    modules = _SUBSTRATE_SDKS.get(name, ())
    return all(_module_imports(m) for m in modules)


def _module_imports(module_name: str) -> bool:
    try:
        importlib.import_module(module_name)
    except Exception:  # noqa: BLE001 — optional substrate SDK may be absent
        return False
    return True


@pytest.mark.contract
def test_factory_supports_every_registered_executor() -> None:
    """Every name in :class:`ExecutorRegistry` must have a kwargs builder.

    A third-party plug-in executor registered via entry points without
    a ``kwargs_for_executor`` hook still works — the factory falls
    back to forwarding ``**kwargs`` directly to the constructor — but
    a built-in that ships without a builder is a regression the issue
    explicitly asks this contract test to catch.
    """
    registered = set(ExecutorRegistry.list_available())
    builders = set(name for name, _ in iter_executor_kwargs_builders())
    missing = registered - builders
    assert not missing, (
        "executor names registered in ExecutorRegistry but without a "
        "kwargs_for_executor hook in osimflow/executor_configs/: "
        f"{sorted(missing)}. Issue #1681 requires every built-in "
        "executor to flow through the shared factory so the CLI and "
        "REST surface cannot drift."
    )


@pytest.mark.contract
def test_factory_rejects_unknown_executor() -> None:
    """An unknown executor name must raise the same informative error
    regardless of which surface (CLI or API) calls the factory."""
    with pytest.raises(ValueError, match="unknown executor 'not_a_real_executor'"):
        build_executor("not_a_real_executor")


@pytest.mark.contract
def test_supported_executor_names_is_consistent() -> None:
    """``supported_executor_names()`` must mirror the kwargs-builder registry."""
    builders = {name for name, _ in iter_executor_kwargs_builders()}
    assert supported_executor_names() == frozenset(builders)


@pytest.mark.contract
def test_factory_returns_base_executor_subclass_for_known_names() -> None:
    """For every name with a builder, the factory must return a real
    :class:`BaseExecutor` instance — guards against the registry / builder
    desync that would let a builder point at a class that was never
    actually registered. Substrate SDKs that aren't installed in the
    current dev env (azure/google/k8s/docker/swarm live behind
    optional extras) are skipped — the structural assertion still
    covers every built-in.
    """
    for name, _ in iter_executor_kwargs_builders():
        if not _substrate_sdk_available(name):
            pytest.skip(
                f"{name!r} substrate SDK not installed in this env "
                f"(requires {sorted(_SUBSTRATE_SDKS.get(name, ()))}); "
                "structural coverage holds for every other built-in."
            )
        executor = build_executor(name, outdir="/tmp/contract")
        assert isinstance(executor, BaseExecutor), (
            f"build_executor({name!r}) returned {type(executor).__name__}, "
            "expected a BaseExecutor subclass"
        )
