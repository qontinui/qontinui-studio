"""Regression tests for the fourth review's reproduction scripts (f1..f11).

Each test is the in-process form of one script: the scenario that broke, and
the property that must now hold.
"""

from __future__ import annotations

import base64
import json
import shlex
from collections.abc import Callable
from pathlib import Path

import pytest

from qontinui_studio.adapters import AdapterSet, GenerationRequest
from qontinui_studio.adapters.base import SubmittedJob
from qontinui_studio.adapters.fake import FakeImageAdapter
from qontinui_studio.briefs import load_brief
from qontinui_studio.errors import (
    ConfigError,
    JournalError,
    PublicationRefused,
    TransientProviderError,
)
from qontinui_studio.pipeline import Context, new_batch, resume_all, run_brief
from qontinui_studio.publish import JOURNAL_PREFIX, Journal, unpublish_batch, unpublish_command
from qontinui_studio.scheduler import Scheduler
from qontinui_studio.sites import load_sites

from .conftest import FakeBridge, FakeClock, FakePreview, debris, notice_text


def one_site_brief(studio_root: Path, name: str, site: str, provider: str, prompt: str) -> Path:
    p = studio_root / "briefs" / name
    p.write_text(
        f"id: {name.split('.')[0]}\npurpose: t\ntargets: [{{site: {site}, slot: hero}}]\n"
        f'aspect_ratio: "16:9"\nprompt: {prompt}\nprovider_preference: [{provider}]\n'
    )
    return p


