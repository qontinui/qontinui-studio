"""Regression tests for the seventh review's reproduction scripts (f1, f2)."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from qontinui_studio.gates import NO_MEDIA
from qontinui_studio.pipeline import Context, run_brief
from qontinui_studio.publish import publish_batch

from .conftest import DomNode, FakeBridge, FakeClock, FakePreview, generate_media_id

LABEL = "Illustrative image (AI-generated)"


def two_slot_brief(studio_root: Path) -> Path:
    p = studio_root / "briefs" / "two.yaml"
    p.write_text(
        "id: two\npurpose: t\n"
        "targets: [{site: cafe-template, slot: hero}, {site: cafe-template, slot: og}]\n"
        'aspect_ratio: "4:3"\nprompt: A warm cafe\nprovider_preference: [fake]\n'
    )
    return p


def test_f1_generate_media_id_port_reproduces_the_collision() -> None:
    def picture(explicit: str | None = None) -> str:
        section = DomNode("section", children=["picture", "p"])
        attrs = {"data-media-id": explicit} if explicit else {}
        return generate_media_id(DomNode("picture", attrs, parent=section))

    def img(src: str, explicit: str | None = None) -> str:
        attrs = {"alt": LABEL, "src": src}
        if explicit:
            attrs["data-media-id"] = explicit
        return generate_media_id(
            DomNode("img", attrs, parent=DomNode("picture", children=["source", "img"]))
        )

    # Without data-media-id every slot mints the SAME ids (alt / child index).
    assert picture() == picture() == "picture-child-0"
    assert img("hero.webp") == img("about.webp") == "img-illustrative-image-ai-generate"
    # With them, ids are the slot's own.
    assert picture("studio-picture-hero") == "studio-picture-hero"
    assert img("hero.webp", "studio-img-hero") == "studio-img-hero"


def test_f1_a_template_without_media_ids_is_refused_naming_the_missing_id(
    make_context: Callable[..., Context], studio_root: Path, preview: FakePreview
) -> None:
    preview.template_media_ids = False
    out = run_brief(make_context(), two_slot_brief(studio_root))
    assert out.status == "refused"
    assert out.publish is not None
    details = " ".join(g.detail for g in out.publish.failures if g.gate == "d7_images")
    assert 'data-media-id="studio-picture-hero"' in details
    assert 'data-media-id="studio-picture-og"' in details


def test_f1_the_readme_template_passes_with_two_slots(
    make_context: Callable[..., Context], studio_root: Path
) -> None:
    out = run_brief(make_context(), two_slot_brief(studio_root))
    assert out.status == "published", out.publish and out.publish.failures


def test_f1_a_bridge_with_no_media_discovery_is_refused_clearly(
    make_context: Callable[..., Context], write_brief: Callable[..., Path], preview: FakePreview
) -> None:
    preview.media_discovery = False
    out = run_brief(make_context(), write_brief())
    assert out.status == "refused"
    assert out.publish is not None
    assert {g.detail for g in out.publish.failures if g.gate == "d7_images"} == {NO_MEDIA}


def test_f2_a_retry_never_binds_to_the_previous_attempts_page(
    make_context: Callable[..., Context],
    studio_root: Path,
    clock: FakeClock,
    preview: FakePreview,
) -> None:
    health = {"cafe": "OK", "barber": "CRITICAL"}  # attempt 1: barber broken
    tabs: dict[str, FakeBridge] = {}

    def factory(base_url: str) -> FakeBridge:
        site = base_url.removeprefix("http://preview.test/").split("/", 1)[0]
        if site not in tabs:
            tab = tabs[site] = preview.bridge(base_url)
            tab.page_health = lambda t=tab: {  # type: ignore[method-assign]
                "success": True,
                "data": {"summary": health[t.site], "findings": []},
            }
        return tabs[site]

    p = studio_root / "briefs" / "b.yaml"
    p.write_text(
        "id: b\npurpose: t\n"
        "targets: [{site: cafe-template, slot: hero}, {site: barber-template, slot: hero}]\n"
        'aspect_ratio: "16:9"\nprompt: A warm interior\nprovider_preference: [fake]\n'
    )
    ctx = make_context(bridge=factory)
    first = run_brief(ctx, p)
    assert first.status == "refused"
    # Between attempts: barber fixed, and the cafe tab is frozen on attempt 1's
    # (healthy) document — the retry's navigate never lands.
    health["barber"] = "OK"
    preview.ignore_navigate = True
    clock.sleep(5)
    retry = publish_batch(
        ctx.conn,
        ctx.sites,
        first.batch_id,
        ctx.notifier,
        ctx.clock,
        bridge=factory,
        fetch=preview.fetch,
        sleep=ctx.sleep,
    )
    assert not retry.published
    assert any(g.gate == "d7_navigate" and "cafe" in g.subject for g in retry.failures), (
        retry.failures
    )


# ---- eighth review ---------------------------------------------------------------


def test_r8_navigate_asks_for_a_hard_load() -> None:
    import json

    import httpx

    from qontinui_studio.gates import HttpUiBridge

    seen: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"success": True, "data": {"delivered": True}})

    bridge = HttpUiBridge(
        "http://preview.test/__ui-bridge", httpx.Client(transport=httpx.MockTransport(handler))
    )
    bridge.navigate("http://preview.test/index.html?studio_batch=b&studio_attempt=a")
    assert seen == [
        {
            "url": "http://preview.test/index.html?studio_batch=b&studio_attempt=a",
            "mode": "hard",
            "hard": True,
        }
    ]


def test_r8_a_client_side_navigation_keeps_the_stale_document(
    make_context: Callable[..., Context],
    write_brief: Callable[..., Path],
    studio_root: Path,
    clock: FakeClock,
    preview: FakePreview,
) -> None:
    """The fake models a router app: without hard=True the URL changes and the
    DOM stays. D7 asks for a hard load, so a republish sees the new document;
    if it did not, the stale document's ?sha= would refuse it."""
    tabs: dict[str, FakeBridge] = {}

    def factory(base_url: str) -> FakeBridge:
        site = base_url.removeprefix("http://preview.test/").split("/", 1)[0]
        return tabs.setdefault(site, preview.bridge(base_url))

    first = run_brief(make_context(bridge=factory), write_brief(name="a.yaml"))
    assert first.status == "published"
    clock.sleep(5)
    second = run_brief(make_context(bridge=factory), write_brief(name="b.yaml"))
    assert second.status == "published", second.publish and second.publish.failures

    # The same tabs, navigated client-side only: the DOM is still batch B's.
    cafe = tabs["cafe"]
    before = cafe.snapshot()["elements"]
    cafe.navigate("http://preview.test/cafe/index.html?studio_batch=x&studio_attempt=y")
    assert cafe.url and "studio_attempt=y" in cafe.url
    assert cafe.snapshot()["elements"] == before


