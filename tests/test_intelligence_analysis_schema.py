"""intelligence/analysis/schema.py: `AIAnalysisOutput` is the strongest layer
of the prompt-injection defense (docs/architecture/05-ai-data-flow.md) — a
hijacked model response is still confined to this closed shape, no matter
what the model was tricked into "wanting" to say.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from personaltrade.intelligence.analysis.schema import AIAnalysisOutput

_VALID = {
    "stance": "bullish",
    "conviction": "high",
    "key_factors": ["strong earnings"],
    "risks": ["sector headwinds"],
    "news_impact": "positive",
    "summary": "Looks strong on fundamentals.",
}


class TestAIAnalysisOutput:
    def test_accepts_a_well_formed_response(self) -> None:
        out = AIAnalysisOutput.model_validate(_VALID)
        assert out.stance == "bullish"

    def test_rejects_a_non_advisory_stance_value(self) -> None:
        """An injected instruction trying to make the model emit an order
        ("BUY 500 shares now") cannot even fit in the `stance` field — it
        isn't one of the three literals."""
        with pytest.raises(ValidationError):
            AIAnalysisOutput.model_validate({**_VALID, "stance": "BUY 500 shares now"})

    def test_rejects_unknown_extra_fields(self) -> None:
        """Guards against a hijacked response trying to smuggle a new field
        (e.g. "target_price") that downstream code might accidentally trust."""
        with pytest.raises(ValidationError):
            AIAnalysisOutput.model_validate({**_VALID, "target_price": 1234})

    def test_rejects_oversized_key_factors_list(self) -> None:
        with pytest.raises(ValidationError):
            AIAnalysisOutput.model_validate(
                {**_VALID, "key_factors": [f"factor {i}" for i in range(6)]}
            )

    def test_rejects_oversized_summary(self) -> None:
        with pytest.raises(ValidationError):
            AIAnalysisOutput.model_validate({**_VALID, "summary": "x" * 601})
