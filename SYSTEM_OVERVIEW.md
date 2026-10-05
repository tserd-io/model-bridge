# Model Bridge: Architecture and Behavior

Last reviewed against source: 2026-10-05.

Model Bridge provides one API for calling LLM providers. It chooses models,
limits work, saves request results, and measures response quality. This
overview describes its current behavior and remaining work.

## Source map

Application code is grouped by responsibility. Tests and supporting commands
live in `tests/`, separate from the `model_bridge` package. Dependency files
live at the repository root. Package `__init__.py` files, virtual environments,
caches, and local SQLite files are omitted from this map.

```text
model-bridge/
├─ model_bridge/
│  ├─ main.py                     # Builds the service and FastAPI app
│  ├─ api/
│  │  ├─ chat.py                  # Chat HTTP route
│  │  ├─ health.py                # Liveness and readiness routes
│  │  ├─ dependencies.py          # Supplies tenant identity and chat service
│  │  ├─ schemas.py               # Checks request and response fields
│  │  └─ responses.py             # Turns service results into HTTP responses
│  ├─ application/
│  │  ├─ chat_service.py          # Coordinates the chat request
│  │  └─ outcomes.py              # Request and result data classes
│  ├─ providers/
│  │  ├─ contracts.py             # Shared provider interface and errors
│  │  ├─ factory.py               # Creates providers from settings
│  │  ├─ ollama.py
│  │  ├─ openai.py
│  │  ├─ fake.py
│  │  ├─ pricing.py               # Token-cost calculation
│  │  └─ transport.py             # Timeouts and retryable error checks
│  ├─ execution/
│  │  ├─ generation.py            # Retries, timeouts, fallback and breaker checks
│  │  ├─ circuit_breaker.py
│  │  ├─ rate_limit.py
│  │  └─ concurrency_limit.py
│  ├─ storage/
│  │  └─ request_store.py         # Saves requests and returns stored results
│  ├─ config/
│  │  ├─ loader.py                # JSON loading and environment overrides
│  │  ├─ models.py                # Validated settings
│  │  └─ config.json
│  ├─ observability/
│  │  ├─ logging.py               # Timestamped JSON events
│  │  ├─ metrics.py               # Counts requests, latency and active work
│  │  └─ labels.py                # Limits tenant labels in metrics
│  └─ evaluations/
│     ├─ README.md                # Evaluation usage and dataset format
│     ├─ eval_runner.py
│     ├─ eval_summary.py
│     ├─ eval_writer.py
│     └─ datasets/
│        └─ golden_dataset.json
├─ tests/
│  ├─ conftest.py                 # Shared test setup and temporary services
│  ├─ testing_app.py              # Test app with fake provider and identity
│  ├─ check_startup.py            # Starts and checks a Uvicorn server
│  ├─ smoke_test.py               # Manual local checks; --live opts into a provider call
│  ├─ test_chat_responses.py      # Response codes, data and headers
│  ├─ test_chat_service.py        # Service calls and saved results per tenant
│  ├─ test_circuit_breaker.py      # Breaker opening, cooldown and recovery
│  ├─ test_concurrency_limits.py   # Capacity limits and retries after rejection
│  ├─ test_evaluation.py          # Evaluation summaries and files
│  ├─ test_gateway.py             # Routing, retries and stored results
│  ├─ test_generation_circuit.py  # Breaker checks around provider calls
│  ├─ test_health.py              # Liveness and database readiness
│  ├─ test_input_limits.py        # Message length and HTTP body limits
│  └─ test_tenant_concurrency.py  # Tenant identity and capacity limits
├─ requirements.txt              # Application dependencies
├─ requirements-dev.txt          # Development tools and application dependencies
├─ requirements-ci.lock          # Exact versions and download hashes for CI
├─ data/                          # Database and evaluation results; ignored by Git
├─ .github/
│  └─ workflows/
│     ├─ ci.yaml                  # Installation, lint, security and test checks
│     └─ ruff.toml                # CI lint rules
├─ .gitignore
├─ pytest.ini
├─ README.md
└─ SYSTEM_OVERVIEW.md
```


