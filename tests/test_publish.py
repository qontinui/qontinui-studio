"""Publication transactions: D7 on a preview before any live change, the
journalled swap and its crash recovery, per-site locking, restore-on-unpublish,
completeness and status checks."""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from qontinui_studio import publish as publish_mod
from qontinui_studio.errors import ConfigError, PublicationRefused
from qontinui_studio.locks import site_locks
from qontinui_studio.pipeline import Context, run_brief
from qontinui_studio.publish import JOURNAL_PREFIX, publish_batch, recover, unpublish_batch
from qontinui_studio.sites import load_sites

from .conftest import FakeBridge, FakeClock, FakePreview, debris, notice_text


def _edit_site(studio_root: Path, name: str, old: str, new: str) -> None:
    p = studio_root / "sites" / name
    text = p.read_text()
    assert old in text, old
    p.write_text(text.replace(old, new))


CAFE_PREVIEW = (
    "ui_bridge_url: http://preview.test/cafe/__ui-bridge\n"
    "preview_url: http://preview.test/cafe/index.html\n"
    "preview_dir: ../../previews/cafe\n"
    "preview_manifest_url: http://preview.test/cafe/studio-manifest.json\n"
)


def _bridges(preview: FakePreview, page_health: Callable[[FakeBridge], dict[str, object]]):  # type: ignore[no-untyped-def]
    """The fake preview's bridges, with page health answered by ``page_health``."""

    def factory(base_url: str) -> FakeBridge:
        bridge = preview.bridge(base_url)
        bridge.page_health = lambda: page_health(bridge)  # type: ignore[method-assign]
        return bridge

    return factory


def _ok(bridge: FakeBridge) -> dict[str, object]:
    return {"success": True, "data": {"summary": "OK"}}


def _critical(bridge: FakeBridge) -> dict[str, object]:
    return {"success": True, "data": {"summary": "CRITICAL", "findings": []}}


def _live(studio_root: Path, site: str = "cafe") -> tuple[bytes, str]:
    d = studio_root / "published" / site
    manifest = json.loads((d / "studio-manifest.json").read_text())
    return (d / "hero.webp").read_bytes(), manifest["slots"]["hero"]["batch_id"]


def _active_batches(ctx: Context, site: str) -> set[str]:
    return {
        r["batch_id"]
        for r in ctx.conn.execute(
            "SELECT batch_id FROM publications WHERE site = ? AND unpublished_at IS NULL", (site,)
        )
    }


def _status(ctx: Context, batch_id: str) -> str:
    return str(
        ctx.conn.execute("SELECT status FROM batches WHERE id = ?", (batch_id,)).fetchone()[0]
    )


def test_missing_page_health_refuses_with_no_waiver(
    make_context: Callable[..., Context], write_brief: Callable[..., Path], studio_root: Path
) -> None:
    _edit_site(studio_root, "cafe.yaml", CAFE_PREVIEW, "")
    ctx = make_context()
    out = run_brief(ctx, write_brief())
    assert out.status == "refused"
    assert out.publish is not None
    [g] = [g for g in out.publish.failures if g.gate == "d7_page_health"]
    assert g.outcome == "not_configured"
    assert not (studio_root / "published" / "cafe" / "hero.webp").exists()
    assert not (studio_root / "published" / "barber" / "hero.webp").exists()


def test_there_is_no_waiver_field(studio_root: Path) -> None:
    _edit_site(studio_root, "cafe.yaml", CAFE_PREVIEW, "page_health: waived\n")
    with pytest.raises(ConfigError, match="page_health"):
        load_sites(studio_root / "sites")


def test_page_health_needs_a_preview(studio_root: Path) -> None:
    _edit_site(
        studio_root,
        "cafe.yaml",
        CAFE_PREVIEW,
        "ui_bridge_url: http://preview.test/cafe/__ui-bridge\n",
    )
    with pytest.raises(ConfigError, match="needs all of"):
        load_sites(studio_root / "sites")


@pytest.mark.parametrize(
    "preview_dir",
    [
        "..",  # the studio root itself
        "../..",  # a parent of the studio root
        "../somewhere",  # inside the studio root
        "../published/cafe",  # the publish dir
        "../published",  # a parent of the publish dir
        "../published/cafe/sub",  # inside the publish dir
    ],
)
def test_preview_dir_path_limits(studio_root: Path, preview_dir: str) -> None:
    _edit_site(studio_root, "cafe.yaml", "../../previews/cafe", preview_dir)
    with pytest.raises(ConfigError, match="preview_dir"):
        load_sites(studio_root / "sites")