def test_f1_a_planted_journal_is_never_acted_on(
    make_context: Callable[..., Context], studio_root: Path, tmp_path: Path
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = outside / "victim.txt"
    victim.write_text("precious")
    planted = outside / "planted.sh"
    pub = studio_root / "published" / "cafe"
    pub.mkdir(parents=True)
    (pub / "payload").write_text("#!/bin/sh\necho owned\n")
    evil = {
        "kind": "publish",
        "batch_id": "nonexistent",
        "site": "cafe-template",
        "publish_dir": str(outside),
        "placed": [[str(victim), str(outside / "x"), "0" * 64]],
        "moved": [[str(planted), str(pub / "payload"), "0" * 64, True]],
        "manifest_before": None,
        "manifest_after": base64.b64encode(b"zz").decode(),
    }
    (pub / f"{JOURNAL_PREFIX}evil.json").write_text(json.dumps(evil))
    ctx = make_context()
    resume_all(ctx)
    assert victim.read_text() == "precious"
    assert not planted.exists()
    assert (pub / "payload").exists()
    # And the journal is refused with a reason, not silently acted on.
    with pytest.raises(JournalError, match="batch_id does not match"):
        Journal.read(pub / f"{JOURNAL_PREFIX}evil.json", ctx.sites["cafe-template"])


@pytest.mark.parametrize(
    ("field", "value", "why"),
    [
        (
            "placed",
            [["/etc/.hero.webp.staged-" + "a" * 32, "/etc/hero.webp", "0" * 64]],
            "publish dir",
        ),
        ("placed", [["{pub}/.hero.webp.staged-xyz", "{pub}/hero.webp", "0" * 64]], "staged file"),
        (
            "moved",
            [["{pub}/hero.webp", "{pub}/../hero.webp.prev-" + "a" * 32, "0" * 64, True]],
            "prev file",
        ),
        (
            "moved",
            [["{pub}/notaslot.webp", "{pub}/.notaslot.webp.prev-" + "a" * 32, "0" * 64, True]],
            "slot file",
        ),
        ("site", "barber-template", "names site"),
    ],
)
def test_f1_journal_validation_rejects_every_escape(
    make_context: Callable[..., Context], studio_root: Path, field: str, value: object, why: str
) -> None:
    pub = studio_root / "published" / "cafe"
    pub.mkdir(parents=True)
    text = json.dumps(value).replace("{pub}", str(pub))
    journal = {
        "kind": "publish",
        "batch_id": "b1",
        "site": "cafe-template",
        "placed": [],
        "moved": [],
        "manifest_before": None,
        "manifest_after": "",
        field: json.loads(text),
    }
    path = pub / f"{JOURNAL_PREFIX}b1.json"
    path.write_text(json.dumps(journal))
    with pytest.raises(JournalError, match=why):
        Journal.read(path, make_context().sites["cafe-template"])


def test_f2_unpublish_restore_is_regated_and_left_empty_when_it_fails(
    make_context: Callable[..., Context], studio_root: Path, clock: FakeClock
) -> None:
    ctx = make_context()
    a = run_brief(ctx, one_site_brief(studio_root, "a.yaml", "cafe-template", "fake2", "A image"))
    assert a.status == "published"  # fake2 is non-commercial; cafe is not commercial
    clock.sleep(5)
    b = run_brief(ctx, one_site_brief(studio_root, "b.yaml", "cafe-template", "fake", "B image"))
    site = studio_root / "sites" / "cafe.yaml"
    site.write_text(site.read_text().replace("commercial: false", "commercial: true"))
    ctx = make_context()
    unpublish_batch(
        ctx.conn, ctx.sites, b.batch_id, ctx.notifier, ctx.clock, bridge=ctx.bridge, fetch=ctx.fetch
    )
    cafe = studio_root / "published" / "cafe"
    assert not (cafe / "hero.webp").exists()
    assert "hero" not in json.loads((cafe / "studio-manifest.json").read_text())["slots"]
    text = notice_text(ctx.workspace.notices_dir, b.batch_id, "unpublished")
    assert "slot left EMPTY" in text and "d5_licence" in text


def test_f2_restore_also_needs_d7(
    make_context: Callable[..., Context],
    studio_root: Path,
    clock: FakeClock,
    preview: FakePreview,
) -> None:
    run_brief(make_context(), one_site_brief(studio_root, "a.yaml", "cafe-template", "fake", "A"))
    clock.sleep(5)
    b = run_brief(
        make_context(), one_site_brief(studio_root, "b.yaml", "cafe-template", "fake", "B")
    )
    preview.summary = "CRITICAL"
    ctx = make_context()
    unpublish_batch(
        ctx.conn, ctx.sites, b.batch_id, ctx.notifier, ctx.clock, bridge=ctx.bridge, fetch=ctx.fetch
    )
    assert not (studio_root / "published" / "cafe" / "hero.webp").exists()
    assert "D7" in notice_text(ctx.workspace.notices_dir, b.batch_id, "unpublished")


def test_f3_a_generated_batch_whose_publish_could_not_start_is_published_by_jobs_run(
    make_context: Callable[..., Context], write_brief: Callable[..., Path], studio_root: Path
) -> None:
    pub = studio_root / "published" / "barber"
    pub.mkdir(parents=True)
    (pub / f"{JOURNAL_PREFIX}old.json").write_text("{truncated")
    ctx = make_context()
    out = run_brief(ctx, write_brief())
    assert out.status == "generated"
    assert out.error and "JournalError" in out.error
    (pub / f"{JOURNAL_PREFIX}old.json").unlink()
    outcomes = resume_all(make_context())
    assert [(o.batch_id, o.status) for o in outcomes] == [(out.batch_id, "published")]


def test_f3_no_publish_batches_are_never_published_by_resume(
    make_context: Callable[..., Context], write_brief: Callable[..., Path]
) -> None:
    ctx = make_context()
    out = run_brief(ctx, write_brief(), publish=False)
    assert out.status == "generated"
    assert resume_all(make_context()) == []


def test_f4_an_owner_file_written_during_d7_is_never_moved_or_deleted(
    make_context: Callable[..., Context],
    write_brief: Callable[..., Path],
    studio_root: Path,
    preview: FakePreview,
) -> None:
    cafe = studio_root / "published" / "cafe"

    def factory(base_url: str) -> FakeBridge:
        bridge = preview.bridge(base_url)
        real = bridge.page_health

        def page_health() -> dict[str, object]:
            if bridge.site == "cafe":
                cafe.mkdir(parents=True, exist_ok=True)
                (cafe / "hero.webp").write_bytes(b"OWNER-SUPPLIED-PHOTO")
            return real()

        bridge.page_health = page_health  # type: ignore[method-assign]
        return bridge

    out = run_brief(make_context(bridge=factory), write_brief())
    assert out.status == "refused"
    assert out.publish is not None
    assert any(g.gate == "foreign_file" for g in out.publish.failures)
    assert (cafe / "hero.webp").read_bytes() == b"OWNER-SUPPLIED-PHOTO"
    assert not debris(cafe)


def test_f4_a_change_between_journal_and_move_aborts_and_rolls_back(
    make_context: Callable[..., Context],
    write_brief: Callable[..., Path],
    studio_root: Path,
    clock: FakeClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_brief(make_context(), write_brief())
    cafe = studio_root / "published" / "cafe"
    before = (cafe / "hero.webp").read_bytes()
    clock.sleep(60)
    real_write = Journal.write

    def write_then_owner_edits(self: Journal) -> None:
        real_write(self)
        if self.site == "cafe-template":
            (cafe / "hero.webp").write_bytes(b"OWNER EDIT")

    monkeypatch.setattr(Journal, "write", write_then_owner_edits)
    out = run_brief(make_context(), write_brief(name="h2.yaml"))
    assert out.status == "refused"
    assert (cafe / "hero.webp").read_bytes() == b"OWNER EDIT"
    assert before != b"OWNER EDIT"
    assert not list(cafe.glob(f"{JOURNAL_PREFIX}*"))


def test_f5_preview_dirs_cannot_overlap_any_sites_publish_or_preview_dir(
    studio_root: Path,
) -> None:
    # Real deployments publish into a site repo OUTSIDE the studio root.
    barber = studio_root / "sites" / "barber.yaml"
    barber.write_text(barber.read_text().replace("../published/barber", "../../live/barber"))
    cafe = studio_root / "sites" / "cafe.yaml"
    original = cafe.read_text()
    cafe.write_text(original.replace("../../previews/cafe", "../../live/barber"))
    with pytest.raises(ConfigError, match="overlaps site 'barber-template' publish_dir"):
        load_sites(studio_root / "sites")
    cafe.write_text(original.replace("../../previews/cafe", "../../previews/barber"))
    with pytest.raises(ConfigError, match="overlapping preview_dir"):
        load_sites(studio_root / "sites")


class QuotaSpent(FakeImageAdapter):
    def submit(self, request: GenerationRequest, staging_dir: Path) -> SubmittedJob:
        raise TransientProviderError("429 RESOURCE_EXHAUSTED: daily request quota exceeded")


def test_f6a_a_quota_429_defers_to_the_reset_instead_of_failing(
    make_context: Callable[..., Context], write_brief: Callable[..., Path]
) -> None:
    ctx = make_context(adapters=AdapterSet(adapters={"fake": QuotaSpent("fake")}))
    out = run_brief(ctx, write_brief())
    assert out.status == "generating"
    job = ctx.conn.execute("SELECT * FROM jobs").fetchone()
    assert job["status"] == "queued" and job["tried_providers"] == "[]"
    assert job["next_attempt_at"].startswith("2026-10-10T00:00:00")
    assert (
        ctx.conn.execute("SELECT until FROM provider_spent")
        .fetchone()[0]
        .startswith("2026-10-10T00:00:00")
    )


def test_f6a_quota_resets_follow_the_providers_timezone(
    make_context: Callable[..., Context], studio_root: Path
) -> None:
    p = studio_root / "providers.yaml"
    p.write_text(
        p.read_text().replace(
            "daily_quota: 10\n", "daily_quota: 10\n    quota_reset_tz: America/Los_Angeles\n", 1
        )
    )
    sched = make_context().scheduler()
    from datetime import UTC, datetime

    now = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)  # 05:00 in Los Angeles
    assert sched.quota_day("fake", now) == "2026-10-09"
    assert sched.next_reset("fake", now).isoformat() == "2026-10-10T07:00:00+00:00"


def test_f6b_the_last_quota_unit_is_spent_once(
    make_context: Callable[..., Context], write_brief: Callable[..., Path], studio_root: Path
) -> None:
    p = studio_root / "providers.yaml"
    p.write_text(p.read_text().replace("daily_quota: 10", "daily_quota: 1", 1))
    a, b = make_context(), make_context()
    lx = load_brief(write_brief(name="x.yaml"), root=studio_root)
    ly = load_brief(write_brief(name="y.yaml"), root=studio_root)
    bx, by = new_batch(a, lx), new_batch(b, ly)
    from qontinui_studio import pipeline

    sa, sb = a.scheduler(), b.scheduler()
    orig = Scheduler.quota_remaining
    fired: list[int] = []

    def interleave(self: Scheduler, pid: str, now: object) -> int:
        r = orig(self, pid, now)  # type: ignore[arg-type]
        if self is sa and not fired:
            fired.append(1)
            sb.step({by: pipeline._request_for(ly)})
        return r

    Scheduler.quota_remaining = interleave  # type: ignore[method-assign]
    try:
        sa.step({bx: pipeline._request_for(lx)})
    finally:
        Scheduler.quota_remaining = orig  # type: ignore[method-assign]
    assert a.conn.execute("SELECT used FROM quota_usage").fetchone()[0] == 1
    assert a.conn.execute("SELECT COUNT(*) FROM jobs WHERE status != 'queued'").fetchone()[0] == 1


def test_f7_claims_read_the_clock_and_are_never_born_expired(
    make_context: Callable[..., Context],
    write_brief: Callable[..., Path],
    studio_root: Path,
    clock: FakeClock,
) -> None:
    calls: list[str] = []
    holder: dict[str, object] = {}

    class Slow(FakeImageAdapter):
        hard_timeout_s = 360.0

        def submit(self, request: GenerationRequest, staging_dir: Path) -> SubmittedJob:
            calls.append(request.job_id)
            if request.job_id.endswith("-v1"):
                row = (
                    holder["conn"]
                    .execute(  # type: ignore[attr-defined]
                        "SELECT next_attempt_at FROM jobs WHERE id = ?", (request.job_id,)
                    )
                    .fetchone()
                )
                assert row[0] > clock().isoformat()  # the lease runs from the claim
                holder["other"].step(holder["req"])  # type: ignore[attr-defined]
            clock.sleep(1000)
            return super().submit(request, staging_dir)

    adapters = AdapterSet(adapters={"fake": Slow("fake")})
    ctx = make_context(adapters=adapters)
    loaded = load_brief(write_brief(variants=2), root=studio_root)
    batch = new_batch(ctx, loaded)
    from qontinui_studio import pipeline

    other_ctx = make_context(adapters=adapters)
    holder.update(
        conn=ctx.conn, other=other_ctx.scheduler(), req={batch: pipeline._request_for(loaded)}
    )
    ctx.scheduler().step({batch: pipeline._request_for(loaded)})
    assert calls == [f"{batch}-v0", f"{batch}-v1"]  # nothing resubmitted
    assert ctx.conn.execute("SELECT used FROM quota_usage").fetchone()[0] == 2


def test_f7_the_batch_lease_is_renewed_and_an_expired_lease_stops_the_driver(
    make_context: Callable[..., Context],
    write_brief: Callable[..., Path],
    studio_root: Path,
    clock: FakeClock,
) -> None:
    from qontinui_studio import pipeline
    from qontinui_studio.errors import LeaseLost

    ctx = make_context()
    batch = new_batch(ctx, load_brief(write_brief(), root=studio_root))
    lease = pipeline._Lease(ctx, batch)
    assert lease.acquire()
    clock.sleep(1800)
    lease.renew()
    until = ctx.conn.execute("SELECT driver_until FROM batches").fetchone()[0]
    assert until == (clock.now + pipeline.timedelta(seconds=pipeline.BATCH_LEASE_S)).isoformat()
    clock.sleep(pipeline.BATCH_LEASE_S + 1)
    assert not lease.set_status("generated")  # an expired lease may not write
    with pytest.raises(LeaseLost):
        lease.renew()


def test_f8_printed_commands_are_shell_quoted(studio_root: Path, tmp_path: Path) -> None:
    root = tmp_path / "Joshua Spinak" / "studio"
    cmd = unpublish_command("b-1", root)
    assert shlex.split(cmd) == [
        "studio",
        "--root",
        str(root.resolve()),
        "unpublish",
        "--batch",
        "b-1",
    ]


def test_f9_d7_is_bound_to_the_preview_and_broken_images_refuse(
    make_context: Callable[..., Context],
    write_brief: Callable[..., Path],
    studio_root: Path,
    clock: FakeClock,
    preview: FakePreview,
) -> None:
    assert run_brief(make_context(), write_brief(name="a.yaml")).status == "published"
    clock.sleep(5)
    ctx = make_context()
    real_fetch = ctx.fetch

    def fetch(url: str) -> bytes:
        for f in (studio_root.parent / "previews").rglob("hero.*"):
            f.write_bytes(b"not an image")
        return real_fetch(url)

    ctx.fetch = fetch
    out = run_brief(ctx, write_brief(name="b.yaml"))
    assert out.status == "refused"
    assert out.publish is not None
    assert {g.gate for g in out.publish.failures} == {"d7_images"}


def test_f9_a_bridge_that_does_not_move_to_the_preview_refuses(
    make_context: Callable[..., Context], write_brief: Callable[..., Path], preview: FakePreview
) -> None:
    preview.ignore_navigate = True  # a stale tab that never loads the batch
    preview.stale_url = {
        "cafe": "http://preview.test/cafe/index.html",
        "barber": "http://127.0.0.1:9876/",
    }
    out = run_brief(make_context(), write_brief())
    assert out.status == "refused"
    assert out.publish is not None
    assert {g.gate for g in out.publish.failures} == {"d7_navigate"}
    assert all("studio_batch=" in u for u in preview.navigated)


def test_f10_post_commit_cleanup_failure_is_still_a_publication(
    make_context: Callable[..., Context],
    write_brief: Callable[..., Path],
    studio_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fails(self: Journal) -> list[str]:
        raise PermissionError(13, "in use by another process")

    monkeypatch.setattr(Journal, "roll_forward", fails)
    ctx = make_context()
    out = run_brief(ctx, write_brief())
    assert out.status == "published" and out.publish is not None and out.publish.published
    assert notice_text(ctx.workspace.notices_dir, out.batch_id, "published")
    assert not list(ctx.workspace.notices_dir.glob("*-refused-*.md"))
    monkeypatch.undo()
    resume_all(make_context())  # recovery finishes the cleanup
    assert not debris(studio_root / "published" / "cafe")


def test_f11_unpublish_refuses_when_a_live_sites_config_is_missing(
    make_context: Callable[..., Context], write_brief: Callable[..., Path], studio_root: Path
) -> None:
    ctx = make_context()
    out = run_brief(ctx, write_brief())
    del ctx.sites["cafe-template"]
    with pytest.raises(PublicationRefused, match="config is not loaded"):
        unpublish_batch(ctx.conn, ctx.sites, out.batch_id, ctx.notifier, ctx.clock)
    assert ctx.conn.execute("SELECT status FROM batches").fetchone()[0] == "published"
    assert (studio_root / "published" / "cafe" / "hero.webp").exists()
