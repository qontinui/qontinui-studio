"""Schema versioning: a studio.db created by the 355ce64 schema (user_version
0) is upgraded in place to v1 with every row kept, and a fresh database ends
up with exactly the same schema."""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Callable
from pathlib import Path

import pytest

from qontinui_studio.library import Library
from qontinui_studio.pipeline import Context, run_brief
from qontinui_studio.store import SCHEMA_VERSION, connect
from qontinui_studio.workspace import Workspace

from .library_support import write_image

V0_SCHEMA = (Path(__file__).parent / "fixtures" / "studio-db-schema-355ce64.sql").read_text()
T = "2026-10-09T12:00:00+00:00"


def _v0_db(path: Path, raw: Path) -> None:
    """A studio.db as the 355ce64 code left it: one published batch."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(V0_SCHEMA)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 0
    conn.execute(
        "INSERT INTO batches (id, brief_id, brief_path, brief_sha256, targets_json, created_at, "
        "status, notified_at, no_publish) VALUES (?,?,?,?,?,?,?,?,0)",
        (
            "hero-1",
            "hero",
            "/x/briefs/hero.yaml",
            "a" * 64,
            '[["cafe", "hero"]]',
            T,
            "published",
            T,
        ),
    )
    conn.execute(
        "INSERT INTO jobs (id, batch_id, variant_index, provider, status, prompt, model, "
        "terms_json, next_attempt_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("hero-1-v0", "hero-1", 0, "fake", "ingested", "a cafe", "fake-v1", "{}", T, T),
    )
    conn.execute(
        "INSERT INTO assets (uid, asset_id, brief_id, batch_id, job_id, provider, provider_name, "
        "model, prompt, created_at, raw_path, sha256, width, height, ai_generated, "
        "licence_commercial_use, licence_source, licence_tos_reviewed, automation_allowed, "
        "watermark, watermark_visible) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?,?,?,?,?,?)",
        (
            "hero-1-v0-0",
            "hero",
            "hero",
            "hero-1",
            "hero-1-v0",
            "fake",
            "Fake",
            "fake-v1",
            "a cafe",
            T,
            str(raw),
            "b" * 64,
            12,
            8,
            "yes",
            "https://example.test/tos",
            "2026-10-09",
            "yes",
            "none",
            0,
        ),
    )
    conn.execute(
        "INSERT INTO variants (id, asset_uid, site, slot, campaign, format, path, width, height, "
        "sha256, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            "hero-1-v0-0:cafe:hero:webp",
            "hero-1-v0-0",
            "cafe",
            "hero",
            None,
            "webp",
            str(raw),
            12,
            8,
            "c" * 64,
            T,
        ),
    )
    conn.execute(
        "INSERT INTO gate_results VALUES (?,?,?,?,?,?)", ("hero-1", "d5", "x", "pass", "ok", T)
    )
    conn.execute(
        "INSERT INTO publications (batch_id, variant_id, site, slot, published_path, label, "
        "published_at) VALUES (?,?,?,?,?,?,?)",
        ("hero-1", "hero-1-v0-0:cafe:hero:webp", "cafe", "hero", "/p/hero.webp", "AI", T),
    )
    conn.commit()
    conn.close()


def _tables(conn: sqlite3.Connection) -> dict[str, str]:
    return {
        r["name"]: r["sql"]
        for r in conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY name"
        )
    }


def test_upgrade_from_the_355ce64_schema_keeps_every_row(tmp_path: Path) -> None:
    db = tmp_path / ".studio" / "studio.db"
    _v0_db(db, write_image(tmp_path / "raw.png"))
    conn = connect(db)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION == 7
    # v6: compile batches; every upgraded batch is an ordinary one.
    assert (
        conn.execute("SELECT COUNT(*) FROM batches WHERE compile_target IS NOT NULL").fetchone()[0]
        == 0
    )
    # v5: a launch's notice fields, frozen at publish; pre-v5 launches have none.
    launch_cols = {r["name"] for r in conn.execute("PRAGMA table_info(launches)")}
    assert {"unpublish_command", "public_url"} <= launch_cols
    # v4: the image-generation dial's last-known level; an upgraded studio has
    # read none, and a level outside the dial's vocabulary is refused.
    assert conn.execute("SELECT COUNT(*) FROM imagegen_dial_last_known").fetchone()[0] == 0
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO imagegen_dial_last_known (repo_key, level, read_at) "
            "VALUES ('', 'local-only', 't')"
        )
    # v3: library tags; an upgraded library starts with none.
    assert conn.execute("SELECT COUNT(*) FROM asset_tags").fetchone()[0] == 0
    # v2: typed job states and the refundable quota day.
    job_cols = {r["name"] for r in conn.execute("PRAGMA table_info(jobs)")}
    assert {"quota_day", "adapter"} <= job_cols
    assert conn.execute("SELECT COUNT(*) FROM job_attempts").fetchone()[0] == 0
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO job_attempts (job_id, provider, outcome, at) VALUES (?, 'p', 'pending', 't')",
            (conn.execute("SELECT id FROM jobs").fetchone()[0],),
        )
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert not conn.execute("PRAGMA foreign_key_check").fetchall()
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"

    b = conn.execute("SELECT * FROM batches WHERE id = 'hero-1'").fetchone()
    assert (b["origin"], b["status"], b["brief_path"], b["notified_at"]) == (
        "generated",
        "published",
        "/x/briefs/hero.yaml",
        T,
    )
    a = Library(conn).asset("hero-1-v0-0")
    assert (a.origin, a.job_id, a.brief_id, a.provider_name) == (
        "generated",
        "hero-1-v0",
        "hero",
        "Fake",
    )
    assert (a.approval_status, a.approval_by, a.approval_on) == ("candidate", None, None)
    assert a.reference_images == [] and a.account is None and a.mime is None
    assert a.licence_tos_reviewed == "2026-10-09" and a.watermark_visible is False
    assert [v.id for v in Library(conn).variants(a.uid)] == ["hero-1-v0-0:cafe:hero:webp"]
    for table, n in (("jobs", 1), ("gate_results", 1), ("publications", 1)):
        assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == n
    conn.close()

    # A second connect is a no-op.
    again = connect(db)
    assert again.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    assert again.execute("SELECT COUNT(*) FROM assets").fetchone()[0] == 1


def test_fresh_and_upgraded_databases_have_the_same_schema(tmp_path: Path) -> None:
    upgraded = tmp_path / "old" / "studio.db"
    _v0_db(upgraded, write_image(tmp_path / "raw.png"))
    fresh = connect(tmp_path / "new" / "studio.db")
    assert _tables(connect(upgraded)) == _tables(fresh)
    assert "assets_imported_identity" in _tables(fresh)


def test_v1_checks_hold_in_the_database(tmp_path: Path) -> None:
    db = tmp_path / "studio.db"
    _v0_db(db, write_image(tmp_path / "raw.png"))
    conn = connect(db)
    for statement in (
        # A generated asset must keep its job.
        "UPDATE assets SET job_id = NULL WHERE uid = 'hero-1-v0-0'",
        # ai_generated stays pinned.
        "UPDATE assets SET ai_generated = 0 WHERE uid = 'hero-1-v0-0'",
        # An approval carries who and when ...
        "UPDATE assets SET approval_status = 'approved' WHERE uid = 'hero-1-v0-0'",
        # ... a candidate carries neither ...
        "UPDATE assets SET approval_by = 'josh' WHERE uid = 'hero-1-v0-0'",
        # ... and (D5) only a commercially usable asset is approved.
        "UPDATE assets SET licence_commercial_use = 'no', approval_status = 'approved', "
        "approval_by = 'j', approval_on = '2026-10-10' WHERE uid = 'hero-1-v0-0'",
        # A generated batch is never 'imported', and keeps its brief.
        "UPDATE batches SET status = 'imported' WHERE id = 'hero-1'",
        "UPDATE batches SET brief_id = NULL WHERE id = 'hero-1'",
    ):
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(statement)
        conn.rollback()


def test_the_pipeline_runs_on_an_upgraded_database(
    make_context: Callable[..., Context],
    write_brief: Callable[..., Path],
    studio_root: Path,
    tmp_path: Path,
) -> None:
    _v0_db(Workspace(studio_root).db_path, write_image(tmp_path / "raw.png"))
    ctx = make_context()
    outcome = run_brief(ctx, write_brief())
    assert outcome.status == "published", outcome
    fresh = Library(ctx.conn).assets(outcome.batch_id)
    assert fresh and all(a.origin == "generated" and a.mime == "image/png" for a in fresh)
    assert all(a.approval_status == "candidate" for a in fresh)


def test_a_dangling_reference_that_predates_the_migration_does_not_block_it(
    tmp_path: Path,
) -> None:
    db = tmp_path / "studio.db"
    _v0_db(db, write_image(tmp_path / "raw.png"))
    raw = sqlite3.connect(db)  # foreign keys off, as a hand edit would be
    raw.execute("UPDATE gate_results SET batch_id = 'gone'")
    raw.commit()
    raw.close()
    conn = connect(db)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    assert conn.execute("SELECT COUNT(*) FROM assets").fetchone()[0] == 1


def test_concurrent_connects_migrate_exactly_once(tmp_path: Path) -> None:
    db = tmp_path / "studio.db"
    _v0_db(db, write_image(tmp_path / "raw.png"))
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            c = connect(db)
            assert c.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
            c.close()
        except BaseException as e:  # surfaced below
            errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors
    conn = connect(db)
    assert conn.execute("SELECT COUNT(*) FROM assets").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM batches").fetchone()[0] == 1


def test_a_v6_database_without_compile_checkout_gains_it(tmp_path: Path) -> None:
    """This phase's first commits shipped the compile columns without
    ``compile_checkout``; v7 adds it where absent, and leaves it where
    present."""
    db = tmp_path / ".studio" / "studio.db"
    conn = connect(db)
    conn.execute("ALTER TABLE batches DROP COLUMN compile_checkout")
    conn.execute("PRAGMA user_version = 6")
    conn.commit()
    conn.close()
    again = connect(db)
    assert again.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION == 7
    columns = {r[1] for r in again.execute("PRAGMA table_info(batches)")}
    assert "compile_checkout" in columns
    again.execute("PRAGMA user_version = 6")  # a v6 that already has it
    again.commit()
    again.close()
    third = connect(db)
    assert third.execute("PRAGMA user_version").fetchone()[0] == 7
