# Model Bridge: Architecture and Behavior

Last reviewed against source: 2026-10-05.

Model Bridge is a Python gateway that exposes one API for calling LLM
providers. It selects models, controls generation attempts, records
request outcomes, and evaluates responses against a versioned dataset.

Use this overview as a starting map for diagnostics and code review.
Verify relevant implementation and tests before drawing conclusions;
this document describes the source at the review date, not a guarantee
of current behavior or a record of passing tests.

## Source map

Application code is grouped by responsibility. Tests and supporting commands
live in `tests/`, separate from the `model_bridge` package. Dependency files
live at the repository root. Package `__init__.py` files, virtual environments,
caches, and local SQLite files are omitted from this map.

```text
model-bridge/
├─ model_bridge/
│  ├─ main.py                     # Service construction and application factory
│  ├─ api/
│  │  ├─ chat.py                  # Chat HTTP route
│  │  ├─ health.py                # Liveness and readiness routes
│  │  ├─ dependencies.py          # Trusted tenant and service dependencies
│  │  ├─ schemas.py               # Public request and response models
│  │  └─ responses.py             # Outcome-to-HTTP conversion
│  ├─ application/
│  │  ├─ chat_service.py          # Routing, admission, replay and generation workflow
│  │  └─ outcomes.py              # Plain command, result and outcome dataclasses
│  ├─ providers/
│  │  ├─ contracts.py             # Provider protocol, results and exceptions
│  │  ├─ factory.py               # Adapter construction from explicit settings
│  │  ├─ ollama.py
│  │  ├─ openai.py
│  │  ├─ fake.py
│  │  ├─ pricing.py               # Token-cost calculation
│  │  └─ transport.py             # Shared timeout and status classification
│  ├─ execution/
│  │  ├─ generation.py            # Attempts, deadlines, fallback and circuit admission
│  │  ├─ circuit_breaker.py
│  │  ├─ rate_limit.py
│  │  └─ concurrency_limit.py
│  ├─ storage/
│  │  └─ request_store.py         # SQLite tenant/request state and replay
│  ├─ config/
│  │  ├─ loader.py                # JSON loading and environment overrides
│  │  ├─ models.py                # Validated settings
│  │  └─ config.json
│  ├─ observability/
│  │  ├─ logging.py               # Timestamped JSON events
│  │  ├─ metrics.py               # Counters, histograms and active generation gauge
│  │  └─ labels.py                # Bounded tenant labels and label normalization
│  └─ evaluations/
│     ├─ README.md                # Evaluation usage and dataset format
│     ├─ eval_runner.py
│     ├─ eval_summary.py
│     ├─ eval_writer.py
│     └─ datasets/
│        └─ golden_dataset.json
├─ tests/
│  ├─ conftest.py                 # Shared fixtures and isolated test services
│  ├─ testing_app.py              # Fake-provider app factory with simulated identity
│  ├─ check_startup.py            # Actual Uvicorn startup check
│  ├─ smoke_test.py               # Manual local checks; --live opts into a provider call
│  ├─ test_chat_responses.py      # Outcome-to-HTTP status, body and headers
│  ├─ test_chat_service.py        # Direct service execution and tenant-scoped replay
│  ├─ test_circuit_breaker.py      # Circuit state transitions
│  ├─ test_concurrency_limits.py   # Generation capacity and retryable rejection
│  ├─ test_evaluation.py          # Evaluation aggregation and output
│  ├─ test_gateway.py             # Routing, retries, persistence and replay
│  ├─ test_generation_circuit.py  # Breaker admission during provider execution
│  ├─ test_health.py              # Liveness and database readiness
│  ├─ test_input_limits.py        # Message length and HTTP body limits
│  └─ test_tenant_concurrency.py  # Tenant capacity, identity and shared platform limits
├─ requirements.txt              # Application dependencies
├─ requirements-dev.txt          # Development dependencies; includes requirements.txt
├─ requirements-ci.lock          # Pinned, hashed dependencies for CI
├─ data/                          # Runtime database and evaluation output; ignored by Git
├─ .github/
│  └─ workflows/
│     ├─ ci.yaml                  # Dependency, lint, audit, test and startup checks
│     └─ ruff.toml                # CI lint rules
├─ .gitignore
├─ pytest.ini
├─ README.md
└─ SYSTEM_OVERVIEW.md
```

