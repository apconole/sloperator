"""sloperator command line entry point."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import click

from sloperator import __version__
from sloperator.analysis.actions import ActionItemWriter
from sloperator.config.loading import load_config
from sloperator.config.models import (
    AnySourceConfig,
    Config,
    GithubSourceConfig,
    MboxSourceConfig,
    NntpSourceConfig,
    PipermailSourceConfig,
    Urgency,
)
from sloperator.config.secrets import SecretStore
from sloperator.database.dao import ProjectDao
from sloperator.database.db import Database
from sloperator.database.migrations import current_version
from sloperator.ingest.pipeline import IngestPipeline
from sloperator.log import configure_logging
from sloperator.sources import SourceAdapter
from sloperator.sources.github import GithubAdapter
from sloperator.sources.mbox import MboxAdapter
from sloperator.sources.nntp import NntpAdapter
from sloperator.sources.pipermail import PipermailAdapter
from sloperator.threads.reconstruct import ThreadReconstructor

DEFAULT_CONFIG = Path.home() / ".config" / "sloperator" / "config.yaml"


@dataclass
class AppContext:
    config: Config
    db: Database
    secrets: SecretStore
    config_path: Path

    def close(self) -> None:
        self.db.close()

    def adapters(self, source_ids: tuple[str, ...] = ()) -> list[SourceAdapter]:
        adapters: list[SourceAdapter] = []
        for source in self.config.sources:
            if source_ids and source.id not in source_ids:
                continue
            adapters.append(build_adapter(source, self.secrets))
        return adapters


def build_adapter(source: AnySourceConfig, secrets: SecretStore) -> SourceAdapter:
    if isinstance(source, MboxSourceConfig):
        return MboxAdapter(source)
    if isinstance(source, NntpSourceConfig):
        return NntpAdapter(source, password=secrets.get_optional(source.password_env))
    if isinstance(source, PipermailSourceConfig):
        return PipermailAdapter(source)
    if isinstance(source, GithubSourceConfig):
        return GithubAdapter(source, token=secrets.get_optional(source.api_token_env))
    raise click.ClickException(f"unsupported source type for {source.id!r}")


@click.group(name="sloperator")
@click.version_option(__version__, prog_name="sloperator")
@click.option(
    "--config",
    "config_path",
    type=click.Path(path_type=Path, dir_okay=False),
    default=DEFAULT_CONFIG,
    envvar="SLOPERATOR_CONFIG",
    show_default=True,
    help="Path to the YAML configuration file.",
)
@click.pass_context
def main(ctx: click.Context, config_path: Path) -> None:
    """Maintainer event monitor and agentic review assistant."""
    try:
        config = load_config(config_path)
    except Exception as exc:
        raise click.ClickException(f"configuration error: {exc}") from exc
    log_path = configure_logging(config.logging, config.paths.logs)
    db_path = config.paths.database / config.database.filename
    db = Database(db_path)
    db.migrate()
    with db.transaction() as conn:
        for project_id, project_cfg in config.projects.items():
            ProjectDao.upsert(conn, project_id=project_id, name=project_cfg.name)
    ctx.obj = AppContext(config=config, db=db, secrets=SecretStore(), config_path=config_path)
    ctx.call_on_close(db.close)
    if log_path is not None:
        click.echo(f"logfile: {log_path}", err=True)


@main.command()
@click.pass_obj
def doctor(ctx: AppContext) -> None:
    """Validate configuration, database, paths, and source health."""
    failures = 0

    def report(name: str, ok: bool, detail: str = "") -> None:
        nonlocal failures
        marker = "OK  " if ok else "FAIL"
        if not ok:
            failures += 1
        click.echo(f"[{marker}] {name}{f': {detail}' if detail else ''}")

    report("config", True, str(ctx.config_path))
    db_version = current_version(ctx.db.connect())
    report("database", db_version > 0, f"{ctx.db.path} schema v{db_version}")
    for name in ("database", "state", "cache", "artifacts", "workspaces", "logs"):
        path: Path = getattr(ctx.config.paths, name)
        try:
            path.mkdir(parents=True, exist_ok=True)
            probe = path / ".sloperator-probe"
            probe.write_text("x")
            probe.unlink()
            report(f"path:{name}", True, str(path))
        except OSError as exc:
            report(f"path:{name}", False, str(exc))

    for source in ctx.config.sources:
        adapter = build_adapter(source, ctx.secrets)
        try:
            health = adapter.health_check()
            ok = health.status.value == "ok"
            report(f"source:{source.id}", ok, f"{health.status} {health.detail or ''}".strip())
        except Exception as exc:
            report(f"source:{source.id}", False, f"{type(exc).__name__}: {exc}")

    if failures:
        raise click.exceptions.Exit(1)


@main.command()
@click.option("--source", "source_ids", multiple=True, help="Only ingest these source ids.")
@click.pass_obj
def ingest(ctx: AppContext, source_ids: tuple[str, ...]) -> None:
    """Poll configured sources and persist new events."""
    pipeline = IngestPipeline(ctx.db)
    adapters = ctx.adapters(source_ids)
    if not adapters:
        raise click.ClickException("no sources matched")
    for adapter in adapters:
        stats = pipeline.ingest_source(
            adapter, project_id=_source_project(ctx.config, adapter.source_id)
        )
        click.echo(
            f"{stats.source_id}: received={stats.received} inserted={stats.inserted} "
            f"dup_ids={stats.duplicate_ids} dup_content={stats.content_duplicates} "
            f"cursor={stats.cursor}"
        )


def _source_project(config: Config, source_id: str) -> str:
    for source in config.sources:
        if source.id == source_id:
            return source.project
    raise click.ClickException(f"unknown source {source_id!r}")


@main.command()
@click.option("--project", "project_ids", multiple=True, help="Only process these projects.")
@click.pass_obj
def process(ctx: AppContext, project_ids: tuple[str, ...]) -> None:
    """Rebuild threads and extract action items for processed events."""
    projects = list(ctx.config.projects) or []
    targets = [p for p in projects if not project_ids or p in project_ids]
    if not targets:
        raise click.ClickException("no projects matched")
    reconstructor = ThreadReconstructor(ctx.db)
    writer = ActionItemWriter(ctx.db)
    for project_id in targets:
        stats = reconstructor.rebuild_project(project_id)
        click.echo(
            f"{project_id}: events={stats.events} threads={stats.threads} "
            f"reply_to={stats.in_reply_to} refs={stats.references} "
            f"subject={stats.subject_links}"
        )
        rows = (
            ctx.db.connect()
            .execute("SELECT id FROM threads WHERE project_id = ?", (project_id,))
            .fetchall()
        )
        total_items = 0
        for row in rows:
            result = writer.process_thread(project_id, int(row["id"]))
            total_items += result.inserted
        click.echo(f"{project_id}: action_items_added={total_items}")


@main.command()
@click.pass_obj
def status(ctx: AppContext) -> None:
    """Show counts and source cursors."""
    conn = ctx.db.connect()

    def count(table: str, where: str = "") -> int:
        return int(conn.execute(f"SELECT COUNT(*) AS n FROM {table} {where}").fetchone()["n"])

    click.echo(f"events: {count('events')}")
    click.echo(f"threads: {count('threads')}")
    open_items = count("action_items", "WHERE status = 'open'")
    click.echo(f"action_items open: {open_items}")
    click.echo(f"artifacts: {count('artifacts')}")
    for state, n in sorted(
        (str(r["status"]), int(r["n"]))
        for r in conn.execute("SELECT status, COUNT(*) AS n FROM tasks GROUP BY status")
    ):
        click.echo(f"tasks {state}: {n}")
    for row in conn.execute("SELECT id, type, cursor, last_success_at FROM sources ORDER BY id"):
        click.echo(
            f"source {row['id']} ({row['type']}): cursor={row['cursor']} "
            f"last_success={row['last_success_at']}"
        )


@main.group(name="list")
def list_group() -> None:
    """List threads, events, or artifacts."""


@list_group.command(name="threads")
@click.option("--project", default=None)
@click.option("--limit", default=20, show_default=True)
@click.pass_obj
def list_threads(ctx: AppContext, project: str | None, limit: int) -> None:
    rows = _fetch(
        ctx,
        "SELECT t.id, t.project_id, t.subject, t.last_activity_at, "
        "(SELECT COUNT(*) FROM thread_events te WHERE te.thread_id = t.id) AS message_count "
        "FROM threads t "
        + ("WHERE t.project_id = ? " if project else "")
        + "ORDER BY t.last_activity_at DESC LIMIT ?",
        ((project,) if project else ()) + (limit,),
    )
    for row in rows:
        click.echo(
            f"#{row['id']} [{row['project_id']}] {row['message_count']:>3} msgs  "
            f"{row['last_activity_at']}  {row['subject'] or '(no subject)'}"
        )


@list_group.command(name="events")
@click.option("--project", default=None)
@click.option("--limit", default=20, show_default=True)
@click.pass_obj
def list_events(ctx: AppContext, project: str | None, limit: int) -> None:
    rows = _fetch(
        ctx,
        "SELECT id, source_id, timestamp, subject FROM events "
        + ("WHERE project_id = ? " if project else "")
        + "ORDER BY timestamp DESC LIMIT ?",
        ((project,) if project else ()) + (limit,),
    )
    for row in rows:
        click.echo(f"{row['id']}  {row['timestamp']}  [{row['source_id']}]  {row['subject'] or ''}")


@list_group.command(name="artifacts")
@click.option("--project", default=None)
@click.option("--limit", default=20, show_default=True)
@click.pass_obj
def list_artifacts(ctx: AppContext, project: str | None, limit: int) -> None:
    rows = _fetch(
        ctx,
        "SELECT id, project_id, artifact_type, title, created_at FROM artifacts "
        + ("WHERE project_id = ? " if project else "")
        + "ORDER BY created_at DESC, id DESC LIMIT ?",
        ((project,) if project else ()) + (limit,),
    )
    for row in rows:
        click.echo(
            f"#{row['id']} [{row['project_id']}] {row['artifact_type']}  "
            f"{row['created_at']}  {row['title'] or ''}"
        )


@main.group(name="show")
def show_group() -> None:
    """Show one thread, event, or artifact."""


@show_group.command(name="event")
@click.argument("event_id")
@click.pass_obj
def show_event(ctx: AppContext, event_id: str) -> None:
    row = ctx.db.connect().execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
    if row is None:
        raise click.ClickException(f"no such event: {event_id}")
    for key in ("id", "source_id", "external_id", "event_type", "timestamp", "subject"):
        click.echo(f"{key}: {row[key]}")
    body = str(row["body"] or "")
    click.echo("---")
    click.echo(body)


@show_group.command(name="thread")
@click.argument("thread_id", type=int)
@click.pass_obj
def show_thread(ctx: AppContext, thread_id: int) -> None:
    conn = ctx.db.connect()
    thread = conn.execute("SELECT * FROM threads WHERE id = ?", (thread_id,)).fetchone()
    if thread is None:
        raise click.ClickException(f"no such thread: {thread_id}")
    click.echo(f"thread #{thread['id']} [{thread['project_id']}] {thread['subject'] or ''}")
    for member in conn.execute(
        """SELECT te.event_id, te.relationship, te.confidence, e.timestamp, e.subject
           FROM thread_events te JOIN events e ON e.id = te.event_id
           WHERE te.thread_id = ? ORDER BY e.timestamp""",
        (thread_id,),
    ):
        click.echo(
            f"  {member['timestamp']}  {member['relationship']}({member['confidence']:.2f})  "
            f"{member['event_id']}  {member['subject'] or ''}"
        )


@show_group.command(name="artifact")
@click.argument("artifact_id", type=int)
@click.option("--content/--metadata", default=True, show_default=True)
@click.pass_obj
def show_artifact(ctx: AppContext, artifact_id: int, content: bool) -> None:
    from sloperator.artifacts.store import ArtifactStore

    store = ArtifactStore(ctx.db, ctx.config.paths.artifacts)
    record = store.get(artifact_id)
    if record is None:
        raise click.ClickException(f"no such artifact: {artifact_id}")
    click.echo(f"#{record.id} {record.artifact_type} [{record.status}] {record.title}")
    if content:
        click.echo("---")
        click.echo(record.read())


@main.command()
@click.option("--project", "project_ids", multiple=True, help="Only brief these projects.")
@click.pass_obj
def brief(ctx: AppContext, project_ids: tuple[str, ...]) -> None:
    """Generate the daily briefing artifact and notifications."""
    from sloperator.artifacts.store import ArtifactStore
    from sloperator.notify.briefing import DailyBriefing

    store = ArtifactStore(ctx.db, ctx.config.paths.artifacts)
    briefing = DailyBriefing(ctx.db, store, ctx.config)
    targets = [p for p in ctx.config.projects if not project_ids or p in project_ids]
    if not targets:
        raise click.ClickException("no projects matched")
    for project_id in targets:
        result = briefing.generate(project_id)
        click.echo(
            f"{project_id}: events={result.new_events} threads={result.active_threads} "
            f"open_items={result.open_action_items} requests={result.outstanding_requests} "
            f"artifact=#{result.artifact.id} notifications={result.notifications}"
        )


@main.command()
@click.option(
    "--min-urgency",
    type=click.Choice([u.value for u in Urgency]),
    default=None,
)
@click.option("--undelivered", is_flag=True, help="Only notifications not yet delivered.")
@click.pass_obj
def notifications(ctx: AppContext, min_urgency: str | None, undelivered: bool) -> None:
    """List principal notifications."""
    from sloperator.artifacts.store import ArtifactStore
    from sloperator.notify.briefing import DailyBriefing

    store = ArtifactStore(ctx.db, ctx.config.paths.artifacts)
    briefing = DailyBriefing(ctx.db, store, ctx.config)
    rows = briefing.list_notifications(
        min_urgency=Urgency(min_urgency) if min_urgency else None,
        undelivered_only=undelivered,
    )
    for row in rows:
        click.echo(f"#{row['id']} [{row['urgency']}] {row['created_at']}  {row['title']}")


def _fetch(ctx: AppContext, sql: str, params: tuple[Any, ...]) -> list[Any]:
    return list(ctx.db.connect().execute(sql, params).fetchall())


if __name__ == "__main__":
    main()
