from __future__ import annotations

import socket
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from qontinui_studio.adapters import AdapterSet, build_adapters
from qontinui_studio.adapters.fake import FakeImageAdapter
from qontinui_studio.notify import FileNotifier
from qontinui_studio.pipeline import Context
from qontinui_studio.providers import load_providers
from qontinui_studio.sites import load_sites
from qontinui_studio.store import connect
from qontinui_studio.workspace import Workspace

PROVIDERS = """
note: test inventory
operator_accounts_owed: false
providers:
  - id: fake
    provider: Fake generator
    plan: test
    api_available: {value: "yes", source: "https://example.test/api"}
    automation_allowed: {value: "yes", source: "https://example.test/tos"}
    commercial_use: {value: "yes", source: "https://example.test/tos#commercial"}
    watermark: none
    watermark_visible: false
    adapter: fake
    model: fake-v1
    daily_quota: 10
    tos_reviewed: 2026-10-09
    domains: [example.test]
  - id: fake2
    provider: Second fake generator
    plan: test
    api_available: {value: "yes", source: "https://example2.test/api"}
    automation_allowed: {value: "yes", source: "https://example2.test/tos"}
    commercial_use: {value: "no", source: "https://example2.test/tos#noncommercial"}
    watermark: none
    adapter: fake
    model: fake-v2
    daily_quota: 10
    tos_reviewed: 2026-10-09
    domains: [example2.test]
"""

CAFE = """
id: cafe-template
name: Cafe template
kind: demo
commercial: false
publish_dir: ../published/cafe
page_health_url: http://preview.test/cafe/ui-bridge/control/page-health
preview_dir: ../../previews/cafe
preview_manifest_url: http://preview.test/cafe/studio-manifest.json
slots:
  hero: {width: 1600, height: 900, formats: [webp, avif]}
  og: {width: 1200, height: 630, formats: [png]}
"""

BARBER = """
id: barber-template
name: Barber template
kind: demo
commercial: true
publish_dir: ../published/barber
page_health_url: http://preview.test/barber/ui-bridge/control/page-health
preview_dir: ../../previews/barber
preview_manifest_url: http://preview.test/barber/studio-manifest.json
slots:
  hero: {width: 1440, height: 1080, formats: [webp, avif]}
"""

BRIEF = """
id: hero-test
purpose: test hero
targets:
  - {site: cafe-template, slot: hero}
  - {site: barber-template, slot: hero}
  - {campaign: launch}
aspect_ratio: "16:9"
prompt: An empty cafe interior in warm light
style_guide: style.md
provider_preference: [{providers}]
variants: {variants}
"""


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests never touch the network: provider HTTP is mocked with respx, which
    intercepts at the httpx transport, so any real socket connect is a bug."""

    def refuse(*args: object, **kwargs: object) -> None:
        raise RuntimeError("tests must not open network connections")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


def notice_text(notices_dir: Path, batch_id: str, outcome: str) -> str:
    """The newest notice for (batch, outcome); notices are timestamped."""
    matches = sorted(notices_dir.glob(f"{batch_id}-{outcome}-*.md"))
    assert matches, f"no {outcome} notice for {batch_id} in {notices_dir}"
    return matches[-1].read_text()


def debris(publish_dir: Path) -> list[Path]:
    """Hidden staging/backup/journal/tmp files left in a publish dir (the
    persistent lock file is expected and excluded)."""
    return [p for p in publish_dir.iterdir() if p.name.startswith(".") and p.name != ".studio.lock"]


class FakeClock:
    def __init__(self, start: datetime | None = None) -> None:
        self.now = start or datetime(2026, 10, 9, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def studio_root(tmp_path: Path) -> Path:
    root = tmp_path / "studio"
    (root / "sites").mkdir(parents=True)
    (root / "briefs").mkdir()
    (root / "providers.yaml").write_text(PROVIDERS)
    (root / "sites" / "cafe.yaml").write_text(CAFE)
    (root / "sites" / "barber.yaml").write_text(BARBER)
    (root / "briefs" / "style.md").write_text("Warm and minimal.")
    return root


@pytest.fixture
def write_brief(studio_root: Path) -> Callable[..., Path]:
    def _write(providers: str = "fake", variants: int = 1, name: str = "hero.yaml") -> Path:
        path = studio_root / "briefs" / name
        path.write_text(
            BRIEF.replace("{providers}", providers).replace("{variants}", str(variants))
        )
        return path

    return _write


def _async_fakes(adapters: AdapterSet) -> AdapterSet:
    """Make every fake report one pending poll, as an asynchronous provider would."""
    for pid, a in list(adapters.adapters.items()):
        adapters.adapters[pid] = FakeImageAdapter(pid, a.model, pending_polls=1)
    return adapters


class FakePreview:
    """A fake preview server for the template sites: serves the manifest the
    studio wrote into ``<previews>/<site>/`` and answers page health OK. The
    D7 gates run for real against it (manifest sha check + summary check)."""

    def __init__(self, previews: Path, summary: str = "OK") -> None:
        self.previews = previews
        self.summary = summary
        self.probed: list[str] = []

    def fetch(self, url: str) -> bytes:
        site = url.removeprefix("http://preview.test/").split("/", 1)[0]
        return (self.previews / site / "studio-manifest.json").read_bytes()

    def probe(self, url: str) -> dict[str, object]:
        self.probed.append(url)
        return {"success": True, "data": {"summary": self.summary, "findings": []}}


@pytest.fixture
def preview(studio_root: Path) -> FakePreview:
    return FakePreview(studio_root.parent / "previews")


@pytest.fixture
def make_context(
    studio_root: Path, clock: FakeClock, preview: FakePreview
) -> Callable[..., Context]:
    def _make(**overrides: object) -> Context:
        ws = Workspace(studio_root)
        inventory = load_providers(ws.providers_path)
        ctx = Context(
            workspace=ws,
            conn=connect(ws.db_path),
            inventory=inventory,
            sites=load_sites(ws.sites_dir),
            adapters=_async_fakes(build_adapters(inventory)),
            notifier=FileNotifier(ws.notices_dir),
            clock=clock,
            sleep=clock.sleep,
            probe=preview.probe,
            fetch=preview.fetch,
        )
        for k, v in overrides.items():
            setattr(ctx, k, v)
        return ctx

    return _make