## Main components

| Module or package | Responsibility |
|---|---|
| `model_bridge/main.py` | Constructs the chat service and registers HTTP routers through `create_app()`. |
| `model_bridge/api/` | Validates HTTP input, obtains trusted tenant identity, converts outcomes and exposes health probes. |
| `model_bridge/application/chat_service.py` | Coordinates routing, admission, idempotency, provider execution and outcome persistence. |
| `model_bridge/application/outcomes.py` | Defines `ChatCommand`, `ChatResult` and `ChatOutcome` without HTTP framework types. |
| `model_bridge/providers/` | Defines the provider contract, configured adapter factory, real/fake adapters and transport helpers. |
| `model_bridge/execution/` | Coordinates generation and process-local circuit, rate and concurrency policies. |
| `model_bridge/storage/request_store.py` | Claims tenant/request identities and persists processing states and responses. |
| `model_bridge/config/` | Loads and validates configuration while preserving repository-root storage resolution. |
| `model_bridge/observability/` | Emits structured events and bounded Prometheus instrumentation. |
| `model_bridge/evaluations/` | Runs, scores, summarizes and writes versioned evaluations. |

`create_app()` stores an injected `ChatService` in `app.state.chat_service`.
The API dependency retrieves that service; route modules do not import
`main.py`. The service returns plain outcomes, and `api/responses.py`
maps them to the public response schema, status codes and headers.
Tests and the smoke runner replace service dependencies rather than
module globals. Authentication remains deliberately deferred.

## Request lifecycle

1. **Check input and identity.** Body-limit middleware caps request bytes;
   FastAPI and Pydantic validate fields, including maximum message length.
   The chat dependency requires a trusted tenant identity. The handler
   replaces the body's tenant ID with that identity and resolves its policy.
2. **Choose a route.** Simple tasks select `fast`; complex and high-risk
   tasks select `balanced`. Without a task type, the requested model
   preference is used.
3. **Apply the rate limit.** Rejected requests receive HTTP 429 and a
   `Retry-After` header before database or provider work begins.
4. **Claim the request.** SQLite checks the tenant/request composite key
   and a hash of the validated payload. Existing requests may return a saved result,
   pending state, uncertain outcome, or conflict.
5. **Acquire capacity.** New generation must fit both the process limit
   and the authenticated tenant's limit. Saturation returns HTTP 503 with
   `Retry-After: 1` and saves a retryable request state. Saved-response
   replay bypasses generation capacity, but still passes the rate limit.
6. **Generate a response.** The generation runner checks circuit
   admission and invokes the provider with a bounded attempt timeout.
   Retryable failures may trigger fallback or backoff within the
   original generation deadline and attempt budget.
7. **Persist the outcome.** Successful responses are saved for replay.
   Failures are classified as definite or uncertain.
8. **Return and record.** The API returns a normalized response and
   records completion metrics and logs.

High-risk routing sets `human_review_required`; it does not implement
a human approval workflow.

## Tenant identity, capacity, and configuration

Every chat request requires an `AuthenticatedTenant` in
`request.state.authenticated_tenant`. The production app currently has
no authenticator that populates this state, so otherwise valid chat
requests receive HTTP 401. A client-supplied tenant ID is not proof of
identity. Connecting verified credentials to a tenant remains necessary.

Tests supply identity through dependency overrides. The explicit
`tests.testing_app:create_test_app` factory also supplies a test
identity, and requires a fake provider, no fallback, and an explicit
storage path. It is a test entry point, not production authentication.

The generation limiter checks both counters under one lock, rejects
immediately when full, and releases capacity on success, failure, or
cancellation. A slot covers generation attempts and retry delays; it
does not cover body parsing, database claims, or final result writes.
Empty tenant counters are removed. Limits apply per process: multiple
workers or replicas multiply capacity unless admission is coordinated.
Concurrency and input limits reduce memory pressure but do not provide
a hard per-tenant RAM budget or bound retained database data.

The settings loader validates JSON and supported environment overrides,
resolves storage paths against the project root, and exposes both typed
settings and legacy constants. Tenant overrides inherit defaults and
are validated against platform ceilings. Unknown tenant IDs receive
default policy; configuration is not an authorization registry.

