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
    assert picture("studio-hero") == "studio-hero"
    assert img("hero.webp", "studio-hero-img") == "studio-hero-img"


def test_f1_a_template_without_media_ids_is_refused_naming_the_missing_id(
    make_context: Callable[..., Context], studio_root: Path, preview: FakePreview
) -> None:
    preview.template_media_ids = False
    out = run_brief(make_context(), two_slot_brief(studio_root))
    assert out.status == "refused"
    assert out.publish is not None
    details = " ".join(g.detail for g in out.publish.failures if g.gate == "d7_images")
    assert 'data-media-id="studio-hero"' in details
    assert 'data-media-id="studio-og"' in details


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
