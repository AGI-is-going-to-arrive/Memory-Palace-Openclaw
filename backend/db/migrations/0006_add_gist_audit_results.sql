CREATE TABLE IF NOT EXISTS gist_audit_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    gist_id INTEGER NOT NULL,
    memory_id INTEGER NOT NULL,
    gist_method TEXT NOT NULL,
    coverage_score REAL,
    factual_preservation_score REAL,
    actionability_score REAL,
    missing_anchors TEXT,
    hallucination_flags TEXT,
    judge_model TEXT,
    judge_raw_response TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    source_content_hash TEXT
);
CREATE INDEX IF NOT EXISTS idx_gist_audit_gist_id ON gist_audit_results(gist_id);
CREATE INDEX IF NOT EXISTS idx_gist_audit_memory_id ON gist_audit_results(memory_id);
CREATE INDEX IF NOT EXISTS idx_gist_audit_created ON gist_audit_results(created_at);