Runtime enforcement is still partial:

| Setting | Current enforcement |
|---|---|
| Platform and tenant concurrency | Enforced around generation. |
| Platform input bytes and message characters | Enforced by middleware and request validation. |
| Tenant input bytes, output tokens, deadline, and rate limit | Defined and validated, but not applied by the chat service. |
| Platform output tokens | Request schema still hardcodes a maximum of 8192 instead of reading this setting. |
| Platform deadline and rate limit | Injected into the chat service from validated settings. |
| Metrics enabled | Controls application metric recording and metrics endpoint registration. |
| Tenant metric labels | Disabled by default; enabled labels use a bounded allowlist and an `other` bucket. |
| Logging switches and shutdown grace | Defined but not wired into runtime behavior. |
| Storage busy timeout and lease margin | Runtime still uses literal five-second values. |

Validated settings describe policy. Check their consumers before assuming
that every configured tenant allowance or operational switch is enforced.

## Provider abstraction

Each provider implements an asynchronous `complete()` method and
returns `GenerationResult`.

The adapters translate provider-specific responses and errors into
shared types. This lets generation and API code work with a consistent
interface.

- **Ollama:** local model calls and reported token usage.
- **OpenAI:** hosted model calls, request-correlation headers, token
  usage, and estimated cost where pricing is configured.
- **Fake provider:** predictable responses without external calls.

OpenAI SDK retries are disabled so the gateway owns the retry budget.

## Reliability and request state

### Idempotency

Requests are identified by the composite key `(tenant_id, request_id)`.
The handler obtains tenant identity from its trusted dependency before
claiming or updating storage. Different tenants can reuse the same
request ID without colliding.

Within one tenant, repeating a successful request with the same ID and
validated payload returns the saved response with `cache_hit=true` and
an `X-Idempotent-Replay: true` header. Reusing that ID with different
input returns HTTP 409.

Startup creates the database and requests table when absent. Disposable
prototype storage was reset, and application startup verified the new
composite primary key. Automatic schema migration and startup rejection
of incompatible existing tables are not implemented.

SQLite uses transactions to coordinate request claims. Processing
leases identify work that did not record a final result; expired
in-progress requests become `unknown` when checked again.

Requests rejected for generation capacity use `retryable`; a later
matching claim can atomically reacquire them. There is no retention
cleanup. Database claim and final-write failures do not yet have a
consistent API recovery policy, including failed saves after generation.

An unknown result means generation may have completed remotely.
Replaying that request does not automatically start generation again.
This does not guarantee exactly-once execution at an external provider.

### Retries and fallback

Generation distinguishes transient failures, permanent rejections,
and uncertain outcomes. Retry and fallback attempts share a deadline
and attempt budget.

Fallback after an uncertain failure can duplicate remote work.
A timeout does not prove that the provider stopped processing.

The default configuration selects Ollama for both primary and fallback
using the same model mapping and host. An independent recovery path
requires different configuration.

### Circuit breaker

The breaker has three states:

- **Closed:** calls are admitted and qualifying failures are counted.
- **Open:** calls are rejected during a cooldown.
- **Half-open:** one recovery probe is admitted.

A successful probe closes the circuit. An unsuccessful or cancelled
probe starts another cooldown. Results from older circuit generations
cannot overwrite newer state.

Same-backend primary and fallback adapters share a breaker.
Circuit rejections do not count as actual provider attempts.

Rate limiting, circuit state, and concurrency counters are process-local.
Rate limiting controls admission frequency; concurrency separately limits
active generation jobs.

The chat service still catches `CircuitUnavailableError` through the generic
retryable-provider handler. A circuit rejection before any provider call
is stored as a terminal failure, without forwarding its retry delay.
This integration gap is separate from the breaker's state-machine tests.

## Storage, logs, and health

SQLite stores request payloads and successful responses. Operational
JSON logs include explicit UTC timestamps with millisecond precision,
request IDs, routes, attempts, outcomes, and latency. Request and
provider-attempt events include tenant identity. Operational events
omit prompt and response content.

