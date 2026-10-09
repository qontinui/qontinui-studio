"""The studio's one SQLite file: scheduler state and the asset library.

Kept in one file so a batch's jobs, the assets they produced, the gate results
that judged them and the publications they became can be read in one join.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS batches (
    id TEXT PRIMARY KEY,
    brief_id TEXT NOT NULL,
    brief_path TEXT NOT NULL,
    brief_sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN
        ('generating', 'generated', 'published', 'refused', 'unpublished', 'failed'))
);

CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL REFERENCES batches(id),
    variant_index INTEGER NOT NULL,
    provider TEXT,
    status TEXT NOT NULL CHECK (status IN ('queued', 'submitted', 'done', 'failed')),
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT NOT NULL,
    external_id TEXT,
    handle_json TEXT,
    tried_providers TEXT NOT NULL DEFAULT '[]',
    last_error TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS quota_usage (
    provider TEXT NOT NULL,
    day TEXT NOT NULL,
    used INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (provider, day)
);

-- D5 provenance. ai_generated is pinned true by a CHECK: everything a
-- generator adapter produced is AI-generated, and no code path may say otherwise.
CREATE TABLE IF NOT EXISTS assets (
    uid TEXT PRIMARY KEY,
    asset_id TEXT NOT NULL,
    brief_id TEXT NOT NULL,
    batch_id TEXT NOT NULL REFERENCES batches(id),
    job_id TEXT NOT NULL REFERENCES jobs(id),
    provider TEXT NOT NULL,
    provider_name TEXT NOT NULL,
    model TEXT NOT NULL,
    prompt TEXT NOT NULL,
    created_at TEXT NOT NULL,
    raw_path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    width INTEGER NOT NULL,
    height INTEGER NOT NULL,
    ai_generated INTEGER NOT NULL DEFAULT 1 CHECK (ai_generated = 1),
    licence_commercial_use TEXT NOT NULL CHECK (licence_commercial_use IN ('yes', 'no', 'unknown')),
    licence_source TEXT,
    licence_tos_reviewed TEXT,
    automation_allowed TEXT NOT NULL,
    watermark TEXT NOT NULL,
    watermark_visible INTEGER
);

CREATE TABLE IF NOT EXISTS variants (
    id TEXT PRIMARY KEY,
    asset_uid TEXT NOT NULL REFERENCES assets(uid),
    site TEXT,
    slot TEXT,
    campaign TEXT,
    format TEXT NOT NULL,
    path TEXT NOT NULL,
    width INTEGER NOT NULL,
    height INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS gate_results (
    batch_id TEXT NOT NULL REFERENCES batches(id),
    gate TEXT NOT NULL,
    subject TEXT NOT NULL,
    outcome TEXT NOT NULL CHECK (outcome IN ('pass', 'fail', 'not_configured')),
    detail TEXT NOT NULL,
    evaluated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS publications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id TEXT NOT NULL REFERENCES batches(id),
    variant_id TEXT NOT NULL REFERENCES variants(id),
    site TEXT NOT NULL,
    slot TEXT NOT NULL,
    published_path TEXT NOT NULL,
    label TEXT NOT NULL,
    published_at TEXT NOT NULL,
    unpublished_at TEXT
);
"""


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    return conn
