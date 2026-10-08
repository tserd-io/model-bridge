import math
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol
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
                retry_after = self._timestamps[0] + self.window_seconds - now
                return max(1, math.ceil(retry_after))

            self._timestamps.append(now)
            return None


# Defines the admission boundary used by the application service.
class TenantAdmissionLimiter(Protocol):
    # Returns a retry delay unless both tenant and platform quotas admit the request.
    def try_acquire(
        self, tenant_id: str, *, limit: int, window_seconds: float
    ) -> int | None: ...


# Keeps one tenant's admission history and the window used to expire it.
@dataclass(slots=True)
class _TenantWindow:
    limit: int
    seconds: float
    timestamps: deque[float] = field(default_factory=deque)


# Enforces platform and tenant windows atomically within one application process.
class TenantRateLimiter:
    # Accepts a monotonic clock so window calculations can be verified deterministically.
    def __init__(
        self,
        platform_limit: int,
        platform_window_seconds: float,
        *,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._validate(platform_limit, platform_window_seconds)
        self._platform_limit = platform_limit
        self._platform_window = platform_window_seconds
        self._clock = clock if clock is not None else time.monotonic
        self._timestamps: deque[float] = deque()
        self._tenants: dict[str, _TenantWindow] = {}
        self._lock = Lock()

    # Validates quota arguments supplied outside the settings loader.
    @staticmethod
    def _validate(limit: int, seconds: float) -> None:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("Rate limit must be a positive integer")
        if not math.isfinite(seconds) or seconds <= 0:
            raise ValueError("Rate-limit window must be positive and finite")

    # Expires admissions exactly at the end of their rolling window.
    @staticmethod
    def _expire(timestamps: deque[float], cutoff: float) -> None:
        while timestamps and timestamps[0] <= cutoff:
            timestamps.popleft()

    # Supplies a read-only snapshot of tenant histories retained in memory.
    @property
    def active_tenant_ids(self) -> frozenset[str]:
        with self._lock:
            return frozenset(self._tenants)

    # Checks both windows before spending either quota; rejected requests allocate no tenant history.
    def try_acquire(
        self, tenant_id: str, *, limit: int, window_seconds: float
    ) -> int | None:
        self._validate(limit, window_seconds)
        with self._lock:
            now = self._clock()
            self._expire(self._timestamps, now - self._platform_window)
            for identity, history in list(self._tenants.items()):
                self._expire(history.timestamps, now - history.seconds)
                if not history.timestamps:
                    del self._tenants[identity]

            history = self._tenants.get(tenant_id)
            if history is not None and (
                history.limit != limit or history.seconds != window_seconds
            ):
                # Changing a live window would erase or reinterpret already spent quota.
                raise ValueError(
                    "Tenant rate policy changed while admissions remain active; restart with new settings"
                )
            waits = []
            if len(self._timestamps) >= self._platform_limit:
                waits.append(self._timestamps[0] + self._platform_window - now)
            if history is not None and len(history.timestamps) >= limit:
                waits.append(history.timestamps[0] + window_seconds - now)
            if waits:
                return max(1, math.ceil(max(waits)))

            if history is None:
                history = _TenantWindow(limit, window_seconds)
                self._tenants[tenant_id] = history
            self._timestamps.append(now)
            history.timestamps.append(now)
            return None


            if len(self._timestamps) >= self.limit:
                retry_after = (
                    self._timestamps[0] + self.window_seconds - now
                )
                return max(1, math.ceil(retry_after))

            self._timestamps.append(now)
            return None
