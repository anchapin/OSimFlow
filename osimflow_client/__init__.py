"""osimflow_client — minimal Python client SDK for OSimFlow REST API.

Provides :class:`CampaignClient` and :class:`ResultsClient` for programmatic
interaction with a running OSimFlow API server.

.. deprecated::
    ``osimflow.client.OSimFlowClient`` is the canonical REST client (issue
    #1887). This synchronous package is retained only for backwards
    compatibility and will be removed in a future release.
"""

import warnings

from osimflow_client.campaign import (
    CampaignClient,
    CampaignCreateResult,
    CampaignDetail,
    CampaignSummary,
)
from osimflow_client.exceptions import (
    CampaignCreationError,
    CampaignDeletionError,
    CampaignNotFoundError,
    OSimFlowClientError,
    ResultsNotFoundError,
)
from osimflow_client.results import FailureResult, ResultsClient, SampleResult

warnings.warn(
    "osimflow_client is deprecated; use osimflow.client.OSimFlowClient instead.",
    DeprecationWarning,
    stacklevel=2,
)

__all__ = [
    "CampaignClient",
    "ResultsClient",
    "CampaignCreateResult",
    "CampaignDetail",
    "CampaignSummary",
    "SampleResult",
    "FailureResult",
    "OSimFlowClientError",
    "CampaignNotFoundError",
    "CampaignCreationError",
    "CampaignDeletionError",
    "ResultsNotFoundError",
]

__version__ = "0.1.0"
