"""`LLMProvider` — the replaceable seam (CLAUDE.md Rule 7) between whichever
model/backend answers a call (Claude direct, Claude on Bedrock today; GPT or
others later, ADR-009/ADR-013) and everything downstream, which only ever
sees a schema-validated `LLMResult`.

No caller outside `intelligence/` may import a concrete provider
(docs/architecture/03-interfaces.md).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

from pydantic import BaseModel

from personaltrade.core.errors import PersonalTradeError


class LLMProviderError(PersonalTradeError):
    """Transport/API failure talking to the model backend — retryable."""


class LLMOutputInvalid(PersonalTradeError):
    """The model's response did not parse against the requested schema.

    Never surfaced as if it were valid output (docs/architecture/03-interfaces.md);
    the caller must treat analysis as unavailable, same as a provider error.
    """


@dataclass(frozen=True)
class LLMResult[T: BaseModel]:
    """Everything the audit trail (`AIAnalysis` row, ROADMAP M14) needs to record."""

    parsed: T
    raw_text: str
    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: Decimal


class LLMProvider(Protocol):
    """Claude first; GPT/others later. Selected by config `ai.provider` + `ai.model`."""

    def analyze[T: BaseModel](
        self, *, system: str, user_content: str, schema: type[T], max_tokens: int
    ) -> LLMResult[T]:
        """Raises `LLMOutputInvalid` on schema mismatch, `LLMProviderError` on
        transport/API failure. Never returns free text as if it were valid.
        """
        ...
