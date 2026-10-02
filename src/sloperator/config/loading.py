"""Configuration loading and validation entry points."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from sloperator.config.errors import ConfigError
from sloperator.config.models import Config


def load_config(path: Path) -> Config:
    """Load, parse, and validate configuration from a YAML file.

    Raises ConfigError with a precise message on any problem. The
    application must not start when configuration is invalid.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read configuration file {path}: {exc}") from exc
    return parse_config(text, source=str(path))


def parse_config(text: str, source: str = "<string>") -> Config:
    """Parse and validate configuration from a YAML string."""
    data: Any
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in {source}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"top-level YAML in {source} must be a mapping")
    try:
        return Config.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(_format_validation_error(exc, source)) from exc


def _format_validation_error(exc: ValidationError, source: str) -> str:
    lines = [f"invalid configuration in {source}:"]
    for err in exc.errors():
        location = ".".join(str(part) for part in err["loc"]) or "config"
        message = err["msg"].removeprefix("Value error, ").strip()
        lines.append(f"  {location}: {message}")
    return "\n".join(lines)
