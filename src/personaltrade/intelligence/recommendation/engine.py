"""Recommendation Engine orchestration (ROADMAP M15): one screening pass over
a universe of instruments, producing persisted, ranked `Recommendation` rows.

A recommendation requires a deterministic basis — no strategy Signal on an
instrument's latest bar means no recommendation for it at all (docs/
architecture/05-ai-data-flow.md rule 1). AI analysis is attempted best-effort
per instrument and never blocks: a disabled/budget-exhausted/failed AI call
degrades that one recommendation to deterministic-only, exactly the "AI
outage" behavior ROADMAP M15 asks to be tested (docs/architecture/
05-ai-data-flow.md "Failure behavior").
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pandas as pd
from sqlalchemy.orm import Session

from personaltrade.core.config import AIConfig, RecommendationConfig
from personaltrade.core.enums import Mode, RecommendationAction
from personaltrade.core.logging import get_logger
from personaltrade.data.store.models import AIAnalysis, Instrument, Position, Recommendation
from personaltrade.data.store.repos import (
    InstrumentRepository,
    NewsRepository,
    PositionRepository,
    RecommendationRepository,
)
from personaltrade.intelligence.analysis.schema import AIAnalysisOutput
from personaltrade.intelligence.analysis.service import (
    AIAnalysisDisabled,
    AIBudgetExhausted,
    analyze_instrument,
)
from personaltrade.intelligence.analysis.snapshot import (
    NewsSnapshotItem,
    PositionSnapshot,
    build_market_snapshot,
)
from personaltrade.intelligence.llm.provider import LLMOutputInvalid, LLMProvider, LLMProviderError
from personaltrade.intelligence.recommendation.merge import merge_recommendation, rank_sort_key
from personaltrade.intelligence.recommendation.screener import latest_signal
from personaltrade.strategy.base import FLAT_POSITION, PositionView, Strategy

logger = get_logger(__name__)


@dataclass(frozen=True)
class ScreenedRecommendation:
    """One instrument's outcome, the ORM row it was persisted as, and whatever
    AI output informed it (None if AI was unavailable for this instrument)."""

    instrument: Instrument
    record: Recommendation
    ai_output: AIAnalysisOutput | None


@dataclass(frozen=True)
class _Candidate:
    instrument: Instrument
    action: RecommendationAction
    rationale: dict[str, Any]
    ai_output: AIAnalysisOutput | None
    ai_record: AIAnalysis | None


def _position_view(position_row: Position | None) -> PositionView:
    if position_row is None or position_row.qty == 0:
        return FLAT_POSITION
    return PositionView(qty=position_row.qty, avg_price=float(position_row.avg_price))


def _position_snapshot(position_row: Position | None, last_close: Any) -> PositionSnapshot | None:
    if position_row is None or position_row.qty == 0:
        return None
    mark = Decimal(str(last_close))
    return PositionSnapshot(
        qty=position_row.qty,
        avg_price=position_row.avg_price,
        unrealized_pnl=(mark - position_row.avg_price) * position_row.qty,
    )


def _try_ai_analysis(
    session: Session,
    provider: LLMProvider | None,
    ai_cfg: AIConfig,
    instrument: Instrument,
    candles: pd.DataFrame,
    position_row: Position | None,
    *,
    now: datetime,
) -> tuple[AIAnalysisOutput | None, AIAnalysis | None]:
    """Best-effort AI analysis for one instrument. Every failure mode listed
    in docs/architecture/05-ai-data-flow.md's "Failure behavior" table
    (disabled, budget exhausted, provider error, schema-validation failure)
    degrades to `(None, None)` rather than propagating — the caller then
    merges deterministically-only, never blocking the whole cycle on one
    instrument's AI call.
    """
    if provider is None:
        return None, None

    since = now - timedelta(days=ai_cfg.news_lookback_days)
    news_rows = NewsRepository(session).list_for_instrument(instrument.id, since)
    news = [
        NewsSnapshotItem(
            news_item_id=item.id,
            source=item.source,
            published_at=item.published_at,
            title=item.title,
            body=item.body,
        )
        for item in news_rows[: ai_cfg.max_news_items]
    ]
    position = _position_snapshot(position_row, candles["close"].iloc[-1])
    snapshot = build_market_snapshot(instrument.symbol, candles, position=position, news=news)

    try:
        outcome = analyze_instrument(session, provider, ai_cfg, instrument, snapshot, now=now)
    except AIAnalysisDisabled:
        return None, None
    except AIBudgetExhausted as exc:
        logger.info("recommendation.ai_budget_exhausted", symbol=instrument.symbol, reason=str(exc))
        return None, None
    except (LLMProviderError, LLMOutputInvalid) as exc:
        logger.warning("recommendation.ai_unavailable", symbol=instrument.symbol, error=str(exc))
        return None, None
    return outcome.output, outcome.record


def run_recommendation_cycle(
    session: Session,
    provider: LLMProvider | None,
    ai_cfg: AIConfig,
    rec_cfg: RecommendationConfig,
    strategy: Strategy,
    candles_by_symbol: Mapping[str, pd.DataFrame],
    *,
    exchange: str = "NSE",
    now: datetime | None = None,
) -> list[ScreenedRecommendation]:
    """One pass over `candles_by_symbol`. Returns persisted recommendations
    ranked best-first (`Recommendation.rank` starts at 1); instruments with
    no strategy signal on their latest bar, or not present in `instruments`,
    produce no row at all (no deterministic basis, docs/architecture/
    05-ai-data-flow.md rule 1) — the caller must not synthesize one.
    """
    now = now or datetime.now(UTC)
    instrument_repo = InstrumentRepository(session)
    position_repo = PositionRepository(session)
    rec_repo = RecommendationRepository(session)

    candidates: list[_Candidate] = []
    for symbol, candles in candles_by_symbol.items():
        instrument = instrument_repo.get_by_symbol(symbol, exchange)
        if instrument is None:
            logger.warning("recommendation.unknown_instrument", symbol=symbol)
            continue

        position_row = position_repo.get_for(instrument.id, Mode.PAPER)
        position = _position_view(position_row)
        signal = latest_signal(strategy, candles, position)
        if signal is None:
            continue

        ai_output, ai_record = _try_ai_analysis(
            session, provider, ai_cfg, instrument, candles, position_row, now=now
        )
        action, rationale = merge_recommendation(signal.direction, position.qty, ai_output, rec_cfg)
        candidates.append(_Candidate(instrument, action, rationale, ai_output, ai_record))

    candidates.sort(key=lambda c: (*rank_sort_key(c.action, c.ai_output), c.instrument.symbol))

    results: list[ScreenedRecommendation] = []
    for rank, candidate in enumerate(candidates, start=1):
        record = rec_repo.add(
            Recommendation(
                instrument_id=candidate.instrument.id,
                signal_id=None,
                ai_analysis_id=candidate.ai_record.id if candidate.ai_record is not None else None,
                action=candidate.action,
                rank=rank,
                rationale=candidate.rationale,
                created_at=now,
            )
        )
        results.append(
            ScreenedRecommendation(
                instrument=candidate.instrument, record=record, ai_output=candidate.ai_output
            )
        )
    return results
