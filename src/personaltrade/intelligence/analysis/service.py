"""Analysis Service (ROADMAP M14): the only place that turns a `MarketSnapshot`
into a persisted, audited `AIAnalysis` row. Budget gates run before any
provider call — a capped-out day/month costs nothing, not even a failed API
call (docs/architecture/05-ai-data-flow.md "Budget exhausted" row).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from personaltrade.core.config import AIConfig
from personaltrade.core.errors import PersonalTradeError
from personaltrade.data.store.models import AIAnalysis, Instrument
from personaltrade.data.store.repos import AIAnalysisRepository
from personaltrade.intelligence.analysis.prompt import build_system_prompt, build_user_content
from personaltrade.intelligence.analysis.schema import AIAnalysisOutput
from personaltrade.intelligence.analysis.snapshot import MarketSnapshot
from personaltrade.intelligence.llm.provider import LLMProvider


class AIAnalysisDisabled(PersonalTradeError):
    """`ai.enabled` is False in config."""


class AIBudgetExhausted(PersonalTradeError):
    """Daily call cap or monthly USD cap already spent — checked, not clamped:
    a cap of 0 means no calls at all, same as the kill switch's "0 tolerance"
    reading of its own limits."""


@dataclass(frozen=True)
class AnalysisOutcome:
    record: AIAnalysis
    output: AIAnalysisOutput


def _month_start(now: datetime) -> datetime:
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def _day_start(now: datetime) -> datetime:
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def _snapshot_dict(snapshot: MarketSnapshot) -> dict[str, object]:
    """What ends up in `AIAnalysis.input_snapshot` — indicator values and
    *which* news items were shown (docs/architecture/02-data-model.md: "indicators
    + news ids shown"), not the full news bodies, which are already durable in
    `news_items` and would only bloat this audit row."""
    return {
        "symbol": snapshot.symbol,
        "as_of": snapshot.as_of.isoformat(),
        "last_close": str(snapshot.last_close),
        "last_volume": snapshot.last_volume,
        "indicators": snapshot.indicators,
        "position": (
            {
                "qty": snapshot.position.qty,
                "avg_price": str(snapshot.position.avg_price),
                "unrealized_pnl": str(snapshot.position.unrealized_pnl),
            }
            if snapshot.position is not None
            else None
        ),
        "news_item_ids": [item.news_item_id for item in snapshot.news],
    }


def analyze_instrument(
    session: Session,
    provider: LLMProvider,
    ai_cfg: AIConfig,
    instrument: Instrument,
    snapshot: MarketSnapshot,
    *,
    now: datetime | None = None,
) -> AnalysisOutcome:
    """Raises `AIAnalysisDisabled`/`AIBudgetExhausted` (checked before any
    provider call) or lets the provider's own `LLMProviderError`/
    `LLMOutputInvalid` propagate (docs/architecture/03-interfaces.md) — the
    caller (CLI today, Recommendation Engine at M15) treats all of these the
    same way: analysis unavailable, nothing else in the system blocks on it.
    """
    if not ai_cfg.enabled:
        raise AIAnalysisDisabled("ai.enabled is False in config")

    now = now or datetime.now(UTC)
    repo = AIAnalysisRepository(session)
    if repo.count_since(_day_start(now)) >= ai_cfg.daily_call_cap:
        raise AIBudgetExhausted(f"daily_call_cap={ai_cfg.daily_call_cap} reached")
    if repo.sum_cost_since(_month_start(now)) >= ai_cfg.monthly_usd_cap:
        raise AIBudgetExhausted(f"monthly_usd_cap={ai_cfg.monthly_usd_cap} reached")

    system = build_system_prompt()
    user_content = build_user_content(snapshot)
    prompt_hash = hashlib.sha256(f"{system}\n{user_content}".encode()).hexdigest()

    result = provider.analyze(
        system=system,
        user_content=user_content,
        schema=AIAnalysisOutput,
        max_tokens=ai_cfg.max_tokens_per_call,
    )

    record = repo.add(
        AIAnalysis(
            instrument_id=instrument.id,
            model=result.model,
            prompt_hash=prompt_hash,
            input_snapshot=_snapshot_dict(snapshot),
            output=result.parsed.model_dump(),
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            cost_usd=result.cost_usd,
        )
    )
    return AnalysisOutcome(record=record, output=result.parsed)
