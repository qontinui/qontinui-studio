"""``studio library approve|reject`` and ``studio library export``."""

from __future__ import annotations

import os
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml
from click.testing import CliRunner

from qontinui_studio import cli
from qontinui_studio.cli import main
from qontinui_studio.errors import ApprovalRefused
from qontinui_studio.library import Library, export_yaml
from qontinui_studio.pipeline import Context, run_brief
from qontinui_studio.store import connect
from qontinui_studio.workspace import Workspace

from .library_support import DROP, do_import, entry, make_inbox

TODAY = date(2026, 10, 10)


def _lib(studio_root: Path) -> Library:
    return Library(connect(Workspace(studio_root).db_path))


def _import_one(studio_root: Path, tmp_path: Path, **fields: Any) -> str:
    name = f"inbox-{len(list(tmp_path.glob('inbox-*')))}"
    return do_import(studio_root, make_inbox(tmp_path, [entry(**fields)], name=name)).imported[0]


# ---- approve / reject -----------------------------------------------------------


def test_approve_records_who_and_utc_today(studio_root: Path, tmp_path: Path) -> None:
    uid = _import_one(studio_root, tmp_path)
    asset, changed = _lib(studio_root).decide(uid, "approved", "josh", TODAY)
    assert changed
    assert (asset.approval_status, asset.approval_by, asset.approval_on) == (
        "approved",
        "josh",
        "2026-10-10",
    )


@pytest.mark.parametrize(
    "fields",
    [{"commercial_use": "no"}, {"commercial_use": "unknown"}, {"tos_reviewed": DROP}],
    ids=["no", "unknown", "yes-but-unreviewed"],
)
def test_approve_refuses_a_non_commercial_licence(
    studio_root: Path, tmp_path: Path, fields: dict[str, Any]
) -> None:
    uid = _import_one(studio_root, tmp_path, **fields)
    with pytest.raises(ApprovalRefused, match=r"cannot be approved: licence_commercial_use is"):
        _lib(studio_root).decide(uid, "approved", "josh", TODAY)
    assert _lib(studio_root).asset(uid).approval_status == "candidate"


@pytest.mark.parametrize("alias", ["josh@example.com", "josh spinak", "", "jo/sh"])
def test_decide_refuses_a_reviewer_that_is_not_an_alias(
    studio_root: Path, tmp_path: Path, alias: str
) -> None:
    uid = _import_one(studio_root, tmp_path)
    with pytest.raises(ApprovalRefused, match="is not an alias"):
        _lib(studio_root).decide(uid, "rejected", alias, TODAY)


def test_decide_refuses_an_unknown_asset(studio_root: Path) -> None:
    with pytest.raises(ApprovalRefused, match="no asset 'ghost'"):
        _lib(studio_root).decide("ghost", "approved", "josh", TODAY)


def test_reject_any_licence_then_approve_and_repeat_is_a_noop(
    studio_root: Path, tmp_path: Path
) -> None:
    lib = _lib(studio_root)
    noncommercial = _import_one(studio_root, tmp_path, commercial_use="no")
    asset, changed = lib.decide(noncommercial, "rejected", "josh", TODAY)
    assert changed and asset.approval_status == "rejected"

    uid = _import_one(studio_root, tmp_path, asset_id="other")
    lib.decide(uid, "rejected", "josh", TODAY)
    asset, changed = lib.decide(uid, "approved", "kim", date(2026, 10, 11))
    assert changed and (asset.approval_by, asset.approval_on) == ("kim", "2026-10-11")
    asset, changed = lib.decide(uid, "approved", "someone-else", date(2026, 10, 12))
    assert not changed and asset.approval_by == "kim"


def test_cli_approve_and_reject(
    studio_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "utc_now", lambda: datetime(2026, 10, 10, 23, 30, tzinfo=UTC))
    runner = CliRunner()
    root = ["--root", str(studio_root)]
    good = _import_one(studio_root, tmp_path)
    bad = _import_one(studio_root, tmp_path, asset_id="nc", commercial_use="no")

    r = runner.invoke(main, [*root, "library", "approve", good, "--by", "josh"])
    assert r.exit_code == 0, r.output
    assert f"{good}: approved by josh on 2026-10-10" in r.output
    r = runner.invoke(main, [*root, "library", "approve", good, "--by", "josh"])
    assert "already approved" in r.output

    r = runner.invoke(main, [*root, "library", "approve", bad, "--by", "josh"])
    assert r.exit_code == 1 and "(D5)" in r.output
    r = runner.invoke(main, [*root, "library", "reject", bad, "--by", "josh@example.com"])
    assert r.exit_code == 1 and "not an alias" in r.output
    r = runner.invoke(main, [*root, "library", "reject", bad, "--by", "josh"])
    assert r.exit_code == 0 and f"{bad}: rejected by josh" in r.output
    r = runner.invoke(main, [*root, "library", "list"])
    assert "approval=approved" in r.output and "approval=rejected" in r.output


def test_generated_batches_publish_without_approval(
    make_context: Callable[..., Context], write_brief: Callable[..., Path]
) -> None:
    """D7: approval is library metadata, not a publish gate. A generated batch
    publishes on the automated gates while its assets are still candidates, and
    a generated asset can then be approved."""
    ctx = make_context()
    outcome = run_brief(ctx, write_brief())
    assert outcome.status == "published", outcome
    lib = Library(ctx.conn)
    assets = lib.assets(outcome.batch_id)
    assert assets and all(a.approval_status == "candidate" for a in assets)
    asset, _ = lib.decide(assets[0].uid, "approved", "josh", TODAY)
    assert asset.approval_status == "approved"


# ---- export ------------------------------------------------------------------------


