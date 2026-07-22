"""Layered configuration (see docs/architecture/06-config-security-ops.md).

Precedence, highest first:
    1. environment variables  — PT_ prefix, nested keys via __  (PT_TRADING__MODE=paper)
    2. config/local.yaml      — git-ignored user overrides
    3. config/default.yaml    — committed safe defaults

Secrets never live in YAML; they come from .env / environment via `Secrets`.
Loading fails fast on unknown keys or invalid values (ConfigError).
"""

from __future__ import annotations

import os
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, SecretStr, ValidationError
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    YamlConfigSettingsSource,
)

from personaltrade.core.errors import ConfigError


class TradingConfig(BaseModel):
    model_config = {"extra": "forbid"}

    mode: Literal["paper", "live"] = "paper"
    live_orders_enabled: bool = False  # second key of the two-key live gate (ADR-008)
    universe: list[str] = Field(default_factory=list)
    #: Which registered strategy (strategy/registry.py) `pt run` trades live/paper,
    #: and its params — validated against that strategy's own params_schema at
    #: startup (ROADMAP M11), the same way `pt backtest run --params` is.
    strategy: str = "sma_crossover"
    strategy_params: dict[str, Any] = Field(default_factory=dict)
    interval: Literal["1m", "15m"] = "1m"  # bar interval the live feed aggregates to


class KillSwitchConfig(BaseModel):
    model_config = {"extra": "forbid"}

    max_consecutive_errors: int = Field(default=5, ge=1)


class RiskConfig(BaseModel):
    model_config = {"extra": "forbid"}

    capital: Decimal = Field(default=Decimal("500000"), gt=0)
    risk_per_trade_pct: Decimal = Field(default=Decimal("1.0"), gt=0, le=10)
    max_open_positions: int = Field(default=5, ge=1)
    max_daily_loss_pct: Decimal = Field(default=Decimal("3.0"), gt=0, le=100)
    kill_switch: KillSwitchConfig = Field(default_factory=KillSwitchConfig)


class ModelPricing(BaseModel):
    """USD per million tokens (ROADMAP M14). Published rates drift — verify
    against the provider's current pricing page before relying on cost totals
    for anything beyond a rough budget guard."""

    model_config = {"extra": "forbid"}

    input_per_mtok: Decimal = Field(ge=0)
    output_per_mtok: Decimal = Field(ge=0)


class AIConfig(BaseModel):
    model_config = {"extra": "forbid"}

    enabled: bool = True
    provider: str = "anthropic"
    model: str = "claude-opus-4-8"
    max_tokens_per_call: int = Field(default=2048, ge=256)
    daily_call_cap: int = Field(default=100, ge=0)
    monthly_usd_cap: Decimal = Field(default=Decimal("25"), ge=0)
    #: How much recent tagged news (ROADMAP M13) to fold into the prompt.
    news_lookback_days: int = Field(default=3, ge=0)
    max_news_items: int = Field(default=5, ge=0)
    #: Keyed by the canonical `model` id (never a backend-specific id — see
    #: ADR-013/ADR-024 for why Bedrock/direct resolve to different wire IDs).
    pricing: dict[str, ModelPricing] = Field(
        default_factory=lambda: {
            "claude-opus-4-8": ModelPricing(
                input_per_mtok=Decimal("5"), output_per_mtok=Decimal("25")
            ),
            "claude-haiku-4-5": ModelPricing(
                input_per_mtok=Decimal("1"), output_per_mtok=Decimal("5")
            ),
        }
    )


class DataConfig(BaseModel):
    model_config = {"extra": "forbid"}

    candle_root: Path = Path("data/candles")
    db_path: Path = Path("data/personaltrade.db")


class LogConfig(BaseModel):
    model_config = {"extra": "forbid"}

    level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    format: Literal["json", "console"] = "json"
    dir: Path | None = Path("data/logs")


class CostConfig(BaseModel):
    """Indian NSE equity cost stack (see docs/architecture/ADRS.md ADR-013).

    Defaults are the commonly-cited structure as of this writing (typical
    discount-broker brokerage + statutory STT/stamp-duty/exchange/SEBI/GST
    rates). Government and exchange rates change periodically and brokerage
    varies by broker — verify against your actual broker's current rate card
    before relying on these for anything beyond research/paper trading.
    """

    model_config = {"extra": "forbid"}

    brokerage_pct: Decimal = Field(default=Decimal("0.0003"), ge=0)  # 0.03%
    brokerage_max: Decimal = Field(default=Decimal("20"), ge=0)  # flat cap per order, ₹
    stt_delivery_pct: Decimal = Field(default=Decimal("0.001"), ge=0)  # 0.1%, both legs
    stt_intraday_sell_pct: Decimal = Field(default=Decimal("0.00025"), ge=0)  # 0.025%, sell leg
    exchange_txn_pct: Decimal = Field(default=Decimal("0.0000297"), ge=0)  # NSE approx
    sebi_pct: Decimal = Field(default=Decimal("0.000001"), ge=0)  # ₹10/crore
    stamp_duty_buy_delivery_pct: Decimal = Field(default=Decimal("0.00015"), ge=0)  # 0.015%
    stamp_duty_buy_intraday_pct: Decimal = Field(default=Decimal("0.00003"), ge=0)  # 0.003%
    gst_pct: Decimal = Field(default=Decimal("0.18"), ge=0)  # on brokerage+exchange+SEBI only


class BacktestConfig(BaseModel):
    model_config = {"extra": "forbid"}

    slippage_bps: Decimal = Field(default=Decimal("5"), ge=0)  # adverse fill slippage
    default_segment: Literal["DELIVERY", "INTRADAY"] = "DELIVERY"