def test_preview_only_deletes_files_it_wrote(
    make_context: Callable[..., Context],
    write_brief: Callable[..., Path],
    studio_root: Path,
    clock: FakeClock,
) -> None:
    previews = studio_root.parent / "previews" / "cafe"
    previews.mkdir(parents=True)
    (previews / "index.html").write_text("the preview site's own page")
    run_brief(make_context(), write_brief())
    clock.sleep(60)
    run_brief(make_context(), write_brief(name="hero2.yaml"))
    assert (previews / "index.html").read_text() == "the preview site's own page"


def test_d7_runs_on_the_preview_before_anything_goes_live(
    make_context: Callable[..., Context],
    write_brief: Callable[..., Path],
    studio_root: Path,
    preview: FakePreview,
) -> None:
    seen: dict[str, object] = {}

    def page_health(bridge: FakeBridge) -> dict[str, object]:
        # At probe time the bridged page is this batch's preview, and live is untouched.
        if bridge.site != "cafe":
            return _ok(bridge)
        seen["url"] = bridge.url
        seen["preview"] = json.loads(
            (studio_root.parent / "previews" / "cafe" / "studio-manifest.json").read_text()
        )
        seen["live_exists"] = (studio_root / "published" / "cafe" / "hero.webp").exists()
        return _ok(bridge)

    ctx = make_context(bridge=_bridges(preview, page_health))
    out = run_brief(ctx, write_brief())
    assert out.status == "published", out.publish and out.publish.failures
    assert seen["live_exists"] is False
    assert str(seen["url"]).startswith(
        f"http://preview.test/cafe/index.html?studio_batch={out.batch_id}&studio_attempt="
    )
    shown = seen["preview"]
    assert isinstance(shown, dict) and shown["slots"]["hero"]["batch_id"] == out.batch_id
    assert out.publish is not None
    assert {g.gate for g in out.publish.gate_results if g.outcome == "pass"} >= {
        "d7_preview",
        "d7_navigate",
        "d7_page_health",
        "d7_images",
    }


def test_failed_page_health_leaves_live_content_untouched(
    make_context: Callable[..., Context],
    write_brief: Callable[..., Path],
    studio_root: Path,
    clock: FakeClock,
    preview: FakePreview,
) -> None:
    first = run_brief(make_context(), write_brief())
    assert first.status == "published"
    before = _live(studio_root)
    clock.sleep(60)
    ctx = make_context(bridge=_bridges(preview, _critical))
    second = run_brief(ctx, write_brief(name="hero2.yaml"))
    assert second.status == "refused"
    assert _status(ctx, second.batch_id) == "refused"
    assert _live(studio_root) == before
    assert _live(studio_root, "barber")[1] == first.batch_id
    assert _active_batches(ctx, "cafe-template") == {first.batch_id}
    assert not debris(studio_root / "published" / "cafe")
    assert "nothing went live" in notice_text(ctx.workspace.notices_dir, second.batch_id, "refused")


@pytest.mark.parametrize(
    "probe_error", [httpx.InvalidURL("bad"), RuntimeError("boom"), ValueError("x")]
)
def test_any_probe_exception_refuses_and_never_publishes(
    make_context: Callable[..., Context],
    write_brief: Callable[..., Path],
    studio_root: Path,
    probe_error: Exception,
    preview: FakePreview,
) -> None:
    def page_health(bridge: FakeBridge) -> dict[str, object]:
        raise probe_error

    ctx = make_context(bridge=_bridges(preview, page_health))
    out = run_brief(ctx, write_brief())
    assert out.status == "refused"
    assert _status(ctx, out.batch_id) == "refused"
    assert not (studio_root / "published" / "cafe" / "hero.webp").exists()


def test_preview_serving_a_different_manifest_refuses(
    make_context: Callable[..., Context], write_brief: Callable[..., Path], studio_root: Path
) -> None:
    ctx = make_context(fetch=lambda url: b'{"slots": {}}')
    out = run_brief(ctx, write_brief())
    assert out.status == "refused"
    assert out.publish is not None
    assert {g.gate for g in out.publish.failures} == {"d7_preview"}


