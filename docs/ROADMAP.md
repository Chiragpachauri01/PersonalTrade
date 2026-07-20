# PersonalTrade — Implementation Roadmap

The project is distributed into **5 phases / 20 milestones**. Each milestone must compile, be fully
tested, documented, and production-ready before the next begins (CLAUDE.md Rule 2). Complexity is
rated S / M / L / XL.

## Status

| Phase | Milestone | Status |
|---|---|---|
| 0 | M1 Architecture Design | ✅ Approved (2026-07-19) |
| 0 | M2 Project Bootstrap | ✅ Approved (2026-07-19) |
| 0 | M3 Database Design & Migrations | ✅ Approved (2026-07-19) |
| 1 | M4 Historical Data Pipeline | ✅ Approved (2026-07-19) |
| 1 | M5 Technical Indicator Engine | ✅ Approved (2026-07-19) |
| 1 | M6 Backtesting Engine | ✅ Approved (2026-07-20) |
| 1 | M7 Strategy Engine | ✅ Approved (2026-07-20) |
| 2 | M8 Risk Engine | ✅ Delivered (2026-07-20) — awaiting approval |
| 2 | M9 Paper Broker | ⬜ Not started |
| 2 | M10 Live Market Data Feed | ⬜ Not started |
| 2 | M11 Trade Orchestrator | ⬜ Not started |
| 2 | M12 Analytics & Trade Journal | ⬜ Not started |
| 3 | M13 News Service | ⬜ Not started |
| 3 | M14 AI Analysis Service | ⬜ Not started |
| 3 | M15 Recommendation Engine | ⬜ Not started |
| 3 | M16 Dashboard | ⬜ Not started |
| 4 | M17 Upstox Integration | ⬜ Not started |
| 4 | M18 E2E Testing & Paper Soak | ⬜ Not started |
| 4 | M19 Monitoring & Alerting | ⬜ Not started |
| 4 | M20 Docs & Production Readiness Review | ⬜ Not started |

---

## Phase 0 — Foundation

### M1 · Architecture Design
- **Objective:** Approved architecture for all load-bearing decisions.
- **Components:** CLAUDE.md rules v2, system architecture, data model, interfaces, trade lifecycle,
  AI data flow, config/security/ops strategy, ADRs, this roadmap.
- **Deliverables:** `docs/architecture/*`, `docs/ROADMAP.md`, `CLAUDE.md`.
- **Dependencies:** none. **Testing:** review/approval. **Risks:** over-design — mitigated by
  two-tier design rule. **Complexity:** M.

### M2 · Project Bootstrap
- **Objective:** Running skeleton: package layout, tooling, CI-quality gates, config, logging.
- **Components:** `uv` project, `src/personaltrade/` layout, pydantic-settings config loader,
  structlog JSON logging, pytest + ruff + mypy, pre-commit, Makefile/justfile, `.env.example`.
- **Deliverables:** `personaltrade --version` runs; `pytest`, `ruff`, `mypy` all green.
- **Dependencies:** M1. **Testing:** smoke tests for config/logging; toolchain runs clean.
- **Risks:** Windows path/tooling quirks. **Complexity:** S.

### M3 · Database Design & Migrations
- **Objective:** SQLite schema per ER diagram with Alembic migrations and repository layer.
- **Components:** SQLAlchemy models (instruments, orders, trades, positions, signals,
  recommendations, risk_events, backtest_runs, news_items), Alembic, repository interfaces.
- **Deliverables:** `alembic upgrade head` builds the DB; repositories with CRUD + tests.
- **Dependencies:** M2. **Testing:** repository round-trip tests on temp DB; migration up/down.
- **Risks:** schema churn later — mitigated by migrations from day one. **Complexity:** M.

## Phase 1 — Research Core (find the edge before building execution)

### M4 · Historical Data Pipeline
- **Objective:** Clean, adjusted historical OHLCV for NSE instruments in Parquet.
- **Components:** `MarketDataProvider` interface, Upstox historical-candles implementation
  (read-only, no order scopes), instrument master sync, corporate-action adjustment, NSE holiday
  calendar, Parquet store + DuckDB query layer, data-quality checks (gaps, spikes, duplicates).
- **Deliverables:** CLI: `pt data sync NIFTY50 --interval 1d/15m`; validated Parquet datasets.
- **Dependencies:** M3. **Testing:** golden checks vs known closes; gap/duplicate detectors; calendar tests.
- **Risks:** vendor data quality; rate limits. **Complexity:** L.

### M5 · Technical Indicator Engine
- **Objective:** Deterministic, vectorized indicator library (Rule 9 — no LLMs).
- **Components:** SMA/EMA, RSI, MACD, Bollinger, ATR, VWAP, supertrend, volume stats; pure
  functions over pandas/numpy; incremental (streaming) variants for live use.
- **Deliverables:** `indicators/` package; golden-file tests against independently computed values.
- **Dependencies:** M4. **Testing:** golden files, property tests (NaN handling, warm-up windows),
  batch-vs-incremental equivalence tests.
