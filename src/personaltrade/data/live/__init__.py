"""Live feed pipeline (ROADMAP M10): provider-agnostic candle aggregation,
staleness detection, and orchestration on top of `MarketDataProvider.stream_quotes()`
(data/providers/base.py). Reconnection is the provider's own concern (see
data/providers/upstox.py) — everything here consumes an already-reconnecting
`AsyncIterator[Quote]` and never touches transport details.
"""
