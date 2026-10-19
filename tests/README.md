# Testing Model Bridge

Start with complete request scenarios. Use focused tests when diagnosing a
specific algorithm or policy boundary. Real provider calls remain opt-in.

```powershell
# Everyday check: routing, replay, limits, resilience and observability.
python -m pytest tests/acceptance -q

# Everything, including a real local Uvicorn server.
python -m pytest -q

# Only process and network behavior.
python -m pytest tests/e2e -q
```

Run from the repository root with development dependencies installed. Pytest
selects fake providers and temporary storage before application imports, so it
does not depend on API credentials or an existing local database. Output is
captured unless a test fails. Linux shutdown tests skip on Windows.

If Windows denies access to its existing `pytest-of-tomsl` temporary folder,
select a fresh workspace directory for that terminal session:

```powershell
$env:PYTEST_ADDOPTS = '--basetemp=data/pytest-' + [guid]::NewGuid().ToString('N')
```

Use a dedicated test directory: pytest clears its chosen base temporary directory.

## Which group answers which question?

| Directory | Question | Boundary |
|---|---|---|
| `acceptance/` | Does a complete user request behave correctly? | In-process HTTP through the real app, service, execution runner, limits and temporary SQLite; controlled provider and identity. |
| `focused/` | Does a tricky policy or algorithm edge case work? | Existing service, middleware, timing, race, cancellation and configuration regressions; narrow mocks where needed. |
| `unit/` | Is a calculation or translation correct? | Pricing, summaries, HTTP outcome conversion and provider SDK translation. |
| `e2e/` | Does the application actually start and communicate over HTTP? | Real Uvicorn process and loopback requests; Linux also verifies shutdown. |
| `support/` | How do scenarios get repeatable inputs? | Fresh-app factory, scripted provider and event-controlled blocking provider. |

Acceptance tests are integration tests, not real-model evaluations. They do
not prove model quality, real credentials, or production database durability.
The Docker job separately checks the packaged production entry point and
readiness. Authentication and advanced storage remain outside project scope.

## Reading a scenario

```python
provider = ScriptedProvider(RetryableProviderError("temporary failure"))
gateway = app_factory(primary=provider)
with gateway.client() as client:
    response = client.post("/chat", json=payload())
assert response.status_code == 200
assert response.json()["attempts"] == 2
assert len(provider.calls) == 2
```

The provider fails once, then returns its default response. The application
must perform the retry itself. Mocking `service.generate` would skip that
behavior, so acceptance tests never replace it. The provider's synthetic
usage/cost fields demonstrate propagation; `unit/test_pricing.py` checks the
calculation independently.

`app_factory` creates independent settings, storage, service and app instances.
The `x-test-tenant` header is interpreted only by this test factory, never by
production authentication. Request IDs may be reused across separate tests
because their stores are independent.

For concurrency, `BlockingProvider` signals when work enters and waits for an
explicit release event. Tests don't guess when a request started using sleeps.
Circuit recovery uses a controllable breaker clock rather than waiting through
the cooldown. Each scenario owns its state; no fixture replaces `main.app`.

## What changed and what was kept

- Removed session-wide automatic service/identity fixtures. Focused tests ask
  explicitly for `test_app`, `isolated_chat_service`, or `policy_service`.
  One disposable app still exists at import time because the production module
  exports `app`; collection is isolated from developer data.
- Consolidated gateway replay, conflict, routing, unknown-result and evaluation
  scenarios into acceptance tests. Preserved detailed race, cancellation,
  byte-boundary and direct-service regression checks in `focused/`.
- Moved the unique Ollama transport and OpenAI correlation checks from the
  manual smoke runner into collected unit tests.
- The old `python -m tests.smoke_test` command now delegates to acceptance and
  adapter tests. `--live` additionally makes one explicitly requested provider
  call using disposable storage. It is not run in CI.
- The existing startup probe is collected by `e2e/test_server.py`; CI no longer
  runs that same probe a second time as a separate command.

This migration simplifies the primary suite and setup, rather than deleting
useful edge coverage to minimize the total test count. A new feature normally
gets one readable acceptance scenario; add focused cases only for distinct
boundary or failure behavior.

CI runs acceptance, then focused/unit tests with sockets blocked (Unix sockets
allowed), then real-server tests with loopback networking. Docker checks remain
in their existing separate job.

## Verification

On 2026-10-19 the migrated Windows suite passed **216 tests**, with **2 Linux-only
shutdown tests skipped**, including the real Uvicorn startup probe. Ruff passed.
This is local verification; the updated GitHub workflow and Linux-only tests
still require a hosted run.
The compatibility smoke command also passed all 20 selected checks using a fresh
workspace temporary directory after the default Windows temp folder denied access.
