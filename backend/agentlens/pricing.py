from __future__ import annotations

from typing import Any

DEFAULT_INPUT_PER_MILLION = 14.0
DEFAULT_OUTPUT_PER_MILLION = 56.0
DEFAULT_CACHED_PER_MILLION = 3.5
DEFAULT_TOOL_COST = 0.01


def candidate_pricing(
    model_parameters: dict[str, Any],
) -> dict[str, float] | None:
    raw = model_parameters.get("pricing_cny")
    if not isinstance(raw, dict):
        return None
    keys = (
        "input_per_million",
        "output_per_million",
        "cached_per_million",
        "tool_cost",
    )
    if any(key not in raw for key in keys):
        return None
    values: dict[str, float] = {}
    for key in keys:
        value = raw[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
            return None
        values[key] = float(value)
    return values


def compute_usage_cost_cny(
    *,
    input_tokens: int,
    output_tokens: int,
    cached_tokens: int,
    tool_calls: int,
    input_per_million: float = DEFAULT_INPUT_PER_MILLION,
    output_per_million: float = DEFAULT_OUTPUT_PER_MILLION,
    cached_per_million: float = DEFAULT_CACHED_PER_MILLION,
    tool_cost: float = DEFAULT_TOOL_COST,
) -> float:
    """Price usage where cached tokens are a subset of input tokens."""
    if cached_tokens > input_tokens:
        raise ValueError("cached_tokens cannot exceed input_tokens")
    uncached_input_tokens = input_tokens - cached_tokens
    return (
        uncached_input_tokens * input_per_million
        + cached_tokens * cached_per_million
        + output_tokens * output_per_million
    ) / 1_000_000 + tool_calls * tool_cost
