"""Backward-compatible alias for :mod:`osimflow.storage.azure` (issue #1891).

The old module name is bound to the very same module object, so
``patch("osimflow._storage_azure.X")`` and private-name imports keep working.
"""

import sys

from osimflow.storage import azure as _impl

sys.modules[__name__] = _impl
