"""ORM models — the SQLite half of docs/architecture/02-data-model.md.

Candles live in Parquet (M4), not here. Keep this file in lockstep with the ER
diagram; schema changes require an Alembic migration (tests enforce no drift).
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Date,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy import (
    Enum as SAEnum,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from personaltrade.core.enums import (
    Mode,
    OrderState,
    OrderType,
    RecommendationAction,
    RiskEventKind,
    Side,
    SignalDirection,
    SignalStatus,
)
from personaltrade.data.store.types import MoneyText, UTCDateTime, utcnow


def _enum(enum_cls: type[StrEnum], length: int = 20) -> SAEnum:
    return SAEnum(enum_cls, native_enum=False, length=length, validate_strings=True)


class Base(DeclarativeBase):
    pass


class Instrument(Base):
    __tablename__ = "instruments"
    __table_args__ = (UniqueConstraint("symbol", "exchange", name="uq_instrument_symbol_exchange"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32))
    exchange: Mapped[str] = mapped_column(String(8), default="NSE")
    isin: Mapped[str | None] = mapped_column(String(12))
    instrument_key: Mapped[str] = mapped_column(String(64), unique=True)  # Upstox key
    #: Company name from the Upstox instrument master (ROADMAP M13: news-tagging
    #: matches on this too, since prose rarely spells out a raw ticker symbol).
    name: Mapped[str | None] = mapped_column(String(128))
    tick_size: Mapped[Decimal] = mapped_column(MoneyText)
    lot_size: Mapped[int] = mapped_column(Integer, default=1)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class StrategyRun(Base):
    __tablename__ = "strategy_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    strategy_name: Mapped[str] = mapped_column(String(64))
    params: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    mode: Mapped[Mode] = mapped_column(_enum(Mode))
    started_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class Signal(Base):
    __tablename__ = "signals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    instrument_id: Mapped[int] = mapped_column(ForeignKey("instruments.id"), index=True)
    strategy_run_id: Mapped[int] = mapped_column(ForeignKey("strategy_runs.id"), index=True)
    direction: Mapped[SignalDirection] = mapped_column(_enum(SignalDirection))
    ref_price: Mapped[Decimal] = mapped_column(MoneyText)
    context: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # indicator snapshot
    status: Mapped[SignalStatus] = mapped_column(_enum(SignalStatus), default=SignalStatus.NEW)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    instrument: Mapped[Instrument] = relationship()
    strategy_run: Mapped[StrategyRun] = relationship()


class Order(Base):
    __tablename__ = "orders"
    __table_args__ = (
        CheckConstraint("qty > 0", name="ck_order_qty_positive"),
        CheckConstraint("filled_qty >= 0 AND filled_qty <= qty", name="ck_order_filled_qty_bounds"),
        Index("ix_orders_state", "state"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    client_order_id: Mapped[str] = mapped_column(String(40), unique=True)  # idempotency key
    broker_order_id: Mapped[str | None] = mapped_column(String(64))  # null until acked
    instrument_id: Mapped[int] = mapped_column(ForeignKey("instruments.id"), index=True)
    signal_id: Mapped[int | None] = mapped_column(ForeignKey("signals.id"))
    side: Mapped[Side] = mapped_column(_enum(Side))
    order_type: Mapped[OrderType] = mapped_column(_enum(OrderType))
    qty: Mapped[int] = mapped_column(Integer)
    filled_qty: Mapped[int] = mapped_column(Integer, default=0)
    limit_price: Mapped[Decimal | None] = mapped_column(MoneyText)
    state: Mapped[OrderState] = mapped_column(_enum(OrderState), default=OrderState.PENDING_RISK)
    mode: Mapped[Mode] = mapped_column(_enum(Mode))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)

    instrument: Mapped[Instrument] = relationship()
    signal: Mapped[Signal | None] = relationship()
    events: Mapped[list[OrderEvent]] = relationship(
        back_populates="order", order_by="OrderEvent.id"
    )
    trades: Mapped[list[Trade]] = relationship(back_populates="order")


class OrderEvent(Base):
    """Append-only audit of every order state transition (ADR-007)."""

    __tablename__ = "order_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), index=True)
    from_state: Mapped[OrderState | None] = mapped_column(_enum(OrderState))
    to_state: Mapped[OrderState] = mapped_column(_enum(OrderState))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    order: Mapped[Order] = relationship(back_populates="events")


class Trade(Base):
    """A fill, with the full Indian cost breakdown (never a single 'fees' blob)."""

    __tablename__ = "trades"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), index=True)
    price: Mapped[Decimal] = mapped_column(MoneyText)
    qty: Mapped[int] = mapped_column(Integer)
    brokerage: Mapped[Decimal] = mapped_column(MoneyText, default=Decimal("0"))
    stt: Mapped[Decimal] = mapped_column(MoneyText, default=Decimal("0"))
    stamp_duty: Mapped[Decimal] = mapped_column(MoneyText, default=Decimal("0"))
    gst: Mapped[Decimal] = mapped_column(MoneyText, default=Decimal("0"))
    exchange_charges: Mapped[Decimal] = mapped_column(MoneyText, default=Decimal("0"))
    sebi_charges: Mapped[Decimal] = mapped_column(MoneyText, default=Decimal("0"))
    net_amount: Mapped[Decimal] = mapped_column(MoneyText)
    #: Set only on a closing leg (mirrors backtest ExecutedTrade.realized_pnl) —
    #: the full round-trip P&L including both legs' costs, needed live (ROADMAP
    #: M11) to compute today's realized P&L for the max-daily-loss risk check.
    realized_pnl: Mapped[Decimal | None] = mapped_column(MoneyText)
    executed_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)

    order: Mapped[Order] = relationship(back_populates="trades")


class Position(Base):
    __tablename__ = "positions"
    __table_args__ = (
        UniqueConstraint("instrument_id", "mode", name="uq_position_instrument_mode"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    instrument_id: Mapped[int] = mapped_column(ForeignKey("instruments.id"))
    qty: Mapped[int] = mapped_column(Integer, default=0)  # signed
    avg_price: Mapped[Decimal] = mapped_column(MoneyText, default=Decimal("0"))
    realized_pnl: Mapped[Decimal] = mapped_column(MoneyText, default=Decimal("0"))
    mode: Mapped[Mode] = mapped_column(_enum(Mode))
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)

    instrument: Mapped[Instrument] = relationship()


class RiskEvent(Base):
    __tablename__ = "risk_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[RiskEventKind] = mapped_column(_enum(RiskEventKind))
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)


class KillSwitchState(Base):
    """Current kill-switch state — singleton row (id=1), mirrors the Order/OrderEvent
    split already used for order state: this is "what's true now", RiskEvent
    (kind=KILL_SWITCH) is the append-only "what happened when" audit trail."""

    __tablename__ = "kill_switch_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tripped: Mapped[bool] = mapped_column(Boolean, default=False)
    reason: Mapped[str | None] = mapped_column(String(256))
    tripped_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    consecutive_errors: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)


class PaperAccount(Base):
    """Paper-trading cash balance — singleton row (id=1), same "current state, not
    derived" reasoning as KillSwitchState: cash is genuine state (fills mutate it
    incrementally), not something safe to re-derive from a full trade-history scan."""

    __tablename__ = "paper_account"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    cash: Mapped[Decimal] = mapped_column(MoneyText)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)


class UpstoxToken(Base):
    """Encrypted Upstox OAuth access token — singleton row (id=1), same
    "current state, not derived" reasoning as KillSwitchState/PaperAccount
    (ROADMAP M17): one account, one token, genuinely mutated in place by each
    `pt auth upstox-login`. `encrypted_access_token` is Fernet ciphertext
    (ADR-027) — the plaintext token never touches the database."""

    __tablename__ = "upstox_tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    encrypted_access_token: Mapped[str] = mapped_column(Text)
    obtained_at: Mapped[datetime] = mapped_column(UTCDateTime)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime)


class AIAnalysis(Base):
    """Audit trail for every LLM call (Rule 10): what it saw, what it said, what it cost."""

    __tablename__ = "ai_analyses"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    instrument_id: Mapped[int] = mapped_column(ForeignKey("instruments.id"), index=True)
    model: Mapped[str] = mapped_column(String(64))
    prompt_hash: Mapped[str] = mapped_column(String(64))  # sha256 of full input
    input_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    output: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[Decimal] = mapped_column(MoneyText, default=Decimal("0"))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class Recommendation(Base):
    __tablename__ = "recommendations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    instrument_id: Mapped[int] = mapped_column(ForeignKey("instruments.id"), index=True)
    signal_id: Mapped[int | None] = mapped_column(ForeignKey("signals.id"))
    ai_analysis_id: Mapped[int | None] = mapped_column(ForeignKey("ai_analyses.id"))
    action: Mapped[RecommendationAction] = mapped_column(_enum(RecommendationAction))
    rank: Mapped[int] = mapped_column(Integer, default=0)
    rationale: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class NewsItem(Base):
    __tablename__ = "news_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(String(64))
    url: Mapped[str] = mapped_column(String(512), unique=True)
    title: Mapped[str] = mapped_column(String(512))
    body: Mapped[str] = mapped_column(Text, default="")  # untrusted input
    published_at: Mapped[datetime | None] = mapped_column(UTCDateTime, index=True)
    ingested_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class NewsInstrumentTag(Base):
    """Many-to-many: one article can mention several instruments, and instrument
    tagging (ROADMAP M13) is the only way to answer "news for symbol X"."""

    __tablename__ = "news_instrument_tags"
    __table_args__ = (
        UniqueConstraint("news_item_id", "instrument_id", name="uq_news_instrument_tag"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    news_item_id: Mapped[int] = mapped_column(ForeignKey("news_items.id"), index=True)
    instrument_id: Mapped[int] = mapped_column(ForeignKey("instruments.id"), index=True)


class BacktestRun(Base):
    __tablename__ = "backtest_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    strategy_name: Mapped[str] = mapped_column(String(64))
    params: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    cost_model_version: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    from_date: Mapped[date] = mapped_column(Date)
    to_date: Mapped[date] = mapped_column(Date)
    metrics: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    data_fingerprint: Mapped[str | None] = mapped_column(String(64))  # reproducibility
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    trades: Mapped[list[BacktestTrade]] = relationship(back_populates="run")


class BacktestTrade(Base):
    __tablename__ = "backtest_trades"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    backtest_run_id: Mapped[int] = mapped_column(ForeignKey("backtest_runs.id"), index=True)
    instrument_id: Mapped[int] = mapped_column(ForeignKey("instruments.id"))
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    run: Mapped[BacktestRun] = relationship(back_populates="trades")


class SoakPeriod(Base):
    """One M18 paper-trading soak window (CLAUDE.md Rule 11: >=4 weeks of paper
    trading with positive edge before live is enabled). Unlike KillSwitchState/
    PaperAccount/UpstoxToken, this is deliberately NOT a singleton: a soak can
    be aborted (e.g. a bug fix invalidates everything measured so far) and
    restarted, so multiple rows may exist over the project's life —
    `SoakPeriodRepository.current()` returns the latest one with `ended_at IS
    NULL`. `baseline_backtest_run_id` pins the `BacktestRun` weekly reviews
    (`pt soak report`) diff paper performance against; nullable because a
    soak can start before any backtest has been run for the live universe."""

    __tablename__ = "soak_periods"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    started_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    target_days: Mapped[int] = mapped_column(Integer, default=28)
    baseline_backtest_run_id: Mapped[int | None] = mapped_column(ForeignKey("backtest_runs.id"))
    ended_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    end_reason: Mapped[str | None] = mapped_column(String(256))
    notes: Mapped[str | None] = mapped_column(String(512))
