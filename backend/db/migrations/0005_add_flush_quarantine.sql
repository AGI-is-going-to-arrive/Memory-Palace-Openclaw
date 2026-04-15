CREATE TABLE IF NOT EXISTS flush_quarantine (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    source TEXT NOT NULL,
    summary TEXT NOT NULL,
    gist_text TEXT,
    trace_text TEXT,
    guard_action TEXT NOT NULL,
    guard_method TEXT,
    guard_reason TEXT,
    guard_target_uri TEXT,
    content_hash TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    expires_at TEXT NOT NULL,
    replayed_at TEXT,
    status TEXT NOT NULL DEFAULT 'pending'
);
CREATE INDEX IF NOT EXISTS idx_flush_quarantine_session ON flush_quarantine(session_id);
CREATE INDEX IF NOT EXISTS idx_flush_quarantine_status ON flush_quarantine(status);
CREATE INDEX IF NOT EXISTS idx_flush_quarantine_expires ON flush_quarantine(expires_at);
