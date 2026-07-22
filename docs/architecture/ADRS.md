# Architecture Decision Records

One section per decision. Status: **Accepted** unless noted. New decisions append here (or split
into `adr/NNN-*.md` files if this grows unwieldy).

---

## ADR-001 · Modular monolith, not microservices

**Context.** A single-user trading platform with sub-second (not microsecond) latency needs and
one operator. Rules demand replaceability, testability, low cost.
**Options.** (a) Microservices + message broker — independent scaling/deploys, but network failure
modes, ops burden, infra cost, and no benefit at N=1 users. (b) Single script — cheap but
unmaintainable, violates every rule. (c) **Modular monolith** — strict interface boundaries,
in-process event bus, one deployable.
**Decision.** (c). Replaceability comes from interfaces + a composition root, not from HTTP.
**Consequences.** Near-zero ops cost; simple debugging; a module can be extracted later because
boundaries already exist. Discipline required: interface-only imports across modules (reviewed).

## ADR-002 · Python 3.12+, FastAPI, uv

**Context.** User rules mandate deterministic Python for math; ecosystem fit matters
(pandas/numpy, official Upstox SDK, Anthropic SDK).
**Decision.** Python 3.12+; `uv` for env/deps (fast, lockfile); FastAPI for dashboard/API (typed,
async, websocket support); APScheduler for in-process scheduling.
**Consequences.** Single-language codebase; GIL is irrelevant at our throughput; if a hot loop
ever matters, numpy vectorization first, then targeted optimization (Rule: no premature optimization).

## ADR-003 · SQLite for state, Parquet + DuckDB for market data

**Context.** Two very different workloads: transactional order/position state vs. columnar scans
over millions of candles.
**Options.** (a) Postgres+Timescale for everything — capable but a server to run, against the
low-ops goal. (b) SQLite for everything — candle analytics would be slow and bloat the DB.
(c) **Split:** SQLite (WAL) for state, Parquet files + DuckDB for candles/backtests.
**Decision.** (c), both behind repository interfaces.
**Consequences.** Zero database ops; `data/` is one backup unit; backtests scan Parquet at native
columnar speed. If concurrency needs ever outgrow SQLite, the repository seam takes us to Postgres
without touching business logic.

## ADR-004 · In-process event bus, no external broker

**Context.** Components must be decoupled (candle → strategy → risk → execution) but all live in
one process.
**Decision.** Lightweight typed pub/sub in `core.events`; synchronous dispatch by default; slow
work goes to scheduled jobs. No Kafka/Redis/RabbitMQ.
**Consequences.** Deterministic ordering, trivial testing (assert on published events), zero infra.
If a consumer ever needs true parallelism, asyncio tasks first; external brokers only with a new ADR.

## ADR-005 · LLM is advisory-only behind a hard deterministic gate

**Context.** Rules 6/10/12: AI for reasoning and explanation, never math or execution; LLM output
is untrusted (hallucination + prompt injection via news).
**Decision.** `LLMProvider` returns schema-validated pydantic objects only (structured outputs via
`messages.parse`); the schema contains no numeric trading fields; the Recommendation Engine merges
AI with deterministic signals under config-weighted rules; execution never reads recommendations.
Every call audited (inputs snapshot, hash, tokens, cost).
**Consequences.** A fully compromised model output still cannot trade. AI outage degrades to
deterministic-only operation. Auditability answers "why did it recommend this?" forever.

## ADR-006 · One strategy contract across backtest, paper, live

**Context.** The classic failure: a strategy backtests well, then gets rewritten for live and the
live version is a different strategy.
**Decision.** `Strategy.on_candle(ctx)` is the only contract; the backtester, paper loop, and live
loop all drive it identically; strategies are pure (no I/O, no clock, no sizing). Cost/slippage
model is one shared module used by both backtester and paper broker.
**Consequences.** Backtest results are evidence about the exact code that will trade. Constrains
strategy authors (no intraday external calls) — acceptable; anything needing external data becomes
a data-pipeline feature feeding the context instead.

## ADR-007 · Orders are idempotent via client_order_id + persisted state machine

**Context.** Crash between "decided to order" and "broker confirmed" is the most dangerous moment
in any trading system.
**Decision.** Generate and persist `client_order_id` before any broker call; persist every state
transition append-only; on restart reconcile against the broker (broker wins); handlers idempotent.
**Consequences.** Kill -9 at any moment is recoverable without double orders. Slight write overhead
per transition — irrelevant at our volume, priceless in an incident.

## ADR-008 · Staged live enablement with a two-key config gate

**Context.** Rule 11 (paper first) needs a mechanism, not a promise.
**Decision.** Live orders require both `trading.mode: live` and `trading.live_orders_enabled: true`;
Upstox integration ships read-only first; order placement arrives later behind the gate with a
dry-run mode; enablement recorded as an ADR after the M18 soak review.
**Consequences.** No accidental live trading via a single typo; the audit trail shows exactly when
and why live was enabled.

## ADR-009 · Anthropic Claude as first LLM implementation

**Context.** Need one concrete `LLMProvider` to start; interface guarantees swappability.
**Decision.** `anthropic` Python SDK; default model `claude-opus-4-8` ($5/$25 per MTok) for
analysis quality at personal-scale volume; optional `claude-haiku-4-5` pre-filter for news triage
(config, off by default); prompt caching on the stable system prompt; model id always from config.
**Consequences.** Best-in-class structured outputs + tooling now; a GPT/other provider is a new
class implementing `LLMProvider` plus a config change, nothing else.

## ADR-010 · Money stored as canonical TEXT Decimals in SQLite

**Context.** SQLite has no exact decimal type; SQLAlchemy `Numeric` round-trips through float
(silent precision loss — unacceptable for money). Deferred from ADR-003.
**Options.** (a) Integer paise — exact and fast, but conversion boilerplate everywhere and awkward
for sub-paisa values (computed charges, per-share cost fractions). (b) **TEXT via a
`MoneyText` TypeDecorator** — exact, human-readable in the DB, floats rejected at the bind
boundary. SQL-side arithmetic/sorting on money is lost, but aggregation happens in Python/DuckDB.
**Decision.** (b). Companion `UTCDateTime` decorator enforces tz-aware UTC on every timestamp.
**Consequences.** `Decimal` end-to-end with fail-fast on float; migrations carry plain
`sa.String(40)`/`sa.DateTime()` (wire types), keeping Alembic files free of app imports.

## ADR-011 · Candle arrays are float64; transactional money stays Decimal

**Context.** CLAUDE.md mandates Decimal for money. Applied to OHLCV arrays this would force
object-dtype pandas columns — orders of magnitude slower, incompatible with numpy/DuckDB
vectorization, and pointless: indicators are statistical, not accounting.
**Decision.** Market-data frames (candles, indicator inputs/outputs) use float64. The Decimal rule
applies to everything transactional: order prices, fills, costs, P&L, config. Any price derived
from float analytics is quantized to tick size as Decimal at the risk/order boundary.
**Consequences.** Fast vectorized research stack; a clearly named boundary (risk engine) where
floats become money. Relative float64 error (~1e-16) is far below one paisa at NSE price scales.

## ADR-012 · Indicator conventions: SMA-seeded EMA, Wilder RSI/ATR, population-std Bollinger

