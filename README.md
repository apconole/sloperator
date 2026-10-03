# sloperator

Maintainer event monitor and agentic review assistant.

sloperator continuously monitors software project communication and development
activity (NNTP mailing lists, Pipermail-style HTML archives, mbox files, and
GitHub repositories), reconstructs conversations and patch series, tracks
outstanding action items, and uses configurable LLM roles to prepare summaries,
reviews, investigations, and proposed work for a human maintainer.

The operating model is:

    observe -> understand -> investigate -> prepare -> report -> human decides

sloperator is read-only by default: it never sends mail, posts to GitHub, or
modifies an authoritative repository. Every externally visible action requires
explicit human authorization.

See [IMPLEMENTATION.md](IMPLEMENTATION.md) for the full design.

## Requirements

- Python 3.12 or newer
- Linux, running as a dedicated unprivileged user
- SQLite 3

## Usage

Point sloperator at a YAML configuration file (see `tests/fixtures/valid_config.yaml`
for a complete example) and run:

    sloperator doctor        # validate config, database, paths, source health
    sloperator ingest        # poll sources and persist new events
    sloperator process       # rebuild threads, extract action items
    sloperator status        # counts, task states, source cursors
    sloperator list threads  # also: list events, list artifacts
    sloperator show thread 1 # also: show event <id>, show artifact <id>
    sloperator brief         # daily briefing artifact + notifications
    sloperator notifications # list principal notifications by urgency

The config path defaults to `~/.config/sloperator/config.yaml` and can be
overridden with `--config` or `SLOPERATOR_CONFIG`. Secrets are never stored
in the configuration file; they reference environment variable names and
are resolved on demand.

## Development

    python3 -m venv .venv
    . .venv/bin/activate
    pip install -e ".[dev]"

Run the checks used by CI:

    ruff check .
    ruff format --check .
    mypy src/
    pytest

## License

GPL-2.0-only, see [LICENSE](LICENSE).