- **Risks:** subtle off-by-one/warm-up bugs — the equivalence tests exist for this. **Complexity:** M.

### M6 · Backtesting Engine  ⭐ critical path
- **Objective:** Event-driven backtester that replays candles through the same strategy interface
  used live, with realistic Indian cost model and zero look-ahead.
- **Components:** candle replay loop, simulated execution (next-bar fills, slippage model),
  shared cost model (brokerage, STT, stamp duty, GST, SEBI + exchange charges), portfolio
  accounting, run persistence (`backtest_runs`), metrics (CAGR, Sharpe, max DD, win rate, expectancy).
- **Deliverables:** `pt backtest run <strategy> --from --to`; reproducible runs with stored configs.
- **Dependencies:** M4, M5. **Testing:** hand-computed toy scenarios; look-ahead sentinel tests
  (future data poisoned → must not change results); cost-model unit tests vs broker calculator.
- **Risks:** look-ahead bias, over-optimistic fills — pessimistic defaults. **Complexity:** XL.

### M7 · Strategy Engine
- **Objective:** Pluggable `Strategy` interface + 1–2 reference strategies validated in the backtester.
- **Components:** `Strategy` protocol (`on_candle → Signal | None`), registry, parameter schema
  per strategy, reference implementations (e.g., EMA crossover + ATR stop; mean-reversion RSI).
- **Deliverables:** strategies runnable in backtester; parameter sweep CLI.
- **Dependencies:** M6. **Testing:** deterministic replay tests (same data + params ⇒ same signals).
- **Risks:** overfitting during sweeps — out-of-sample split enforced in tooling. **Complexity:** M.

## Phase 2 — Execution Core (deterministic spine, end to end)

### M8 · Risk Engine
- **Objective:** Deterministic gate between signals and orders; the kill switch lives here.
- **Components:** position sizing (fixed-fractional / ATR-based), exposure & per-trade limits,
  max daily loss, max open positions, circuit-breaker (consecutive errors), kill switch with
  persisted state, `risk_events` audit.
- **Deliverables:** `RiskEngine.evaluate(Signal) → ApprovedOrder | Rejection` fully tested.
- **Dependencies:** M7. **Testing:** table-driven limit tests; kill-switch trip/reset tests.
- **Risks:** silent bypass paths — orchestrator (M11) enforces risk as the only path to a broker. **Complexity:** M.

### M9 · Paper Broker
- **Objective:** `Broker` implementation simulating realistic execution against live/replayed quotes.
- **Components:** `Broker` interface, paper implementation (order book, market/limit fills,
  partial fills, latency, shared cost model from M6), persisted paper positions/funds.
- **Deliverables:** full order lifecycle in paper mode surviving process restarts.
- **Dependencies:** M8 (interface work can start alongside). **Testing:** lifecycle state-machine
  tests; restart/recovery tests; cost assertions match backtester model.
- **Risks:** optimistic fills making paper results misleading — pessimistic defaults. **Complexity:** L.

### M10 · Live Market Data Feed
- **Objective:** Streaming quotes/candles during market hours with robust reconnect.
- **Components:** Upstox websocket client behind `MarketDataProvider`, candle aggregation
  (tick→1m→15m), reconnect/backoff, staleness detection, market-hours scheduler.
- **Deliverables:** live candles flowing onto the event bus during NSE hours.
- **Dependencies:** M4 (interface), M2. **Testing:** recorded-stream replay tests; reconnect chaos tests.
- **Risks:** websocket instability; daily token expiry — handled by auth flow (M17 design pre-wired). **Complexity:** L.

### M11 · Trade Orchestrator
- **Objective:** The loop that wires the spine: candle → strategy → risk → broker → persistence,
  via the in-process event bus.
- **Components:** event bus, orchestrator service, order state machine + reconciliation-on-startup,
  APScheduler jobs (pre-open sync, session start/stop, EOD tasks).
- **Deliverables:** `pt run --mode paper` trades a strategy end-to-end all session, restart-safe.
- **Dependencies:** M8, M9, M10. **Testing:** integration test on replayed session; kill/restart
  mid-trade test; reconciliation divergence tests.
- **Risks:** hidden coupling — events and interfaces only, no cross-module imports of internals. **Complexity:** L.

### M12 · Analytics & Trade Journal
- **Objective:** Truthful performance accounting for paper (and later live) trading.
- **Components:** P&L engine (realized/unrealized, net of costs), equity curve, per-strategy and
  per-instrument breakdowns, trade journal with entry/exit context snapshots, CLI reports.
- **Deliverables:** `pt report daily/weekly`; journal entries for every closed trade.
- **Dependencies:** M11. **Testing:** P&L cross-checked against hand-computed fixtures.
- **Risks:** cost omissions inflating P&L — single shared cost model, asserted in tests. **Complexity:** M.

