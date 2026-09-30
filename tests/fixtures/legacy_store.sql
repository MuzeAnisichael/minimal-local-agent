-- Frozen schema from v0.2.0 (before PRAGMA user_version was introduced).
CREATE TABLE sessions (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    history_json TEXT
);
CREATE TABLE runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    prompt TEXT NOT NULL,
    response TEXT,
    success INTEGER NOT NULL,
    error TEXT,
    usage_json TEXT
);
CREATE TABLE tool_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    status TEXT NOT NULL,
    details_json TEXT NOT NULL
);
INSERT INTO sessions VALUES ('legacy', 'old', 'old', '[]');
INSERT INTO runs VALUES (42, 'legacy', 'old', 'hello', 'world', 1, NULL, '{}');
INSERT INTO tool_events VALUES (7, 'legacy', 'old', 'read_file', 'ok', '{}');
