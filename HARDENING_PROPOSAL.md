# Proposed runtime policy implementation

## Deliverables review

Reviewed against the current working tree on 2026-10-07. Checked items are complete
within the scope described; unchecked items are incomplete or still require
verification. Implemented code excerpts have been removed from the body.
Runtime files were reviewed, not modified during this review.

- [x] **Test implementation:** the proposed fixtures, smoke checks, and regression cases are implemented in [tests](./tests), including four permanent outcome-metadata regression cases.
- [x] **Independent request-policy module:** [request_policy.py](./model_bridge/application/request_policy.py) checks token ceilings, message length, normalized caller-input bytes, and UTF-8 without HTTP or provider dependencies.
- [x] **HTTP validation layer:** per-application schemas, safe Unicode errors, and 413/422 response mapping are implemented in [schemas.py](./model_bridge/api/schemas.py) and [responses.py](./model_bridge/api/responses.py).
- [x] **Output-token enforcement end to end:** platform and tenant ceilings are enforced before admission/storage, with no silent clamping; boundary tests pass.
- [x] **Input-size enforcement end to end:** actual body bytes and normalized input are checked independently; tenant boundaries, Unicode rejection, trusted-identity exclusion, and replay tests pass.
- [x] **Atomic rate-limiter implementation:** [rate_limit.py](./model_bridge/execution/rate_limit.py) implements combined admission, independent expiry, lazy cleanup, and live-policy-change rejection.
- [x] **Rate-admission integration:** tenant/platform rejection, quota retention, and rate-limit Retry-After tests pass.
- [x] **Generation deadline enforcement:** [generation.py](./model_bridge/execution/generation.py) passes the deadline into attempt execution, rechecks after admission/logging, releases unused probes, and preserves prior attempts and uncertainty.
- [x] **Provider-specific timeouts and fallback:** attempts now use the active provider's timeout capped by remaining time; fallback shares the original deadline and attempt budget. All 19 generation regressions pass.
- [x] **Configurable structured logging:** [logging.py](./model_bridge/observability/logging.py) imports JSON and applies configured enable/level settings; severity and privacy tests pass.
- [x] **SQLite connection timeout:** [request_store.py](./model_bridge/storage/request_store.py) uses the configured write timeout while readiness retains its independent one-second read timeout.
- [x] **Processing leases:** tenant generation budgets plus the configured lease margin are passed into claims; the lease and replay tests pass.
- [x] **Outcome delivery metadata:** [chat_service.py](./model_bridge/application/chat_service.py) preserves `retry_after`, `error_type`, and `timed_out`; permanent service-level regression tests pass.
- [x] **Outcome-metadata regression coverage:** [test_chat_service.py](./tests/test_chat_service.py) preclaims a real request, verifies the service-produced in-progress outcome and HTTP `Retry-After: 1`, and checks error classification/timeout flags through direct service calls for raw timeouts, deadline expiry, and permanent rejection. All four cases pass.
- [x] **Application configuration consistency:** [main.py](./model_bridge/main.py) derives settings from an injected service and rejects explicit mismatches; both consistency regressions pass.
- [x] **Configured startup wiring:** one-worker startup reads shutdown grace, preserves the importable app, and is used by [Dockerfile](./Dockerfile). Startup-argument and Docker-command tests pass.
- [x] **Local verification:** full Windows test suite, Ruff, fake-provider smoke checks, and isolated real Uvicorn startup checks pass.
- [ ] **Linux shutdown and container verification:** two real SIGTERM tests skip on Windows. No Docker build/startup or Linux shutdown run was verified in this review.
- [ ] **Final acceptance:** complete the outstanding Linux/container verification. Outcome metadata and its permanent regression coverage are finished.

### Resolved outcome-metadata finding

The state-response helper now preserves retry/error/timeout metadata. The new
tests exercise the public service workflow and response conversion, rather than
constructing an outcome directly. The earlier logging, generation, tuple-return,
and fallback-timeout findings are also fixed.

