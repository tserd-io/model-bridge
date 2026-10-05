import pytest

from model_bridge.evaluations.eval_summary import summarize_results


# Checks aggregation across successful, failed, and uncertain saved results.
@pytest.mark.parametrize("second_cost", [0.02, None])
def test_summary_preserves_metrics_and_cost_coverage(second_cost):
    cases = [
        {
            "result_status": status,
            "http_status": http_status,
            "latency_ms": latency,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "estimated_cost_usd": cost,
            "passed": passed,
            "exact_match": exact_match,
            "field_metrics": field_metrics,
            "cache_hit": cache_hit,
        }
        for status, http_status, latency, input_tokens, output_tokens, cost,
        passed, exact_match, field_metrics, cache_hit in [
            ("success", 200, 10, 100, 20, 0.01, True, True,
             {"precision": 1.0, "recall": 1.0, "f1": 1.0}, False),
            ("success", 200, 30, 50, 10, second_cost, False, False,
             {"precision": 0.0, "recall": 0.0, "f1": 0.0}, True),
            ("failed", 503, 50, None, None, None, False, None, None, False),
            ("unknown", 202, None, None, None, None, False, None, None, False),
        ]
    ]

    summary = summarize_results(
        cases,
        run_id="saved-run",
        dataset_version="dataset-v1",
        prompt_version="prompt-v2",
        provider_name="supplied-provider",
        elapsed_seconds=2,
    )

    assert summary["provider"] == "supplied-provider"
    assert summary["dataset_version"] == "dataset-v1"
    assert summary["prompt_version"] == "prompt-v2"
    assert summary["case_count"] == 4
    assert summary["pass_rate"] == 0.25
    assert summary["exact_match_rate"] == 0.5
    assert summary["field_precision"] == 0.5
    assert summary["field_recall"] == 0.5
    assert summary["field_f1"] == 0.5
    assert summary["latency_ms"] == {"p50": 30, "p95": 50, "p99": 50}
    assert summary["timeout_or_unknown_rate"] == 0.25
    assert summary["error_rate"] == 0.25
    assert summary["availability_rate"] == 0.5
    assert summary["throughput_requests_per_second"] == 2
    assert summary["input_tokens"] == 150
    assert summary["output_tokens"] == 30
    assert summary["cache_hit_rate"] == 0.25
    if second_cost is None:
        assert summary["estimated_cost_usd"] is None
        assert summary["estimated_cost_per_success_usd"] is None
        assert summary["cost_coverage"] == 0.5
    else:
        assert summary["estimated_cost_usd"] == pytest.approx(0.03)
        assert summary["estimated_cost_per_success_usd"] == pytest.approx(0.015)
        assert summary["cost_coverage"] == 1
