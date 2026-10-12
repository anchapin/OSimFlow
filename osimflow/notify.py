"""Backward-compatible alias for :mod:`osimflow.notifications.notify` (issue #1891).

The old module name is bound to the very same module object, so
``patch("osimflow.notify.X")`` and private-name imports keep working.
"""

import sys

from osimflow.notifications import notify as _impl

sys.modules[__name__] = _impl
