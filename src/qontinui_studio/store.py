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
    -- The (site, slot) targets the brief named when the batch was created; a
    -- batch publishes only when every one of them has a variant.
    targets_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN
        ('generating', 'generated', 'publishing', 'published', 'refused', 'unpublished',
         'failed')),
    -- Why the batch is failed/refused, when it is.
    reason TEXT,
    -- Lease: the one process currently driving this batch (drive_batch). A
    -- second driver's claim fails while the lease is live.
    driver TEXT,
    driver_until TEXT,
    -- Set once the operator notice for the publication was delivered;
    -- recovery re-sends for a published batch that has none.
    notified_at TEXT,
    -- Created by `brief run --no-publish`: resume never publishes it.
    no_publish INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL REFERENCES batches(id),
    variant_index INTEGER NOT NULL,
    provider TEXT,
    -- downloaded: files are on disk but not yet in the library; ingested: the
    -- library rows and every target's variants exist. A crash between the two
    -- leaves 'downloaded', which the next run re-ingests.
    status TEXT NOT NULL CHECK (status IN
        ('queued', 'submitting', 'submitted', 'downloaded', 'ingesting', 'ingested', 'failed')),
    -- Claim token of the process currently allowed to write this job. Every
    -- write after a claim is conditional on it, so a process whose claim was
    -- reclaimed as stale has its late result discarded.
    claim TEXT,
    -- Persisted when the job is CLAIMED for submission: the exact prompt sent,
    -- the model, and the provider's terms as reviewed at that moment (D5).
    -- Ingest reads these, never the current brief or providers.yaml.
    prompt TEXT,
    model TEXT,
    terms_json TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    polls INTEGER NOT NULL DEFAULT 0,
    submitted_at TEXT,
    files_json TEXT,
    next_attempt_at TEXT NOT NULL,
    external_id TEXT,
    handle_json TEXT,
    tried_providers TEXT NOT NULL DEFAULT '[]',
    last_error TEXT,
    updated_at TEXT NOT NULL
);

-- A provider that reported its quota spent (QuotaExhaustedError) is skipped
-- until its next quota reset.
CREATE TABLE IF NOT EXISTS provider_spent (
    provider TEXT PRIMARY KEY,
    until TEXT NOT NULL
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
    unpublished_at TEXT,
    -- Set when a newer batch took this slot over; unpublishing that newer
    -- batch restores this publication.
    superseded_by TEXT
);
"""


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    return conn
