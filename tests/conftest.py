from __future__ import annotations

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
  - id: fake2
    provider: Second fake generator
    plan: test
    api_available: {value: "yes", source: "https://example.test/api"}
    automation_allowed: {value: "yes", source: "https://example.test/tos"}
    commercial_use: {value: "no", source: "https://example.test/tos#noncommercial"}
    watermark: none
    adapter: fake
    model: fake-v2
    daily_quota: 10
    tos_reviewed: 2026-10-09
"""

CAFE = """
id: cafe-template
name: Cafe template
kind: demo
commercial: false
publish_dir: ../published/cafe
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


@pytest.fixture
def make_context(studio_root: Path, clock: FakeClock) -> Callable[..., Context]:
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
        )
        for k, v in overrides.items():
            setattr(ctx, k, v)
        return ctx

    return _make
