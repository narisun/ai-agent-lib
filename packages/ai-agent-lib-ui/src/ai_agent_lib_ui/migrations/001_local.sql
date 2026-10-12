PRAGMA journal_mode = WAL;
CREATE TABLE IF NOT EXISTS ui_schema (version INTEGER PRIMARY KEY);
INSERT OR IGNORE INTO ui_schema VALUES (1);
CREATE TABLE IF NOT EXISTS threads (
    id TEXT PRIMARY KEY, tenant TEXT NOT NULL, subject TEXT NOT NULL, agent TEXT NOT NULL,
    title TEXT NOT NULL, updated TEXT NOT NULL, archived INTEGER NOT NULL DEFAULT 0,
    record TEXT NOT NULL, binding TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS threads_owner ON threads(tenant, subject, updated DESC, id);
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY, thread TEXT NOT NULL REFERENCES threads(id),
    key TEXT NOT NULL, question TEXT NOT NULL, state TEXT NOT NULL, record TEXT NOT NULL,
    UNIQUE(thread, key)
);
CREATE UNIQUE INDEX IF NOT EXISTS one_active_turn ON runs(thread)
    WHERE state IN ('accepted', 'running', 'unknown');
CREATE TABLE IF NOT EXISTS sessions (
    digest TEXT PRIMARY KEY, identity TEXT NOT NULL, expires REAL NOT NULL
);
