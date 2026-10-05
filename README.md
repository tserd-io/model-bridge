# Model Bridge

Model Bridge is a minimal Python API that gives applications a single endpoint for calling large language models. Built with FastAPI, it routes requests to a configured provider, records request outcomes, and includes an evaluation process for measuring response quality and performance.

## How it works

1. An application sends a message to `POST /chat` with a unique request ID, model preference, and output token limit.
2. Model Bridge selects a model from the configured provider. An optional task type overrides the preference: simple tasks use `fast`; complex and high-risk tasks use `balanced`.
3. The provider generates a response. The gateway applies timeouts and bounded retries, with optional fallback to another configured provider.
4. The API returns a consistent response containing the outcome, generated content, provider, model, latency, attempt count, and available usage information.

Supported providers are **Ollama** for local models and **OpenAI** for hosted models. Provider selection, model mappings, timeouts, and pricing are configured in [`model_bridge/config/config.json`](model_bridge/config/config.json), with environment-variable overrides.

## Request records and observability

SQLite stores request payloads, processing states, and successful responses. Within one tenant, repeating a completed request with the same ID and payload returns the saved response; reusing an ID with different input is rejected. Uncertain provider outcomes are recorded as `unknown`.

Structured JSON logs track request IDs, provider attempts, latency, and outcomes. Prompt and response content is stored in SQLite rather than included in those operational logs. Prometheus metrics are available at `/metrics/`, including request counts, errors, retries, token usage, and estimated OpenAI costs where pricing is configured.

Start the API from the repository root:

```sh
python -m uvicorn model_bridge.main:app
```

Health endpoints report application liveness at `/health/live` and database read availability at `/health/ready`.

## Evaluation

The evaluation runner sends a versioned set of prompts through the gateway and compares responses with expected answers, answer points, or JSON fields. It saves per-case results and a summary covering response quality, latency, availability, token usage, and available cost estimates.

Run from the project root with the dependencies installed and your provider configured:

```sh
python -m model_bridge.evaluations.eval_runner
```

Prompts are defined in [`model_bridge/evaluations/datasets/golden_dataset.json`](model_bridge/evaluations/datasets/golden_dataset.json). Results are written to `data/evaluation_runs/`.