### Current verification

- Latest focused run: **19 passed**, covering service and HTTP response tests, including the four new regression cases (`python -m pytest tests/test_chat_service.py tests/test_chat_responses.py -o addopts= -q --tb=short`).
- Latest test-file Ruff check: passed. No production source changes were needed.
- Previous full-suite run, before the four new cases: **199 passed, 2 skipped** (`python -m pytest tests -o addopts= -q --tb=short`); both skips require Linux. The full suite was not rerun for this focused test-only addition.
- Ruff: passed (`python -m ruff check model_bridge tests --config .github/workflows/ruff.toml`).
- Fake-provider smoke checks: passed (`python -m tests.smoke_test`), without live-provider requests.
- Real Uvicorn startup: liveness, readiness, and fake-provider chat passed (`python -m tests.check_startup`). This uses the test application factory, not a Docker image.
- The earlier one-off metadata probe has been superseded by permanent passing regression tests.
- Only tests and this document changed in the coverage update. Earlier verification records below are historical.

## Historical test implementation status

Finished on 2026-10-07: all test changes proposed in this document have been
implemented in the test suite. Their proposal sections have been removed.

- [x] Shared fixtures and fake-provider smoke checks.
- [x] Service, gateway, circuit-breaker, and concurrency test collaborators.
- [x] Generation deadline, timeout, and probe-release regression tests.
- [x] Custom input/token limits and invalid-Unicode regression tests.
- [x] Logger restoration, shutdown assertions, and application/settings consistency tests.

At the time of this test-only change, runtime hardening was not implemented and
`TenantRateLimiter` was missing, preventing full-suite collection. The subsequent
runtime implementation is assessed in the deliverables review above.

Verification of this test-only change:

- Ruff checks on `tests`: passed.
- Generation and runtime-settings modules: 16 passed, 17 failed, 2 skipped.
  Failures expose pending timeout, deadline, logging, startup, and settings behavior.
  The skips are the existing Linux-only signal tests on Windows.
- Full-suite collection: blocked in seven modules by the missing
  `TenantRateLimiter` implementation. These tests must pass after the runtime
  changes are applied; no expected-failure markers or fallback implementations
  were added to hide the missing behavior.

The original runtime requirements and review notes follow for context.

Implemented code excerpts have been removed. Only unresolved code is retained in the remaining proposed code section; use the source files for completed implementation details.

## Runtime behavior and boundaries

1. **Output tokens:** reject requests above the effective tenant or platform ceiling before rate admission, storage, or generation. No silent clamping. HTTP returns 422; application calls receive `invalid_request`.
2. **Input size:** check both actual HTTP bytes and normalized caller-controlled fields. Trusted tenant identity and transport metadata do not affect normalized size or alter the existing storage fingerprint. Oversized input returns 413 / `input_too_large`.
3. **Generation deadline:** each call to generation receives its tenant's budget. The monotonic deadline is created at generation start and shared across attempts, delays, and fallback, with time rechecked after breaker acquisition and logging. This is a generation deadline, not an HTTP or storage deadline.
4. **Rate admission:** use one lock to check and spend tenant and platform quotas together. Denials spend neither. Replay, conflicts, and later provider failures still count as admitted requests. Clean expired histories on the next admission; no background thread. These limits remain process-local.
5. **Logging:** configure the process-wide application logger once when composing the app. Apply enabled/minimum severity, use explicit event severities and UTC timestamps, and continue logging operational fields without prompt, generated content, credentials, or exception text. Uvicorn's own logger remains separate. Callers of the general log_event helper must continue to supply only permitted operational fields.
6. **SQLite:** pass configured busy timeout into write connections and derive processing leases from the tenant's generation budget plus the configured margin. Keep readiness's independent one-second read timeout. Existing request keys, hashes, and state transitions are preserved.
7. **Shutdown:** introduce `run_server` and use it as Docker's command. One worker receives configured graceful shutdown seconds. Keep the imported `main:app` entry point available. The module guard avoids constructing two applications when started with `python -m model_bridge.main`.
8. **Provider timeouts:** switch the attempt timeout when switching providers, always capped by remaining generation time. Attempts and backoff remain one shared budget using the primary retry settings. Fallback does not receive a fresh attempt count or deadline. Wrap provider calls using the existing transport timeout helper, so an adapter ignoring its timeout parameter is still cancelled. As with other asyncio timeouts, adapters must cooperate with cancellation.

