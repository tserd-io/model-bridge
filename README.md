# Model Bridge

Model Bridge is a minimal Python API that gives applications a single endpoint for calling large language models. Built with FastAPI, it routes requests to a configured provider, records request outcomes, and includes an evaluation process for measuring response quality and performance.

## How it works

1. An application sends a message to `POST /chat` with a unique request ID, model preference, and output token limit.
2. Model Bridge selects a model from the configured provider. An optional task type overrides the preference: simple tasks use `fast`; complex and high-risk tasks use `balanced`.
3. The gateway checks input, token, rate, and concurrency limits before generation. Provider calls use bounded retries and optional fallback within one shared generation deadline.
4. The API returns a consistent response containing the outcome, generated content, provider, model, latency, attempt count, and available usage information.

Supported providers are **Ollama** for local models and **OpenAI** for hosted models. Provider selection, model mappings, timeouts, and pricing are configured in [`model_bridge/config/config.json`](model_bridge/config/config.json), with environment-variable overrides.

Chat requires a trusted tenant identity supplied by an integration. The production app does not establish that identity itself, so otherwise-valid chat requests receive **401** without it. A tenant ID in the request body does not authenticate the caller. Tests supply controlled identities; production authentication is an optional extension, not a current deliverable.

## Runtime policies

- **Input and output limits:** enforce tenant/platform byte and output-token ceilings, platform message length, and valid UTF-8. Oversized input returns 413; invalid fields or excessive output-token requests return 422. Token requests are rejected rather than silently reduced.
- **Rate limits:** check tenant and platform quotas atomically. Rejection returns 429 with `Retry-After` and spends neither quota. Admitted requests count even if they later replay a response, conflict, or fail.
- **Concurrency:** check tenant and process generation capacity together. Capacity rejection returns 503 with `Retry-After: 1`; capacity is released after success, failure, or cancellation.
- **Generation budgets:** retries and fallback share the original deadline and attempt budget. Each provider uses its own attempt timeout, capped by remaining time. This bounds generation, not the entire HTTP request or database work; async adapters must cooperate with cancellation.
- **Circuit breakers:** temporarily stop calls to failing providers and permit recovery probes after cooldown.

HTTP schemas use the application's configured limits. Independent service-level policy checks also protect callers that bypass HTTP. Rate limits, concurrency counts, and circuit breakers are process-local, not shared across workers or replicas. The default primary and fallback use the same Ollama backend, so fallback does not provide an independent backend by default.

## Request records and observability

SQLite stores request payloads, processing states, and successful responses. Within one tenant, repeating a completed request with the same ID and payload returns the saved response; reusing an ID with different input is rejected. Uncertain provider outcomes are recorded as `unknown`.

A matching request still in progress returns 202 with `Retry-After: 1`. Service outcomes preserve retry timing, error classification, and timeout flags for direct callers. Processing leases use the effective generation budget plus the configured margin; write connections use the configured SQLite busy timeout.

Structured JSON logs track request IDs, provider attempts, latency, and outcomes. Prompt and response content is stored in SQLite rather than included in those operational logs. Logging enablement and minimum severity are configurable for the process-wide application logger. When enabled, Prometheus metrics are available at `/metrics/`, including request counts, errors, retries, token usage, and estimated costs where pricing is configured.

SQLite is a disposable reference store for demonstrating request states and tenant-scoped replay. Durable storage, migrations, and production recovery are outside the current scope.

## Running locally

Install application dependencies from the repository root:

```sh
python -m pip install -r requirements.txt
```

Start the API from the repository root:

```sh
python -m model_bridge.main
```

This starts one Uvicorn worker on port 8000 and applies the configured graceful-shutdown period. The importable `model_bridge.main:app` remains available for other launchers, which must configure their own server shutdown grace.

Health endpoints report application liveness at `/health/live` and database read availability at `/health/ready`. Readiness uses an independent one-second SQLite read timeout; it does not check provider availability or database writes. Local Ollama use requires a running Ollama server with the configured models available. The application does not bundle a model.

The [Dockerfile](./Dockerfile) uses the same configured startup entry point. A container needs a reachable provider address: its `localhost` refers to the container, not the host machine. Linux shutdown and container verification remain outstanding in the [hardening checklist](./HARDENING_PROPOSAL.md#deliverables-review).

## Evaluation

The evaluation runner sends a versioned set of prompts through the gateway and compares responses with expected answers, answer points, or JSON fields. It saves per-case results and a summary covering response quality, latency, availability, token usage, and available cost estimates.

The CLI entry point is:

```sh
python -m model_bridge.evaluations.eval_runner
```

Prompts are defined in [`model_bridge/evaluations/datasets/golden_dataset.json`](model_bridge/evaluations/datasets/golden_dataset.json). Results are written to `data/evaluation_runs/`.

The CLI currently supplies no trusted tenant identity, so its chat calls receive 401 rather than generating model answers. Evaluation tests use a controlled identity; programmatic callers can supply an appropriately configured client. See the [evaluation guide](./model_bridge/evaluations/README.md) for the dataset and runner structure.

## Development and verification

```sh
python -m pip install -r requirements-dev.txt
python -m pytest tests
python -m tests.smoke_test
python -m tests.check_startup
```

Tests cover policy boundaries, retries, deadlines, fallback, circuit breakers, tenant-scoped replay, outcome metadata, logging, and startup settings. Smoke checks use simulated providers by default; `--live` explicitly opts into a provider call. The startup check runs a real Uvicorn server with a fake provider, temporary storage, and test identity. Two real SIGTERM shutdown tests require Linux and skip on Windows.

[CI](./.github/workflows/ci.yaml) runs locked dependency installation, lint, a dependency audit, socket-restricted tests, and startup checks on Linux. A separate Docker smoke job builds the image and checks readiness with a fake provider. These configured checks do not imply that a hosted run has been verified. The CI lock targets Linux Python 3.12 and includes `uvloop`; use the development requirements for Windows rather than that lock.

See [SYSTEM_OVERVIEW.md](./SYSTEM_OVERVIEW.md) for the source map, architecture, and scope boundaries, and [HARDENING_PROPOSAL.md](./HARDENING_PROPOSAL.md) for confirmed deliverables and remaining verification.
