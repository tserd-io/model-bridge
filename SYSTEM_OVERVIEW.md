# Model Bridge: Architecture and Behavior

Last reviewed against source: 2026-10-03.

Model Bridge is a Python gateway that exposes one API for calling LLM
providers. It selects models, controls generation attempts, records
request outcomes, and evaluates responses against a versioned dataset.

Use this overview as a starting map for diagnostics and code review.
Verify relevant implementation and tests before drawing conclusions;
this document describes the source at the review date, not a guarantee
of current behavior or a record of passing tests.

## Main components

| Module | Responsibility |
|---|---|
| `scripts/main.py` | Creates the FastAPI application, wires dependencies, and coordinates chat requests and health endpoints. |
| `scripts/schemas.py` | Defines validated request fields and the public response format. |
| `scripts/contracts.py` | Defines the provider interface, normalized generation result, and shared exceptions. |
| `scripts/providers.py` | Implements Ollama, OpenAI, and deterministic fake providers. |
| `scripts/generation.py` | Coordinates provider attempts, timeouts, retries, fallback, and circuit admission. |
| `scripts/circuit_breaker.py` | Tracks backend failures and controls recovery probes. |
| `scripts/rate_limit.py` | Enforces a sliding-window request limit. |
| `scripts/request_store.py` | Stores request payloads, processing states, and saved responses in SQLite. |
| `scripts/observability.py` | Produces structured logs and Prometheus metrics. |
| `scripts/load_settings.py` | Loads JSON configuration and environment-variable overrides. |
| `scripts/chat_service.py` | Contains routing, response-building, and completion-recording helpers. |

`scripts/chat_outcomes.py` and `scripts/chat_http_responses.py` introduce
an outcome object and HTTP converter for a future service extraction.
The active chat endpoint does not yet use them; the main workflow
remains in `scripts/main.py`.

## Request lifecycle

1. **Validate input.** FastAPI and Pydantic validate the request ID,
   message, model preference, task type, and output-token limit.
2. **Choose a route.** Simple tasks select `fast`; complex and high-risk
   tasks select `balanced`. Without a task type, the requested model
   preference is used.
3. **Apply the rate limit.** Rejected requests receive HTTP 429 and a
   `Retry-After` header before database or provider work begins.
4. **Claim the request.** SQLite checks the request ID and a hash of the
   validated payload. Existing requests may return a saved result,
   pending state, uncertain outcome, or conflict.
5. **Generate a response.** The generation runner checks circuit
   admission and invokes the provider with a bounded attempt timeout.
   Retryable failures may trigger fallback or backoff within the
   original generation deadline and attempt budget.
6. **Persist the outcome.** Successful responses are saved for replay.
   Failures are classified as definite or uncertain.
7. **Return and record.** The API returns a normalized response and
   records completion metrics and logs.

High-risk routing sets `human_review_required`; it does not implement
a human approval workflow.

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

Repeating the same request ID and payload after success returns the
stored response with `cache_hit=true` and an `X-Idempotent-Replay`
header. Reusing the ID with different input returns HTTP 409.

SQLite uses transactions to coordinate request claims. Processing
leases identify work that did not record a final result; expired
in-progress requests become `unknown` when checked again.

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

Rate limiting and circuit state are process-local. Multiple workers
have separate state. A rate limit also does not impose a maximum
number of simultaneous provider calls.

## Storage, logs, and health

SQLite stores request payloads and successful responses. JSON
operational logs record request IDs, routes, attempts, outcomes, and
latency rather than prompt and response content.

Prometheus metrics are exposed at `/metrics/`. They cover requests,
errors, retries, fallback, latency, cache hits, and available usage
and cost estimates.

| Endpoint | Meaning |
|---|---|
| `GET /health/live` | The application can respond. |
| `GET /health/ready` | The existing requests table can be read. |

Readiness opens SQLite read-only, so it does not create a missing
database. It does not verify database writes or provider availability.

## Evaluation pipeline

Evaluation is split into three modules under `scripts/evaluations/`:

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
python -m scripts.evaluations.eval_runner
```

The CLI creates its own local API client and uses the configured
provider. Programmatic callers supply the client and manage its lifetime.

## What the tests demonstrate

Test files are under `scripts/tests/`.

| Test file | Coverage |
|---|---|
| `test_gateway.py` | Response normalization, saved-response replay, conflicting IDs, routing, retries, fallback, unknown outcomes, rate-limit headers, concurrent limiter admission, shared circuit state, and evaluation output. |
| `test_circuit_breaker.py` | Failure thresholds, success resets, cooldown, single recovery-probe admission, stale completion handling, and duplicate permit completion. |
| `test_generation_circuit.py` | Circuit integration with generation, fallback, attempt counting, uncertainty, deadlines, cancellation, and error classification. |
| `test_health.py` | Liveness independence, readable storage, missing/corrupt/schema-less databases, and recovery after storage is restored. |
| `test_evaluation.py` | Aggregation of saved results, including mixed outcomes and incomplete cost information. |
| `smoke_test.py` | Manual checks for adapter behavior, tracing headers, retries, timeouts, deadlines, and idempotency. |
| `test_api.py` | An entry point for the manual smoke runner; it defines no pytest test cases. |

Automated tests use fake providers, controlled clocks, mocks, and
temporary databases to check gateway behavior without requiring
live model responses. Set the fake provider and a temporary
`IDEMPOTENCY_DB_PATH` before importing the application, which constructs
its configured providers and request store at import time.

These tests verify selected behaviors. They do not establish model
quality, production load capacity, tenant isolation, or complete
coverage of every external SDK failure.

Functions named `smoke_*` are not automatically collected by pytest.
The smoke entry point also sends an initial request through the
configured provider, so it is separate from the isolated unit and
integration suite.

## Continuous integration

`.github/workflows/ci.yaml` defines checks for pushes and pull requests.
It uses Python 3.12 on Ubuntu, installs hash-locked dependencies from
`scripts/requirements-ci.lock`, runs `pip check` and Ruff, and runs
pytest with fake providers and temporary storage. The test command
disables network sockets while allowing Unix sockets.

At this review, `scripts/requirements-ci.lock` is absent, so the workflow's
dependency-install step cannot complete until that file is supplied or
the installation configuration is changed. The workflow's presence is
not evidence that a hosted CI run has passed. It does not currently build
a Docker image.

## Current boundaries

- The chat-service extraction is incomplete.
- Concurrency caps and maximum input-size enforcement remain to be added.
- Storage failure handling and retention need further hardening.
- Cost estimates are not a complete provider billing ledger.
- Tenant IDs are caller-supplied metadata, not authenticated identities.
- Authentication and enforced tenant isolation are not implemented.
- Docker packaging and image checks are not yet present.
