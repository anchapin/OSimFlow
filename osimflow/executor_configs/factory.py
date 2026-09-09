"""Shared executor construction factory (issue #1681).

Single source of truth for the ``executor name → BaseExecutor kwargs`` mapping.

Both ``osimflow.__main__._build_executor`` (CLI) and
``osimflow.api.campaigns._build_executor_from_request`` (REST) call into
this module so the two surfaces can never silently drift — the
``mirror has already drifted`` failure mode that the issue documents.
The mapping is registered next to the per-executor ``add_arguments``
hooks in :mod:`osimflow.executor_configs.__init__`, so any new
executor plug-in only needs to register one ``kwargs_for_executor``
function to be wired into both surfaces.

The factory deliberately accepts a flat ``**kwargs`` payload rather
than a typed ``CampaignConfig`` (issue body suggestion) so it stays
usable from ``argparse.Namespace`` (CLI), Pydantic v2
``CampaignCreateRequest.model_dump()`` (API), and any future transport
that satisfies the attribute-based contract. Each per-executor
``kwargs_for_executor`` function reads the keys it needs and returns
the dict to pass to the executor constructor.

Usage::

    from osimflow.executor_configs import build_executor
    executor = build_executor("local", max_workers=8)

    # Plugin executors that registered via ExecutorRegistry.register
    # but did not register a ``kwargs_for_executor`` hook fall back
    # to forwarding **kwargs directly to the constructor.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from osimflow.executors import BaseExecutor, ExecutorRegistry

log = logging.getLogger("osimflow.executor_configs.factory")

#: Signature of a per-executor ``kwargs_for_executor`` hook. Receives
#: the flat executor-name-prefixed kwargs (e.g. ``slurm_partition``,
#: ``aws_batch_queue``) from whichever caller invoked the factory and
#: returns the dict to pass to the executor constructor.
ExecutorKwargsBuilder = Callable[..., dict[str, Any]]

# Anchored in this module (issue #1463 pattern): a module-level dict
# literal in the package ``__init__`` would be re-created whenever the
# package is re-executed (``importlib.reload``), silently dropping
# every registration. Importing this module (cached by the import
# system) keeps registry state stable across reloads.
_EXECUTOR_KWARGS_BUILDERS: dict[str, ExecutorKwargsBuilder] = {}


def register_executor_kwargs_builder(name: str, builder: ExecutorKwargsBuilder) -> None:
    """Register *builder* under executor *name* (issue #1681).

    Re-registering the same name overwrites the previous builder — the
    same semantics as :func:`osimflow.executors.ExecutorRegistry.register`
    and :func:`osimflow.executor_configs.base.register_executor_arguments`
    — so idempotent re-import never accumulates duplicates.
    """
    _EXECUTOR_KWARGS_BUILDERS[name] = builder
    log.debug("registered executor kwargs builder %s", name)


def iter_executor_kwargs_builders() -> list[tuple[str, ExecutorKwargsBuilder]]:
    """Return ``[(name, builder), ...]`` sorted by executor name.

    Sorted order keeps contract assertions and registry iteration
    deterministic regardless of registration order.
    """
    return sorted(_EXECUTOR_KWARGS_BUILDERS.items())


def supported_executor_names() -> frozenset[str]:
    """Return the executor names this factory has a ``kwargs_for_executor`` hook for.

    Subset of :func:`osimflow.executors.ExecutorRegistry.list_available`
    — third-party plug-in executors may register a class without a
    builder, in which case the factory falls back to forwarding
    ``**kwargs`` directly to the constructor.
    """
    return frozenset(_EXECUTOR_KWARGS_BUILDERS)


def build_executor(name: str, **kwargs: Any) -> BaseExecutor:
    """Construct a :class:`BaseExecutor` for *name* using the shared registry.

    The function is the single place that maps an executor name to
    constructor kwargs (issue #1681 acceptance criteria). Built-in
    executors registered with a ``kwargs_for_executor`` hook translate
    the flat ``**kwargs`` payload (the same names argparse produces
    and the API request schema declares) into the specific constructor
    signature. Plug-in executors that did not register a hook fall
    back to forwarding ``**kwargs`` directly to the constructor
    (mirroring the pre-#1681 CLI behaviour for plugin classes).

    Parameters
    ----------
    name
        The executor name. Must be a key in
        :class:`osimflow.executors.ExecutorRegistry`.
    **kwargs
        Flat executor-name-prefixed keyword arguments. The CLI passes
        ``vars(args)``; the API passes ``body.model_dump()``. The
        factory is indifferent to the source — only the per-executor
        ``kwargs_for_executor`` hook decides which keys to consume.

    Returns
    -------
    BaseExecutor
        The constructed executor instance.

    Raises
    ------
    ValueError
        If *name* is not a registered executor. The message lists
        every available name so the caller can fix the typo.
    """
    try:
        executor_cls = ExecutorRegistry.get(name)
    except ValueError:
        # Re-raise with the same registry-provided message; the CLI
        # and API previously used a different error string, but the
        # registry's message is more informative and the acceptance
        # criteria explicitly call for a single source of truth.
        raise

    builder = _EXECUTOR_KWARGS_BUILDERS.get(name)
    if builder is None:
        # Plugin path: no per-executor hook registered. Forward
        # kwargs directly (the plugin class accepts them in
        # ``__init__(**kwargs)`` by convention — issue #1275).
        log.debug(
            "executor %r has no kwargs_for_executor hook; "
            "forwarding **kwargs directly to the constructor",
            name,
        )
        return executor_cls(**kwargs)

    executor_kwargs = builder(name=name, **kwargs)
    return executor_cls(**executor_kwargs)
