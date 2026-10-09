"""Regression tests for the third review's reproduction scripts (r1..r9).

Each test is the in-process form of one script: the scenario that broke, and
the property that must now hold.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path

import pytest
from PIL import Image

from qontinui_studio import pipeline
from qontinui_studio import publish as publish_mod
from qontinui_studio.adapters import AdapterSet, GenerationRequest
from qontinui_studio.adapters.base import SubmittedJob
from qontinui_studio.adapters.fake import FakeImageAdapter
from qontinui_studio.briefs import load_brief
from qontinui_studio.pipeline import Context, drive_batch, new_batch, resume_all, run_brief
from qontinui_studio.publish import JOURNAL_PREFIX, unpublish_batch

from .conftest import FakeClock, debris, notice_text


class Crash(BaseException):
    """Stands in for the process dying: nothing after it runs."""


def test_r1_concurrent_ingest_of_a_stale_row_is_a_no_op(
    make_context: Callable[..., Context], write_brief: Callable[..., Path], studio_root: Path
) -> None:
    a, b = make_context(), make_context()
    loaded = load_brief(write_brief(), root=studio_root)
    batch = new_batch(a, loaded)
    a.scheduler().run({batch: pipeline._request_for(loaded)}, sleep=a.sleep)
    stale_b = b.conn.execute("SELECT * FROM jobs WHERE status = 'downloaded'").fetchall()
    for job in a.conn.execute("SELECT * FROM jobs WHERE status = 'downloaded'").fetchall():
        assert pipeline.ingest(a, loaded, job) is True
    for job in stale_b:
        assert pipeline.ingest(b, loaded, job) is False  # claim fails; nothing written
    row = a.conn.execute("SELECT status, last_error FROM jobs").fetchone()
    assert (row["status"], row["last_error"]) == ("ingested", None)
    assert a.conn.execute("SELECT COUNT(*) FROM assets").fetchone()[0] == 1


def test_r2_publish_never_clobbers_an_owner_supplied_file(
    make_context: Callable[..., Context], write_brief: Callable[..., Path], studio_root: Path
) -> None:
    live = studio_root / "published" / "cafe"
    live.mkdir(parents=True)
    (live / "hero.webp").write_bytes(b"OWNER-SUPPLIED REAL PHOTO")
    ctx = make_context()
    out = run_brief(ctx, write_brief())
    assert out.status == "refused"
    assert out.publish is not None
    assert [g.subject for g in out.publish.failures if g.gate == "foreign_file"] == [
        str(live / "hero.webp")
    ]
    assert (live / "hero.webp").read_bytes() == b"OWNER-SUPPLIED REAL PHOTO"
    assert not debris(live)
    assert not (studio_root / "published" / "barber" / "hero.webp").exists()


def test_r3_crash_after_commit_still_records_gates_and_notifies(
    make_context: Callable[..., Context],
    write_brief: Callable[..., Path],
    studio_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def die(self: publish_mod.Journal) -> None:
        raise Crash()

    monkeypatch.setattr(publish_mod.Journal, "roll_forward", die)
    ctx = make_context()
    with pytest.raises(Crash):
        run_brief(ctx, write_brief())
    monkeypatch.undo()
    batch = ctx.conn.execute("SELECT * FROM batches").fetchone()
    assert batch["status"] == "published" and batch["notified_at"] is None
    # Gate results were committed WITH the publication.
    assert ctx.conn.execute("SELECT COUNT(*) FROM gate_results").fetchone()[0] > 0
    assert not list(ctx.workspace.notices_dir.glob("*-published-*.md"))

    resume_all(make_context())
    assert notice_text(ctx.workspace.notices_dir, batch["id"], "published")
    assert ctx.conn.execute("SELECT notified_at FROM batches").fetchone()[0] is not None
    assert not debris(studio_root / "published" / "cafe")


def test_r4_a_site_whose_recovery_fails_only_holds_back_its_own_batches(
    make_context: Callable[..., Context],
    write_brief: Callable[..., Path],
    studio_root: Path,
) -> None:
    other = (studio_root / "sites" / "cafe.yaml").read_text()
    other = (
        other.replace("cafe-template", "other")
        .replace("../published/cafe", "../published/other")
        .replace("previews/cafe", "previews/other")
        .replace("preview.test/cafe", "preview.test/other")
    )
    (studio_root / "sites" / "other.yaml").write_text(other)
    broken = studio_root / "published" / "other"
    broken.mkdir(parents=True)
    (broken / f"{JOURNAL_PREFIX}x.json").write_text("{ not json")
    ctx = make_context()
    healthy = new_batch(ctx, load_brief(write_brief(), root=studio_root))
    held = write_brief(name="other.yaml")
    held.write_text(
        held.read_text()
        .replace("{site: cafe-template, slot: hero}", "{site: other, slot: hero}")
        .replace("  - {site: barber-template, slot: hero}\n", "")
    )
    held_batch = new_batch(ctx, load_brief(held, root=studio_root))
    outcomes = {o.batch_id: o for o in resume_all(ctx)}
    assert outcomes[healthy].status == "published"
    assert outcomes[held_batch].error and "held back" in outcomes[held_batch].error
    assert outcomes[held_batch].status == "generating"


class SlowNonCommercial(FakeImageAdapter):
    """Process A's submit outlives its claim lease; process B runs meanwhile."""

    def __init__(self, clock: FakeClock, during: Callable[[], None]) -> None:
        super().__init__("fake2", "fake-v2")
        self.clock = clock
        self.during = during

    def submit(self, request: GenerationRequest, staging_dir: Path) -> SubmittedJob:
        self.clock.now += timedelta(seconds=901)
        self.during()
        handle = super().submit(request, staging_dir)
        Image.new("RGB", (1600, 900), (255, 0, 0)).save(handle.data["staged"][0])
        return handle


