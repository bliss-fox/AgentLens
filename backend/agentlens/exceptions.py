class ExperimentCancelled(Exception):
    """Raised when durable cancellation reaches an execution boundary."""


class ToolAuthorizationError(Exception):
    """Stable public error for a run-scoped tool grant transition."""

    def __init__(self, category: str, message: str, *, status_code: int) -> None:
        super().__init__(message)
        self.category = category
        self.public_message = message
        self.status_code = status_code


class FailureReviewStateError(Exception):
    """Stable domain error for a durable Judge review claim transition."""

    def __init__(self, category: str, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.category = category
        self.public_message = message
        self.retryable = retryable
