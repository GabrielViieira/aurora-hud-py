-- 001_init.sql — esquema inicial da Fase 1
-- Camadas de memória: sessão (messages/sessions), episódica e semântica (memories).

CREATE TABLE IF NOT EXISTS sessions (
    id          TEXT PRIMARY KEY,
    started_at  TEXT NOT NULL,
    ended_at    TEXT,
    title       TEXT NOT NULL DEFAULT '',
    summary     TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    role        TEXT NOT NULL,
    content     TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    tokens_est  INTEGER NOT NULL DEFAULT 0,
    meta_json   TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, id);

CREATE TABLE IF NOT EXISTS tasks (
    id          TEXT PRIMARY KEY,
    session_id  TEXT,
    goal        TEXT NOT NULL,
    status      TEXT NOT NULL,
    plan_json   TEXT NOT NULL DEFAULT '[]',
    checkpoint_json TEXT NOT NULL DEFAULT '{}',
    error       TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status, updated_at);

CREATE TABLE IF NOT EXISTS memories (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    layer       TEXT NOT NULL,
    kind        TEXT NOT NULL DEFAULT 'fact',
    content     TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    tags        TEXT NOT NULL DEFAULT '',
    source      TEXT NOT NULL DEFAULT '',
    importance  REAL NOT NULL DEFAULT 0.5,
    session_id  TEXT,
    created_at  TEXT NOT NULL,
    last_used_at TEXT,
    use_count   INTEGER NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_memories_hash ON memories(layer, content_hash);
CREATE INDEX IF NOT EXISTS idx_memories_layer ON memories(layer, created_at);

CREATE TABLE IF NOT EXISTS tool_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id     TEXT,
    tool        TEXT NOT NULL,
    args_json   TEXT NOT NULL DEFAULT '{}',
    risk        TEXT NOT NULL DEFAULT 'L0',
    status      TEXT NOT NULL,
    summary     TEXT NOT NULL DEFAULT '',
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    duration_ms INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_tool_runs_task ON tool_runs(task_id, id);

CREATE TABLE IF NOT EXISTS metrics (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    kind        TEXT NOT NULL,
    model       TEXT NOT NULL DEFAULT '',
    value       REAL NOT NULL DEFAULT 0,
    meta_json   TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_metrics_ts ON metrics(ts);
