"""sloperator command line entry point."""

from __future__ import annotations

import click

from sloperator import __version__


@click.group(name="sloperator")
@click.version_option(__version__, prog_name="sloperator")
def main() -> None:
    """Maintainer event monitor and agentic review assistant."""


if __name__ == "__main__":
    main()
