"""Backward-compatible alias for :mod:`osimflow.observability.mlflow_hook` (issue #1891).

The old module name is bound to the very same module object, so
``patch("osimflow.mlflow_hook.X")`` and private-name imports keep working.
"""

import sys

from osimflow.observability import mlflow_hook as _impl

sys.modules[__name__] = _impl
