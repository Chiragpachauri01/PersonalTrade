"""The structured-output contract the LLM must fill (docs/architecture/05-ai-data-flow.md).

Named `AIAnalysisOutput`, not `AIAnalysis`, to stay distinct from the ORM
audit row (`data/store/models.AIAnalysis`) that persists it — one is the
schema the model fills in, the other is the database record.

No numeric trading fields (no target price, no quantity, no stop): by
construction the model cannot express an order (CLAUDE.md Rule 9/10). Every
field is a bounded `Literal` or a length-capped list/string, so even a fully
prompt-injected response is confined to this shape — the strongest layer of
the injection defense (docs/architecture/05-ai-data-flow.md).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class AIAnalysisOutput(BaseModel):
    model_config = {"extra": "forbid"}

    stance: Literal["bullish", "bearish", "neutral"]
    conviction: Literal["low", "medium", "high"]
    key_factors: list[str] = Field(max_length=5)
    risks: list[str] = Field(max_length=5)
    news_impact: Literal["positive", "negative", "mixed", "none"]
    summary: str = Field(max_length=600)  # ~2-3 sentences for the dashboard
