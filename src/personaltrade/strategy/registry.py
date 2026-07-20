"""Strategy registry: resolve a friendly name (or a dotted module:ClassName
escape hatch) to a Strategy class. Explicit and hardcoded, not dynamically
discovered — three reference strategies is not enough to justify plugin
machinery (Rule: no premature optimization). Add new strategies here as they
ship.
"""

from __future__ import annotations

import importlib

from personaltrade.core.errors import PersonalTradeError
from personaltrade.strategy.base import Strategy
from personaltrade.strategy.strategies.ema_atr_stop import EMAAtrStopStrategy
from personaltrade.strategy.strategies.rsi_mean_reversion import RSIMeanReversionStrategy
from personaltrade.strategy.strategies.sma_crossover import SMACrossoverStrategy


class UnknownStrategy(PersonalTradeError):
    """No registered strategy matches the given name, and it isn't a valid dotted path."""


_STRATEGIES: dict[str, type[Strategy]] = {
    SMACrossoverStrategy.name: SMACrossoverStrategy,
    EMAAtrStopStrategy.name: EMAAtrStopStrategy,
    RSIMeanReversionStrategy.name: RSIMeanReversionStrategy,
}


def list_strategies() -> list[str]:
    return sorted(_STRATEGIES)


def get_strategy_class(name: str) -> type[Strategy]:
    try:
        return _STRATEGIES[name]
    except KeyError:
        raise UnknownStrategy(
            f"unknown strategy {name!r}; known strategies: {list_strategies()}"
        ) from None


def resolve_strategy_class(ref: str) -> type[Strategy]:
    """Resolve a registry name (e.g. "sma_crossover") or a "module:ClassName" dotted path.

    The dotted-path form is the escape hatch for strategies under development
    that aren't registered yet — the same mechanism M6 used before this
    registry existed.
    """
    if ":" not in ref:
        return get_strategy_class(ref)

    module_name, _, class_name = ref.partition(":")
    if not class_name:
        raise UnknownStrategy(f"expected module:ClassName, got {ref!r}")
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise UnknownStrategy(f"could not import module {module_name!r}: {exc}") from exc
    try:
        strategy_cls: type[Strategy] = getattr(module, class_name)
    except AttributeError as exc:
        raise UnknownStrategy(f"module {module_name!r} has no attribute {class_name!r}") from exc
    return strategy_cls