## How the parts fit together

`main.py` builds the FastAPI app and a `ChatService`. The API checks input
and identity, then passes a request to the service. The service chooses a
model, applies limits, calls providers, and saves the result. It returns
plain data; `api/responses.py` adds the HTTP status and headers.

Providers share an asynchronous `complete()` interface that returns a
`GenerationResult`. Each adapter translates its provider's responses and
errors into these shared types:

- **Ollama:** local models and reported token usage.
- **OpenAI:** hosted models, request-tracing headers, token usage, and
  estimated costs when pricing is configured. SDK retries are disabled
  so the gateway controls the number of attempts.
- **Fake:** predictable responses without external model calls.

`config/` loads JSON settings and environment overrides. Relative storage
paths resolve from the repository root. Tenant settings inherit defaults
and cannot exceed configured platform limits. Unknown tenants use defaults;
the settings file does not determine who is allowed to access the API.

## What happens to a chat request

1. **Check input and identity.** Enforce request-body and message-length
   limits. Use the trusted tenant identity instead of the body's tenant ID.
2. **Choose a model.** Simple tasks use `fast`; complex and high-risk tasks
   use `balanced`. Otherwise, use the requested model preference.
3. **Check the rate limit.** Too many requests receive HTTP 429 with
   `Retry-After`, before database or provider work.
4. **Check stored requests.** Look up `(tenant_id, request_id)` and compare
   the validated input. Return a saved result, existing state, or conflict
   when appropriate.
5. **Check generation capacity.** New work must fit both the tenant and
   process limits. A full limit returns HTTP 503 with `Retry-After: 1`.
   Saved responses skip this check but still pass the rate limit.
6. **Call the provider.** Check the circuit breaker and apply timeouts.
   Retries and fallback share the original deadline and attempt limit.
7. **Save and return the result.** Store successful responses for later
   reuse, record failures or uncertain results, and emit logs and metrics.

High-risk routing sets `human_review_required`; there is no human approval
workflow yet.

## Tenant identity and limits

A tenant represents a caller or customer whose requests should be kept
separate. Chat requires an `AuthenticatedTenant` in
`request.state.authenticated_tenant`. Authentication is deliberately left
open-ended: the production app does not establish this identity, so valid
chat requests currently receive HTTP 401 unless trusted identity is supplied.
A tenant ID in the request body cannot establish identity.

Tests supply identities through dependency overrides.
`tests.testing_app:create_test_app` supplies a test identity and requires a
fake provider, no fallback, and an explicit storage path.

The concurrency limiter checks tenant and process capacity together and
rejects excess work immediately. It releases capacity after success,
failure, or cancellation, and removes unused tenant counters. A slot covers
provider attempts and retry delays, but not input parsing or database work.

Rate limits, concurrency counts, and circuit breakers are held in each
process. Multiple workers or replicas have separate limits and state.
These limits reduce memory pressure; they do not set a hard tenant memory
budget or limit the amount of data retained in SQLite.

Some settings are validated but not yet used:

| Setting | Current behavior |
|---|---|
| Tenant and platform concurrency | Enforced during generation. |
| Platform body bytes and message length | Enforced before generation. |
| Platform deadline and rate limit | Applied by the chat service. |
| Tenant body bytes, output tokens, deadline, and rate limit | Validated, but not enforced by the chat service. |
| Platform output-token limit | The request schema uses a fixed maximum of 8192. |
| Metrics enabled and tenant labels | Applied; tenant labels are limited to an allowlist. |
| Logging switches and shutdown grace | Not connected to runtime behavior. |
| Storage busy timeout and lease margin | Still use fixed five-second values. |

## Stored requests and duplicate handling

