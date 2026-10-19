"""Shared fixtures for ``studio compile`` tests: a checkout of a consumer
repository with slot specs, a compile target with a (fake) D7 preview, fake
input readers and a fake image store. Nothing here touches the network."""

from __future__ import annotations

import textwrap
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from qontinui_studio.compiler.image_store import StoreOutcome
from qontinui_studio.compiler.inputs import Digest, ImagegenManifest
from qontinui_studio.compiler.run import (
    AltText,
    CompileOptions,
    CompileReport,
    Readers,
    compile_slots,
)
from qontinui_studio.compiler.target import load_target
from qontinui_studio.pipeline import Context
from qontinui_studio.postprocess import sha256_file

IMAGERY = "a" * 64
IMAGERY_2 = "b" * 64

TARGET = """
id: web
name: Qontinui web
repo: qontinui/qontinui-web
commercial: true
providers: [fake]
candidates: 2
ui_bridge_url: http://preview.test/web/__ui-bridge
preview_url: http://preview.test/web/index.html
preview_dir: ../../previews/web
preview_manifest_url: http://preview.test/web/studio-manifest.json
"""


def spec_yaml(
    slot: str,
    role: str = "hero",
    *,
    aspect: str = "16/9",
    alt: str = "kind: decorative",
    job: str = "Show a calm automation run so a visitor reads that it takes work off their hands.",
    exemplars: str | None = None,
    lora: str | None = None,
    text: str = "kind: forbidden",
    must: str = "[a laptop showing a workflow canvas]",
    breakpoints: str = "[160, 320]",
    themes: str = "[dark]",
    dpr: str = "[1]",
) -> str:
    style = ["imagery: domain_spec/visual-system#imagery"]
    if exemplars:
        style.append(f"exemplars: {exemplars}")
    if lora:
        style.append(f"lora: {lora}")
    style_block = "\n".join(f"  {line}" for line in style)
    return (
        textwrap.dedent(
            f"""\
        slot: {slot}
        role: {role}
        job: "{job}"
        audience: audience_profile/external-operator
        contexts:
          breakpoints: {breakpoints}
          themes: {themes}
          dpr: {dpr}
        aspect:
          preferred: {aspect}
          allowed: []
        safe_areas:
          kind: derived
        focal:
          region: {{x: 0.3, y: 0.2, width: 0.4, height: 0.6}}
          must_survive_crop: true
        style:
        """
        )
        + style_block
        + textwrap.dedent(
            f"""
        content:
          must: {must}
          must_not: [third-party logos]
        text_in_image:
          {text}
        alt:
          {alt}
        thresholds:
          kind: from_visual_system
        """
        )
    )


@dataclass
class FakeStore:
    """The image store: records every upload and proposal; ``answer`` decides
    what an upload reports."""

    uploads: list[dict[str, Any]] = field(default_factory=list)
    proposals: list[dict[str, Any]] = field(default_factory=list)
    answer: str = "stored"

    def upload(self, path: Path, kind: str, provenance: dict[str, Any] | None) -> StoreOutcome:
        self.uploads.append(
            {"name": path.name, "kind": kind, "sha256": sha256_file(path), "provenance": provenance}
        )
        if self.answer == "unknown":
            return StoreOutcome("unknown", "fake store: no answer")
        return StoreOutcome("stored", "fake store")

    def propose(self, **kw: Any) -> StoreOutcome:
        self.proposals.append(kw)
        return StoreOutcome("proposed", "fake store")


@dataclass
class CompileEnv:
    studio_root: Path
    checkout: Path
    make_context: Callable[..., Context]
    imagery: dict[str, Digest] = field(default_factory=dict)
    store: FakeStore = field(default_factory=FakeStore)
    imagegen: ImagegenManifest = field(
        default_factory=lambda: ImagegenManifest(Path("/nonexistent"))
    )
    ctx: Context | None = None

    @property
    def slots_dir(self) -> Path:
        return self.checkout / "frontend" / "image-slots"

    @property
    def publish_dir(self) -> Path:
        return self.checkout / "frontend" / "public" / "image-slots"

    def write_spec(self, slot: str, role: str = "hero", **kw: Any) -> Path:
        path = self.slots_dir / f"{slot}.yaml"
        path.write_text(spec_yaml(slot, role, **kw))
        return path

    def write_target(self, extra: str = "") -> None:
        (self.studio_root / "targets").mkdir(exist_ok=True)
        (self.studio_root / "targets" / "web.yaml").write_text(TARGET + extra)

    def imagery_reader(self, ref: str) -> Digest:
        return self.imagery.get(ref, Digest(IMAGERY, "domain_spec/visual-system@v3#imagery"))

    def context(self) -> Context:
        if self.ctx is None:
            self.ctx = self.make_context()
        return self.ctx

    def run(
        self,
        *,
        changed: bool = False,
        dry_run: bool = False,
        only: set[str] | None = None,
        alt_texts: dict[str, AltText] | None = None,
        checkout: Path | None = None,
    ) -> CompileReport:
        target = load_target(self.studio_root / "targets" / "web.yaml", self.studio_root)
        slots_dir = (checkout or self.checkout) / "frontend" / "image-slots"
        options = CompileOptions(
            slots_dir=slots_dir,
            target=target,
            changed_only=changed,
            dry_run=dry_run,
            only=frozenset(only) if only else None,
            alt_texts=alt_texts or {},
        )
        readers = Readers(imagery=self.imagery_reader, imagegen=self.imagegen, store=self.store)
        return compile_slots(self.context(), options, readers)


def make_checkout(path: Path) -> Path:
    (path / "frontend" / "image-slots").mkdir(parents=True)
    # A linked worktree's .git is a file naming its git dir,
    # <common>/worktrees/<name>, which holds git's `commondir` pointer.
    gitdir = path.parent / f".git-common-{path.name}" / "worktrees" / path.name
    gitdir.mkdir(parents=True)
    (gitdir / "commondir").write_text("../..\n")
    (path / ".git").write_text(f"gitdir: {gitdir}\n")
    return path
