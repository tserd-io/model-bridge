from dataclasses import dataclass
from typing import Protocol

# Holds normalized provider output, model identity, attempts, and available usage estimates.
@dataclass(frozen=True)
class GenerationResult:
    content: str
    model: str
    attempts: int = 1
    input_tokens: int | None = None
    output_tokens: int | None = None
    estimated_cost_usd: float | None = None
    provider: str | None = None

# Defines the asynchronous completion interface expected of every provider implementation.
class Provider(Protocol):
    # Declares the asynchronous completion signature that provider adapters must implement.
    async def complete(
        self,
        request_id: str,
        attempt: int,
        message: str,
        model_preference: str,
        max_tokens: int,
        timeout: float,
    ) -> GenerationResult:
        ...

#Error handling
# Carries transient failure details, attempt counts, and uncertainty for retry and API handling.
class RetryableProviderError(Exception):
    """A transient provider failure that may succeed on a later attempt."""

    # Attaches attempt history, uncertainty, and provider context to a transient failure.
    def __init__(
		self,
		message: str,
		attempts: int = 1,
		outcome_unknown: bool = False,
        failure_type: str = "provider_error",
        status_code: int = 0,
        provider: str | None = None,
        model: str | None = None,
	) -> None:
        super().__init__(message)
        self.attempts = attempts
        self.outcome_unknown = outcome_unknown
        self.failure_type = failure_type
        self.status_code = status_code
        self.provider = provider
        self.model = model

# Signals that generation may have completed even though no usable response was received.
class ProviderOutcomeUnknown(Exception):
    """The provider may have completed the request but its response was lost."""

    # Stores the explanation and attempt count for an unresolved provider outcome.
    def __init__(self, message: str, attempts: int = 1) -> None:
        super().__init__(message)
        self.attempts = attempts

# Signals a definite provider rejection that should not be retried automatically.
class PermanentProviderError(Exception):
    """The provider explicitly rejected a request that should not be retried."""

    # Stores a definite rejection and the number of attempts made.
    def __init__(self, message: str, attempts: int = 1) -> None:
        super().__init__(message)
        self.attempts = attempts