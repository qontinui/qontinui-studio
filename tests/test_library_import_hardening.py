"""Regression tests for the independent review of the import at 3ab95e5:
YAML alias bomb, deep nesting, aggregate memory, a parent directory swapped
for a symlink mid-import, a file swapped for a FIFO, and schema/code drift.
Each is ported from the reviewer's repro script."""

from __future__ import annotations

import errno
import json
import os
import signal
import sqlite3
import time
import tracemalloc
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import jsonschema
import pytest
import yaml
from PIL import Image

from qontinui_studio import inbox as inbox_mod
from qontinui_studio.errors import ImportRefused
from qontinui_studio.store import connect
from qontinui_studio.workspace import Workspace

from .library_support import do_import, entry, make_inbox, write_image

REPO = Path(__file__).resolve().parent.parent
posix_only = pytest.mark.skipif(not inbox_mod._DIR_FD, reason="needs dir_fd + O_NOFOLLOW (POSIX)")


def _refused(studio_root: Path, inbox: Path) -> list[str]:
    with pytest.raises(ImportRefused) as info:
        do_import(studio_root, inbox)
    return info.value.errors


def _no_staging_left(studio_root: Path) -> None:
    staging = Workspace(studio_root).raw_dir / "imported" / ".staging"
    assert not staging.exists() or not any(staging.iterdir())


@pytest.fixture
def alarm() -> Iterator[None]:
    """Turn a hang into a failure (POSIX)."""
    if not hasattr(signal, "SIGALRM"):
        pytest.skip("needs SIGALRM")

    def boom(*_: object) -> None:
        raise TimeoutError("the import hung")

    old = signal.signal(signal.SIGALRM, boom)
    signal.alarm(10)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old)


# ---- r1: YAML alias bomb --------------------------------------------------------


def _alias_bomb(depth: int) -> str:
    lines = ["x0: &a0 [lol,lol,lol,lol,lol,lol,lol,lol,lol,lol]"]
    for i in range(1, depth + 1):
        lines.append(f"x{i}: &a{i} [" + ",".join([f"*a{i - 1}"] * 10) + "]")
    anchors = "\n".join("  " + line for line in lines)
    return (
        "- "
        + anchors.lstrip()
        + "\n- file: hero.png\n  asset_id: cafe-hero\n  provider: mj\n  model: v7\n"
        "  account: a1\n  prompt: p\n  generated_on: 2026-10-01\n"
        "  licence_terms_url: https://example.test/terms\n  commercial_use: 'no'\n"
        f"  watermark: none\n  ai_generated: true\n  approved_by: *a{depth}\n"
    )


def test_r1_yaml_alias_bomb_is_refused_fast(studio_root: Path, tmp_path: Path) -> None:
    inbox = make_inbox(tmp_path)
    (inbox / "manifest.yaml").write_text(_alias_bomb(7))  # 10^8 'lol' if expanded
    started = time.monotonic()
    errors = _refused(studio_root, inbox)
    assert time.monotonic() - started < 5
    assert len(errors) == 1 and "anchors and aliases are not allowed" in errors[0], errors
    assert sum(len(e) for e in errors) < 2000


@pytest.mark.parametrize(
    "text",
    [
        "- &x {a: 1}\n",
        "- {a: &x 1, b: *x}\n",
        "- {<<: {file: a.png}, asset_id: a}\n",
    ],
    ids=["anchor", "alias", "merge-key"],
)
def test_r1_anchors_aliases_and_merge_keys_are_refused(
    studio_root: Path, tmp_path: Path, text: str
) -> None:
    inbox = make_inbox(tmp_path)
    (inbox / "manifest.yaml").write_text(text)
    assert any("not allowed" in e for e in _refused(studio_root, inbox))


