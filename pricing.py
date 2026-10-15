"""Unit prices for cost estimates. Defaults drift: verify against vendor pricing pages
and override through .env."""
import os

import config

# model id -> (input USD per million tokens, output USD per million tokens)
DEFAULT_MODEL_PRICES = {
    "claude-sonnet-5-5": (2.00, 10.00),
}


def _f(name, default):
    return float(os.getenv(name, default))


def claude_cost(model, input_tokens, output_tokens):
    """Return the USD cost, or None if the model has no price entry (tracked as unpriced)."""
    prices = DEFAULT_MODEL_PRICES.get(model)
    if prices is None:
        return None
    in_price, out_price = prices
    if model == "claude-sonnet-5-5":
        in_price = _f("SONNET_INPUT_USD_PER_MTOK", in_price)
        out_price = _f("SONNET_OUTPUT_USD_PER_MTOK", out_price)
    multiplier = _f("ANTHROPIC_PRICE_MULTIPLIER", 1.0)
    return (input_tokens * in_price + output_tokens * out_price) / 1_000_000 * multiplier


def serper_cost(credits):
    return credits * _f("SERPER_USD_PER_CREDIT", 0.001)


def apify_fallback_cost(n_results):
    return n_results * _f("APIFY_FALLBACK_USD_PER_RESULT", 0.0005) + _f("APIFY_FALLBACK_USD_PER_START", 0)
