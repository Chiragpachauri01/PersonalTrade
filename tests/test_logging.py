from __future__ import annotations

import json
from pathlib import Path

from personaltrade.core.config import LogConfig
from personaltrade.core.logging import get_logger, setup_logging


def _read_log_lines(log_dir: Path) -> list[dict[str, object]]:
    text = (log_dir / "personaltrade.log").read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def test_json_log_written_to_file(tmp_path: Path) -> None:
    log_dir = tmp_path / "logs"
    setup_logging(LogConfig(level="INFO", format="json", dir=log_dir))

    get_logger("test").info("order_submitted", order_id="ord-1", qty=10)

    lines = _read_log_lines(log_dir)
    assert any(
        line["event"] == "order_submitted" and line["qty"] == 10 and "timestamp" in line
        for line in lines
    )


def test_secret_fields_are_redacted(tmp_path: Path) -> None:
    log_dir = tmp_path / "logs"
    setup_logging(LogConfig(level="INFO", format="json", dir=log_dir))

    get_logger("test").info(
        "auth_refresh",
        api_key="sk-live-abc123",
        access_token="tok-xyz",
        password="hunter2",
        symbol="RELIANCE",
    )

    line = next(li for li in _read_log_lines(log_dir) if li["event"] == "auth_refresh")
    assert line["api_key"] == "[REDACTED]"
    assert line["access_token"] == "[REDACTED]"
    assert line["password"] == "[REDACTED]"
    assert line["symbol"] == "RELIANCE"  # non-secret fields untouched


def test_level_filtering(tmp_path: Path) -> None:
    log_dir = tmp_path / "logs"
    setup_logging(LogConfig(level="WARNING", format="json", dir=log_dir))

    logger = get_logger("test")
    logger.info("too_quiet")
    logger.warning("loud_enough")

    events = [line["event"] for line in _read_log_lines(log_dir)]
    assert "loud_enough" in events
    assert "too_quiet" not in events
