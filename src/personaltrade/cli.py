"""`pt` — the PersonalTrade command line. Grows with each milestone."""

from __future__ import annotations

import os
from datetime import date, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer

if TYPE_CHECKING:
    from sqlalchemy.orm import Session, sessionmaker

    from personaltrade.data.store.candles import CandleStore
from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig
from sqlalchemy import inspect

from personaltrade import __version__
from personaltrade.core.calendar import NSECalendar
from personaltrade.core.config import AppConfig, load_config
from personaltrade.core.enums import Interval
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


@app.command()
def run() -> None:
    """Run the trading loop. Available from Milestone 11 (Trade Orchestrator)."""
    cfg = _load_config_or_exit()
    setup_logging(cfg.log)
    typer.secho(
        "The trading loop arrives with Milestone 11. See docs/ROADMAP.md.",
        fg=typer.colors.YELLOW,
    )
    raise typer.Exit(code=2)


def main() -> None:
    app()
