import json
import sys
import time
from pathlib import Path
from uuid import uuid4

project_root = Path(__file__).resolve().parents[2]
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from scripts.evaluations.eval_summary import summarize_results
from scripts.evaluations.eval_writer import write_results

DEFAULT_DATASET = Path(__file__).with_name("golden_dataset.json")
DEFAULT_RUNS_DIR = Path(__file__).with_name("runs")


# Scores output against expected answers, answer points, or JSON fields and records result labels.
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
            # Use empty fields when output cannot be parsed so field scoring can continue.
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


# Runs the versioned dataset through the API and saves per-case results and aggregate summaries.
def run_evaluation(
    dataset_path: Path,
    output_dir: Path,
    *,
    client,
    provider_name: str,
) -> dict:
    dataset = json.loads(dataset_path.read_text(encoding="utf-8"))
    run_id = uuid4().hex
    started_at = time.perf_counter()
    cases = []

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
            # Represent a non-JSON HTTP response as an error record so the evaluation can continue.
            response_body = {"status": "error", "content": response.text}

        labels = _evaluate_case(case, response_body, response.status_code)
        cases.append(
            {
                "run_id": run_id,
                "dataset_version": dataset["dataset_version"],
                "prompt_version": dataset["prompt_version"],
                "provider": response_body.get("provider", provider_name),
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
    summary = summarize_results(
        cases,
        run_id=run_id,
        dataset_version=dataset["dataset_version"],
        prompt_version=dataset["prompt_version"],
        provider_name=provider_name,
        elapsed_seconds=elapsed_seconds,
    )
    write_results(output_dir, run_id=run_id, cases=cases, summary=summary)
    return summary


# Reads evaluation command-line options, runs the dataset, and prints the summary as JSON.
def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Run the gateway golden evaluation dataset")
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_RUNS_DIR)
    args = parser.parse_args()
    # Load application dependencies only when executing the local CLI.
    from fastapi.testclient import TestClient
    from scripts.load_settings import LLM_PROVIDER
    from scripts.main import app

    with TestClient(app) as client:
        summary = run_evaluation(
            args.dataset,
            args.output_dir,
            client=client,
            provider_name=LLM_PROVIDER,
        )
    print(json.dumps(summary, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
