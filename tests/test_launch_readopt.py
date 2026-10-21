"""``studio launch readopt`` (plan 2026-10-17-build-qontinui-io-factory-hub,
AD3 + AF2) and the product's ``publish.unpublish_command`` (B4).

A hub clone is reset to its landed tree when a publish PR (or a retraction PR)
closes unlanded. The landed bytes are then back on disk with no live launch
row, and ``gate_foreign_file`` would refuse every later publish. ``readopt``
makes the launch whose bytes those are live again — only when the directory
holds exactly its bytes and nothing else, and no launch there is live."""

from __future__ import annotations

import json
import shutil
from collections.abc import Callable
from pathlib import Path

import pytest
from click.testing import CliRunner

from qontinui_studio.cli import main
from qontinui_studio.launch.kit import KitOptions, KitRun, run_kit
from qontinui_studio.launch.publish import ReadoptRefused, readopt_launch, unpublish_launch
from qontinui_studio.pipeline import Context

from .conftest import notice_text
from .launch_support import preview_ctx, public_client, use_public_door, write_product


@pytest.fixture(autouse=True)
def _public_door(monkeypatch: pytest.MonkeyPatch) -> None:
    use_public_door(monkeypatch)


def _launch(ctx: Context, product_path: Path) -> KitRun:
    ctx.clock.sleep(1)  # type: ignore[attr-defined]  # distinct created_at/published_at
    run = run_kit(ctx, product_path, KitOptions(video=False), public_client(), publish=True)
    assert run.publish is not None and run.publish.published, run.publish.failures
    return run


def _snapshot(published: Path, into: Path) -> Path:
    """The directory's tree as the hub's git tree would hold it: the landed
    files, without the studio's lock (gitignored)."""
    shutil.copytree(published, into, ignore=shutil.ignore_patterns(".studio.lock"))
    return into


def _reset_to(published: Path, snapshot: Path) -> None:
    """``git reset --hard`` to the landed tree: tracked files back to the
    snapshot's bytes; the (untracked, gitignored) lock stays."""
    for path in list(published.rglob("*")):
        if path.is_file() and path.name != ".studio.lock":
            path.unlink()
    shutil.copytree(snapshot, published, dirs_exist_ok=True)


def _row(ctx: Context, launch_id: str) -> tuple[str, str | None, str | None]:
    row = ctx.conn.execute(
        "SELECT status, superseded_by, unpublished_at FROM launches WHERE id = ?", (launch_id,)
    ).fetchone()
    return row["status"], row["superseded_by"], row["unpublished_at"]


def _superseded_then_successor_unpublished(
    ctx: Context, studio_root: Path, tmp_path: Path, publish_extra: str = ""
) -> tuple[KitRun, KitRun, Path, Path]:
    """L1 published and landed; L2 published (superseding L1), its PR never
    landed, so L2 is unpublished and the clone is reset to L1's tree."""
    product_path = write_product(studio_root, capture=False)
    if publish_extra:
        product_path.write_text(
            product_path.read_text().replace(
                "  commercial: false", f"  commercial: false\n  {publish_extra}"
            )
        )
    published = studio_root / "published-launch"
    first = _launch(ctx, product_path)
    landed = _snapshot(published, tmp_path / "landed")
    second = _launch(ctx, product_path)
    assert _row(ctx, first.launch_id)[:2] == ("unpublished", second.launch_id)
    unpublish_launch(ctx.conn, second.launch_id, ctx.notifier, ctx.clock)
    _reset_to(published, landed)
    return first, second, published, landed


def test_readopt_superseded_launch_when_bytes_match(
    make_context: Callable[..., Context], studio_root: Path, tmp_path: Path
) -> None:
    ctx = make_context(**preview_ctx(studio_root))
    first, second, published, landed = _superseded_then_successor_unpublished(
        ctx, studio_root, tmp_path
    )
    before = {p.relative_to(published): p.read_bytes() for p in published.rglob("*") if p.is_file()}

    result = readopt_launch(ctx.conn, first.launch_id, ctx.notifier, ctx.clock)

    assert (result.arm, result.publish_dir) == ("superseded", published.resolve())
    assert _row(ctx, first.launch_id) == ("published", None, None)
    assert _row(ctx, second.launch_id)[0] == "unpublished"
    # Nothing on disk changed, and the readopted launch now owns the dir: the
    # foreign-file gate a later publish runs passes.
    after = {p.relative_to(published): p.read_bytes() for p in published.rglob("*") if p.is_file()}
    assert after == before
    from qontinui_studio.launch.publish import gate_foreign_file

    gate = gate_foreign_file(ctx.conn, published.resolve(), "next")
    assert not gate.failed, gate.detail
    assert "readopted" in notice_text(ctx.workspace.notices_dir, first.launch_id, "readopted")


