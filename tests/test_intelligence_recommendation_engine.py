"""intelligence/recommendation/engine.py: the end-to-end merge (ROADMAP M15)
— a Recommendation row requires a deterministic Signal; AI is attempted
best-effort per instrument and never blocks the cycle (docs/architecture/
05-ai-data-flow.md "Failure behavior" — the AI-outage degradation test
ROADMAP M15 calls for).
"""

from __future__ import annotations

from decimal import Decimal
from typing import cast

import pandas as pd
from pydantic import BaseModel
from sqlalchemy.orm import Session

from personaltrade.core.config import AIConfig, RecommendationConfig
from personaltrade.core.enums import Mode, RecommendationAction, SignalDirection
from personaltrade.data.store.models import Instrument, Position
from personaltrade.data.store.repos import (
    AIAnalysisRepository,
    InstrumentRepository,
    PositionRepository,
)
from personaltrade.intelligence.analysis.schema import AIAnalysisOutput
from personaltrade.intelligence.llm.provider import LLMProviderError, LLMResult
from personaltrade.intelligence.recommendation.engine import run_recommendation_cycle
from personaltrade.strategy.base import Signal
from tests.factories import ScriptedStrategy, synthetic_candles

_LAST_INDEX = 44  # synthetic_candles([100+i for i in range(45)]) -> indices 0..44


def _instrument(session: Session, symbol: str) -> Instrument:
    return InstrumentRepository(session).add(
        Instrument(
            symbol=symbol,
            exchange="NSE",
            instrument_key=f"NSE_EQ|{symbol}",
            tick_size=Decimal("0.05"),
        )
    )


def _candles() -> pd.DataFrame:
    return synthetic_candles([100.0 + i for i in range(45)])


def _ai_output(conviction: str = "medium") -> AIAnalysisOutput:
    return AIAnalysisOutput(
        stance="bullish",
        conviction=conviction,
        key_factors=["demand"],
        risks=["rates"],
        news_impact="none",
        summary="test summary",
    )


class _QueueProvider:
    """LLMProvider test double: pops one scripted result/exception per call,
    in call order — lets a test control what AI says about each instrument
    independently within one recommendation cycle."""

    def __init__(self, results: list[LLMResult[AIAnalysisOutput] | Exception]) -> None:
        self._results = list(results)
        self.calls = 0

    def analyze[T: BaseModel](
        self, *, system: str, user_content: str, schema: type[T], max_tokens: int
    ) -> LLMResult[T]:
        self.calls += 1
        result = self._results.pop(0)
        if isinstance(result, Exception):
            raise result
        return cast(LLMResult[T], result)


def _result(output: AIAnalysisOutput, cost: str = "0.01") -> LLMResult[AIAnalysisOutput]:
    return LLMResult(
        parsed=output,
        raw_text="raw",
        model="claude-opus-4-8",
        input_tokens=100,
        output_tokens=50,
        cost_usd=Decimal(cost),
    )


