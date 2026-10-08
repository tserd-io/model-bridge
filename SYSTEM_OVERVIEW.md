# Model Bridge: Architecture and Behavior

Runtime hardening documentation updated: 2026-10-07, based on the confirmed
deliverables in [HARDENING_PROPOSAL.md](./HARDENING_PROPOSAL.md).
Project scope and earlier Docker/CI review: 2026-10-06.

Model Bridge provides one API for calling LLM providers. It chooses models,
limits work, saves request results, and measures response quality. This
overview describes its current behavior and remaining work.

## Project scope and review guidance

Model Bridge demonstrates LLM routing, provider abstraction, bounded execution,
request tracking, evaluation, and portable application packaging. It is not
currently pursuing a full production database service or a complete
authentication product.

SQLite is a temporary reference implementation used to demonstrate and test
request states, duplicate handling, saved responses, and tenant-scoped keys.
Demonstration data is disposable; durability across container replacement is
not a project requirement. Keep existing functional storage tests, but do not
expand the roadmap into SQLite-specific resilience, persistent-volume testing,
migrations, backup/restore, retention operations, or exhaustive database failure
testing. A durable storage implementation is an optional extension and is not
being advanced within the current scope.

Authentication is also an optional extension that is not currently being
pursued. Tenant identity remains useful for demonstrating policy, capacity,
and request separation. The identity dependency is an integration point for a
future authenticator, while controlled tests supply simulated identities.
Building credential verification, login, key management, or an identity
provider is not a current deliverable.

Future reviews should respect these decisions: do not repeatedly list absent
production authentication or advanced database capabilities as defects,
deployment blockers, or recommended next improvements unless the project owner
explicitly expands the scope. Continue to identify regressions in the promised
functional behavior, such as incorrect replay, cross-tenant result mixing,
broken routing, or ineffective execution limits. Document actual limitations
accurately without converting optional extensions into an active backlog.

## Source map

Application code is grouped by responsibility. Tests and supporting commands
live in `tests/`, separate from the `model_bridge` package. Dependency files
live at the repository root. Package `__init__.py` files, virtual environments,
caches, and local SQLite files are omitted from this map.

```text
model-bridge/
├─ model_bridge/
│  ├─ main.py                     # Builds the app and starts the configured server
│  ├─ api/
│  │  ├─ chat.py                  # Chat HTTP route
│  │  ├─ health.py                # Liveness and readiness routes
│  │  ├─ dependencies.py          # Supplies tenant identity and chat service
│  │  ├─ schemas.py               # Per-app field limits and UTF-8 validation
│  │  └─ responses.py             # Turns service results into HTTP responses
│  ├─ application/
│  │  ├─ chat_service.py          # Coordinates the chat request
│  │  ├─ request_policy.py        # HTTP-independent input and tenant policy checks
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
│  ├─ test_circuit_breaker.py      # Breaker states, API retryability and provider settings
│  ├─ test_concurrency_limits.py   # Capacity limits and retries after rejection
│  ├─ test_evaluation.py          # Evaluation summaries and files
│  ├─ test_gateway.py             # Routing, retries and stored results
│  ├─ test_generation_circuit.py  # Breakers, deadlines and provider timeouts
│  ├─ test_health.py              # Liveness and database readiness
│  ├─ test_input_limits.py        # Tenant/platform input, token and Unicode limits
│  ├─ test_runtime_settings.py    # Logging, leases, app settings and shutdown
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
├─ Dockerfile                    # Non-root image and readiness health check
├─ pytest.ini
├─ README.md
└─ SYSTEM_OVERVIEW.md
```


## How the parts fit together

`main.py` builds the FastAPI app and a `ChatService`. The API checks input
and identity, then passes a request to the service. The service chooses a
model, applies limits, calls providers, and saves the result. It returns
plain data; `api/responses.py` adds the HTTP status and headers.

