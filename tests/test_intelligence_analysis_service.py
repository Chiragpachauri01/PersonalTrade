"""intelligence/analysis/service.py: budget gates run before any provider
call, and a successful call persists the full audit row
(docs/architecture/05-ai-data-flow.md `AI_ANALYSIS` row; ROADMAP M14).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import cast

import pytest
from pydantic import BaseModel
from sqlalchemy.orm import Session

from personaltrade.core.config import AIConfig
from personaltrade.data.store.models import AIAnalysis, Instrument
from personaltrade.data.store.repos import AIAnalysisRepository, InstrumentRepository
from personaltrade.intelligence.analysis.schema import AIAnalysisOutput
from personaltrade.intelligence.analysis.service import (
    AIAnalysisDisabled,
    AIBudgetExhausted,
    analyze_instrument,
)
from personaltrade.intelligence.analysis.snapshot import MarketSnapshot
from personaltrade.intelligence.llm.provider import LLMProviderError, LLMResult

_NOW = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)
_OUTPUT = AIAnalysisOutput(
    stance="bullish",
    conviction="medium",
    key_factors=["steady volumes"],
    risks=["rate sensitivity"],
    news_impact="none",
    summary="Range-bound with a mild upward bias.",
)


class FakeProvider:
    """LLMProvider test double: returns a scripted result or raises a scripted error."""

    def __init__(self, result: LLMResult[AIAnalysisOutput] | Exception) -> None:
        self._result = result
        self.calls = 0

    def analyze[T: BaseModel](
        self, *, system: str, user_content: str, schema: type[T], max_tokens: int
    ) -> LLMResult[T]:
        self.calls += 1
        if isinstance(self._result, Exception):
            raise self._result
        return cast(LLMResult[T], self._result)


def _result(cost: str = "0.01") -> LLMResult[AIAnalysisOutput]:
    return LLMResult(
        parsed=_OUTPUT,
        raw_text="raw",
        model="claude-opus-4-8",
        input_tokens=1000,
        output_tokens=200,
        cost_usd=Decimal(cost),
    )


@pytest.fixture()
def reliance(db_session: Session) -> Instrument:
    return InstrumentRepository(db_session).add(
        Instrument(
            symbol="RELIANCE",
            exchange="NSE",
            instrument_key="NSE_EQ|RELIANCE",
            tick_size=Decimal("0.05"),
        )
    )


def _snapshot() -> MarketSnapshot:
    return MarketSnapshot(
        symbol="RELIANCE",
        as_of=_NOW,
        last_close=Decimal("1300"),
        last_volume=100,
        indicators={"rsi_14": 55.0},
        position=None,
    )


class TestAnalyzeInstrument:
    def test_successful_call_persists_audit_row(
        self, db_session: Session, reliance: Instrument
    ) -> None:
        provider = FakeProvider(_result(cost="0.02"))

        outcome = analyze_instrument(
            db_session, provider, AIConfig(), reliance, _snapshot(), now=_NOW
        )

        assert outcome.output == _OUTPUT
        assert outcome.record.id is not None
        assert outcome.record.instrument_id == reliance.id
        assert outcome.record.model == "claude-opus-4-8"
        assert outcome.record.input_tokens == 1000
        assert outcome.record.output_tokens == 200
        assert outcome.record.cost_usd == Decimal("0.02")
        assert outcome.record.output["stance"] == "bullish"
        assert provider.calls == 1

    def test_disabled_raises_and_never_calls_provider(
        self, db_session: Session, reliance: Instrument
    ) -> None:
        provider = FakeProvider(_result())
        cfg = AIConfig(enabled=False)

        with pytest.raises(AIAnalysisDisabled):
            analyze_instrument(db_session, provider, cfg, reliance, _snapshot(), now=_NOW)

        assert provider.calls == 0
        assert AIAnalysisRepository(db_session).list_all() == []

    def test_daily_call_cap_reached_blocks_before_calling_provider(
        self, db_session: Session, reliance: Instrument
    ) -> None:
        repo = AIAnalysisRepository(db_session)
        repo.add(
            AIAnalysis(
                instrument_id=reliance.id,
                model="claude-opus-4-8",
                prompt_hash="x",
                input_snapshot={},
                output={},
                input_tokens=1,
                output_tokens=1,
                cost_usd=Decimal("0.01"),
                created_at=_NOW,
            )
        )
        provider = FakeProvider(_result())
        cfg = AIConfig(daily_call_cap=1)

        with pytest.raises(AIBudgetExhausted):
            analyze_instrument(db_session, provider, cfg, reliance, _snapshot(), now=_NOW)

        assert provider.calls == 0

    def test_monthly_usd_cap_reached_blocks_before_calling_provider(
        self, db_session: Session, reliance: Instrument
    ) -> None:
        repo = AIAnalysisRepository(db_session)
        repo.add(
            AIAnalysis(
                instrument_id=reliance.id,
                model="claude-opus-4-8",
                prompt_hash="x",
                input_snapshot={},
                output={},
                input_tokens=1,
                output_tokens=1,
                cost_usd=Decimal("25"),
                created_at=_NOW,
            )
        )
        provider = FakeProvider(_result())
        cfg = AIConfig(monthly_usd_cap=Decimal("25"))

        with pytest.raises(AIBudgetExhausted):
            analyze_instrument(db_session, provider, cfg, reliance, _snapshot(), now=_NOW)

        assert provider.calls == 0

    def test_spend_from_a_previous_month_does_not_count(
        self, db_session: Session, reliance: Instrument
    ) -> None:
        repo = AIAnalysisRepository(db_session)
        repo.add(
            AIAnalysis(
                instrument_id=reliance.id,
                model="claude-opus-4-8",
                prompt_hash="x",
                input_snapshot={},
                output={},
                input_tokens=1,
                output_tokens=1,
                cost_usd=Decimal("25"),
                created_at=_NOW - timedelta(days=40),
            )
        )
        provider = FakeProvider(_result(cost="0.01"))
        cfg = AIConfig(monthly_usd_cap=Decimal("25"))

        outcome = analyze_instrument(db_session, provider, cfg, reliance, _snapshot(), now=_NOW)

        assert outcome.output == _OUTPUT
        assert provider.calls == 1

    def test_provider_error_propagates_and_persists_nothing(
        self, db_session: Session, reliance: Instrument
    ) -> None:
        provider = FakeProvider(LLMProviderError("backend down"))

        with pytest.raises(LLMProviderError):
            analyze_instrument(db_session, provider, AIConfig(), reliance, _snapshot(), now=_NOW)

        assert AIAnalysisRepository(db_session).list_all() == []
