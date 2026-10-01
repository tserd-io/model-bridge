from concurrent.futures import ThreadPoolExecutor

import pytest

from scripts.circuit_breaker import CircuitBreaker, CircuitOpenError


# Supplies a breaker and a controllable clock without real waiting.
@pytest.fixture
def breaker_clock():
    now = [100.0]
    breaker = CircuitBreaker(
        failure_threshold=2,
        cooldown_seconds=30,
        clock=lambda: now[0],
    )
    return breaker, now


# Checks that success resets consecutive failures before opening occurs.
def test_success_resets_failure_count(breaker_clock):
    breaker, _ = breaker_clock

    breaker.finish(breaker.acquire(), "failure")
    breaker.finish(breaker.acquire(), "success")
    breaker.finish(breaker.acquire(), "failure")
    assert breaker.state == "closed"

    transition = breaker.finish(breaker.acquire(), "failure")
    assert transition == ("closed", "open")

    with pytest.raises(CircuitOpenError) as error:
        breaker.acquire()

    assert error.value.retry_after == 30


# Checks that only one competing caller receives a recovery permit.
def test_half_open_allows_one_probe(breaker_clock):
    breaker, now = breaker_clock
    breaker.finish(breaker.acquire(), "failure")
    breaker.finish(breaker.acquire(), "failure")
    now[0] += 30

    # Attempts admission and records rejections without provider work.
    def acquire_or_none(_):
        try:
            return breaker.acquire()
        except CircuitOpenError:
            return None

    with ThreadPoolExecutor(max_workers=12) as executor:
        results = list(executor.map(acquire_or_none, range(12)))

    permits = [permit for permit in results if permit is not None]
    assert len(permits) == 1
    assert breaker.state == "half_open"

    assert breaker.finish(permits[0], "success") == (
        "half_open",
        "closed",
    )


# Checks that failed or inconclusive probes reopen and restart cooldown.
@pytest.mark.parametrize("outcome", ["failure", "ignored"])
def test_unsuccessful_probe_reopens(breaker_clock, outcome):
    breaker, now = breaker_clock
    breaker.finish(breaker.acquire(), "failure")
    breaker.finish(breaker.acquire(), "failure")
    now[0] += 30

    probe = breaker.acquire()
    assert breaker.finish(probe, outcome) == ("half_open", "open")

    with pytest.raises(CircuitOpenError) as error:
        breaker.acquire()

    assert error.value.retry_after == 30


# Checks that a late success cannot erase a newer outage.
def test_old_success_cannot_close_open_circuit(breaker_clock):
    breaker, _ = breaker_clock
    old_call = breaker.acquire()

    breaker.finish(breaker.acquire(), "failure")
    breaker.finish(breaker.acquire(), "failure")

    assert breaker.finish(old_call, "success") is None
    assert breaker.state == "open"


# Checks that a call's failure cannot be counted twice.
def test_permit_can_only_be_finished_once(breaker_clock):
    breaker, _ = breaker_clock
    permit = breaker.acquire()
    breaker.finish(permit, "failure")

    with pytest.raises(ValueError):
        breaker.finish(permit, "failure")