"""Strategy interface (docs/architecture/03-interfaces.md).

`strategy/base.py` carries the interface shared by backtest, paper, and live
(Rule 11). Concrete registered strategies, a parameter-sweep CLI, and
additional reference implementations arrive in M7 — see
`strategy/examples.py` for the single minimal strategy M6 uses to exercise
the backtest engine end-to-end.
"""
