"""The Trade Orchestrator (ROADMAP M11): the loop that wires candle -> strategy
-> risk -> broker -> persistence, driven by `core.events.EventBus`. Owns the
invariant that risk is the only path to a broker (CLAUDE.md Rule 14) — nothing
here calls `Broker.place_order()` except with a `risk.engine.ApprovedOrder`.
"""