def test_unpublishing_the_newest_batch_restores_the_previous_one(
    make_context: Callable[..., Context],
    write_brief: Callable[..., Path],
    studio_root: Path,
    clock: FakeClock,
) -> None:
    first = run_brief(make_context(), write_brief())
    before = _live(studio_root)
    clock.sleep(60)
    ctx = make_context()
    second = run_brief(ctx, write_brief(name="hero2.yaml"))
    assert second.status == "published"
    assert _live(studio_root)[1] == second.batch_id
    unpublish_batch(
        ctx.conn,
        ctx.sites,
        second.batch_id,
        ctx.notifier,
        ctx.clock,
        bridge=ctx.bridge,
        fetch=ctx.fetch,
    )
    assert _live(studio_root) == before
    assert _active_batches(ctx, "cafe-template") == {first.batch_id}
    assert "Restored cafe-template/hero" in notice_text(
        ctx.workspace.notices_dir, second.batch_id, "unpublished"
    )
    unpublish_batch(
        ctx.conn,
        ctx.sites,
        first.batch_id,
        ctx.notifier,
        ctx.clock,
        bridge=ctx.bridge,
        fetch=ctx.fetch,
    )
    assert not (studio_root / "published" / "cafe" / "hero.webp").exists()


def test_unpublish_restore_refuses_unknown_bytes_and_tampered_variants(
    make_context: Callable[..., Context],
    write_brief: Callable[..., Path],
    studio_root: Path,
    clock: FakeClock,
) -> None:
    first = run_brief(make_context(), write_brief())
    clock.sleep(60)
    ctx = make_context()
    second = run_brief(ctx, write_brief(name="hero2.yaml"))
    # Someone replaced the live cafe webp by hand.
    (studio_root / "published" / "cafe" / "hero.webp").write_bytes(b"hand-made")
    # And the first batch's stored barber variant was altered.
    [vpath] = [
        r["path"]
        for r in ctx.conn.execute(
            "SELECT v.path FROM publications p JOIN variants v ON v.id = p.variant_id "
            "WHERE p.batch_id = ? AND p.site = 'barber-template' AND v.format = 'webp'",
            (first.batch_id,),
        )
    ]
    Path(vpath).write_bytes(b"tampered")
    unpublish_batch(
        ctx.conn,
        ctx.sites,
        second.batch_id,
        ctx.notifier,
        ctx.clock,
        bridge=ctx.bridge,
        fetch=ctx.fetch,
    )
    assert (studio_root / "published" / "cafe" / "hero.webp").read_bytes() == b"hand-made"
    text = notice_text(ctx.workspace.notices_dir, second.batch_id, "unpublished")
    assert "NOT restored cafe-template/hero" in text and "did not place" in text
    assert "NOT restored barber-template/hero" in text and "missing or altered" in text


def test_unpublish_refuses_a_batch_that_is_not_live(
    make_context: Callable[..., Context], write_brief: Callable[..., Path]
) -> None:
    ctx = make_context()
    out = run_brief(ctx, write_brief(), publish=False)
    with pytest.raises(PublicationRefused, match="nothing to unpublish"):
        unpublish_batch(
            ctx.conn,
            ctx.sites,
            out.batch_id,
            ctx.notifier,
            ctx.clock,
            bridge=ctx.bridge,
            fetch=ctx.fetch,
        )
    assert _status(ctx, out.batch_id) == "generated"


class Crash(BaseException):
    """Stands in for the process dying: nothing after it runs."""


