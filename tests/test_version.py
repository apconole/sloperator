"""Tests for package metadata and CLI entry point."""

from click.testing import CliRunner

from sloperator import __version__
from sloperator.cli.main import main


def test_version() -> None:
    assert __version__ == "0.1.0"


def test_cli_help() -> None:
    result = CliRunner().invoke(main, ["--help"])
    assert result.exit_code == 0
    assert "sloperator" in result.output


def test_cli_version() -> None:
    result = CliRunner().invoke(main, ["--version"])
    assert result.exit_code == 0
    assert __version__ in result.output