def _strings(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for k, v in value.items() for s in (*_strings(k), *_strings(v))]
    if isinstance(value, list):
        return [s for v in value for s in _strings(v)]
    return []


@pytest.fixture
def populated(
    make_context: Callable[..., Context],
    write_brief: Callable[..., Path],
    studio_root: Path,
    tmp_path: Path,
) -> Context:
    ctx = make_context()
    assert run_brief(ctx, write_brief(variants=2)).status == "published"
    _import_one(studio_root, tmp_path, asset_id="zz-last")
    _import_one(
        studio_root,
        tmp_path,
        asset_id="aa-first",
        reference_images=["zz-last", {"url": "https://example.test/r.png", "licence": "CC0"}],
        status="approved",
        approved_by="josh",
        approved_on="2026-10-03",
    )
    return ctx


def test_export_is_deterministic_and_complete(populated: Context, studio_root: Path) -> None:
    first = export_yaml(populated.conn)
    second = export_yaml(connect(Workspace(studio_root).db_path))
    assert first == second
    doc = yaml.safe_load(first)
    assert doc["format"] == "qontinui-studio/library-export" and doc["version"] == 1
    assets = doc["assets"]
    keys = [(a["asset_id"], a["created_at"], a["uid"]) for a in assets]
    assert keys == sorted(keys)
    assert assets[0]["asset_id"] == "aa-first" and assets[-1]["asset_id"] == "zz-last"

    imported = assets[0]
    assert imported["origin"] == "imported" and imported["batch_id"] == "import:inbox-1"
    assert imported["account"] == "studio-mj.1" and imported["generated_on"] == "2026-10-01"
    assert imported["reference_images"] == [
        "zz-last",
        {"licence": "CC0", "url": "https://example.test/r.png"},
    ]
    assert imported["licence"] == {
        "commercial_use": "yes",
        "source": "https://example.test/terms",
        "terms_url": "https://example.test/terms",
        "tos_reviewed": "2026-10-02",
    }
    assert imported["approval"] == {"by": "josh", "on": "2026-10-03", "status": "approved"}
    assert imported["mime"] == "image/png" and imported["ai_generated"] is True
    assert imported["variants"] == []
    assert len(imported["sha256"]) == 64 and (imported["width"], imported["height"]) == (12, 8)

    generated = [a for a in assets if a["origin"] == "generated"]
    assert len(generated) == 2
    for a in generated:
        assert a["provider"] == "fake" and a["approval"]["status"] == "candidate"
        assert a["variants"], a
        for v in a["variants"]:
            assert set(v) == {
                "id",
                "site",
                "slot",
                "campaign",
                "format",
                "width",
                "height",
                "sha256",
            }
            assert len(v["sha256"]) == 64
        order = [
            (v["site"] or "", v["slot"] or "", v["campaign"] or "", v["format"])
            for v in a["variants"]
        ]
        assert order == sorted(order)


def test_export_carries_no_paths_and_no_binaries(
    populated: Context, studio_root: Path, tmp_path: Path
) -> None:
    text = export_yaml(populated.conn).decode("utf-8")
    assert str(tmp_path) not in text and str(studio_root) not in text
    assert ".studio" not in text and "raw_path" not in text and "!!binary" not in text
    for value in _strings(yaml.safe_load(text)):
        assert not os.path.isabs(value), value
        assert not value.startswith(("/", "\\")), value


def test_cli_export_is_byte_identical_on_rerun(populated: Context, studio_root: Path) -> None:
    runner = CliRunner()
    out = studio_root / "export" / "library.yaml"
    r = runner.invoke(main, ["--root", str(studio_root), "library", "export", "--out", str(out)])
    assert r.exit_code == 0, r.output
    first = out.read_bytes()
    r = runner.invoke(main, ["--root", str(studio_root), "library", "export", "--out", str(out)])
    assert r.exit_code == 0 and out.read_bytes() == first
    assert first == export_yaml(populated.conn)


def test_export_of_an_empty_library(studio_root: Path) -> None:
    doc = yaml.safe_load(export_yaml(connect(Workspace(studio_root).db_path)))
    assert doc == {"assets": [], "format": "qontinui-studio/library-export", "version": 1}


def test_cli_list_filters_by_approval_status_and_origin(
    studio_root: Path,
    tmp_path: Path,
    make_context: Callable[..., Context],
    write_brief: Callable[..., Path],
) -> None:
    """A consumer project picks usable assets with ``--status``/``--origin``."""
    approved = _import_one(studio_root, tmp_path)
    rejected = _import_one(studio_root, tmp_path, asset_id="nc", commercial_use="no")
    candidate = _import_one(studio_root, tmp_path, asset_id="later")
    lib = _lib(studio_root)
    lib.decide(approved, "approved", "josh", TODAY)
    lib.decide(rejected, "rejected", "josh", TODAY)
    ctx = make_context()
    run_brief(ctx, write_brief(), publish=False)
    generated = {a.uid for a in Library(ctx.conn).assets() if a.origin == "generated"}
    assert generated

    runner = CliRunner()
    root = ["--root", str(studio_root)]

    def uids(*args: str) -> set[str]:
        r = runner.invoke(main, [*root, "library", "list", *args])
        assert r.exit_code == 0, r.output
        return {line.split()[0] for line in r.output.splitlines() if line.strip()}

    assert uids("--status", "approved") == {approved}
    assert uids("--status", "rejected") == {rejected}
    assert uids("--status", "approved", "--status", "candidate") == {
        approved,
        candidate,
        *generated,
    }
    assert uids("--origin", "imported") == {approved, rejected, candidate}
    assert uids("--origin", "generated") == generated
    assert uids("--origin", "generated", "--status", "approved") == set()
    r = runner.invoke(main, [*root, "library", "list", "--status", "maybe"])
    assert r.exit_code == 2
