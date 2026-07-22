"""intelligence/recommendation/merge.py: the deterministic gate (docs/
architecture/05-ai-data-flow.md) — a recommendation's action always has a
deterministic basis; AI may only veto it, and only at/above the configured
conviction threshold. Table-driven per ROADMAP M15's own testing note.
"""

from __future__ import annotations

import pytest

from personaltrade.core.config import RecommendationConfig
from personaltrade.core.enums import RecommendationAction, SignalDirection
from personaltrade.intelligence.analysis.schema import AIAnalysisOutput
from personaltrade.intelligence.recommendation.merge import (
    deterministic_action,
    merge_recommendation,
    rank_sort_key,
)


def _ai(conviction: str = "high", news_impact: str = "none") -> AIAnalysisOutput:
    return AIAnalysisOutput(
        stance="neutral",
        conviction=conviction,  # type: ignore[arg-type]
        key_factors=[],
        risks=[],
        news_impact=news_impact,  # type: ignore[arg-type]
        summary="test",
    )


class TestDeterministicAction:
    @pytest.mark.parametrize(
        ("direction", "position_qty", "expected"),
        [
            (SignalDirection.LONG, 0, RecommendationAction.BUY),
            (SignalDirection.LONG, -5, RecommendationAction.BUY),
            (SignalDirection.LONG, 5, RecommendationAction.HOLD),
            (SignalDirection.SHORT, 0, RecommendationAction.SELL),
            (SignalDirection.SHORT, 5, RecommendationAction.SELL),
            (SignalDirection.SHORT, -5, RecommendationAction.HOLD),
            (SignalDirection.EXIT, 5, RecommendationAction.SELL),
            (SignalDirection.EXIT, -5, RecommendationAction.BUY),
            (SignalDirection.EXIT, 0, RecommendationAction.AVOID),
        ],
    )
    def test_table(
        self, direction: SignalDirection, position_qty: int, expected: RecommendationAction
    ) -> None:
        assert deterministic_action(direction, position_qty) == expected


class TestMergeRecommendation:
    def test_no_ai_output_keeps_deterministic_action(self) -> None:
        action, rationale = merge_recommendation(
            SignalDirection.LONG, 0, None, RecommendationConfig()
        )
        assert action == RecommendationAction.BUY
        assert rationale["ai"] is None
        assert rationale["deterministic_action"] == "BUY"
        assert rationale["final_action"] == "BUY"
        assert "veto" not in rationale

    def test_high_conviction_negative_news_vetoes_buy_to_hold(self) -> None:
        ai = _ai(conviction="high", news_impact="negative")
        action, rationale = merge_recommendation(
            SignalDirection.LONG, 0, ai, RecommendationConfig()
        )
        assert action == RecommendationAction.HOLD
        assert "veto" in rationale
        assert rationale["final_action"] == "HOLD"

    def test_high_conviction_positive_news_vetoes_sell_to_hold(self) -> None:
        ai = _ai(conviction="high", news_impact="positive")
        action, _rationale = merge_recommendation(
            SignalDirection.SHORT, 0, ai, RecommendationConfig()
        )
        assert action == RecommendationAction.HOLD

    def test_low_conviction_negative_news_does_not_veto_buy(self) -> None:
        ai = _ai(conviction="low", news_impact="negative")
        action, rationale = merge_recommendation(
            SignalDirection.LONG, 0, ai, RecommendationConfig()
        )
        assert action == RecommendationAction.BUY
        assert "veto" not in rationale

    def test_medium_conviction_does_not_veto_at_default_high_threshold(self) -> None:
        ai = _ai(conviction="medium", news_impact="negative")
        action, _rationale = merge_recommendation(
            SignalDirection.LONG, 0, ai, RecommendationConfig()
        )
        assert action == RecommendationAction.BUY

    def test_medium_conviction_vetoes_when_threshold_lowered(self) -> None:
        ai = _ai(conviction="medium", news_impact="negative")
        cfg = RecommendationConfig(veto_conviction_threshold="medium")
        action, _rationale = merge_recommendation(SignalDirection.LONG, 0, ai, cfg)
        assert action == RecommendationAction.HOLD

    def test_matching_direction_news_never_vetoes(self) -> None:
        """Negative news doesn't touch a SELL, positive news doesn't touch a BUY."""
        ai_negative = _ai(conviction="high", news_impact="negative")
        action, _rationale = merge_recommendation(
            SignalDirection.SHORT, 0, ai_negative, RecommendationConfig()
        )
        assert action == RecommendationAction.SELL

        ai_positive = _ai(conviction="high", news_impact="positive")
        action, _rationale = merge_recommendation(
            SignalDirection.LONG, 0, ai_positive, RecommendationConfig()
        )
        assert action == RecommendationAction.BUY

    def test_ai_only_touches_buy_or_sell_never_originates_hold_avoid(self) -> None:
        """AVOID (EXIT while flat, no deterministic basis to act) is untouched
        by AI regardless of conviction/news_impact — AI can only veto an
        actionable BUY/SELL, never turn a non-action into one."""
        ai = _ai(conviction="high", news_impact="negative")
        action, _rationale = merge_recommendation(
            SignalDirection.EXIT, 0, ai, RecommendationConfig()
        )
        assert action == RecommendationAction.AVOID


class TestRankSortKey:
    def test_actionable_ranks_ahead_of_hold_and_avoid(self) -> None:
        buy_key = rank_sort_key(RecommendationAction.BUY, None)
        hold_key = rank_sort_key(RecommendationAction.HOLD, None)
        avoid_key = rank_sort_key(RecommendationAction.AVOID, None)
        assert buy_key < hold_key < avoid_key

    def test_higher_conviction_ranks_first_within_tier(self) -> None:
        high_key = rank_sort_key(RecommendationAction.BUY, _ai(conviction="high"))
        low_key = rank_sort_key(RecommendationAction.BUY, _ai(conviction="low"))
        no_ai_key = rank_sort_key(RecommendationAction.BUY, None)
        assert high_key < low_key < no_ai_key