class PaperConfig(BaseModel):
    """Paper Broker execution simulation (ROADMAP M9). Initial cash comes from
    `risk.capital`, not duplicated here — "how much capital the account starts
    with" is one setting, used identically for sizing (risk) and funds (paper)."""

    model_config = {"extra": "forbid"}

    slippage_bps: Decimal = Field(default=Decimal("5"), ge=0)  # adverse fill slippage
    segment: Literal["DELIVERY", "INTRADAY"] = "DELIVERY"
    latency_ms: int = Field(default=250, ge=0)  # simulated order-ack/fill delay


class NewsSourceConfig(BaseModel):
    model_config = {"extra": "forbid"}

    name: str
    url: str


class NewsConfig(BaseModel):
    """News ingestion (ROADMAP M13). Sources are RSS feeds, config-driven so a
    flaky/dead feed is a config edit, never a code change (`NewsProvider` is
    one generic `RssNewsProvider` per source, ADR-023)."""

    model_config = {"extra": "forbid"}

    sources: list[NewsSourceConfig] = Field(
        default_factory=lambda: [
            NewsSourceConfig(
                name="economic_times_markets",
                url="https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms",
            ),
            NewsSourceConfig(name="livemint_markets", url="https://www.livemint.com/rss/markets"),
        ]
    )
    lookback_days: int = Field(default=2, ge=1)  # `pt news sync`'s default "since" window
    max_title_length: int = Field(default=300, ge=1)
    max_body_length: int = Field(default=2000, ge=1)
    request_timeout_seconds: float = Field(default=15.0, gt=0)


class AppConfig(BaseSettings):
    """Top level is extra="ignore" so unrelated PT_* env vars (secrets) don't break loading.

    Section typos in YAML are still caught: top-level keys are pre-validated in
    load_config, and each section model forbids unknown keys.
    """

    model_config = SettingsConfigDict(
        env_prefix="PT_",
        env_nested_delimiter="__",
        extra="ignore",
    )

    trading: TradingConfig = Field(default_factory=TradingConfig)
    risk: RiskConfig = Field(default_factory=RiskConfig)
    ai: AIConfig = Field(default_factory=AIConfig)
    data: DataConfig = Field(default_factory=DataConfig)
    log: LogConfig = Field(default_factory=LogConfig)
    costs: CostConfig = Field(default_factory=CostConfig)
    backtest: BacktestConfig = Field(default_factory=BacktestConfig)
    paper: PaperConfig = Field(default_factory=PaperConfig)
    news: NewsConfig = Field(default_factory=NewsConfig)


class Secrets(BaseSettings):
    """Secrets from environment / .env only — never from YAML (Rule 15).

    All optional until the milestone that needs them; callers must handle None.
    """

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    upstox_api_key: SecretStr | None = None
    upstox_api_secret: SecretStr | None = None
    upstox_redirect_uri: str = "http://localhost:8700/auth/callback"
    #: Stopgap for ROADMAP M10 (`pt data stream`): a manually-obtained Upstox
    #: access token (e.g. from their developer console), since the automatic
    #: daily re-auth flow is M17. Expires daily like any Upstox token — M17
    #: replaces manual pasting with the real OAuth + refresh flow.
    upstox_access_token: SecretStr | None = None
    anthropic_api_key: SecretStr | None = None
    #: Amazon Bedrock long-term API key (bearer token) — the default LLM
    #: backend for M14 while AWS free-tier credits last (ADR-013). Falls back
    #: to `anthropic_api_key` (the direct API) when unset.
    aws_bearer_token_bedrock: SecretStr | None = None
    aws_region: str | None = None
    pt_token_encryption_key: SecretStr | None = None
    pt_dashboard_password_hash: SecretStr | None = None


def _reject_unknown_top_level_keys(yaml_file: Path) -> None:
    raw = yaml.safe_load(yaml_file.read_text(encoding="utf-8"))
    if raw is None:
        return
    if not isinstance(raw, dict):
        raise ConfigError(f"{yaml_file}: top level must be a mapping")
    unknown = set(raw) - set(AppConfig.model_fields)
    if unknown:
        raise ConfigError(f"{yaml_file}: unknown top-level keys: {sorted(unknown)}")


def load_config(config_dir: Path | None = None) -> AppConfig:
    """Load and validate the layered configuration; raise ConfigError on any problem.

    `config_dir` defaults to $PT_CONFIG_DIR or ./config.
    """
    resolved = config_dir or Path(os.environ.get("PT_CONFIG_DIR", "config"))
    default_file = resolved / "default.yaml"
    local_file = resolved / "local.yaml"

    if not default_file.is_file():
        raise ConfigError(f"missing config file: {default_file}")
    _reject_unknown_top_level_keys(default_file)
    if local_file.is_file():
        _reject_unknown_top_level_keys(local_file)

    class _Loaded(AppConfig):
        @classmethod
        def settings_customise_sources(
            cls,
            settings_cls: type[BaseSettings],
            init_settings: PydanticBaseSettingsSource,
            env_settings: PydanticBaseSettingsSource,
            dotenv_settings: PydanticBaseSettingsSource,
            file_secret_settings: PydanticBaseSettingsSource,
        ) -> tuple[PydanticBaseSettingsSource, ...]:
            sources: list[PydanticBaseSettingsSource] = [init_settings, env_settings]
            if local_file.is_file():
                sources.append(YamlConfigSettingsSource(settings_cls, yaml_file=local_file))
            sources.append(YamlConfigSettingsSource(settings_cls, yaml_file=default_file))
            return tuple(sources)

    try:
        return _Loaded()
    except ValidationError as exc:
        raise ConfigError(f"invalid configuration: {exc}") from exc


def load_secrets() -> Secrets:
    return Secrets()
