"""The third independent review of the launch kit (790652e): each finding as a
regression test (scratchpad launch-rev3/proj/tests/test_rev3*.py, scripts/r4.py)."""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

import pytest

from qontinui_studio.launch import bundle as bundle_module
from qontinui_studio.launch.bundle import BundleRefused, media_group
from qontinui_studio.launch.kit import KitOptions, run_kit
from qontinui_studio.launch.product import load_product
from qontinui_studio.launch.publish import (
    gate_integrity,
    gate_public_text,
    image_groups,
    publish_launch,
    recover_launches,
)
from qontinui_studio.launch.record import canonical_sha256
from qontinui_studio.pipeline import Context

from .launch_support import (
    document,
    playwright_ready,
    preview_ctx,
    public_client,
    use_public_door,
    write_product,
)


@pytest.fixture(autouse=True)
def _public_door(monkeypatch: pytest.MonkeyPatch) -> None:
    use_public_door(monkeypatch)


@pytest.fixture(autouse=True)
def _no_bearers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("STUDIO_WEB_BEARER", raising=False)
    monkeypatch.delenv("STUDIO_WEB_BEARER_FILE", raising=False)


# ---- R1: the snapshot must be OWNED by the operator's tenant -----------------------------


def _mallory() -> dict[str, object]:
    doc = document()
    doc["product"]["title"] = "Mallory Labs (not the operator)"
    doc["work_units"][0]["title"] = "Mallory wrote this plan title"
    doc["prs"], doc["timeline"] = [], []
    return doc


def test_r1_mallory_owns_the_public_snapshot(
    make_context: Callable[..., Context], studio_root: Path
) -> None:
    """The operator defined the slug and opted in, but Mallory published the
    public snapshot first: /publication says owned_by_caller_tenant=false."""
    doc = _mallory()
    ctx = make_context(**preview_ctx(studio_root))
    product_path = write_product(studio_root, capture=False)
    client = public_client(
        doc,
        # Exactly what qontinui-web serves when another tenant owns the slug.
        publication={
            "public_slug": "qontinui-studio",
            "owned_by_caller_tenant": False,
            "unpublished": False,
            "version": None,
            "content_sha256": None,
        },
    )
    run = run_kit(ctx, product_path, KitOptions(video=False), client, publish=True)
    assert run.publish is not None and not run.publish.published
    tenant = next(g for g in run.publish.failures if g.gate == "d1_tenant")
    assert "owned by another tenant" in tenant.detail
    assert not (studio_root / "published-launch" / "how-it-was-built.html").exists()


@pytest.mark.parametrize(
    ("answer", "pin", "passes"),
    [
        ("owned", None, True),
        ("owned-other-version", None, False),
        ("owned-unpublished", None, False),
        ("route-404", None, False),
        ("route-404", "right", True),
        ("route-404", "wrong", False),
        ("no-bearer", None, False),
        ("no-bearer", "right", True),
    ],
)
def test_r1_tenant_proof_matrix(
    make_context: Callable[..., Context],
    studio_root: Path,
    answer: str,
    pin: str | None,
    passes: bool,
) -> None:
    doc = document()
    digest = canonical_sha256(doc)
    owned = {
        "public_slug": "qontinui-studio",
        "owned_by_caller_tenant": True,
        "version": 1,
        "content_sha256": digest,
    }
    publication: dict[str, object] | int
    bearer: str | None = "test-web-bearer"
    if answer == "owned":
        publication = {**owned, "unpublished": False}
    elif answer == "owned-other-version":
        publication = {**owned, "version": 2, "unpublished": False}
    elif answer == "owned-unpublished":
        publication = {**owned, "unpublished": True}
    elif answer == "route-404":
        publication = 404
    else:
        publication, bearer = 404, None
    ctx = make_context(**preview_ctx(studio_root))
    product_path = write_product(studio_root, capture=False)
    if pin is not None:
        sha = digest if pin == "right" else "f" * 64
        text = product_path.read_text()
        product_path.write_text(
            text.replace(
                "public_url: 'https://build.example.test/qontinui-studio'}",
                f"public_url: 'https://build.example.test/qontinui-studio', expected_sha256: '{sha}'}}",
            )
        )
    client = public_client(doc, publication=publication, bearer=bearer)
    run = run_kit(ctx, product_path, KitOptions(video=False), client, publish=True)
    assert run.publish is not None
    tenant = next(g for g in run.publish.gate_results if g.gate == "d1_tenant")
    assert (tenant.outcome == "pass") is passes, tenant.detail
    assert run.publish.published is passes


