"""Backward-compatible alias for :mod:`osimflow.queues.control_plane` (issue #1891).

The old module name is bound to the very same module object, so
``patch("osimflow.distributed_jobqueue.X")`` and private-name imports keep working.
"""

import sys

from osimflow.queues import control_plane as _impl

sys.modules[__name__] = _impl