def test_r8_slot_names_cannot_make_media_ids_collide() -> None:
    from qontinui_studio.gates import slot_media_ids

    names = ["hero", "hero-img", "img-hero", "picture-hero", "og", "picture", "img"]
    ids = [mid for n in names for mid in slot_media_ids(n)]
    assert len(ids) == len(set(ids))


# ---- ninth review ----------------------------------------------------------------


def test_r9_fragment_urls_are_refused(tmp_path: Path) -> None:
    import pytest as _pytest

    from qontinui_studio.sites import Site

    base = {
        "id": "spa",
        "name": "SPA",
        "kind": "demo",
        "commercial": False,
        "publish_dir": str(tmp_path / "live"),
        "preview_dir": str(tmp_path / "preview"),
        "ui_bridge_url": "http://preview.test/__ui-bridge",
        "preview_url": "http://preview.test/app/",
        "preview_manifest_url": "http://preview.test/app/studio-manifest.json",
        "slots": {"hero": {"width": 64, "height": 36}},
    }
    assert Site.model_validate(base)
    for field_name, value in (
        ("preview_url", "http://preview.test/app/#/home"),
        ("preview_manifest_url", "http://preview.test/app/studio-manifest.json#x"),
        ("ui_bridge_url", "http://preview.test/__ui-bridge#x"),
    ):
        with _pytest.raises(ValueError, match="must not contain a #fragment"):
            Site.model_validate({**base, field_name: value})


def test_r9_target_params_land_in_the_query_and_fragments_must_match(tmp_path: Path) -> None:
    from urllib.parse import parse_qs, urlparse

    from qontinui_studio import gates
    from qontinui_studio.sites import Site

    site = Site.model_validate(
        {
            "id": "s",
            "name": "s",
            "kind": "demo",
            "commercial": False,
            "publish_dir": str(tmp_path / "live"),
            "preview_dir": str(tmp_path / "preview"),
            "ui_bridge_url": "http://preview.test/__ui-bridge",
            "preview_url": "http://preview.test/app/?lang=de",
            "preview_manifest_url": "http://preview.test/app/studio-manifest.json",
            "slots": {"hero": {"width": 64, "height": 36}},
        }
    )
    target = gates.preview_target(site, "B", "new")
    q = parse_qs(urlparse(target).query)
    assert q == {"lang": ["de"], "studio_batch": ["B"], "studio_attempt": ["new"]}
    assert gates.same_page(target, target)
    assert not gates.same_page(target + "#other", target)
    stale = "http://preview.test/app/?lang=de&studio_batch=A&studio_attempt=old"
    assert not gates.same_page(stale, target)
