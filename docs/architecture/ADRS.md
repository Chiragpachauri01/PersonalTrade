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

## ADR-011 · LLM backend starts on Amazon Bedrock, config-switchable to direct API

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
