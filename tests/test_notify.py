from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

from qontinui_studio.notify import CoordFindingNotifier, FileNotifier, Notice

from .conftest import notice_text

NOTICE = Notice(
    batch_id="b1",
    outcome="published",
    title="published b1",
    body="ok",
    urls=["https://example.test/hero.webp"],
    unpublish_command="studio unpublish --batch b1",
)
URL = "https://coord.qontinui.io/coord/agent-findings"


def test_without_bearer_writes_local_notice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("STUDIO_COORD_BEARER", raising=False)
    monkeypatch.delenv("STUDIO_COORD_BEARER_FILE", raising=False)
    d = CoordFindingNotifier(FileNotifier(tmp_path)).notify(NOTICE)
    assert d.channel == "file" and "no coord bearer" in (d.note or "")
    md = notice_text(tmp_path, "b1", "published")
    assert "studio unpublish --batch b1" in md
    [js] = tmp_path.glob("b1-published-*.json")
    assert json.loads(js.read_text())["batch_id"] == "b1"


@respx.mock
def test_with_bearer_posts_to_the_agent_findings_door(tmp_path: Path) -> None:
    route = respx.post(URL).mock(return_value=httpx.Response(201, json={"id": "f-1"}))
    d = CoordFindingNotifier(FileNotifier(tmp_path), bearer="tok").notify(NOTICE)
    assert d.channel == "coord" and d.location.endswith("#f-1")
    req = route.calls.last.request
    assert req.headers["authorization"] == "Bearer tok"
    body = json.loads(req.content)
    assert body["topic"] == "studio-publication"
    assert body["artifact_refs"]["unpublish_command"] == "studio unpublish --batch b1"
    # No identity fields — coord refuses them (tenant/author come from the JWT).
    assert not {"tenant_id", "author_session", "author_device"} & set(body)
    assert "also posted to coord finding f-1" in notice_text(tmp_path, "b1", "published")


@respx.mock
def test_coord_refusal_falls_back_and_says_why(tmp_path: Path) -> None:
    respx.post(URL).mock(return_value=httpx.Response(401, text="expired"))
    d = CoordFindingNotifier(FileNotifier(tmp_path), bearer="tok").notify(NOTICE)
    assert d.channel == "file" and "HTTP 401" in (d.note or "")
    assert "HTTP 401" in notice_text(tmp_path, "b1", "published")


def test_bearer_from_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    f = tmp_path / "bearer"
    f.write_text("from-file\n")
    monkeypatch.delenv("STUDIO_COORD_BEARER", raising=False)
    monkeypatch.setenv("STUDIO_COORD_BEARER_FILE", str(f))
    assert CoordFindingNotifier(FileNotifier(tmp_path)).bearer == "from-file"


def test_file_notices_never_overwrite(tmp_path: Path) -> None:
    n = FileNotifier(tmp_path)
    first = n.notify(NOTICE)
    second = n.notify(NOTICE)
    assert first.location != second.location
    assert len(list(tmp_path.glob("b1-published-*.md"))) == 2
