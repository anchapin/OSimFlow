"""Backward-compatible alias for :mod:`osimflow.campaign.epw` (issue #1891).

The old module name is bound to the very same module object, so
``patch("osimflow._campaign_epw.X")`` and private-name imports keep working.
"""

import sys

from osimflow.campaign import epw as _impl

sys.modules[__name__] = _impl
