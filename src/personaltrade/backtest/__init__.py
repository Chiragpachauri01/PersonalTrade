"""Event-driven backtesting engine (docs/architecture ROADMAP M6 — critical path).

The same Strategy interface drives backtest, paper, and live (Rule 11).
Fills are conservative: next-bar open plus adverse slippage, full Indian
equity cost stack, no look-ahead (docs/architecture/ADRS.md ADR-013/014).
"""
