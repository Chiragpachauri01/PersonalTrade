"""`pt` — the PersonalTrade command line. Grows with each milestone."""

from __future__ import annotations

import json as jsonlib
import os
import sys
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer

if TYPE_CHECKING:
    import pandas as pd
    from sqlalchemy.orm import Session, sessionmaker

    from personaltrade.analytics.reports import PerformanceReport
    from personaltrade.data.store.candles import CandleStore
    from personaltrade.execution.paper.broker import PaperBroker
from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig
from sqlalchemy import inspect

from personaltrade import __version__
from personaltrade.core.calendar import NSECalendar
from personaltrade.core.config import AppConfig, load_config
from personaltrade.core.enums import Interval, Mode
from personaltrade.core.errors import ConfigError, PersonalTradeError
from personaltrade.core.logging import setup_logging

app = typer.Typer(
    name="pt",
    help="PersonalTrade — AI trading research & execution platform (NSE via Upstox).",
    no_args_is_help=True,
)
config_app = typer.Typer(help="Inspect and validate configuration.", no_args_is_help=True)
app.add_typer(config_app, name="config")
db_app = typer.Typer(help="State database management.", no_args_is_help=True)
app.add_typer(db_app, name="db")
data_app = typer.Typer(help="Historical market data pipeline.", no_args_is_help=True)
app.add_typer(data_app, name="data")
backtest_app = typer.Typer(help="Event-driven backtesting.", no_args_is_help=True)
app.add_typer(backtest_app, name="backtest")
strategy_app = typer.Typer(help="Strategy registry.", no_args_is_help=True)
app.add_typer(strategy_app, name="strategy")
risk_app = typer.Typer(help="Risk Engine: limits and kill switch.", no_args_is_help=True)
app.add_typer(risk_app, name="risk")
kill_switch_app = typer.Typer(
    help="Kill switch: one-command halt (CLAUDE.md Rule 14).", no_args_is_help=True
)
risk_app.add_typer(kill_switch_app, name="kill-switch")
paper_app = typer.Typer(help="Paper Broker: simulated execution.", no_args_is_help=True)
app.add_typer(paper_app, name="paper")
report_app = typer.Typer(help="Performance reports (ROADMAP M12).", no_args_is_help=True)
app.add_typer(report_app, name="report")
news_app = typer.Typer(help="News ingestion (ROADMAP M13).", no_args_is_help=True)
app.add_typer(news_app, name="news")
recommend_app = typer.Typer(help="Recommendation Engine (ROADMAP M15).", no_args_is_help=True)
app.add_typer(recommend_app, name="recommend")

ConfigDirOption = Annotated[
    Path | None,
    typer.Option("--config-dir", help="Config directory (default: $PT_CONFIG_DIR or ./config)."),
]

_DEFAULT_LOOKBACK_DAYS = {Interval.D1: 3 * 365, Interval.M15: 60, Interval.M1: 7}


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"personaltrade {__version__}")
        raise typer.Exit()


@app.callback()
def _root(
    version: Annotated[
        bool,
        typer.Option(
            "--version", callback=_version_callback, is_eager=True, help="Show version and exit."
        ),
    ] = False,
) -> None: ...


@app.command()
def version() -> None:
    """Show the installed version."""
    typer.echo(f"personaltrade {__version__}")