def test_readopt_by_bytes_restores_unpublished_launch(
    make_context: Callable[..., Context], studio_root: Path, tmp_path: Path
) -> None:
    """AF2: a retraction PR closed unlanded. The launch was unpublished outright
    (no superseded_by) and the reset restored its landed bytes."""
    ctx = make_context(**preview_ctx(studio_root))
    product_path = write_product(studio_root, capture=False)
    published = studio_root / "published-launch"
    run = _launch(ctx, product_path)
    landed = _snapshot(published, tmp_path / "landed")
    unpublish_launch(ctx.conn, run.launch_id, ctx.notifier, ctx.clock)
    assert _row(ctx, run.launch_id)[:2] == ("unpublished", None)
    assert not (published / "index.html").exists()
    _reset_to(published, landed)

    result = readopt_launch(ctx.conn, run.launch_id, ctx.notifier, ctx.clock)

    assert result.arm == "by_bytes"
    assert _row(ctx, run.launch_id) == ("published", None, None)
    assert (published / "index.html").read_bytes() == (landed / "index.html").read_bytes()


def test_readopt_by_bytes_refuses_a_launch_that_is_not_the_newest(
    make_context: Callable[..., Context], studio_root: Path, tmp_path: Path
) -> None:
    ctx = make_context(**preview_ctx(studio_root))
    first, second, published, _ = _superseded_then_successor_unpublished(ctx, studio_root, tmp_path)
    # As if L1 had been unpublished outright: a NEWER launch went live there since.
    ctx.conn.execute("UPDATE launches SET superseded_by = NULL WHERE id = ?", (first.launch_id,))
    ctx.conn.commit()
    with pytest.raises(ReadoptRefused) as e:
        readopt_launch(ctx.conn, first.launch_id, ctx.notifier, ctx.clock)
    assert e.value.code == "not_newest" and second.launch_id in e.value.detail
    assert _row(ctx, first.launch_id)[0] == "unpublished"


def test_readopt_refuses_when_bytes_differ(
    make_context: Callable[..., Context], studio_root: Path, tmp_path: Path
) -> None:
    # Arm 1: one landed file was changed.
    ctx = make_context(**preview_ctx(studio_root))
    first, _, published, _ = _superseded_then_successor_unpublished(ctx, studio_root, tmp_path)
    (published / "index.html").write_text("<p>someone else's bytes</p>")
    with pytest.raises(ReadoptRefused) as e:
        readopt_launch(ctx.conn, first.launch_id, ctx.notifier, ctx.clock)
    assert e.value.code == "bytes_differ" and "index.html" in e.value.detail
    assert _row(ctx, first.launch_id)[0] == "unpublished"

    # Arm 2: one of the launch's files is missing.
    (published / "index.html").unlink()
    ctx.conn.execute("UPDATE launches SET superseded_by = NULL WHERE id = ?", (first.launch_id,))
    ctx.conn.execute(
        "UPDATE launches SET published_at = NULL WHERE id != ?", (first.launch_id,)
    )  # make it the newest, so only the bytes decide
    ctx.conn.commit()
    with pytest.raises(ReadoptRefused) as e:
        readopt_launch(ctx.conn, first.launch_id, ctx.notifier, ctx.clock)
    assert e.value.code == "bytes_differ" and "index.html" in e.value.detail
    assert _row(ctx, first.launch_id)[0] == "unpublished"


