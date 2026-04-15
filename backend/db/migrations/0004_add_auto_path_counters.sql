CREATE TABLE IF NOT EXISTS auto_path_counters (
    domain TEXT NOT NULL,
    parent_path TEXT NOT NULL DEFAULT '',
    next_id INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (domain, parent_path)
);
