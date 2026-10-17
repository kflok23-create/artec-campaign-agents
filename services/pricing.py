"""Claude API list prices, used for budget estimates and to cross-check the SDK's cost figure.

The Agent SDK reports `total_cost_usd`, but it is a client-side estimate from a price table
bundled with the SDK. We compute our own from token usage and log both.

Prices: USD per million tokens, Anthropic first-party API, as of 2026-10-06.
Cache writes use the 5-minute TTL rate (1.25x input); cache reads are 0.1x input.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ModelPrice:
    input: float
    output: float
    # Some models charge more once a prompt exceeds a size; None means a flat rate.
    long_prompt_threshold: int | None = None
    long_input: float | None = None
    long_output: float | None = None


PRICES: dict[str, ModelPrice] = {
    "claude-sonnet-5-5": ModelPrice(input=2.00, output=10.00),
    "claude-haiku-5-5": ModelPrice(
        input=0.10, output=0.50, long_prompt_threshold=100_000, long_input=0.50, long_output=2.50
    ),
}

CACHE_WRITE_MULTIPLIER = 1.25
CACHE_READ_MULTIPLIER = 0.10


def cost_from_usage(model: str, usage: dict[str, Any] | None) -> float | None:
    """USD cost of one call from its token usage, or None if the model is not priced here."""
    price = PRICES.get(model)
    if price is None or not usage:
        return None
    fresh_in = int(usage.get("input_tokens") or 0)
    out = int(usage.get("output_tokens") or 0)
    cache_write = int(usage.get("cache_creation_input_tokens") or 0)
    cache_read = int(usage.get("cache_read_input_tokens") or 0)

    rate_in, rate_out = price.input, price.output
    prompt_size = fresh_in + cache_write + cache_read
    if price.long_prompt_threshold and prompt_size > price.long_prompt_threshold:
        rate_in = price.long_input or rate_in
        rate_out = price.long_output or rate_out

    total = (
        fresh_in * rate_in
        + cache_write * rate_in * CACHE_WRITE_MULTIPLIER
        + cache_read * rate_in * CACHE_READ_MULTIPLIER
        + out * rate_out
    )
    return total / 1_000_000