@pytest.mark.parametrize(
    ("field", "value", "shown"),
    [
        ("approved_by", list(range(1000)), "a value of type list"),
        ("account", {"k": "v" * 10_000}, "a value of type dict"),
        ("asset_id", 12345, "a value of type int"),
        ("prompt", ["x"] * 1000, "a value of type list"),
        ("reference_images", "x" * 10_000, "'" + "x" * 80 + "'…"),
    ],
)
def test_r1_non_string_values_never_render_into_messages(
    studio_root: Path, tmp_path: Path, field: str, value: Any, shown: str
) -> None:
    inbox = make_inbox(tmp_path, [entry(**{field: value})])
    errors = _refused(studio_root, inbox)
    joined = "\n".join(errors)
    assert len(joined) < 1000, joined[:300]
    assert shown in joined, errors


def test_r1_manifest_over_1_mib_is_refused_without_parsing(
    studio_root: Path, tmp_path: Path
) -> None:
    inbox = make_inbox(tmp_path)
    (inbox / "manifest.yaml").write_text("# " + "x" * inbox_mod.MAX_MANIFEST_BYTES + "\n[]\n")
    errors = _refused(studio_root, inbox)
    assert errors[0].startswith("manifest.yaml: is ") and "(1 MiB) cap" in errors[0], errors


def test_r1_duplicate_keys_are_refused(studio_root: Path, tmp_path: Path) -> None:
    """Last-wins would let `account: a@b.c` hide behind a later valid alias."""
    inbox = make_inbox(tmp_path)
    text = yaml.safe_dump([entry()], sort_keys=False).replace(
        "- file: hero.png", "- file: hero.png\n  account: a@b.c", 1
    )
    (inbox / "manifest.yaml").write_text(text)
    assert any("duplicate key 'account'" in e for e in _refused(studio_root, inbox))


def test_entry_count_cap(studio_root: Path, tmp_path: Path) -> None:
    inbox = make_inbox(tmp_path, [entry() for _ in range(inbox_mod.MAX_ENTRIES + 1)])
    errors = _refused(studio_root, inbox)
    assert errors == [
        f"manifest.yaml: has {inbox_mod.MAX_ENTRIES + 1} entries, over the "
        f"{inbox_mod.MAX_ENTRIES}-entry cap"
    ]


def test_cap_constants() -> None:
    assert inbox_mod.MAX_MANIFEST_BYTES == 1024 * 1024
    assert inbox_mod.MAX_ENTRIES == 1000
    assert inbox_mod.MAX_TOTAL_BYTES == 2 * 1024 * 1024 * 1024


# ---- r2: deep nesting -------------------------------------------------------------


def test_r2_deep_nesting_is_a_refusal_not_a_crash(studio_root: Path, tmp_path: Path) -> None:
    inbox = make_inbox(tmp_path)
    (inbox / "manifest.yaml").write_text("[" * 5000 + "]" * 5000)
    errors = _refused(studio_root, inbox)
    assert len(errors) == 1 and errors[0].startswith("manifest.yaml: "), errors


# ---- r3: aggregate memory -------------------------------------------------------------


def test_r3_entries_hold_no_file_bytes() -> None:
    names = set(inbox_mod._Entry.__dataclass_fields__)
    assert "data" not in names and {"sha256", "size", "width", "height", "staged"} <= names


def test_r3_one_file_listed_many_times_is_streamed_not_held(
    studio_root: Path, tmp_path: Path
) -> None:
    """The reviewer's repro: one large file under K asset ids, last entry
    invalid. Python-heap peak stays near one chunk, not K x the file."""
    inbox = make_inbox(tmp_path, images={})
    Image.frombytes("RGB", (900, 900), os.urandom(900 * 900 * 3)).save(
        inbox / "big.png", "PNG", compress_level=0
    )
    size = (inbox / "big.png").stat().st_size
    k = 20
    (inbox / "manifest.yaml").write_text(
        yaml.safe_dump([entry(file="big.png", asset_id=f"a{i}") for i in range(k)] + [{"bad": 1}])
    )
    tracemalloc.start()
    try:
        errors = _refused(studio_root, inbox)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert errors and all(e.startswith(f"entry {k}:") for e in errors), errors
    assert peak < 4 * size < k * size, (peak, size)
    _no_staging_left(studio_root)


