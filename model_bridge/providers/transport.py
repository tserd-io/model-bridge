import asyncio
from collections.abc import Awaitable
from typing import TypeVar

T = TypeVar("T")
RETRYABLE_STATUS_CODES = frozenset({408, 429, 500, 502, 503, 504})


# Bounds one asynchronous operation and raises a timeout if it does not finish in time.
async def run_with_attempt_timeout(awaitable: Awaitable[T], timeout: float) -> T:
    return await asyncio.wait_for(awaitable, timeout=timeout)


# Checks whether the HTTP status belongs to the configured transient-error set.
def is_retryable_status_code(status_code: int) -> bool:
    return status_code in RETRYABLE_STATUS_CODES
