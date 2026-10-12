"""Backward-compatible alias for :mod:`osimflow.campaign.fanout` (issue #1891).

The old module name is bound to the very same module object, so
``patch("osimflow._campaign_fanout.X")`` and private-name imports keep working.
"""

import sys

from osimflow.campaign import fanout as _impl

sys.modules[__name__] = _impl
