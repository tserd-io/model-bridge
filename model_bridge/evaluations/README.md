# Gateway Evaluation

Run the versioned golden dataset from the workspace root:

```powershell
.\.venv\Scripts\python.exe -m model_bridge.evaluations.eval_runner
```

Results are written to `data/evaluation_runs/` as one JSONL record per case and one summary JSON per run. Use a deterministic fake provider to check harness plumbing without model calls:

```powershell
$env:LLM_PROVIDER = "fake"
.\.venv\Scripts\python.exe -m model_bridge.evaluations.eval_runner
Remove-Item Env:LLM_PROVIDER
```

Edit `datasets/golden_dataset.json` to add cases. Each case records task type, input, expected fields or answer points, allowed answers/variation, expected sources, and a known failure category. The runner records dataset and prompt versions, actual model/provider, latency, attempts, token usage, estimated cost when configured, and result labels.

The current task-quality scores are exact-match rate, answer-point pass rate, and micro precision/recall/F1 for expected JSON fields. The gateway has no retrieval/source metadata or tool execution yet, so grounding and agentic scores are recorded as not applicable rather than inferred from generated text.

The operational summary includes p50/p95/p99 latency, error and unknown-outcome rates, throughput, and availability. Token totals come from provider usage. Estimated USD cost stays `null` until `model_pricing_usd_per_million_tokens` is configured per model with `input` and `output` rates. Local Ollama compute/energy cost is not included.

Routing can be specified per request with `task_type`: `simple` selects `fast`; `complex` and `high_risk` select `balanced`. High-risk responses set `human_review_required`; this flag is not a review workflow. Fallback follows `config.json` and can be overridden with `LLM_FALLBACK_PROVIDER`; an empty value disables it. Fallback after an ambiguous timeout may duplicate provider work or charges.

The evaluation code is split into three modules: `eval_runner.py` executes and scores cases, `eval_summary.py` calculates aggregate metrics, and `eval_writer.py` saves results. Programmatic callers supply `client` and `provider_name` to `run_evaluation()` and manage the client lifetime. The CLI creates its own local API client.

The standalone CLI does not establish trusted tenant identity. Without a supplied identity, chat calls receive HTTP 401. Programmatic callers can use an explicitly configured application client.
