"""Tests for structured application logging setup."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from sloperator.config import LoggingConfig
from sloperator.log import configure_logging
from sloperator.security.redaction import REDACTED, register_secret


def _managed_handler_count() -> int:
    return sum(
        1
        for handler in logging.getLogger().handlers
        if getattr(handler, "_sloperator_managed", False)
    )


def test_configure_logging_writes_json(tmp_path: Path) -> None:
    logs = tmp_path / "logs"
    logs.mkdir()
    log_file = configure_logging(LoggingConfig(level="INFO", json_format=True), logs)
    assert log_file == logs / "application.log"

    logging.getLogger("sloperator.test").info("hello %s", "world", extra={"task_id": "task-1"})

    lines = [line for line in log_file.read_text(encoding="utf-8").splitlines() if line]
    record = json.loads(lines[-1])
    assert record["message"] == "hello world"
    assert record["level"] == "INFO"
    assert record["task_id"] == "task-1"


def test_configure_logging_masks_registered_secrets(tmp_path: Path) -> None:
    logs = tmp_path / "logs"
    logs.mkdir()
    log_file = configure_logging(LoggingConfig(level="INFO", json_format=True), logs)
    assert log_file is not None

    register_secret("s3cr3t")
    logging.getLogger("sloperator.test").info("the key is s3cr3t")

    text = log_file.read_text(encoding="utf-8")
    assert "s3cr3t" not in text
    assert REDACTED in text


def test_configure_logging_skips_file_when_dir_missing(tmp_path: Path) -> None:
    log_file = configure_logging(LoggingConfig(), tmp_path / "does-not-exist")
    assert log_file is None


def test_configure_logging_is_idempotent(tmp_path: Path) -> None:
    logs = tmp_path / "logs"
    logs.mkdir()
    config = LoggingConfig(level="INFO", json_format=True)
    configure_logging(config, logs)
    first = _managed_handler_count()
    configure_logging(config, logs)
    assert _managed_handler_count() == first
