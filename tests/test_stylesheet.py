"""The style-sheet page rendered from style/ (plan
2026-10-09-qontinui-marketing-studio-animated-process-explainers, Phase 1)."""

from __future__ import annotations

import shutil
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from click.testing import CliRunner

from qontinui_studio.briefs import load_brief, validate_brief
from qontinui_studio.cli import main
from qontinui_studio.errors import ConfigError
from qontinui_studio.library import Library
from qontinui_studio.pipeline import Context, run_brief
from qontinui_studio.providers import load_providers
from qontinui_studio.store import connect
from qontinui_studio.stylesheet import build_style_sheet, load_guide, load_icon, read_tokens_css
from qontinui_studio.workspace import Workspace

REPO = Path(__file__).resolve().parent.parent
STYLE = REPO / "style"
NOW = datetime(2026, 10, 17, tzinfo=UTC)


def _tokens_css(guide_tokens: list[str], tmp_path: Path, drop: str | None = None) -> Path:
    """A tokens.css declaring every token the guide names (values chosen so the
    palette hexes of the shipped look are all token values), plus a later
    override that must not win."""
    values = {
        "--qontinui-surface-canvas": "#111115",
        "--qontinui-surface-raised": "#1e1e22",
        "--qontinui-brand-primary": "#4a90d9",
        "--qontinui-brand-secondary": "#8b6bb5",
        "--qontinui-brand-success": "#4db89d",
    }
    lines = [":root {", "  /* a comment: --qontinui-fake: 1; */"]
    for t in guide_tokens:
        if t != drop:
            lines.append(f"  {t}: {values.get(t, '#123456')};")
    lines += ["}", "@media (prefers-reduced-motion: reduce) {",
              "  :root { --qontinui-duration-base: 0ms; } }"]  # fmt: skip
    path = tmp_path / "tokens.css"
    path.write_text("\n".join(lines))
    return path


def test_the_shipped_guide_loads_and_every_icon_meets_the_contract() -> None:
    guide = load_guide(STYLE)
    assert guide.id == guide.references.tag == "studio-style-v1"
    assert guide.references.target_approved >= 20
    for icon in guide.icons:
        markup = load_icon(STYLE / "icons" / f"{icon.id}.svg")
        assert markup.startswith("<svg") and "currentColor" in markup
    assert sorted(p.stem for p in (STYLE / "icons").glob("*.svg")) == sorted(
        i.id for i in guide.icons
    ), "every icon file is in the guide and every guide icon has a file"


def test_the_reference_briefs_validate_and_tag_the_set() -> None:
    guide = load_guide(STYLE)
    inventory = load_providers(REPO / "providers.yaml")
    assert len(guide.references.briefs) >= 1
    total = 0
    for rel in guide.references.briefs:
        brief = load_brief(REPO / rel, root=REPO).brief
        assert validate_brief(brief, {}, inventory) == []
        assert guide.references.tag in brief.tags
        assert all(not t.is_slot for t in brief.targets), "a reference brief publishes nowhere"
        assert brief.provider_preference == ["local-gpu"] and brief.priority == "bulk"
        assert brief.style_guide == STYLE / "studio-style-v1.md"
        total += brief.variants
    assert total >= guide.references.target_approved


def test_render_resolves_tokens_and_reports_no_drift(tmp_path: Path) -> None:
    guide = load_guide(STYLE)
    css = _tokens_css([t.token for t in guide.tokens.all()], tmp_path)
    tokens = read_tokens_css(css)
    assert tokens["--qontinui-duration-base"] != "0ms", "the first declaration wins"
    assert "--qontinui-fake" not in tokens, "comments are not declarations"
    sheet = build_style_sheet(
        guide, STYLE, tmp_path / "out", NOW, tokens=tokens, tokens_source=str(css), library=None
    )
    assert sheet.tokens_resolved == sheet.tokens_named == len(guide.tokens.all())
    assert sheet.fragment_drift == []
    page = sheet.page.read_text()
    assert "var(--qontinui-brand-primary)" in page and "UNRESOLVED" not in page
    assert "the reference set is UNKNOWN" in page


def test_render_without_tokens_says_unknown(tmp_path: Path) -> None:
    sheet = build_style_sheet(
        load_guide(STYLE), STYLE, tmp_path, NOW, tokens=None, tokens_source=None, library=None
    )
    assert sheet.tokens_resolved == 0
    assert "UNRESOLVED" in sheet.page.read_text()


def test_a_guide_token_missing_from_tokens_css_refuses(tmp_path: Path) -> None:
    guide = load_guide(STYLE)
    css = _tokens_css([t.token for t in guide.tokens.all()], tmp_path, "--qontinui-ease-enter")
    with pytest.raises(ConfigError, match="--qontinui-ease-enter"):
        build_style_sheet(
            guide, STYLE, tmp_path / "o", NOW, tokens=read_tokens_css(css), tokens_source="t",
            library=None,
        )  # fmt: skip


