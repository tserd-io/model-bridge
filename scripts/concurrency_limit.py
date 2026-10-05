from collections.abc import Iterator
from contextlib import contextmanager
from threading import Lock


# Indicates that no generation slot was available.
class ConcurrencyLimitExceeded(Exception):
    pass


# Enforces platform and tenant capacity within one application process.
class GenerationConcurrencyLimiter:
    def __init__(self, limit: int) -> None:
        if limit < 1:
            raise ValueError("Concurrency limit must be positive")

        self.limit = limit
        self._active = 0
        self._tenant_active: dict[str, int] = {}
        self._lock = Lock()

    # Admits immediately and releases every acquired counter on exit.
    @contextmanager
    def slot(
        self,
        *,
        tenant_id: str | None = None,
        tenant_limit: int | None = None,
    ) -> Iterator[None]:
        if (tenant_id is None) != (tenant_limit is None):
            raise ValueError("Tenant identity and limit must be supplied together")

        if tenant_id is not None:
            if not tenant_id.strip():
                raise ValueError("Tenant identity must be nonempty")
            if tenant_limit is None or tenant_limit < 1:
                raise ValueError("Tenant concurrency limit must be positive")

        # Check both ceilings before modifying either counter.
        with self._lock:
            if self._active >= self.limit:
                raise ConcurrencyLimitExceeded()

            if tenant_id is not None:
                active = self._tenant_active.get(tenant_id, 0)
                if active >= tenant_limit:
                    raise ConcurrencyLimitExceeded()

                self._tenant_active[tenant_id] = active + 1

            self._active += 1

        try:
            yield
        finally:
            # Release on success, failure, or task cancellation.
            with self._lock:
                self._active -= 1

                if tenant_id is not None:
                    remaining = self._tenant_active[tenant_id] - 1
                    if remaining:
                        self._tenant_active[tenant_id] = remaining
                    else:
                        del self._tenant_active[tenant_id]