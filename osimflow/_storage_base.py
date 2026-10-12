"""Backward-compatible alias for :mod:`osimflow.storage.base` (issue #1891).

The old module name is bound to the very same module object, so
``patch("osimflow._storage_base.X")`` and private-name imports keep working.
"""

import sys

from osimflow.storage import base as _impl

sys.modules[__name__] = _impl
