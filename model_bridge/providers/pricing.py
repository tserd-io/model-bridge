from model_bridge.config.loader import MODEL_PRICING_USD_PER_MILLION_TOKENS


# Estimates standard uncached token cost using configured rates. None means
# unknown, not free: missing pricing or either usage count must remain unknown.
# This is not invoice accounting (cache discounts, tiers and tools are excluded).
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
