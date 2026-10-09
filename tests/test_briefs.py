from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

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
    # Only problem: neither billed API has an operator-set quota (honest D4 outcome).
    assert len(problems) == 1 and "no preferred provider is schedulable" in problems[0]


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
