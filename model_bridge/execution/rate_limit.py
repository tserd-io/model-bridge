import math
import time
from collections import deque
from threading import Lock


# Tracks admissions within a rolling window and uses a lock to enforce a per-instance limit across threads.
class SlidingWindowRateLimiter:
    # Validates the rate limits and initializes admission history and its thread lock.
    def __init__(self, limit: int, window_seconds: float) -> None:
        if limit < 1:
            raise ValueError("Rate limit must be at least 1")
        if not math.isfinite(window_seconds) or window_seconds <= 0:
            raise ValueError("Rate-limit window must be positive and finite")

        self.limit = limit
        self.window_seconds = window_seconds
        self._timestamps: deque[float] = deque()
        self._lock = Lock()

    # Expires old admissions, then atomically admits a request or returns a rounded retry delay.
    def try_acquire(self) -> int | None:
        """Return None when allowed, or seconds until a retry is possible."""
        with self._lock:
            now = time.monotonic()
            cutoff = now - self.window_seconds

            while self._timestamps and self._timestamps[0] <= cutoff:
                self._timestamps.popleft()

            if len(self._timestamps) >= self.limit:
                retry_after = (
                    self._timestamps[0] + self.window_seconds - now
                )
                return max(1, math.ceil(retry_after))

            self._timestamps.append(now)
            return None
