-- Frozen schema-2 change tables, retained unchanged since v0.3.
CREATE TABLE change_sets (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    undone_at TEXT
);
CREATE TABLE file_changes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    change_set_id TEXT NOT NULL REFERENCES change_sets(id) ON DELETE CASCADE,
    path TEXT NOT NULL,
    existed INTEGER NOT NULL,
    before_text TEXT,
    before_sha256 TEXT,
    after_sha256 TEXT NOT NULL,
    after_bytes INTEGER NOT NULL
);
INSERT INTO change_sets VALUES ('change-old', 'legacy', 'old', 'edit', 'applied', NULL);
INSERT INTO file_changes VALUES (9, 'change-old', 'safe.txt', 1, 'before', 'hash1', 'hash2', 5);
