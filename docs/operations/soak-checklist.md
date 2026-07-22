# Paper-Trading Soak Checklist (ROADMAP M18, ADR-028)

CLAUDE.md Rule 11: live trading is enabled only after backtests and **≥4 weeks of paper trading**
show positive edge net of realistic Indian costs and slippage. This checklist is what to actually
look at during that window — some items `pt soak status` checks for you, the rest need a human.

## Starting a soak

1. Run (or pick) a `BacktestRun` over the same universe/strategy/date-range shape you intend to
   paper-trade, so there's a baseline to compare against: `pt backtest run <strategy> <symbols>...`.
   Note the printed `backtest_run_id`.
2. `pt soak start --backtest-run-id <id> --target-days 28` (target-days defaults to
   `soak.target_days` in config — override only with a reason, not to make review day arrive sooner).
3. Make sure `pt run --mode paper` is actually running through full NSE sessions on a real schedule
   (OS-level scheduler/task, same precedent as `pt news sync` — nothing inside this codebase starts
   it automatically). A soak with no paper trading happening under it produces no data to review.

## Weekly (every 7 days of the soak)

- [ ] `pt soak status` — confirm days elapsed is tracking as expected, kill switch is `clear` (a
      TRIPPED kill switch mid-soak is itself a finding — investigate why before resetting it), and
      the Upstox token is `valid` (if paper mode's live feed depends on one).
- [ ] `pt soak report` — read the paper-vs-backtest deltas. A **real** divergence (not just
      backtest-vs-paper noise on tiny trade counts) is a signal to investigate the strategy or the
      paper simulator's fill/cost assumptions — **never** "fix" it by loosening the paper broker's
      slippage/cost model to make the numbers agree (ROADMAP M18's own named risk).
- [ ] Skim the week's log output (`data/logs/`) for anything that scrolled past silently: repeated
      `orchestrator_housekeeping_failed`, `reconciliation_stuck_order_failed`, or `feed_stale`
      entries that a human should have been paged for (M19 will add real alerting; until then, this
      is the manual substitute).
- [ ] Confirm `pt auth upstox-login` is still being re-run daily (or is otherwise automated) —
      a paper session that silently loses its live feed for days looks like inactivity, not a bug,
      unless someone checks.

## At ≥28 days (or whenever `pt soak status` reports `days_remaining=0`)

1. `pt soak review` — read every criterion, not just the final GO/NO-GO line. A NO-GO on
   `min_closed_trades` needs more time or a higher-frequency strategy, not a lowered threshold; a
   NO-GO on `positive_net_pnl`/`min_sharpe`/`max_drawdown` means the edge the backtest promised isn't
   showing up in the market as it actually behaves — go back to Phase 1 research, don't patch around
   it in the risk engine.
2. If GO: `pt soak end --reason "soak review passed, see pt soak review output <date>"`, then record
   the live-enablement decision as a new ADR (CLAUDE.md Rule 1) before ever setting
   `trading.live_orders_enabled: true` — ADR-008's two-key gate exists precisely so this is a
   deliberate, logged act, not a config typo.
3. If NO-GO: decide whether to keep the same soak running longer (more days, same baseline) or
   `pt soak end --reason "..."` and start a fresh one after fixing whatever the review surfaced —
   either way, the reason goes in the CLI, not just someone's memory.

## What this checklist deliberately does not automate

Rule 13 (learning is human-in-the-loop): nothing here suggests the system adjust its own thresholds,
strategy parameters, or risk limits based on soak results. A human reads the numbers and decides.
