import json
import math
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

project_root = Path(__file__).resolve().parents[2]
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from scripts.config import LLM_PROVIDER
from scripts.main import app

DEFAULT_DATASET = Path(__file__).with_name("golden_dataset.json")
DEFAULT_RUNS_DIR = Path(__file__).with_name("runs")


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, math.ceil(percentile * len(ordered)) - 1)
    return ordered[index]


def _evaluate_case(case: dict, response: dict, status_code: int) -> dict:
    expected = case["expected"]
    content = response.get("content") or ""
    normalized_content = content.strip().casefold()
    allowed_answers = [
        answer.strip().casefold()
        for answer in expected.get("allowed_answers", [])
    ]
    exact_match = normalized_content in allowed_answers if allowed_answers else None
    answer_points = expected.get("answer_points", [])
    matched_points = [
        point
        for point in answer_points
        if point.casefold() in normalized_content
    ]
    expected_fields = expected.get("expected_fields")
    parsed_fields = None
    field_metrics = None
    fields_pass = None
    if expected_fields is not None:
        try:
            parsed_fields = json.loads(content)
        except (json.JSONDecodeError, TypeError):
            parsed_fields = {}
        expected_pairs = set(expected_fields.items())
        actual_pairs = set(parsed_fields.items()) if isinstance(parsed_fields, dict) else set()
        true_positive = len(expected_pairs & actual_pairs)
        false_positive = len(actual_pairs - expected_pairs)
        false_negative = len(expected_pairs - actual_pairs)
        precision = (
            true_positive / (true_positive + false_positive)
            if true_positive + false_positive
            else 0.0
        )
        recall = (
            true_positive / (true_positive + false_negative)
            if true_positive + false_negative
            else 0.0
        )
        field_metrics = {
            "precision": precision,
            "recall": recall,
            "f1": 2 * precision * recall / (precision + recall)
            if precision + recall
            else 0.0,
        }
        fields_pass = not false_negative and not false_positive

    answer_points_pass = len(matched_points) == len(answer_points)
    passed = fields_pass if fields_pass is not None else (
        exact_match if exact_match is not None else answer_points_pass
    )
    if exact_match is not None:
        passed = bool(passed and exact_match)

    expected_sources = expected.get("expected_sources", [])
    return {
        "case_id": case["case_id"],
        "known_failure_category": case.get("known_failure_category"),
        "http_status": status_code,
        "result_status": response.get("status", "error"),
        "passed": passed,
        "exact_match": exact_match,
        "matched_answer_points": matched_points,
        "expected_answer_points": answer_points,
        "field_metrics": field_metrics,
        "grounding": {
            "applicable": bool(expected_sources),
            "retrieval_recall_at_k": None,
            "citation_correctness": None,
            "faithfulness": None,
            "unsupported_claim_rate": None,
            "reason": "The current gateway does not return retrieved-source metadata",
        },
        "agentic_metrics": {
            "applicable": False,
            "tool_call_correctness": None,
            "invalid_tool_call_rate": None,
            "steps_per_successful_task": None,
            "human_escalation_rate": None,
            "task_completion_rate": None,
            "unsafe_action_prevention_rate": None,
            "reason": "The current gateway does not execute tools",
        },
    }


def run_evaluation(dataset_path: Path, output_dir: Path) -> dict:
    dataset = json.loads(dataset_path.read_text(encoding="utf-8"))
    run_id = uuid4().hex
    started_at = time.perf_counter()
    cases = []

    with TestClient(app) as client:
        for case in dataset["cases"]:
            request_id = f"eval-{run_id}-{case['case_id']}"
            payload = {
                "request_id": request_id,
                "message": case["message"],
                "model_preference": case["model_preference"],
                "max_tokens": case["max_tokens"],
                "task_type": case.get("task_type"),
            }
            response_started = time.perf_counter()
            response = client.post("/chat", json=payload)
            observed_latency_ms = round((time.perf_counter() - response_started) * 1000)
            try:
                response_body = response.json()
            except ValueError:
                response_body = {"status": "error", "content": response.text}

            labels = _evaluate_case(case, response_body, response.status_code)
            cases.append(
                {
                    "run_id": run_id,
                    "dataset_version": dataset["dataset_version"],
                    "prompt_version": dataset["prompt_version"],
                    "provider": response_body.get("provider", LLM_PROVIDER),
                    "request_id": request_id,
                    "model_version": response_body.get("model"),
                    "task_type": case.get("task_type"),
                    "latency_ms": response_body.get("latency_ms", observed_latency_ms),
                    "input_tokens": response_body.get("input_tokens"),
                    "output_tokens": response_body.get("output_tokens"),
                    "estimated_cost_usd": response_body.get("estimated_cost_usd"),
                    "cache_hit": response.headers.get("X-Idempotent-Replay") == "true",
                    "prompt": case["message"],
                    "allowed_answer_variation": case.get("allowed_answer_variation"),
                    "expected_sources": case["expected"].get("expected_sources", []),
                    **labels,
                }
            )

    elapsed_seconds = time.perf_counter() - started_at
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
        "provider": LLM_PROVIDER,
        "dataset_version": dataset["dataset_version"],
        "prompt_version": dataset["prompt_version"],
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

    output_dir.mkdir(parents=True, exist_ok=True)
    with (output_dir / f"{run_id}.jsonl").open("w", encoding="utf-8") as result_file:
        for case in cases:
            result_file.write(json.dumps(case, ensure_ascii=False) + "\n")
    (output_dir / f"{run_id}-summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return summary


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Run the gateway golden evaluation dataset")
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_RUNS_DIR)
    args = parser.parse_args()
    summary = run_evaluation(args.dataset, args.output_dir)
    print(json.dumps(summary, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
