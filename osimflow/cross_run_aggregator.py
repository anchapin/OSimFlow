"""Backward-compatible alias for :mod:`osimflow.registry.cross_run_aggregator` (issue #1891).

The old module name is bound to the very same module object, so
``patch("osimflow.cross_run_aggregator.X")`` and private-name imports keep working.
"""

import sys

from osimflow.registry import cross_run_aggregator as _impl

sys.modules[__name__] = _impl
