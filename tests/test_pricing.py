import pytest

from delivery_agent.llm import TokenUsage
from delivery_agent.pricing import cost_usd


def test_haiku_prices_per_million_tokens() -> None:
    million = 1_000_000

    assert cost_usd("claude-haiku-5-5", input_tokens=million, output_tokens=0) == pytest.approx(0.10)
    assert cost_usd("claude-haiku-5-5", input_tokens=0, output_tokens=million) == pytest.approx(0.50)
    assert cost_usd(
        "claude-haiku-5-5", input_tokens=0, output_tokens=0, cache_write_tokens=million
    ) == pytest.approx(0.125)
    assert cost_usd(
        "claude-haiku-5-5", input_tokens=0, output_tokens=0, cache_read_tokens=million
    ) == pytest.approx(0.01)


def test_unknown_model_has_no_price() -> None:
    with pytest.raises(KeyError, match=r"pricing\.py"):
        cost_usd("modelo-inexistente", input_tokens=1, output_tokens=1)


def test_token_usage_adds_up_and_costs() -> None:
    usage = TokenUsage(input_tokens=1000, output_tokens=200, calls=1) + TokenUsage(
        input_tokens=500, output_tokens=100, cache_read_tokens=4000, calls=1
    )

    assert usage == TokenUsage(input_tokens=1500, output_tokens=300, cache_read_tokens=4000, calls=2)
    assert usage.cost_usd("claude-haiku-5-5") == pytest.approx((1500 * 0.10 + 300 * 0.50 + 4000 * 0.01) / 1e6)
