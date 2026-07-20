from __future__ import annotations

import pytest

from personaltrade.strategy.registry import (
    UnknownStrategy,
    get_strategy_class,
    list_strategies,
    resolve_strategy_class,
)
from personaltrade.strategy.strategies.ema_atr_stop import EMAAtrStopStrategy
from personaltrade.strategy.strategies.rsi_mean_reversion import RSIMeanReversionStrategy
from personaltrade.strategy.strategies.sma_crossover import SMACrossoverStrategy


class TestListAndGet:
    def test_list_strategies_sorted(self) -> None:
        assert list_strategies() == ["ema_atr_stop", "rsi_mean_reversion", "sma_crossover"]

    def test_get_by_name(self) -> None:
        assert get_strategy_class("sma_crossover") is SMACrossoverStrategy
        assert get_strategy_class("ema_atr_stop") is EMAAtrStopStrategy
        assert get_strategy_class("rsi_mean_reversion") is RSIMeanReversionStrategy

    def test_get_unknown_name_raises_with_known_list(self) -> None:
        with pytest.raises(UnknownStrategy, match="unknown strategy") as exc_info:
            get_strategy_class("nope")
        assert "sma_crossover" in str(exc_info.value)


class TestResolve:
    def test_resolve_registry_name(self) -> None:
        assert resolve_strategy_class("sma_crossover") is SMACrossoverStrategy

    def test_resolve_dotted_path(self) -> None:
        resolved = resolve_strategy_class(
            "personaltrade.strategy.strategies.sma_crossover:SMACrossoverStrategy"
        )
        assert resolved is SMACrossoverStrategy

    def test_resolve_dotted_path_to_a_different_class_than_the_registry_default(self) -> None:
        # proves the dotted-path escape hatch actually imports fresh, not
        # silently falling back to a registry lookup
        resolved = resolve_strategy_class(
            "personaltrade.strategy.strategies.ema_atr_stop:EMAAtrStopStrategy"
        )
        assert resolved is EMAAtrStopStrategy

    def test_malformed_dotted_path_empty_class_name(self) -> None:
        with pytest.raises(UnknownStrategy, match="module:ClassName"):
            resolve_strategy_class("some.module:")

    def test_unimportable_module(self) -> None:
        with pytest.raises(UnknownStrategy, match="could not import module"):
            resolve_strategy_class("no.such.module:Whatever")

    def test_module_missing_the_named_class(self) -> None:
        with pytest.raises(UnknownStrategy, match="has no attribute"):
            resolve_strategy_class("personaltrade.strategy.strategies.sma_crossover:NoSuchClass")

    def test_colonless_unknown_name_is_a_registry_lookup_not_a_path_error(self) -> None:
        with pytest.raises(UnknownStrategy, match="unknown strategy"):
            resolve_strategy_class("totally_made_up")
