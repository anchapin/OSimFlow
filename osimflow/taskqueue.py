"""Backward-compatible alias for :mod:`osimflow.queues.dispatch` (issue #1891).

The old module name is bound to the very same module object, so
``patch("osimflow.taskqueue.X")`` and private-name imports keep working.
"""

import sys

from osimflow.queues import dispatch as _impl

sys.modules[__name__] = _impl