class TestRunRecommendationCycle:
    def test_no_signal_produces_no_recommendation(self, db_session: Session) -> None:
        _instrument(db_session, "AAA")
        strategy = ScriptedStrategy({})  # never emits

        results = run_recommendation_cycle(
            db_session, None, AIConfig(), RecommendationConfig(), strategy, {"AAA": _candles()}
        )

        assert results == []

    def test_unknown_instrument_is_skipped_not_an_error(self, db_session: Session) -> None:
        strategy = ScriptedStrategy({_LAST_INDEX: Signal(SignalDirection.LONG, 150.0, {})})

        results = run_recommendation_cycle(
            db_session, None, AIConfig(), RecommendationConfig(), strategy, {"NOPE": _candles()}
        )

        assert results == []

    def test_signal_with_no_provider_persists_deterministic_only(self, db_session: Session) -> None:
        instrument = _instrument(db_session, "AAA")
        strategy = ScriptedStrategy({_LAST_INDEX: Signal(SignalDirection.LONG, 150.0, {})})

        results = run_recommendation_cycle(
            db_session, None, AIConfig(), RecommendationConfig(), strategy, {"AAA": _candles()}
        )

        assert len(results) == 1
        result = results[0]
        assert result.instrument.id == instrument.id
        assert result.ai_output is None
        assert result.record.action == RecommendationAction.BUY
        assert result.record.rank == 1
        assert result.record.ai_analysis_id is None
        assert result.record.signal_id is None
        assert result.record.rationale["ai"] is None

    def test_ai_disabled_degrades_to_deterministic_only(self, db_session: Session) -> None:
        _instrument(db_session, "AAA")
        strategy = ScriptedStrategy({_LAST_INDEX: Signal(SignalDirection.LONG, 150.0, {})})
        provider = _QueueProvider([_result(_ai_output())])  # would succeed if called
        ai_cfg = AIConfig(enabled=False)

        results = run_recommendation_cycle(
            db_session, provider, ai_cfg, RecommendationConfig(), strategy, {"AAA": _candles()}
        )

        assert len(results) == 1
        assert results[0].ai_output is None
        assert results[0].record.action == RecommendationAction.BUY
        assert provider.calls == 0

    def test_ai_budget_exhausted_degrades_to_deterministic_only(self, db_session: Session) -> None:
        _instrument(db_session, "AAA")
        strategy = ScriptedStrategy({_LAST_INDEX: Signal(SignalDirection.LONG, 150.0, {})})
        provider = _QueueProvider([_result(_ai_output())])
        ai_cfg = AIConfig(daily_call_cap=0)

        results = run_recommendation_cycle(
            db_session, provider, ai_cfg, RecommendationConfig(), strategy, {"AAA": _candles()}
        )

        assert len(results) == 1
        assert results[0].ai_output is None
        assert results[0].record.action == RecommendationAction.BUY
        assert provider.calls == 0

    def test_ai_provider_error_degrades_to_deterministic_only(self, db_session: Session) -> None:
        _instrument(db_session, "AAA")
        strategy = ScriptedStrategy({_LAST_INDEX: Signal(SignalDirection.LONG, 150.0, {})})
        provider = _QueueProvider([LLMProviderError("backend down")])

        results = run_recommendation_cycle(
            db_session, provider, AIConfig(), RecommendationConfig(), strategy, {"AAA": _candles()}
        )

        assert len(results) == 1
        assert results[0].ai_output is None
        assert results[0].record.action == RecommendationAction.BUY
        assert AIAnalysisRepository(db_session).list_all() == []

    def test_ai_veto_downgrades_action_and_persists_audit_row(self, db_session: Session) -> None:
        _instrument(db_session, "AAA")
        strategy = ScriptedStrategy({_LAST_INDEX: Signal(SignalDirection.LONG, 150.0, {})})
        ai_output = AIAnalysisOutput(
            stance="bearish",
            conviction="high",
            key_factors=[],
            risks=["bad print"],
            news_impact="negative",
            summary="Negative results just dropped.",
        )
        provider = _QueueProvider([_result(ai_output)])

        results = run_recommendation_cycle(
            db_session, provider, AIConfig(), RecommendationConfig(), strategy, {"AAA": _candles()}
        )

        assert len(results) == 1
        result = results[0]
        assert result.ai_output == ai_output
        assert result.record.action == RecommendationAction.HOLD
        assert result.record.rationale["deterministic_action"] == "BUY"
        assert "veto" in result.record.rationale
        assert result.record.ai_analysis_id is not None
        assert AIAnalysisRepository(db_session).get(result.record.ai_analysis_id) is not None

    def test_already_in_position_signal_still_evaluated_against_current_position(
        self, db_session: Session
    ) -> None:
        instrument = _instrument(db_session, "AAA")
        db_session.add(
            Position(instrument_id=instrument.id, qty=10, avg_price=Decimal("100"), mode=Mode.PAPER)
        )
        db_session.flush()
        strategy = ScriptedStrategy({_LAST_INDEX: Signal(SignalDirection.LONG, 150.0, {})})

        results = run_recommendation_cycle(
            db_session, None, AIConfig(), RecommendationConfig(), strategy, {"AAA": _candles()}
        )

        assert len(results) == 1
        assert results[0].record.action == RecommendationAction.HOLD
        assert results[0].record.rationale["position_qty"] == 10

    def test_multiple_instruments_ranked_actionable_first_then_by_conviction(
        self, db_session: Session
    ) -> None:
        _instrument(db_session, "AAA")  # will end up HOLD (vetoed)
        _instrument(db_session, "BBB")  # BUY, high conviction
        _instrument(db_session, "CCC")  # BUY, no AI (medium via absent provider call order)
        strategy = ScriptedStrategy({_LAST_INDEX: Signal(SignalDirection.LONG, 150.0, {})})

        vetoed = AIAnalysisOutput(
            stance="bearish",
            conviction="high",
            key_factors=[],
            risks=[],
            news_impact="negative",
            summary="bad news",
        )
        strong_buy = _ai_output(conviction="high")
        weak_buy = _ai_output(conviction="low")
        # Iteration order over the dict below is AAA, BBB, CCC — the provider
        # queue must match that call order.
        provider = _QueueProvider([_result(vetoed), _result(strong_buy), _result(weak_buy)])

        results = run_recommendation_cycle(
            db_session,
            provider,
            AIConfig(),
            RecommendationConfig(),
            strategy,
            {"AAA": _candles(), "BBB": _candles(), "CCC": _candles()},
        )

        symbols_in_rank_order = [r.instrument.symbol for r in results]
        assert symbols_in_rank_order == ["BBB", "CCC", "AAA"]
        assert [r.record.rank for r in results] == [1, 2, 3]
        assert results[0].record.action == RecommendationAction.BUY
        assert results[1].record.action == RecommendationAction.BUY
        assert results[2].record.action == RecommendationAction.HOLD

    def test_position_row_is_untouched_by_a_recommendation_cycle(self, db_session: Session) -> None:
        """The engine is read-only over position state (CLAUDE.md Rule 10 —
        advisory only): screening never mutates Position/Order/Trade rows."""
        instrument = _instrument(db_session, "AAA")
        db_session.add(
            Position(instrument_id=instrument.id, qty=3, avg_price=Decimal("50"), mode=Mode.PAPER)
        )
        db_session.flush()
        strategy = ScriptedStrategy({_LAST_INDEX: Signal(SignalDirection.EXIT, 150.0, {})})

        run_recommendation_cycle(
            db_session, None, AIConfig(), RecommendationConfig(), strategy, {"AAA": _candles()}
        )

        position = PositionRepository(db_session).get_for(instrument.id, Mode.PAPER)
        assert position is not None
        assert position.qty == 3
