"""Custom exception classes for the OSimFlow client SDK."""


class OSimFlowClientError(Exception):
    """Base exception for all OSimFlow client errors."""

    pass


class CampaignNotFoundError(OSimFlowClientError):
    """Raised when a campaign cannot be found on the server."""

    pass


class CampaignCreationError(OSimFlowClientError):
    """Raised when campaign creation fails."""

    pass


class CampaignDeletionError(OSimFlowClientError):
    """Raised when campaign deletion fails."""

    pass


class ResultsNotFoundError(OSimFlowClientError):
    """Raised when results cannot be found."""

    pass


class APIError(OSimFlowClientError):
    """Raised when the API returns an unexpected HTTP status code."""

    def __init__(self, status_code: int, message: str) -> None:
        self.status_code = status_code
        self.message = message
        super().__init__(f"API error {status_code}: {message}")