[request_policy.py](./model_bridge/application/request_policy.py) performs
deterministic input checks without HTTP, database, or provider dependencies.
The service applies these checks even for callers that bypass HTTP. The HTTP
adapter measures body bytes and passes that count separately to the service;
transport metadata does not enter the request command or its replay hash.

HTTP schemas are built from each application's settings. When an existing
service is injected, the app derives settings from it; explicitly supplying
different settings is rejected rather than mixing validation and runtime policy.

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

1. **Check input and identity.** Validate HTTP fields and UTF-8 using the app's
   configured limits. Use the trusted tenant identity instead of the body's
   tenant ID.
2. **Choose a model.** Simple tasks use `fast`; complex and high-risk tasks
   use `balanced`. Otherwise, use the requested model preference. The service
   checks effective token, message, and input-byte limits before admission or
   storage, including for direct callers. Oversized tokens are rejected, not clamped.
3. **Check the rate limit.** Admit against tenant and platform quotas atomically.
   Too many requests receive HTTP 429 with `Retry-After`, before database or
   provider work; rejection spends neither quota.
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
This is an intentional extension boundary, not an active authentication task.

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

Runtime settings now control the following behavior:

| Setting | Current behavior |
|---|---|
| Tenant and platform concurrency | Enforced during generation. |
| Tenant and platform input bytes | Actual HTTP body bytes and normalized caller-input bytes are checked independently; oversize returns 413. Trusted identity is excluded from normalized input. |
| Platform message length and tenant/platform output tokens | Per-app HTTP schemas and independent service checks enforce effective ceilings; invalid input returns 422. |
| Tenant and platform deadlines | Generation uses the effective tenant budget bounded by the platform deadline; this does not bound the whole HTTP request or storage work. |
| Tenant and platform rate limits | One admission decision checks and spends both quotas, each with its own window. |
| Metrics enabled and tenant labels | Applied; tenant labels are limited to an allowlist. |
| Logging enabled and minimum level | Applied to the process-wide application logger. |
| Shutdown grace | Passed to the one-worker server by the configured module entry point. Real Linux signal behavior still awaits verification. |
| Storage busy timeout and lease margin | Write connections use the configured timeout; claims use the effective generation budget plus the configured margin. |

Admitted requests spend rate quota even when they later replay a saved response,
conflict with an existing request, or fail at the provider. Expired tenant
histories are cleaned under the admission lock on the next admission, not by a
background worker. Changing a tenant's rate policy while its history is active
is rejected; settings are startup configuration, not a hot-reload mechanism.

## Stored requests and duplicate handling

Request keys combine **tenant ID and request ID**, so two tenants can use
the same request ID without a collision. Within one tenant:

- Repeating a successful request with the same validated input returns its
  saved response, with `cache_hit=true` and `X-Idempotent-Replay: true`.
- Reusing that ID with different input returns HTTP 409.
- A matching request still in progress returns HTTP 202 with `Retry-After: 1`.
- Capacity rejection saves a `retryable` state that a matching request can
  claim again.
- `unknown` means the provider may have completed the work. Repeating the
  request does not automatically generate again.

This duplicate handling is called **idempotency**. It does not guarantee
that an external provider runs each request exactly once.

Service outcomes retain retry timing, error classification, and timeout flags
for direct callers as well as HTTP response conversion. Regression tests cover
service-produced in-progress responses and error/timeout metadata.

SQLite transactions coordinate request claims. Each running request has a
time limit for recording its result, called a processing lease. It is derived
from the tenant's effective generation budget plus the configured lease margin.
If that lease expires, the request becomes `unknown` when checked again.

Startup creates missing databases and tables. Existing incompatible tables
are neither migrated nor rejected at startup. The reference implementation
assumes a fresh compatible database for demonstrations; it does not promise
production recovery, retention, or successful persistence after disk failures.
Failures while saving an already-generated response remain a limitation of
this placeholder. Advancing these database-specific capabilities is outside
the current scope, rather than required follow-up work.

## Retries, fallback, and the circuit breaker

Generation separates temporary failures, definite rejections, and uncertain
results. A timeout does not prove that remote work stopped; retrying or
falling back after an uncertain result can duplicate that work.

