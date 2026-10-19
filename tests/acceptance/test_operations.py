"""Health and observability for complete API requests."""

import json
import logging
from pathlib import Path
from tempfile import TemporaryDirectory
import pytest
from model_bridge.evaluations.eval_runner import run_evaluation
from model_bridge.observability.logging import logger
from tests.support.harness import payload

pytestmark = pytest.mark.acceptance


# Health probes succeed without making a generation call.
def test_health_endpoints(app_factory):
    gateway = app_factory()
    with gateway.client() as client:
        assert client.get("/health/live").json() == {"status": "alive"}
        assert client.get("/health/ready").status_code == 200
    assert gateway.primary.calls == []


# A completed request produces correlated JSON events and scrapeable request metrics.
def test_logs_and_metrics(app_factory):
    gateway = app_factory()
    records = []
    handler = logging.Handler()
    handler.emit = records.append
    logger.addHandler(handler)
    try:
        with gateway.client() as client:
            response = client.post("/chat", json=payload("observable"))
            metrics = client.get(
                "/metrics/",
                headers={
                    "Accept": "application/openmetrics-text; version=1.0.0; charset=utf-8",
                },
            )
        assert response.status_code == metrics.status_code == 200
        events = [json.loads(record.getMessage()) for record in records]
        finished = next(
            event for event in events if event["event"] == "provider_attempt_finished"
        )
        assert finished["request_id"] == "observable"
        assert finished["latency_ms"] >= 0
        assert finished["timestamp"].endswith("Z")
        assert "llm_requests_total" in metrics.text
        assert "llm_active_generations" in metrics.text
        assert 'request_id="observable"' in metrics.text
    finally:
        logger.removeHandler(handler)


# A scored request produces versioned evaluation artifacts through the actual API.
def test_evaluation_output(app_factory) -> None:
    gateway = app_factory()
    dataset = {
        "dataset_version": "unit-v1",
        "prompt_version": "prompt-v7",
        "cases": [
            {
                "case_id": "echo-orange",
                "task_type": "simple",
                "message": "say orange",
                "model_preference": "balanced",
                "max_tokens": 32,
                "expected": {
                    "allowed_answers": [],
                    "answer_points": ["orange"],
                    "expected_fields": None,
                    "expected_sources": [],
                },
                "allowed_answer_variation": "answer may include other wording",
                "known_failure_category": "instruction_following",
            }
        ],
    }

    with TemporaryDirectory() as directory:
        root = Path(directory)
        dataset_path = root / "dataset.json"
        dataset_path.write_text(json.dumps(dataset), encoding="utf-8")
        with gateway.client() as client:
            summary = run_evaluation(
                dataset_path,
                root / "results",
                client=client,
                provider_name="fake",
            )
        saved_summary = json.loads(
            (root / "results" / f"{summary['run_id']}-summary.json").read_text(
                encoding="utf-8"
            )
        )
        assert saved_summary == summary
        result_path = next((root / "results").glob("*.jsonl"))
        result = json.loads(result_path.read_text(encoding="utf-8").splitlines()[0])

    assert summary["dataset_version"] == "unit-v1"
    assert summary["prompt_version"] == "prompt-v7"
    assert summary["pass_rate"] == 1.0
    assert summary["estimated_cost_usd"] == pytest.approx(0.001)
    assert result["passed"] is True
    assert result["model_version"] == "fake-fast"
    assert result["known_failure_category"] == "instruction_following"
    assert result["grounding"]["faithfulness"] is None
    assert result["agentic_metrics"]["applicable"] is False
