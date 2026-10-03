"""End-to-end CLI tests against a temporary config and real mbox source."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from sloperator.cli.main import main

FIXTURE = Path(__file__).parent / "fixtures" / "valid_config.yaml"

MBOX = (
    b"From bounce@example.org Mon Oct  1 12:00:00 2026\n"
    b"Message-ID: <root@mbox>\nFrom: Jane <jane@example.org>\n"
    b"Subject: [PATCH] fix leak\nDate: Mon, 01 Oct 2026 12:00:00 +0000\n\n"
    b"Please review this change before the release.\n"
    b"From bounce@example.org Mon Oct  1 13:00:00 2026\n"
    b"Message-ID: <reply@mbox>\nFrom: Bob <bob@example.org>\n"
    b"Subject: Re: [PATCH] fix leak\nDate: Mon, 01 Oct 2026 13:00:00 +0000\n"
    b"In-Reply-To: <root@mbox>\n\n"
    b"I will take a look and test it today.\n"
)


@pytest.fixture()
def config_path(tmp_path: Path) -> Path:
    base = yaml.safe_load(FIXTURE.read_text())
    for key in ("database", "state", "cache", "artifacts", "workspaces", "logs"):
        base["paths"][key] = str(tmp_path / "state" / key)
    mbox_file = tmp_path / "list.mbox"
    mbox_file.write_bytes(MBOX)
    base["sources"] = [
        {"id": "mbox1", "type": "mbox", "project": "example", "path": str(mbox_file)}
    ]
    base["projects"]["example"]["sources"] = ["mbox1"]
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(base))
    return path


def invoke(args: list[str], config_path: Path):  # type: ignore[no-untyped-def]
    result = CliRunner().invoke(main, ["--config", str(config_path), *args])
    return result


def test_doctor_ok(config_path: Path) -> None:
    result = invoke(["doctor"], config_path)
    assert result.exit_code == 0, result.output
    assert "[OK  ] config" in result.output
    assert "[OK  ] database" in result.output
    assert "[OK  ] source:mbox1" in result.output


def test_ingest_process_status_and_views(config_path: Path) -> None:
    result = invoke(["ingest"], config_path)
    assert result.exit_code == 0, result.output
    assert "inserted=2" in result.output

    result = invoke(["process"], config_path)
    assert result.exit_code == 0, result.output
    assert "threads=1" in result.output
    assert "action_items_added=2" in result.output

    result = invoke(["status"], config_path)
    assert result.exit_code == 0, result.output
    assert "events: 2" in result.output
    assert "threads: 1" in result.output
    assert "action_items open: 2" in result.output

    result = invoke(["list", "threads"], config_path)
    assert result.exit_code == 0, result.output
    assert "fix leak" in result.output

    result = invoke(["show", "event", "mbox1:root@mbox"], config_path)
    assert result.exit_code == 0, result.output
    assert "Please review this change" in result.output

    result = invoke(["show", "thread", "1"], config_path)
    assert result.exit_code == 0, result.output
    assert "reply_of" in result.output


def test_ingest_idempotent_rerun(config_path: Path) -> None:
    assert invoke(["ingest"], config_path).exit_code == 0
    result = invoke(["ingest"], config_path)
    assert result.exit_code == 0, result.output
    assert "received=0 inserted=0" in result.output


def test_bad_config_exits_with_error(tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text("application: {}\n")
    result = CliRunner().invoke(main, ["--config", str(bad), "status"])
    assert result.exit_code != 0
    assert "configuration error" in result.output
