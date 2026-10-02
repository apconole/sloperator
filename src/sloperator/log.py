"""Structured application logging.

Logs are JSON objects (one per line) where practical, with registered
secret values masked before output. Extra structured fields may be passed
via the standard logging ``extra`` mechanism; see EXTRA_FIELDS.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sloperator.config.models import LoggingConfig
from sloperator.security.redaction import redact_text

EXTRA_FIELDS = ("task_id", "project_id", "role", "backend", "model")


class JsonFormatter(logging.Formatter):
    """Format records as single-line JSON objects with secret masking."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": redact_text(record.getMessage()),
        }
        if record.exc_info:
            payload["exception"] = redact_text(self.formatException(record.exc_info))
        for field in EXTRA_FIELDS:
            value = getattr(record, field, None)
            if value is not None:
                payload[field] = value
        return json.dumps(payload, ensure_ascii=False)


class PlainFormatter(logging.Formatter):
    """Human-readable format, still with secret masking applied."""

    def format(self, record: logging.LogRecord) -> str:
        return redact_text(super().format(record))


def configure_logging(config: LoggingConfig, logs_dir: Path) -> Path | None:
    """Configure root logging for the application.

    Installs a stderr handler and, when a logs directory is available, an
    application.log file handler. Returns the log file path if one was
    created. Idempotent: repeated calls replace previously installed
    handlers rather than stacking them.
    """
    root = logging.getLogger()
    root.setLevel(config.level)

    for handler in list(root.handlers):
        if getattr(handler, "_sloperator_managed", False):
            root.removeHandler(handler)
            handler.close()

    formatter: logging.Formatter
    if config.json_format:
        formatter = JsonFormatter()
    else:
        formatter = PlainFormatter("%(asctime)s %(levelname)s %(name)s %(message)s")

    stderr_handler: logging.Handler = logging.StreamHandler()
    stderr_handler.setFormatter(formatter)
    _mark_managed(stderr_handler)
    root.addHandler(stderr_handler)

    log_file: Path | None = None
    if logs_dir.is_dir():
        log_file = logs_dir / "application.log"
        file_handler: logging.Handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setFormatter(formatter)
        _mark_managed(file_handler)
        root.addHandler(file_handler)

    return log_file


def _mark_managed(handler: logging.Handler) -> None:
    handler._sloperator_managed = True  # type: ignore[attr-defined]
