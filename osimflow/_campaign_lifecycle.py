"""Backward-compatible alias for :mod:`osimflow.campaign.lifecycle` (issue #1891).

The old module name is bound to the very same module object, so
``patch("osimflow._campaign_lifecycle.X")`` and private-name imports keep working.
"""

import sys

from osimflow.campaign import lifecycle as _impl

sys.modules[__name__] = _impl