Request keys combine **tenant ID and request ID**, so two tenants can use
the same request ID without a collision. Within one tenant:

- Repeating a successful request with the same validated input returns its
  saved response, with `cache_hit=true` and `X-Idempotent-Replay: true`.
- Reusing that ID with different input returns HTTP 409.
- Capacity rejection saves a `retryable` state that a matching request can
  claim again.
- `unknown` means the provider may have completed the work. Repeating the
  request does not automatically generate again.

This duplicate handling is called **idempotency**. It does not guarantee
that an external provider runs each request exactly once.

SQLite transactions coordinate request claims. Each running request has a
time limit for recording its result, called a processing lease. If that
lease expires, the request becomes `unknown` when checked again.

Startup creates missing databases and tables. The prototype database was
reset and the tenant/request primary key verified. Existing incompatible
tables are neither migrated nor rejected at startup. Data cleanup and
consistent handling of database failures remain unfinished, including
failures while saving an already-generated response.

## Retries, fallback, and the circuit breaker

Generation separates temporary failures, definite rejections, and uncertain
results. A timeout does not prove that remote work stopped; retrying or
falling back after an uncertain result can duplicate that work.

The default primary and fallback both use the same Ollama host and model
mapping. A fallback that can survive that backend failing needs a different
configuration.

The circuit breaker stops repeatedly calling a failing backend:

| State | Behavior |
|---|---|
| Closed | Allow calls and count qualifying failures. |
| Open | Reject calls during a cooldown. |
| Half-open | Allow one call to check whether the backend recovered. |

A successful recovery call closes the breaker. A failed or cancelled one
starts another cooldown. Old call results cannot overwrite newer breaker
state. Primary and fallback share a breaker when they use the same configured
backend; rejected calls do not count as provider attempts.

One API integration issue remains: `CircuitUnavailableError` uses the generic
retryable-error handler. A rejection before any provider call is saved as a
final failure, and its retry delay is not passed to the client.

## Logs, metrics, and health

SQLite stores request input and successful responses. JSON logs include UTC
timestamps to the millisecond, request IDs, routes, attempts, outcomes, and
latency. Request and provider-call events include tenant identity. Logs omit
prompt and response content.

When enabled, `/metrics/` reports requests, errors, retries, fallback,
latency, saved-response reuse, available token usage, and estimated cost.
`llm_active_generations` counts jobs holding generation capacity in the
current process, including retry delays. It decreases when work completes,
fails, or is cancelled. Disabling metrics stops recording and removes the
endpoint. Storage-failure metrics are not implemented.

Tenant labels are disabled by default, so all tenants use `all`. When
enabled, up to 100 configured tenants get individual labels; everyone else
uses `other`. Each distinct label adds metric series, which consume memory.
The allowlist prevents unbounded growth as new tenants appear.

Token and cost estimates depend on available provider data and pricing;
they are not a complete record of all remote work or charges.

| Endpoint | What it checks |
|---|---|
| `GET /health/live` | The application can respond. |
| `GET /health/ready` | The existing SQLite requests table can be read. |

Readiness opens SQLite read-only and does not create a missing database.
It does not check the full table structure, database writes, or provider
availability. An incompatible table can pass readiness while chat fails.

## Evaluations

Evaluations use a versioned dataset to measure response quality and performance.
The work is split under `model_bridge/evaluations/`:

| Module | Responsibility |
|---|---|
| `eval_runner.py` | Read prompts, send requests through a supplied client, and score answers. |
| `eval_summary.py` | Summarize scores, latency, errors, usage, and available cost. |
| `eval_writer.py` | Save per-case JSONL and summary JSON files. |

Scoring checks allowed answers, required answer points, or expected JSON
fields. Dataset and prompt versions identify what was evaluated. Checks for
source-grounded answers and tool use remain placeholders because the gateway
does not retrieve source material or execute tools.

Run from the repository root:

```sh
python -m model_bridge.evaluations.eval_runner
```

The CLI creates a local API client and writes results to
`data/evaluation_runs/`. It currently supplies no trusted tenant identity,
so chat calls receive 401. Evaluation tests supply a test identity;
programmatic callers supply and manage their own client.

## Tests and manual checks

Tests in `tests/` cover routing, retries, fallback, stored responses, tenant
keys, circuit breakers, rate and concurrency limits, input limits, health,
HTTP responses, and evaluation summaries. The source map identifies each file.

They use fake providers, controlled clocks, mocks, and temporary databases.
`conftest.py` supplies a default test identity; identity-specific tests replace
it. Importing the application still constructs its configured providers and
store, so use fake-provider settings and a temporary `IDEMPOTENCY_DB_PATH`
before importing it for isolated checks.

Run from the repository root:

```sh
python -m pytest tests
python -m tests.smoke_test
python -m tests.check_startup
```

The smoke runner checks provider adapters, request-tracing headers, retries,
timeouts, deadlines, and duplicate handling. Its `smoke_*` functions are not
run by pytest. It uses simulated providers by default; `--live` also calls
the configured primary provider with fallback disabled.

The startup check starts a real Uvicorn server with a fake provider, temporary
storage, and test identity. It checks both health endpoints and chat, then
stops the server.

These checks cover selected behavior, not real-model quality, production
capacity, every provider failure, or complete tenant isolation.

## Continuous integration

`.github/workflows/ci.yaml` runs on pushes and pull requests using Python 3.12
on Ubuntu 24.04. The job has a ten-minute timeout and read-only repository
permissions. Steps run in order; a failure stops the later steps.

| Step | Behavior |
|---|---|
| Install dependencies | Install exact versions from `requirements-ci.lock`, verify download hashes, and run `pip check`. |
| Ruff lint | Check `model_bridge` and `tests` for syntax errors, undefined names, unused imports, and related issues using `.github/workflows/ruff.toml`. |
| Security audit | Run `pip-audit --require-hashes --strict` on the lock in a separate environment. The audit tool itself is unpinned. |
| Tests | Use fake providers and temporary storage. Block network sockets; allow Unix sockets. |
| Startup | Run `python -m tests.check_startup` against a local server. |

Fallback is disabled and no OpenAI credential is supplied. Installation and
auditing need network access; the socket restriction applies only to pytest.
Lint failures fail the job rather than producing advisory warnings.

Dependency files live at the repository root. Windows development uses
`python -m pip install -r requirements-dev.txt`. The CI lock targets Linux
Python 3.12 and contains `uvloop`, which cannot be installed on Windows.

On 2026-10-05, local checks passed Ruff, **82 tests**, smoke checks, startup,
and `pip check`. Windows pytest used a fresh workspace temporary directory
after the default temporary directory caused a permissions error; it ran
without Linux socket restrictions. Earlier migration checks in a clean Linux
Python 3.12 container also passed locked installation, `pip check`, Ruff,
82 socket-restricted tests, and startup.

The security audit was not rerun during the migration. No hosted GitHub
Actions run or real-provider call was verified here. CI does not build Docker
images, run load tests or live-model evaluations, or deploy the application.

## Remaining work and future direction

- Connect trusted credentials to tenant identity when production authentication
  is needed; keep simulated identity confined to test entry points.
- Apply the remaining tenant policies and unused configuration switches.
- Make circuit-open API responses retryable when no provider call occurred.
- Improve database failure handling, add storage-failure metrics, and define
  data cleanup and incompatible-schema handling.
- Verify tenant isolation across identity, storage, and future features.
- Review model pricing; cost estimates are incomplete and need updating.
- Add Docker packaging and image checks.

The intended future fallback is a lightweight open-source model, with support
for stronger models through Ollama and other providers. This is a future goal;
the current default still uses the same Ollama backend for primary and fallback.