Generation creates one monotonic deadline at its start. Primary attempts,
retry delays, and fallback share that deadline and the primary retry settings;
fallback does not get a new attempt budget. Each attempt uses the active
provider's own timeout, capped by the remaining generation time. The gateway
also wraps the call in a timeout so adapters cannot bypass it merely by ignoring
their timeout argument. Cancellation still requires a cooperative async adapter.

Remaining time is checked again after breaker admission and synchronous logging.
Expiry before a provider call releases the unused probe without recording a
provider attempt, while preserving earlier attempts and any uncertain outcome.

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

Each configured provider supplies its own circuit-breaker failure threshold
and cooldown. Primary and fallback share one breaker when they use the same
configured provider name.

A circuit rejection before any provider call returns HTTP 503 with
`Retry-After` and saves the request as `retryable`. Repeating the same ID and
input can reclaim that record after cooldown. Different input still returns
HTTP 409. Requests with uncertain earlier attempts remain `unknown` and are
not automatically generated again.

## Logs, metrics, and health

SQLite stores request input and successful responses. JSON logs include UTC
timestamps to the millisecond, request IDs, routes, attempts, outcomes, and
latency. Request and provider-call events include tenant identity. Logs omit
prompt and response content.

Application composition applies logging enablement and minimum severity.
Events use explicit severity levels and operational metadata, excluding
credentials and exception text as well as prompt/generated content. The logging
helper is not a general-purpose redactor: callers must supply permitted fields.
This logger is process-wide, so multiple apps in one process cannot maintain
independent logging policies. Uvicorn's own logging is configured separately.

When enabled, `/metrics/` reports requests, errors, retries, fallback,
latency, saved-response reuse, available token usage, and estimated cost.
`llm_active_generations` counts jobs holding generation capacity in the
current process, including retry delays. It decreases when work completes,
fails, or is cancelled. Disabling metrics stops recording and removes the
endpoint. Storage-failure metrics are not implemented and are not part of the
current database demonstration scope.

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
Its read timeout remains one second, independent of the configured write timeout.
It does not check the full table structure, database writes, or provider
availability. An incompatible table can pass readiness while chat fails.
This is a basic demonstration health check, not a database durability or
schema-compatibility guarantee; expanding it into those checks is not planned.

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
Preserve this limitation in usage guidance. Making controlled evaluations
usable need not introduce production authentication; that extension remains
outside scope.

## Tests and manual checks

Tests in `tests/` cover routing, retries, fallback, stored responses, tenant
keys, circuit breakers, rate and concurrency limits, input limits, health,
HTTP responses, and evaluation summaries. The source map identifies each file.

Hardening regressions additionally cover custom app limits, direct-service
policy checks, normalized versus HTTP input bytes, invalid Unicode, atomic rate
admission, deadline expiry before provider work, fallback-specific timeouts,
logging settings, processing leases, and service-produced outcome metadata.
Startup wiring is tested separately from real signal handling; two real SIGTERM
shutdown tests require Linux and skip on Windows.

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
on Ubuntu 24.04. Both jobs have a ten-minute timeout and read-only repository
permissions. Steps run in order within each job; the application checks and
Docker smoke job can run independently.

| Step | Behavior |
|---|---|
| Install dependencies | Install exact versions from `requirements-ci.lock`, verify download hashes, and run `pip check`. |
| Ruff lint | Check `model_bridge` and `tests` for syntax errors, undefined names, unused imports, and related issues using `.github/workflows/ruff.toml`. |
| Security audit | Run `pip-audit --require-hashes --strict` on the lock in a separate environment. The audit tool itself is unpinned. |
| Tests | Use fake providers and temporary storage. Block network sockets; allow Unix sockets. |
| Startup | Run `python -m tests.check_startup` against a local server. |

The separate `docker-smoke` job builds the image, starts it with a fake
provider and no fallback, and waits for its Docker readiness health check.
It reports logs and container state on failure and removes the container on
completion. The Dockerfile uses a non-root user, writable `/data`, and one
Uvicorn worker. It currently installs the CI lock, including development tools.

