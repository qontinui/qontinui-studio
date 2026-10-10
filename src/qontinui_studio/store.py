"""The studio's one SQLite file: scheduler state and the asset library.

Kept in one file so a batch's jobs, the assets they produced, the gate results
that judged them and the publications they became can be read in one join.
"""

from __future__ import annotations

import sqlite3
from collections import Counter
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


# ---------------------------------------------------------------------------
# Schema versions. ``SCHEMA`` above is version 0 — the schema every studio.db
# was created with before versioning existed (``PRAGMA user_version`` = 0).
# Every database, fresh or old, is created at version 0 and brought forward by
# the migrations below, so a fresh database and an upgraded one take the SAME
# path and cannot drift apart.
#
# Version 1 — the library holds IMPORTED assets as well as generated ones:
#
# * ``batches.origin`` ('generated' | 'imported'). An imported batch has no
#   brief, and its status is pinned to 'imported' by a CHECK in both
#   directions, so no code path (publish, jobs run, recovery) can move it to
#   'publishing' — the database itself refuses.
# * ``assets.origin``; ``job_id``/``brief_id``/``provider_name``/
#   ``automation_allowed`` are required for a generated asset and absent for an
#   imported one (CHECKs say which). New provenance columns: ``account`` (an
#   alias, never an address), ``reference_images_json``, ``generated_on``,
#   ``licence_terms_url``, ``mime``, and the approval state
#   (``approval_status``/``approval_by``/``approval_on``). An approved asset
#   must have ``licence_commercial_use = 'yes'`` (D5), enforced by a CHECK.
#   ``ai_generated = 1`` stays pinned.
# ---------------------------------------------------------------------------

SCHEMA_VERSION = 1

_BATCHES_V1 = """
CREATE TABLE batches_v1 (
    id TEXT PRIMARY KEY,
    origin TEXT NOT NULL DEFAULT 'generated' CHECK (origin IN ('generated', 'imported')),
    brief_id TEXT,
    brief_path TEXT,
    brief_sha256 TEXT,
    targets_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN
        ('generating', 'generated', 'publishing', 'published', 'refused', 'unpublished',
         'failed', 'imported')),
    reason TEXT,
    driver TEXT,
    driver_until TEXT,
    notified_at TEXT,
    no_publish INTEGER NOT NULL DEFAULT 0,
    -- An imported batch is 'imported' forever, and only an imported batch is.
    CHECK ((origin = 'imported') = (status = 'imported')),
    CHECK (origin = 'imported' OR
           (brief_id IS NOT NULL AND brief_path IS NOT NULL AND brief_sha256 IS NOT NULL)),
    CHECK (origin = 'generated' OR no_publish = 1)
)
"""

_ASSETS_V1 = """
CREATE TABLE assets_v1 (
    uid TEXT PRIMARY KEY,
    asset_id TEXT NOT NULL,
    origin TEXT NOT NULL DEFAULT 'generated' CHECK (origin IN ('generated', 'imported')),
    brief_id TEXT,
    batch_id TEXT NOT NULL REFERENCES batches(id),
    job_id TEXT REFERENCES jobs(id),
    provider TEXT NOT NULL,
    provider_name TEXT,
    model TEXT NOT NULL,
    prompt TEXT NOT NULL,
    created_at TEXT NOT NULL,
    raw_path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    width INTEGER NOT NULL,
    height INTEGER NOT NULL,
    mime TEXT,
    ai_generated INTEGER NOT NULL DEFAULT 1 CHECK (ai_generated = 1),
    licence_commercial_use TEXT NOT NULL CHECK (licence_commercial_use IN ('yes', 'no', 'unknown')),
    licence_source TEXT,
    licence_tos_reviewed TEXT,
    licence_terms_url TEXT,
    automation_allowed TEXT,
    watermark TEXT NOT NULL,
    watermark_visible INTEGER,
    account TEXT,
    reference_images_json TEXT NOT NULL DEFAULT '[]',
    generated_on TEXT,
    approval_status TEXT NOT NULL DEFAULT 'candidate'
        CHECK (approval_status IN ('candidate', 'approved', 'rejected')),
    approval_by TEXT,
    approval_on TEXT,
    CHECK (origin = 'imported' OR (job_id IS NOT NULL AND brief_id IS NOT NULL AND
           provider_name IS NOT NULL AND automation_allowed IS NOT NULL)),
    CHECK (origin = 'generated' OR (job_id IS NULL AND account IS NOT NULL AND
           generated_on IS NOT NULL AND licence_terms_url IS NOT NULL AND mime IS NOT NULL)),
    -- A candidate carries no decision; an approval or rejection carries who and when.
    CHECK (approval_status = 'candidate' OR (approval_by IS NOT NULL AND approval_on IS NOT NULL)),
    CHECK (approval_status <> 'candidate' OR (approval_by IS NULL AND approval_on IS NULL)),
    -- D5: only an asset whose terms allow commercial use can be approved.
    CHECK (approval_status <> 'approved' OR licence_commercial_use = 'yes')
)
"""

