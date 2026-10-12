"""Backward-compatible alias for :mod:`osimflow.registry.results_query` (issue #1891).

The old module name is bound to the very same module object, so
``patch("osimflow.results_query.X")`` and private-name imports keep working.
"""

import sys

from osimflow.registry import results_query as _impl

sys.modules[__name__] = _impl