When metrics are enabled, `/metrics/` exposes request counts, errors,
retries, fallback, latency, cache hits, available token usage, and
estimated cost. `llm_active_generations` measures generation jobs holding
capacity in the current process. It includes retry delays and releases
on success, failure, or cancellation.

Tenant metric labels are bounded. With tenant labels disabled, all
tenants use `all`. When enabled, configured allowlisted tenants receive
individual labels; other tenants use `other`. The allowlist is limited
to 100 entries.

Disabling metrics stops application metric recording and omits the
metrics endpoint. Dedicated storage-failure metrics remain
unimplemented. Usage and cost estimates are not a complete ledger of
remote work.

| Endpoint | Meaning |
|---|---|
| `GET /health/live` | The application can respond. |
| `GET /health/ready` | The existing requests table can be read. |

Readiness opens SQLite read-only, so it does not create a missing
database. It checks that the requests table can be read, but does not
validate its complete schema, database writes, or provider availability.
An incompatible table may therefore pass readiness and fail chat requests.

## Evaluation pipeline

Evaluation is split into three modules under `model_bridge/evaluations/`:

| Module | Responsibility |
|---|---|
| `eval_runner.py` | Reads the dataset, sends requests through a supplied client, and scores each case. |
| `eval_summary.py` | Calculates aggregate metrics from scored records. |
| `eval_writer.py` | Writes per-case JSONL and summary JSON files. |

Scoring supports allowed answers, required answer points, and expected
JSON fields. Summaries include quality scores, latency percentiles,
availability, error rates, token totals, and available cost estimates.

Dataset and prompt versions identify what was evaluated. Grounding
and agentic metrics remain placeholders because the gateway does not
provide retrieval evidence or execute tools.

Run the evaluation CLI from the repository root:

```sh
python -m model_bridge.evaluations.eval_runner
```

The CLI creates its own local API client and uses the configured
provider. New results are written under `data/evaluation_runs/`, outside
the application package. Programmatic callers supply the client and manage its lifetime.
The CLI currently supplies no authenticated identity, so its chat calls
receive 401 until authentication is integrated. Successful evaluation
tests use an application client with a test identity; they do not prove
the standalone CLI can generate responses.

## What the tests demonstrate

Test files are under `tests/`.

| Test file | Coverage |
|---|---|
| `test_gateway.py` | Response normalization, saved-response replay, conflicting IDs, routing, retries, fallback, unknown outcomes, rate-limit headers, concurrent limiter admission, shared circuit state, and evaluation output. |
| `test_circuit_breaker.py` | Failure thresholds, success resets, cooldown, single recovery-probe admission, stale completion handling, and duplicate permit completion. |
| `test_generation_circuit.py` | Circuit integration with generation, fallback, attempt counting, uncertainty, deadlines, cancellation, and error classification. |
| `test_health.py` | Liveness independence, readable storage, missing/corrupt/schema-less databases, and recovery after storage is restored. |
| `test_evaluation.py` | Aggregation of saved results, including mixed outcomes and incomplete cost information. |
| `test_concurrency_limits.py` | Capacity saturation and recovery, simultaneous admission, release on failure/cancellation, retryable rejection, and replay at full capacity. |
| `test_input_limits.py` | Character and byte boundaries, Unicode messages, chunked bodies, missing/misleading content lengths, and oversized invalid JSON. |
| `test_tenant_concurrency.py` | Tenant and platform ceilings, atomic admission, cleanup on cancellation, policy overrides, body-tenant spoofing resistance, and unauthenticated rejection. |
| `tests/smoke_test.py` | Manual checks for adapter behavior, tracing headers, retries, timeouts, deadlines, and idempotency. |
| `test_chat_responses.py` | HTTP status, response data, retry guidance and replay headers for application outcomes. |
| `test_chat_service.py` | Direct service execution and tenant-scoped replay without an HTTP client. |

Automated tests use fake providers, controlled clocks, mocks, and
temporary databases to check gateway behavior without requiring
live model responses. Set the fake provider and a temporary
`IDEMPOTENCY_DB_PATH` before importing the application, which constructs
its configured providers and request store at import time.
`conftest.py` supplies a default test identity for ordinary API tests;
identity-specific tests control that dependency themselves.

