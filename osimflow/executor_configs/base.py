"""Shared registry machinery for per-executor CLI argument hooks.

Issue #1575: every built-in executor owns its configuration surface —
the ``XConfig`` dataclass(es) it needs plus an
``add_arguments(parser_group)`` hook that registers its ``--flags`` on
the ``run`` subparser. This module holds the registry those hooks are
registered into, mirroring the anchoring pattern from
``osimflow/executors/base.py`` (issue #1463): the registry dict lives in
its own module so it survives ``importlib.reload`` of the package
``__init__``.

The registry is deliberately decoupled from
``osimflow.executors.ExecutorRegistry`` (which exposes
``register_arguments`` / ``iter_argument_hooks`` as thin delegates) so
that ``osimflow.config`` — the composer of every ``XConfig`` — can import
this leaf package without pulling the executor implementations (and
their SDK imports) into its import graph.

Third-party executor plug-ins register a hook either directly — by
calling :func:`register_executor_arguments` with a module-level
``add_arguments(parser_group)`` function that registers the plug-in's
flags on the run parser (see any built-in module under
``osimflow/executor_configs/`` for a concrete example) — or by defining
an ``add_arguments`` staticmethod on the executor class registered via
the ``osimflow.executors`` entry point: ``ExecutorRegistry.discover_plugins``
auto-registers it.
"""

import argparse
import logging
from collections.abc import Callable

log = logging.getLogger("osimflow.executor_configs")

#: Signature of a per-executor ``add_arguments`` hook: receives the
#: ``run`` subparser (an ``argparse.ArgumentParser``; an argument group
#: is equally acceptable) and registers that executor's ``--flags`` on
#: it with exactly the same names / defaults / help the executor would
#: have declared inline pre-#1575.
ExecutorArgumentHook = Callable[[argparse.ArgumentParser], None]

# Anchored in this module (issue #1463 pattern): a dict literal in the
# package ``__init__`` would be re-created whenever the package is
# re-executed (``importlib.reload``), silently dropping every
# registration. Importing this module (cached by the import system)
# keeps registry state stable across reloads.
_EXECUTOR_ARGUMENT_HOOKS: dict[str, ExecutorArgumentHook] = {}


def register_executor_arguments(name: str, hook: ExecutorArgumentHook) -> None:
    """Register *hook* under executor *name* (issue #1575).

    Re-registering the same name overwrites the previous hook — the same
    semantics as ``ExecutorRegistry.register`` — so idempotent re-import
    never accumulates duplicates.
    """
    _EXECUTOR_ARGUMENT_HOOKS[name] = hook
    log.debug("registered executor argument hook %s", name)


def iter_executor_argument_hooks() -> list[tuple[str, ExecutorArgumentHook]]:
    """Return ``[(name, hook), ...]`` sorted by executor name.

    Sorted order keeps parser construction (and therefore ``--help``
    output) deterministic regardless of registration order.
    """
    return sorted(_EXECUTOR_ARGUMENT_HOOKS.items())


def add_executor_arguments(parser: argparse.ArgumentParser) -> None:
    """Register every executor's ``--flags`` on *parser*.

    Called by ``osimflow.__main__._add_run_args`` for the ``run`` (and
    ``warm-cache``) subparser: iterates every registered hook — built-in
    modules plus any plug-in hooks registered via
    :func:`register_executor_arguments` — in sorted executor-name order.
    """
    for _name, hook in iter_executor_argument_hooks():
        hook(parser)