**Context.** Every popular TA library disagrees on warm-up seeding, and getting it wrong is a
silent correctness bug (a strategy trained on one convention drifts against a broker computing
another). A convention had to be picked and locked down before any strategy code depends on it.
**Decision.** EMA seeds with the SMA of the first `period` closes (TA-Lib convention). RSI and ATR
use Wilder smoothing (`(prev*(n-1)+x)/n`), seeded with a simple average of the first `period`
values — the original Wilder (1978) method, and what most Indian broker platforms display. Bollinger
uses population standard deviation (ddof=0). VWAP anchors per IST trading session (Rule 16).
**Consequences.** Documented once in `indicators/__init__.py`, verified three ways per indicator:
hand-computed micro goldens (values a reviewer can check with a calculator), an independently
written scalar reference implementation, and a frozen golden file computed from real NSE data
(`tests/golden/`). Streaming (incremental) classes are tested for exact equivalence with the batch
functions, so live and backtest code paths can never silently diverge (Rule 11/ADR-006).

## ADR-013 · LLM backend starts on Amazon Bedrock, config-switchable to direct API

> Renumbered from a duplicate "ADR-011" (collided with the candle-dtype ADR above) when M6
> appended ADR-014/015 — content unchanged, only the number and this note were fixed.

**Context.** Amends ADR-009. User holds an AWS account with free-tier credits (~$100–200,
expiring ~6 months after account creation) that price Claude on Bedrock identically to the
direct Anthropic API. Using them costs nothing extra and the `LLMProvider` interface already
isolates the SDK client from the rest of the codebase.
**Options.** (a) Direct Anthropic API only, as ADR-009 originally specified — simplest, no AWS
model-access setup, same-day new-model availability, but forgoes free credits already available.
(b) Bedrock only — free tokens now, but no server-side tools (web search/code execution/Files
API), no Message Batches, and region-dependent model access. (c) **Bedrock first, direct API as
the fallback/successor, selected by config/environment presence — no code branching.**
**Decision.** (c). `LLMProvider`'s Anthropic implementation picks its client at construction time:
`AnthropicBedrock(api_key=AWS_BEARER_TOKEN_BEDROCK, aws_region=...)` when a Bedrock bearer token
is configured, else the direct `Anthropic()` client. Model IDs are resolved per-backend (e.g.
`claude-opus-4-8` direct vs. an `anthropic.`-prefixed / cross-region-profile ID on Bedrock) —
never hardcoded once, since Bedrock and direct API IDs differ. Switch to direct-API-only once
Bedrock credits are exhausted or a feature gap (Files API, batches, server-side tools) is needed.
**Consequences.** Free usage during early development and M14 buildout; a config change (not a
code change) reverts to the direct API. Requires: confirming Anthropic model access is granted in
the Bedrock console for the target region before M14 starts; verifying the free-credit expiry
date doesn't lapse before M14; the Bedrock API key must never be committed (same `.env`-only rule
as the direct key) and any key pasted outside `.env` must be treated as compromised and rotated.

## ADR-014 · Indian equity cost model: configurable Decimal rates, not hardcoded constants

**Context.** Rule 11 requires backtests to net out realistic Indian trading costs before any
capital is risked; Rule 9 requires deterministic, non-LLM math. Government/exchange rates (STT,
stamp duty, SEBI/exchange charges) and broker brokerage both change periodically and vary by
broker — hardcoding today's numbers would silently go stale.
**Options.** (a) Hardcode current rates as constants — simplest, but stale rates fail silently
(a backtest keeps running, just with wrong economics) and there's no single place to update them.
(b) **Configurable Decimal rates in `CostConfig`** (`config/default.yaml` under `costs:`), applied
by `backtest/costs.py::calculate_costs()` — brokerage (percentage, flat-capped), STT
(delivery: both legs; intraday: sell leg only), stamp duty (buy leg only, delivery/intraday
rates differ), exchange transaction charges, SEBI charges, and GST (levied only on
brokerage + exchange + SEBI, never on STT/stamp duty).
**Decision.** (b), documented in `CostConfig`'s docstring and `config/default.yaml` comments as
"verify against your broker's current rate card before going live" — the shape of the model
(which components apply to which leg/segment) is what protects the relative edge-vs-no-edge
determination; exact rates only need to be right enough for research/paper trading, and Rule 11's
paper-soak gate catches drift before real capital is at risk.
**Consequences.** One place to update rates without touching engine code; every `Trade`/
`BacktestTrade` row carries the full component breakdown (not a single "fees" blob) so analytics
can attribute P&L drag correctly. Verified via hand-computed golden test cases
(`tests/test_backtest_costs.py`) cross-checked against independently-typed arithmetic.

## ADR-015 · Backtest execution: structural look-ahead prevention, next-bar fills, single position

**Context.** The backtester (M6) is the platform's most consequential correctness surface — a
look-ahead bug or optimistic fill model produces a strategy that looks profitable in backtest and
loses money live, exactly the failure Rule 12 exists to prevent. "Don't let the strategy peek
ahead" is easy to say and easy to violate by accident (e.g. precomputing indicators is safe only
if they're causal; a careless API could still expose a future index).
**Decision.**
1. **Structural, not disciplined, look-ahead prevention.** `StrategyContext` is rebuilt fresh per
   bar; `candles` is a slice ending at the current index and `IndicatorView.value()`/`.window()`
   take no index parameter — there is no method signature through which a strategy could request
   a future bar, even by mistake. Indicators are precomputed once per run (not per bar, for
   intraday-scale performance) — safe only because every `personaltrade.indicators` batch function
   is provably causal (rolling windows, forward-recursive EMA/Wilder, session-anchored cumsum);
   the engine additionally waits for every declared indicator to stop returning NaN before calling
   `on_candle()`, regardless of what the strategy's own `warmup_bars()` claims.
2. **Next-bar-open fills with adverse slippage** (Rule 12): a signal at bar *i* fills at bar
   *i+1*'s open, adjusted against the trader by a configurable `slippage_bps`, before the full cost
   stack (ADR-014) applies. A signal on the final bar has no next bar to fill on and is recorded as
   unexecuted, not silently dropped or back-filled.
3. **One position at a time; no same-bar reversal.** LONG/SHORT/EXIT map onto a fixed transition
   table (FLAT+LONG→open long, FLAT+SHORT→open short, LONG+EXIT→close, SHORT+EXIT→cover); a signal
   that would reverse directly (LONG while SHORT or vice versa) is ignored with a logged warning —
   a strategy wanting to reverse must emit EXIT on one bar and the new direction on a later one.
   Avoids same-bar multi-leg fills and their attendant cost/slippage double-counting ambiguity.
4. **`avg_price` always includes that leg's own transaction costs**, folded into the per-share cost
   basis at open — so `ExecutedTrade.realized_pnl` on a closing trade is the true, complete
   round-trip P&L (entry costs + exit costs), not just the exit leg in isolation. This is what
   `win_rate`/`expectancy`/`profit_factor` need to mean what a trader actually cares about: "was
   this trade profitable after everything."
