"""Backward-compatible alias for :mod:`osimflow.campaign.sample_trace` (issue #1891).

The old module name is bound to the very same module object, so
``patch("osimflow._campaign_sample_trace.X")`` and private-name imports keep working.
"""

import sys

from osimflow.campaign import sample_trace as _impl

sys.modules[__name__] = _impl