## Phase 3 — Intelligence Layer (advisory only)

### M13 · News Service
- **Objective:** Ingest and store market/stock news for AI context.
- **Components:** `NewsProvider` interface, RSS/API implementations, dedup, instrument tagging,
  `news_items` persistence. News text is treated as **untrusted input** (prompt-injection surface).
- **Deliverables:** scheduled ingestion; query API "news for symbol X, last N days".
- **Dependencies:** M3. **Testing:** parser fixtures; dedup tests; tagging accuracy spot checks.
- **Risks:** flaky free sources — provider interface makes them swappable. **Complexity:** M.

### M14 · AI Analysis Service
- **Objective:** `LLMProvider` interface + Claude implementation producing schema-validated,
  advisory-only analysis. See [architecture/05-ai-data-flow.md](architecture/05-ai-data-flow.md).
- **Components:** `LLMProvider` protocol, Anthropic implementation (`claude-opus-4-8` default,
  config-switchable), prompt builder (market snapshot + indicators + news), structured outputs via
  pydantic (`messages.parse`), prompt caching for the stable system prompt, audit trail (inputs
  hash + full response persisted), cost/token accounting.
- **Deliverables:** `pt analyze SYMBOL` returns validated `AIAnalysis`; every call audited.
- **Dependencies:** M5, M13. **Testing:** mocked-provider unit tests; schema-rejection tests;
  prompt-injection red-team fixtures (hostile news text must not alter structure/verdict fields).
- **Risks:** cost creep — token budgets + per-day spend cap in config; injection — deterministic gate. **Complexity:** M.

### M15 · Recommendation Engine
- **Objective:** Merge deterministic signals with AI analysis into ranked, explained recommendations.
- **Components:** deterministic merge rules (AI can veto/annotate/rank, never originate an order),
  recommendation persistence, explanation renderer.
- **Deliverables:** daily ranked recommendation list with full "why" trace.
- **Dependencies:** M7, M14. **Testing:** merge-rule table tests; AI-outage degradation test
  (system still produces deterministic recommendations). **Risks:** AI over-weighting — weights in
  config, default conservative. **Complexity:** M.

### M16 · Dashboard
- **Objective:** Local web UI: positions, P&L, recommendations, journal, kill switch, AI explanations.
- **Components:** FastAPI routes + REST API, simple auth (single user), server-rendered or light
  React front end (decided by ADR at milestone start), websocket for live updates.
- **Deliverables:** dashboard on `localhost` covering daily workflow without CLI.
- **Dependencies:** M12, M15. **Testing:** API tests; smoke E2E via Playwright.
- **Risks:** scope creep — daily-workflow screens only. **Complexity:** L.

## Phase 4 — Live Readiness

### M17 · Upstox Integration (staged)
- **Objective:** Live `Broker` implementation, enabled read-only first.
- **Components:** OAuth login flow with daily token refresh UX, encrypted token store,
  stage 1: funds/holdings/positions/order status (read-only); stage 2: order placement behind
  `trading.live_orders_enabled` config flag + kill-switch integration; rate-limit handling.
- **Deliverables:** paper→live switch is config-only; reconciliation runs against real account.
- **Dependencies:** M11. **Testing:** sandbox/mock API tests; stage-2 dry-run mode (log, don't send);
  smallest-quantity live smoke test only after user approval.
- **Risks:** API quirks, token expiry mid-session — staleness detection + alerting. **Complexity:** L.

### M18 · End-to-End Testing & Paper Soak
- **Objective:** Prove the whole system for ≥4 weeks of live-market paper trading.
- **Components:** full-session E2E suite on recorded data, chaos tests (feed drop, restart,
  token expiry), soak checklist, weekly review reports comparing paper vs backtest expectations.
- **Deliverables:** soak report; go/no-go recommendation for live.
- **Dependencies:** M17. **Testing:** is the milestone. **Risks:** paper/backtest divergence —
  investigate before live, never "fix" by loosening the simulator. **Complexity:** M.

### M19 · Monitoring & Alerting
- **Objective:** Know when it breaks without watching it.
- **Components:** health checks, heartbeat, error-rate metrics, Telegram/email alerts (fills,
  kill-switch trips, feed staleness, auth expiry), log rotation, daily SQLite/Parquet backups.
- **Deliverables:** alert within 1 minute of any critical failure during market hours.
- **Dependencies:** M11 (useful from M18 onward). **Testing:** induced-failure alert tests.
- **Risks:** alert fatigue — severity tiers. **Complexity:** S.

### M20 · Documentation & Production Readiness Review
- **Objective:** Operator docs + final review against every rule in CLAUDE.md.
- **Components:** runbook (daily ops, incident response, recovery), architecture doc refresh,
  security checklist pass, disaster-recovery test (restore from backup).
- **Deliverables:** signed-off readiness review; live enablement decision recorded as an ADR.
- **Dependencies:** M18, M19. **Testing:** restore drill; runbook walk-through. **Complexity:** S.