## Modular design and self-review

- Add only one runtime module: `application/request_policy.py`. It performs deterministic input checks and returns semantic policy violations; it imports neither FastAPI nor storage/provider code.
- Keep request-body measurement in the HTTP adapter and pass the byte count as a keyword to `handle`. Do not add it to ChatCommand, so serialization and replay hashes are unchanged.
- Construct HTTP schemas per application from its settings. This preserves FastAPI field-validation responses and supports injected settings with ceilings above or below the previous fixed defaults. The application still checks policy independently for direct service calls.
- Define `TenantAdmissionLimiter` as the service's structural interface; keep the existing standalone SlidingWindowRateLimiter for its separate unit use. Production wiring uses TenantRateLimiter. Test collaborators are updated to the new boundary instead of adding production special cases for old fakes.
- Reject changes to a tenant's live rate policy while history remains active. Settings are startup configuration; restarting adopts a new policy. Reinterpreting live histories could otherwise bypass quota.
- Rate cleanup scans retained tenant histories under the lock. This is deliberately simple for the present deployment; measure contention before adopting a more complex expiry index. Cleanup is lazy, so idle expired entries remain until the next admission.
- The logger is process-wide, not tenant-specific. Multiple applications with different logging policies in one process are not independently configured.
- Fix one test-fixture issue: assigning logger.level restores the numeric value but leaves Python logging's severity cache stale. Restore it with setLevel instead.
- Additional checks cover custom token/message ceilings, invalid Unicode in direct and HTTP input, configuration consistency, provider timeout enforcement, and deadline expiry during local admission.

## Historical verification on the isolated proposal

These results describe the earlier isolated implementation, not the current
working tree or the test-only implementation above.

- Full existing and newly specified suite: **199 passed, 2 skipped**.
- Includes the four earlier focused checks and ten regression cases for the review findings.
- Ruff lint: passed.
- Fake-provider smoke script: passed.
- Real Uvicorn startup: liveness, readiness, and fake-provider chat passed.
- The two Linux signal/shutdown cases were skipped on Windows; run them in Linux CI before claiming shutdown behavior verified. No Docker image build or live-provider evaluation was performed for this proposal.
- At that time, no application source changes were applied to the working tree. Tests and runtime changes have since been added; their current completion status is recorded in the deliverables review above.

## Corrections following review

- **Unicode errors:** request schemas validate UTF-8 for request IDs, messages, and body tenant metadata. Validation responses omit the rejected `input` field while preserving field locations, error types, messages, and validation context; this also avoids echoing private prompt text. Direct service policy retains its separate normalized-input validation.
- **One application configuration:** `create_app(service=...)` derives its settings from that service. Supplying different explicit settings raises ValueError before app construction rather than mixing HTTP limits, logging, and execution policies.
- **Deadline before provider work:** check remaining time after breaker acquisition and again after synchronous logging. Expiry releases a permit as ignored, records no provider call, and preserves any earlier actual attempt count and unknown outcome. A non-expired call receives the reduced remaining timeout.
- **Shutdown assertion:** accept either normal exit or `-signal.SIGTERM` after cleanup, since Uvicorn may re-raise the captured signal. Checks for short requests finishing, longer requests being cancelled, and released capacity remain; Windows still skips the real Linux signal cases.

## Remaining proposed code

None. All proposed runtime code, including outcome metadata, is implemented.
Completed snippets have been removed; the unchecked deliverables above concern
Linux shutdown and container verification.

