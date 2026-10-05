import math


# Calculates a nearest-rank percentile, returning None for an empty sample.
def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, math.ceil(percentile * len(ordered)) - 1)
    return ordered[index]


# Calculates aggregate metrics without making requests or writing files.
def summarize_results(
    cases: list[dict],
    *,
    run_id: str,
    dataset_version: str,
    prompt_version: str,
    provider_name: str,
    elapsed_seconds: float,
) -> dict:
    successful = [case for case in cases if case["result_status"] == "success"]
    latencies = [case["latency_ms"] for case in cases if case["latency_ms"] is not None]
    input_token_values = [case["input_tokens"] for case in cases if case["input_tokens"] is not None]
    output_token_values = [case["output_tokens"] for case in cases if case["output_tokens"] is not None]
    known_costs = [case["estimated_cost_usd"] for case in successful if case["estimated_cost_usd"] is not None]
    passed_count = sum(case["passed"] for case in cases)
    error_count = sum(case["http_status"] >= 400 for case in cases)
    unknown_count = sum(case["result_status"] == "unknown" for case in cases)
    exact_scored = [case for case in cases if case["exact_match"] is not None]
    field_scored = [case["field_metrics"] for case in cases if case["field_metrics"] is not None]
    successful_with_cost = [case for case in successful if case["estimated_cost_usd"] is not None]

    summary = {
        "run_id": run_id,
        "provider": provider_name,
        "dataset_version": dataset_version,
        "prompt_version": prompt_version,
        "case_count": len(cases),
        "pass_rate": passed_count / len(cases) if cases else None,
        "exact_match_rate": (
            sum(case["exact_match"] for case in exact_scored) / len(exact_scored)
            if exact_scored
            else None
        ),
        "field_precision": (
            sum(metrics["precision"] for metrics in field_scored) / len(field_scored)
            if field_scored
            else None
        ),
        "field_recall": (
            sum(metrics["recall"] for metrics in field_scored) / len(field_scored)
            if field_scored
            else None
        ),
        "field_f1": (
            sum(metrics["f1"] for metrics in field_scored) / len(field_scored)
            if field_scored
            else None
        ),
        "latency_ms": {
            "p50": _percentile(latencies, 0.50),
            "p95": _percentile(latencies, 0.95),
            "p99": _percentile(latencies, 0.99),
        },
        "timeout_or_unknown_rate": unknown_count / len(cases) if cases else None,
        "error_rate": error_count / len(cases) if cases else None,
        "throughput_requests_per_second": len(cases) / elapsed_seconds if elapsed_seconds else None,
        "availability_rate": len(successful) / len(cases) if cases else None,
        "input_tokens": sum(input_token_values) if input_token_values else None,
        "output_tokens": sum(output_token_values) if output_token_values else None,
        "estimated_cost_usd": sum(known_costs) if len(known_costs) == len(successful) else None,
        "estimated_cost_per_success_usd": (
            sum(known_costs) / len(successful)
            if successful and len(known_costs) == len(successful)
            else None
        ),
        "cost_coverage": len(successful_with_cost) / len(successful) if successful else None,
        "cache_hit_rate": sum(case.get("cache_hit", False) for case in cases) / len(cases)
        if cases
        else None,
        "grounding": "not applicable until retrieval/source metadata is implemented",
        "agentic": "not applicable until tool execution is implemented",
        "elapsed_seconds": elapsed_seconds,
    }

    return summary
