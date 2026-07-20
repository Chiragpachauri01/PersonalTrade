"""Risk Engine (docs/architecture/03-interfaces.md, ROADMAP M8).

The sole gate between a Strategy's Signal and an order: sizing, exposure/
per-trade limits, max daily loss, and the kill switch all live here.
`orchestrator` (M11) is the only caller and the only path from an approved
order to a Broker (CLAUDE.md Rule 14).
"""
