from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from personaltrade.core.config import load_config
from personaltrade.core.errors import ConfigError


def test_defaults_load_paper_mode(config_dir: Path) -> None:
    cfg = load_config(config_dir)
    assert cfg.trading.mode == "paper"
    assert cfg.trading.live_orders_enabled is False
    assert cfg.trading.universe == ["RELIANCE"]
    assert cfg.ai.model == "claude-opus-4-8"


def test_soak_defaults(config_dir: Path) -> None:
    cfg = load_config(config_dir)
    assert cfg.soak.target_days == 28
    assert cfg.soak.min_closed_trades == 20
    assert cfg.soak.require_positive_net_pnl is True


def test_money_fields_are_decimal(config_dir: Path) -> None:
    cfg = load_config(config_dir)
    assert isinstance(cfg.risk.capital, Decimal)
    assert cfg.risk.capital == Decimal("500000")
    assert cfg.risk.risk_per_trade_pct == Decimal("1.0")


def test_local_yaml_overrides_default(config_dir: Path) -> None:
    (config_dir / "local.yaml").write_text("risk:\n  max_open_positions: 9\n", encoding="utf-8")
    cfg = load_config(config_dir)
    assert cfg.risk.max_open_positions == 9
    # untouched sections keep their defaults
    assert cfg.trading.mode == "paper"


def test_env_overrides_yaml(config_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (config_dir / "local.yaml").write_text("risk:\n  max_open_positions: 9\n", encoding="utf-8")
    monkeypatch.setenv("PT_RISK__MAX_OPEN_POSITIONS", "3")
    cfg = load_config(config_dir)
    assert cfg.risk.max_open_positions == 3


def test_unknown_top_level_key_rejected(config_dir: Path) -> None:
    (config_dir / "local.yaml").write_text("riskk:\n  capital: '1'\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="unknown top-level keys"):
        load_config(config_dir)


def test_unknown_section_key_rejected(config_dir: Path) -> None:
    (config_dir / "local.yaml").write_text(
        "trading:\n  mode: paper\n  turbo: true\n", encoding="utf-8"
    )
    with pytest.raises(ConfigError, match="invalid configuration"):
        load_config(config_dir)


def test_invalid_mode_rejected(config_dir: Path) -> None:
    (config_dir / "local.yaml").write_text("trading:\n  mode: yolo\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="invalid configuration"):
        load_config(config_dir)


def test_missing_default_yaml_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="missing config file"):
        load_config(tmp_path / "nowhere")


def test_unrelated_pt_env_vars_ignored(config_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Secrets like PT_TOKEN_ENCRYPTION_KEY share the PT_ prefix but are not config fields.
    monkeypatch.setenv("PT_TOKEN_ENCRYPTION_KEY", "super-secret")
    cfg = load_config(config_dir)
    assert cfg.trading.mode == "paper"
