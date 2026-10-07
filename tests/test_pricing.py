"""Verified configured rates and unknown-cost behavior without provider calls."""

import pytest

from model_bridge.providers.pricing import estimate_cost_usd


def test_verified_gpt_55_standard_rates():
    assert estimate_cost_usd("gpt-5.5", 1000, 100) == pytest.approx(0.008)


@pytest.mark.parametrize("model", ["gpt-6.0", "smollm2:135m", "llama3.1:8b", "unknown"])
def test_unpriced_model_cost_remains_unknown(model):
    assert estimate_cost_usd(model, 1000, 100) is None


@pytest.mark.parametrize("input_tokens,output_tokens", [(None, 100), (1000, None), (None, None)])
def test_missing_usage_remains_unknown(input_tokens, output_tokens):
    assert estimate_cost_usd("gpt-5.5", input_tokens, output_tokens) is None


def test_reported_zero_usage_is_known_zero_cost():
    assert estimate_cost_usd("gpt-5.5", 0, 0) == 0.0
