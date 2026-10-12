"""Backward-compatible alias for :mod:`osimflow.observability.monitoring` (issue #1891).

The old module name is bound to the very same module object, so
``patch("osimflow.monitoring.X")`` and private-name imports keep working.
"""

import sys

from osimflow.observability import monitoring as _impl

sys.modules[__name__] = _impl