def test_readopt_refuses_when_a_launch_is_live(
    make_context: Callable[..., Context], studio_root: Path, tmp_path: Path
) -> None:
    ctx = make_context(**preview_ctx(studio_root))
    product_path = write_product(studio_root, capture=False)
    published = studio_root / "published-launch"
    # Arm 1: L1 superseded by L2, and L2 is still live.
    first = _launch(ctx, product_path)
    second = _launch(ctx, product_path)
    with pytest.raises(ReadoptRefused) as e:
        readopt_launch(ctx.conn, first.launch_id, ctx.notifier, ctx.clock)
    assert e.value.code == "launch_live" and second.launch_id in e.value.detail
    assert _row(ctx, first.launch_id)[:2] == ("unpublished", second.launch_id)
    assert _row(ctx, second.launch_id)[0] == "published"

    # Arm 2: L2 unpublished outright, then L3 went live there.
    landed = _snapshot(published, tmp_path / "landed-2")
    unpublish_launch(ctx.conn, second.launch_id, ctx.notifier, ctx.clock)
    third = _launch(ctx, product_path)
    shutil.rmtree(landed)
    with pytest.raises(ReadoptRefused) as e:
        readopt_launch(ctx.conn, second.launch_id, ctx.notifier, ctx.clock)
    assert e.value.code == "launch_live" and third.launch_id in e.value.detail
    assert _row(ctx, second.launch_id)[:2] == ("unpublished", None)

    # A live launch itself is not readopted.
    with pytest.raises(ReadoptRefused) as e:
        readopt_launch(ctx.conn, third.launch_id, ctx.notifier, ctx.clock)
    assert e.value.code == "not_unpublished"


def test_readopt_refuses_a_foreign_file_in_the_dir(
    make_context: Callable[..., Context], studio_root: Path, tmp_path: Path
) -> None:
    ctx = make_context(**preview_ctx(studio_root))
    first, _, published, _ = _superseded_then_successor_unpublished(ctx, studio_root, tmp_path)
    # The studio's own lock is not foreign (it is held right now, on disk).
    assert (published / ".studio.lock").exists()
    (published / "someone-elses.html").write_text("x")
    with pytest.raises(ReadoptRefused) as e:
        readopt_launch(ctx.conn, first.launch_id, ctx.notifier, ctx.clock)
    assert e.value.code == "foreign_file" and "someone-elses.html" in e.value.detail
    assert _row(ctx, first.launch_id)[0] == "unpublished"
    assert (published / "someone-elses.html").read_text() == "x"  # never touched

    # Without it, the same directory readopts.
    (published / "someone-elses.html").unlink()
    assert readopt_launch(ctx.conn, first.launch_id, ctx.notifier, ctx.clock).arm == "superseded"


