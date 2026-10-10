"""``studio library import``: every validation rule, atomicity, idempotency,
the raw store, and the schema file the operator is handed."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import struct
import zlib
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner
from PIL import Image
from PIL.PngImagePlugin import PngImageFile

from qontinui_studio import inbox as inbox_mod
from qontinui_studio.cli import main
from qontinui_studio.errors import ImportRefused
from qontinui_studio.library import ALIAS_PATTERN, APPROVAL_STATUSES, Library
from qontinui_studio.postprocess import sha256_file
from qontinui_studio.store import connect
from qontinui_studio.workspace import Workspace

from .library_support import DROP, do_import, entry, make_inbox, write_image

REPO = Path(__file__).resolve().parent.parent
SCHEMA_PATH = REPO / "schemas" / "import-manifest.schema.json"


def _library(studio_root: Path) -> Library:
    return Library(connect(Workspace(studio_root).db_path))


def _refused(studio_root: Path, inbox: Path) -> list[str]:
    with pytest.raises(ImportRefused) as info:
        do_import(studio_root, inbox)
    return info.value.errors


def _nothing_written(studio_root: Path) -> None:
    conn = connect(Workspace(studio_root).db_path)
    assert conn.execute("SELECT COUNT(*) FROM assets").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM batches").fetchone()[0] == 0
    raw = Workspace(studio_root).raw_dir / "imported"
    assert not raw.exists() or not any(p.is_file() for p in raw.rglob("*"))


# ---- the happy path -------------------------------------------------------


def test_import_measures_copies_and_records_provenance(studio_root: Path, tmp_path: Path) -> None:
    inbox = make_inbox(
        tmp_path,
        [
            entry(
                reference_images=[
                    {"url": "https://example.test/ref.jpg", "licence": "CC-BY-4.0"},
                ],
                status="approved",
                approved_by="josh",
                approved_on="2026-10-03",
                watermark="invisible",
            )
        ],
    )
    source = inbox / "hero.png"
    outcome = do_import(studio_root, inbox)
    assert outcome.batch_id == "import:inbox-1"
    assert len(outcome.imported) == 1 and not outcome.skipped and not outcome.warnings

    # The inbox is never referenced after import.
    digest = sha256_file(source)
    shutil.rmtree(inbox)
    a = _library(studio_root).asset(outcome.imported[0])
    assert a.uid == f"import-cafe-hero-{digest[:16]}"
    assert a.origin == "imported" and a.batch_id == "import:inbox-1"
    assert a.job_id is None and a.brief_id is None
    raw = Path(a.raw_path)
    assert raw.is_file() and sha256_file(raw) == digest == a.sha256
    assert raw.parent.parent == Workspace(studio_root).raw_dir / "imported"
    assert raw.name == f"{digest}.png"
    assert (a.width, a.height, a.mime) == (12, 8, "image/png")
    assert a.ai_generated is True
    assert (a.provider, a.model, a.account) == ("midjourney", "v7", "studio-mj.1")
    assert a.prompt == "An empty cafe interior in warm light"
    assert a.reference_images == [{"licence": "CC-BY-4.0", "url": "https://example.test/ref.jpg"}]
    assert a.generated_on == "2026-10-01"
    assert a.licence_terms_url == "https://example.test/terms"
    assert (a.licence_commercial_use, a.licence_tos_reviewed) == ("yes", "2026-10-02")
    assert (a.watermark, a.watermark_visible) == ("invisible", False)
    assert (a.approval_status, a.approval_by, a.approval_on) == ("approved", "josh", "2026-10-03")
    batch = (
        connect(Workspace(studio_root).db_path)
        .execute("SELECT * FROM batches WHERE id = 'import:inbox-1'")
        .fetchone()
    )
    assert (batch["origin"], batch["status"], batch["no_publish"]) == ("imported", "imported", 1)


def test_format_is_measured_not_taken_from_the_extension(studio_root: Path, tmp_path: Path) -> None:
    inbox = make_inbox(tmp_path, [entry(file="really-a-jpeg.png")], images={})
    jpeg = write_image(tmp_path / "x.jpg", size=(20, 10))
    shutil.copyfile(jpeg, inbox / "really-a-jpeg.png")
    a = _library(studio_root).asset(do_import(studio_root, inbox).imported[0])
    assert (a.mime, a.width, a.height) == ("image/jpeg", 20, 10)
    assert a.raw_path.endswith(".jpg")


def test_webp_and_subdirectory_files(studio_root: Path, tmp_path: Path) -> None:
    inbox = make_inbox(
        tmp_path, [entry(file="sub/dir/a.webp")], images={"sub/dir/a.webp": (1, 2, 3)}
    )
    a = _library(studio_root).asset(do_import(studio_root, inbox).imported[0])
    assert a.mime == "image/webp"


def test_several_files_may_share_an_asset_id(studio_root: Path, tmp_path: Path) -> None:
    inbox = make_inbox(
        tmp_path,
        [entry(file="a.png"), entry(file="b.png")],
        images={"a.png": (1, 1, 1), "b.png": (2, 2, 2)},
    )
    outcome = do_import(studio_root, inbox)
    assert len(outcome.imported) == 2
    assert {a.asset_id for a in _library(studio_root).assets("import:inbox-1")} == {"cafe-hero"}


def test_rfc3339_generated_on_and_defaults(studio_root: Path, tmp_path: Path) -> None:
    inbox = make_inbox(
        tmp_path,
        [
            entry(
                file="a.png",
                generated_on="2026-10-01T09:30:00Z",
                reference_images=DROP,
                tos_reviewed=DROP,
                commercial_use="no",
            ),
            entry(file="b.png", generated_on="2026-10-01T09:30:00.123+02:00"),
        ],
        images={"a.png": (1, 1, 1), "b.png": (2, 2, 2)},
    )
    lib = _library(studio_root)
    rows = {lib.asset(u).generated_on: lib.asset(u) for u in do_import(studio_root, inbox).imported}
    assert set(rows) == {"2026-10-01T09:30:00Z", "2026-10-01T09:30:00.123+02:00"}
    a = rows["2026-10-01T09:30:00Z"]
    assert a.reference_images == [] and a.licence_tos_reviewed is None
    assert a.approval_status == "candidate" and a.approval_by is None


def test_reference_to_an_asset_already_in_the_library(studio_root: Path, tmp_path: Path) -> None:
    do_import(studio_root, make_inbox(tmp_path, [entry(asset_id="base-look")], name="first"))
    second = make_inbox(
        tmp_path,
        [entry(asset_id="cafe-hero", reference_images=["base-look"])],
        name="second",
        images={"hero.png": (9, 9, 9)},
    )
    a = _library(studio_root).asset(do_import(studio_root, second).imported[0])
    assert a.reference_images == ["base-look"]


def test_reference_to_another_asset_in_the_same_manifest(studio_root: Path, tmp_path: Path) -> None:
    inbox = make_inbox(
        tmp_path,
        [
            entry(file="a.png", asset_id="base-look"),
            entry(file="b.png", reference_images=["base-look"]),
        ],
        images={"a.png": (1, 1, 1), "b.png": (2, 2, 2)},
    )
    assert len(do_import(studio_root, inbox).imported) == 2


def test_unquoted_yaml_yes_and_dates_are_read_as_strings(studio_root: Path, tmp_path: Path) -> None:
    """YAML 1.1 would read ``yes`` as True and ``2026-10-01`` as a date; the
    manifest loader reads both as the strings the schema describes."""
    inbox = make_inbox(tmp_path)
    (inbox / "manifest.yaml").write_text(
        "- file: hero.png\n"
        "  asset_id: cafe-hero\n"
        "  provider: midjourney\n"
        "  model: v7\n"
        "  account: studio-mj\n"
        "  prompt: A cafe\n"
        "  generated_on: 2026-10-01\n"
        "  licence_terms_url: https://example.test/terms\n"
        "  tos_reviewed: 2026-10-02\n"
        "  commercial_use: yes\n"
        "  watermark: none\n"
        "  ai_generated: true\n"
    )
    a = _library(studio_root).asset(do_import(studio_root, inbox).imported[0])
    assert (a.licence_commercial_use, a.generated_on) == ("yes", "2026-10-01")


# ---- normalisation: recorded with a warning, not refused ------------------


def test_commercial_yes_without_tos_reviewed_is_recorded_unknown(
    studio_root: Path, tmp_path: Path
) -> None:
    outcome = do_import(studio_root, make_inbox(tmp_path, [entry(tos_reviewed=DROP)]))
    assert any("recorded as 'unknown'" in w and w.startswith("entry 0:") for w in outcome.warnings)
    assert _library(studio_root).asset(outcome.imported[0]).licence_commercial_use == "unknown"


@pytest.mark.parametrize(
    "fields",
    [
        {"status": "approved", "approved_on": "2026-10-03"},
        {"status": "approved", "approved_by": "josh"},
        {"status": "approved"},
        {"status": "rejected", "approved_by": "josh"},
    ],
    ids=["approved-no-by", "approved-no-on", "approved-neither", "rejected-no-on"],
)
def test_decision_without_who_and_when_is_recorded_candidate(
    studio_root: Path, tmp_path: Path, fields: dict[str, Any]
) -> None:
    outcome = do_import(studio_root, make_inbox(tmp_path, [entry(**fields)]))
    assert any("recorded as 'candidate'" in w for w in outcome.warnings)
    a = _library(studio_root).asset(outcome.imported[0])
    assert (a.approval_status, a.approval_by, a.approval_on) == ("candidate", None, None)


def test_rejected_with_who_and_when_is_recorded(studio_root: Path, tmp_path: Path) -> None:
    fields = {"status": "rejected", "approved_by": "josh", "approved_on": "2026-10-03"}
    outcome = do_import(studio_root, make_inbox(tmp_path, [entry(commercial_use="no", **fields)]))
    a = _library(studio_root).asset(outcome.imported[0])
    assert (a.approval_status, a.approval_by, a.approval_on) == ("rejected", "josh", "2026-10-03")


# ---- one test per refusal ---------------------------------------------------

REFUSALS: list[Any] = [
    # file
    pytest.param({"file": DROP}, "file: required", id="file-missing"),
    pytest.param({"file": ""}, "file: must be a non-empty relative path", id="file-empty"),
    pytest.param({"file": "/etc/passwd"}, "is absolute", id="file-absolute-posix"),
    pytest.param({"file": "C:/x/hero.png"}, "is absolute", id="file-absolute-drive"),
    pytest.param({"file": "\\\\server\\share\\a.png"}, "is absolute", id="file-absolute-unc"),
    pytest.param({"file": "../outside.png"}, "escapes the inbox", id="file-dotdot"),
    pytest.param({"file": "sub/../../outside.png"}, "escapes the inbox", id="file-dotdot-nested"),
    pytest.param({"file": "nope.png"}, "does not exist", id="file-nonexistent"),
    pytest.param({"file": "adir"}, "is not a regular file", id="file-directory"),
    pytest.param({"file": "renamed.png"}, "not a decodable PNG/JPEG/WebP", id="file-not-an-image"),
    pytest.param({"file": "anim.gif"}, "is GIF, not PNG/JPEG/WebP", id="file-gif"),
    pytest.param({"file": "truncated.png"}, "not a decodable", id="file-truncated"),
    # asset_id
    pytest.param({"asset_id": DROP}, "asset_id: required", id="asset-id-missing"),
    pytest.param({"asset_id": "Cafe_Hero"}, "asset_id: 'Cafe_Hero' must match", id="asset-id-case"),
    pytest.param({"asset_id": "-hero"}, "asset_id: '-hero' must match", id="asset-id-leading-dash"),
    pytest.param({"asset_id": 7}, "asset_id: must be a non-empty string", id="asset-id-not-string"),
    # provider / model / prompt
    pytest.param({"provider": DROP}, "provider: required", id="provider-missing"),
    pytest.param({"provider": "  "}, "provider: must be a non-empty string", id="provider-blank"),
    pytest.param({"model": DROP}, "model: required", id="model-missing"),
    pytest.param({"model": ""}, "model: must be a non-empty string", id="model-empty"),
    pytest.param({"prompt": DROP}, "prompt: required", id="prompt-missing"),
    pytest.param({"prompt": ""}, "prompt: must be a non-empty string", id="prompt-empty"),
    # account
    pytest.param({"account": DROP}, "account: required", id="account-missing"),
    pytest.param({"account": "josh@example.com"}, "never an e-mail", id="account-email"),
    pytest.param({"account": "josh mj"}, "account: 'josh mj' must be an alias", id="account-space"),
    pytest.param({"account": "josh/mj"}, "account: 'josh/mj' must be an alias", id="account-char"),
    # reference_images
    pytest.param(
        {"reference_images": "base"}, "reference_images: must be a list", id="refs-not-list"
    ),
    pytest.param(
        {"reference_images": ["unknown-look"]},
        "'unknown-look' is not in this library",
        id="refs-unknown-id",
    ),
    pytest.param(
        {"reference_images": ["./ref.png"]}, "local paths are refused", id="refs-local-path"
    ),
    pytest.param(
        {"reference_images": ["refs/a"]}, "local paths are refused", id="refs-relative-path"
    ),
    pytest.param({"reference_images": ["cafe-hero"]}, "cannot reference itself", id="refs-self"),
    pytest.param(
        {"reference_images": [{"url": "http://example.test/a.jpg", "licence": "x"}]},
        "url: must be an https URL",
        id="refs-http-url",
    ),
    pytest.param(
        {"reference_images": [{"url": "https://example.test/a.jpg"}]},
        "exactly url and licence",
        id="refs-no-licence",
    ),
    pytest.param(
        {"reference_images": [{"url": "https://example.test/a.jpg", "licence": " "}]},
        "licence: must be a non-empty string",
        id="refs-blank-licence",
    ),
    pytest.param(
        {"reference_images": [{"url": "https://e.test/a", "licence": "x", "path": "/a"}]},
        "exactly url and licence",
        id="refs-extra-key",
    ),
    pytest.param(
        {"reference_images": [3]}, "must be an asset_id or {url, licence}", id="refs-number"
    ),
    # dates
    pytest.param({"generated_on": DROP}, "generated_on: required", id="generated-on-missing"),
    pytest.param(
        {"generated_on": "yesterday"}, "generated_on: must be an ISO date", id="generated-on-word"
    ),
    pytest.param(
        {"generated_on": "2026-13-01"}, "generated_on: must be an ISO date", id="generated-on-month"
    ),
    pytest.param(
        {"generated_on": "2026-10-01T10:00:00"},
        "generated_on: must be an ISO date",
        id="generated-on-naive",
    ),
    pytest.param(
        {"tos_reviewed": "02/10/2026"},
        "tos_reviewed: must be an ISO date",
        id="tos-reviewed-format",
    ),
    pytest.param(
        {"tos_reviewed": None}, "tos_reviewed: must be an ISO date", id="tos-reviewed-null"
    ),
    pytest.param(
        {"approved_by": None}, "approved_by: 'None' must be an alias", id="approved-by-null"
    ),
    # licence
    pytest.param(
        {"licence_terms_url": DROP}, "licence_terms_url: required", id="terms-url-missing"
    ),
    pytest.param(
        {"licence_terms_url": "http://example.test/t"},
        "licence_terms_url: must be an https URL",
        id="terms-url-http",
    ),
    pytest.param(
        {"licence_terms_url": "https://"},
        "licence_terms_url: must be an https URL",
        id="terms-url-no-host",
    ),
    pytest.param(
        {"licence_terms_url": "https://@/"},
        "licence_terms_url: must be an https URL",
        id="terms-url-userinfo-only",
    ),
    pytest.param(
        {"licence_terms_url": "https://:80/"},
        "licence_terms_url: must be an https URL",
        id="terms-url-port-only",
    ),
    pytest.param(
        {"generated_on": "2026-02-30"},
        "generated_on: must be an ISO date",
        id="generated-on-not-a-day",
    ),
    pytest.param({"commercial_use": DROP}, "commercial_use: required", id="commercial-missing"),
    pytest.param(
        {"commercial_use": "maybe"}, "commercial_use: must be one of", id="commercial-bad"
    ),
    pytest.param({"commercial_use": True}, "commercial_use: must be one of", id="commercial-bool"),
    # watermark / ai_generated
    pytest.param({"watermark": DROP}, "watermark: required", id="watermark-missing"),
    pytest.param({"watermark": "faint"}, "watermark: must be one of", id="watermark-bad"),
    pytest.param({"ai_generated": DROP}, "ai_generated: required", id="ai-generated-missing"),
    pytest.param({"ai_generated": False}, "ai_generated: must be true", id="ai-generated-false"),
    pytest.param({"ai_generated": "true"}, "ai_generated: must be true", id="ai-generated-string"),
    # approval
    pytest.param({"status": "pending"}, "status: must be one of", id="status-bad"),
    pytest.param(
        {"status": "approved", "approved_by": "j@x.test", "approved_on": "2026-10-03"},
        "approved_by: 'j@x.test' must be an alias",
        id="approved-by-email",
    ),
    pytest.param(
        {"status": "approved", "approved_by": "j", "approved_on": "Oct 3"},
        "approved_on: must be an ISO date",
        id="approved-on-bad",
    ),
    pytest.param(
        {
            "status": "approved",
            "approved_by": "j",
            "approved_on": "2026-10-03",
            "commercial_use": "no",
        },
        "cannot be approved: commercial_use is 'no'",
        id="approved-noncommercial",
    ),
    pytest.param(
        {
            "status": "approved",
            "approved_by": "j",
            "approved_on": "2026-10-03",
            "tos_reviewed": DROP,
        },
        "cannot be approved: commercial_use is 'unknown'",
        id="approved-unreviewed",
    ),
    # measured fields are never taken from the manifest
    pytest.param({"width": 1}, "width: unknown field", id="width-not-trusted"),
    pytest.param({"sha256": "0" * 64}, "sha256: unknown field", id="sha-not-trusted"),
]


@pytest.mark.parametrize(("overrides", "message"), REFUSALS)
def test_refusal(
    studio_root: Path, tmp_path: Path, overrides: dict[str, Any], message: str
) -> None:
    inbox = make_inbox(tmp_path, [entry(**overrides)])
    write_image(tmp_path / "outside.png")
    (inbox / "adir").mkdir()
    (inbox / "renamed.png").write_text("this is a text file wearing a .png extension")
    write_image(inbox / "anim.gif")
    (inbox / "truncated.png").write_bytes((inbox / "hero.png").read_bytes()[:40])
    errors = _refused(studio_root, inbox)
    assert any(e.startswith("entry 0: ") and message in e for e in errors), errors
    _nothing_written(studio_root)


def _symlink(link: Path, target: Path) -> None:
    try:
        os.symlink(target, link)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable on this platform")


def test_refusal_symlink_escaping_the_inbox(studio_root: Path, tmp_path: Path) -> None:
    inbox = make_inbox(tmp_path, [entry(file="link.png")])
    _symlink(inbox / "link.png", write_image(tmp_path / "secret.png"))
    errors = _refused(studio_root, inbox)
    assert any("symlinks are refused" in e for e in errors), errors
    _nothing_written(studio_root)


def test_refusal_symlinked_directory(studio_root: Path, tmp_path: Path) -> None:
    inbox = make_inbox(tmp_path, [entry(file="d/hero.png")])
    write_image(tmp_path / "elsewhere" / "hero.png")
    _symlink(inbox / "d", tmp_path / "elsewhere")
    assert any("symlink (d)" in e for e in _refused(studio_root, inbox))


def test_refusal_symlink_inside_the_inbox(studio_root: Path, tmp_path: Path) -> None:
    inbox = make_inbox(tmp_path, [entry(file="alias.png")])
    _symlink(inbox / "alias.png", inbox / "hero.png")
    assert any("symlinks are refused" in e for e in _refused(studio_root, inbox))


def test_refusal_hard_link(studio_root: Path, tmp_path: Path) -> None:
    inbox = make_inbox(tmp_path, [entry(file="linked.png")])
    try:
        os.link(write_image(tmp_path / "outside.png"), inbox / "linked.png")
    except OSError:
        pytest.skip("hard links unavailable here")
    assert any("more than one hard link" in e for e in _refused(studio_root, inbox))
    _nothing_written(studio_root)


@pytest.mark.skipif(
    not hasattr(os, "geteuid") or os.geteuid() == 0, reason="needs POSIX permissions, non-root"
)
def test_unreadable_file_is_reported_and_later_entries_still_checked(
    studio_root: Path, tmp_path: Path
) -> None:
    inbox = make_inbox(
        tmp_path,
        [entry(file="a.png"), entry(file="b.png", watermark="faint")],
        images={"a.png": (1, 1, 1), "b.png": (2, 2, 2)},
    )
    (inbox / "a.png").chmod(0)
    try:
        errors = _refused(studio_root, inbox)
    finally:
        (inbox / "a.png").chmod(0o644)
    assert any(e.startswith("entry 0: file: 'a.png' cannot be read") for e in errors), errors
    assert any(e.startswith("entry 1: watermark:") for e in errors), errors


def _png_header_only(path: Path, width: int, height: int) -> Path:
    """A PNG whose IHDR declares ``width`` x ``height`` but carries one tiny
    IDAT: decoding it fully would expand to the declared size."""

    def chunk(kind: bytes, body: bytes) -> bytes:
        return (
            struct.pack(">I", len(body))
            + kind
            + body
            + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(b"\x00" * 64))
        + chunk(b"IEND", b"")
    )
    return path


def test_size_caps_are_the_documented_values() -> None:
    assert inbox_mod.MAX_FILE_BYTES == 50 * 1024 * 1024
    assert inbox_mod.MAX_PIXELS == 100_000_000


def test_refusal_file_over_the_byte_cap(
    studio_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inbox = make_inbox(
        tmp_path,
        [entry(file="small.png"), entry(file="big.png")],
        images={"small.png": (1, 1, 1)},
    )
    write_image(inbox / "big.png", size=(400, 400))
    cap = (inbox / "small.png").stat().st_size + 1
    assert (inbox / "big.png").stat().st_size > cap
    monkeypatch.setattr(inbox_mod, "MAX_FILE_BYTES", cap)
    errors = _refused(studio_root, inbox)
    assert errors == [
        f"entry 1: file: 'big.png' is {(inbox / 'big.png').stat().st_size} bytes, over the {cap}-byte (50 MiB) cap"
    ]
    _nothing_written(studio_root)


def test_refusal_real_file_over_50_mib(studio_root: Path, tmp_path: Path) -> None:
    """At the real cap: a sparse 50 MiB + 1 byte file (no disk cost)."""
    inbox = make_inbox(tmp_path, [entry(file="huge.png")], images={})
    with (inbox / "huge.png").open("wb") as f:
        f.truncate(50 * 1024 * 1024 + 1)
    errors = _refused(studio_root, inbox)
    assert any(e.startswith("entry 0: file: 'huge.png'") and "50 MiB" in e for e in errors)


@pytest.mark.parametrize(
    ("width", "height"),
    [(20_000, 5_001), (40_000, 40_000)],
    ids=["just-over-cap", "past-pillow-bomb-limit"],
)
def test_refusal_pixel_cap_from_the_header_before_decoding(
    studio_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    width: int,
    height: int,
) -> None:
    inbox = make_inbox(tmp_path, [entry(file="bomb.png")], images={})
    _png_header_only(inbox / "bomb.png", width, height)

    def no_decoding(self: object, *args: object, **kwargs: object) -> None:
        raise AssertionError("pixel data decoded before the cap was checked")

    monkeypatch.setattr(Image.Image, "load", no_decoding)
    monkeypatch.setattr(PngImageFile, "verify", no_decoding)
    monkeypatch.setattr(PngImageFile, "load", no_decoding)
    errors = _refused(studio_root, inbox)
    assert len(errors) == 1 and errors[0].startswith(
        "entry 0: file: 'bomb.png' is over the 100000000-pixel (100 megapixel) cap"
    ), errors
    _nothing_written(studio_root)


def test_image_at_the_pixel_cap_is_judged_by_decoding(studio_root: Path, tmp_path: Path) -> None:
    """Exactly 100 MP passes the header check and goes on to the full decode
    (which this header-only file then fails, as a non-decodable image)."""
    inbox = make_inbox(tmp_path, [entry(file="edge.png")], images={})
    _png_header_only(inbox / "edge.png", 20_000, 5_000)
    errors = _refused(studio_root, inbox)
    assert len(errors) == 1 and "not a decodable" in errors[0], errors


def test_refusal_entry_not_a_mapping(studio_root: Path, tmp_path: Path) -> None:
    errors = _refused(studio_root, make_inbox(tmp_path, [entry(), "hero.png"]))
    assert "entry 1: must be a mapping" in errors


def test_refusal_same_bytes_twice_in_one_manifest(studio_root: Path, tmp_path: Path) -> None:
    inbox = make_inbox(tmp_path, [entry(), entry(prompt="other")])
    errors = _refused(studio_root, inbox)
    assert any(e.startswith("entry 1: file: same bytes and asset_id as entry 0") for e in errors)


@pytest.mark.parametrize(
    ("text", "message"),
    [
        pytest.param(None, "manifest.yaml: missing", id="manifest-missing"),
        pytest.param("a: 1\n", "non-empty list", id="manifest-mapping"),
        pytest.param("[]\n", "non-empty list", id="manifest-empty"),
        pytest.param("- [unclosed\n", "manifest.yaml:", id="manifest-bad-yaml"),
        pytest.param(b"- file: \xff\xfe\n", "unreadable", id="manifest-not-utf8"),
    ],
)
def test_refusal_manifest(
    studio_root: Path, tmp_path: Path, text: str | bytes | None, message: str
) -> None:
    inbox = make_inbox(tmp_path)
    if text is None:
        (inbox / "manifest.yaml").unlink()
    elif isinstance(text, bytes):
        (inbox / "manifest.yaml").write_bytes(text)
    else:
        (inbox / "manifest.yaml").write_text(text)
    assert any(message in e for e in _refused(studio_root, inbox))
    _nothing_written(studio_root)


@pytest.mark.parametrize("name", ["Inbox", "my inbox", "-inbox", "in@box"])
def test_refusal_inbox_name(studio_root: Path, tmp_path: Path, name: str) -> None:
    errors = _refused(studio_root, make_inbox(tmp_path, name=name))
    assert any("must match" in e for e in errors)
    _nothing_written(studio_root)


def test_refusal_inbox_not_a_directory(studio_root: Path, tmp_path: Path) -> None:
    with pytest.raises(ImportRefused):
        do_import(studio_root, tmp_path / "absent")


# ---- atomicity --------------------------------------------------------------


def test_one_bad_entry_imports_nothing_and_every_error_is_reported(
    studio_root: Path, tmp_path: Path
) -> None:
    inbox = make_inbox(
        tmp_path,
        [
            entry(file="a.png"),
            entry(file="b.png", account="josh@example.com", watermark="faint"),
            entry(file="c.png"),
            entry(file="missing.png", ai_generated=False),
        ],
        images={"a.png": (1, 1, 1), "b.png": (2, 2, 2), "c.png": (3, 3, 3)},
    )
    errors = _refused(studio_root, inbox)
    assert [e.split(":")[0] for e in errors] == ["entry 1"] * 2 + ["entry 3"] * 2, errors
    _nothing_written(studio_root)


def test_library_reference_error_imports_nothing(studio_root: Path, tmp_path: Path) -> None:
    """The reference check runs against the library under the write lock; it
    still refuses the whole manifest."""
    inbox = make_inbox(
        tmp_path,
        [entry(file="a.png"), entry(file="b.png", reference_images=["ghost"])],
        images={"a.png": (1, 1, 1), "b.png": (2, 2, 2)},
    )
    errors = _refused(studio_root, inbox)
    assert errors == ["entry 1: reference_images[0]: asset_id 'ghost' is not in this library"]
    _nothing_written(studio_root)


def test_a_failure_while_writing_rolls_everything_back(
    studio_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inbox = make_inbox(
        tmp_path,
        [entry(file="a.png"), entry(file="b.png")],
        images={"a.png": (1, 1, 1), "b.png": (2, 2, 2)},
    )
    real = inbox_mod.atomic_write
    calls: list[Path] = []

    def flaky(path: Path, data: bytes) -> None:
        calls.append(path)
        if len(calls) == 2:
            raise OSError("disk full")
        real(path, data)

    monkeypatch.setattr(inbox_mod, "atomic_write", flaky)
    with pytest.raises(OSError):
        do_import(studio_root, inbox)
    conn = connect(Workspace(studio_root).db_path)
    assert conn.execute("SELECT COUNT(*) FROM assets").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM batches").fetchone()[0] == 0
    monkeypatch.setattr(inbox_mod, "atomic_write", real)
    assert len(do_import(studio_root, inbox).imported) == 2


# ---- idempotency --------------------------------------------------------------


def test_reimport_of_identical_content_is_a_noop(studio_root: Path, tmp_path: Path) -> None:
    inbox = make_inbox(tmp_path)
    first = do_import(studio_root, inbox)
    second = do_import(studio_root, inbox)
    assert second.imported == [] and second.skipped == first.imported
    conn = connect(Workspace(studio_root).db_path)
    assert conn.execute("SELECT COUNT(*) FROM assets").fetchone()[0] == 1
    # ... also from another inbox carrying the same bytes under the same id.
    other = make_inbox(tmp_path, name="inbox-2")
    third = do_import(studio_root, other)
    assert third.imported == [] and third.skipped == first.imported
    assert conn.execute("SELECT COUNT(*) FROM batches").fetchone()[0] == 1


def test_reimport_with_changed_details_warns_and_changes_nothing(
    studio_root: Path, tmp_path: Path
) -> None:
    inbox = make_inbox(tmp_path, [entry(commercial_use="no")])
    uid = do_import(studio_root, inbox).imported[0]
    make_inbox(tmp_path, [entry(commercial_use="yes", prompt="fixed prompt")])
    again = do_import(studio_root, inbox)
    assert again.skipped == [uid]
    drift = [w for w in again.warnings if "NOT applied" in w]
    assert drift and "licence_commercial_use" in drift[0] and "prompt" in drift[0]
    assert _library(studio_root).asset(uid).licence_commercial_use == "no"
    # An identical re-import stays silent.
    make_inbox(tmp_path, [entry(commercial_use="no")])
    assert do_import(studio_root, inbox).warnings == []


def test_same_name_different_bytes_is_a_new_row(studio_root: Path, tmp_path: Path) -> None:
    inbox = make_inbox(tmp_path)
    first = do_import(studio_root, inbox)
    write_image(inbox / "hero.png", (0, 0, 255))
    second = do_import(studio_root, inbox)
    assert len(second.imported) == 1 and second.imported != first.imported
    rows = _library(studio_root).assets("import:inbox-1")
    assert len(rows) == 2 and len({r.sha256 for r in rows}) == 2


def test_same_bytes_new_asset_id_is_a_new_row(studio_root: Path, tmp_path: Path) -> None:
    do_import(studio_root, make_inbox(tmp_path))
    again = do_import(studio_root, make_inbox(tmp_path, [entry(asset_id="barber-hero")], "inbox-2"))
    assert len(again.imported) == 1


def test_identity_is_unique_in_the_database(studio_root: Path, tmp_path: Path) -> None:
    uid = do_import(studio_root, make_inbox(tmp_path)).imported[0]
    conn = connect(Workspace(studio_root).db_path)
    conn.execute("CREATE TEMP TABLE twin AS SELECT * FROM assets WHERE uid = ?", (uid,))
    conn.execute("UPDATE twin SET uid = 'another-uid'")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO assets SELECT * FROM twin")


# ---- the CLI ------------------------------------------------------------------


def test_cli_import_ok_and_refused(studio_root: Path, tmp_path: Path) -> None:
    runner = CliRunner()
    root = ["--root", str(studio_root)]
    good = make_inbox(tmp_path, [entry(tos_reviewed=DROP)])
    r = runner.invoke(main, [*root, "library", "import", str(good)])
    assert r.exit_code == 0, r.output
    assert "imported import-cafe-hero-" in r.output
    assert "warning: entry 0: commercial_use 'yes' without tos_reviewed" in r.output
    r = runner.invoke(main, [*root, "library", "import", str(good)])
    assert r.exit_code == 0 and "unchanged import-cafe-hero-" in r.output

    bad = make_inbox(tmp_path, [entry(account="a@b.c"), entry(prompt="")], name="bad")
    r = runner.invoke(main, [*root, "library", "import", str(bad)])
    assert r.exit_code == 1
    assert "entry 0: account:" in r.output and "entry 1: prompt:" in r.output


# ---- the schema file is the contract ---------------------------------------------


def _schema() -> dict[str, Any]:
    return dict(json.loads(SCHEMA_PATH.read_text(encoding="utf-8")))


def test_schema_file_matches_the_validator() -> None:
    schema = _schema()
    defs = schema["$defs"]
    item = defs["entry"]
    assert schema["type"] == "array" and schema["minItems"] == 1
    assert item["additionalProperties"] is False
    assert tuple(item["required"]) == inbox_mod.REQUIRED_FIELDS
    assert set(item["properties"]) == set(inbox_mod.REQUIRED_FIELDS) | set(
        inbox_mod.OPTIONAL_FIELDS
    )
    props = item["properties"]
    assert tuple(props["commercial_use"]["enum"]) == inbox_mod.COMMERCIAL_USE
    assert tuple(props["watermark"]["enum"]) == inbox_mod.WATERMARKS
    assert tuple(props["status"]["enum"]) == APPROVAL_STATUSES
    assert props["ai_generated"]["const"] is True
    assert defs["alias"]["pattern"] == ALIAS_PATTERN
    assert defs["assetId"]["pattern"] == inbox_mod.ASSET_ID_PATTERN
    assert defs["httpsUrl"]["pattern"] == inbox_mod.HTTPS_URL_PATTERN
    assert defs["isoDate"]["pattern"] == inbox_mod.DATE_PATTERN
    assert defs["rfc3339"]["pattern"] == inbox_mod.RFC3339_PATTERN
    for name in ("account", "approved_by"):
        assert props[name]["$ref"] == "#/$defs/alias"
    for name in ("tos_reviewed", "approved_on"):
        assert props[name]["$ref"] == "#/$defs/isoDate"
    assert props["licence_terms_url"]["$ref"] == "#/$defs/httpsUrl"
    assert props["asset_id"]["$ref"] == "#/$defs/assetId"
    refs = props["reference_images"]["items"]["oneOf"]
    assert refs[0]["$ref"] == "#/$defs/assetId"
    assert refs[1]["required"] == ["url", "licence"] and refs[1]["additionalProperties"] is False


def test_schema_validates_the_shipped_example_when_jsonschema_is_available(
    tmp_path: Path,
) -> None:
    """When ``jsonschema`` happens to be installed, the schema itself must
    accept a valid entry and reject the structural refusals."""
    jsonschema = pytest.importorskip("jsonschema")
    validator = jsonschema.Draft202012Validator(_schema())
    assert not list(validator.iter_errors([entry()]))
    for bad in (
        [entry(account="a@b.c")],
        [entry(ai_generated=False)],
        [entry(commercial_use="maybe")],
        [entry(reference_images=["./x.png"])],
        [entry(width=3)],
        [entry(licence_terms_url="https://@/")],
        [entry(generated_on="2026-10-01T10:00:00")],
        [],
    ):
        assert list(validator.iter_errors(bad)), bad