def test_a_look_colour_off_the_tokens_is_drift(tmp_path: Path) -> None:
    style = tmp_path / "style"
    shutil.copytree(STYLE, style)
    shutil.copytree(REPO / "briefs", tmp_path / "briefs")
    frag = style / "studio-style-v1.md"
    frag.write_text(frag.read_text() + " Accent #FF00FF, edge #fff.")
    guide = load_guide(style)
    css = _tokens_css([t.token for t in guide.tokens.all()], tmp_path)
    sheet = build_style_sheet(
        guide, style, tmp_path / "o", NOW, tokens=read_tokens_css(css), tokens_source="t",
        library=None,
    )  # fmt: skip
    assert sheet.fragment_drift == ["studio-style-v1: #FF00FF", "studio-style-v1: #FFF"]


@pytest.mark.parametrize(
    "body",
    [
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><script>alert(1)</script></svg>',
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><path d="M0 0" onload="x"/></svg>',
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><path d="M0 0" stroke="#f00"/></svg>',
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32"><path d="M0 0"/></svg>',
        '<!DOCTYPE svg [<!ENTITY a "b">]><svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"/>',
        '<svg viewBox="0 0 24 24"><path d="M0 0"/></svg>',
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><use href="#a"/></svg>',
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><image href="x.png"/></svg>',
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><foreignObject/></svg>',
        '<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" '
        'viewBox="0 0 24 24"><path d="M0 0" xlink:href="#a"/></svg>',
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><path d="M0 0" style="x"/></svg>',
    ],
)
def test_icons_outside_the_contract_are_refused(tmp_path: Path, body: str) -> None:
    path = tmp_path / "x.svg"
    path.write_text(body)
    with pytest.raises(ConfigError):
        load_icon(path)


def test_the_reference_set_is_the_library_tag_query(
    make_context: Callable[..., Context], write_brief: Callable[..., Path], tmp_path: Path
) -> None:
    ctx = make_context()
    brief = write_brief(variants=2)
    brief.write_text(brief.read_text() + "tags: [studio-style-v1]\n")
    run_brief(ctx, brief)
    lib = Library(ctx.conn)
    first = lib.assets()[0].uid
    lib.decide(first, "approved", "josh", date(2026, 10, 17))
    sheet = build_style_sheet(
        load_guide(STYLE), STYLE, tmp_path / "o", NOW, tokens=None, tokens_source=None, library=lib
    )
    assert (sheet.references_tagged, sheet.references_approved) == (2, 1)
    page = sheet.page.read_text()
    assert f"refs/{first}." in page and "1 approved of 2 tagged" in page
    assert len(list((tmp_path / "o" / "refs").iterdir())) == 1


def _copy_guide(studio_root: Path) -> None:
    shutil.copytree(STYLE, studio_root / "style")
    for b in (REPO / "briefs").glob("style-v1-*.yaml"):
        shutil.copy(b, studio_root / "briefs" / b.name)


def test_cli_style_render(studio_root: Path, tmp_path: Path) -> None:
    _copy_guide(studio_root)
    connect(Workspace(studio_root).db_path).close()  # an empty library
    guide = load_guide(STYLE)
    css = _tokens_css([t.token for t in guide.tokens.all()], tmp_path)
    r = CliRunner().invoke(
        main, ["--root", str(studio_root), "style", "render", "--tokens-css", str(css)]
    )
    assert r.exit_code == 0, r.output
    assert (studio_root / ".studio" / "style" / "index.html").is_file()
    assert f"tokens: {len(guide.tokens.all())}/{len(guide.tokens.all())} resolved" in r.output
    assert "reference set studio-style-v1: 0 approved of 0 tagged (target 20)" in r.output


def test_a_missing_reference_brief_refuses_the_guide(tmp_path: Path) -> None:
    style = tmp_path / "style"
    shutil.copytree(STYLE, style)
    with pytest.raises(ConfigError, match="reference brief"):
        load_guide(style)  # tmp_path has no briefs/


def test_a_missing_preview_file_renders_a_note(
    make_context: Callable[..., Context], write_brief: Callable[..., Path], tmp_path: Path
) -> None:
    ctx = make_context()
    run_brief(ctx, write_brief())
    lib = Library(ctx.conn)
    uid = lib.assets()[0].uid
    lib.tag(uid, "studio-style-v1", "josh", date(2026, 10, 17))
    lib.decide(uid, "approved", "josh", date(2026, 10, 17))
    for v in lib.variants(uid):
        Path(v.path).unlink()
    Path(lib.asset(uid).raw_path).unlink()
    sheet = build_style_sheet(
        load_guide(STYLE), STYLE, tmp_path / "o", NOW, tokens=None, tokens_source=None, library=lib
    )
    assert "preview not copied" in sheet.page.read_text()


def test_render_does_not_create_a_library(studio_root: Path) -> None:
    _copy_guide(studio_root)
    r = CliRunner().invoke(main, ["--root", str(studio_root), "style", "render"])
    assert r.exit_code == 0, r.output
    assert "UNKNOWN (library not read)" in r.output
    assert not (studio_root / ".studio" / "studio.db").exists()
