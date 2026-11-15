"""Calls to Claude through the official SDK, with structured output, effort and prompt caching."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

import anthropic
from anthropic.types import Usage
from pydantic import BaseModel, ValidationError

from delivery_agent.pricing import cost_usd

Effort = Literal["low", "medium", "high"]

# Enough room for adaptive thinking plus a short structured answer, without streaming.
MAX_TOKENS = 8_000


@dataclass(frozen=True)
class TokenUsage:
    """Tokens spent by one or more model calls."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_write_tokens: int = 0
    cache_read_tokens: int = 0
    calls: int = 0

    def __add__(self, other: TokenUsage) -> TokenUsage:
        return TokenUsage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_write_tokens=self.cache_write_tokens + other.cache_write_tokens,
            cache_read_tokens=self.cache_read_tokens + other.cache_read_tokens,
            calls=self.calls + other.calls,
        )

    @classmethod
    def from_api(cls, usage: Usage) -> TokenUsage:
        """Read the usage block of one API response.

        Args:
            usage: ``response.usage`` from the Messages API.

        Returns:
            The tokens of that single call.
        """
        return cls(
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_write_tokens=usage.cache_creation_input_tokens or 0,
            cache_read_tokens=usage.cache_read_input_tokens or 0,
            calls=1,
        )

    def cost_usd(self, model: str) -> float:
        """Cost of these tokens on a model, with cache prices.

        Args:
            model: Model id that produced the tokens.

        Returns:
            Cost in US dollars.
        """
        return cost_usd(
            model,
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            cache_write_tokens=self.cache_write_tokens,
            cache_read_tokens=self.cache_read_tokens,
        )


@dataclass(frozen=True)
class ModelCall[OutputT: BaseModel]:
    """Outcome of one model call: the parsed output, or the reason why there is none."""

    output: OutputT | None
    stop_reason: str  # end_turn, refusal, max_tokens or invalid_output
    usage: TokenUsage


class LanguageModel(Protocol):
    """What the graph needs from a model. The tests use a fake with the same shape."""

    name: str

    def ask[OutputT: BaseModel](
        self, *, system: str, prompt: str, effort: Effort, output_type: type[OutputT]
    ) -> ModelCall[OutputT]:
        """Send one request and parse the structured output."""
        ...


class AnthropicModel:
    """Claude through the official ``anthropic`` SDK."""

    def __init__(self, api_key: str, model: str, client: anthropic.Anthropic | None = None) -> None:
        self.name = model
        self._client = client or anthropic.Anthropic(api_key=api_key, timeout=60.0, max_retries=2)

    def ask[OutputT: BaseModel](
        self, *, system: str, prompt: str, effort: Effort, output_type: type[OutputT]
    ) -> ModelCall[OutputT]:
        """Send one request with a cached system prompt and parse the structured output.

        The stop reason is checked before the text: a refusal or a cut answer comes back as a
        ``ModelCall`` without output, so the graph can decide what to do and the usage still counts.

        Args:
            system: Fixed instructions of the node, with the schema description. Cached.
            prompt: The variable part: the question and, for some nodes, SQL, errors or rows.
            effort: How much the model thinks before answering.
            output_type: Pydantic model of the expected output.

        Returns:
            The parsed output, or ``None`` with the stop reason that explains why.

        Raises:
            anthropic.APIError: The API failed after the SDK retries.
        """
        response = self._client.messages.create(
            model=self.name,
            max_tokens=MAX_TOKENS,
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": prompt}],
            output_config={
                "effort": effort,
                "format": {"type": "json_schema", "schema": anthropic.transform_schema(output_type)},
            },
        )
        usage = TokenUsage.from_api(response.usage)

        if response.stop_reason in ("refusal", "max_tokens"):
            return ModelCall(output=None, stop_reason=response.stop_reason, usage=usage)

        text = "".join(block.text for block in response.content if block.type == "text")

        try:
            output = output_type.model_validate_json(text)
        except ValidationError:
            return ModelCall(output=None, stop_reason="invalid_output", usage=usage)

        return ModelCall(output=output, stop_reason="end_turn", usage=usage)
