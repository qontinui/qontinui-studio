"""Publication transactions: D7 refusal/waiver, rollback that preserves the
previously live content, restore-on-unpublish, completeness, status checks."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest

from qontinui_studio import publish as publish_mod
from qontinui_studio.errors import ConfigError, PublicationRefused
from qontinui_studio.pipeline import Context, run_brief
from qontinui_studio.publish import publish_batch, unpublish_batch
from qontinui_studio.sites import load_sites

from .conftest import FakeClock, notice_text


def _edit_site(studio_root: Path, name: str, old: str, new: str) -> None:
    p = studio_root / "sites" / name
    p.write_text(p.read_text().replace(old, new))


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


def test_missing_page_health_refuses_unless_waived(
    make_context: Callable[..., Context], write_brief: Callable[..., Path], studio_root: Path
) -> None:
    _edit_site(studio_root, "cafe.yaml", "page_health: waived\n", "")
    ctx = make_context()
    out = run_brief(ctx, write_brief())
    assert out.status == "refused"
    assert out.publish is not None
    [g] = [g for g in out.publish.failures if g.gate == "d7_page_health"]
    assert g.outcome == "not_configured"
    assert not (studio_root / "published" / "cafe" / "hero.webp").exists()
    assert not (studio_root / "published" / "barber" / "hero.webp").exists()


def test_waiver_only_on_demo_sites(studio_root: Path) -> None:
    _edit_site(studio_root, "cafe.yaml", "kind: demo", "kind: product")
    with pytest.raises(ConfigError, match="waived is allowed only on a demo site"):
        load_sites(studio_root / "sites")


def test_failed_page_health_restores_previous_live_content(
    make_context: Callable[..., Context],
    write_brief: Callable[..., Path],
    studio_root: Path,
    clock: FakeClock,
) -> None:
    first = run_brief(make_context(), write_brief())
    assert first.status == "published"
    before = _live(studio_root)

    _edit_site(
        studio_root,
        "cafe.yaml",
        "page_health: waived\n",
        "page_health_url: http://127.0.0.1:9/ui-bridge/control/page-health\n",
    )

    def critical(url: str) -> dict[str, object]:
        return {"success": True, "data": {"summary": "CRITICAL", "findings": []}}

    clock.sleep(60)
    ctx = make_context(probe=critical)
    second = run_brief(ctx, write_brief(providers="fake, fake2", name="hero2.yaml"))
    assert second.status == "refused"
    # The first batch is live again on BOTH sites: bytes, manifest and rows.
    assert _live(studio_root) == before
    assert _live(studio_root, "barber")[1] == first.batch_id
    assert _active_batches(ctx, "cafe-template") == {first.batch_id}
    assert not [p for p in (studio_root / "published" / "cafe").iterdir() if p.name.startswith(".")]
    assert "previous content restored" in notice_text(
        ctx.workspace.notices_dir, second.batch_id, "refused"
    )


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
    unpublish_batch(ctx.conn, ctx.sites, second.batch_id, ctx.notifier, ctx.clock)
    assert _live(studio_root) == before
    assert _active_batches(ctx, "cafe-template") == {first.batch_id}
    assert "Restored cafe-template/hero" in notice_text(
        ctx.workspace.notices_dir, second.batch_id, "unpublished"
    )
    # Unpublishing the restored one then leaves the slot empty.
    unpublish_batch(ctx.conn, ctx.sites, first.batch_id, ctx.notifier, ctx.clock)
    assert not (studio_root / "published" / "cafe" / "hero.webp").exists()


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
    real = publish_mod._write_manifest

    def flaky_manifest(publish_dir: Path, data: dict[str, object]) -> None:
        calls["n"] += 1
        if calls["n"] == 2:  # second site's manifest: first site already swapped
            raise OSError("disk full")
        real(publish_dir, data)

    monkeypatch.setattr(publish_mod, "_write_manifest", flaky_manifest)
    ctx = make_context()
    second = run_brief(ctx, write_brief(name="hero2.yaml"))
    assert second.status == "refused"
    assert second.publish is not None
    assert any(g.gate == "publish_io" for g in second.publish.failures)
    assert _live(studio_root) == before
    assert _active_batches(ctx, "cafe-template") == {first.batch_id}


def test_incomplete_batch_is_refused(
    make_context: Callable[..., Context], write_brief: Callable[..., Path]
) -> None:
    ctx = make_context()
    out = run_brief(ctx, write_brief(), publish=False)
    assert out.status == "generated"
    ctx.conn.execute("DELETE FROM variants WHERE site = 'barber-template'")
    ctx.conn.commit()
    result = publish_batch(ctx.conn, ctx.sites, out.batch_id, ctx.notifier, ctx.clock, ctx.probe)
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
