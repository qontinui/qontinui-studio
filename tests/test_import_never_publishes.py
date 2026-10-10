"""An ``import:`` batch can never be published: not by ``publish``, not by
``jobs run`` (resume), not by crash recovery, not by a direct status write."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path

import pytest
from click.testing import CliRunner

from qontinui_studio.cli import main
from qontinui_studio.errors import PublicationRefused
from qontinui_studio.pipeline import Context, drive_batch, resume_all, run_brief
from qontinui_studio.publish import publish_batch, recover, unpublish_batch
from qontinui_studio.review import build_review_page
from qontinui_studio.store import connect
from qontinui_studio.workspace import Workspace

from .conftest import FakeClock
from .library_support import do_import, make_inbox

BATCH = "import:inbox-1"


@pytest.fixture
def imported(studio_root: Path, tmp_path: Path) -> str:
    do_import(studio_root, make_inbox(tmp_path))
    return BATCH


def _status(ctx: Context, batch_id: str) -> str:
    return str(
        ctx.conn.execute("SELECT status FROM batches WHERE id = ?", (batch_id,)).fetchone()[0]
    )


def test_publish_refuses_an_import_batch(
    make_context: Callable[..., Context], imported: str
) -> None:
    ctx = make_context()
    with pytest.raises(PublicationRefused, match="is 'imported'"):
        publish_batch(
            ctx.conn,
            ctx.sites,
            imported,
            ctx.notifier,
            ctx.clock,
            bridge=ctx.bridge,
            fetch=ctx.fetch,
            sleep=ctx.sleep,
        )
    assert _status(ctx, imported) == "imported"
    assert ctx.conn.execute("SELECT COUNT(*) FROM publications").fetchone()[0] == 0


def test_cli_publish_refuses_an_import_batch(studio_root: Path, imported: str) -> None:
    r = CliRunner().invoke(main, ["--root", str(studio_root), "publish", "--batch", imported])
    assert r.exit_code == 2, r.output
    assert "is 'imported'" in r.output


def test_jobs_run_and_drive_never_pick_up_an_import_batch(
    make_context: Callable[..., Context], write_brief: Callable[..., Path], imported: str
) -> None:
    ctx = make_context()
    # A generated batch left 'generated' (as `brief run --no-publish` would
    # not, but a crash would) is resumed; the import is not.
    generated = run_brief(ctx, write_brief(), publish=False)
    ctx.conn.execute("UPDATE batches SET no_publish = 0 WHERE id = ?", (generated.batch_id,))
    ctx.conn.commit()
    outcomes = resume_all(ctx)
    assert {o.batch_id for o in outcomes} == {generated.batch_id}
    assert imported not in {o.batch_id for o in outcomes}
    assert drive_batch(ctx, imported).driven is False
    assert _status(ctx, imported) == "imported"


def test_cli_jobs_run_skips_an_import_batch(studio_root: Path, imported: str) -> None:
    r = CliRunner().invoke(main, ["--root", str(studio_root), "jobs", "run"])
    assert r.exit_code == 0, r.output
    assert "no batches are generating" in r.output


def test_recovery_leaves_an_import_batch_alone(
    make_context: Callable[..., Context], imported: str
) -> None:
    ctx = make_context()
    for site in ctx.sites.values():
        site.publish_dir.mkdir(parents=True, exist_ok=True)
    report = recover(ctx.conn, ctx.sites, ctx.notifier)
    assert not any(imported in line for line in report.lines)
    assert _status(ctx, imported) == "imported"


def test_unpublish_refuses_an_import_batch(
    make_context: Callable[..., Context], imported: str
) -> None:
    ctx = make_context()
    with pytest.raises(PublicationRefused, match="nothing to unpublish"):
        unpublish_batch(ctx.conn, ctx.sites, imported, ctx.notifier, ctx.clock)


@pytest.mark.parametrize(
    "status", ["generating", "generated", "publishing", "published", "refused", "unpublished"]
)
def test_the_database_refuses_to_move_an_import_batch(
    make_context: Callable[..., Context], imported: str, status: str
) -> None:
    ctx = make_context()
    with pytest.raises(sqlite3.IntegrityError):
        ctx.conn.execute("UPDATE batches SET status = ? WHERE id = ?", (status, imported))
    ctx.conn.rollback()
    with pytest.raises(sqlite3.IntegrityError):
        ctx.conn.execute("UPDATE batches SET no_publish = 0 WHERE id = ?", (imported,))
    ctx.conn.rollback()


def test_review_page_shows_an_import_without_a_regenerate_command(
    studio_root: Path, imported: str, clock: FakeClock
) -> None:
    ws = Workspace(studio_root)
    page = build_review_page(connect(ws.db_path), ws.review_dir, clock(), root=studio_root)
    text = page.read_text()
    assert "import:inbox-1 — imported" in text
    assert "an import is never published" in text
    assert "brief run None" not in text and "approval" in text
