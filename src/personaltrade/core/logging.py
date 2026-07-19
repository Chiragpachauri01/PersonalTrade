"""structlog setup: console (always) + rotating file (when log.dir is set).

Renderer follows config: JSON lines for machines, pretty console for dev.
A redaction processor masks any field whose name suggests a secret — logs must
never contain credentials (Rule 15).
"""

from __future__ import annotations

import logging
import logging.handlers
import sys

import structlog
from structlog.typing import EventDict, WrappedLogger

from personaltrade.core.config import LogConfig

_REDACT_MARKERS = ("key", "secret", "token", "password", "authorization", "credential")


def redact_secrets(_: WrappedLogger, __: str, event_dict: EventDict) -> EventDict:
    """Mask values of any log field whose name looks secret-bearing."""
    for field in event_dict:
        if any(marker in field.lower() for marker in _REDACT_MARKERS):
            event_dict[field] = "[REDACTED]"
    return event_dict


def setup_logging(cfg: LogConfig) -> None:
    """Configure structlog + stdlib logging. Safe to call more than once."""
    shared_processors: list[structlog.typing.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        redact_secrets,
    ]

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    renderer: structlog.typing.Processor = (
        structlog.processors.JSONRenderer()
        if cfg.format == "json"
        else structlog.dev.ConsoleRenderer()
    )
    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.format_exc_info,
            renderer,
        ],
    )

    root = logging.getLogger()
    root.handlers.clear()

    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(formatter)
    root.addHandler(console)

    if cfg.dir is not None:
        cfg.dir.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.TimedRotatingFileHandler(
            cfg.dir / "personaltrade.log",
            when="midnight",
            backupCount=14,
            encoding="utf-8",
            utc=True,
        )
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)

    root.setLevel(cfg.level)


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    return structlog.stdlib.get_logger(name)
