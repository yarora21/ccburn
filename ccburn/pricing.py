"""
API-equivalent pricing for Claude models.

All costs are ESTIMATES based on published Anthropic API pricing.
Users on Pro/Max plans pay a flat rate — these numbers show what
the same usage would cost at API rates, for comparison purposes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict


@dataclass(frozen=True)
class ModelPricing:
    """Per-token prices (USD) for a model tier."""
    input: float
    output: float
    cache_write: float
    cache_read: float


# Prices per token (divide published per-MTok rates by 1e6).
TIERS: Dict[str, ModelPricing] = {
    "opus": ModelPricing(
        input=15 / 1e6,
        output=75 / 1e6,
        cache_write=18.75 / 1e6,
        cache_read=1.50 / 1e6,
    ),
    "sonnet": ModelPricing(
        input=3 / 1e6,
        output=15 / 1e6,
        cache_write=3.75 / 1e6,
        cache_read=0.30 / 1e6,
    ),
    "haiku": ModelPricing(
        input=0.80 / 1e6,
        output=4 / 1e6,
        cache_write=1.00 / 1e6,
        cache_read=0.08 / 1e6,
    ),
}

DEFAULT_TIER = "sonnet"


def tier_for_model(model: str) -> str:
    """Map a model id string (e.g. 'claude-opus-4-6') to a pricing tier."""
    if not model:
        return DEFAULT_TIER
    m = model.lower()
    for tier in TIERS:
        if tier in m:
            return tier
    return DEFAULT_TIER


def token_cost(
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_write_tokens: int = 0,
    cache_read_tokens: int = 0,
    *,
    tier: str = DEFAULT_TIER,
) -> float:
    """Compute total USD cost for a set of token counts at the given tier."""
    p = TIERS[tier]
    return (
        input_tokens * p.input
        + output_tokens * p.output
        + cache_write_tokens * p.cache_write
        + cache_read_tokens * p.cache_read
    )


def usage_cost(usage: dict, tier: str) -> float:
    """Compute cost from a raw usage dict (as found in JSONL)."""
    return token_cost(
        input_tokens=usage.get("input_tokens", 0),
        output_tokens=usage.get("output_tokens", 0),
        cache_write_tokens=usage.get("cache_creation_input_tokens", 0),
        cache_read_tokens=usage.get("cache_read_input_tokens", 0),
        tier=tier,
    )


def cache_read_cost(usage: dict, tier: str) -> float:
    """Return just the cache-read portion of cost."""
    return usage.get("cache_read_input_tokens", 0) * TIERS[tier].cache_read
