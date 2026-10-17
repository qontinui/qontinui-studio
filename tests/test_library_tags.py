"""Library tags (schema v3): a brief files its assets under a tag at ingest,
``studio library tag|untag`` edits the set, and ``library list --tag`` is the
query a set's consumer runs (plan 2026-10-09-qontinui-marketing-studio-
animated-process-explainers, Phase 1 arming: the studio-style-v1 set)."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import date
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from qontinui_studio.briefs import load_brief
from qontinui_studio.cli import main
from qontinui_studio.errors import ConfigError, TagRefused
from qontinui_studio.library import Library, export_yaml
from qontinui_studio.pipeline import Context, run_brief
from qontinui_studio.store import connect
from qontinui_studio.workspace import Workspace

TODAY = date(2026, 10, 17)


def _tagged_brief(write_brief: Callable[..., Path], tags: str = "[studio-style-v1]") -> Path:
    path = write_brief(variants=2)
    path.write_text(path.read_text() + f"tags: {tags}\n")
    return path


def test_a_brief_tags_every_asset_it_produces(
    make_context: Callable[..., Context], write_brief: Callable[..., Path]
) -> None:
    ctx = make_context()
    run_brief(ctx, _tagged_brief(write_brief))
    lib = Library(ctx.conn)
    assets = lib.assets()
    assert len(assets) == 2
    assert all(lib.tags(a.uid) == ["studio-style-v1"] for a in assets)
    tagged_by = {r[0] for r in ctx.conn.execute("SELECT tagged_by FROM asset_tags")}
    assert tagged_by == {"brief:hero-test"}
    # A tag is a selection, never an approval.
    assert {a.approval_status for a in lib.tagged("studio-style-v1")} == {"candidate"}


def test_an_untagged_brief_tags_nothing(
    make_context: Callable[..., Context], write_brief: Callable[..., Path]
) -> None:
    ctx = make_context()
    run_brief(ctx, write_brief())
    assert ctx.conn.execute("SELECT COUNT(*) FROM asset_tags").fetchone()[0] == 0


@pytest.mark.parametrize("tags", ["[Studio-Style]", "[a, a]", "['-x']", "['a b']"])
def test_a_brief_with_a_bad_tag_is_refused(write_brief: Callable[..., Path], tags: str) -> None:
    with pytest.raises(ConfigError):
        load_brief(_tagged_brief(write_brief, tags))


def test_tag_untag_and_the_status_filter(
    make_context: Callable[..., Context], write_brief: Callable[..., Path]
) -> None:
    ctx = make_context()
    run_brief(ctx, write_brief(variants=2))
    lib = Library(ctx.conn)
    a, b = (x.uid for x in lib.assets())
    assert lib.tag(a, "studio-style-v1", "josh", TODAY) is True
    assert lib.tag(a, "studio-style-v1", "someone-else", TODAY) is False  # first record kept
    assert lib.tag(b, "studio-style-v1", "josh", TODAY) is True
    lib.decide(a, "approved", "josh", TODAY)
    assert [x.uid for x in lib.tagged("studio-style-v1", status="approved")] == [a]
    assert {x.uid for x in lib.tagged("studio-style-v1")} == {a, b}
    assert lib.untag(b, "studio-style-v1") is True
    assert lib.untag(b, "studio-style-v1") is False
    assert [x.uid for x in lib.tagged("studio-style-v1")] == [a]
    with pytest.raises(ValueError):
        lib.tagged("studio-style-v1", status="aproved")


@pytest.mark.parametrize(
    ("uid", "tag", "by"),
    [
        ("missing", "studio-style-v1", "josh"),
        (None, "Bad Tag", "josh"),
        (None, "studio-style-v1", "josh@example.com"),
        (None, "studio-style-v1", "brief:"),
        (None, "studio-style-v1", "brief:hero-test"),  # brief provenance is ingest's alone
    ],
)
def test_tag_refusals(
    make_context: Callable[..., Context],
    write_brief: Callable[..., Path],
    uid: str | None,
    tag: str,
    by: str,
) -> None:
    ctx = make_context()
    run_brief(ctx, write_brief())
    lib = Library(ctx.conn)
    with pytest.raises(TagRefused):
        lib.tag(uid or lib.assets()[0].uid, tag, by, TODAY)


def test_cli_tag_then_list_by_tag_and_status(
    make_context: Callable[..., Context], write_brief: Callable[..., Path], studio_root: Path
) -> None:
    ctx = make_context()
    run_brief(ctx, write_brief(variants=2))
    a, b = (x.uid for x in Library(ctx.conn).assets())
    runner = CliRunner()
    root = ["--root", str(studio_root)]
    r = runner.invoke(main, [*root, "library", "tag", a, "studio-style-v1", "--by", "josh"])
    assert r.exit_code == 0 and "tagged studio-style-v1" in r.output, r.output
    r = runner.invoke(main, [*root, "library", "tag", b, "NOPE", "--by", "josh"])
    assert r.exit_code == 1 and "must match" in r.output
    r = runner.invoke(main, [*root, "library", "approve", a, "--by", "josh"])
    assert r.exit_code == 0, r.output

    r = runner.invoke(
        main,
        [*root, "library", "list", "--tag", "studio-style-v1", "--status", "approved", "--json"],
    )
    assert r.exit_code == 0, r.output
    listed = json.loads(r.output)
    assert [x["uid"] for x in listed] == [a] and listed[0]["tags"] == ["studio-style-v1"]

    r = runner.invoke(main, [*root, "library", "list", "--tag", "other"])
    assert r.exit_code == 0 and r.output == ""
    r = runner.invoke(main, [*root, "library", "untag", a, "studio-style-v1"])
    assert r.exit_code == 0 and "untagged" in r.output


def test_export_carries_tags(
    make_context: Callable[..., Context], write_brief: Callable[..., Path], studio_root: Path
) -> None:
    ctx = make_context()
    run_brief(ctx, _tagged_brief(write_brief))
    doc = yaml.safe_load(export_yaml(connect(Workspace(studio_root).db_path)))
    assert doc["version"] == 2
    assert all(a["tags"] == ["studio-style-v1"] for a in doc["assets"])