The image starts with `python -m model_bridge.main`. This entry point passes
configured graceful-shutdown seconds to Uvicorn and avoids constructing the app
twice. The importable `model_bridge.main:app` remains available, but launching it
directly with Uvicorn requires configuring Uvicorn's shutdown grace separately.

Fallback is disabled and no OpenAI credential is supplied. Installation and
auditing need network access; the socket restriction applies only to pytest.
Lint failures fail the job rather than producing advisory warnings.

Dependency files live at the repository root. Windows development uses
`python -m pip install -r requirements-dev.txt`. The CI lock targets Linux
Python 3.12 and contains `uvloop`, which cannot be installed on Windows.

The 2026-10-07 hardening review records **199 passed, 2 skipped** in the full
Windows suite before four new outcome-metadata cases were added. The subsequent
focused service/HTTP run passed **19 tests**, including all four additions; the
full suite was not rerun for that addition. Ruff, fake-provider smoke checks,
and isolated real Uvicorn startup also passed. These results do not verify a
Docker image or the two Linux-only shutdown tests. See
[the hardening verification record](./HARDENING_PROPOSAL.md#current-verification)
for the distinction between current and historical runs.

Earlier, on 2026-10-06, local checks passed Ruff and **86 tests**, including four new
circuit cases covering retryable storage, recovery with the same request ID,
preserved uncertainty, and provider-specific breaker settings. These checks
used fake providers and temporary storage; no application code changed in
this follow-up.

On 2026-10-05, local checks passed Ruff, **82 tests**, smoke checks, startup,
and `pip check`. Windows pytest used a fresh workspace temporary directory
after the default temporary directory caused a permissions error; it ran
without Linux socket restrictions. Earlier migration checks in a clean Linux
Python 3.12 container also passed locked installation, `pip check`, Ruff,
82 socket-restricted tests, and startup.

The security audit was not rerun during the migration. No hosted GitHub
Actions run or real-provider call was verified here. Docker build and health
checks are now configured; this documentation update does not establish that
their hosted run passed. CI does not scan the built image, run load tests or
live-model evaluations, or deploy the application.

## Docker priorities within the current scope

| Area | Priority and scope |
|---|---|
| Image build, non-root startup, basic health | Keep the existing functional baseline. |
| `.dockerignore` | Small, useful next addition to exclude local environments, data, credentials, and Git history from the build context. |
| HTTP through a published port | Useful next check that the packaged API is reachable through container networking. No production authentication is required to check health endpoints. |
| Image vulnerability scan | Useful packaging check covering OS and application dependencies; add as CI maturity work. |
| Basic shutdown | Optional bounded stop check; durable request draining and database recovery testing are outside scope. |
| Runtime-only dependency lock | Optional image-size and dependency cleanup, not a functional blocker. |
| Persistent volumes and database survival after replacement | Outside scope for disposable SQLite demonstration data. Do not add as a required check. |
| Production authentication inside the container | Optional extension, not an image acceptance requirement. |

Keep Docker checks focused on packaging and basic operation. Extensive storage
testing and full credential flows would change the project's scope rather
than complete the current Docker work.

## Remaining work and future direction

- Complete Linux shutdown and container verification for the implemented
  hardening changes; Windows checks do not establish these results.
- Preserve demonstrated tenant separation and duplicate handling when changing
  existing features; this does not require a production identity system.
- Review model pricing; cost estimates are incomplete and need updating.
- Improve packaging checks according to the scoped Docker priorities above.

Durable storage and production authentication remain documented extension
points, not work being pursued. A future storage adapter could replace the
SQLite request store; a future authenticator could supply trusted tenant
identity through the existing dependency. Neither extension is required to
complete the current functional demonstration.

The intended future fallback is a lightweight open-source model, with support
for stronger models through Ollama and other providers. This is a future goal;
the current default still uses the same Ollama backend for primary and fallback.