_V0_ASSET_COLUMNS = (
    "uid, asset_id, brief_id, batch_id, job_id, provider, provider_name, model, prompt, "
    "created_at, raw_path, sha256, width, height, ai_generated, licence_commercial_use, "
    "licence_source, licence_tos_reviewed, automation_allowed, watermark, watermark_visible"
)

_V0_BATCH_COLUMNS = (
    "id, brief_id, brief_path, brief_sha256, targets_json, created_at, status, reason, "
    "driver, driver_until, notified_at, no_publish"
)


def _migrate_v0_to_v1(conn: sqlite3.Connection) -> None:
    """Rebuild ``batches`` and ``assets`` with the v1 columns and CHECKs (SQLite
    cannot alter a CHECK in place). Existing rows are generated rows."""
    conn.execute(_BATCHES_V1)
    conn.execute(
        f"INSERT INTO batches_v1 ({_V0_BATCH_COLUMNS}) SELECT {_V0_BATCH_COLUMNS} FROM batches"
    )
    conn.execute("DROP TABLE batches")
    conn.execute("ALTER TABLE batches_v1 RENAME TO batches")
    conn.execute(_ASSETS_V1)
    conn.execute(
        f"INSERT INTO assets_v1 ({_V0_ASSET_COLUMNS}) SELECT {_V0_ASSET_COLUMNS} FROM assets"
    )
    conn.execute("DROP TABLE assets")
    conn.execute("ALTER TABLE assets_v1 RENAME TO assets")
    # Re-importing the same bytes under the same asset id is a no-op.
    conn.execute(
        "CREATE UNIQUE INDEX assets_imported_identity ON assets (asset_id, sha256) "
        "WHERE origin = 'imported'"
    )


_MIGRATIONS = {0: _migrate_v0_to_v1}


def _dangling(conn: sqlite3.Connection) -> Counter[tuple[str, str]]:
    """Foreign-key violations as (table, parent) counts. Rowids are not
    compared: a table rebuild renumbers them."""
    return Counter((r[0], r[2]) for r in conn.execute("PRAGMA foreign_key_check"))


def _migrate(conn: sqlite3.Connection) -> None:
    """Bring the database to :data:`SCHEMA_VERSION`, one version per
    transaction. Foreign keys are off while tables are rebuilt (SQLite ignores
    the pragma inside a transaction, so it is set around it) and every
    reference is re-checked before the version is committed."""
    if conn.execute("PRAGMA user_version").fetchone()[0] >= SCHEMA_VERSION:
        return
    conn.commit()
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        while True:
            conn.execute("BEGIN IMMEDIATE")
            # Re-read under the write lock: another process may have migrated.
            version = int(conn.execute("PRAGMA user_version").fetchone()[0])
            if version >= SCHEMA_VERSION:
                conn.rollback()
                return
            try:
                # Only references the migration itself broke refuse it: a
                # dangling row that predates it must not brick every command.
                before = _dangling(conn)
                _MIGRATIONS[version](conn)
                new = _dangling(conn) - before
                if new:
                    raise sqlite3.IntegrityError(
                        f"migration {version}->{version + 1} left dangling references: "
                        f"{sorted(new.elements())[:5]}"
                    )
                conn.execute(f"PRAGMA user_version = {version + 1}")
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
    finally:
        conn.execute("PRAGMA foreign_keys = ON")


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn
