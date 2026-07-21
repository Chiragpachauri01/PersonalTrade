"""Trade journal (ROADMAP M12): one entry per CLOSED round trip, with the
entry/exit signal's indicator context snapshot — "why" the trade happened,
not just "what."

Trades are grouped into round-trip episodes per instrument by replaying them
chronologically and tracking running qty (mirrors
`execution/paper/broker.py::_apply_fill_to_position`'s same-direction-vs-closing
test): an episode starts flat, accumulates same-direction "entry" legs
(partial fills, ADR-019, can split one logical entry across several Trade
rows), then accumulates opposite-direction "exit" legs until qty returns to
zero. Under the current RiskEngine (ADR-018: no pyramiding, EXIT always sized
to exactly `abs(position.qty)`), most episodes are a single entry + single
exit leg — the multi-leg grouping exists for the partial-fill case, not
speculative generality.

A still-open position at the end of history has no closing leg, so it never
produces a journal entry — this is "every CLOSED trade," not a position
snapshot (that's `pt paper status` / `PositionRepository`).
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from personaltrade.core.enums import Mode, Side
from personaltrade.data.store.models import Instrument, Trade
from personaltrade.data.store.repos import InstrumentRepository, TradeRepository


@dataclass(frozen=True)
class JournalEntry:
    instrument_id: int
    symbol: str
    side: Side  # the ENTRY side: BUY = went long, SELL = went short
    qty: int
    entry_price: Decimal
    entry_at: datetime
    entry_context: dict[str, Any]
    exit_price: Decimal
    exit_at: datetime
    exit_context: dict[str, Any]
    realized_pnl: Decimal
    total_costs: Decimal


def _trade_costs(trade: Trade) -> Decimal:
    return (
        trade.brokerage
        + trade.stt
        + trade.stamp_duty
        + trade.gst
        + trade.exchange_charges
        + trade.sebi_charges
    )


def _signal_context(trade: Trade) -> dict[str, Any]:
    signal = trade.order.signal
    return dict(signal.context) if signal is not None else {}


def _build_entry(
    instrument: Instrument, entry_trades: list[Trade], exit_trades: list[Trade]
) -> JournalEntry:
    entry_qty = sum(t.qty for t in entry_trades)
    entry_value = sum((t.price * t.qty for t in entry_trades), Decimal("0"))
    exit_qty = sum(t.qty for t in exit_trades)
    exit_value = sum((t.price * t.qty for t in exit_trades), Decimal("0"))
    realized_pnl = sum(
        (t.realized_pnl for t in exit_trades if t.realized_pnl is not None), Decimal("0")
    )
    total_costs = sum((_trade_costs(t) for t in entry_trades + exit_trades), Decimal("0"))

    return JournalEntry(
        instrument_id=instrument.id,
        symbol=instrument.symbol,
        side=entry_trades[0].order.side,
        qty=entry_qty,
        entry_price=entry_value / entry_qty,
        entry_at=entry_trades[0].executed_at,
        entry_context=_signal_context(entry_trades[0]),
        exit_price=exit_value / exit_qty,
        exit_at=exit_trades[-1].executed_at,
        exit_context=_signal_context(exit_trades[-1]),
        realized_pnl=realized_pnl,
        total_costs=total_costs,
    )


def _episodes_for_instrument(instrument: Instrument, trades: list[Trade]) -> list[JournalEntry]:
    ordered = sorted(trades, key=lambda t: t.executed_at)
    entries: list[JournalEntry] = []
    qty = 0
    entry_trades: list[Trade] = []
    exit_trades: list[Trade] = []

    for trade in ordered:
        side = trade.order.side
        signed_qty = trade.qty if side == Side.BUY else -trade.qty

        if not entry_trades:
            entry_trades = [trade]
            qty = signed_qty
            continue

        same_direction_as_entry = (qty > 0) == (side == Side.BUY)
        if same_direction_as_entry:
            entry_trades.append(trade)
            qty += signed_qty
        else:
            exit_trades.append(trade)
            qty += signed_qty
            if qty == 0:
                entries.append(_build_entry(instrument, entry_trades, exit_trades))
                entry_trades = []
                exit_trades = []
    return entries


def build_journal(
    session: Session, mode: Mode, since: datetime | None = None
) -> list[JournalEntry]:
    """`since` filters by *exit* time, applied after episodes are built from
    the full trade history — filtering individual trade legs first would
    break entry/exit pairing for any episode whose entry predates the window.
    """
    trades = TradeRepository(session).list_for_mode(mode)
    by_instrument: dict[int, list[Trade]] = defaultdict(list)
    for trade in trades:
        by_instrument[trade.order.instrument_id].append(trade)

    instruments = InstrumentRepository(session)
    entries: list[JournalEntry] = []
    for instrument_id, inst_trades in by_instrument.items():
        instrument = instruments.get(instrument_id)
        if instrument is None:
            continue
        entries.extend(_episodes_for_instrument(instrument, inst_trades))

    if since is not None:
        entries = [e for e in entries if e.exit_at >= since]
    entries.sort(key=lambda e: e.exit_at)
    return entries
