"""Anthropic API prices, used to report the cost of each question."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Price:
    """US dollars per million tokens."""

    input: float
    output: float
    cache_write: float  # 5-minute cache writes cost 1.25x the input price
    cache_read: float


# Standard prices of November 2026 for prompts of up to 100K tokens; this agent's prompts are far smaller.
# Thinking tokens are billed as output and already come inside output_tokens.
PRICES: dict[str, Price] = {
    "claude-haiku-5-5": Price(input=0.10, output=0.50, cache_write=0.125, cache_read=0.01),
    "claude-sonnet-5-5": Price(input=2.00, output=10.00, cache_write=2.50, cache_read=0.10),
    "claude-opus-5-5": Price(input=4.00, output=20.00, cache_write=5.00, cache_read=0.20),
}


def cost_usd(
    model: str,
    *,
    input_tokens: int,
    output_tokens: int,
    cache_write_tokens: int = 0,
    cache_read_tokens: int = 0,
) -> float:
    """Compute what a set of tokens costs on a model.

    Args:
        model: Model id, such as ``claude-haiku-5-5``.
        input_tokens: Uncached input tokens.
        output_tokens: Output tokens, thinking included.
        cache_write_tokens: Input tokens written to the prompt cache.
        cache_read_tokens: Input tokens read from the prompt cache.

    Returns:
        Cost in US dollars.

    Raises:
        KeyError: The model has no price in ``PRICES``.
    """
    try:
        price = PRICES[model]
    except KeyError:
        raise KeyError(f"Sem preço cadastrado para o modelo {model!r}; adicione em pricing.py.") from None

    total = (
        input_tokens * price.input
        + output_tokens * price.output
        + cache_write_tokens * price.cache_write
        + cache_read_tokens * price.cache_read
    )

    return total / 1_000_000