def test_crash_mid_swap_is_replayed_by_the_next_run(
    make_context: Callable[..., Context],
    write_brief: Callable[..., Path],
    studio_root: Path,
    clock: FakeClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = run_brief(make_context(), write_brief())
    before = _live(studio_root)
    clock.sleep(60)

    # Die right after the live files moved, before the manifest and the DB
    # commit — and, as a real crash would, without running any cleanup.
    real_write = publish_mod.atomic_write

    def dying_write(path: Path, data: bytes) -> None:
        if path.name == "studio-manifest.json" and "published" in path.parts:
            raise Crash()
        real_write(path, data)

    monkeypatch.setattr(publish_mod, "atomic_write", dying_write)
    monkeypatch.setattr(publish_mod.Journal, "roll_back", lambda self: [])
    ctx = make_context()
    with pytest.raises(Crash):
        run_brief(ctx, write_brief(name="hero2.yaml"))
    monkeypatch.undo()

    cafe = studio_root / "published" / "cafe"
    assert list(cafe.glob(f"{JOURNAL_PREFIX}*.json")), "the crash left its journal"
    report = recover(ctx.conn, ctx.sites, ctx.notifier, root=studio_root)
    assert any("rolled back interrupted publish" in r for r in report.lines)
    assert not report.failed_sites
    # The operator hears about the rollback.
    assert list(ctx.workspace.notices_dir.glob("*-publish-rolled-back-*.md"))
    assert _live(studio_root) == before
    assert _active_batches(ctx, "cafe-template") == {first.batch_id}
    assert not debris(cafe)


def test_rollback_never_removes_bytes_it_did_not_place(tmp_path: Path) -> None:
    d = tmp_path / "site"
    d.mkdir()
    dest = d / "hero.webp"
    backup = d / ".hero.webp.prev-x"
    backup.write_bytes(b"old")
    dest.write_bytes(b"someone else's")
    journal = publish_mod.Journal(
        batch_id="b",
        site="s",
        publish_dir=d,
        placed=[(d / ".hero.webp.staged-x", dest, "0" * 64)],
        moved=[publish_mod.Moved(dest, backup, "1" * 64, True)],
        manifest_before=None,
        manifest_after=b"{}",
    )
    journal.write()
    problems = journal.roll_back()
    assert dest.read_bytes() == b"someone else's"
    assert backup.read_bytes() == b"old"
    assert problems and "unknown bytes" in problems[0]


def test_site_lock_is_exclusive(tmp_path: Path) -> None:
    acquired = threading.Event()

    def second() -> None:
        with site_locks([tmp_path]):
            acquired.set()

    with site_locks([tmp_path]):
        t = threading.Thread(target=second)
        t.start()
        time.sleep(0.2)
        assert not acquired.is_set()
    t.join(timeout=5)
    assert acquired.is_set()


def test_io_failure_mid_publish_restores_everything(
    make_context: Callable[..., Context],
    write_brief: Callable[..., Path],
    studio_root: Path,
    clock: FakeClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = run_brief(make_context(), write_brief())
    before = _live(studio_root)
    clock.sleep(60)
    calls = {"n": 0}
    real = publish_mod.atomic_write

    def flaky(path: Path, data: bytes) -> None:
        if path.name == "studio-manifest.json":
            calls["n"] += 1
            if calls["n"] == 2:  # second site's manifest: first site already swapped
                raise OSError("disk full")
        real(path, data)

    monkeypatch.setattr(publish_mod, "atomic_write", flaky)
    ctx = make_context()
    second = run_brief(ctx, write_brief(name="hero2.yaml"))
    assert second.status == "refused"
    assert second.publish is not None
    assert any(g.gate == "publish_io" for g in second.publish.failures)
    assert _live(studio_root) == before
    assert _active_batches(ctx, "cafe-template") == {first.batch_id}
    assert not debris(studio_root / "published" / "cafe")


def test_incomplete_batch_is_refused(
    make_context: Callable[..., Context], write_brief: Callable[..., Path]
) -> None:
    ctx = make_context()
    out = run_brief(ctx, write_brief(), publish=False)
    assert out.status == "generated"
    ctx.conn.execute("DELETE FROM variants WHERE site = 'barber-template'")
    ctx.conn.commit()
    result = publish_batch(
        ctx.conn, ctx.sites, out.batch_id, ctx.notifier, ctx.clock, ctx.bridge, ctx.fetch
    )
    assert not result.published
    assert [g.subject for g in result.failures if g.gate == "completeness"] == [
        "barber-template/hero"
    ]


def test_publish_refuses_unknown_and_unpublishable_batches(
    make_context: Callable[..., Context], write_brief: Callable[..., Path]
) -> None:
    ctx = make_context()
    with pytest.raises(ConfigError, match="no batch"):
        publish_batch(ctx.conn, ctx.sites, "nope", ctx.notifier, ctx.clock)
    out = run_brief(ctx, write_brief())
    with pytest.raises(PublicationRefused, match="is 'published'"):
        publish_batch(ctx.conn, ctx.sites, out.batch_id, ctx.notifier, ctx.clock)
