"""`AnthropicLLMProvider` — Claude behind `LLMProvider` (ADR-009).

Backend (Bedrock vs. the direct API) is picked once, at construction time,
purely from which secret is configured (ADR-013) — `analyze()` itself never
branches on backend. Model ids are resolved per-backend since Bedrock uses
`region.anthropic.<id>` inference-profile ids while the direct API takes the
canonical id as-is (ADR-013/ADR-024).
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Literal

import anthropic
from anthropic import Anthropic, AnthropicBedrock
from pydantic import BaseModel

from personaltrade.core.config import AIConfig, ModelPricing, Secrets
from personaltrade.core.errors import ConfigError
from personaltrade.intelligence.llm.provider import LLMOutputInvalid, LLMProviderError, LLMResult

Backend = Literal["bedrock", "direct"]

#: Bedrock cross-region inference-profile ids for the canonical model ids this
#: project prices in `AIConfig.pricing`. Discovered via
#: `aws bedrock list-inference-profiles` (ADR-024) — a canonical model needs
#: an entry here before it can run on the Bedrock backend; the direct API
#: uses the canonical id unchanged.
_BEDROCK_MODEL_IDS: dict[str, str] = {
    "claude-opus-4-8": "global.anthropic.claude-opus-4-8",
    "claude-haiku-4-5": "global.anthropic.claude-haiku-4-5-20251001-v1:0",
}


class UnknownBedrockModel(LLMProviderError):
    """Canonical model id has no known Bedrock inference-profile mapping."""


def _resolve_model_id(canonical: str, backend: Backend) -> str:
    if backend == "direct":
        return canonical
    try:
        return _BEDROCK_MODEL_IDS[canonical]
    except KeyError:
        raise UnknownBedrockModel(
            f"no Bedrock inference-profile id mapped for model {canonical!r} — "
            f"add one to _BEDROCK_MODEL_IDS in {__name__}"
        ) from None


def _cost(
    model: str, input_tokens: int, output_tokens: int, pricing: dict[str, ModelPricing]
) -> Decimal:
    rates = pricing.get(model)
    if rates is None:
        return Decimal("0")
    mtok = Decimal(1_000_000)
    return (Decimal(input_tokens) * rates.input_per_mtok / mtok) + (
        Decimal(output_tokens) * rates.output_per_mtok / mtok
    )


class AnthropicLLMProvider:
    """Wraps an already-constructed SDK client so tests can inject a fake one
    (`build_anthropic_provider` below owns the real ADR-013 selection logic).

    `client` is typed `Any`: the Stainless-generated `Anthropic`/`AnthropicBedrock`
    `.messages.parse()` overload set is too detailed to hand-roll a matching
    Protocol against, and `build_anthropic_provider` — the only place that
    constructs a real client — already type-checks each SDK constructor call
    precisely, which is where a genuine typo would actually be caught.
    """

    def __init__(
        self,
        client: Any,
        *,
        backend: Backend,
        model: str,
        pricing: dict[str, ModelPricing],
    ) -> None:
        self._client = client
        self._backend = backend
        self._model = model
        self._pricing = pricing

    def analyze[T: BaseModel](
        self, *, system: str, user_content: str, schema: type[T], max_tokens: int
    ) -> LLMResult[T]:
        model_id = _resolve_model_id(self._model, self._backend)
        try:
            response = self._client.messages.parse(
                model=model_id,
                max_tokens=max_tokens,
                system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": user_content}],
                output_format=schema,
            )
        except anthropic.APIError as exc:
            raise LLMProviderError(f"{self._backend} backend call failed: {exc}") from exc

        parsed = response.parsed_output
        if parsed is None:
            raise LLMOutputInvalid(
                f"model response for {model_id} did not parse against {schema.__name__}"
            )

        raw_text = "".join(block.text for block in response.content if block.type == "text")
        return LLMResult(
            parsed=parsed,
            raw_text=raw_text,
            model=self._model,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            cost_usd=_cost(
                self._model,
                response.usage.input_tokens,
                response.usage.output_tokens,
                self._pricing,
            ),
        )


def build_anthropic_provider(secrets: Secrets, ai_cfg: AIConfig) -> AnthropicLLMProvider:
    """ADR-013 backend selection: a configured Bedrock bearer token wins (free
    credits first); otherwise the direct API key; otherwise a `ConfigError` —
    there is no silent no-op AI provider."""
    if secrets.aws_bearer_token_bedrock is not None:
        client: Anthropic | AnthropicBedrock = AnthropicBedrock(
            api_key=secrets.aws_bearer_token_bedrock.get_secret_value(),
            aws_region=secrets.aws_region or "us-east-1",
        )
        return AnthropicLLMProvider(
            client, backend="bedrock", model=ai_cfg.model, pricing=ai_cfg.pricing
        )
    if secrets.anthropic_api_key is not None:
        client = Anthropic(api_key=secrets.anthropic_api_key.get_secret_value())
        return AnthropicLLMProvider(
            client, backend="direct", model=ai_cfg.model, pricing=ai_cfg.pricing
        )
    raise ConfigError(
        "no AI backend configured — set AWS_BEARER_TOKEN_BEDROCK (+ AWS_REGION) "
        "or ANTHROPIC_API_KEY in .env (see .env.example)"
    )
