"""Approximate list prices (USD per million tokens) used for cost estimates.

Estimates only: check your provider's pricing page for billing figures.
Cache writes are priced at 1.25x input, cache reads at 0.1x input.
"""

from __future__ import annotations

from dataclasses import dataclass

from concoct.providers.base import TokenUsage


@dataclass(frozen=True)
class ModelPrice:
    input_per_mtok: float
    output_per_mtok: float

    @property
    def cache_write_per_mtok(self) -> float:
        return self.input_per_mtok * 1.25

    @property
    def cache_read_per_mtok(self) -> float:
        return self.input_per_mtok * 0.1


PRICES: dict[str, ModelPrice] = {
    "claude-fable-5-1": ModelPrice(10.0, 50.0),
    "claude-fable-5": ModelPrice(10.0, 50.0),
    "claude-opus-5-5": ModelPrice(4.0, 20.0),
    "claude-opus-5": ModelPrice(5.0, 25.0),
    "claude-opus-4-8": ModelPrice(5.0, 25.0),
    "claude-opus-4-7": ModelPrice(5.0, 25.0),
    "claude-opus-4-6": ModelPrice(5.0, 25.0),
    "claude-sonnet-5": ModelPrice(2.0, 10.0),
    "claude-sonnet-4-6": ModelPrice(3.0, 15.0),
    "claude-haiku-4-5": ModelPrice(1.0, 5.0),
    "offline": ModelPrice(0.0, 0.0),
}


def price_for(model: str) -> ModelPrice | None:
    if model in PRICES:
        return PRICES[model]
    # Tolerate dated or suffixed identifiers, longest prefix first.
    for known in sorted(PRICES, key=len, reverse=True):
        if model.startswith(known):
            return PRICES[known]
    return None


def estimate_cost(model: str, usage: TokenUsage) -> float | None:
    price = price_for(model)
    if price is None:
        return None
    return (
        usage.input_tokens * price.input_per_mtok
        + usage.output_tokens * price.output_per_mtok
        + usage.cache_write_tokens * price.cache_write_per_mtok
        + usage.cache_read_tokens * price.cache_read_per_mtok
    ) / 1_000_000
