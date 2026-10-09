from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest
from click.testing import CliRunner

from qontinui_studio.cli import main

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _no_bearer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("STUDIO_COORD_BEARER", raising=False)
    monkeypatch.delenv("STUDIO_COORD_BEARER_FILE", raising=False)


def test_cli_run_list_review_unpublish(studio_root: Path, write_brief: Callable[..., Path]) -> None:
    runner = CliRunner()
    root = ["--root", str(studio_root)]
    brief = write_brief()

    r = runner.invoke(main, [*root, "brief", "validate", str(brief)])
    assert r.exit_code == 0, r.output

    r = runner.invoke(main, [*root, "brief", "run", str(brief)])
    assert r.exit_code == 0, r.output
    assert ": published" in r.output
    batch_id = r.output.split()[1].rstrip(":")
    assert (studio_root / "published" / "cafe" / "hero.webp").is_file()

    r = runner.invoke(main, [*root, "library", "list", "--json"])
    rows = json.loads(r.output)
    assert rows[0]["ai_generated"] is True and rows[0]["licence_tos_reviewed"] == "2026-10-09"

    r = runner.invoke(main, [*root, "review", "build"])
    assert r.exit_code == 0 and Path(r.output.strip()).is_file()

    r = runner.invoke(main, [*root, "unpublish", "--batch", batch_id])
    assert r.exit_code == 0, r.output
    assert not (studio_root / "published" / "cafe" / "hero.webp").exists()

    r = runner.invoke(main, [*root, "publish", "--batch", batch_id])
    assert r.exit_code == 0, r.output
    assert (studio_root / "published" / "cafe" / "hero.webp").is_file()


def test_cli_refusal_exit_code(studio_root: Path, write_brief: Callable[..., Path]) -> None:
    r = CliRunner().invoke(
        main, ["--root", str(studio_root), "brief", "run", str(write_brief(providers="fake2"))]
    )
    assert r.exit_code == 2, r.output
    assert "d5_licence fail" in r.output


def test_cli_providers_list_on_the_shipped_inventory(monkeypatch: pytest.MonkeyPatch) -> None:
    r = CliRunner().invoke(main, ["--root", str(REPO), "providers", "list"])
    assert r.exit_code == 0
    assert "bing" in r.output and "off: automation_allowed is 'no'" in r.output
