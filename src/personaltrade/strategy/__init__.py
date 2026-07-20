"""Strategy interface + reference implementations (docs/architecture/03-interfaces.md).

`strategy/base.py` carries the interface shared by backtest, paper, and live
(Rule 11). `strategy/strategies/` holds the reference implementations;
`strategy/registry.py` resolves a name (or a `module:ClassName` dotted path)
to a class for the CLI and the parameter-sweep tool.
"""
