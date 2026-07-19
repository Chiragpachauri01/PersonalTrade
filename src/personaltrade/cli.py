"""`pt` — the PersonalTrade command line. Grows with each milestone."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from personaltrade import __version__
from personaltrade.core.config import load_config
from personaltrade.core.errors import ConfigError
from personaltrade.core.logging import setup_logging

app = typer.Typer(
    name="pt",
    help="PersonalTrade — AI trading research & execution platform (NSE via Upstox).",
    no_args_is_help=True,
)
config_app = typer.Typer(help="Inspect and validate configuration.", no_args_is_help=True)
app.add_typer(config_app, name="config")

ConfigDirOption = Annotated[
    Path | None,
    typer.Option("--config-dir", help="Config directory (default: $PT_CONFIG_DIR or ./config)."),
]


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


@config_app.command("validate")
def config_validate(config_dir: ConfigDirOption = None) -> None:
    """Load the layered config; exit non-zero if anything is invalid."""
    try:
        cfg = load_config(config_dir)
    except ConfigError as exc:
        typer.secho(f"config INVALID: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    typer.secho(
        f"config OK — mode={cfg.trading.mode} "
        f"live_orders_enabled={cfg.trading.live_orders_enabled}",
        fg=typer.colors.GREEN,
    )


@config_app.command("show")
def config_show(config_dir: ConfigDirOption = None) -> None:
    """Print the effective (merged) configuration as JSON. Secrets are never included."""
    try:
        cfg = load_config(config_dir)
    except ConfigError as exc:
        typer.secho(f"config INVALID: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(cfg.model_dump_json(indent=2))


@app.command()
def run() -> None:
    """Run the trading loop. Available from Milestone 11 (Trade Orchestrator)."""
    cfg = load_config()
    setup_logging(cfg.log)
    typer.secho(
        "The trading loop arrives with Milestone 11. See docs/ROADMAP.md.",
        fg=typer.colors.YELLOW,
    )
    raise typer.Exit(code=2)


def main() -> None:
    app()