**Consequences.** The defining test (`tests/test_backtest_engine.py::TestNoLookAheadBias`) proves
results for bars `[0, split)` are byte-identical whether or not wildly different (corrupted, price
10x'd, reversed) data exists afterward — the ROADMAP M6 acceptance criterion. Position sizing
(`backtest/sizing.py::FixedFractionalSizer`) is an explicit placeholder superseded by the real Risk
Engine at M8 without changing this engine's interface. Multi-instrument portfolio backtests
(`backtest/run.py`) simulate each symbol independently under an equal capital split — no
cross-symbol correlation or exposure limits, deliberately deferred to M8's Risk Engine scope.

## ADR-016 · Stateful strategies: fill-price-anchored state, cleared on flat, isolated per symbol

**Context.** M6's only reference strategy (SMA crossover) was stateless — every decision derived
purely from `ctx.indicators`/`ctx.position`. M7's `EMAAtrStopStrategy` needs to remember an
ATR-based stop level across bars while a position is open, which introduces two new failure modes
`Strategy.on_candle()`'s pure-function contract (ADR-006) doesn't rule out by itself: anchoring the
stop to the wrong price, and one strategy instance leaking state across symbols in a multi-symbol
run (`backtest/run.py::run_backtest_for_symbols`) or across independent runs.
**Decision.**
1. **The stop anchors to the actual fill price, not the signal-time close.** A strategy only learns
   its real entry price one bar later, via `ctx.position.avg_price` — which already reflects
   slippage and the entry leg's transaction costs (ADR-015 point 4). `self._stop` is set lazily on
   the first bar the strategy observes itself already in the position, never at signal emission
   time. Verified by a discriminating test
   (`tests/test_strategy_ema_atr_stop.py::test_stop_anchors_to_actual_fill_price_not_signal_close`)
   constructed so the wrong basis and the right basis produce different pass/fail outcomes, not
   just different numbers.
2. **State unconditionally clears whenever the position is flat**, not just on a recognized exit —
   covering both a stop/cross-down exit and a fresh symbol's very first bar. This makes a strategy
   instance self-healing: reusing one across a new, flat-starting run cannot leak a stale stop.
3. **`backtest/run.py` additionally constructs a fresh strategy instance per symbol** via
   `Strategy.clone()` (ADR-017), as defense in depth on top of (2) — a strategy that forgets to
   self-heal must not be able to silently corrupt a later symbol's run. Proven by a purpose-built
   non-self-healing test double (`tests/factories.py::LeakyOnceStrategy`, which emits its one signal
   only on an instance's very first `on_candle` call ever) in
   `tests/test_backtest_run.py::TestFreshStrategyInstancePerSymbol` — this fails immediately if the
   orchestration-level guarantee regresses, independent of whether any real strategy happens to
   self-heal correctly.
**Consequences.** Two independent safety nets (self-healing + fresh-instance orchestration) instead
of trusting either alone; a new stateful strategy that forgets rule (2) is still safe in multi-symbol
runs, just wasteful of instances. `RSIMeanReversionStrategy` (M7's other new strategy) has no
per-position state at all, so it only needs to exist correctly, not participate in this discipline.

## ADR-017 · Strategy construction: `clone()` on instances, a cast-isolated helper for the registry

**Context.** M7 needs to construct `Strategy` instances two different ways that the `Strategy`
Protocol (ADR-006) doesn't describe, since Protocols only specify what an *instance* looks like, not
how one is built: (a) `backtest/run.py` needs a fresh instance with the same params as an existing
one (ADR-016), and (b) the strategy registry (`strategy/registry.py`) resolves a runtime string to a
*class*, which the CLI and `backtest/sweep.py` then construct from JSON/grid-supplied params after
validating them against that class's own `params_schema`. mypy strict flagged both as "too many
arguments" — `type[Strategy]` has no declared constructor.
**Options.** (a) Add `__init__` to the `Strategy` Protocol — rejected: Protocol parameter types are
checked contravariantly, so every concrete strategy's `__init__(self, params: OwnParams | None)`
would need to accept the Protocol's declared type or wider, forcing every strategy to accept plain
`BaseModel` and defeating pydantic's per-strategy validation. (b) A class-side `StrategyFactory`
Protocol with `__call__` — tried first, but mypy cannot match a `ClassVar`-qualified protocol member
against a class object at all (a documented mypy limitation, not something wideninig the type
signature works around: https://github.com/python/mypy/issues/11515), and dropping `ClassVar` still
leaves the same contravariance rejection as (a) once `__call__`'s parameter type must accept
`BaseModel`. (c) **Split the two use cases.** `Strategy.clone()` (an *instance* method, implemented
per concrete class as `return type(self)(self.params)`) covers (a) — sound and fully static, since
`type(self)` resolves to a known, non-erased type inside each concrete class's own method body, no
Protocol involved. `strategy/base.py::construct_strategy(strategy_cls, params)` covers (b) via one
explicit, documented `cast` at a single call site, rather than a suppression scattered across every
registry/sweep/CLI construction site.
**Decision.** (c). `construct_strategy` is the only place a strategy is constructed from a
dynamically-resolved class; every caller obtains `params` via `strategy_cls.params_schema
.model_validate(...)` immediately beforehand, which is what makes the cast sound in practice (the
class's own schema always produces the params type its `__init__` expects) even though the type
system cannot prove the link between a registry entry's class and its params type statically for a
heterogeneous, string-keyed registry.
**Consequences.** mypy strict is clean with zero blanket `# type: ignore` — the one unprovable
boundary (dynamic registry lookup meeting static construction) is isolated to a single function with
a docstring explaining exactly why it's safe, instead of hidden per-call-site. Adding a fourth
strategy requires no changes to `construct_strategy`, `clone()`'s contract, or either registry/sweep
call site — only the new class's own `__init__`/`clone()`, matching the existing three.

## ADR-018 · Risk Engine: explicit equity/P&L inputs, singleton kill-switch state, shared sizing

**Context.** M8 builds `RiskEngine.evaluate(Signal) -> ApprovedOrder | Rejection` (docs/architecture/
03-interfaces.md, ROADMAP M8) — the sole gate between a Signal and an order (CLAUDE.md Rules 10, 14).
Three design questions had no existing answer: where sizing math should live now that both the
backtester (M6) and the live risk engine need the identical calculation; where "current equity" and
"today's realized P&L" come from when neither a Paper Broker (M9) nor live quotes (M10) exist yet;
and how kill-switch state should be persisted and audited.
**Decisions.**
1. **Position sizing moves to `risk/sizing.py`** (from its M6 placeholder home, `backtest/sizing.py`
   — ADR-015 flagged this move in advance). The backtester now imports `PositionSizer`/
   `FixedFractionalSizer` from `risk/`, so backtest and live size positions with the literal same
   code, never two implementations that could silently drift (Rule 11).
2. **`equity` and `daily_realized_pnl` are explicit parameters to `evaluate()`, not derived
   internally.** Nothing in the codebase can correctly source either yet — no Paper Broker fills
   (M9) for realized P&L, no live quotes (M10) for mark-to-market equity — so any internal
   computation today would be a placeholder that has to be torn out the moment those milestones
   land. An honest explicit input is cheaper than that churn, and it keeps `RiskEngine` a pure,
   trivially unit-testable function of (signal + numeric context + persisted kill-switch/position
   state) *now*, with the future orchestrator (M11) responsible for sourcing real values once the
   components that produce them exist.
3. **Kill-switch state is a singleton row (`KillSwitchState`, id=1), not derived from the event
   log.** Mirrors the `Order`/`OrderEvent` split already used elsewhere in this codebase: one
   mutable "what's true now" row (`tripped`, `reason`, `tripped_at`, `consecutive_errors`) plus an
   append-only `RiskEvent` (kind `KILL_SWITCH`/`KILL_SWITCH_RESET`) audit trail on every trip/reset
   — an O(1) status check instead of scanning history, while still satisfying "persisted, survives
   restart, explicit human reset with a logged reason" (docs/architecture/04-trade-lifecycle.md).
   Trip is idempotent (a second trip while already tripped logs nothing further — the first reason
   is what matters); reset raises `KillSwitchNotTripped` rather than silently no-op-ing, so a reset
   is never accidentally meaningless.
4. **Opening a new position while already in one is rejected (`ALREADY_IN_POSITION`), never
   auto-reversed.** Mirrors the backtest engine's fixed transition table (ADR-015 point 3) exactly —
   a strategy wanting to flip direction must emit EXIT on one evaluation and the new direction on a
   later one — so live/paper/backtest can never disagree about what a same-direction-while-positioned
   or reversal signal means (ADR-006).
5. **Float ref_price is quantized to the instrument's tick size at this boundary**
   (`_to_tick_decimal`, `ROUND_HALF_EVEN`), fulfilling ADR-011's forward reference to "the risk/order
   boundary" as the place float-analytics prices become tick-aligned Decimal money — sizing input
   only, since every order here is MARKET (no limit price to quantize).
6. **Only rejections are logged to `risk_events`, not approvals.** The resulting `Order` row (once
   the orchestrator, M11, creates one) is the approval's audit trail; duplicating it in `risk_events`
   would just be noise. `risk_events` is specifically "what the risk engine blocked and why."
**Consequences.** `RiskEngine` has zero dependency on components that don't exist yet (Paper Broker,
live quotes, orchestrator) while still being fully real and fully tested — 3 already-passing
`RejectionReason`s (`MAX_OPEN_POSITIONS`, `MAX_DAILY_LOSS`, kill-switch) need no rework when M9-M11
land; only their callers gain the ability to compute correct `equity`/`daily_realized_pnl` instead of
supplying them by hand. `pt risk kill-switch status|trip|reset` gives Rule 14's "one-command halt" a
concrete, live-verified CLI surface ahead of the orchestrator that will eventually trip it
automatically via `KillSwitch.record_error()`.

## ADR-019 · Paper Broker: self-contained fills, synchronous latency, shared slippage

**Context.** M9 builds `PaperBroker` (docs/architecture/03-interfaces.md `Broker`, ROADMAP M9), but
it lands *before* the Live Market Data Feed (M10) and the Trade Orchestrator (M11) — the two
components that would normally supply "the current price" and "a loop that drives fills over time."
Building a fully realistic broker (live quotes, a real event loop, genuine resting-order latency)
isn't possible yet; the question was how to build something genuinely correct and useful *today*
without designing something M10/M11 would have to tear out.
**Decisions.**
1. **`QuoteSource` is a new, deliberately narrow Protocol** (`execution/broker.py`) — one method,
   `get_ltp(instrument) -> Decimal | None` — not the richer, async `MarketDataProvider.stream_quotes`
   (M4/M10). `execution/paper/quotes.py::ReplayQuoteSource` is the only implementation until M10: it
   returns the most recently *synced* candle's close via the existing `CandleStore` (M4). Coarse
   (daily-bar granularity today) but a genuine, correct reference price — not a fake one — and
   exactly the seam M10's real live-tick implementation plugs into later with zero changes to
   `PaperBroker` itself.
2. **Fills are driven synchronously, not by a live loop.** `place_order()` attempts a fill
   immediately inline; `check_resting_orders()` is a separate, fully-tested method that re-attempts
   every OPEN/PARTIALLY_FILLED order against the current quote — built and proven correct now, ready
   for M11's orchestrator to call on every new quote/candle tick once a loop exists to call it from.
   Nothing here needs to change when M11 lands; only who calls `check_resting_orders()` and how often.
3. **Simulated latency is a timestamp offset, not real sleeping.** `PaperConfig.latency_ms` shifts
   the *recorded* fill time (`Trade.executed_at`, the fill's `OrderUpdate.at`) forward from an
   injectable `Clock` (`core/clock.py`, new) rather than blocking the call — keeps the whole broker
   synchronous and deterministic in tests (a `ManualClock` test double, not real waiting) while still
   producing realistic-looking audit timestamps.
4. **Slippage is now genuinely shared, not just documented as shared.** `apply_slippage()` moved from
   a `backtest/engine.py`-private function to `backtest/costs.py` (public), alongside the cost model
   ADR-014 already made shared — closing a real gap ADR-015 only asserted in prose ("Backtester and
   paper broker share one cost/slippage model") until this milestone actually built the second
   consumer.
5. **Cash is a new persisted singleton row** (`PaperAccount`, id=1 — same reasoning as
   `KillSwitchState`, ADR-018: genuine incrementally-mutated state, not something safe to re-derive
   from a full trade-history scan) rather than computed from `risk.capital` config plus a lifetime
   trade sum, which would silently go wrong the moment a user edited `risk.capital` after any trades
   already existed. `PositionRepository`/`OrderRepository`'s existing tables need no new columns —
   position average-cost blending and realized P&L accounting mirror `backtest/engine.py`'s
   `_open_or_add`/`_close` math exactly, adapted to a row that's reused across open/close cycles
   (`realized_pnl` accumulates over the row's lifetime) rather than backtest's per-run `_Portfolio`.
6. **Only BUY orders are cash-clamped** (`_clamp_to_cash`, identical mechanism to
   `backtest/engine.py`'s), matching backtest engine's existing scope exactly rather than expanding
   it — no margin/collateral engine exists for opening shorts anywhere in this codebase yet, so SELL
   orders execute at the requested quantity without a funds check, same as before this milestone.
**Consequences.** A user can paper-trade manually via `pt paper order` *today*, against real
(if end-of-day-granularity) market data, with real Indian cost economics and real slippage — not a
toy. Restart-safety falls out of the design rather than needing special-casing: every mutation lives
in existing SQLite tables (`Order`/`OrderEvent`/`Trade`/`Position`) plus the one new `PaperAccount`
row, so a freshly constructed `PaperBroker` on the same DB after a restart sees exactly the same
truth (verified directly: `tests/test_execution_paper_broker.py::TestRestartSafety` disposes and
reconstructs the engine/session entirely between writing and reading back). The one real limitation —
end-of-day-only reference prices until M10 — is explicit and load-bearing in `ReplayQuoteSource`'s
own docstring, not a silent gap.

## ADR-020 · Live Market Data Feed: vendored protobuf, provider-owned reconnect, mock-server testing

**Context.** M10 builds streaming quotes/candles (docs/architecture/03-interfaces.md
`MarketDataProvider.stream_quotes`, ROADMAP M10) — but it lands *before* M17 (Upstox
Integration), which is where the OAuth access-token flow is built. Upstox's real-time feed (i)
requires a valid access token to connect at all, and (ii) is protobuf-only wire format with no JSON
fallback — two facts that shaped every decision below.
**Decisions.**
1. **The official V3 schema is vendored and compiled**, not hand-rolled. Upstox doesn't host a
   clean, direct download of `MarketDataFeedV3.proto`; it was retrieved from a community-published
   mirror cross-checked against Upstox's own docs (field names, oneofs, and the `RequestMode`/
   `MarketStatus` enums all matched independently-fetched documentation) and compiled via
   `grpc_tools.protoc` into `data/providers/proto/market_data_feed_v3_pb2.py` — committed, not
   regenerated at build time, with regeneration instructions in that package's docstring. Generated
   stubs are excluded from strict mypy/ruff (they're not hand-written and regenerating overwrites
   any fixes anyway).
2. **`stream_quotes()` lives directly on `UpstoxMarketData`**, not a separate `UpstoxLiveFeed`
   class — matching 03-interfaces.md's original description ("UpstoxMarketData (historical +
   websocket)") exactly. Historical (M4) and live (M10) are two capabilities of "talking to
   Upstox," not two components.
3. **Reconnection is the provider's own concern, invisible to every caller.** `stream_quotes()` is
   an async *generator* method (the Protocol deliberately omits `async` on the signature — a plain
   `def` returning `AsyncIterator[Quote]` is the correct way to type a method whose calling
   convention is "call synchronously, then `async for`," a distinct thing from a coroutine function
   that must be awaited to get its result). Internally it loops forever: authorize, connect,
   subscribe, decode, and on any transport failure (`OSError`/`WebSocketException`/`MarketDataError`
   — but never `MissingAccessToken`, a config error that's never worth retrying), back off
   (`data/providers/reconnect.py::ReconnectPolicy`, pure exponential-backoff math) and try again.
   Callers (`LiveFeed`) never see a dropped connection, only a brief gap in ticks — verified
   directly by dropping a real connection mid-stream against a local mock server and asserting the
   client transparently reconnects and keeps yielding correct ticks
   (`tests/test_provider_upstox_stream.py::test_reconnects_transparently_after_a_drop`).
4. **`data/live/` (aggregation, staleness, orchestration) is provider-agnostic**, consuming only
   `MarketDataProvider.stream_quotes()` and the shared `Quote` DTO (Rule 7) — it has zero Upstox
   knowledge and needs none. `CandleAggregator` buckets on raw UTC-epoch alignment (a 1-minute
   boundary is the same instant in every timezone, unlike the historical pipeline's IST *trading
   day* boundaries), and only builds 1m/15m bars — daily candles remain the historical pipeline's
   job. `StalenessDetector` and `LiveFeed.check_staleness()` are edge-triggered (publish `FeedStale`
   once when tripped, not on every subsequent poll) — same idempotent-notification shape as
   `KillSwitch.trip()` (ADR-018).
5. **A new `core/events.py` EventBus** (ADR-004's design, not yet built by any prior milestone)
   ships now because M10 is the first real producer (`CandleReceived`, `FeedStale`) — only those two
   events are defined; the rest of the architecture doc's vocabulary arrives with the milestones
   that produce them. Handler storage is type-erased behind one documented `cast`, the same
   established pattern as `construct_strategy()` (ADR-017).
6. **Market-hours gating is a plain decision function** (`NSECalendar.is_open_at()`, new), not a
   running scheduler — `LiveFeed.run()` simply declines to start outside NSE hours. The actual
   *scheduling* (starting/stopping the feed automatically at session boundaries via APScheduler) is
   M11's job, per ROADMAP M11's own component list; M10 only needed the yes/no decision.
7. **A stopgap `UPSTOX_ACCESS_TOKEN` secret** (config.py `Secrets`, `.env.example`) lets `pt data
   stream` work *today*, manually, ahead of M17's automatic daily re-auth — expires daily like any
   Upstox token, exactly like the real one M17 will manage automatically.
**Consequences — this was verified against Upstox's real production servers, not just documentation.**
Using a manually-configured real access token, `_authorize_websocket()` and `stream_quotes()` were
exercised directly against `api.upstox.com`/the live feed on 2026-07-21: the authorize call
succeeded, the websocket connected and accepted the subscribe message, and a real RELIANCE tick
(`ltp=1303.7`, correctly-scaled `ltt`) decoded correctly — confirming the vendored schema, the
epoch-*milliseconds* assumption for `ltt` (inferred from the field's type, now empirically
confirmed), and the whole authorize → connect → subscribe → decode chain are actually correct, not
merely plausible from public docs. What remains genuinely unverified until M17 exists: sustained
multi-hour connections, behavior across a real reconnect in production (only mock-server-tested),
and other instruments/modes. `pt data stream`'s market-hours gate is conservative by design (regular
session only, 09:15–15:30 IST) — a live tick observed slightly after 15:30 during manual testing
reflects NSE's closing-session price discovery continuing briefly past the continuous session, which
this milestone deliberately treats as out of scope rather than silently guessing at its exact rules.

## ADR-021 · Trade Orchestrator: per-candle transactions, live indicators, session scheduling

**Context.** M11 wires the spine ROADMAP called for since M1: candle -> strategy -> risk -> broker ->
persistence, via the event bus M10 built. It's the first milestone with no new external system of its
own — it composes M7 (Strategy), M8 (Risk Engine), M9 (Paper Broker), and M10 (Live Feed) — so most of
its decisions are about *how those four talk to each other* under a live, continuously-running loop
rather than a backtest's single-pass replay or a CLI command's one-shot request.
**Decisions.**
1. **Indicators go live via the streaming states M5 already built**
   (`orchestrator/indicator_bridge.py::LiveIndicatorView`), not by recomputing a batch series on
   every new candle — the live analogue of `backtest/indicator_bridge.py::BatchIndicatorView`, over
   the identical `IndicatorSpec` declarations (Rule 11: one Strategy contract). Only kinds with a
   streaming state (`sma`/`ema`/`rsi`/`atr`/`macd`/`bollinger`) work live; `vwap`/`obv`/`supertrend`
   backtest fine but can't run live/paper yet — an honest, narrow gap (`UnknownIndicatorKind`), not
   silently wrong numbers. Warmup gates on *both* `warmup_bars()` and every indicator reporting a
   value (`all_warm()`), mirroring ADR-015's backtest rule exactly.
2. **One committed transaction per candle.** `Orchestrator._on_candle` wraps the whole
   signal -> risk -> order flow in a single `session_scope()` — it either all commits or none of it
   does. This also reframes ADR-019's reconciliation rationale: under this calling pattern a crash
   mid-flow rolls back entirely rather than leaving a stuck order (SQLite transaction semantics, not
   a new guarantee this milestone added) — reconciliation stays valuable for M17, where a live broker
   is a second system with a real network round-trip that can genuinely diverge from the local commit.
3. **A handler exception is contained at the orchestrator boundary, never left to reach the event
   bus.** `core/events.py`'s `EventBus` (M10) has no handler isolation of its own — an uncaught
   exception in `_on_candle` would otherwise propagate straight through `LiveFeed`'s tick-consuming
   loop and kill the whole session over one bad signal. The catch-all here is deliberate and
   documented, feeding `KillSwitch.record_error()`/`record_success()` (M8) as the circuit breaker
   Rule 14 calls for — verified directly by a strategy test double that raises on every call, proving
   the kill switch actually trips after `max_consecutive_errors` and stays clear when interleaved
   with successes.
4. **`equity` and `daily_realized_pnl` (ADR-018's explicit `RiskEngine.evaluate()` inputs) are finally
   real, not placeholders** — `equity` from `PaperBroker.get_funds()` (M9), `daily_realized_pnl` from
   a new `Trade.realized_pnl` column (nullable, set only on closing legs, mirroring backtest's
   `ExecutedTrade.realized_pnl`) summed since IST midnight (`core/calendar.py::ist_midnight_utc`, new).
   This was the one real schema change this milestone needed — Trade rows previously had no per-leg
   P&L to query by date, only `Position.realized_pnl`'s lifetime-cumulative total.
5. **A new `LiveQuoteSource`** (execution/paper/quotes.py) supersedes M9's `ReplayQuoteSource` during
   a live session: the Orchestrator updates it with each `CandleReceived`'s close before processing
   that candle, so the Paper Broker prices fills off the session's actual last trade instead of
   yesterday's close — the exact seam ADR-019 built `QuoteSource` to let a real feed plug into.
6. **Session timing is APScheduler's `AsyncIOScheduler`** (new dependency; the process already lives
   inside one asyncio loop for the live feed's async generator chain), with exactly three jobs:
   session start/stop (cron at 09:15/15:30 IST) and a periodic housekeeping tick (resting-order fills
   + staleness — M9/M10 built the mechanisms and left "who calls them, how often" for here). `pt run`
   starting mid-session (between two daily cron firings) needed an explicit "start immediately if
   already within market hours" check in `LiveScheduler.start()` — without it, a process started at,
   say, 11:00 IST would silently wait until *tomorrow's* 09:15 trigger.
**Consequences.** Verified two ways: 462 tests including a "replayed session" integration test
(`tests/test_orchestrator_service.py`) driving real `CandleReceived` events through a real
`EventBus`/`RiskEngine`/`PaperBroker`, and — since the market was closed during this milestone's own
build window, the same honest constraint M10 hit — a direct script replaying 877 real RELIANCE daily
candles through the actual Orchestrator wiring against an isolated database (never the user's real
paper trading records): 115 real orders placed and filled, alternating BUY/SELL correctly tracking
the strategy's own crossover logic, zero unexpected risk events, ending at a plausible position and
realized P&L. `pt run --mode paper` itself was confirmed to construct, reconcile, register all three
scheduler jobs, and correctly decline to start the feed while the market is closed — the actual
trading loop's first live-market run is still pending real trading hours, same honest gap M10 had.

## ADR-022 · Performance Analytics: read-only reports over M9-M11's own records

**Context.** M12 asks "how is the strategy actually doing" — P&L, win rate, and a trade-by-trade
journal — entirely from what execution (M9 Paper Broker, M11 Orchestrator) already persists. Nothing
here is a new source of truth; it's read-only accounting over `Trade`/`Order`/`Position`/`Signal`.
**Decisions.**
1. **`win_rate`/`expectancy`/`profit_factor` are re-derived over a plain P&L list, not imported from
   `backtest/metrics.py`.** That module's versions are coupled to its own `ExecutedTrade` dataclass;
   these three are a few lines each, and a shared abstraction across two different trade-record
   shapes isn't worth it yet (Rule 5). `cagr`/`sharpe_ratio`/`max_drawdown` **are** reused unchanged —
   they already operate on the generic `EquitySeries` type with no backtest-specific coupling.
2. **The equity curve is a cash-only step function** (`analytics/pnl.py::equity_curve_from_trades`),
   replaying `Trade.net_amount` chronologically from an explicit `initial_cash` — not mark-to-market
   at every historical instant. A continuous curve would need a historical price at arbitrary past
   timestamps, which nothing in this codebase indexes; a realized curve that steps at fills is what
   most trade journals present anyway. Only *current* open positions get marked (`unrealized_pnl`,
   same last-synced-candle-close convention as ADR-019's `ReplayQuoteSource`) — a point-in-time
   report has no live tick to mark against.
3. **The trade journal groups fills into round-trip episodes by replaying them chronologically per
   instrument and tracking running signed qty** (`analytics/journal.py`), mirroring
   `PaperBroker._apply_fill_to_position`'s own same-direction-vs-closing test. This naturally handles
   partial-fill multi-leg entries (ADR-019) as one episode with a qty-weighted entry price, without
   ever needing to special-case them. A still-open position produces no entry — this is "every
   CLOSED trade," not a position snapshot (that's `pt paper status`).
4. **`since` filtering happens on the *built* episode's `exit_at`, never on raw trade legs before
   grouping** — filtering legs first would break entry/exit pairing for any episode whose entry
   predates the window. This was caught in design review, before any code was written, by tracing
   through exactly this case.
5. **Per-strategy/per-instrument breakdown required retrofitting M11's `Orchestrator`** to actually
   persist `Signal`/`StrategyRun` rows and link `Order.signal_id` — M3 designed this schema but M11
   never wired it up (an oversight, not a deferred decision). The retrofit is minimal: one
   `StrategyRun` row per orchestrator lifetime, one `Signal` row per produced signal with its
   approve/reject status, `Order.signal_id` set post-hoc once the broker acks — the Broker interface
   itself stays ignorant of "signals," an internal-only concept (Rule 7). Trades whose order predates
   this retrofit (or predates M11's `Trade.realized_pnl` column entirely) have no strategy attribution
   and fall back to a `(unattributed)` bucket rather than being silently dropped or guessed at.
**Consequences.** 33 new tests across `analytics/pnl.py`, `analytics/journal.py`,
`analytics/reports.py`, the `Orchestrator` retrofit, and `pt report daily`/`weekly` — all passing,
alongside the full 495-test suite, mypy strict, and ruff. Live-verified against the real project
database (`data/personaltrade.db`, migrated to head — an additive `ALTER TABLE`/`CREATE INDEX` only,
confirmed via `alembic history` before running): `pt report weekly` correctly surfaced a real BUY/SELL
round trip from an earlier milestone's manual testing, and `pt report daily`/`weekly` correctly split
it by IST day/week boundaries. That round trip predates M11's `Trade.realized_pnl` column, so its
`realized_pnl` is genuinely unknown (not zero) — the journal honestly reports `pnl=₹0` for it (nothing
to sum, not a fabricated figure) while the summary's `closed_trades`/win-rate/expectancy correctly
exclude it entirely, since only trades with a *recorded* `realized_pnl` count toward those stats. A
real, historical position's true economics (a small loss, still visible in `Position.realized_pnl`'s
lifetime total) are consequently invisible to the new per-trade stats — an honest, documented scope
boundary of instrumenting a running system after the fact, not a defect to retrofit further.

## ADR-023 · News Service: one generic RSS provider, feedparser, and hard-won tagging precision

**Context.** M13 ingests market/stock news for M14's AI layer to eventually read. M3 had already
designed the schema (`NewsItem`, `NewsRepository.add_if_new` for dedup) and 03-interfaces.md's
`NewsProvider` Protocol; this milestone builds a real implementation, instrument tagging (schema gap:
`Instrument` had no company-name column), sanitization, and the CLI.
**Decisions.**
1. **One generic `RssNewsProvider`, parametrized by feed URL, not a bespoke class per source** —
   matches 03-interfaces.md's "multiple registered providers run on a schedule; dedup + tagging is
   shared pipeline code." Sources live in `news.sources` (config), so adding or dropping a feed is a
   config edit, never a code change — directly serves ROADMAP's own "flaky free sources" risk note.
2. **Feeds are parsed with `feedparser`, not `xml.etree.ElementTree`** — a new dependency, added only
   after direct evidence: moneycontrol.com's own RSS feed returned outright unparseable XML across
   separate live requests (and, separately, a live HTTP 403) during this milestone's build. `ElementTree`
   is a strict parser; real-world Indian financial RSS is not reliably well-formed. `feedparser` is the
   long-standing standard tool for exactly this, and degrades a bad feed to zero entries rather than an
   exception — one dead source never blocks the others (verified: `pipeline.ingest` isolates a raising
   provider's failure into that source's own `IngestResult.error`).
3. **Default sources are `economic_times_markets` and `livemint_markets`**, chosen only after fetching
   each live candidate directly: moneycontrol dropped (repeated real parse failures, then a 403);
   NDTV Profit dropped (works, but is a general business/tech feed, not market-focused) in favor of two
   verified-clean, verified-current, market-specific feeds.
4. **News text is sanitized before storage, not at read time** — `intelligence/news/sanitize.py` strips
   markup with `html.parser.HTMLParser` (not regex: a regex tag-stripper is a known-bypassable approach
   against attacker-controlled input) and clamps length, so every downstream reader (CLI, a future
   dashboard, M14's prompt builder) sees already-safe text. This is prompt-injection defense #2 from
   docs/architecture/05-ai-data-flow.md, applied one milestone earlier than strictly required, since the
   sanitizer has no reason to live anywhere else.
5. **`Instrument.name` is now persisted** (Upstox's instrument-master company name — fetched by M4's
   `sync_instruments` since the beginning, silently discarded every time because the column didn't
   exist). Tagging needs it: ticker-only matching misses most prose ("Reliance Industries" never says
   "RELIANCE"). A new `news_instrument_tags` many-to-many table (one article can name several
   instruments) backs `NewsRepository.list_for_instrument` ("news for symbol X, last N days").
6. **Tagging precision was substantially wrong on the first live-E2E pass, and got fixed in place**
   (all three findings only showed up against real feeds and the real ~2,400-symbol universe, not the
   hand-picked unit-test fixtures):
   - *Case-insensitive symbol matching tagged almost everything.* A large slice of real NSE tickers are
     ordinary English words (`OIL`, `ENERGY`, `DOLLAR`, `TOTAL`, `TECH`, `GLOBAL`, `MIDCAP`, `RETAIL`,
     `METAL`...). Matching case-insensitively meant "Dollar wavers as markets grapple with Gulf
     tensions" tagged the ticker `DOLLAR`. Fix: symbol matching is now case-**sensitive** (company-name
     matching stays case-insensitive, since prose title-cases names inconsistently). This cut spurious
     tags from 106 to 35 across the same 85 real articles.
   - *"Corp"/"Corporation" was in the suffix-stripping list.* `_normalize_name("Birla Corporation Ltd")`
     stripped both `Ltd` and `Corporation`, leaving just `Birla` — which then matched every unrelated
     Aditya Birla Group mention ("Aditya Birla Sun Life AMC posts record profit" tagged Birla
     Corporation, ticker `BIRLACORPN`). Unlike `Ltd`/`Limited`/`Inc`/`Plc`, "Corporation" is sometimes the
     actual distinguishing second word of an Indian company's name, not a generic legal-entity suffix —
     removed from the strip list.
   - *A residual, accepted ambiguity remains*, deliberately not chased further: a handful of real tickers
     are structurally indistinguishable from common usage even case-sensitive — `BSE` is both a real
     company's ticker and the near-universal shorthand for the exchange itself (appearing in nearly
     every IPO-listing article); `BANKINDIA`'s company name, "Bank of India," is a literal substring of
     "Reserve Bank of India," itself mentioned in a large fraction of Indian financial news. A general
     fix here means real entity disambiguation (NER/entity-linking), a materially bigger feature than a
     v1 tagger warrants (Rule 5). This is an acceptable scope boundary specifically *because* M14's LLM
     analysis layer reads the tagged article in full context before reasoning about it — over-tagging
     (occasional noise) is a far cheaper failure mode than under-tagging (missing real news), and a
     downstream reasoning step is the natural, already-planned place to absorb it, not this deterministic
     filter.
7. **`pt news sync` is a plain CLI command, not an APScheduler job wired into the live orchestrator** —
   news ingestion is useful independent of whether a live/paper trading session is running (research on
   a closed market day, for instance), and coupling `intelligence/` to `orchestrator/`'s session-hours
   scheduler would cross the module-dependency rule for no real benefit. "Scheduled" means "the user's OS
   task scheduler runs `pt news sync` periodically" — identical precedent to M4's `pt data sync`, which
   nothing schedules internally either.
8. **`NewsIngested` (named in 01-system-architecture.md's event vocabulary) is not implemented yet.**
   Nothing subscribes to it until a live dashboard (M16) needs a push update — defining an event with no
   subscriber is dead scaffolding, not "the architecture," so it waits for the milestone that actually
   consumes it.
9. **Bugfix, discovered by this milestone's own live E2E, unrelated to M13's feature set:**
   `sync_instruments` (M4) looked up an existing row by `instrument_key` only; Upstox's instrument
   master occasionally rotates a symbol's `instrument_key` (hit directly while backfilling `name` for
   real data), so the old row went undetected and the insert collided with its own `symbol`+`exchange`
   unique constraint. Fixed with a fallback lookup by `symbol`+`exchange`, updating `instrument_key` in
   place; regression test added (`test_rotated_instrument_key_updates_in_place_instead_of_colliding`).
**Consequences.** 38 new tests (`sanitize`, `tagging`, `rss` over a mocked `httpx.MockTransport`,
`pipeline`, repo/CLI additions) — all passing, alongside the full 533-test suite, mypy strict, and
ruff. A real bug was caught by the test suite itself before live E2E even began: `_published_at` used
`time.mktime` (assumes *local* time) on `feedparser`'s already-UTC `published_parsed`, silently shifting
every timestamp by the local UTC offset (IST, +5:30, in this environment) — fixed to `calendar.timegm`.
Live-verified end-to-end against the real project database and real feeds: `pt data sync-instruments`
backfilled real company names for 2,393 NSE instruments (after the rotation-fallback fix); `pt news
sync` fetched and stored 85 real, current articles from both configured sources; tagging was iterated
live against this real data until the precision issues above were found and fixed; `pt news list
HDFCBANK`/`BAJAJ-AUTO` correctly returned real, current, correctly-tagged articles end to end.

## ADR-024 · AI Analysis Service: `.messages.parse()` structured outputs, schema as the injection wall, checked (not clamped) budgets

**Context.** M14 builds `LLMProvider` (already specified in 03-interfaces.md) and the Prompt Builder /
Analysis Service pipeline from 05-ai-data-flow.md. ADR-013 already chose Bedrock-first-then-direct-API
as the backend strategy; this milestone is the first to actually implement and exercise that choice, plus
everything downstream of it: the structured-output contract, the prompt-injection defenses, and the
budget gate.

**Decisions.**
1. **`AIAnalysisOutput` (the pydantic schema), not `AIAnalysis` (the ORM audit row), is the model's output
   type** — same name would collide two very different things (a closed, LLM-filled contract vs. a
   database record) in the same process. Every field is a bounded `Literal` or a length-capped
   list/string with `extra="forbid"`, so a fully hijacked model response is still structurally incapable
   of expressing a price, quantity, or anything outside its five fields (CLAUDE.md Rule 9/10) — this is
   the strongest defense layer, verified directly in tests (`test_rejects_a_non_advisory_stance_value`,
   `test_rejects_unknown_extra_fields`), not just asserted in a docstring.
2. **News is fenced with a literal `-----BEGIN/END UNTRUSTED NEWS ITEM-----` delimiter, and any
   5+-hyphen run inside a news title/body is broken up before wrapping** (`intelligence/analysis/prompt.py
   _defuse`) — a hostile article containing the real closing fence text cannot forge a fake boundary and
   masquerade as system content past it. This is defense layer 2 (layer 1 is the schema above); a
   red-team fixture (`test_forged_fence_inside_news_body_is_defused`) asserts the forged fence does not
   survive intact, since there is no live model in a unit test to assert "the model resisted it" against.
3. **`LLMProvider.analyze()`'s `client` parameter is typed `Any` inside `AnthropicLLMProvider`, not a
   hand-rolled Protocol matching the SDK.** A first attempt at a narrow structural Protocol
   (`_AnthropicClient` with a `.messages.parse(**kwargs) -> Any` method) failed mypy strict: the real
   `Anthropic`/`AnthropicBedrock.messages.parse()` is a Stainless-generated method with a large, precise
   keyword-argument signature that a `**kwargs: Any` Protocol cannot structurally match in either
   direction. `build_anthropic_provider` — the only place that constructs a real client — keeps
   `Anthropic | AnthropicBedrock` typed precisely, so a genuine construction-site typo is still caught;
   only the test-injection seam is loosely typed, which is an honest reflection of "this is an SDK
   boundary," not a type-safety regression.
4. **Budget caps are checked before the call, against persisted `AIAnalysis` rows only, never clamped
   mid-call.** `AIAnalysisRepository.count_since`/`sum_cost_since` sum real audit rows; a transient
   provider failure that never produced a row never eats into `daily_call_cap`/`monthly_usd_cap`. A cap
   of exactly `0` means zero calls allowed (not "unlimited") — consistent, safety-first reading matching
   the kill switch's own "0 tolerance" semantics, applied identically to both caps (an earlier draft
   special-cased `daily_call_cap == 0` as unlimited; rejected for the inconsistency with `monthly_usd_cap`).
5. **Pricing (`AIConfig.pricing`) and the Bedrock inference-profile id map (`_BEDROCK_MODEL_IDS`) are two
   different kinds of constant, deliberately not both in config.** Pricing is a rate that genuinely drifts
   and a user might reasonably override without a code change, matching `CostConfig`'s precedent — it
   lives in config with sensible defaults. The Bedrock id map is AWS account/region plumbing (discovered
   live via `aws bedrock list-inference-profiles`, ADR-013's caveat about model-access confirmation), not
   a trading policy — it lives as a code constant with a comment on how to regenerate it.

**Live verification (2026-07-22).** Bedrock was retested first (ADR-013's stated backend of record):
`list_foundation_models`/`list_inference_profiles` now succeed (model access is granted, unlike the
2026-07-19 finding), but every `messages.parse()` invoke call — `global.anthropic.claude-opus-4-8`,
`claude-haiku-4-5`, and even the old on-demand `anthropic.claude-3-haiku-20240307-v1:0` — returns HTTP
400 `{"message": "Operation not allowed"}`. This is an IAM invoke-permission gap on the AWS side (not a
model-access or code issue) and remains open as a user action item. `pt analyze RELIANCE` was then run
against the direct API (`ANTHROPIC_API_KEY`, backend picked automatically per ADR-013 with zero code
change) and succeeded end-to-end against the real project database: real synced RELIANCE candles, real
ingested news, a real `claude-opus-4-8` response reasoning correctly about a genuine data nuance (the
matched news item's timestamp postdating the candle close), and a correctly persisted `ai_analyses` audit
row (prompt hash, input/output token counts, `$0.017215` computed cost, full validated output JSON).

**Consequences.** 40 new tests (`anthropic_provider`, `schema`, `prompt`, `service`, `snapshot`, CLI) on
top of the existing 533, all green, alongside mypy strict and ruff. Bedrock stays wired as the preferred
backend per ADR-013 — nothing about this milestone's code favors direct-API long-term, only today's
verification run did, because Bedrock's IAM gap is still open.

## ADR-025 · Recommendation Engine: a standalone screener merging Strategy signals with AI as a veto-only layer

**Context.** M15 builds the Recommendation Engine (docs/architecture/05-ai-data-flow.md "Deterministic
gate rules", ROADMAP M15): merge a deterministic `Strategy` signal (M7) with AI analysis (M14) into a
ranked, explained `Recommendation` row, with AI able to veto/rank/explain but never originate. Two
things didn't exist yet and needed a design: how to get "today's signal" for an instrument without a
live trading loop (M11's orchestrator persists `Signal` rows, but only while actually trading a session),
and how to make the merge rule concrete instead of just the architecture doc's one prose example.

**Decisions.**
1. **`intelligence/recommendation/screener.py::latest_signal()` evaluates a `Strategy` against only an
   instrument's most recent bar**, reusing `backtest/indicator_bridge.py`'s exact precompute-once +
   `max(warmup_bars(), first_all_valid_index(...))` gating (ADR-015 point 1) so a real signal here can
   never disagree with what the same candle would have produced mid-backtest. This is deliberately a
   read-only decision function, not a second orchestrator: no `StrategyRun`/`Signal` rows are persisted
   (those belong to real order flow, ADR-022's retrofit) — `Recommendation.signal_id` stays null, and the
   deterministic basis is instead recorded inline in `Recommendation.rationale` (signal direction,
   position qty, the action it implied). A recommendation with no signal on the latest bar produces no
   row at all — no exception, no placeholder, matching rule 1 exactly and verified against real,
   unmodified production data (see Live verification).
2. **`merge.py::deterministic_action()` mirrors the Risk Engine's own reading of "already positioned"**
   (ADR-018 point 4: opening while already in one is rejected): a LONG/SHORT signal while already
   positioned that way recommends HOLD, not a duplicate BUY/SELL, so the Recommendation Engine's advice
   can never contradict what the Risk Engine would actually do with the same signal. EXIT resolves
   against the real position side (SELL to close a long, BUY to cover a short); EXIT while already flat
   has nothing to act on and returns AVOID rather than a fabricated action.
3. **AI is a pure veto layer, gated by a new `RecommendationConfig.veto_conviction_threshold`
   (default `"high"`)** — the strictest bound on the ordinal low/medium/high conviction scale, i.e. the
   architecture doc's own literal example ("negative news_impact + high conviction downgrades BUY to
   HOLD") *is* the default, not a looser illustration of it. AI can only flip an actionable BUY/SELL to
   HOLD when its assessed `news_impact` contradicts the direction at/above the threshold; it can never
   touch HOLD/AVOID (nothing to originate) or a same-direction news assessment (positive news never
   vetoes a BUY). Every merge decision — deterministic action, AI stance/conviction/news_impact/summary
   if consulted, and the veto reason if one fired — lands in `Recommendation.rationale`, the full "why"
   trace ROADMAP M15 asks for.
4. **AI is attempted best-effort per instrument inside the same cycle, never gating the whole run.**
   `engine.py::_try_ai_analysis()` catches `AIAnalysisDisabled`, `AIBudgetExhausted`,
   `LLMProviderError`, and `LLMOutputInvalid` individually and degrades that one instrument's
   recommendation to deterministic-only (`ai_output=None`, `ai_analysis_id=None`) rather than letting any
   of them abort the cycle — the AI-outage degradation behavior ROADMAP M15 calls for by name, and the
   same "still produces deterministic recommendations" contract docs/architecture/05-ai-data-flow.md's
   failure table describes for M14 itself.
5. **Ranking is actionable-first, then conviction-descending within a tier**
   (`merge.py::rank_sort_key`): BUY/SELL rank ahead of HOLD, which ranks ahead of AVOID; ties inside a
   tier break by AI conviction score (an AI-absent recommendation scores lowest within its own tier,
   never ahead of one AI actually looked at). `Recommendation.rank` is assigned only after every
   instrument in the pass has been screened, so it reflects the whole cycle, not arrival order.
6. **No schema changes.** M3 already modeled `Recommendation` with a nullable `signal_id`/`ai_analysis_id`
   exactly shaped for "deterministic basis, optional AI support" (docs/architecture/02-data-model.md) —
   M15 is the first milestone to actually populate that table, with zero migration needed.
7. **`pt recommend run` is the only new CLI surface** — screens `trading.universe` with `trading.strategy`
   (the same strategy/params config `pt run` trades live/paper with, so "what would this recommend" and
   "what would this trade" can never silently diverge) and prints the ranked list it just persisted. No
   separate `recommend list` — reading back historical days is the dashboard's job (M16); this command's
   own output already shows what it wrote.

**Live verification (2026-07-22).** Ran directly against the real project database and real synced
candles (RELIANCE, INFY — 877 real daily bars each), not fixtures. First pass: default `sma_crossover`
(10/30) produced **zero** recommendations for either symbol — a real, honest "no crossover on today's
bar," proving rule 1 holds against unmodified production data, not just engineered test fixtures. Second
pass, with `strategy_params: {fast_period: 5, slow_period: 15}` (a legitimate config choice, not modified
data): a genuine LONG crossover fired on RELIANCE's real last bar, driving one real Anthropic API call
(`claude-opus-4-8`, $0.01784) whose result — `conviction=medium`, `news_impact=negative` — correctly did
**not** veto the BUY, because medium conviction sits below the configured `"high"` threshold; the
persisted row (`Recommendation` id, rank 1, `ai_analysis_id` linked to the real `AIAnalysis` row) carries
the complete rationale trace end to end. INFY correctly produced no row in both passes (no signal). The
temporary `strategy_params`/`universe` override used for this run lived only in the git-ignored
`config/local.yaml` and was removed afterward; the real `Recommendation`/`AIAnalysis` rows it wrote
remain in the project database as evidence, matching ADR-024's own precedent for live-verification rows.

**Consequences.** 32 new tests (`merge` table tests including the AI-veto/no-veto/threshold cases,
`screener` including a real `sma_crossover` golden-cross-on-final-bar case, `engine` covering every AI
failure mode plus multi-instrument ranking, and 5 CLI tests) on top of the existing suite, all green,
alongside mypy strict and ruff. The Recommendation Engine has zero dependency on the live orchestrator or
a running trading session — it works standalone against whatever candles `pt data sync` has already
stored, exactly the "daily ranked recommendation list" ROADMAP M15 asks for, ahead of M16's dashboard
giving it a UI.
