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
