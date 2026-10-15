"""The second independent review of the launch kit (65683c7): each finding
reproduced as a regression test (scratchpad launch-rev2/rt/test_rev2_*.py)."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest
from click.testing import CliRunner

from qontinui_studio.cli import main
from qontinui_studio.errors import ConfigError
from qontinui_studio.launch.kit import KitOptions, run_kit
from qontinui_studio.launch.product import load_product
from qontinui_studio.launch.publish import publish_launch
from qontinui_studio.pipeline import Context

from .launch_support import (
    SLUG,
    document,
    launch_preview,
    preview_ctx,
    preview_root,
    public_client,
    use_public_door,
    write_product,
)


@pytest.fixture(autouse=True)
def _public_door(monkeypatch: pytest.MonkeyPatch) -> None:
    use_public_door(monkeypatch)


@pytest.fixture(autouse=True)
def _no_bearers(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "STUDIO_COORD_BEARER",
        "STUDIO_COORD_BEARER_FILE",
        "STUDIO_WEB_BEARER",
        "STUDIO_WEB_BEARER_FILE",
    ):
        monkeypatch.delenv(name, raising=False)


def _published(ctx: Context, studio_root: Path) -> tuple[str, Path]:
    product_path = write_product(studio_root, capture=False)
    run = run_kit(ctx, product_path, KitOptions(video=False), public_client(), publish=True)
    assert run.publish is not None and run.publish.published, run.publish.failures
    return run.launch_id, product_path


# ---- finding 1: --out onto a live/locked/owned directory ------------------------------


def test_rev2_out_onto_the_live_publish_dir_is_refused(
    make_context: Callable[..., Context], studio_root: Path, tmp_path: Path
) -> None:
    ctx = make_context(**preview_ctx(studio_root))
    _, product_path = _published(ctx, studio_root)
    live = studio_root / "published-launch"
    before = {p.name: p.read_bytes() for p in live.iterdir() if p.is_file()}
    doc = document()
    doc["gates"]["registered"] = 9999
    rec = tmp_path / "rec.json"
    rec.write_text(json.dumps(doc))
    root = ["--root", str(studio_root)]
    for out in (live, preview_root(studio_root) / "launch", live / "sub"):
        r = CliRunner().invoke(
            main,
            [*root, "launch", "build", str(product_path), "--record-file", str(rec),
             "--no-generate", "--no-capture", "--no-video", "--out", str(out)],
        )  # fmt: skip
        # (`studio` prints a StudioError as "error: ..." and exits 1; CliRunner
        # invokes the group directly, so the typed error is on r.exception.)
        assert r.exit_code == 1 and isinstance(r.exception, ConfigError), r.output
        assert "refusing" in str(r.exception)
    after = {p.name: p.read_bytes() for p in live.iterdir() if p.is_file()}
    assert after == before


def test_rev2_out_onto_a_site_dir_or_a_locked_dir_is_refused(
    make_context: Callable[..., Context], studio_root: Path, tmp_path: Path
) -> None:
    ctx = make_context(**preview_ctx(studio_root))
    product_path = write_product(studio_root, capture=False)
    site = next(iter(ctx.sites.values()))
    with pytest.raises(ConfigError, match="overlaps site"):
        run_kit(
            ctx, product_path, KitOptions(video=False, out_dir=site.publish_dir), public_client()
        )
    locked = tmp_path / "locked"
    locked.mkdir()
    (locked / ".studio.lock").write_text("")
    (locked / "launch-manifest.json").write_text(json.dumps({"schema": "launch-bundle/1"}))
    with pytest.raises(ConfigError, match=".studio.lock"):
        run_kit(ctx, product_path, KitOptions(video=False, out_dir=locked), public_client())


def test_rev2_out_onto_another_launchs_bundle_is_refused(
    make_context: Callable[..., Context], studio_root: Path
) -> None:
    ctx = make_context(**preview_ctx(studio_root))
    product_path = write_product(studio_root, capture=False)
    a = run_kit(ctx, product_path, KitOptions(video=False), public_client())
    with pytest.raises(ConfigError, match="refusing --out"):
        run_kit(
            ctx, product_path, KitOptions(video=False, out_dir=a.bundle.out_dir), public_client()
        )


# ---- finding 2: bytes changed between the gates and the swap ----------------------------


@pytest.mark.parametrize("victim", ["how-it-was-built.html", "launch-manifest.json"])
def test_rev2_bundle_changed_during_d7_never_goes_live(
    make_context: Callable[..., Context], studio_root: Path, victim: str
) -> None:
    fake = launch_preview(studio_root)
    ctx = make_context(bridge=fake.bridge, fetch=fake.fetch)
    product_path = write_product(studio_root, capture=False)
    a = run_kit(ctx, product_path, KitOptions(video=False), public_client())
    bundle = a.bundle.out_dir
    swapped: list[str] = []

    def fetch(url: str) -> bytes:
        body = fake.fetch(url)
        if not swapped:
            swapped.append(url)
            # A concurrent kit cannot rebuild a registered bundle...
            with pytest.raises(ConfigError):
                run_kit(ctx, product_path, KitOptions(video=False, out_dir=bundle), public_client())
            # ...so the attacker edits the file directly.
            path = bundle / victim
            path.write_bytes(path.read_bytes().replace(b"qontinui-studio", b"QONTINUI-STUDIO"))
        return body

    res = publish_launch(
        ctx.conn, a.launch_id, load_product(product_path, studio_root), ctx.notifier, ctx.clock,
        fake.bridge, fetch, studio_root, ctx.sleep, records=public_client(),
    )  # fmt: skip
    assert not res.published
    integrity = [g for g in res.failures if g.gate == "integrity"]
    assert integrity and (
        "not the bytes the gates judged" in integrity[0].detail or "changed" in integrity[0].detail
    )
    live = studio_root / "published-launch"
    assert not (live / "index.html").exists()


def test_rev2_bundle_dir_is_locked_while_publishing(
    make_context: Callable[..., Context], studio_root: Path
) -> None:
    ctx = make_context(**preview_ctx(studio_root))
    launch_id, _ = _published(ctx, studio_root)
    row = ctx.conn.execute("SELECT bundle_dir FROM launches WHERE id = ?", (launch_id,)).fetchone()
    assert (Path(row["bundle_dir"]) / ".studio.lock").exists()


# ---- finding 3: the snapshot must be the operator's tenant's ----------------------------


def test_rev2_a_squatted_slug_is_refused(
    make_context: Callable[..., Context], studio_root: Path
) -> None:
    """Another tenant published under our slug first: our tenant's listing does
    not have the slug public, so the launch refuses."""
    ctx = make_context(**preview_ctx(studio_root))
    product_path = write_product(studio_root, capture=False)
    doc = document()
    doc["product"]["title"] = "Squatted"
    squat = public_client(
        doc,
        publication={
            "public_slug": SLUG,
            "owned_by_caller_tenant": False,
            "unpublished": False,
            "version": None,
            "content_sha256": None,
        },
    )
    run = run_kit(ctx, product_path, KitOptions(video=False), squat, publish=True)
    assert run.publish is not None and not run.publish.published
    tenant = next(g for g in run.publish.failures if g.gate == "d1_tenant")
    assert "owned by another tenant" in tenant.detail
    assert not (studio_root / "published-launch" / "how-it-was-built.html").exists()


def test_rev2_without_a_bearer_a_pin_is_required(
    make_context: Callable[..., Context], studio_root: Path
) -> None:
    from qontinui_studio.launch.record import canonical_sha256

    ctx = make_context(**preview_ctx(studio_root))
    product_path = write_product(studio_root, capture=False)
    no_bearer = public_client(bearer=None)
    run = run_kit(ctx, product_path, KitOptions(video=False), no_bearer, publish=True)
    assert run.publish is not None and not run.publish.published
    tenant = next(g for g in run.publish.failures if g.gate == "d1_tenant")
    assert "pins no record.expected_sha256" in tenant.detail

    def pin(sha: str) -> None:
        text = product_path.read_text()
        product_path.write_text(
            text.replace(
                "record: {door: public, public_url: 'https://build.example.test/qontinui-studio'}",
                "record: {door: public, public_url: 'https://build.example.test/qontinui-studio', "
                f"expected_sha256: '{sha}'}}",
            )
        )

    pin("0" * 64)
    ctx.clock.sleep(1)
    run = run_kit(ctx, product_path, KitOptions(video=False), no_bearer, publish=True)
    assert run.publish is not None and not run.publish.published
    assert (
        "pinned content_sha256"
        in next(g for g in run.publish.failures if g.gate == "d1_tenant").detail
    )

    product_path.write_text(
        product_path.read_text().replace("0" * 64, canonical_sha256(document()))
    )
    ctx.clock.sleep(1)
    run = run_kit(ctx, product_path, KitOptions(video=False), no_bearer, publish=True)
    assert run.publish is not None and run.publish.published, run.publish.failures


# ---- finding 5: a retracted (or superseded) record ---------------------------------------


@pytest.mark.parametrize("change", ["retracted", "new-version", "new-content"])
def test_rev2_a_record_no_longer_current_does_not_publish(
    make_context: Callable[..., Context], studio_root: Path, change: str
) -> None:
    ctx = make_context(**preview_ctx(studio_root))
    product_path = write_product(studio_root, capture=False)
    run = run_kit(ctx, product_path, KitOptions(video=False), public_client())
    if change == "retracted":
        now = public_client(public_status=404)
    elif change == "new-version":
        now = public_client(version=2)
    else:
        doc = document()
        doc["gates"]["registered"] = 4
        now = public_client(doc)
    res = publish_launch(
        ctx.conn, run.launch_id, load_product(product_path, studio_root), ctx.notifier,
        ctx.clock, ctx.bridge, ctx.fetch, studio_root, ctx.sleep, records=now,
    )  # fmt: skip
    assert not res.published
    current = next(g for g in res.failures if g.gate == "d1_current")
    expected = "no longer available" if change == "retracted" else "rebuild it"
    assert expected in current.detail
    assert not (studio_root / "published-launch" / "build-record.json").exists()


# ---- finding 6: the remaining CLI verbs ---------------------------------------------------


def test_cli_launch_run_publishes(
    make_context: Callable[..., Context], studio_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from qontinui_studio import cli
    from qontinui_studio.launch import kit as kit_module

    fake = launch_preview(studio_root)
    real = cli._context

    def _ctx(root: Path, generate: bool = False) -> Context:
        ctx = real(root, generate)
        ctx.bridge, ctx.fetch = fake.bridge, fake.fetch
        return ctx

    monkeypatch.setattr(cli, "_context", _ctx)
    monkeypatch.setattr(kit_module, "BuildRecordClient", lambda **kw: public_client())
    product_path = write_product(studio_root, capture=False)
    r = CliRunner().invoke(
        main,
        ["--root", str(studio_root), "launch", "run", str(product_path), "--no-video",
         "--no-generate", "--no-capture"],
    )  # fmt: skip
    assert r.exit_code == 0, r.output
    assert "pass            d1_tenant" in r.output and "pass            d7_images" in r.output
    assert (studio_root / "published-launch" / "index.html").is_file()


def test_cli_launch_record(monkeypatch: pytest.MonkeyPatch) -> None:
    from qontinui_studio import cli

    monkeypatch.setattr(cli, "BuildRecordClient", lambda **kw: public_client())
    r = CliRunner().invoke(main, ["launch", "record", SLUG])
    assert r.exit_code == 0, r.output
    assert "build-record/1" in r.output and "Pull requests landed" in r.output
    r = CliRunner().invoke(main, ["launch", "record", SLUG, "--json"])
    assert json.loads(r.output)["product"]["slug"] == SLUG
    monkeypatch.setattr(cli, "BuildRecordClient", lambda **kw: public_client(public_status=404))
    r = CliRunner().invoke(main, ["launch", "record", SLUG])
    assert r.exit_code == 4 and "no build record yet (not_found)" in r.output
