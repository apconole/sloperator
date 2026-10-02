-- sloperator initial schema (SQLite dialect, designed to port to PostgreSQL).
-- Conventions: TEXT ids are canonical strings; timestamps are ISO-8601 UTC
-- text; JSON documents are stored as TEXT; booleans are INTEGER 0/1.

CREATE TABLE repositories (
    id TEXT PRIMARY KEY,
    path TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'working_tree',
    default_branch TEXT NOT NULL DEFAULT 'main'
);

CREATE TABLE projects (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT,
    repository_id TEXT REFERENCES repositories(id),
    created_at TEXT NOT NULL
);

CREATE TABLE sources (
    id TEXT PRIMARY KEY,
    type TEXT NOT NULL,
    project_id TEXT NOT NULL REFERENCES projects(id),
    cursor TEXT,
    last_success_at TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE actors (
    id INTEGER PRIMARY KEY,
    key TEXT NOT NULL UNIQUE,
    name TEXT,
    email TEXT,
    is_principal INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE events (
    id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL REFERENCES sources(id),
    source_type TEXT NOT NULL,
    external_id TEXT NOT NULL,
    project_id TEXT NOT NULL REFERENCES projects(id),
    event_type TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    actor_id INTEGER REFERENCES actors(id),
    author_key TEXT,
    subject TEXT,
    body TEXT,
    reply_to TEXT,
    content_hash TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    raw_stored INTEGER NOT NULL DEFAULT 0,
    ingested_at TEXT NOT NULL,
    UNIQUE (source_id, external_id)
);

CREATE INDEX idx_events_project_time ON events(project_id, timestamp);
CREATE INDEX idx_events_content_hash ON events(content_hash);
CREATE INDEX idx_events_reply_to ON events(reply_to);

CREATE TABLE threads (
    id INTEGER PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id),
    root_event_id TEXT REFERENCES events(id),
    subject TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    started_at TEXT NOT NULL,
    last_activity_at TEXT NOT NULL
);

CREATE TABLE thread_events (
    thread_id INTEGER NOT NULL REFERENCES threads(id),
    event_id TEXT NOT NULL REFERENCES events(id),
    relationship TEXT NOT NULL,
    confidence REAL NOT NULL DEFAULT 1.0,
    basis_json TEXT NOT NULL DEFAULT '[]',
    PRIMARY KEY (thread_id, event_id)
);

CREATE TABLE patch_series (
    id INTEGER PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id),
    thread_id INTEGER REFERENCES threads(id),
    title TEXT NOT NULL,
    latest_version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE patches (
    id INTEGER PRIMARY KEY,
    series_id INTEGER NOT NULL REFERENCES patch_series(id),
    event_id TEXT REFERENCES events(id),
    version INTEGER NOT NULL,
    position INTEGER NOT NULL DEFAULT 0,
    total INTEGER,
    subject TEXT NOT NULL,
    content_hash TEXT,
    UNIQUE (series_id, version, position)
);

CREATE TABLE reviews (
    id INTEGER PRIMARY KEY,
    event_id TEXT REFERENCES events(id),
    project_id TEXT NOT NULL REFERENCES projects(id),
    thread_id INTEGER REFERENCES threads(id),
    reviewer_actor_id INTEGER REFERENCES actors(id),
    state TEXT NOT NULL,
    patch_id INTEGER REFERENCES patches(id),
    created_at TEXT NOT NULL
);

CREATE TABLE review_requests (
    id INTEGER PRIMARY KEY,
    event_id TEXT REFERENCES events(id),
    project_id TEXT NOT NULL REFERENCES projects(id),
    thread_id INTEGER REFERENCES threads(id),
    requested_actor_id INTEGER REFERENCES actors(id),
    requested_principal_id TEXT,
    patch_id INTEGER REFERENCES patches(id),
    created_at TEXT NOT NULL
);

CREATE TABLE decisions (
    id INTEGER PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id),
    thread_id INTEGER REFERENCES threads(id),
    statement TEXT NOT NULL,
    decided_by_actor_id INTEGER REFERENCES actors(id),
    source_event_ids_json TEXT NOT NULL DEFAULT '[]',
    confidence REAL,
    created_at TEXT NOT NULL
);

CREATE TABLE questions (
    id INTEGER PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id),
    thread_id INTEGER REFERENCES threads(id),
    question TEXT NOT NULL,
    asked_by_actor_id INTEGER REFERENCES actors(id),
    status TEXT NOT NULL DEFAULT 'open',
    source_event_ids_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE action_items (
    id INTEGER PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id),
    thread_id INTEGER REFERENCES threads(id),
    description TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    confidence REAL NOT NULL DEFAULT 0.5,
    explicitness TEXT NOT NULL,
    owner_actor_id INTEGER REFERENCES actors(id),
    source_event_ids_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE commits (
    sha TEXT NOT NULL,
    repository_id TEXT NOT NULL REFERENCES repositories(id),
    author_key TEXT,
    summary TEXT,
    authored_at TEXT,
    PRIMARY KEY (sha, repository_id)
);

CREATE TABLE ci_results (
    id INTEGER PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id),
    context TEXT NOT NULL,
    state TEXT NOT NULL,
    target_url TEXT,
    event_id TEXT REFERENCES events(id),
    created_at TEXT NOT NULL
);

CREATE TABLE tasks (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,
    project_id TEXT,
    dedupe_key TEXT UNIQUE,
    status TEXT NOT NULL DEFAULT 'pending',
    priority INTEGER NOT NULL DEFAULT 5,
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER,
    run_after TEXT NOT NULL,
    locked_by TEXT,
    locked_at TEXT,
    last_error TEXT,
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX idx_tasks_status_run_after ON tasks(status, run_after);

CREATE TABLE agent_runs (
    id INTEGER PRIMARY KEY,
    task_id INTEGER REFERENCES tasks(id),
    project_id TEXT,
    role TEXT NOT NULL,
    backend TEXT NOT NULL,
    model TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'running',
    input_json TEXT,
    raw_response TEXT,
    parsed_output_json TEXT,
    confidence REAL,
    error TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT
);

CREATE TABLE tool_calls (
    id INTEGER PRIMARY KEY,
    agent_run_id INTEGER REFERENCES agent_runs(id),
    task_id INTEGER REFERENCES tasks(id),
    tool TEXT NOT NULL,
    access TEXT NOT NULL,
    arguments_json TEXT NOT NULL DEFAULT '{}',
    result_summary TEXT,
    denied INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE artifacts (
    id INTEGER PRIMARY KEY,
    task_id INTEGER REFERENCES tasks(id),
    agent_run_id INTEGER REFERENCES agent_runs(id),
    project_id TEXT,
    thread_id INTEGER REFERENCES threads(id),
    artifact_type TEXT NOT NULL,
    title TEXT,
    status TEXT NOT NULL DEFAULT 'ready_for_review',
    factual INTEGER,
    confidence REAL,
    workspace_path TEXT,
    backend TEXT,
    model TEXT,
    source_event_ids_json TEXT NOT NULL DEFAULT '[]',
    content_path TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE notifications (
    id INTEGER PRIMARY KEY,
    project_id TEXT,
    urgency TEXT NOT NULL,
    title TEXT NOT NULL,
    body TEXT,
    artifact_id INTEGER REFERENCES artifacts(id),
    delivered_at TEXT,
    created_at TEXT NOT NULL
);
