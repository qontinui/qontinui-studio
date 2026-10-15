from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from qontinui_studio import postprocess
from qontinui_studio.briefs import Brief, Target, load_brief, validate_brief
from qontinui_studio.errors import ConfigError
from qontinui_studio.providers import load_providers
from qontinui_studio.sites import load_sites

REPO = Path(__file__).resolve().parent.parent


def test_shipped_example_brief_loads_and_resolves_its_style_guide() -> None:
    loaded = load_brief(REPO / "briefs" / "example-hero.yaml")
    b = loaded.brief
    assert b.id == "template-hero-interior"
    assert {(t.site, t.slot) for t in b.targets if t.is_slot} == {
        ("cafe-template", "hero"),
        ("barber-template", "hero"),
    }
    assert b.style_guide is not None and b.style_guide.is_file()
    assert "Style guide:" in b.full_prompt()
    assert len(loaded.sha256) == 64


def test_shipped_example_cross_validates_except_for_schedulability() -> None:
    loaded = load_brief(REPO / "briefs" / "example-hero.yaml")
    problems = validate_brief(
        loaded.brief, load_sites(REPO / "sites"), load_providers(REPO / "providers.yaml")
    )
    # The local GPU (unmetered, first preference) is schedulable; neither billed
    # API has an operator-set quota, which no longer leaves the brief unrunnable.
    assert problems == []


@pytest.mark.parametrize(
    "target",
    [{"site": "a"}, {"slot": "b"}, {"site": "a", "slot": "b", "campaign": "c"}, {}],
)
def test_target_is_site_slot_or_campaign(target: dict[str, str]) -> None:
    with pytest.raises(ValueError):
        Target.model_validate(target)


@pytest.mark.parametrize("ratio", ["16x9", "16:0", "a:b", "16:9:1"])
def test_bad_aspect_ratio(ratio: str) -> None:
    with pytest.raises(ValueError):
        Brief.model_validate(
            {
                "id": "x",
                "purpose": "p",
                "targets": [{"campaign": "c"}],
                "aspect_ratio": ratio,
                "prompt": "p",
                "provider_preference": ["fake"],
            }
        )


def test_cross_validation_names_every_problem(
    studio_root: Path, write_brief: Callable[..., Path]
) -> None:
    path = write_brief(providers="fake, nope")
    text = path.read_text().replace("barber-template, slot: hero", "barber-template, slot: footer")
    path.write_text(text + "reference_images: [missing.png]\n")
    b = load_brief(path).brief
    problems = validate_brief(
        b, load_sites(studio_root / "sites"), load_providers(studio_root / "providers.yaml")
    )
    joined = "\n".join(problems)
    assert "no slot 'footer'" in joined
    assert "unknown provider 'nope'" in joined
    assert "missing.png" in joined


def test_unreadable_brief_is_a_config_error(tmp_path: Path) -> None:
    p = tmp_path / "b.yaml"
    p.write_text("- just a list")
    with pytest.raises(ConfigError):
        load_brief(p)


def test_campaign_and_slot_names_cannot_escape_paths() -> None:
    for bad in ("../x", "a/b", "a\\b", "x\x00", ".."):
        with pytest.raises(ValueError):
            Target.model_validate({"campaign": bad})
        with pytest.raises(ValueError):
            Target.model_validate({"site": "s", "slot": bad})


def test_style_guide_and_references_must_stay_under_the_root(
    studio_root: Path, write_brief: Callable[..., Path], tmp_path: Path
) -> None:
    outside = tmp_path / "secret.md"
    outside.write_text("x")
    path = write_brief()
    path.write_text(path.read_text().replace("style_guide: style.md", f"style_guide: {outside}"))
    with pytest.raises(ConfigError, match="outside the studio root"):
        load_brief(path, root=studio_root)
    path.write_text(
        path.read_text().replace(f"style_guide: {outside}", "style_guide: ../../secret.md")
    )
    with pytest.raises(ConfigError, match="outside the studio root"):
        load_brief(path, root=studio_root)


def test_validation_prechecks_local_tools_before_spending_quota(
    studio_root: Path, write_brief: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    site = studio_root / "sites" / "cafe.yaml"
    site.write_text(
        site.read_text().replace(
            "hero: {width: 1600, height: 900, formats: [webp, avif]}",
            "hero: {width: 1600, height: 900, formats: [webp, avif], remove_background: true}",
        )
    )
    monkeypatch.setattr(postprocess, "rembg_available", lambda: False)
    monkeypatch.setattr(postprocess, "format_supported", lambda fmt: fmt != "avif")
    problems = validate_brief(
        load_brief(write_brief()).brief,
        load_sites(studio_root / "sites"),
        load_providers(studio_root / "providers.yaml"),
    )
    joined = "\n".join(problems)
    assert "rembg" in joined
    assert "cannot encode 'avif'" in joined


def test_real_business_sites_must_state_slot_categories(tmp_path: Path) -> None:
    (tmp_path / "s.yaml").write_text(
        "id: shop\nname: Shop\nkind: real_business\ncommercial: true\npublish_dir: out\n"
        "slots:\n  hero: {width: 10, height: 10}\n"
    )
    with pytest.raises(ConfigError, match="must state every slot's category"):
        load_sites(tmp_path)
    (tmp_path / "s.yaml").write_text(
        "id: shop\nname: Shop\nkind: demo\ncommercial: false\npublish_dir: out\n"
        "slots:\n  ../evil: {width: 10, height: 10}\n"
    )
    with pytest.raises(ConfigError, match="bad slot name"):
        load_sites(tmp_path)


def test_duplicate_targets_and_providers_are_rejected() -> None:
    base = {
        "id": "x",
        "purpose": "p",
        "aspect_ratio": "1:1",
        "prompt": "p",
    }
    with pytest.raises(ValueError, match="duplicate target"):
        Brief.model_validate(
            {
                **base,
                "targets": [{"site": "a", "slot": "b"}, {"site": "a", "slot": "b"}],
                "provider_preference": ["fake"],
            }
        )
    with pytest.raises(ValueError, match="names a provider twice"):
        Brief.model_validate(
            {**base, "targets": [{"campaign": "c"}], "provider_preference": ["fake", "fake"]}
        )
