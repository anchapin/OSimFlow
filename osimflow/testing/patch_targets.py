"""Testing surface for executor patch seams (issue #1574).

Why this module exists
----------------------
Before issue #1574, ``osimflow/executors/__init__.py`` deliberately
re-exported private helpers (``_AWSBatchHandle``, ``_TokenBucketRateLimiter``,
``_SpotPriceCache``, ``_retry_nomad_request``, ``_NOMAD_RETRY_*``, ...) and
even imported ``time`` and ``random`` with ``# noqa: F401`` so tests could
patch ``osimflow.executors.time.sleep``. The module docstring documented
this as a supported surface, and every mention in AGENTS.md made those
underscore-prefixed names load-bearing public API — renaming or
restructuring them broke tests and the AGENTS.md contract check.

This module moves the patch seam to an explicit, opt-in testing surface.
Production code never imports from here; only tests do.

Usage in tests::

    from unittest.mock import patch
    from osimflow.testing import patch_targets

    # Patches time.sleep in every executor module (they all reference the
    # same time module object via Python's import cache).
    with patch("osimflow.testing.patch_targets.time.sleep"):
        ex._wait_for_terminal("j-3")

    # Direct attribute access on private helpers (mirrors the old
    # ``from osimflow.executors import _AWSBatchHandle`` pattern).
    handle = patch_targets._AWSBatchHandle(job_id="j-1", executor=ex, submit_params={})

Why a separate module instead of patching via the source module
--------------------------------------------------------------
Patching ``osimflow.executors.aws_batch_executor.time.sleep`` works
because every executor module that does ``import time`` shares the same
``time`` module singleton, so setting ``time.sleep`` on any attribute path
affects every caller. We keep that mechanism and only change the *entry
point* tests use to reach the singleton.

``osimflow.executors.__init__`` no longer re-exports ``time`` or
``random`` (the bare ``import time`` / ``import random`` ``# noqa: F401``
lines are gone), so patching via that path would raise ``AttributeError``.
Tests now patch through this dedicated module instead.

Deprecation policy
------------------
If a third-party executor plug-in imports a private name from
``osimflow.executors`` (the old surface), it keeps working through a
``__getattr__`` deprecation shim on the package — but the shim emits
``DeprecationWarning`` so the plug-in author can migrate to this module.
Production code that needs to use the helpers should import them from
their defining module (``osimflow.executors.aws_batch_executor``, ...);
this module is for tests only.
"""

from __future__ import annotations

import random as _random
import time as _time

from osimflow.executors.aws_batch_executor import (
    _aws_error_code,
    _AWSBatchHandle,
    _SpotPriceCache,
    _TokenBucketRateLimiter,
)
from osimflow.executors.base import BaseExecutor, Handle, PollingHandle
from osimflow.executors.nomad_executor import (
    _NOMAD_RETRY_CAP_S,
    _NOMAD_RETRY_INITIAL_DELAY_S,
    _NOMAD_RETRY_MAX_ATTEMPTS,
    _NOMAD_RETRYABLE_HTTP_CODES,
    _NomadClient,
    _NomadHandle,
    _retry_nomad_request,
    _slugify_job_name,
)
from osimflow.executors.slurm_executor import _apply_slurm_params
from osimflow.executors.transport import (
    coerce_transport_mode,
    materialize_object_storage_result,
    resolve_result_for_callback,
    validate_transport_mode,
)

# Re-bind the stdlib modules under their well-known attribute names so
# tests can patch via ``osimflow.testing.patch_targets.time.sleep`` /
# ``osimflow.testing.patch_targets.random.uniform``. They are the same
# module objects every executor file imports, so attribute patches here
# propagate to every call site that uses ``time.sleep(...)`` or
# ``random.uniform(...)``.
time = _time
random = _random

__all__ = [
    # stdlib re-exports for sleep / jitter patching
    "time",
    "random",
    # AWS Batch private helpers (issue #1574)
    "_AWSBatchHandle",
    "_SpotPriceCache",
    "_TokenBucketRateLimiter",
    "_aws_error_code",
    # Nomad private helpers
    "_NOMAD_RETRY_CAP_S",
    "_NOMAD_RETRY_INITIAL_DELAY_S",
    "_NOMAD_RETRY_MAX_ATTEMPTS",
    "_NOMAD_RETRYABLE_HTTP_CODES",
    "_NomadClient",
    "_NomadHandle",
    "_retry_nomad_request",
    "_slugify_job_name",
    # Slurm private helper
    "_apply_slurm_params",
    # Transport helpers (re-exported so tests can patch the reference
    # instead of the underlying executor's lookup path)
    "coerce_transport_mode",
    "materialize_object_storage_result",
    "resolve_result_for_callback",
    "validate_transport_mode",
    # Public base types — re-exported so third-party plug-in tests can
    # import the canonical classes without taking a hard dependency on
    # the executor module layout.
    "BaseExecutor",
    "Handle",
    "PollingHandle",
]
