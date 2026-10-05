from model_bridge.config.loader import MODEL_PRICING_USD_PER_MILLION_TOKENS


# Estimates token cost from per-million rates, or returns None when pricing or usage is missing.
def estimate_cost_usd(
    model: str,
    input_tokens: int | None,
    output_tokens: int | None,
) -> float | None:
    pricing = MODEL_PRICING_USD_PER_MILLION_TOKENS.get(model)
    if pricing is None or input_tokens is None or output_tokens is None:
        return None
    return (
        input_tokens * pricing["input"] + output_tokens * pricing["output"]
    ) / 1_000_000