def _load_config_or_exit(config_dir: Path | None = None) -> AppConfig:
    try:
        return load_config(config_dir)
    except ConfigError as exc:
        typer.secho(f"config INVALID: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc


@config_app.command("validate")
def config_validate(config_dir: ConfigDirOption = None) -> None:
    """Load the layered config; exit non-zero if anything is invalid."""
    cfg = _load_config_or_exit(config_dir)
    typer.secho(
        f"config OK — mode={cfg.trading.mode} "
        f"live_orders_enabled={cfg.trading.live_orders_enabled}",
        fg=typer.colors.GREEN,
    )


@config_app.command("show")
def config_show(config_dir: ConfigDirOption = None) -> None:
    """Print the effective (merged) configuration as JSON. Secrets are never included."""
    cfg = _load_config_or_exit(config_dir)
    typer.echo(cfg.model_dump_json(indent=2))


@db_app.command("upgrade")
def db_upgrade() -> None:
    """Create/upgrade the state database to the latest schema (Alembic)."""
    cfg = _load_config_or_exit()
    cfg.data.db_path.parent.mkdir(parents=True, exist_ok=True)
    alembic_cfg = AlembicConfig()
    alembic_cfg.set_main_option("script_location", "alembic")
    alembic_cfg.set_main_option("sqlalchemy.url", f"sqlite:///{cfg.data.db_path}")
    alembic_command.upgrade(alembic_cfg, "head")
    typer.secho(f"database upgraded: {cfg.data.db_path}", fg=typer.colors.GREEN)


def _open_store_and_session(cfg: AppConfig) -> tuple[CandleStore, sessionmaker[Session]]:
    from personaltrade.data.store.candles import CandleStore
    from personaltrade.data.store.db import build_engine, build_session_factory

    engine = build_engine(cfg.data.db_path)
    if not inspect(engine).has_table("instruments"):
        typer.secho("state database missing/empty — run `pt db upgrade` first", fg=typer.colors.RED)
        raise typer.Exit(code=1)
    return CandleStore(cfg.data.candle_root), build_session_factory(engine)


def _calendar_or_none() -> NSECalendar | None:
    holidays = Path(os.environ.get("PT_CONFIG_DIR", "config")) / "nse_holidays.yaml"
    if not holidays.is_file():
        typer.secho(f"note: {holidays} missing — gap checks disabled", fg=typer.colors.YELLOW)
        return None
    return NSECalendar.load(holidays)


def _parse_date(value: str | None, fallback: date) -> date:
    if value is None:
        return fallback
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise typer.BadParameter(f"expected YYYY-MM-DD, got {value!r}") from exc


@data_app.command("sync-instruments")
def data_sync_instruments() -> None:
    """Download the NSE equity instrument master into the instruments table."""
    from personaltrade.data.historical.sync import sync_instruments
    from personaltrade.data.providers.upstox import UpstoxMarketData
    from personaltrade.data.store.db import session_scope

    cfg = _load_config_or_exit()
    setup_logging(cfg.log)
    _, factory = _open_store_and_session(cfg)
    with session_scope(factory) as session:
        count = sync_instruments(UpstoxMarketData(), session)
    typer.secho(f"instrument master synced: {count} NSE equities", fg=typer.colors.GREEN)


@data_app.command("sync")
def data_sync(
    symbols: Annotated[
        list[str] | None,
        typer.Argument(help="Symbols to sync (default: trading.universe from config)."),
    ] = None,
    interval: Annotated[Interval, typer.Option("--interval", "-i")] = Interval.D1,
    from_str: Annotated[str | None, typer.Option("--from", help="YYYY-MM-DD")] = None,
    to_str: Annotated[str | None, typer.Option("--to", help="YYYY-MM-DD")] = None,
) -> None:
    """Fetch, validate, and store historical candles for symbols."""
    from personaltrade.data.historical.sync import sync_candles
    from personaltrade.data.providers.upstox import UpstoxMarketData
    from personaltrade.data.store.db import session_scope

    cfg = _load_config_or_exit()
    setup_logging(cfg.log)
    targets = symbols or cfg.trading.universe
    if not targets:
        typer.secho("no symbols given and trading.universe is empty", fg=typer.colors.RED)
        raise typer.Exit(code=1)

    to_date = _parse_date(to_str, date.today())
    from_date = _parse_date(from_str, to_date - timedelta(days=_DEFAULT_LOOKBACK_DAYS[interval]))
    store, factory = _open_store_and_session(cfg)
    calendar = _calendar_or_none()
    provider = UpstoxMarketData()

    failures = 0
    with session_scope(factory) as session:
        for symbol in targets:
            try:
                result = sync_candles(
                    provider, store, session, symbol, interval, from_date, to_date, calendar
                )
            except PersonalTradeError as exc:
                failures += 1
                typer.secho(f"{symbol}: FAILED — {exc}", fg=typer.colors.RED)
                continue
            color = {
                "ok": typer.colors.GREEN,
                "warnings": typer.colors.YELLOW,
                "errors": typer.colors.RED,
            }[result.report.status]
            typer.secho(
                f"{symbol} {interval.value}: +{result.fetched_rows} rows "
                f"(total {result.total_rows}) validation={result.report.status}",
                fg=color,
            )
            for finding in result.report.findings:
                typer.echo(f"    [{finding.severity}] {finding.kind}: {finding.detail}")
    raise typer.Exit(code=1 if failures else 0)


@data_app.command("check")
def data_check(
    symbol: Annotated[str, typer.Argument()],
    interval: Annotated[Interval, typer.Option("--interval", "-i")] = Interval.D1,
) -> None:
    """Run data-quality checks on an already-stored dataset."""
    from personaltrade.data.historical.quality import check_candles

    cfg = _load_config_or_exit()
    store, _ = _open_store_and_session(cfg)
    frame = store.read(symbol, "NSE", interval)
    if frame.empty:
        typer.secho(f"no stored data for {symbol} {interval.value}", fg=typer.colors.RED)
        raise typer.Exit(code=1)
    report = check_candles(frame, interval, _calendar_or_none())
    typer.echo(f"{symbol} {interval.value}: {len(frame)} rows — {report.summary()}")
    raise typer.Exit(code=1 if report.has_errors else 0)


@data_app.command("info")
def data_info() -> None:
    """List stored candle datasets (from manifests)."""
    from personaltrade.data.store.candles import CandleStore

    cfg = _load_config_or_exit()
    datasets = CandleStore(cfg.data.candle_root).datasets()
    if not datasets:
        typer.echo("no datasets stored yet — run `pt data sync`")
        return
    for ds in datasets:
        typer.echo(
            f"{ds.exchange}/{ds.symbol} {ds.interval.value}: {ds.rows} rows "
            f"[{ds.first_ts} .. {ds.last_ts}] validation={ds.validation} "
            f"synced={ds.synced_at}"
        )


@data_app.command("stream")
def data_stream(
    symbols: Annotated[
        list[str] | None,
        typer.Argument(help="Symbols to stream (default: trading.universe from config)."),
    ] = None,
    interval: Annotated[Interval, typer.Option("--interval", "-i")] = Interval.M1,
    duration: Annotated[
        int, typer.Option("--duration", help="Seconds to stream before stopping.")
    ] = 60,
    exchange: Annotated[str, typer.Option("--exchange")] = "NSE",
) -> None:
    """Stream live candles (ROADMAP M10). Needs an Upstox access token — see
    .env.example UPSTOX_ACCESS_TOKEN (the automatic daily re-auth flow is Milestone 17)."""
    import asyncio

    from personaltrade.core.config import Secrets
    from personaltrade.core.events import CandleReceived, EventBus, FeedStale
    from personaltrade.data.live.feed import LiveFeed
    from personaltrade.data.providers.upstox import MissingAccessToken, UpstoxMarketData
    from personaltrade.data.store.db import session_scope
    from personaltrade.data.store.repos import InstrumentRepository

    if interval not in (Interval.M1, Interval.M15):
        typer.secho(f"can only stream 1m/15m bars, got {interval.value!r}", fg=typer.colors.RED)
        raise typer.Exit(code=1)

    cfg = _load_config_or_exit()
    setup_logging(cfg.log)
    secrets = Secrets()
    if secrets.upstox_access_token is None:
        typer.secho(
            "no Upstox access token configured — set UPSTOX_ACCESS_TOKEN in .env "
            "(see .env.example). Automatic daily re-auth arrives at Milestone 17.",
            fg=typer.colors.RED,
        )
        raise typer.Exit(code=1)

    targets = symbols or cfg.trading.universe
    if not targets:
        typer.secho("no symbols given and trading.universe is empty", fg=typer.colors.RED)
        raise typer.Exit(code=1)

    calendar = _calendar_or_none()
    if calendar is None:
        typer.secho(
            "NSE holiday calendar unavailable — cannot determine market hours", fg=typer.colors.RED
        )
        raise typer.Exit(code=1)

    _, factory = _open_store_and_session(cfg)
    key_to_symbol: dict[str, str] = {}
    with session_scope(factory) as session:
        instrument_repo = InstrumentRepository(session)
        for symbol in targets:
            inst = instrument_repo.get_by_symbol(symbol, exchange)
            if inst is None:
                typer.secho(f"{symbol} ({exchange}) not in instruments table", fg=typer.colors.RED)
                raise typer.Exit(code=1)
            key_to_symbol[inst.instrument_key] = inst.symbol
    subscriptions: dict[str, list[Interval]] = {key: [interval] for key in key_to_symbol}

    provider = UpstoxMarketData(access_token=secrets.upstox_access_token.get_secret_value())
    bus = EventBus()
    bus.subscribe(
        CandleReceived,
        lambda e: typer.echo(
            f"{key_to_symbol.get(e.instrument_key, e.instrument_key)} {e.interval.value} "
            f"{e.ts.isoformat()}: O={e.open} H={e.high} L={e.low} C={e.close} V={e.volume}"
        ),
    )
    bus.subscribe(
        FeedStale,
        lambda e: typer.secho(
            f"feed stale — last tick at {e.last_tick_at}", fg=typer.colors.YELLOW
        ),
    )
    feed = LiveFeed(provider, bus, calendar, subscriptions)

    if not feed.is_market_open():
        typer.secho("market is closed right now — nothing to stream", fg=typer.colors.YELLOW)
        return

    typer.echo(f"streaming {', '.join(targets)} for up to {duration}s (Ctrl+C to stop early)...")
    try:
        asyncio.run(asyncio.wait_for(feed.run(), timeout=duration))
    except TimeoutError:
        pass
    except MissingAccessToken as exc:
        typer.secho(str(exc), fg=typer.colors.RED)
        raise typer.Exit(code=1) from exc
    except KeyboardInterrupt:
        pass
    feed.flush()
    typer.echo("stopped.")


@backtest_app.command("run")
def backtest_run(
    strategy_path: Annotated[
        str,
        typer.Argument(help="Registry name (see `pt strategy list`) or module:ClassName"),
    ],
    symbols: Annotated[
        list[str] | None,
        typer.Argument(help="Symbols to backtest (default: trading.universe from config)."),
    ] = None,
    interval: Annotated[Interval, typer.Option("--interval", "-i")] = Interval.D1,
    from_str: Annotated[str | None, typer.Option("--from", help="YYYY-MM-DD")] = None,
    to_str: Annotated[str | None, typer.Option("--to", help="YYYY-MM-DD")] = None,
    capital: Annotated[
        str | None, typer.Option("--capital", help="Total capital, ₹ (default: risk.capital)")
    ] = None,
    risk_pct: Annotated[
        str | None,
        typer.Option("--risk-pct", help="Per-trade risk % (default: risk.risk_per_trade_pct)"),
    ] = None,
    params_json: Annotated[
        str | None,
        typer.Option("--params", help="JSON strategy params, e.g. '{\"fast_period\":5}'"),
    ] = None,
) -> None:
    """Run a backtest across one or more symbols and persist the result."""
    from personaltrade.backtest.run import run_backtest_for_symbols
    from personaltrade.data.store.db import session_scope
    from personaltrade.strategy.base import construct_strategy
    from personaltrade.strategy.registry import UnknownStrategy, resolve_strategy_class

    cfg = _load_config_or_exit()
    setup_logging(cfg.log)
    targets = symbols or cfg.trading.universe
    if not targets:
        typer.secho("no symbols given and trading.universe is empty", fg=typer.colors.RED)
        raise typer.Exit(code=1)

    try:
        strategy_cls = resolve_strategy_class(strategy_path)
    except UnknownStrategy as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc

    params = strategy_cls.params_schema.model_validate(
        jsonlib.loads(params_json) if params_json else {}
    )
    strategy = construct_strategy(strategy_cls, params)

    to_date = _parse_date(to_str, date.today())
    from_date = _parse_date(from_str, to_date - timedelta(days=_DEFAULT_LOOKBACK_DAYS[interval]))
    store, factory = _open_store_and_session(cfg)

    try:
        with session_scope(factory) as session:
            result = run_backtest_for_symbols(
                strategy,
                targets,
                interval,
                from_date,
                to_date,
                session=session,
                candle_store=store,
                initial_capital=Decimal(capital) if capital else cfg.risk.capital,
                risk_per_trade_pct=Decimal(risk_pct) if risk_pct else cfg.risk.risk_per_trade_pct,
                cost_rates=cfg.costs,
                backtest_cfg=cfg.backtest,
            )
    except PersonalTradeError as exc:
        typer.secho(f"backtest FAILED: {exc}", fg=typer.colors.RED)
        raise typer.Exit(code=1) from exc

    pm = result.portfolio_metrics
    typer.secho(
        f"backtest_run_id={result.backtest_run_id} strategy={strategy.name} "
        f"symbols={','.join(targets)} trades={pm.total_trades} closed={pm.closed_trades}",
        fg=typer.colors.GREEN,
    )
    typer.echo(
        f"  CAGR={pm.cagr:.2%} Sharpe={pm.sharpe:.2f} MaxDD={pm.max_drawdown:.2%} "
        f"WinRate={pm.win_rate:.2%} Expectancy=₹{pm.expectancy:.2f} "
        f"ProfitFactor={pm.profit_factor:.2f}"
    )
    for sr in result.per_symbol:
        typer.echo(
            f"  {sr.symbol}: trades={len(sr.result.trades)} CAGR={sr.metrics.cagr:.2%} "
            f"MaxDD={sr.metrics.max_drawdown:.2%}"
        )


@backtest_app.command("sweep")
def backtest_sweep(
    strategy_path: Annotated[
        str, typer.Argument(help="Registry name (see `pt strategy list`) or module:ClassName")
    ],
    symbols: Annotated[
        list[str] | None,
        typer.Argument(help="Symbols to backtest (default: trading.universe from config)."),
    ] = None,
    grid_json: Annotated[
        str,
        typer.Option(
            "--grid", help='JSON param grid, e.g. \'{"fast_period":[5,10],"slow_period":[20,30]}\''
        ),
    ] = "{}",
    interval: Annotated[Interval, typer.Option("--interval", "-i")] = Interval.D1,
    from_str: Annotated[str | None, typer.Option("--from", help="YYYY-MM-DD")] = None,
    to_str: Annotated[str | None, typer.Option("--to", help="YYYY-MM-DD")] = None,
    oos_fraction: Annotated[
        float,
        typer.Option(
            "--oos-fraction",
            help="Fraction of the range held out as out-of-sample (chronological, never shuffled).",
        ),
    ] = 0.3,
    capital: Annotated[str | None, typer.Option("--capital")] = None,
    risk_pct: Annotated[str | None, typer.Option("--risk-pct")] = None,
) -> None:
    """Grid-sweep strategy parameters with an enforced in-sample/out-of-sample split.

    Guards against reading in-sample-only results as if they were real edge
    (ROADMAP M7): every combination is scored on both windows, sorted by
    out-of-sample Sharpe, so an overfit combo that only shines in-sample is
    visibly not the top row.
    """
    from personaltrade.backtest.sweep import InvalidSweepGrid, SweepResult, run_parameter_sweep
    from personaltrade.data.store.db import session_scope
    from personaltrade.strategy.registry import UnknownStrategy, resolve_strategy_class

    cfg = _load_config_or_exit()
    setup_logging(cfg.log)
    targets = symbols or cfg.trading.universe
    if not targets:
        typer.secho("no symbols given and trading.universe is empty", fg=typer.colors.RED)
        raise typer.Exit(code=1)

    try:
        strategy_cls = resolve_strategy_class(strategy_path)
    except UnknownStrategy as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc

    grid: dict[str, list[object]] = jsonlib.loads(grid_json)
    combos = 1
    for values in grid.values():
        combos *= len(values)
    to_date = _parse_date(to_str, date.today())
    from_date = _parse_date(from_str, to_date - timedelta(days=_DEFAULT_LOOKBACK_DAYS[interval]))
    typer.echo(f"sweeping {combos} combination(s) x 2 windows (in-sample + out-of-sample)...")

    store, factory = _open_store_and_session(cfg)
    try:
        with session_scope(factory) as session:
            results = run_parameter_sweep(
                strategy_cls,
                grid,
                targets,
                interval,
                from_date,
                to_date,
                session=session,
                candle_store=store,
                initial_capital=Decimal(capital) if capital else cfg.risk.capital,
                risk_per_trade_pct=Decimal(risk_pct) if risk_pct else cfg.risk.risk_per_trade_pct,
                cost_rates=cfg.costs,
                backtest_cfg=cfg.backtest,
                oos_fraction=oos_fraction,
            )
    except InvalidSweepGrid as exc:
        typer.secho(f"sweep FAILED: {exc}", fg=typer.colors.RED)
        raise typer.Exit(code=1) from exc

    def sort_key(r: SweepResult) -> tuple[int, float]:
        return (1, 0.0) if r.out_of_sample is None else (0, -r.out_of_sample.sharpe)

    results.sort(key=sort_key)
    for r in results:
        if r.error:
            typer.secho(f"  {r.params}: ERROR — {r.error}", fg=typer.colors.RED)
            continue
        assert r.in_sample is not None and r.out_of_sample is not None
        typer.echo(
            f"  {r.params}: "
            f"IS[Sharpe={r.in_sample.sharpe:.2f} CAGR={r.in_sample.cagr:.2%} "
            f"trades={r.in_sample.closed_trades}] "
            f"OOS[Sharpe={r.out_of_sample.sharpe:.2f} CAGR={r.out_of_sample.cagr:.2%} "
            f"trades={r.out_of_sample.closed_trades}]"
        )


@strategy_app.command("list")
def strategy_list() -> None:
    """List registered strategies."""
    from personaltrade.strategy.registry import list_strategies

    for name in list_strategies():
        typer.echo(name)


@kill_switch_app.command("status")
def kill_switch_status() -> None:
    """Show whether the kill switch is currently tripped."""
    from personaltrade.data.store.db import session_scope
    from personaltrade.risk.kill_switch import KillSwitch

    cfg = _load_config_or_exit()
    _, factory = _open_store_and_session(cfg)
    with session_scope(factory) as session:
        state = KillSwitch(session).state()
        if state.tripped:
            tripped_at = state.tripped_at.isoformat() if state.tripped_at else "unknown"
            typer.secho(f"TRIPPED since {tripped_at}: {state.reason}", fg=typer.colors.RED)
        else:
            typer.secho("clear", fg=typer.colors.GREEN)
        typer.echo(f"consecutive_errors={state.consecutive_errors}")


@kill_switch_app.command("trip")
def kill_switch_trip(
    reason: Annotated[str, typer.Option("--reason", help="Why trading is being halted.")],
) -> None:
    """Manually halt trading (Rule 14: one-command halt)."""
    from personaltrade.data.store.db import session_scope
    from personaltrade.risk.kill_switch import KillSwitch

    cfg = _load_config_or_exit()
    _, factory = _open_store_and_session(cfg)
    with session_scope(factory) as session:
        state = KillSwitch(session).trip(reason, detail={"source": "cli"})
    typer.secho(f"kill switch TRIPPED: {state.reason}", fg=typer.colors.RED)


@kill_switch_app.command("reset")
def kill_switch_reset(
    reason: Annotated[str, typer.Option("--reason", help="Why it's safe to resume.")],
) -> None:
    """Reset a tripped kill switch. Requires a logged reason — never a silent no-op."""
    from personaltrade.data.store.db import session_scope
    from personaltrade.risk.kill_switch import KillSwitch, KillSwitchNotTripped

    cfg = _load_config_or_exit()
    _, factory = _open_store_and_session(cfg)
    try:
        with session_scope(factory) as session:
            KillSwitch(session).reset(reason)
    except KillSwitchNotTripped as exc:
        typer.secho(str(exc), fg=typer.colors.YELLOW)
        raise typer.Exit(code=1) from exc
    typer.secho("kill switch reset — clear", fg=typer.colors.GREEN)


def _build_paper_broker(
    cfg: AppConfig, session: Session, store: CandleStore, interval: Interval
) -> PaperBroker:
    from personaltrade.execution.paper.broker import PaperBroker
    from personaltrade.execution.paper.quotes import ReplayQuoteSource

    quotes = ReplayQuoteSource(store, interval)
    return PaperBroker(
        session,
        quotes,
        cost_rates=cfg.costs,
        paper_cfg=cfg.paper,
        initial_cash=cfg.risk.capital,
    )


@paper_app.command("status")
def paper_status(
    interval: Annotated[Interval, typer.Option("--interval", "-i")] = Interval.D1,
) -> None:
    """Show paper account funds and open positions."""
    from personaltrade.data.store.db import session_scope
    from personaltrade.data.store.repos import InstrumentRepository

    cfg = _load_config_or_exit()
    store, factory = _open_store_and_session(cfg)
    with session_scope(factory) as session:
        broker = _build_paper_broker(cfg, session, store, interval)
        funds = broker.get_funds()
        typer.echo(f"cash=₹{funds.cash} equity=₹{funds.equity}")
        positions = broker.get_positions()
        if not positions:
            typer.echo("no open positions")
            return
        instruments = InstrumentRepository(session)
        for p in positions:
            inst = instruments.get(p.instrument_id)
            symbol = inst.symbol if inst else f"instrument#{p.instrument_id}"
            typer.echo(f"  {symbol}: qty={p.qty} avg_price=₹{p.avg_price}")


@paper_app.command("order")
def paper_order(
    symbol: Annotated[str, typer.Argument(help="Instrument symbol, e.g. RELIANCE.")],
    side: Annotated[str, typer.Argument(help="BUY or SELL.")],
    qty: Annotated[int, typer.Argument(help="Quantity.")],
    order_type: Annotated[str, typer.Option("--type", help="market or limit.")] = "market",
    price: Annotated[
        str | None, typer.Option("--price", help="Limit price (required for --type limit).")
    ] = None,
    interval: Annotated[Interval, typer.Option("--interval", "-i")] = Interval.D1,
    exchange: Annotated[str, typer.Option("--exchange")] = "NSE",
) -> None:
    """Manually place a paper order (for testing/experimentation ahead of the M11 orchestrator)."""
    from uuid import uuid4

    from personaltrade.core.enums import OrderType, Side
    from personaltrade.data.store.db import session_scope
    from personaltrade.data.store.repos import InstrumentRepository
    from personaltrade.execution.broker import OrderRequest

    cfg = _load_config_or_exit()
    store, factory = _open_store_and_session(cfg)

    try:
        side_enum = Side(side.upper())
    except ValueError as exc:
        typer.secho(f"invalid side {side!r}; expected BUY or SELL", fg=typer.colors.RED)
        raise typer.Exit(code=1) from exc
    try:
        type_enum = OrderType(order_type.upper())
    except ValueError as exc:
        typer.secho(f"invalid --type {order_type!r}; expected market or limit", fg=typer.colors.RED)
        raise typer.Exit(code=1) from exc
    if type_enum == OrderType.LIMIT and price is None:
        typer.secho("--price is required for --type limit", fg=typer.colors.RED)
        raise typer.Exit(code=1)

    with session_scope(factory) as session:
        instrument = InstrumentRepository(session).get_by_symbol(symbol, exchange)
        if instrument is None:
            typer.secho(f"{symbol} ({exchange}) not in instruments table", fg=typer.colors.RED)
            raise typer.Exit(code=1)

        broker = _build_paper_broker(cfg, session, store, interval)
        request = OrderRequest(
            client_order_id=str(uuid4()),
            instrument_id=instrument.id,
            side=side_enum,
            order_type=type_enum,
            qty=qty,
            limit_price=Decimal(price) if price is not None else None,
        )
        ack = broker.place_order(request)
        status = broker.get_order_status(ack.client_order_id)
    typer.secho(
        f"order {status.client_order_id} ({ack.broker_order_id}): "
        f"state={status.state} filled={status.filled_qty}/{status.qty} "
        f"avg_fill_price={status.avg_fill_price}",
        fg=typer.colors.GREEN,
    )


def _print_report(report: PerformanceReport) -> None:
    s = report.summary
    typer.echo(f"since {report.since.isoformat()}")
    typer.echo(f"realized=₹{s.realized_pnl} unrealized=₹{s.unrealized_pnl} total=₹{s.total_pnl}")
    typer.echo(
        f"closed_trades={s.closed_trades} win_rate={s.win_rate:.1%} "
        f"expectancy=₹{s.expectancy:.2f} profit_factor={s.profit_factor:.2f}"
    )
    typer.echo(f"cagr={s.cagr:.2%} sharpe={s.sharpe:.2f} max_drawdown={s.max_drawdown:.2%}")

    if report.by_instrument:
        typer.echo("by instrument:")
        for b in report.by_instrument:
            typer.echo(
                f"  {b.label}: pnl=₹{b.realized_pnl} trades={b.closed_trades} "
                f"win_rate={b.win_rate:.1%}"
            )

    if report.by_strategy:
        typer.echo("by strategy:")
        for b in report.by_strategy:
            typer.echo(
                f"  {b.label}: pnl=₹{b.realized_pnl} trades={b.closed_trades} "
                f"win_rate={b.win_rate:.1%}"
            )

    if report.journal:
        typer.echo("journal (closed trades):")
        for entry in report.journal:
            typer.echo(
                f"  {entry.symbol} {entry.side.value} qty={entry.qty} "
                f"entry={entry.entry_price}@{entry.entry_at.isoformat()} "
                f"exit={entry.exit_price}@{entry.exit_at.isoformat()} "
                f"pnl=₹{entry.realized_pnl} costs=₹{entry.total_costs}"
            )
    else:
        typer.echo("journal: no closed trades in this period")


def _run_report(since: datetime) -> None:
    from personaltrade.analytics.reports import generate_report
    from personaltrade.data.store.db import session_scope

    cfg = _load_config_or_exit()
    store, factory = _open_store_and_session(cfg)
    with session_scope(factory) as session:
        report = generate_report(
            session,
            store,
            mode=Mode.PAPER,
            initial_cash=cfg.risk.capital,
            interval=Interval(cfg.trading.interval),
            since=since,
        )
    _print_report(report)


@report_app.command("daily")
def report_daily() -> None:
    """Today's (IST) P&L, breakdowns, and journal."""
    from personaltrade.core.calendar import ist_midnight_utc

    _run_report(ist_midnight_utc(datetime.now(UTC)))


@report_app.command("weekly")
def report_weekly() -> None:
    """This ISO week's (Monday to now, IST) P&L, breakdowns, and journal."""
    from personaltrade.core.calendar import ist_week_start_utc

    _run_report(ist_week_start_utc(datetime.now(UTC)))


@news_app.command("sync")
def news_sync(
    since_days: Annotated[
        int | None, typer.Option("--since-days", help="Override news.lookback_days.")
    ] = None,
) -> None:
    """Fetch every configured RSS source, dedup, and tag against the instrument universe."""
    from personaltrade.data.store.db import session_scope
    from personaltrade.intelligence.news.pipeline import ingest
    from personaltrade.intelligence.news.rss import RssNewsProvider

    cfg = _load_config_or_exit()
    setup_logging(cfg.log)
    if not cfg.news.sources:
        typer.secho("no news sources configured", fg=typer.colors.RED)
        raise typer.Exit(code=1)

    _, factory = _open_store_and_session(cfg)
    since = datetime.now(UTC) - timedelta(
        days=since_days if since_days is not None else cfg.news.lookback_days
    )
    providers = [
        RssNewsProvider(source.name, source.url, timeout=cfg.news.request_timeout_seconds)
        for source in cfg.news.sources
    ]

    with session_scope(factory) as session:
        results = ingest(session, providers, since=since, cfg=cfg.news)

    failures = 0
    for result in results:
        if result.error is not None:
            failures += 1
            typer.secho(f"{result.source}: FAILED — {result.error}", fg=typer.colors.RED)
            continue
        typer.secho(
            f"{result.source}: fetched={result.fetched} stored={result.stored}",
            fg=typer.colors.GREEN,
        )
    raise typer.Exit(code=1 if failures else 0)


@news_app.command("list")
def news_list(
    symbol: Annotated[str, typer.Argument(help="Instrument symbol, e.g. RELIANCE.")],
    days: Annotated[int, typer.Option("--days", help="Lookback window.")] = 7,
    exchange: Annotated[str, typer.Option("--exchange")] = "NSE",
) -> None:
    """News tagged to SYMBOL in the last N days."""
    from personaltrade.data.store.db import session_scope
    from personaltrade.data.store.repos import InstrumentRepository, NewsRepository

    cfg = _load_config_or_exit()
    _, factory = _open_store_and_session(cfg)
    since = datetime.now(UTC) - timedelta(days=days)

    with session_scope(factory) as session:
        instrument = InstrumentRepository(session).get_by_symbol(symbol, exchange)
        if instrument is None:
            typer.secho(f"{symbol} ({exchange}) not in instruments table", fg=typer.colors.RED)
            raise typer.Exit(code=1)
        items = NewsRepository(session).list_for_instrument(instrument.id, since)
        if not items:
            typer.echo(f"no news for {symbol} in the last {days} day(s)")
            return
        for item in items:
            ts = (item.published_at or item.ingested_at).isoformat()
            typer.echo(f"[{item.source}] {ts} {item.title}")
            typer.echo(f"    {item.url}")


@app.command()
def analyze(
    symbol: Annotated[str, typer.Argument(help="Instrument symbol, e.g. RELIANCE.")],
    interval: Annotated[Interval, typer.Option("--interval", "-i")] = Interval.D1,
    exchange: Annotated[str, typer.Option("--exchange")] = "NSE",
) -> None:
    """AI analysis for one instrument (ROADMAP M14): indicators + position +
    recent news -> a schema-validated, advisory-only assessment. Never touches
    orders (CLAUDE.md Rule 10) — every call is audited in `ai_analyses`."""
    from personaltrade.core.config import Secrets
    from personaltrade.data.store.db import session_scope
    from personaltrade.data.store.repos import (
        InstrumentRepository,
        NewsRepository,
        PositionRepository,
    )
    from personaltrade.intelligence.analysis.service import (
        AIAnalysisDisabled,
        AIBudgetExhausted,
        analyze_instrument,
    )
    from personaltrade.intelligence.analysis.snapshot import (
        NewsSnapshotItem,
        PositionSnapshot,
        build_market_snapshot,
    )
    from personaltrade.intelligence.llm.anthropic_provider import build_anthropic_provider
    from personaltrade.intelligence.llm.provider import LLMOutputInvalid, LLMProviderError

    cfg = _load_config_or_exit()
    setup_logging(cfg.log)
    if not cfg.ai.enabled:
        typer.secho(
            "AI analysis is disabled (ai.enabled=false in config)", fg=typer.colors.YELLOW
        )
        raise typer.Exit(code=1)

    store, factory = _open_store_and_session(cfg)
    with session_scope(factory) as session:
        instrument = InstrumentRepository(session).get_by_symbol(symbol, exchange)
        if instrument is None:
            typer.secho(f"{symbol} ({exchange}) not in instruments table", fg=typer.colors.RED)
            raise typer.Exit(code=1)

        frame = store.read(symbol, exchange, interval)
        if frame.empty:
            typer.secho(
                f"no stored candles for {symbol} {interval.value} — run `pt data sync` first",
                fg=typer.colors.RED,
            )
            raise typer.Exit(code=1)

        try:
            provider = build_anthropic_provider(Secrets(), cfg.ai)
        except ConfigError as exc:
            typer.secho(str(exc), fg=typer.colors.RED)
            raise typer.Exit(code=1) from exc

        position_row = PositionRepository(session).get_for(instrument.id, Mode.PAPER)
        position = None
        if position_row is not None and position_row.qty != 0:
            mark = Decimal(str(frame["close"].iloc[-1]))
            position = PositionSnapshot(
                qty=position_row.qty,
                avg_price=position_row.avg_price,
                unrealized_pnl=(mark - position_row.avg_price) * position_row.qty,
            )

        since = datetime.now(UTC) - timedelta(days=cfg.ai.news_lookback_days)
        news_rows = NewsRepository(session).list_for_instrument(instrument.id, since)
        news = [
            NewsSnapshotItem(
                news_item_id=item.id,
                source=item.source,
                published_at=item.published_at,
                title=item.title,
                body=item.body,
            )
            for item in news_rows[: cfg.ai.max_news_items]
        ]

        snapshot = build_market_snapshot(symbol, frame, position=position, news=news)

        try:
            outcome = analyze_instrument(session, provider, cfg.ai, instrument, snapshot)
        except AIAnalysisDisabled as exc:
            typer.secho(str(exc), fg=typer.colors.YELLOW)
            raise typer.Exit(code=1) from exc
        except AIBudgetExhausted as exc:
            typer.secho(f"AI budget exhausted: {exc}", fg=typer.colors.YELLOW)
            raise typer.Exit(code=1) from exc
        except (LLMProviderError, LLMOutputInvalid) as exc:
            typer.secho(f"AI analysis unavailable: {exc}", fg=typer.colors.RED)
            raise typer.Exit(code=1) from exc

        out = outcome.output
        record = outcome.record
        typer.secho(f"{symbol}: {out.stance} (conviction={out.conviction})", fg=typer.colors.GREEN)
        typer.echo(f"  news_impact={out.news_impact}")
        typer.echo(f"  summary: {out.summary}")
        if out.key_factors:
            typer.echo("  key factors:")
            for factor in out.key_factors:
                typer.echo(f"    - {factor}")
        if out.risks:
            typer.echo("  risks:")
            for risk in out.risks:
                typer.echo(f"    - {risk}")
        typer.echo(
            f"  model={record.model} tokens={record.input_tokens}+{record.output_tokens} "
            f"cost=${record.cost_usd}"
        )


@recommend_app.command("run")
def recommend_run(
    interval: Annotated[
        Interval | None,
        typer.Option("--interval", "-i", help="Defaults to recommendation.interval in config."),
    ] = None,
) -> None:
    """Screen trading.universe for signals, merge with AI analysis, and
    persist ranked recommendations (ROADMAP M15). AI is best-effort per
    instrument: disabled, budget-exhausted, or unreachable degrades that
    instrument's recommendation to deterministic-only, never blocks the run
    (CLAUDE.md Rule 10; docs/architecture/05-ai-data-flow.md)."""
    from personaltrade.core.config import Secrets
    from personaltrade.data.store.db import session_scope
    from personaltrade.intelligence.llm.anthropic_provider import build_anthropic_provider
    from personaltrade.intelligence.llm.provider import LLMProvider
    from personaltrade.intelligence.recommendation.engine import run_recommendation_cycle
    from personaltrade.strategy.base import construct_strategy
    from personaltrade.strategy.registry import UnknownStrategy, resolve_strategy_class

    cfg = _load_config_or_exit()
    setup_logging(cfg.log)

    targets = cfg.trading.universe
    if not targets:
        typer.secho("trading.universe is empty — nothing to screen", fg=typer.colors.RED)
        raise typer.Exit(code=1)

    try:
        strategy_cls = resolve_strategy_class(cfg.trading.strategy)
    except UnknownStrategy as exc:
        typer.secho(str(exc), fg=typer.colors.RED)
        raise typer.Exit(code=1) from exc
    params = strategy_cls.params_schema.model_validate(cfg.trading.strategy_params)
    strategy = construct_strategy(strategy_cls, params)
    rec_interval = interval or Interval(cfg.recommendation.interval)

    provider: LLMProvider | None = None
    if cfg.ai.enabled:
        try:
            provider = build_anthropic_provider(Secrets(), cfg.ai)
        except ConfigError as exc:
            typer.secho(
                f"note: {exc} — recommendations will be deterministic-only", fg=typer.colors.YELLOW
            )
    else:
        typer.secho(
            "note: ai.enabled=false — recommendations will be deterministic-only",
            fg=typer.colors.YELLOW,
        )

    store, factory = _open_store_and_session(cfg)
    with session_scope(factory) as session:
        candles_by_symbol: dict[str, pd.DataFrame] = {}
        for symbol in targets:
            frame = store.read(symbol, "NSE", rec_interval)
            if frame.empty:
                typer.secho(
                    f"note: no stored candles for {symbol} {rec_interval.value} — skipping",
                    fg=typer.colors.YELLOW,
                )
                continue
            candles_by_symbol[symbol] = frame

        if not candles_by_symbol:
            typer.secho(
                "no instruments had stored candles — nothing to screen", fg=typer.colors.RED
            )
            raise typer.Exit(code=1)

        results = run_recommendation_cycle(
            session, provider, cfg.ai, cfg.recommendation, strategy, candles_by_symbol
        )

        if not results:
            typer.echo("no recommendations — no instrument had a signal on its latest bar")
            return

        typer.secho(f"{len(results)} recommendation(s), best first:", fg=typer.colors.GREEN)
        for result in results:
            ai_note = (
                f"ai={result.ai_output.stance}/{result.ai_output.conviction}"
                if result.ai_output is not None
                else "ai=unavailable"
            )
            action_color = (
                typer.colors.GREEN
                if result.record.action.value in {"BUY", "SELL"}
                else typer.colors.WHITE
            )
            typer.secho(
                f"  #{result.record.rank} {result.instrument.symbol}: "
                f"{result.record.action.value} ({ai_note})",
                fg=action_color,
            )


@app.command()
def run(
    mode: Annotated[
        str,
        typer.Option(
            "--mode", help="Must match trading.mode in config (paper only; live arrives at M17)."
        ),
    ],
) -> None:
    """Run the live trading loop end-to-end for the session (ROADMAP M11)."""
    import asyncio

    from personaltrade.core.config import Secrets
    from personaltrade.core.enums import Mode as ModeEnum
    from personaltrade.core.events import EventBus
    from personaltrade.data.live.feed import LiveFeed
    from personaltrade.data.providers.upstox import UpstoxMarketData
    from personaltrade.data.store.db import session_scope
    from personaltrade.data.store.repos import InstrumentRepository
    from personaltrade.orchestrator.runner import LiveStrategyRunner
    from personaltrade.orchestrator.scheduler import LiveScheduler
    from personaltrade.orchestrator.service import Orchestrator
    from personaltrade.risk.sizing import FixedFractionalSizer
    from personaltrade.strategy.base import construct_strategy
    from personaltrade.strategy.registry import UnknownStrategy, resolve_strategy_class

    cfg = _load_config_or_exit()
    setup_logging(cfg.log)

    if mode != cfg.trading.mode:
        typer.secho(
            f"--mode {mode!r} does not match trading.mode={cfg.trading.mode!r} in config",
            fg=typer.colors.RED,
        )
        raise typer.Exit(code=1)
    if mode != "paper":
        typer.secho(
            "only --mode paper is implemented — live arrives at Milestone 17", fg=typer.colors.RED
        )
        raise typer.Exit(code=1)

    secrets = Secrets()
    if secrets.upstox_access_token is None:
        typer.secho(
            "no Upstox access token configured — set UPSTOX_ACCESS_TOKEN in .env "
            "(see .env.example). Automatic daily re-auth arrives at Milestone 17.",
            fg=typer.colors.RED,
        )
        raise typer.Exit(code=1)

    targets = cfg.trading.universe
    if not targets:
        typer.secho("trading.universe is empty — nothing to trade", fg=typer.colors.RED)
        raise typer.Exit(code=1)

    calendar = _calendar_or_none()
    if calendar is None:
        typer.secho(
            "NSE holiday calendar unavailable — cannot determine market hours", fg=typer.colors.RED
        )
        raise typer.Exit(code=1)

    try:
        strategy_cls = resolve_strategy_class(cfg.trading.strategy)
    except UnknownStrategy as exc:
        typer.secho(str(exc), fg=typer.colors.RED)
        raise typer.Exit(code=1) from exc
    params = strategy_cls.params_schema.model_validate(cfg.trading.strategy_params)
    interval = Interval(cfg.trading.interval)

    _, factory = _open_store_and_session(cfg)
    runners: dict[str, LiveStrategyRunner] = {}
    subscriptions: dict[str, list[Interval]] = {}
    with session_scope(factory) as session:
        instrument_repo = InstrumentRepository(session)
        for symbol in targets:
            inst = instrument_repo.get_by_symbol(symbol, "NSE")
            if inst is None:
                typer.secho(f"{symbol} (NSE) not in instruments table", fg=typer.colors.RED)
                raise typer.Exit(code=1)
            strategy = construct_strategy(strategy_cls, params)
            runners[inst.instrument_key] = LiveStrategyRunner(inst, strategy)
            subscriptions[inst.instrument_key] = [interval]

    provider = UpstoxMarketData(access_token=secrets.upstox_access_token.get_secret_value())
    bus = EventBus()
    feed = LiveFeed(provider, bus, calendar, subscriptions)
    orchestrator = Orchestrator(
        factory,
        feed,
        bus,
        runners,
        mode=ModeEnum.PAPER,
        risk_cfg=cfg.risk,
        sizer=FixedFractionalSizer(cfg.risk.risk_per_trade_pct),
        cost_rates=cfg.costs,
        paper_cfg=cfg.paper,
        initial_cash=cfg.risk.capital,
        strategy_name=cfg.trading.strategy,
        strategy_params=cfg.trading.strategy_params,
    )
    orchestrator.start_strategy_run()
    findings = orchestrator.reconcile()
    for finding in findings:
        typer.secho(
            f"reconciliation: {finding.client_order_id} was {finding.was_state} — marked FAILED",
            fg=typer.colors.YELLOW,
        )

    scheduler = LiveScheduler(orchestrator, calendar)
    typer.echo(
        f"pt run: trading {cfg.trading.strategy} on {', '.join(targets)} "
        f"@ {interval.value}, mode=paper (Ctrl+C to stop)"
    )

    async def _main() -> None:
        await scheduler.start()
        try:
            while True:
                await asyncio.sleep(3600)
        except asyncio.CancelledError:
            pass

    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        typer.echo("stopping...")
    finally:
        scheduler.shutdown()


def main() -> None:
    # Windows' default console codepage (cp1252) can't encode characters like
    # ₹ once stdout is piped/redirected (it loses the console's native UTF-8
    # handling). Reconfigure unconditionally so any future non-ASCII output
    # is safe too; guarded for streams that don't support it (e.g. test
    # runners' captured buffers).
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    app()
