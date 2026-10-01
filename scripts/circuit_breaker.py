import math
import time
from dataclasses import dataclass
from threading import Lock
from typing import Callable, Literal


CircuitState = Literal["closed", "open", "half_open"]
CallOutcome = Literal["success", "failure", "ignored"]


# Indicates that no provider call was admitted.
class CircuitOpenError(Exception):
    def __init__(self, retry_after: int) -> None:
        super().__init__("Provider circuit is temporarily unavailable")
        self.retry_after = retry_after


# Identifies an admitted call and the circuit generation that admitted it.
@dataclass(frozen=True)
class CircuitPermit:
    call_id: int
    generation: int


# Shares failure history and recovery-probe admission within one process.
class CircuitBreaker:
    def __init__(
        self,
        failure_threshold: int = 5,
        cooldown_seconds: float = 30,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if failure_threshold < 1:
            raise ValueError("failure_threshold must be positive")
        if not math.isfinite(cooldown_seconds) or cooldown_seconds <= 0:
            raise ValueError("cooldown_seconds must be positive and finite")

        self._threshold = failure_threshold
        self._cooldown = cooldown_seconds
        self._clock = clock
        self._lock = Lock()

        self._state: CircuitState = "closed"
        self._failures = 0
        self._retry_at = 0.0
        self._generation = 0
        self._next_call_id = 0
        self._active: set[CircuitPermit] = set()

    # Returns the current state without exposing mutable internal data.
    @property
    def state(self) -> CircuitState:
        with self._lock:
            return self._state

    # Admits a call or rejects it before any provider work starts.
    def acquire(self) -> CircuitPermit:
        with self._lock:
            now = self._clock()

            if self._state == "open":
                if now < self._retry_at:
                    raise CircuitOpenError(
                        max(1, math.ceil(self._retry_at - now))
                    )

                self._state = "half_open"

            elif self._state == "half_open":
                # A recovery probe is already running.
                raise CircuitOpenError(retry_after=1)

            self._next_call_id += 1
            permit = CircuitPermit(
                call_id=self._next_call_id,
                generation=self._generation,
            )
            self._active.add(permit)
            return permit

    # Records an admitted call exactly once and returns any state transition.
    def finish(
        self,
        permit: CircuitPermit,
        outcome: CallOutcome,
    ) -> tuple[CircuitState, CircuitState] | None:
        if outcome not in {"success", "failure", "ignored"}:
            raise ValueError("Unsupported circuit outcome")

        with self._lock:
            if permit not in self._active:
                raise ValueError("Unknown or already completed permit")
            self._active.remove(permit)

            # Ignore results from calls admitted before the circuit opened.
            if permit.generation != self._generation:
                return None

            previous = self._state

            if self._state == "half_open":
                if outcome == "success":
                    self._state = "closed"
                    self._failures = 0
                    self._generation += 1
                else:
                    # Failure or an inconclusive/cancelled probe starts
                    # another cooldown, avoiding a stuck half-open state.
                    self._open()

            elif outcome == "success":
                self._failures = 0

            elif outcome == "failure":
                self._failures += 1
                if self._failures >= self._threshold:
                    self._open()

            # Ignored outcomes do not affect a closed circuit's count.
            if self._state != previous:
                return previous, self._state
            return None

    # Starts a new cooldown and invalidates older calls' state updates.
    # Called only while the lock is held.
    def _open(self) -> None:
        self._state = "open"
        self._retry_at = self._clock() + self._cooldown
        self._generation += 1