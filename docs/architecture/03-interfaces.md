# 03 · Replaceable Interfaces

Every external dependency and every swappable internal component sits behind one of these
interfaces (CLAUDE.md Rule 7). Implementations are selected **by config only** — no code change to
swap. Signatures below are the design contract; exact types are finalized in code at their milestone.

## Broker

```python
class Broker(Protocol):
    """Paper and Upstox implement this identically. Selected by config `trading.broker`."""

    def place_order(self, order: OrderRequest) -> OrderAck: ...          # client_order_id in, broker_order_id out
    def cancel_order(self, client_order_id: str) -> None: ...
    def get_order_status(self, client_order_id: str) -> OrderStatus: ...
    def get_positions(self) -> list[BrokerPosition]: ...                 # broker truth, for reconciliation
    def get_funds(self) -> Funds: ...
    def stream_order_updates(self) -> AsyncIterator[OrderUpdate]: ...    # fills, rejections, cancels
```

Rules: the orchestrator is the **only** caller of `place_order`, and only with a
`RiskEngine`-approved order. Implementations never raise on business rejections — they return typed
rejection statuses; exceptions are reserved for transport failures (retryable).

## MarketDataProvider

```python
class MarketDataProvider(Protocol):
    def get_instruments(self) -> list[Instrument]: ...
    def get_historical_candles(self, key: str, interval: Interval,
                               from_: date, to: date) -> DataFrame: ...
    async def stream_quotes(self, keys: list[str]) -> AsyncIterator[Quote]: ...
```

Implementations: `UpstoxMarketData` (historical + websocket), `ReplayMarketData` (recorded data for
tests/backtests). Candle aggregation (tick→bar) is provider-independent code in `data/live/`.

## LLMProvider

```python
class LLMProvider(Protocol):
    """Claude first; GPT/others later. Selected by config `ai.provider` + `ai.model`."""

    def analyze[T: BaseModel](self, *, system: str, user_content: str,
                              schema: type[T], max_tokens: int) -> LLMResult[T]: ...
    # LLMResult carries: parsed output T, raw text, token usage, cost, model id
```

Rules: callers pass a pydantic schema; the provider guarantees the return parses or raises
`LLMOutputInvalid` (never returns free text as if valid). The Anthropic implementation uses
`client.messages.parse()` with structured outputs and prompt caching on the stable system prompt.
No caller outside `intelligence/` may import a concrete provider.

## NewsProvider

```python
class NewsProvider(Protocol):
    def fetch(self, since: datetime) -> list[RawNewsItem]: ...
```

Multiple registered providers run on a schedule; dedup + tagging is shared pipeline code.

## Strategy

```python
class Strategy(Protocol):
    """Identical contract in backtest, paper, and live (Rule 11)."""

    name: str
    params_schema: type[BaseModel]

    def warmup_bars(self) -> int: ...
    def on_candle(self, ctx: StrategyContext) -> Signal | None: ...
    # StrategyContext: candle history window, indicator accessor, current position — read-only.
```

Rules: strategies are pure decision functions — no I/O, no order placement, no sizing (risk engine
sizes), no wall-clock access (time comes from the candle, so backtests are honest).

## Repositories (storage swap seam)

`OrderRepository`, `TradeRepository`, `PositionRepository`, `CandleStore`, etc. — thin interfaces
over SQLite/Parquet so a future Postgres/Timescale migration touches only `data/store/` and
`core/repo/` implementations.

## Notifier (M19)

```python
class Notifier(Protocol):
    def send(self, level: AlertLevel, message: str) -> None: ...
```

Telegram first; email later. Kill-switch trips and fills always notify.

## Wiring

A single composition root (`orchestrator/wiring.py`) reads config and constructs concrete
implementations; everything else receives interfaces via constructor injection. No service-locator,
no globals — this is what keeps modules independently testable.