# ---- R2: no hidden working files in a bundle -----------------------------------------------


def test_r2_a_failed_film_leaves_no_hidden_files_and_recovery_works(
    make_context: Callable[..., Context], studio_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = make_context(**preview_ctx(studio_root))
    product_path = write_product(studio_root, capture=False)
    product = load_product(product_path)
    monkeypatch.setenv("PATH", "/nonexistent")  # a box without ffmpeg
    run = run_kit(ctx, product_path, KitOptions(video=True), public_client())
    monkeypatch.setenv("PATH", os.environ.get("PATH", ""))
    assert any("film not rendered" in n for n in run.bundle.notes)
    files = run.bundle.manifest["files"]
    assert not [f for f in files if any(p.startswith(".") for p in Path(f).parts)]
    assert not list(run.bundle.out_dir.rglob(".*"))

    import qontinui_studio.launch.publish as lp

    def die(*a: object, **k: object) -> None:
        raise SystemExit("power cut after the live swap, before the commit")

    monkeypatch.setattr(lp, "_record_gates", die)
    monkeypatch.setattr(lp.LaunchJournal, "roll_back", lambda self: [])
    with pytest.raises(SystemExit):
        publish_launch(
            ctx.conn, run.launch_id, product, ctx.notifier, ctx.clock, ctx.bridge, ctx.fetch,
            records=public_client(),
        )  # fmt: skip
    monkeypatch.undo()
    use_public_door(monkeypatch)
    published = studio_root / "published-launch"
    report = recover_launches(ctx.conn, published, ctx.notifier)  # no JournalError
    assert any("rolled back interrupted publish" in r for r in report)
    ctx.clock.sleep(1)
    run2 = run_kit(ctx, product_path, KitOptions(video=False), public_client(), publish=True)
    assert run2.publish is not None and run2.publish.published, run2.publish.failures


def test_r2_assembly_refuses_a_hidden_path(
    make_context: Callable[..., Context], studio_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = bundle_module.write_posts

    def posts_and_a_leftover(out_dir: Path, *a: object, **k: object) -> object:
        (out_dir.parent / ".leftover.tmp").write_text("x")
        return real(out_dir, *a, **k)  # type: ignore[arg-type]

    monkeypatch.setattr(bundle_module, "write_posts", posts_and_a_leftover)
    ctx = make_context(**preview_ctx(studio_root))
    with pytest.raises(BundleRefused, match="hidden path"):
        run_kit(
            ctx, write_product(studio_root, capture=False), KitOptions(video=False), public_client()
        )


def test_r2_integrity_refuses_a_manifest_naming_a_hidden_path(tmp_path: Path) -> None:
    (tmp_path / ".segments").mkdir()
    (tmp_path / ".segments" / "x.mp4").write_bytes(b"x")
    from qontinui_studio.postprocess import sha256_file

    manifest = {
        "launch_id": "x",
        "files": {".segments/x.mp4": sha256_file(tmp_path / ".segments" / "x.mp4")},
    }
    result = gate_integrity(tmp_path, manifest)
    assert result.failed and "hidden (dot-prefixed) component" in result.detail


# ---- R3: operator-only details never reach a published file --------------------------------

_PW_SKIP = playwright_ready()


@pytest.mark.skipif(_PW_SKIP is not None, reason=_PW_SKIP or "")
def test_r3_a_capture_error_with_a_home_path_is_not_published(
    make_context: Callable[..., Context], studio_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Playwright installed, its browsers not: the error names a path under the
    operator's home. It stays an operator note; the published bundle is clean."""
    empty = studio_root.parent / "no-browsers"
    empty.mkdir()
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(empty))
    ctx = make_context(**preview_ctx(studio_root))
    product_path = write_product(studio_root, capture=True)
    run = run_kit(ctx, product_path, KitOptions(video=False), public_client(), publish=True)
    assert run.publish is not None and run.publish.published, run.publish.failures
    assert any("capture skipped" in n for n in run.bundle.notes)
    live = studio_root / "published-launch"
    for path in live.rglob("*"):
        if path.is_file() and path.suffix in (".html", ".json", ".txt"):
            assert str(Path.home()) not in path.read_text(), path


@pytest.mark.parametrize(
    "leak",
    [
        str(Path.home()) + "/secrets",
        "/home/someone/.qontinui/coord-device-jwt",
        "C:\\Users\\Joshua\\studio",
        "http://localhost:3000/preview",
        "http://127.0.0.1:43111/",
        "spaceship.local",
        "file:///etc/passwd",
    ],
)
def test_r3_public_text_gate_refuses_local_details(
    make_context: Callable[..., Context], studio_root: Path, leak: str
) -> None:
    ctx = make_context(**preview_ctx(studio_root))
    run = run_kit(
        ctx, write_product(studio_root, capture=False), KitOptions(video=False), public_client()
    )
    out = run.bundle.out_dir
    (out / "posts" / "x.txt").write_text(f"see {leak}\n")
    result = gate_public_text(out, run.bundle.manifest, run.product)
    assert result.failed, result.detail
    assert (
        gate_public_text(out, {**run.bundle.manifest, "files": {}}, run.product).outcome == "pass"
    )


def test_r3_the_clean_bundle_passes_public_text(
    make_context: Callable[..., Context], studio_root: Path
) -> None:
    ctx = make_context(**preview_ctx(studio_root))
    run = run_kit(
        ctx, write_product(studio_root, capture=False), KitOptions(video=False), public_client()
    )
    assert gate_public_text(run.bundle.out_dir, run.bundle.manifest, run.product).outcome == "pass"


# ---- R4: media groups never collide ------------------------------------------------------------


def test_r4_paths_that_slug_alike_get_distinct_groups(tmp_path: Path) -> None:
    """scripts/r4.py: home_page.png and home-page.png."""
    a, b = "screenshots/home_page.png", "screenshots/home-page.png"
    assert media_group(a) != media_group(b)
    (tmp_path / "index.html").write_text(f"<img src='{a}?sha=a'><img src='{b}?sha=b'>")
    groups, unrendered = image_groups(
        tmp_path, {"images": [{"path": a, "sha256": "a"}, {"path": b, "sha256": "b"}]}
    )
    assert len(groups) == 2 and not unrendered


def test_r4_a_collision_is_refused_at_assembly(
    make_context: Callable[..., Context], studio_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bundle_module, "media_group", lambda rel: "launch-same")
    ctx = make_context(**preview_ctx(studio_root))
    with pytest.raises(BundleRefused, match="share D7 media group"):
        run_kit(
            ctx, write_product(studio_root, capture=False), KitOptions(video=False), public_client()
        )


# ---- the /publication answer is checked exactly as qontinui-web serves it -----------------


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"extra": 1}, "not the /publication shape"),
        ({"public_slug": None}, "not the /publication shape"),
        ({"public_slug": "someone-else"}, "answered for 'someone-else'"),
        ({"version": "1"}, "not the /publication shape"),
        ({"content_sha256": "ABC"}, "not the /publication shape"),
        ({"owned_by_caller_tenant": False}, "must carry no state"),
    ],
)
def test_publication_shape_is_strict(change: dict[str, object], message: str) -> None:
    from qontinui_studio.launch.record import BuildRecordInvalid

    digest = canonical_sha256(document())
    answer: dict[str, object] = {
        "public_slug": "qontinui-studio",
        "owned_by_caller_tenant": True,
        "unpublished": False,
        "version": 1,
        "content_sha256": digest,
    }
    if change.get("public_slug", "") is None:
        del answer["public_slug"]
    else:
        answer.update(change)
    client = public_client(publication=answer)
    with pytest.raises(BuildRecordInvalid, match=message):
        client.publication("qontinui-studio")


def test_a_repo_left_out_of_the_record_is_never_named(
    make_context: Callable[..., Context], studio_root: Path
) -> None:
    """The product declares a repo coord could not prove public (so the record
    leaves it out); a title naming it must not be launched (qontinui-web's
    excluded-names scan, ported)."""
    from qontinui_studio.launch.record import BuildRecordInvalid

    ctx = make_context(**preview_ctx(studio_root))
    product_path = write_product(studio_root, capture=False)
    product_path.write_text(
        product_path.read_text().replace(
            "repos: [qontinui/qontinui-studio]",
            "repos: [qontinui/qontinui-studio, qontinui/secret-billing]",
        )
    )
    doc = document()
    doc["work_units"][0]["title"] = "port the secret-billing engine"
    with pytest.raises(BuildRecordInvalid, match="excluded as not known public"):
        run_kit(ctx, product_path, KitOptions(video=False), public_client(doc))
