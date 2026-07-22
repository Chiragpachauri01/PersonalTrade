"""intelligence/llm/anthropic_provider.py: backend-agnostic analyze(), model id
resolution per backend (ADR-013), and cost computation from usage tokens.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Literal

import anthropic
import httpx
import pytest
from pydantic import BaseModel

from personaltrade.core.config import ModelPricing
from personaltrade.intelligence.llm.anthropic_provider import (
    AnthropicLLMProvider,
    Backend,
    UnknownBedrockModel,
    _resolve_model_id,
    build_anthropic_provider,
)
from personaltrade.intelligence.llm.provider import LLMOutputInvalid, LLMProviderError

_PRICING = {
    "claude-opus-4-8": ModelPricing(input_per_mtok=Decimal("5"), output_per_mtok=Decimal("25")),
}


class _Schema(BaseModel):
    stance: Literal["bullish", "bearish"]


@dataclass
class _FakeTextBlock:
    text: str
    type: str = "text"


@dataclass
class _FakeUsage:
    input_tokens: int
    output_tokens: int


@dataclass
class _FakeParsedMessage:
    parsed_output: _Schema | None
    content: list[_FakeTextBlock] = field(default_factory=list)
    usage: _FakeUsage = field(default_factory=lambda: _FakeUsage(0, 0))


class _FakeMessages:
    def __init__(self, response: _FakeParsedMessage | Exception) -> None:
        self._response = response
        self.calls: list[dict[str, Any]] = []

    def parse(self, **kwargs: Any) -> _FakeParsedMessage:
        self.calls.append(kwargs)
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


class _FakeClient:
    def __init__(self, response: _FakeParsedMessage | Exception) -> None:
        self.messages = _FakeMessages(response)


def _provider(
    response: _FakeParsedMessage | Exception, backend: Backend = "direct"
) -> tuple[AnthropicLLMProvider, _FakeClient]:
    client = _FakeClient(response)
    provider = AnthropicLLMProvider(
        client, backend=backend, model="claude-opus-4-8", pricing=_PRICING
    )
    return provider, client


class TestAnalyze:
    def test_successful_parse_returns_result_with_computed_cost(self) -> None:
        response = _FakeParsedMessage(
            parsed_output=_Schema(stance="bullish"),
            content=[_FakeTextBlock(text="raw model text")],
            usage=_FakeUsage(input_tokens=1_000_000, output_tokens=1_000_000),
        )
        provider, client = _provider(response)

        result = provider.analyze(system="sys", user_content="user", schema=_Schema, max_tokens=512)

        assert result.parsed == _Schema(stance="bullish")
        assert result.raw_text == "raw model text"
        assert result.model == "claude-opus-4-8"
        assert result.input_tokens == 1_000_000
        assert result.output_tokens == 1_000_000
        assert result.cost_usd == Decimal("30")  # 1 MTok * $5 + 1 MTok * $25
        assert client.messages.calls[0]["model"] == "claude-opus-4-8"  # direct: canonical id as-is

    def test_system_prompt_carries_cache_control_breakpoint(self) -> None:
        response = _FakeParsedMessage(parsed_output=_Schema(stance="bullish"))
        provider, client = _provider(response)

        provider.analyze(
            system="stable system text", user_content="u", schema=_Schema, max_tokens=100
        )

        system_blocks = client.messages.calls[0]["system"]
        assert system_blocks == [
            {"type": "text", "text": "stable system text", "cache_control": {"type": "ephemeral"}}
        ]

    def test_none_parsed_output_raises_llm_output_invalid(self) -> None:
        response = _FakeParsedMessage(parsed_output=None)
        provider, _ = _provider(response)

        with pytest.raises(LLMOutputInvalid):
            provider.analyze(system="s", user_content="u", schema=_Schema, max_tokens=100)

    def test_api_error_raises_llm_provider_error(self) -> None:
        request = httpx.Request("POST", "https://example.com")
        provider, _ = _provider(anthropic.APIConnectionError(request=request))

        with pytest.raises(LLMProviderError):
            provider.analyze(system="s", user_content="u", schema=_Schema, max_tokens=100)

    def test_unpriced_model_costs_zero(self) -> None:
        response = _FakeParsedMessage(
            parsed_output=_Schema(stance="bullish"),
            usage=_FakeUsage(input_tokens=500, output_tokens=500),
        )
        client = _FakeClient(response)
        provider = AnthropicLLMProvider(
            client, backend="direct", model="some-unpriced-model", pricing=_PRICING
        )

        result = provider.analyze(system="s", user_content="u", schema=_Schema, max_tokens=100)

        assert result.cost_usd == Decimal("0")

    def test_bedrock_backend_resolves_to_inference_profile_id(self) -> None:
        response = _FakeParsedMessage(parsed_output=_Schema(stance="bullish"))
        provider, client = _provider(response, backend="bedrock")

        provider.analyze(system="s", user_content="u", schema=_Schema, max_tokens=100)

        assert client.messages.calls[0]["model"] == "global.anthropic.claude-opus-4-8"

    def test_bedrock_backend_unknown_model_raises(self) -> None:
        response = _FakeParsedMessage(parsed_output=_Schema(stance="bullish"))
        client = _FakeClient(response)
        provider = AnthropicLLMProvider(
            client, backend="bedrock", model="claude-nonexistent", pricing=_PRICING
        )

        with pytest.raises(UnknownBedrockModel):
            provider.analyze(system="s", user_content="u", schema=_Schema, max_tokens=100)


class TestResolveModelId:
    def test_direct_backend_uses_canonical_id_unchanged(self) -> None:
        assert _resolve_model_id("claude-opus-4-8", "direct") == "claude-opus-4-8"

    def test_bedrock_backend_maps_known_id(self) -> None:
        assert _resolve_model_id("claude-haiku-4-5", "bedrock") == (
            "global.anthropic.claude-haiku-4-5-20251001-v1:0"
        )


class TestBuildAnthropicProvider:
    def test_bedrock_token_takes_priority_over_direct_key(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from personaltrade.core.config import AIConfig, Secrets

        monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "fake-bearer")
        monkeypatch.setenv("AWS_REGION", "ap-south-1")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-direct-key")

        provider = build_anthropic_provider(Secrets(_env_file=None), AIConfig())

        assert isinstance(provider, AnthropicLLMProvider)
        assert provider._backend == "bedrock"

    def test_falls_back_to_direct_api_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from personaltrade.core.config import AIConfig, Secrets

        monkeypatch.delenv("AWS_BEARER_TOKEN_BEDROCK", raising=False)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-direct-key")

        provider = build_anthropic_provider(Secrets(_env_file=None), AIConfig())

        assert provider._backend == "direct"

    def test_no_credentials_raises_config_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from personaltrade.core.config import AIConfig, Secrets
        from personaltrade.core.errors import ConfigError

        monkeypatch.delenv("AWS_BEARER_TOKEN_BEDROCK", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        with pytest.raises(ConfigError):
            build_anthropic_provider(Secrets(_env_file=None), AIConfig())
