# PersonalTrade — AI Trading Research & Execution Platform

A professional-grade, personal trading platform for Indian markets (NSE, via Upstox), built as a
**modular monolith** in Python. Deterministic quant core (data → indicators → strategy → risk →
broker) with an advisory-only AI layer on top. Paper trading first; live trading only after
backtesting and a paper-trading soak period prove positive edge net of costs.

## Documentation map

- [docs/ROADMAP.md](docs/ROADMAP.md) — phases, milestones, current status. **Check this first each session.**
- [docs/architecture/](docs/architecture/) — approved architecture (system, data model, interfaces,
  trade lifecycle, AI data flow, config/security/ops, ADRs). Do not contradict these without a new ADR.

## Mandatory development rules (v2 — approved 2026-07-19)

### Process
1. **Never start coding before design.** Architecture for the affected area must exist and be
   approved before implementation. Load-bearing decisions (module boundaries, data model, external
   interfaces, trade lifecycle) are designed up front; deployment/monitoring/UI details are designed
   just-in-time at the start of the milestone that needs them. Record decisions as ADRs in
   [docs/architecture/ADRS.md](docs/architecture/ADRS.md).
2. **Build incrementally, milestone by milestone.** Each milestone must compile, be fully tested,
   documented, and production-ready. Wait for user approval before starting the next milestone.
   Update the status table in ROADMAP.md when a milestone is completed or approved.
3. **Think like a CTO.** If the user's idea is poor, say so, compare alternatives, and justify a
   recommendation. Never agree just because the user suggested it.
4. **Explain reasoning** before implementing any major feature: why it exists, approaches compared,
   recommendation, future risks, extensibility.
5. **No premature optimization.** Correct → modular → tested → then fast.

### Architecture
6. **Modular monolith.** One deployable Python process. Strict module boundaries enforced through
   interfaces; in-process event bus; no microservices, no external message brokers.
7. **Design for replaceability.** Every external dependency sits behind an interface: `Broker`
   (Paper → Upstox → future), `LLMProvider` (Claude → others), `MarketDataProvider`, `NewsProvider`,
   `Strategy`. Swapping an implementation must not change the rest of the codebase.
8. **Optimize for long-term maintainability:** scalability, reliability, testability, extensibility,
   security, clean code, separation of concerns, low operational cost.

### Trading correctness
9. **Deterministic math only.** Indicators, risk, position sizing, brokerage/cost math, stop-losses:
   plain Python + numpy/pandas, never an LLM. LLMs are for reasoning, summarization, ranking,
   explanation only.
10. **The LLM never touches the order path.** AI output is advisory: schema-validated structured
    output, clamped by the deterministic risk engine, logged with the exact inputs it saw. It can
    never place, size, or modify an order.
11. **Backtest before paper, paper before live.** The same strategy code must run unmodified against
    the backtester, the paper broker, and the live broker. Live trading is enabled only by config
    after backtests and ≥4 weeks of paper trading show positive edge net of realistic Indian costs
    (brokerage, STT, stamp duty, GST, SEBI/exchange charges) and slippage.
12. **Pessimistic simulation.** Backtester and paper broker share one cost/slippage model; no
    look-ahead bias; fills are conservative.
13. **Learning module is human-in-the-loop.** Offline analysis produces suggestions a human reviews.
    No self-modification of live parameters, ever.

### Operational safety
14. **Kill switch:** max daily loss, max consecutive errors, one-command halt. **Reconciliation:**
    on startup, broker state is the source of truth. **Idempotency:** client order IDs + order state
    machine; safe to kill and restart at any point.
15. **Secrets never in code or git.** `.env` / OS keyring only. Upstox tokens expire daily —
    re-auth flow is a designed feature, not an afterthought.
16. **Timezone discipline:** store UTC, display IST; NSE trading calendar is authoritative.

## Tech stack (per ADRs — don't change casually)

- Python 3.12+, `uv` for dependency management
- FastAPI (dashboard/API), APScheduler (in-process scheduling)
- SQLAlchemy + SQLite (transactional state), Parquet + DuckDB (market data/backtests)
- pydantic + pydantic-settings (models/config), structlog (JSON logging), pytest (tests)
- `anthropic` SDK behind `LLMProvider` (default model `claude-opus-4-8`, config-switchable)
- Upstox API v2 behind `Broker`

## Conventions

- All money as `Decimal` (never float) in ₹; all timestamps timezone-aware UTC.
- Every order/trade/recommendation row is append-mostly and auditable.
- Tests live in `tests/` mirroring `src/personaltrade/`; indicator tests use golden files.
