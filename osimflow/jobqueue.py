"""Backward-compatible alias for :mod:`osimflow.queues.journal` (issue #1891).

The old module name is bound to the very same module object, so
``patch("osimflow.jobqueue.X")`` and private-name imports keep working.
"""

import sys

from osimflow.queues import journal as _impl

sys.modules[__name__] = _impl
