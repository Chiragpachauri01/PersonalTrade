"""Deterministic gate rules (docs/architecture/05-ai-data-flow.md): a
recommendation's action always has a deterministic basis (`deterministic_action`);
AI (`AIAnalysisOutput | None`) may only veto it, never originate or upgrade
it. Every merge decision is recorded in the returned rationale dict, which
becomes `Recommendation.rationale` (docs/architecture/02-data-model.md).
"""

from __future__ import annotations

from typing import Any

from personaltrade.core.config import RecommendationConfig
from personaltrade.core.enums import RecommendationAction, SignalDirection
from personaltrade.intelligence.analysis.schema import AIAnalysisOutput

#: Ordinal conviction scale, low to high — used both to gate a veto against
#: `RecommendationConfig.veto_conviction_threshold` and to rank recommendations.
CONVICTION_SCORE: dict[str, int] = {"low": 1, "medium": 2, "high": 3}


def deterministic_action(direction: SignalDirection, position_qty: int) -> RecommendationAction:
    """The action implied by a strategy Signal alone, given the current
    position — no AI involved. Mirrors the risk engine's own "opening while
    already in one is rejected" reading of a same-direction signal (ADR-018):
    a LONG/SHORT signal while already positioned that way recommends HOLD,
    not a second BUY/SELL. EXIT resolves to whichever side actually closes
    the open position; EXIT while flat has nothing to act on (AVOID).
    """
    if direction == SignalDirection.LONG:
        return RecommendationAction.HOLD if position_qty > 0 else RecommendationAction.BUY
    if direction == SignalDirection.SHORT:
        return RecommendationAction.HOLD if position_qty < 0 else RecommendationAction.SELL
    # EXIT
    if position_qty > 0:
        return RecommendationAction.SELL
    if position_qty < 0:
        return RecommendationAction.BUY
    return RecommendationAction.AVOID


def merge_recommendation(
    direction: SignalDirection,
    position_qty: int,
    ai_output: AIAnalysisOutput | None,
    cfg: RecommendationConfig,
) -> tuple[RecommendationAction, dict[str, Any]]:
    """Apply AI as a veto layer over the deterministic action.

    `ai_output=None` (AI disabled, budget-exhausted, or a provider/schema
    failure for this instrument) degrades to the deterministic action
    unchanged — docs/architecture/05-ai-data-flow.md's "still produces
    deterministic recommendations" outage behavior, tested directly by
    ROADMAP M15's AI-outage degradation test.
    """
    base_action = deterministic_action(direction, position_qty)
    rationale: dict[str, Any] = {
        "signal_direction": direction.value,
        "position_qty": position_qty,
        "deterministic_action": base_action.value,
        "ai": None,
    }

    if ai_output is None:
        rationale["final_action"] = base_action.value
        return base_action, rationale

    rationale["ai"] = {
        "stance": ai_output.stance,
        "conviction": ai_output.conviction,
        "news_impact": ai_output.news_impact,
        "summary": ai_output.summary,
    }

    final_action = base_action
    veto_eligible = (
        CONVICTION_SCORE[ai_output.conviction] >= CONVICTION_SCORE[cfg.veto_conviction_threshold]
    )
    if veto_eligible:
        if base_action == RecommendationAction.BUY and ai_output.news_impact == "negative":
            final_action = RecommendationAction.HOLD
            rationale["veto"] = (
                f"AI conviction={ai_output.conviction} negative news_impact downgraded BUY to HOLD"
            )
        elif base_action == RecommendationAction.SELL and ai_output.news_impact == "positive":
            final_action = RecommendationAction.HOLD
            rationale["veto"] = (
                f"AI conviction={ai_output.conviction} positive news_impact downgraded SELL to HOLD"
            )

    rationale["final_action"] = final_action.value
    return final_action, rationale


#: Actionable recommendations sort ahead of HOLD/AVOID (docs/architecture/
#: 05-ai-data-flow.md "AI may ... rank"); ties within a tier break by
#: conviction (AI-absent scores 0, sorting last within its tier).
_ACTION_RANK_TIER: dict[RecommendationAction, int] = {
    RecommendationAction.BUY: 0,
    RecommendationAction.SELL: 0,
    RecommendationAction.HOLD: 1,
    RecommendationAction.AVOID: 2,
}


def rank_sort_key(
    action: RecommendationAction, ai_output: AIAnalysisOutput | None
) -> tuple[int, int]:
    conviction = CONVICTION_SCORE.get(ai_output.conviction, 0) if ai_output is not None else 0
    return (_ACTION_RANK_TIER[action], -conviction)
