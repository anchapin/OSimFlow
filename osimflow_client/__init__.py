"""osimflow_client — minimal Python client SDK for OSimFlow REST API.

Provides :class:`CampaignClient` and :class:`ResultsClient` for programmatic
interaction with a running OSimFlow API server.
"""

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