def test_cli_launch_readopt_json_and_exit_codes(
    make_context: Callable[..., Context],
    studio_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("STUDIO_COORD_BEARER", raising=False)
    monkeypatch.delenv("STUDIO_COORD_BEARER_FILE", raising=False)
    ctx = make_context(**preview_ctx(studio_root))
    first, second, published, _ = _superseded_then_successor_unpublished(ctx, studio_root, tmp_path)
    base = ["--root", str(studio_root), "launch", "readopt"]

    (published / "stray.txt").write_text("x")
    r = CliRunner().invoke(main, [*base, "--launch", first.launch_id, "--json"])
    assert r.exit_code == 2, r.output
    refused = json.loads(r.output)
    assert refused["readopted"] is False and refused["code"] == "foreign_file"
    assert refused["launch_id"] == first.launch_id
    (published / "stray.txt").unlink()

    r = CliRunner().invoke(main, [*base, "--launch", first.launch_id, "--json"])
    assert r.exit_code == 0, r.output
    ok = json.loads(r.output)
    assert ok["readopted"] is True and ok["arm"] == "superseded"
    assert ok["publish_dir"] == str(published.resolve()) and ok["files"] > 0

    r = CliRunner().invoke(main, [*base, "--launch", second.launch_id])
    assert r.exit_code == 2 and "launch_live" in r.output, r.output

    r = CliRunner().invoke(main, [*base, "--launch", "no-such-launch", "--json"])
    assert r.exit_code == 1, r.output
    assert json.loads(r.output)["code"] == "not_found"


# ---- B4: the product's unpublish command in the published notice ---------------


def test_unpublish_command_overrides_the_notice(
    make_context: Callable[..., Context], studio_root: Path, tmp_path: Path
) -> None:
    ctx = make_context(**preview_ctx(studio_root))
    product_path = write_product(studio_root, capture=False)

    # Unset: the studio's own one-step unpublish.
    default = _launch(ctx, product_path)
    text = notice_text(ctx.workspace.notices_dir, default.launch_id, "published")
    assert f"launch unpublish --launch {default.launch_id}" in text
    assert "hub unpublish" not in text

    # Set: printed verbatim instead, in the notice and its coord payload.
    product_path.write_text(
        product_path.read_text().replace(
            "  commercial: false",
            "  commercial: false\n  unpublish_command: hub unpublish qontinui-studio",
        )
    )
    hub = _launch(ctx, product_path)
    text = notice_text(ctx.workspace.notices_dir, hub.launch_id, "published")
    assert "```\nhub unpublish qontinui-studio\n```" in text
    assert "launch unpublish --launch" not in text
    payload = json.loads(
        sorted(ctx.workspace.notices_dir.glob(f"{hub.launch_id}-published-*.json"))[-1].read_text()
    )
    assert payload["unpublish_command"] == "hub unpublish qontinui-studio"

    # Recovery re-sends a missing notice with the product's command too.
    ctx.conn.execute("UPDATE launches SET notified_at = NULL WHERE id = ?", (hub.launch_id,))
    ctx.conn.commit()
    for f in ctx.workspace.notices_dir.glob(f"{hub.launch_id}-published-*"):
        f.unlink()
    from qontinui_studio.launch.publish import recover_all_launches

    # The command was frozen on the launch row at publish: the yaml at that
    # path changing (or vanishing) since does not change what the notice says.
    product_path.write_text(
        product_path.read_text().replace(
            "unpublish_command: hub unpublish qontinui-studio", "unpublish_command: rm -rf it"
        )
    )
    lines, failed = recover_all_launches(ctx.conn, ctx.notifier, studio_root)
    assert not failed and any("sent the missing published notice" in ln for ln in lines)
    text = notice_text(ctx.workspace.notices_dir, hub.launch_id, "published")
    assert "hub unpublish qontinui-studio" in text and "launch unpublish --launch" not in text
    assert "rm -rf it" not in text
    row = ctx.conn.execute(
        "SELECT unpublish_command FROM launches WHERE id = ?", (default.launch_id,)
    ).fetchone()
    assert row["unpublish_command"] is None  # unset: the studio's own, at notice time


@pytest.mark.parametrize("bad", ["", "   ", "hub unpublish x\nrm -rf /"])
def test_unpublish_command_must_be_one_line(studio_root: Path, bad: str) -> None:
    from qontinui_studio.errors import ConfigError
    from qontinui_studio.launch.product import load_product

    product_path = write_product(studio_root, capture=False)
    product_path.write_text(
        product_path.read_text().replace(
            "  commercial: false", f"  commercial: false\n  unpublish_command: {json.dumps(bad)}"
        )
    )
    with pytest.raises(ConfigError, match="unpublish_command"):
        load_product(product_path)


# ---- review fixes ---------------------------------------------------------------


def test_readopt_refreshes_published_at_so_a_later_by_bytes_readopt_is_newest(
    make_context: Callable[..., Context], studio_root: Path, tmp_path: Path
) -> None:
    """L1 lands; L2's publish PR closes unlanded and L1 is readopted (arm 1);
    then L1's unpublish PR closes unlanded. L1 is still the newest launch to
    have gone live there, so readopt-by-bytes takes it back."""
    ctx = make_context(**preview_ctx(studio_root))
    first, second, published, landed = _superseded_then_successor_unpublished(
        ctx, studio_root, tmp_path
    )
    ctx.clock.sleep(1)  # type: ignore[attr-defined]
    assert readopt_launch(ctx.conn, first.launch_id, ctx.notifier, ctx.clock).arm == "superseded"
    ctx.clock.sleep(1)  # type: ignore[attr-defined]
    unpublish_launch(ctx.conn, first.launch_id, ctx.notifier, ctx.clock)
    assert _row(ctx, first.launch_id)[:2] == ("unpublished", None)
    _reset_to(published, landed)

    result = readopt_launch(ctx.conn, first.launch_id, ctx.notifier, ctx.clock)

    assert result.arm == "by_bytes"
    assert _row(ctx, first.launch_id) == ("published", None, None)
    assert _row(ctx, second.launch_id)[0] == "unpublished"


def test_readopt_treats_every_symlink_as_foreign(
    make_context: Callable[..., Context], studio_root: Path, tmp_path: Path
) -> None:
    ctx = make_context(**preview_ctx(studio_root))
    first, _, published, landed = _superseded_then_successor_unpublished(ctx, studio_root, tmp_path)
    cases = {
        # A file symlink to identical bytes is still not a file the launch placed.
        "index.html": lambda p: (p.unlink(), p.symlink_to(landed / "index.html")),
        "linked-dir": lambda p: p.symlink_to(landed, target_is_directory=True),
        "dangling": lambda p: p.symlink_to(tmp_path / "nowhere"),
    }
    for name, make in cases.items():
        path = published / name
        make(path)
        with pytest.raises(ReadoptRefused) as e:
            readopt_launch(ctx.conn, first.launch_id, ctx.notifier, ctx.clock)
        assert e.value.code == "foreign_file" and name in e.value.detail, name
        assert _row(ctx, first.launch_id)[0] == "unpublished"
        path.unlink()
        if name == "index.html":
            shutil.copyfile(landed / "index.html", path)
    assert readopt_launch(ctx.conn, first.launch_id, ctx.notifier, ctx.clock).arm == "superseded"


def test_readopt_ignores_the_lock_and_journals_only_at_the_top_level(
    make_context: Callable[..., Context], studio_root: Path, tmp_path: Path
) -> None:
    ctx = make_context(**preview_ctx(studio_root))
    first, _, published, _ = _superseded_then_successor_unpublished(ctx, studio_root, tmp_path)
    nested = next(p for p in published.rglob("*") if p.is_dir() and not p.is_symlink())
    for name in (".studio.lock", ".studio-journal-x.json"):
        (nested / name).write_text("{}")
        with pytest.raises(ReadoptRefused) as e:
            readopt_launch(ctx.conn, first.launch_id, ctx.notifier, ctx.clock)
        assert e.value.code == "foreign_file" and name in e.value.detail
        (nested / name).unlink()


def test_gate_foreign_file_sees_symlinks_and_nested_studio_names(
    make_context: Callable[..., Context], studio_root: Path, tmp_path: Path
) -> None:
    from qontinui_studio.launch.publish import gate_foreign_file

    ctx = make_context(**preview_ctx(studio_root))
    run = _launch(ctx, write_product(studio_root, capture=False))
    published = (studio_root / "published-launch").resolve()
    assert not gate_foreign_file(ctx.conn, published, "next").failed
    copy = tmp_path / "copy.html"
    shutil.copyfile(published / "index.html", copy)
    nested = next(p for p in published.rglob("*") if p.is_dir())
    makers = {
        "index.html": lambda p: (p.unlink(), p.symlink_to(copy)),
        "linked-dir": lambda p: p.symlink_to(tmp_path, target_is_directory=True),
        "dangling": lambda p: p.symlink_to(tmp_path / "nowhere"),
    }
    for name, make in makers.items():
        make(published / name)
        gate = gate_foreign_file(ctx.conn, published, "next")
        assert gate.failed and name in gate.detail, name
        (published / name).unlink()
        if name == "index.html":
            shutil.copyfile(copy, published / name)
    (nested / ".studio-journal-x.json").write_text("{}")
    gate = gate_foreign_file(ctx.conn, published, "next")
    assert gate.failed and ".studio-journal-x.json" in gate.detail
    assert run.launch_id


def test_readopt_on_a_missing_dir_refuses_without_creating_it(
    make_context: Callable[..., Context], studio_root: Path, tmp_path: Path
) -> None:
    ctx = make_context(**preview_ctx(studio_root))
    first, _, published, _ = _superseded_then_successor_unpublished(ctx, studio_root, tmp_path)
    shutil.rmtree(published)
    with pytest.raises(ReadoptRefused) as e:
        readopt_launch(ctx.conn, first.launch_id, ctx.notifier, ctx.clock)
    assert e.value.code == "bytes_differ"
    assert not published.exists()  # no directory, no lock
    assert _row(ctx, first.launch_id)[0] == "unpublished"


def test_readopt_notice_names_the_public_pages(
    make_context: Callable[..., Context], studio_root: Path, tmp_path: Path
) -> None:
    ctx = make_context(**preview_ctx(studio_root))
    first, _, _, _ = _superseded_then_successor_unpublished(
        ctx, studio_root, tmp_path, "public_url: https://build.example.test/launch/"
    )
    readopt_launch(ctx.conn, first.launch_id, ctx.notifier, ctx.clock)
    payload = json.loads(
        sorted(ctx.workspace.notices_dir.glob(f"{first.launch_id}-readopted-*.json"))[
            -1
        ].read_text()
    )
    assert payload["urls"] and all(
        u.startswith("https://build.example.test/launch/") for u in payload["urls"]
    ), payload["urls"]


def test_cli_launch_readopt_reports_an_unexpected_error_as_json(
    studio_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from qontinui_studio.errors import JournalError

    monkeypatch.delenv("STUDIO_COORD_BEARER", raising=False)
    monkeypatch.delenv("STUDIO_COORD_BEARER_FILE", raising=False)

    def broken(*args: object, **kwargs: object) -> None:
        raise JournalError("leftover journal names a path outside the dir")

    monkeypatch.setattr("qontinui_studio.cli.readopt_launch", broken)
    base = ["--root", str(studio_root), "launch", "readopt", "--launch", "L1"]
    r = CliRunner().invoke(main, [*base, "--json"])
    assert r.exit_code == 1, r.output
    out = json.loads(r.output)
    assert out["code"] == "error" and out["readopted"] is False
    assert "JournalError" in out["detail"]
    r = CliRunner().invoke(main, base)
    assert r.exit_code == 1 and "error: error:" in r.output


# ---- second review: a symlinked root, non-regular files, unreadable dirs --------


def test_a_symlinked_publish_dir_is_foreign_to_the_gate_and_readopt(
    make_context: Callable[..., Context], studio_root: Path, tmp_path: Path
) -> None:
    from qontinui_studio.launch.publish import gate_foreign_file

    ctx = make_context(**preview_ctx(studio_root))
    first, _, published, _ = _superseded_then_successor_unpublished(ctx, studio_root, tmp_path)
    real = tmp_path / "real-dir"
    published.rename(real)
    published.symlink_to(real, target_is_directory=True)

    gate = gate_foreign_file(ctx.conn, published, "next")
    assert gate.failed and "(the directory itself)" in gate.detail, gate.detail
    with pytest.raises(ReadoptRefused) as e:
        readopt_launch(ctx.conn, first.launch_id, ctx.notifier, ctx.clock)
    assert e.value.code == "foreign_file" and "(the directory itself)" in e.value.detail
    assert _row(ctx, first.launch_id)[0] == "unpublished"


def test_a_fifo_is_foreign_and_never_read(
    make_context: Callable[..., Context], studio_root: Path, tmp_path: Path
) -> None:
    """A FIFO would block sha256_file forever; it is listed, never opened."""
    import os

    from qontinui_studio.launch.publish import gate_foreign_file

    if not hasattr(os, "mkfifo"):
        pytest.skip("no FIFOs on this platform")
    ctx = make_context(**preview_ctx(studio_root))
    first, _, published, _ = _superseded_then_successor_unpublished(ctx, studio_root, tmp_path)
    os.mkfifo(published / "pipe")

    gate = gate_foreign_file(ctx.conn, published.resolve(), "next")
    assert gate.failed and "pipe" in gate.detail, gate.detail
    with pytest.raises(ReadoptRefused) as e:
        readopt_launch(ctx.conn, first.launch_id, ctx.notifier, ctx.clock)
    assert e.value.code == "foreign_file" and "pipe" in e.value.detail
    assert _row(ctx, first.launch_id)[0] == "unpublished"


def test_an_unreadable_subdirectory_is_an_error_not_skipped(
    make_context: Callable[..., Context],
    studio_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import os

    from qontinui_studio.launch.publish import gate_foreign_file

    if os.name != "posix" or os.geteuid() == 0:
        pytest.skip("needs POSIX permissions enforced (not root)")
    ctx = make_context(**preview_ctx(studio_root))
    first, _, published, _ = _superseded_then_successor_unpublished(ctx, studio_root, tmp_path)
    hidden = published / "locked-away"
    hidden.mkdir()
    (hidden / "x.html").write_text("x")
    hidden.chmod(0)
    try:
        gate = gate_foreign_file(ctx.conn, published.resolve(), "next")
        assert gate.failed and "cannot be fully read" in gate.detail, gate.detail
        with pytest.raises(PermissionError):
            readopt_launch(ctx.conn, first.launch_id, ctx.notifier, ctx.clock)
        assert _row(ctx, first.launch_id)[0] == "unpublished"
        monkeypatch.delenv("STUDIO_COORD_BEARER", raising=False)
        monkeypatch.delenv("STUDIO_COORD_BEARER_FILE", raising=False)
        r = CliRunner().invoke(
            main,
            [
                "--root",
                str(studio_root),
                "launch",
                "readopt",
                "--launch",
                first.launch_id,
                "--json",
            ],
        )
        assert r.exit_code == 1, r.output
        out = json.loads(r.output)
        assert out["code"] == "error" and "PermissionError" in out["detail"]
    finally:
        hidden.chmod(0o755)