def test_r5_a_reclaimed_submission_never_mixes_provider_and_licence(
    make_context: Callable[..., Context],
    write_brief: Callable[..., Path],
    studio_root: Path,
    clock: FakeClock,
) -> None:
    p = studio_root / "providers.yaml"
    p.write_text(
        p.read_text().replace(
            "model: fake-v2\n    daily_quota: 10", "model: fake-v2\n    daily_quota: 1"
        )
    )
    loaded = load_brief(write_brief(providers="fake2, fake"), root=studio_root)
    holder: dict[str, object] = {}
    b = make_context(
        adapters=AdapterSet(
            adapters={"fake2": FakeImageAdapter("fake2", "fake-v2"), "fake": FakeImageAdapter()}
        )
    )

    def process_b() -> None:
        b.scheduler().step({str(holder["batch"]): pipeline._request_for(loaded)})

    a = make_context(
        adapters=AdapterSet(
            adapters={"fake2": SlowNonCommercial(clock, process_b), "fake": FakeImageAdapter()}
        )
    )
    holder["batch"] = batch = new_batch(a, loaded)
    a.scheduler().run({batch: pipeline._request_for(loaded)}, sleep=clock.sleep)
    job = a.conn.execute("SELECT * FROM jobs").fetchone()
    terms = json.loads(job["terms_json"])
    assert job["provider"] == terms["provider"] == "fake"
    drive_batch(a, batch, publish=False)
    asset = a.conn.execute("SELECT * FROM assets").fetchone()
    assert asset["provider"] == "fake" and asset["licence_commercial_use"] == "yes"
    with Image.open(asset["raw_path"]) as img:
        assert img.getpixel((0, 0)) != (255, 0, 0)  # not A's late (fake2) image


def test_r6_a_crash_mid_unpublish_is_rolled_back_by_recovery(
    make_context: Callable[..., Context],
    write_brief: Callable[..., Path],
    studio_root: Path,
    clock: FakeClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_brief(make_context(), write_brief())
    clock.sleep(60)
    ctx = make_context()
    second = run_brief(ctx, write_brief(name="h2.yaml"))
    before = (studio_root / "published" / "barber" / "hero.webp").read_bytes()

    real = publish_mod.atomic_write

    def dying(path: Path, data: bytes) -> None:
        if path.name == "studio-manifest.json" and "published" in path.parts:
            raise Crash()
        real(path, data)

    monkeypatch.setattr(publish_mod, "atomic_write", dying)
    monkeypatch.setattr(publish_mod.Journal, "roll_back", lambda self: [])
    with pytest.raises(Crash):
        unpublish_batch(
            ctx.conn,
            ctx.sites,
            second.batch_id,
            ctx.notifier,
            ctx.clock,
            bridge=ctx.bridge,
            fetch=ctx.fetch,
        )
    monkeypatch.undo()
    barber = studio_root / "published" / "barber"
    assert list(barber.glob(f"{JOURNAL_PREFIX}*.json"))

    resume_all(make_context())
    assert (barber / "hero.webp").read_bytes() == before
    manifest = json.loads((barber / "studio-manifest.json").read_text())
    assert manifest["slots"]["hero"]["batch_id"] == second.batch_id
    assert not debris(barber)
    assert list(ctx.workspace.notices_dir.glob("*-unpublish-rolled-back-*.md"))


def test_r8_preview_dir_at_the_studio_root_is_refused_before_anything_is_deleted(
    studio_root: Path, make_context: Callable[..., Context]
) -> None:
    from qontinui_studio.errors import ConfigError

    site = studio_root / "sites" / "cafe.yaml"
    site.write_text(site.read_text().replace("preview_dir: ../../previews/cafe", "preview_dir: .."))
    with pytest.raises(ConfigError, match="preview_dir"):
        make_context()
    assert (studio_root / "providers.yaml").is_file()


def test_r9_double_drive_publishes_once_and_unpublish_does_not_restore_itself(
    make_context: Callable[..., Context],
    write_brief: Callable[..., Path],
    studio_root: Path,
    clock: FakeClock,
) -> None:
    a, b = make_context(), make_context()
    batch = new_batch(a, load_brief(write_brief(), root=studio_root))
    assert drive_batch(a, batch).status == "published"
    clock.sleep(5)
    second = drive_batch(b, batch)
    assert second.driven is False and second.publish is None
    rows = a.conn.execute(
        "SELECT COUNT(*) FROM publications WHERE batch_id = ? AND superseded_by IS NULL", (batch,)
    ).fetchone()[0]
    assert rows == 4  # 2 sites x 2 formats, published once
    unpublish_batch(a.conn, a.sites, batch, a.notifier, clock, bridge=a.bridge, fetch=a.fetch)
    assert not (studio_root / "published" / "cafe" / "hero.webp").exists()
    assert (
        a.conn.execute("SELECT COUNT(*) FROM publications WHERE unpublished_at IS NULL").fetchone()[
            0
        ]
        == 0
    )


def test_concurrent_drivers_respect_the_batch_lease(
    make_context: Callable[..., Context], write_brief: Callable[..., Path], studio_root: Path
) -> None:
    a = make_context()
    batch = new_batch(a, load_brief(write_brief(), root=studio_root))
    lease = pipeline._Lease(a, batch)
    assert lease.acquire()
    out = drive_batch(make_context(), batch)
    assert out.driven is False and "another process" in (out.note or "")
    lease.release()
    assert drive_batch(make_context(), batch).status == "published"