def test_r3_total_bytes_cap(
    studio_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inbox = make_inbox(
        tmp_path,
        [entry(file="a.png", asset_id="a"), entry(file="b.png", asset_id="b")],
        images={"a.png": (1, 1, 1), "b.png": (2, 2, 2)},
    )
    monkeypatch.setattr(inbox_mod, "MAX_TOTAL_BYTES", (inbox / "a.png").stat().st_size + 10)
    errors = _refused(studio_root, inbox)
    assert len(errors) == 1 and errors[0].startswith("entry 1: file: 'b.png' would take"), errors
    assert "total cap" in errors[0]
    _no_staging_left(studio_root)


def test_r3_successful_import_leaves_no_staging(studio_root: Path, tmp_path: Path) -> None:
    inbox = make_inbox(
        tmp_path,
        [entry(file="a.png"), entry(file="a.png", asset_id="other")],
        images={"a.png": (1, 1, 1)},
    )
    outcome = do_import(studio_root, inbox)
    assert len(outcome.imported) == 2
    _no_staging_left(studio_root)
    lib_rows = (
        connect(Workspace(studio_root).db_path)
        .execute("SELECT DISTINCT raw_path FROM assets")
        .fetchall()
    )
    assert len(lib_rows) == 1 and Path(lib_rows[0][0]).is_file()


def test_r3_refusal_leaves_no_raw_or_staged_files(studio_root: Path, tmp_path: Path) -> None:
    inbox = make_inbox(tmp_path, [entry(), entry(file="hero.png", asset_id="b", account="a@b")])
    _refused(studio_root, inbox)
    _no_staging_left(studio_root)
    imported = Workspace(studio_root).raw_dir / "imported"
    assert not any(p.is_file() for p in imported.rglob("*"))


# ---- r4: parent directory swapped for a symlink -----------------------------------


@posix_only
@pytest.mark.parametrize("when", ["before-walking-sub", "after-opening-sub"])
def test_r4_parent_swap_never_imports_the_outside_file(
    studio_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, when: str
) -> None:
    inbox = make_inbox(tmp_path, [entry(file="sub/hero.png")], images={})
    write_image(inbox / "sub" / "hero.png", (1, 2, 3), size=(12, 8))
    outside = tmp_path / "outside"
    write_image(outside / "hero.png", (250, 0, 0), size=(33, 21))
    real_open = os.open
    swapped = False

    def swap() -> None:
        nonlocal swapped
        if not swapped:
            swapped = True
            os.rename(inbox / "sub", tmp_path / "sub.real")
            os.symlink(outside, inbox / "sub")

    def racing_open(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        if when == "before-walking-sub" and path == "sub":
            swap()
        if when == "after-opening-sub" and path == "hero.png":
            swap()
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(inbox_mod.os, "open", racing_open)
    try:
        outcome = do_import(studio_root, inbox)
    except ImportRefused as e:
        assert when == "before-walking-sub"
        assert any("goes through a symlink (sub)" in m for m in e.errors), e.errors
        return
    assert when == "after-opening-sub"
    # The descriptor held on the real directory pins the INSIDE file.
    row = (
        connect(Workspace(studio_root).db_path)
        .execute("SELECT width, height FROM assets WHERE uid = ?", (outcome.imported[0],))
        .fetchone()
    )
    assert tuple(row) == (12, 8)


# ---- r5: FIFO --------------------------------------------------------------------


@posix_only
def test_r5_file_swapped_for_a_fifo_is_refused_without_hanging(
    studio_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, alarm: None
) -> None:
    inbox = make_inbox(tmp_path)
    real_open = os.open

    def racing_open(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        if path == "hero.png":
            os.unlink(inbox / "hero.png")
            os.mkfifo(inbox / "hero.png")
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(inbox_mod.os, "open", racing_open)
    errors = _refused(studio_root, inbox)
    assert errors == ["entry 0: file: 'hero.png' is not a regular file"]


@posix_only
def test_r5_a_fifo_in_the_inbox_is_refused(studio_root: Path, tmp_path: Path, alarm: None) -> None:
    inbox = make_inbox(tmp_path, [entry(file="pipe.png")], images={})
    os.mkfifo(inbox / "pipe.png")
    assert _refused(studio_root, inbox) == ["entry 0: file: 'pipe.png' is not a regular file"]


@posix_only
def test_r5_manifest_symlink_is_refused(studio_root: Path, tmp_path: Path) -> None:
    inbox = make_inbox(tmp_path)
    real = tmp_path / "elsewhere.yaml"
    (inbox / "manifest.yaml").rename(real)
    os.symlink(real, inbox / "manifest.yaml")
    errors = _refused(studio_root, inbox)
    assert "symlinks are refused" in errors[0], errors


# ---- r8: schema and code agree -------------------------------------------------------


def _validator() -> jsonschema.Draft202012Validator:
    schema = json.loads((REPO / "schemas" / "import-manifest.schema.json").read_text())
    return jsonschema.Draft202012Validator(schema)


DRIFT_CASES = {
    "port_arabic_digits": {"licence_terms_url": "https://example.test:١٢/terms"},
    "tos_arabic_digits": {"tos_reviewed": "٢٠٢٦-١٠-٠٢"},
    "date_arabic_digits": {"generated_on": "٢٠٢٦-١٠-٠١"},
    "prompt_x1c": {"prompt": "\x1c"},
    "prompt_nbsp": {"prompt": " "},
    "prompt_tab_only": {"prompt": "\t"},
    "leap_second": {"generated_on": "2026-06-30T23:59:60Z"},
    "hour_24": {"generated_on": "2026-06-30T24:00:00Z"},
    "asset_id_trailing_nl": {"asset_id": "cafe-hero\n"},
    "account_trailing_nl": {"account": "studio\n"},
    "url_trailing_nl": {"licence_terms_url": "https://example.test/terms\n"},
    "url_unicode_path": {"licence_terms_url": "https://example.test/tërms"},
    "rfc3339_ok": {"generated_on": "2026-06-30T23:59:59.5+02:00"},
    "plain_ok": {},
}


@pytest.mark.parametrize("name", sorted(DRIFT_CASES))
def test_r8_schema_and_code_agree(studio_root: Path, tmp_path: Path, name: str) -> None:
    fields = DRIFT_CASES[name]
    schema_ok = not list(_validator().iter_errors([entry(**fields)]))
    inbox = make_inbox(tmp_path, [entry(**fields)])
    try:
        do_import(studio_root, inbox)
        code_ok = True
    except ImportRefused:
        code_ok = False
    assert schema_ok == code_ok, (name, schema_ok, code_ok)


def test_r8_leap_second_is_refused_by_both() -> None:
    assert list(_validator().iter_errors([entry(generated_on="2026-06-30T23:59:60Z")]))
    assert inbox_mod._date_or_rfc3339("2026-06-30T23:59:60Z") is None


def test_raw_store_rows_survive_a_reopen(studio_root: Path, tmp_path: Path) -> None:
    """Staged files are moved, not copied, into the raw store; the row's file
    exists and matches after the staging dir is gone."""
    uid = do_import(studio_root, make_inbox(tmp_path)).imported[0]
    conn = connect(Workspace(studio_root).db_path)
    raw, sha = conn.execute("SELECT raw_path, sha256 FROM assets WHERE uid = ?", (uid,)).fetchone()
    from qontinui_studio.postprocess import sha256_file

    assert sha256_file(Path(raw)) == sha
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE batches SET status = 'generated' WHERE id = 'import:inbox-1'")


# ---- third review: r11 cross-filesystem, r12 junction fallback, r13 rmdir race ---


def _other_filesystem(tmp_path: Path) -> Path:
    shm = Path("/dev/shm")
    if not shm.is_dir() or shm.stat().st_dev == tmp_path.stat().st_dev:
        pytest.skip("no second filesystem (/dev/shm) here")
    return shm


@posix_only
@pytest.mark.parametrize("layout", ["studio-elsewhere", "raw-elsewhere"])
def test_r11_raw_store_on_another_filesystem(
    studio_root: Path, tmp_path: Path, layout: str
) -> None:
    import shutil
    import tempfile

    target = Path(tempfile.mkdtemp(prefix="studio-test-", dir=_other_filesystem(tmp_path)))
    try:
        if layout == "studio-elsewhere":
            os.symlink(target, studio_root / ".studio")
        else:
            (studio_root / ".studio").mkdir()
            os.symlink(target, studio_root / ".studio" / "raw")
        outcome = do_import(studio_root, make_inbox(tmp_path))
        assert len(outcome.imported) == 1
        _no_staging_left(studio_root)
    finally:
        shutil.rmtree(target, ignore_errors=True)


def test_r11_install_error_is_a_refusal_without_paths(
    studio_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_replace = os.replace

    def exdev(src: Any, dst: Any) -> None:
        if str(dst).endswith(".png"):  # the move into the raw store, not staging
            raise OSError(errno.EXDEV, "Invalid cross-device link", str(src), None, str(dst))
        real_replace(src, dst)

    monkeypatch.setattr(inbox_mod.os, "replace", exdev)
    with pytest.raises(ImportRefused) as info:
        do_import(studio_root, make_inbox(tmp_path))
    text = str(info.value) + "\n".join(info.value.errors)
    assert "EXDEV" in text and str(tmp_path) not in text and "/" not in "".join(info.value.errors)
    assert (
        connect(Workspace(studio_root).db_path).execute("SELECT COUNT(*) FROM assets").fetchone()[0]
        == 0
    )
    _no_staging_left(studio_root)


def test_staging_write_failure_is_reported_as_staging(
    studio_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_open = Path.open

    class Full:
        def __init__(self, f: Any) -> None:
            self.f = f

        def write(self, _: bytes) -> int:
            raise OSError(errno.ENOSPC, "No space left on device")

        def __getattr__(self, name: str) -> Any:
            return getattr(self.f, name)

        def __enter__(self) -> Full:
            return self

        def __exit__(self, *exc: object) -> None:
            self.f.close()

    def opener(self: Path, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        f = real_open(self, mode, *args, **kwargs)
        return Full(f) if self.name.startswith(".partial-") else f

    monkeypatch.setattr(Path, "open", opener)
    errors = _refused(studio_root, make_inbox(tmp_path))
    assert errors == ["entry 0: file: 'hero.png' could not be staged (ENOSPC)"]


def _force_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(inbox_mod, "_DIR_FD", False)


def test_r12_fallback_imports_a_plain_inbox(
    studio_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _force_fallback(monkeypatch)
    inbox = make_inbox(tmp_path, [entry(file="sub/a.png")], images={"sub/a.png": (1, 1, 1)})
    assert len(do_import(studio_root, inbox).imported) == 1


@posix_only
def test_r12_junction_that_is_not_a_symlink_to_pathlib_is_refused(
    studio_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reviewer's simulation: `sub` links outside, and is_symlink() says
    False for it, as it does for a Windows junction."""
    _force_fallback(monkeypatch)
    inbox = make_inbox(tmp_path, [entry(file="sub/hero.png")], images={})
    outside = tmp_path / "outside"
    write_image(outside / "hero.png", (250, 0, 0), size=(33, 21))
    os.symlink(outside, inbox / "sub")
    real = Path.is_symlink
    monkeypatch.setattr(
        Path, "is_symlink", lambda self: False if self.name == "sub" else real(self)
    )
    errors = _refused(studio_root, inbox)
    assert errors == [
        "entry 0: file: 'sub/hero.png' goes through a symlink or junction (sub); links are refused"
    ]


def test_r12_junction_attribute_is_refused(
    studio_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _force_fallback(monkeypatch)
    inbox = make_inbox(tmp_path, [entry(file="sub/a.png")], images={"sub/a.png": (1, 1, 1)})
    real = Path.is_junction
    monkeypatch.setattr(Path, "is_junction", lambda self: self.name == "sub" or real(self))
    assert any("symlink or junction (sub)" in e for e in _refused(studio_root, inbox))


@posix_only
def test_r12_resolve_check_catches_a_link_the_walk_missed(
    studio_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Even if every per-component check were fooled, the opened file must
    resolve inside the inbox."""
    _force_fallback(monkeypatch)
    monkeypatch.setattr(inbox_mod, "_is_link_like", lambda path: False)
    inbox = make_inbox(tmp_path, [entry(file="sub/hero.png")], images={})
    write_image(tmp_path / "outside" / "hero.png", (250, 0, 0), size=(33, 21))
    os.symlink(tmp_path / "outside", inbox / "sub")
    errors = _refused(studio_root, inbox)
    assert errors == ["entry 0: file: 'sub/hero.png' resolves outside the inbox; refused"]


def test_r13_staging_parent_vanishing_is_a_refusal_not_a_crash(
    studio_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reviewer's forced interleaving: the shared parent is removed right
    after it is created, every time."""
    inbox = make_inbox(tmp_path)
    real_mkdir = os.mkdir

    def mkdir(path: Any, *args: Any, **kwargs: Any) -> None:
        real_mkdir(path, *args, **kwargs)
        if str(path).endswith(".staging"):
            os.rmdir(path)

    monkeypatch.setattr(os, "mkdir", mkdir)
    errors = _refused(studio_root, inbox)
    assert len(errors) == 1 and errors[0].startswith("staging: "), errors


def test_r13_an_import_never_removes_the_shared_staging_parent(
    studio_root: Path, tmp_path: Path
) -> None:
    parent = Workspace(studio_root).raw_dir / "imported" / ".staging"
    other_run = parent / "another-import-in-progress"
    other_run.mkdir(parents=True)
    do_import(studio_root, make_inbox(tmp_path))
    _refused(studio_root, make_inbox(tmp_path, [entry(account="a@b")], name="bad"))
    assert parent.is_dir() and other_run.is_dir()


def test_stale_staging_runs_are_swept(studio_root: Path, tmp_path: Path) -> None:
    parent = Workspace(studio_root).raw_dir / "imported" / ".staging"
    stale, fresh = parent / ("a" * 32), parent / ("b" * 32)
    for d in (stale, fresh):
        (d / "partial").parent.mkdir(parents=True)
        (d / "partial").write_bytes(b"x")
    old = time.time() - inbox_mod.STALE_STAGING_S - 60
    os.utime(stale, (old, old))
    do_import(studio_root, make_inbox(tmp_path))
    assert not stale.exists() and fresh.is_dir()


@pytest.mark.parametrize(
    "name",
    ["hero.png/", "sub//a.png", "./hero.png", "sub/./a.png", "sub\\\\a.png", "hero.png\\"],
)
def test_file_paths_must_be_written_plainly(studio_root: Path, tmp_path: Path, name: str) -> None:
    inbox = make_inbox(
        tmp_path, [entry(file=name)], images={"hero.png": (1, 1, 1), "sub/a.png": (2, 2, 2)}
    )
    errors = _refused(studio_root, inbox)
    assert len(errors) == 1 and errors[0].startswith("entry 0: file: "), errors
    assert "separator" in errors[0] or "segment" in errors[0], errors


# ---- fourth review: r14 the stale sweep never follows a link -------------------

OLD_S = 2 * 86400


def _age(path: Path) -> None:
    old = time.time() - OLD_S
    os.utime(path, (old, old), follow_symlinks=False)


@pytest.fixture
def precious(tmp_path: Path) -> Path:
    """An unrelated folder with old data that no import may ever delete."""
    p = tmp_path / "precious"
    (p / "old-project").mkdir(parents=True)
    (p / "old-project" / "data.txt").write_text("keep me")
    _age(p / "old-project")
    return p


def _staging(studio_root: Path) -> Path:
    return Workspace(studio_root).raw_dir / "imported" / ".staging"


def _alive(precious: Path) -> bool:
    return (precious / "old-project" / "data.txt").exists()


@pytest.mark.parametrize("fallback", [False, True], ids=["posix", "windows-fallback"])
def test_r14_s1_symlinked_staging_is_refused_and_never_swept(
    studio_root: Path,
    tmp_path: Path,
    precious: Path,
    monkeypatch: pytest.MonkeyPatch,
    fallback: bool,
) -> None:
    if fallback:
        _force_fallback(monkeypatch)
    old_run = precious / ("c" * 32)  # run-shaped and old: the sweep's exact target
    old_run.mkdir()
    _age(old_run)
    stg = _staging(studio_root)
    stg.parent.mkdir(parents=True)
    os.symlink(precious, stg)
    errors = _refused(studio_root, make_inbox(tmp_path))
    assert len(errors) == 1 and errors[0].startswith("staging: the staging directory is a"), errors
    assert _alive(precious) and old_run.is_dir()


def test_r14_s1_sweep_skips_a_staging_swapped_for_a_symlink(
    studio_root: Path, tmp_path: Path, precious: Path
) -> None:
    """If .staging becomes a symlink after this import started, the sweep
    warns and deletes nothing."""
    old_run = precious / ("c" * 32)
    old_run.mkdir()
    _age(old_run)
    ws = Workspace(studio_root)
    staging = inbox_mod._Staging(ws)
    staging.start()
    stg = _staging(studio_root)
    os.rename(stg, tmp_path / "real-staging")
    os.symlink(precious, stg)
    warnings: list[str] = []
    staging.sweep_stale(time.time(), warnings)
    assert warnings and "sweep skipped" in warnings[0], warnings
    assert old_run.is_dir() and _alive(precious)


def test_r14_s1b_imported_dir_linked_elsewhere_keeps_unrelated_data(
    studio_root: Path, tmp_path: Path, precious: Path
) -> None:
    (Workspace(studio_root).raw_dir).mkdir(parents=True)
    os.symlink(precious, Workspace(studio_root).raw_dir / "imported")
    (precious / ".staging").mkdir()
    os.symlink(precious, precious / ".staging" / "link-entry")
    assert len(do_import(studio_root, make_inbox(tmp_path)).imported) == 1
    assert _alive(precious)


def test_r14_s2_old_symlink_entry_is_not_followed(
    studio_root: Path, tmp_path: Path, precious: Path
) -> None:
    stg = _staging(studio_root)
    stg.mkdir(parents=True)
    link = stg / ("d" * 32)
    os.symlink(precious / "old-project", link)
    _age(link)
    do_import(studio_root, make_inbox(tmp_path))
    assert _alive(precious)


def test_r14_s3_old_run_containing_a_symlink_out(
    studio_root: Path, tmp_path: Path, precious: Path
) -> None:
    stg = _staging(studio_root)
    run = stg / ("e" * 32)
    run.mkdir(parents=True)
    os.symlink(precious / "old-project", run / "x")
    _age(run)
    do_import(studio_root, make_inbox(tmp_path))
    assert not run.exists() and _alive(precious)


@pytest.mark.parametrize("name", ["operator-notes", "E" * 32, "f" * 31, "f" * 33])
def test_r14_s4_old_dir_that_is_not_a_run_is_kept(
    studio_root: Path, tmp_path: Path, name: str
) -> None:
    stg = _staging(studio_root)
    (stg / name).mkdir(parents=True)
    (stg / name / "n.txt").write_text("x")
    _age(stg / name)
    do_import(studio_root, make_inbox(tmp_path))
    assert (stg / name / "n.txt").exists()
