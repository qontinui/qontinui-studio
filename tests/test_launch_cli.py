"""Review finding 11: the launch CLI's publish, unpublish and recover verbs,
end to end through CliRunner (D7 runs against the fake preview + bridge)."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from click.testing import CliRunner

from qontinui_studio import cli
from qontinui_studio.cli import main
from qontinui_studio.launch.kit import KitOptions, run_kit
from qontinui_studio.pipeline import Context

from .conftest import notice_text
from .launch_support import (
    launch_preview,
    preview_ctx,
    public_client,
    use_public_door,
    write_product,
)


@pytest.fixture(autouse=True)
def _public_door(monkeypatch: pytest.MonkeyPatch) -> None:
    """Publication re-reads the factory's doors; here they are in-process fakes."""
    use_public_door(monkeypatch)


@pytest.fixture(autouse=True)
def _no_bearer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("STUDIO_COORD_BEARER", raising=False)
    monkeypatch.delenv("STUDIO_COORD_BEARER_FILE", raising=False)


@pytest.fixture
def cli_context(monkeypatch: pytest.MonkeyPatch, studio_root: Path) -> None:
    """The CLI's own Context, with D7 pointed at the fake preview + bridge."""
    real = cli._context
    fake = launch_preview(studio_root)

    def _ctx(root: Path) -> Context:
        ctx = real(root)
        ctx.bridge = fake.bridge
        ctx.fetch = fake.fetch
        return ctx

    monkeypatch.setattr(cli, "_context", _ctx)


def _assembled(make_context: Callable[..., Context], studio_root: Path) -> str:
    ctx = make_context(**preview_ctx(studio_root))
    run = run_kit(
        ctx, write_product(studio_root, capture=False), KitOptions(video=False), public_client()
    )
    return run.launch_id


@pytest.mark.usefixtures("cli_context")
def test_cli_publish_unpublish_recover(
    make_context: Callable[..., Context], studio_root: Path
) -> None:
    launch_id = _assembled(make_context, studio_root)
    root = ["--root", str(studio_root)]
    runner = CliRunner()

    r = runner.invoke(main, [*root, "launch", "publish", "--launch", launch_id])
    assert r.exit_code == 0, r.output
    assert "pass            d7_images" in r.output
    published = studio_root / "published-launch"
    assert (published / "index.html").is_file()
    notice = notice_text(studio_root / ".studio" / "notices", launch_id, "published")
    assert f"launch unpublish --launch {launch_id}" in notice

    r = runner.invoke(main, [*root, "launch", "list"])
    assert launch_id in r.output and "published" in r.output

    r = runner.invoke(main, [*root, "launch", "recover"])
    assert r.exit_code == 0 and "nothing to recover" in r.output

    r = runner.invoke(main, [*root, "launch", "unpublish", "--launch", launch_id])
    assert r.exit_code == 0, r.output
    assert f"unpublished {launch_id}" in r.output
    assert not (published / "index.html").exists()

    r = runner.invoke(main, [*root, "launch", "unpublish", "--launch", launch_id])
    assert r.exit_code == 1 and "not published" in r.output


@pytest.mark.usefixtures("cli_context")
def test_cli_recover_rolls_back_an_interrupted_publish(
    make_context: Callable[..., Context], studio_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os

    import qontinui_studio.launch.publish as lp

    launch_id = _assembled(make_context, studio_root)
    real_replace = os.replace

    def crash_on_placement(src: object, dst: object) -> None:
        real_replace(src, dst)  # type: ignore[arg-type]
        if ".staged-" in str(src):
            raise SystemExit("power cut")

    monkeypatch.setattr(os, "replace", crash_on_placement)
    monkeypatch.setattr(lp.LaunchJournal, "roll_back", lambda self: [])
    root = ["--root", str(studio_root)]
    r = CliRunner().invoke(main, [*root, "launch", "publish", "--launch", launch_id])
    assert r.exit_code != 0
    monkeypatch.undo()
    published = studio_root / "published-launch"
    assert list(published.glob(".studio-journal-*.json"))

    r = CliRunner().invoke(main, [*root, "launch", "recover"])
    assert r.exit_code == 0, r.output
    assert f"rolled back interrupted publish of launch {launch_id}" in r.output
    assert not list(published.glob(".studio-journal-*.json"))
    assert not (published / "index.html").exists()