These tests verify selected behaviors. They do not establish model
quality, production load capacity, tenant isolation, or complete
coverage of every external SDK failure.

Run manual checks from the repository root with `python -m tests.smoke_test`.
Run the server startup check with `python -m tests.check_startup`.
Functions named `smoke_*` are not automatically executed by pytest.
The manual runner uses simulated providers by default. Its idempotency
checks use temporary storage, test identity, isolated admission state,
and controlled retry settings.

Passing `--live` additionally calls the configured primary provider
with fallback disabled. Importing the application still initializes
its configured request store, so set a temporary `IDEMPOTENCY_DB_PATH`
before launching isolated checks.

## Continuous integration

Section reviewed against source: 2026-10-05.

`.github/workflows/ci.yaml` runs on pushes and pull requests. Its single
`checks` job uses Python 3.12 on Ubuntu 24.04, has a ten-minute timeout,
and grants read-only repository-content permissions.

Dependency files live at the repository root. For Windows development,
install with `python -m pip install -r requirements-dev.txt`. The CI lock
targets Linux Python 3.12 and includes an unconditional `uvloop` dependency,
so installing that lock on Windows fails; moving it does not change its
platform requirements, pinned versions, or hashes.

| Step | Current behavior |
|---|---|
| Dependency installation | Installs `requirements-ci.lock` with `--require-hashes`, then runs `pip check`. The Python 3.12 lock includes pinned, hashed `uvloop` for `uvicorn[standard]`; CI does not regenerate it. |
| Ruff lint | Checks `model_bridge` and `tests`, and explicitly selects `.github/workflows/ruff.toml`, which enables `E9` and `F` for syntax and Pyflakes checks. |
| Dependency audit | Installs `pip-audit` into a separate temporary virtual environment and audits the lock with `--require-hashes --strict`. The audit tool itself is currently unpinned. |
| Isolated tests | Runs pytest with fake-provider settings, temporary SQLite storage, and a temporary test directory. Network sockets are disabled; Unix sockets are allowed. |
| Application startup | Runs `python -m tests.check_startup`, which starts Uvicorn with `tests.testing_app:create_test_app` on loopback and verifies liveness, readiness, and fake-provider chat using temporary storage and simulated identity. It stops the child process after the check. |

The job disables fallback and supplies no OpenAI credential. Dependency
installation and vulnerability auditing require network access; pytest's
socket restriction applies only to the test step. The separate startup
check uses local HTTP requests and does not call a live model provider.

Steps run sequentially. A failed installation, lint, audit, or test step
normally skips the remaining steps and fails the job. Ruff violations
are failures rather than advisory warnings.

Local verification on 2026-10-05 passed Ruff, all **82 pytest tests**,
the complete local smoke runner, and the Uvicorn startup check.
Verification used fake providers and temporary storage. Windows pytest
ran without CI's Linux socket-restriction flags. The startup check uses
the test factory with simulated identity; it does not verify production
authentication. No real-provider call or hosted GitHub Actions run was
verified in this review.

A clean Linux Python 3.12 container also installed the hashed lock, passed
`pip check` and Ruff, ran all 82 tests with CI's socket restrictions,
and passed the Uvicorn startup check. The dependency audit was not
rerun during this migration. A hosted GitHub Actions pass has not been
verified here.
Docker build and image checks, load tests, live-provider evaluations,
and automatic deployment are not included.

## Current boundaries

- Cost estimates are incomplete, based on outdated estimates of OpenAI models. The aim is to package this application with the lightest weight open-source model as a default fallback with extendability to more powerful models through Ollama and other popular providers.

- Production credential-to-tenant authentication remains to be connected.
- Concurrency and platform input limits are implemented; remaining tenant
  policies and configuration switches need runtime enforcement.
- Circuit-open API handling needs to preserve retryability after zero calls.
- Storage failure handling and retention need further hardening.
- Storage uses tenant/request composite keys; complete tenant isolation
  still depends on trusted identity and consistent boundaries.
- Automatic schema migration and incompatible-schema startup checks
  remain unimplemented.
- UTC log timestamps, bounded tenant metric labels, and active-generation
  metrics are implemented. Dedicated storage-failure metrics remain
  unfinished.
- Docker packaging and image checks are not yet present.
