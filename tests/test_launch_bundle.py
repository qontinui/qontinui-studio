"""Bundle assembly and the launch gates (no browser needed: capture is off)."""

from __future__ import annotations

import json
import os
import re
import shutil
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from PIL import Image

from qontinui_studio.errors import ConfigError, JournalError
from qontinui_studio.launch.bundle import FOOTER_TEXT, img_media_id
from qontinui_studio.launch.kit import KitOptions, run_kit
from qontinui_studio.launch.posts import NETWORKS
from qontinui_studio.launch.product import load_product
from qontinui_studio.launch.publish import (
    LaunchNotPublished,
    publish_launch,
    recover_all_launches,
    recover_launches,
    unpublish_launch,
)
from qontinui_studio.launch.record import ALLOWED_PATHS, NoBuildRecord
from qontinui_studio.library import Library
from qontinui_studio.pipeline import Context

from .conftest import notice_text
from .launch_support import (
    LaunchBridge,
    document,
    launch_preview,
    preview_ctx,
    preview_root,
    public_client,
    use_public_door,
    write_launch_decor,
    write_product,
)

RECORD_URL = "https://build.example.test/qontinui-studio"


@pytest.fixture(autouse=True)
def _public_door(monkeypatch: pytest.MonkeyPatch) -> None:
    """Publication re-reads the factory's doors; here they are in-process fakes."""
    use_public_door(monkeypatch)


@pytest.fixture
def record_file(tmp_path: Path) -> Path:
    path = tmp_path / "record.json"
    path.write_text(json.dumps(document()))
    return path


def _sources(html: str) -> set[str]:
    out: set[str] = set()
    for value in re.findall(r"data-source='([^']*)'", html):
        out.update(value.split())
    return out


def test_how_it_was_built_renders_only_from_the_record(
    make_context: Callable[..., Context], studio_root: Path, record_file: Path
) -> None:
    ctx = make_context(**preview_ctx(studio_root))
    product_path = write_product(studio_root, capture=False)
    run = run_kit(ctx, product_path, KitOptions(video=False), public_client())
    out = run.bundle.out_dir
    page = (out / "how-it-was-built.html").read_text()

    # Every value names its document field, and every field named is allowlisted.
    sources = _sources(page)
    assert sources and sources <= ALLOWED_PATHS, sources - ALLOWED_PATHS
    # Unknowns: "unknown — <reason>", never 0.
    assert "unknown — the session census is still provisional (census_provisional)" in page
    assert "unknown — 1 of 2 PR(s) lack it: the factory could not establish it" in page
    assert re.search(r"data-fact='sessions'[^>]*><div class='value unknown'>unknown", page)
    assert "data-fact='prs_landed' data-source='prs[].landed_at'><div class='value'>2<" in page
    # The record's own unknowns list: every path, with its code in words.
    for u in document()["unknowns"]:
        where, _, code = u.partition(": ")
        assert f"<code>{where}</code>: " in page and f"({code})" in page
    # Footer links the public record on every page.
    anchor = f'<a class="factory" href="{RECORD_URL}">{FOOTER_TEXT}</a>'
    assert anchor in page and anchor in (out / "index.html").read_text()

    # OG/social images at their sizes.
    for name, size in {"og": (1200, 630), "x": (1200, 675), "square": (1080, 1080)}.items():
        with Image.open(out / "social" / f"{name}.png") as img:
            assert img.size == size
    index = (out / "index.html").read_text()
    assert "og:image" in index and "social/og.png" in index

    # Posts: factual, within limits, link the record, never state an unknown.
    for network in NETWORKS:
        text = (out / "posts" / f"{network.name}.txt").read_text().strip()
        assert len(text) <= network.limit
        assert RECORD_URL in text
        assert "unknown" not in text.lower()
    assert "Pull requests landed: 2" in (out / "posts" / "linkedin.txt").read_text()
    x_post = (out / "posts" / "x.txt").read_text()
    assert "Plans shipped: 1" in x_post and "Build record: " in x_post

    manifest = json.loads((out / "launch-manifest.json").read_text())
    assert manifest["record"]["present"] is True
    # Notes are operator-only: in the launch row, never in the published manifest.
    assert "notes" not in manifest
    assert "film not rendered: disabled for this run" in run.bundle.notes
    assert any("no capture configured" in n for n in run.bundle.notes)
    row = ctx.conn.execute(
        "SELECT operator_notes FROM launches WHERE id = ?", (run.launch_id,)
    ).fetchone()
    assert json.loads(row["operator_notes"]) == run.bundle.notes


def test_no_record_assembles_without_numbers_and_refuses_publication(
    make_context: Callable[..., Context], studio_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = make_context(**preview_ctx(studio_root))
    product_path = write_product(studio_root, capture=False)

    def absent(*_: object) -> NoBuildRecord:
        return NoBuildRecord("not_found", "https://api.example.test/x", "HTTP 404")

    monkeypatch.setattr("qontinui_studio.launch.kit.fetch_record", absent)
    run = run_kit(ctx, product_path, KitOptions(), publish=True)
    page = (run.bundle.out_dir / "how-it-was-built.html").read_text()
    assert "data-record='absent'" in page and "not_found" in page
    assert "data-fact=" not in page
    assert not run.bundle.has_record
    assert run.publish is not None and not run.publish.published
    gates = {g.gate: g for g in run.publish.gate_results}
    assert gates["d1_record"].failed and "decoration never stands in" in gates["d1_record"].detail
    # Only the lock the refused publish held; nothing went live.
    assert not (studio_root / "published-launch" / "index.html").exists()


def test_publish_gates_unpublish_and_tamper(
    make_context: Callable[..., Context], studio_root: Path, record_file: Path
) -> None:
    ctx = make_context(**preview_ctx(studio_root))
    product_path = write_product(studio_root, capture=False)
    product = load_product(product_path)
    run = run_kit(ctx, product_path, KitOptions(video=False), public_client(), publish=True)
    assert run.publish is not None and run.publish.published, run.publish.failures
    gates = {g.gate: g.outcome for g in run.publish.gate_results}
    assert gates == {
        "d1_record": "pass",
        "d3_allowlist": "pass",
        "integrity": "pass",
        "d6": "pass",
        "footer": "pass",
        "already_live": "pass",
        "foreign_file": "pass",
        "d1_current": "pass",
        "d1_tenant": "pass",
        "public_text": "pass",
        "d7_preview": "pass",
        "d7_navigate": "pass",
        "d7_images": "pass",
        "d7_page_health": "pass",
    }
    published = studio_root / "published-launch"
    assert (published / "index.html").is_file() and (published / "build-record.json").is_file()
    notice = notice_text(ctx.workspace.notices_dir, run.launch_id, "published")
    root = studio_root.resolve()
    assert f"studio --root {root} launch unpublish --launch {run.launch_id}" in notice
    assert "d7_page_health: pass" in notice

    removed = unpublish_launch(ctx.conn, run.launch_id, ctx.notifier, ctx.clock)
    assert removed and not (published / "index.html").exists()
    # Only the studio's own lock file remains.
    assert [p.name for p in published.rglob("*")] == [".studio.lock"]

    # A bundle edited after assembly no longer passes.
    out = run.bundle.out_dir
    (out / "how-it-was-built.html").write_text("<p>42 PRs</p>")
    again = publish_launch(
        ctx.conn, run.launch_id, product, ctx.notifier, ctx.clock, ctx.bridge, ctx.fetch
    )
    failed = {g.gate for g in again.failures}
    assert {"integrity", "footer"} <= failed and not again.published

    # A build-record.json with an extra key fails D3.
    run2 = run_kit(ctx, product_path, KitOptions(video=False), public_client())
    wrapped = json.loads((run2.bundle.out_dir / "build-record.json").read_text())
    wrapped["document"]["operator_email"] = "x@y.z"
    (run2.bundle.out_dir / "build-record.json").write_text(json.dumps(wrapped))
    third = publish_launch(
        ctx.conn, run2.launch_id, product, ctx.notifier, ctx.clock, ctx.bridge, ctx.fetch
    )
    d3 = next(g for g in third.gate_results if g.gate == "d3_allowlist")
    assert d3.failed and "<unlisted key>" in d3.detail


def test_page_health_failure_rolls_back(
    make_context: Callable[..., Context], studio_root: Path, record_file: Path
) -> None:
    ctx = make_context(**preview_ctx(studio_root, "CRITICAL"))
    product_path = write_product(studio_root, capture=False)
    run = run_kit(ctx, product_path, KitOptions(video=False), public_client(), publish=True)
    assert run.publish is not None and not run.publish.published
    assert not (studio_root / "published-launch" / "index.html").exists()
    row = ctx.conn.execute("SELECT status FROM launches WHERE id = ?", (run.launch_id,)).fetchone()
    assert row["status"] == "refused"


def test_decoration_comes_through_the_phase2_gates(
    make_context: Callable[..., Context], studio_root: Path, record_file: Path
) -> None:
    write_launch_decor(studio_root)
    ctx = make_context(**preview_ctx(studio_root))
    product_path = write_product(studio_root, capture=False, brief=True)
    run = run_kit(ctx, product_path, KitOptions(video=False), public_client(), publish=True)
    images = {i["path"]: i for i in run.bundle.manifest["images"]}
    hero = images["assets/hero.png"]
    assert hero["ai_generated"] is True and hero["label"] == "AI-generated image"
    assert hero["provider"] == "fake" and hero["asset_uid"]
    index = (run.bundle.out_dir / "index.html").read_text()
    assert f"data-ai-generated='true'><img src='assets/hero.png?sha={hero['sha256']}'" in index
    assert "<figcaption>AI-generated image</figcaption>" in index
    assert run.publish is not None and run.publish.published, run.publish.failures
    d5 = [g for g in run.publish.gate_results if g.gate.startswith("d5")]
    assert len(d5) == 4 and all(g.outcome == "pass" for g in d5)


def test_non_commercial_decor_refused_for_commercial_launch(
    make_context: Callable[..., Context], studio_root: Path, record_file: Path
) -> None:
    write_launch_decor(studio_root)
    brief = studio_root / "briefs" / "launch.yaml"
    brief.write_text(brief.read_text().replace("[fake]", "[fake2]"))
    ctx = make_context(**preview_ctx(studio_root))
    product_path = write_product(studio_root, capture=False, brief=True, commercial=True)
    run = run_kit(ctx, product_path, KitOptions(video=False), public_client(), publish=True)
    assert run.publish is not None and not run.publish.published
    lic = [g for g in run.publish.failures if g.gate == "d5_licence"]
    assert lic and "launch 'qontinui-studio' is commercial" in lic[0].detail


def test_ai_image_passed_off_as_a_screenshot_fails_d6(
    make_context: Callable[..., Context], studio_root: Path, record_file: Path
) -> None:
    write_launch_decor(studio_root)
    ctx = make_context(**preview_ctx(studio_root))
    product_path = write_product(studio_root, capture=False, brief=True)
    run = run_kit(ctx, product_path, KitOptions(video=False), public_client())
    out = run.bundle.out_dir
    index = out / "index.html"
    forged = index.read_text().replace(
        "<figure class='decorative' data-ai-generated='true'>",
        "<figure class='screenshot' data-ai-generated='false'>",
    )
    index.write_text(forged)
    manifest_path = out / "launch-manifest.json"
    manifest = json.loads(manifest_path.read_text())
    from qontinui_studio.postprocess import sha256_file

    manifest["files"]["index.html"] = sha256_file(index)
    manifest_path.write_text(json.dumps(manifest))
    result = publish_launch(
        ctx.conn,
        run.launch_id,
        load_product(product_path),
        ctx.notifier,
        ctx.clock,
        ctx.bridge,
        ctx.fetch,
    )
    d6 = next(g for g in result.gate_results if g.gate == "d6")
    assert d6.failed and "shown as a screenshot but is decorative" in d6.detail


def test_no_page_health_url_refuses_before_copying(
    make_context: Callable[..., Context], studio_root: Path, record_file: Path
) -> None:
    """A launch is a product, never a demo: page health cannot be waived."""
    ctx = make_context(**preview_ctx(studio_root))
    product_path = write_product(studio_root, capture=False, preview=False)
    run = run_kit(ctx, product_path, KitOptions(video=False), public_client(), publish=True)
    assert run.publish is not None and not run.publish.published
    d7 = next(g for g in run.publish.gate_results if g.gate == "d7_page_health")
    assert d7.outcome == "not_configured" and d7.failed
    # Only the lock the refused publish held; nothing went live.
    assert not (studio_root / "published-launch" / "index.html").exists()


def test_republish_supersedes_and_a_failed_swap_restores_the_previous(
    make_context: Callable[..., Context], studio_root: Path, record_file: Path
) -> None:
    ctx = make_context(**preview_ctx(studio_root))
    product_path = write_product(studio_root, capture=False)
    product = load_product(product_path)
    first = run_kit(ctx, product_path, KitOptions(video=False), public_client(), publish=True)
    assert first.publish is not None and first.publish.published
    published = studio_root / "published-launch"
    first_manifest = (published / "launch-manifest.json").read_text()

    # A second launch whose page-health fails: the first stays live, untouched.
    critical = launch_preview(studio_root, "CRITICAL").bridge
    ctx.clock.sleep(1)
    second = run_kit(ctx, product_path, KitOptions(video=False), public_client())
    failed = publish_launch(
        ctx.conn, second.launch_id, product, ctx.notifier, ctx.clock, critical, ctx.fetch
    )
    assert not failed.published
    assert (published / "launch-manifest.json").read_text() == first_manifest
    leftovers = [p.name for p in published.rglob(".*") if p.name != ".studio.lock"]
    assert leftovers == []  # no staged file, backup or journal left behind

    # A healthy republish supersedes the first.
    ok = publish_launch(
        ctx.conn, second.launch_id, product, ctx.notifier, ctx.clock, ctx.bridge, ctx.fetch
    )
    assert ok.published
    assert second.launch_id in (published / "launch-manifest.json").read_text()
    row = ctx.conn.execute(
        "SELECT status, superseded_by FROM launches WHERE id = ?", (first.launch_id,)
    ).fetchone()
    assert (row["status"], row["superseded_by"]) == ("unpublished", second.launch_id)
    # Unpublishing the superseded launch must not touch its successor's files.
    with pytest.raises(LaunchNotPublished, match="superseded"):
        unpublish_launch(ctx.conn, first.launch_id, ctx.notifier, ctx.clock)
    assert (published / "index.html").is_file()


def test_foreign_files_in_the_publish_dir_refuse(
    make_context: Callable[..., Context], studio_root: Path, record_file: Path
) -> None:
    ctx = make_context(**preview_ctx(studio_root))
    product_path = write_product(studio_root, capture=False)
    (studio_root / "published-launch").mkdir()
    (studio_root / "published-launch" / "someone-elses.html").write_text("x")
    run = run_kit(ctx, product_path, KitOptions(video=False), public_client(), publish=True)
    assert run.publish is not None and not run.publish.published
    gate = next(g for g in run.publish.failures if g.gate == "foreign_file")
    assert "someone-elses.html" in gate.detail
    assert (studio_root / "published-launch" / "someone-elses.html").read_text() == "x"


def test_d7_judges_the_preview_before_anything_goes_live(
    make_context: Callable[..., Context], studio_root: Path, record_file: Path
) -> None:
    seen: dict[str, object] = {}
    preview = preview_root(studio_root) / "launch"
    published = studio_root / "published-launch"

    fake = launch_preview(studio_root, "WARNING")

    def bridge(url: str) -> LaunchBridge:
        b = fake.bridge(url)
        real = b.page_health

        def page_health() -> dict[str, object]:
            seen["preview_has_bundle"] = (preview / "how-it-was-built.html").is_file()
            seen["live_exists"] = (published / "index.html").exists()
            seen["at"] = b.url
            return real()

        b.page_health = page_health  # type: ignore[method-assign]
        return b

    ctx = make_context(bridge=bridge, fetch=fake.fetch)
    product_path = write_product(studio_root, capture=False)
    run = run_kit(ctx, product_path, KitOptions(video=False), public_client(), publish=True)
    assert run.publish is not None and run.publish.published, run.publish.failures
    assert seen["preview_has_bundle"] is True and seen["live_exists"] is False
    at = urlsplit(str(seen["at"]))
    assert at.path == "/launch/index.html"
    query = parse_qs(at.query)
    assert query["studio_launch"] == [run.launch_id] and len(query["studio_attempt"][0]) == 32
    gates = {g.gate: g.outcome for g in run.publish.gate_results}
    assert gates["d7_navigate"] == "pass" and gates["d7_images"] == "pass"


def test_a_preview_serving_something_else_refuses(
    make_context: Callable[..., Context], studio_root: Path, record_file: Path
) -> None:
    ctx = make_context(**{**preview_ctx(studio_root), "fetch": lambda url: b"{}"})
    product_path = write_product(studio_root, capture=False)
    run = run_kit(ctx, product_path, KitOptions(video=False), public_client(), publish=True)
    assert run.publish is not None and not run.publish.published
    gate = next(g for g in run.publish.failures if g.gate == "d7_preview")
    assert "preview serves manifest" in gate.detail
    assert "d7_page_health" not in {g.gate for g in run.publish.gate_results}
    assert not (studio_root / "published-launch" / "index.html").exists()


def test_an_interrupted_swap_is_rolled_back_by_the_next_publish(
    make_context: Callable[..., Context],
    studio_root: Path,
    record_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = make_context(**preview_ctx(studio_root))
    product_path = write_product(studio_root, capture=False)
    product = load_product(product_path)
    first = run_kit(ctx, product_path, KitOptions(video=False), public_client(), publish=True)
    assert first.publish is not None and first.publish.published
    published = studio_root / "published-launch"
    live_before = (published / "launch-manifest.json").read_bytes()

    # Simulate a crash mid-swap: the journal is written, then the process dies
    # (the rollback in the except-branch never runs).
    ctx.clock.sleep(1)
    second = run_kit(ctx, product_path, KitOptions(video=False), public_client())
    import qontinui_studio.launch.publish as lp

    real_replace = os.replace

    def crash_after_first_move(src: object, dst: object) -> None:
        real_replace(src, dst)  # type: ignore[arg-type]
        if ".prev-" in Path(str(dst)).name:  # the first live file moved to its backup
            raise SystemExit("power cut")

    monkeypatch.setattr(os, "replace", crash_after_first_move)
    monkeypatch.setattr(
        lp.LaunchJournal, "roll_back", lambda self: []
    )  # the dead process cannot undo
    with pytest.raises(SystemExit):
        publish_launch(
            ctx.conn, second.launch_id, product, ctx.notifier, ctx.clock, ctx.bridge, ctx.fetch
        )
    monkeypatch.undo()
    assert list(published.glob(".studio-journal-*.json"))

    # The next operation on the directory replays the journal backward.
    report = recover_launches(ctx.conn, published, ctx.notifier)
    assert any("rolled back interrupted publish" in r for r in report)
    assert "rolled back" in notice_text(
        ctx.workspace.notices_dir, second.launch_id, "publish-rolled-back"
    )
    assert (published / "launch-manifest.json").read_bytes() == live_before
    assert not list(published.glob(".studio-journal-*.json"))
    status = ctx.conn.execute(
        "SELECT status FROM launches WHERE id = ?", (second.launch_id,)
    ).fetchone()["status"]
    assert status == "refused"


def test_an_interrupted_unpublish_is_rolled_back_and_the_launch_stays_live(
    make_context: Callable[..., Context],
    studio_root: Path,
    record_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = make_context(**preview_ctx(studio_root))
    product_path = write_product(studio_root, capture=False)
    run = run_kit(ctx, product_path, KitOptions(video=False), public_client(), publish=True)
    published = studio_root / "published-launch"
    real_replace = os.replace
    moves = {"n": 0}

    def crash_mid_unpublish(src: object, dst: object) -> None:
        real_replace(src, dst)  # type: ignore[arg-type]
        if ".prev-" in Path(str(dst)).name:
            moves["n"] += 1
            if moves["n"] == 3:
                raise SystemExit("power cut")

    import qontinui_studio.launch.publish as lp

    monkeypatch.setattr(os, "replace", crash_mid_unpublish)
    monkeypatch.setattr(lp.LaunchJournal, "roll_back", lambda self: [])
    with pytest.raises(SystemExit):
        unpublish_launch(ctx.conn, run.launch_id, ctx.notifier, ctx.clock)
    monkeypatch.undo()
    assert not (published / "index.html").exists() or not (published / "styles.css").exists()
    report = recover_launches(ctx.conn, published, ctx.notifier)
    assert any("rolled back interrupted unpublish" in r for r in report)
    assert (published / "index.html").is_file() and (published / "styles.css").is_file()
    status = ctx.conn.execute(
        "SELECT status FROM launches WHERE id = ?", (run.launch_id,)
    ).fetchone()["status"]
    assert status == "published"


def test_a_missing_published_notice_is_resent_by_recovery(
    make_context: Callable[..., Context], studio_root: Path, record_file: Path
) -> None:
    ctx = make_context(**preview_ctx(studio_root))
    product_path = write_product(studio_root, capture=False)
    run = run_kit(ctx, product_path, KitOptions(video=False), public_client(), publish=True)
    # As if the process died between the commit and the notice.
    ctx.conn.execute("UPDATE launches SET notified_at = NULL WHERE id = ?", (run.launch_id,))
    ctx.conn.commit()
    for f in ctx.workspace.notices_dir.glob(f"{run.launch_id}-published-*"):
        f.unlink()
    lines, failed = recover_all_launches(ctx.conn, ctx.notifier, studio_root)
    assert not failed and any("sent the missing published notice" in ln for ln in lines)
    assert "launch unpublish --launch" in notice_text(
        ctx.workspace.notices_dir, run.launch_id, "published"
    )
    # Exactly once.
    lines, _ = recover_all_launches(ctx.conn, ctx.notifier, studio_root)
    assert lines == []


def test_already_live_is_refused(
    make_context: Callable[..., Context], studio_root: Path, record_file: Path
) -> None:
    ctx = make_context(**preview_ctx(studio_root))
    product_path = write_product(studio_root, capture=False)
    run = run_kit(ctx, product_path, KitOptions(video=False), public_client(), publish=True)
    again = publish_launch(
        ctx.conn,
        run.launch_id,
        load_product(product_path),
        ctx.notifier,
        ctx.clock,
        ctx.bridge,
        ctx.fetch,
    )
    assert not again.published
    assert any(g.gate == "already_live" for g in again.failures)
    status = ctx.conn.execute(
        "SELECT status FROM launches WHERE id = ?", (run.launch_id,)
    ).fetchone()["status"]
    assert status == "published"  # a refused re-publish never demotes the live launch


def test_preview_path_limits(studio_root: Path) -> None:
    product_path = write_product(studio_root, capture=False)
    text = product_path.read_text()
    inside = product_path.with_name("inside.yaml")
    inside.write_text(text.replace("../../previews/launch", "../previews/launch"))
    with pytest.raises(ConfigError, match="outside the studio root"):
        load_product(inside)
    into_publish = product_path.with_name("into.yaml")
    into_publish.write_text(text.replace("../../previews/launch", "../published-launch/p"))
    with pytest.raises(ConfigError, match="neither publish_dir"):
        load_product(into_publish)


def test_preview_only_deletes_files_it_listed(
    make_context: Callable[..., Context], studio_root: Path, record_file: Path
) -> None:
    preview = preview_root(studio_root) / "launch"
    preview.mkdir(parents=True)
    (preview / "operators-note.txt").write_text("keep me")
    ctx = make_context(**preview_ctx(studio_root))
    product_path = write_product(studio_root, capture=False)
    run_kit(ctx, product_path, KitOptions(video=False), public_client(), publish=True)
    ctx.clock.sleep(1)
    run_kit(ctx, product_path, KitOptions(video=False), public_client(), publish=True)
    assert (preview / "operators-note.txt").read_text() == "keep me"


def test_d7_refuses_a_bridge_that_does_not_load_this_launch(
    make_context: Callable[..., Context], studio_root: Path, record_file: Path
) -> None:
    fake = launch_preview(studio_root)
    fake.ignore_navigate = True  # the bridged page stays wherever it was
    ctx = make_context(bridge=fake.bridge, fetch=fake.fetch)
    product_path = write_product(studio_root, capture=False)
    run = run_kit(ctx, product_path, KitOptions(video=False), public_client(), publish=True)
    assert run.publish is not None and not run.publish.published
    nav = next(g for g in run.publish.failures if g.gate == "d7_navigate")
    assert "not this launch's preview" in nav.detail
    assert not (studio_root / "published-launch" / "index.html").exists()


def test_d7_refuses_a_broken_image_on_the_landing_page(
    make_context: Callable[..., Context], studio_root: Path, record_file: Path
) -> None:
    fake = launch_preview(studio_root)
    real_fetch = fake.fetch
    preview = preview_root(studio_root) / "launch"

    def fetch_and_break(url: str) -> bytes:
        # The launch preview serves the right manifest, but its hero is corrupt.
        if url.endswith("/launch-manifest.json"):
            (preview / "assets" / "hero.png").write_bytes(b"not a png")
        return real_fetch(url)

    write_launch_decor(studio_root)
    ctx = make_context(bridge=fake.bridge, fetch=fetch_and_break)
    product_path = write_product(studio_root, capture=False, brief=True)
    run = run_kit(ctx, product_path, KitOptions(video=False), public_client(), publish=True)
    assert run.publish is not None and not run.publish.published
    images = next(g for g in run.publish.failures if g.gate == "d7_images")
    assert "assets/hero.png" in images.detail and "did not load" in images.detail


def test_a_tampered_journal_is_refused_not_acted_on(
    make_context: Callable[..., Context], studio_root: Path, record_file: Path
) -> None:
    ctx = make_context(**preview_ctx(studio_root))
    product_path = write_product(studio_root, capture=False)
    run = run_kit(ctx, product_path, KitOptions(video=False), public_client(), publish=True)
    published = studio_root / "published-launch"
    outside = studio_root / "precious.txt"
    outside.write_text("do not touch")
    journal = published / f".studio-journal-{run.launch_id}.json"
    journal.write_text(
        json.dumps(
            {
                "kind": "unpublish",
                "launch_id": run.launch_id,
                "placed": [],
                "moved": [["../precious.txt", "../.precious.txt.prev-" + "0" * 32, "0" * 64, True]],
            }
        )
    )
    with pytest.raises(JournalError, match="not a bundle path"):
        recover_launches(ctx.conn, published, ctx.notifier)
    assert outside.read_text() == "do not touch"
    lines, failed = recover_all_launches(ctx.conn, ctx.notifier, studio_root)
    assert str(published) in failed


def test_a_file_changed_between_check_and_move_is_never_moved(
    make_context: Callable[..., Context],
    studio_root: Path,
    record_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = make_context(**preview_ctx(studio_root))
    product_path = write_product(studio_root, capture=False)
    first = run_kit(ctx, product_path, KitOptions(video=False), public_client(), publish=True)
    assert first.publish is not None and first.publish.published
    published = studio_root / "published-launch"
    ctx.clock.sleep(1)
    second = run_kit(ctx, product_path, KitOptions(video=False), public_client())
    import qontinui_studio.launch.publish as lp

    real_write = lp.LaunchJournal.write

    def write_then_hand_edit(self: lp.LaunchJournal) -> None:
        real_write(self)
        (published / "index.html").write_text("<p>an owner's hand edit</p>")

    monkeypatch.setattr(lp.LaunchJournal, "write", write_then_hand_edit)
    result = publish_launch(
        ctx.conn,
        second.launch_id,
        load_product(product_path),
        ctx.notifier,
        ctx.clock,
        ctx.bridge,
        ctx.fetch,
    )
    assert not result.published
    gate = next(g for g in result.failures if g.gate == "foreign_file")
    assert "changed after it was checked" in gate.detail
    assert (published / "index.html").read_text() == "<p>an owner's hand edit</p>"
    assert not list(published.glob(".studio-journal-*.json"))


def test_d7_images_require_this_bundles_bytes_by_sha(
    make_context: Callable[..., Context], studio_root: Path, record_file: Path
) -> None:
    """A landing-page image without this bundle's ?sha= (a stale cached copy,
    a page that forgot the cache-buster) refuses."""
    fake = launch_preview(studio_root)
    preview = preview_root(studio_root) / "launch"
    real_fetch = fake.fetch

    def strip_sha_on_the_page(url: str) -> bytes:
        if url.endswith("/launch-manifest.json"):
            index = preview / "index.html"
            index.write_text(re.sub(r"\?sha=[0-9a-f]{64}", "", index.read_text()))
        return real_fetch(url)

    write_launch_decor(studio_root)
    ctx = make_context(bridge=fake.bridge, fetch=strip_sha_on_the_page)
    product_path = write_product(studio_root, capture=False, brief=True)
    run = run_kit(ctx, product_path, KitOptions(video=False), public_client(), publish=True)
    assert run.publish is not None and not run.publish.published
    images = next(g for g in run.publish.failures if g.gate == "d7_images")
    assert "assets/hero.png" in images.detail and "(?sha=)" in images.detail


def test_d7_fetches_unrendered_images_and_compares_bytes(
    make_context: Callable[..., Context], studio_root: Path, record_file: Path
) -> None:
    """The social cards are not on the landing page: D7 fetches each from the
    preview origin and compares its sha256."""
    fake = launch_preview(studio_root)
    preview = preview_root(studio_root) / "launch"
    real_fetch = fake.fetch

    def corrupt_og(url: str) -> bytes:
        if url.endswith("/launch-manifest.json"):
            (preview / "social" / "og.png").write_bytes(b"someone else's card")
        return real_fetch(url)

    ctx = make_context(bridge=fake.bridge, fetch=corrupt_og)
    product_path = write_product(studio_root, capture=False)
    run = run_kit(ctx, product_path, KitOptions(video=False), public_client(), publish=True)
    assert run.publish is not None and not run.publish.published
    images = next(g for g in run.publish.failures if g.gate == "d7_images")
    assert "social/og.png" in images.detail and "not this batch's bytes" in images.detail


@pytest.mark.parametrize(
    "bridge_url",
    [
        "http://127.0.0.1:9876/__ui-bridge",
        "http://localhost:9875/x",
        "http://[::1]:9876/x",
        "http://0.0.0.0:9876/x",
    ],
)
def test_the_runner_and_supervisor_uis_are_refused_as_a_launch_bridge(
    studio_root: Path, bridge_url: str
) -> None:
    product_path = write_product(studio_root, capture=False)
    text = product_path.read_text()
    product_path.write_text(re.sub(r"ui_bridge_url: \S+", f"ui_bridge_url: {bridge_url}", text))
    with pytest.raises(ConfigError, match="runner/supervisor"):
        load_product(product_path)


def test_d7_binding_is_browser_normalised_and_origin_checked(
    make_context: Callable[..., Context], studio_root: Path, record_file: Path
) -> None:
    """A preview_url spelled with an upper-case host and the default port still
    binds (the browser reports it normalised); a page on another origin does not."""
    product_path = write_product(studio_root, capture=False)
    text = product_path.read_text()
    product_path.write_text(
        text.replace(
            "preview_url: http://preview.test/launch/index.html",
            "preview_url: HTTP://Preview.Test:80/launch/./index.html",
        )
    )
    ctx = make_context(**preview_ctx(studio_root))
    run = run_kit(ctx, product_path, KitOptions(video=False), public_client(), publish=True)
    assert run.publish is not None and run.publish.published, run.publish.failures

    fake = launch_preview(studio_root)
    fake.stale_url["launch"] = "http://elsewhere.test/launch/index.html"
    fake.ignore_navigate = True
    ctx2 = make_context(bridge=fake.bridge, fetch=fake.fetch)
    ctx2.clock.sleep(1)
    again = run_kit(ctx2, product_path, KitOptions(video=False), public_client(), publish=True)
    assert again.publish is not None and not again.publish.published
    nav = next(g for g in again.publish.failures if g.gate == "d7_navigate")
    assert "not the preview origin" in nav.detail


def test_d7_waits_for_launch_images_to_settle(
    make_context: Callable[..., Context], studio_root: Path, record_file: Path
) -> None:
    """Images still decoding are waited for (within the deadline), then pass."""
    fake = launch_preview(studio_root)
    fake.pending_snapshots = 3
    write_launch_decor(studio_root)
    ctx = make_context(bridge=fake.bridge, fetch=fake.fetch)
    product_path = write_product(studio_root, capture=False, brief=True)
    run = run_kit(ctx, product_path, KitOptions(video=False), public_client(), publish=True)
    assert run.publish is not None and run.publish.published, run.publish.failures
    assert fake.pending_snapshots == 0


def test_lazy_launch_images_are_refused(
    make_context: Callable[..., Context], studio_root: Path, record_file: Path
) -> None:
    fake = launch_preview(studio_root)
    preview = preview_root(studio_root) / "launch"
    real_fetch = fake.fetch

    def make_lazy(url: str) -> bytes:
        if url.endswith("/launch-manifest.json"):
            index = preview / "index.html"
            index.write_text(index.read_text().replace("loading='eager'", "loading='lazy'"))
        return real_fetch(url)

    write_launch_decor(studio_root)
    ctx = make_context(bridge=fake.bridge, fetch=make_lazy)
    product_path = write_product(studio_root, capture=False, brief=True)
    run = run_kit(ctx, product_path, KitOptions(video=False), public_client(), publish=True)
    assert run.publish is not None and not run.publish.published
    images = next(g for g in run.publish.failures if g.gate == "d7_images")
    assert 'loading="eager"' in images.detail


def test_landing_page_images_are_eager_with_sha_on_every_url(
    make_context: Callable[..., Context], studio_root: Path, record_file: Path
) -> None:
    write_launch_decor(studio_root)
    ctx = make_context(**preview_ctx(studio_root))
    product_path = write_product(studio_root, capture=False, brief=True)
    run = run_kit(ctx, product_path, KitOptions(video=False), public_client())
    index = (run.bundle.out_dir / "index.html").read_text()
    imgs = re.findall(r"<img [^>]*>", index)
    assert imgs and all("loading='eager'" in tag for tag in imgs)
    shas = {i["path"]: i["sha256"] for i in run.bundle.manifest["images"]}
    for path, sha in shas.items():
        for ref in re.findall(re.escape(path) + r"(\?sha=[0-9a-f]*)?['\"\s,]", index):
            assert ref == f"?sha={sha}", (path, ref)


@pytest.mark.parametrize("source", ["file", "authed", "unversioned"])
def test_review_d1_only_a_public_snapshot_publishes(
    make_context: Callable[..., Context], studio_root: Path, record_file: Path, source: str
) -> None:
    """Review finding 2: a bundle built from a local file, the authed live
    document, or a public answer without a version is a preview only — the
    gates refuse it, and no non-public URL is written into its files."""
    ctx = make_context(**preview_ctx(studio_root))
    product_path = write_product(studio_root, capture=False)
    if source == "file":
        run = run_kit(ctx, product_path, KitOptions(record_file=record_file, video=False))
    elif source == "unversioned":
        import httpx

        from qontinui_studio.launch.record import BuildRecordClient

        from .launch_support import wrapper

        body = {**wrapper(document()), "version": None}
        client = BuildRecordClient(
            http=httpx.Client(
                transport=httpx.MockTransport(lambda r: httpx.Response(200, json=body))
            )
        )
        run = run_kit(ctx, product_path, KitOptions(video=False), client)
    else:
        import httpx

        from qontinui_studio.launch.record import BuildRecordClient

        client = BuildRecordClient(
            http=httpx.Client(
                transport=httpx.MockTransport(lambda r: httpx.Response(200, json=document()))
            ),
            bearer="tok",
        )
        product_path.write_text(product_path.read_text().replace("door: public", "door: authed"))
        run = run_kit(ctx, product_path, KitOptions(video=False), client)
    out = run.bundle.out_dir
    for path in out.rglob("*"):
        if path.is_file() and path.suffix in (".html", ".json", ".txt"):
            text = path.read_text()
            assert "file://" not in text and "/api/v1/build-records/" not in text, path
    result = publish_launch(
        ctx.conn,
        run.launch_id,
        load_product(product_path),
        ctx.notifier,
        ctx.clock,
        ctx.bridge,
        ctx.fetch,
    )
    assert not result.published
    d1 = next(g for g in result.failures if g.gate == "d1_record")
    assert "only from the public snapshot" in d1.detail


def test_review_squat_the_kit_refuses_another_products_record(
    make_context: Callable[..., Context], studio_root: Path
) -> None:
    """Review finding 4 (scripts/squat.py): another product's record served
    under our slug never becomes our launch page."""
    from qontinui_studio.launch.record import BuildRecordInvalid

    doc = document()
    doc["product"]["title"] = "Someone else's product"
    doc["product"]["repos"] = ["mallory/fake"]
    doc["prs"] = []
    ctx = make_context(**preview_ctx(studio_root))
    product_path = write_product(studio_root, capture=False)
    with pytest.raises(BuildRecordInvalid, match="does not declare"):
        run_kit(ctx, product_path, KitOptions(video=False), public_client(doc))
    assert not any((studio_root / ".studio" / "launches").glob("*/how-it-was-built.html"))


def test_review_outwipe_out_refuses_a_directory_that_is_not_a_bundle(
    make_context: Callable[..., Context], studio_root: Path, tmp_path: Path
) -> None:
    """Review finding 6 (scripts/outwipe.py): --out never wipes an unrelated
    directory; an earlier bundle is replaced atomically."""
    from qontinui_studio.errors import ConfigError

    precious = tmp_path / "precious"
    precious.mkdir()
    (precious / "thesis.tex").write_text("years of work")
    ctx = make_context(**preview_ctx(studio_root))
    product_path = write_product(studio_root, capture=False)
    with pytest.raises(ConfigError, match="refusing to replace"):
        run_kit(ctx, product_path, KitOptions(video=False, out_dir=precious), public_client())
    assert sorted(p.name for p in precious.iterdir()) == ["thesis.tex"]

    out = tmp_path / "bundle"
    first = run_kit(ctx, product_path, KitOptions(video=False, out_dir=out), public_client())
    assert json.loads((out / "launch-manifest.json").read_text())["launch_id"] == first.launch_id
    assert not [p for p in tmp_path.iterdir() if p.name.startswith(".bundle.")]
    # A registered launch's bundle is never rebuilt in place (review round 2).
    ctx.clock.sleep(1)
    with pytest.raises(ConfigError, match="refusing --out"):
        run_kit(ctx, product_path, KitOptions(video=False, out_dir=out), public_client())


def test_review_page_captured_publicly_shows_no_absolute_paths(
    make_context: Callable[..., Context], studio_root: Path, write_brief: Callable[..., Path]
) -> None:
    """Review finding 9 (scripts/review.py): the review page the kit captures
    into public screenshots must not carry operator paths."""
    from qontinui_studio.pipeline import run_brief
    from qontinui_studio.review import build_review_page

    ctx = make_context(**preview_ctx(studio_root))
    brief = write_brief()
    run_brief(ctx, brief)
    out = build_review_page(ctx.conn, studio_root / "review-public", ctx.clock(), studio_root, True)
    text = out.read_text()
    assert str(brief) not in text and str(studio_root) not in text
    assert str(Path.home()) not in text
    assert "briefs/hero.yaml" in text
    private = build_review_page(ctx.conn, studio_root / "review-private", ctx.clock(), studio_root)
    assert str(brief) in private.read_text()


def test_launch_d7_refuses_a_bridge_without_media_discovery(
    make_context: Callable[..., Context], studio_root: Path
) -> None:
    fake = launch_preview(studio_root)
    seeded = launch_preview(studio_root)
    seeded.media_discovery = False  # the launch preview's bridge registers no media

    def bridge(url: str) -> LaunchBridge:
        return (seeded if "/launch/" in url else fake).bridge(url)

    write_launch_decor(studio_root)
    ctx = make_context(bridge=bridge, fetch=fake.fetch)
    product_path = write_product(studio_root, capture=False, brief=True)
    run = run_kit(ctx, product_path, KitOptions(video=False), public_client(), publish=True)
    assert run.publish is not None and not run.publish.published
    images = next(g for g in run.publish.failures if g.gate == "d7_images")
    assert "media discovery" in images.detail


def test_launch_images_are_found_by_data_media_id_only(
    make_context: Callable[..., Context], studio_root: Path
) -> None:
    """An image whose data-media-id is missing is not matched by file name."""
    fake = launch_preview(studio_root)
    preview = preview_root(studio_root) / "launch"
    real_fetch = fake.fetch

    def drop_ids(url: str) -> bytes:
        if url.endswith("/launch-manifest.json"):
            index = preview / "index.html"
            index.write_text(re.sub(r" data-media-id='[^']*'", "", index.read_text()))
        return real_fetch(url)

    write_launch_decor(studio_root)
    ctx = make_context(bridge=fake.bridge, fetch=drop_ids)
    product_path = write_product(studio_root, capture=False, brief=True)
    run = run_kit(ctx, product_path, KitOptions(video=False), public_client(), publish=True)
    assert run.publish is not None and not run.publish.published
    images = next(g for g in run.publish.failures if g.gate == "d7_images")
    assert f'"{img_media_id("assets/hero.png")}"' in images.detail


def test_landing_images_carry_media_ids_and_sizes(
    make_context: Callable[..., Context], studio_root: Path
) -> None:
    write_launch_decor(studio_root)
    ctx = make_context(**preview_ctx(studio_root))
    product_path = write_product(studio_root, capture=False, brief=True)
    run = run_kit(ctx, product_path, KitOptions(video=False), public_client())
    index = (run.bundle.out_dir / "index.html").read_text()
    tag = re.search(r"<img [^>]*assets/hero.png[^>]*>", index)
    assert tag is not None
    assert f"data-media-id='{img_media_id('assets/hero.png')}'" in tag.group(0)
    assert img_media_id("assets/hero.png").startswith("studio-img-launch-assets-hero-")
    assert "width='800' height='450'" in tag.group(0)


def _tamper_preview(
    studio_root: Path, edit: Callable[[Path], None]
) -> tuple[Callable[[str], Any], Callable[[str], bytes]]:
    """A fake preview whose launch page is edited once the bundle is written
    (at the manifest fetch, before the bridge loads the page)."""
    fake = launch_preview(studio_root)
    preview = preview_root(studio_root) / "launch"

    def fetch(url: str) -> bytes:
        if url.endswith("/launch-manifest.json"):
            edit(preview)
        return fake.fetch(url)

    return fake.bridge, fetch


def _publish_with(
    make_context: Callable[..., Context], studio_root: Path, edit: Callable[[Path], None]
) -> str:
    bridge, fetch = _tamper_preview(studio_root, edit)
    write_launch_decor(studio_root)
    ctx = make_context(bridge=bridge, fetch=fetch)
    product_path = write_product(studio_root, capture=False, brief=True)
    run = run_kit(ctx, product_path, KitOptions(video=False), public_client(), publish=True)
    assert run.publish is not None and not run.publish.published
    return next(g for g in run.publish.failures if g.gate == "d7_images").detail


def test_eleventh_a_src_less_landing_image_fails(
    make_context: Callable[..., Context], studio_root: Path
) -> None:
    def drop_src(preview: Path) -> None:
        index = preview / "index.html"
        index.write_text(re.sub(r"<img src='[^']*' ", "<img ", index.read_text()))

    detail = _publish_with(make_context, studio_root, drop_src)
    assert "has no src" in detail and "shows none of its files" in detail


def test_eleventh_an_image_served_elsewhere_fails(
    make_context: Callable[..., Context], studio_root: Path
) -> None:
    """Same file name, same ?sha=, but not at urljoin(preview_manifest_url, path)."""

    def move(preview: Path) -> None:
        other = preview / "elsewhere" / "assets"
        other.mkdir(parents=True)
        shutil.copyfile(preview / "assets" / "hero.png", other / "hero.png")
        index = preview / "index.html"
        index.write_text(
            index.read_text().replace("src='assets/hero.png", "src='elsewhere/assets/hero.png")
        )

    detail = _publish_with(make_context, studio_root, move)
    assert "which is not one of the slot's preview files" in detail
    assert "http://preview.test/launch/assets/hero.png" in detail


def test_eleventh_the_shown_url_is_fetched_and_hashed(
    make_context: Callable[..., Context], studio_root: Path
) -> None:
    """A different (valid, loading) image at the right URL with the right
    ?sha= is caught by hashing the bytes the element shows."""

    def swap(preview: Path) -> None:
        Image.new("RGB", (800, 450), (255, 0, 0)).save(preview / "assets" / "hero.png")

    detail = _publish_with(make_context, studio_root, swap)
    assert "assets/hero.png" in detail and "is not this batch's bytes" in detail


def test_eleventh_manifest_url_must_share_the_preview_origin(studio_root: Path) -> None:
    from qontinui_studio.errors import ConfigError

    product_path = write_product(studio_root, capture=False)
    text = product_path.read_text()
    product_path.write_text(
        text.replace(
            "preview_manifest_url: http://preview.test/launch/launch-manifest.json",
            "preview_manifest_url: http://cdn.example/launch/launch-manifest.json",
        )
    )
    with pytest.raises(ConfigError, match="same origin as preview_url"):
        load_product(product_path)


def _reject_hero(ctx: Context) -> str:
    lib = Library(ctx.conn)
    uid = next(a.uid for a in lib.assets() if a.origin == "generated")
    lib.decide(uid, "rejected", "josh", date(2026, 10, 14))
    return uid


def test_rejected_decor_is_skipped_and_the_launch_still_publishes(
    make_context: Callable[..., Context], studio_root: Path, record_file: Path
) -> None:
    """``library reject`` means consumers must not use the asset: the kit
    leaves a rejected published asset out (decoration is optional, D1) and
    says so in an operator note."""
    write_launch_decor(studio_root)
    ctx = make_context(**preview_ctx(studio_root))
    product_path = write_product(studio_root, capture=False, brief=True)
    run_kit(ctx, product_path, KitOptions(video=False), public_client())
    uid = _reject_hero(ctx)
    run = run_kit(
        ctx,
        product_path,
        KitOptions(video=False, generate_decor=False),
        public_client(),
        publish=True,
    )
    assert not [i for i in run.bundle.manifest["images"] if i.get("kind") == "decorative"]
    assert any(f"asset {uid} was rejected in the library by josh" in n for n in run.bundle.notes)
    assert run.publish is not None and run.publish.published, run.publish.failures


def test_decor_rejected_after_assembly_is_refused_at_publish(
    make_context: Callable[..., Context], studio_root: Path, record_file: Path
) -> None:
    write_launch_decor(studio_root)
    ctx = make_context(**preview_ctx(studio_root))
    product_path = write_product(studio_root, capture=False, brief=True)
    run = run_kit(ctx, product_path, KitOptions(video=False), public_client())
    assert [i for i in run.bundle.manifest["images"] if i.get("kind") == "decorative"]
    uid = _reject_hero(ctx)
    result = publish_launch(
        ctx.conn,
        run.launch_id,
        load_product(product_path),
        ctx.notifier,
        ctx.clock,
        ctx.bridge,
        ctx.fetch,
    )
    assert not result.published
    refused = [g for g in result.failures if g.gate == "library_approval"]
    assert refused and uid in refused[0].subject and "rejected" in refused[0].detail


def test_page_scripts_load_on_every_page_and_publication_still_passes(
    make_context: Callable[..., Context], studio_root: Path, record_file: Path
) -> None:
    """`publish.page_scripts` is how a host site gets its UI Bridge boot onto
    studio-rendered bundle pages (plan 2026-10-17-build-qontinui-io-factory-hub,
    D3): every page loads each script, in order, and every gate still passes."""
    ctx = make_context(**preview_ctx(studio_root))
    scripts = ("/_hub/ui-bridge.js", "/_hub/extra.mjs")
    product_path = write_product(studio_root, capture=False, page_scripts=scripts)
    run = run_kit(ctx, product_path, KitOptions(video=False), public_client(), publish=True)
    assert run.publish is not None and run.publish.published, run.publish.failures
    tags = "".join(f"<script type='module' src='{s}'></script>" for s in scripts)
    for page in ("index.html", "how-it-was-built.html"):
        text = (run.bundle.out_dir / page).read_text()
        head = text.split("</head>", 1)[0]
        assert tags in head, page


def test_page_scripts_without_any_render_no_script_tag(
    make_context: Callable[..., Context], studio_root: Path, record_file: Path
) -> None:
    ctx = make_context(**preview_ctx(studio_root))
    product_path = write_product(studio_root, capture=False)
    run = run_kit(ctx, product_path, KitOptions(video=False), public_client())
    for page in ("index.html", "how-it-was-built.html"):
        assert "<script" not in (run.bundle.out_dir / page).read_text()


@pytest.mark.parametrize(
    "bad",
    [
        "https://cdn.example/x.js",
        "//cdn.example/x.js",
        "_hub/ui-bridge.js",
        "/_hub/ui-bridge.js?v=1",
        "/_hub/ui-bridge.js#x",
        "/_hub/../secret.js",
        "/_hub/./ui-bridge.js",
        "/.hidden/x.js",
        "/a/.hidden/x.js",
        "/a/x/.js",
        "/a/.../x.js",
        "/a//b.js",
        "/_hub/ui-bridge.js/",
        "/localhost/x.js",
        "/home/op/x.js",
        "/127.0.0.1/x.js",
        "/box.local/x.js",
        "/_hub:9876/x.js",
        "/_hub/ui-bridge.css",
        "/_hub/ui bridge.js",
        "/_hub/x.js'><script>",
    ],
)
def test_page_scripts_refuse_anything_but_a_root_relative_js_path(
    studio_root: Path, bad: str
) -> None:
    path = write_product(studio_root, capture=False, page_scripts=(bad,))
    with pytest.raises(ConfigError, match="page_scripts"):
        load_product(path)


@pytest.mark.parametrize(
    "good", ["/_hub/ui-bridge.js", "/a/b.c/x.min.js", "/x.mjs", "/home-page/x.js"]
)
def test_page_scripts_accept_site_paths(studio_root: Path, good: str) -> None:
    path = write_product(studio_root, capture=False, page_scripts=(good,))
    assert load_product(path).publish.page_scripts == (good,)


def test_page_scripts_load_when_no_build_record_is_published(
    make_context: Callable[..., Context], studio_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The no-record branch of how_it_was_built still carries the scripts."""
    ctx = make_context(**preview_ctx(studio_root))
    product_path = write_product(studio_root, capture=False, page_scripts=("/_hub/ui-bridge.js",))

    def absent(*_: object) -> NoBuildRecord:
        return NoBuildRecord("not_found", "https://api.example.test/x", "HTTP 404")

    monkeypatch.setattr("qontinui_studio.launch.kit.fetch_record", absent)
    run = run_kit(ctx, product_path, KitOptions(video=False))
    text = (run.bundle.out_dir / "how-it-was-built.html").read_text()
    assert "data-record='absent'" in text
    head = text.split("</head>", 1)[0]
    assert "<script type='module' src='/_hub/ui-bridge.js'></script>" in head


def test_page_scripts_refuse_a_duplicate(studio_root: Path) -> None:
    path = write_product(studio_root, capture=False, page_scripts=("/_hub/a.js", "/_hub/a.js"))
    with pytest.raises(ConfigError, match="twice"):
        load_product(path)
