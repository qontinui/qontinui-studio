"""Assemble a launch bundle: a static directory holding the landing page, the
"How this was built" page, OG/social images, the social-post set, the film,
and ``launch-manifest.json`` describing every file in it.

Authority (D1): the "How this was built" page is rendered ONLY from the build
record. Every value on it carries the document field it came from
(``data-source``), and every null is shown as ``unknown — <reason>``. With no
record, the page says so and shows no numbers at all.
"""

from __future__ import annotations

import hashlib
import html
import json
import os
import re
import shutil
import tempfile
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from PIL import Image

from qontinui_studio.errors import ConfigError, StudioError
from qontinui_studio.gates import slot_media_ids
from qontinui_studio.launch import render
from qontinui_studio.launch.capture import CaptureResult
from qontinui_studio.launch.decor import DecorOutcome
from qontinui_studio.launch.facts import Fact, derive_facts, facts_by_key
from qontinui_studio.launch.posts import write_posts
from qontinui_studio.launch.product import LaunchProduct
from qontinui_studio.launch.record import (
    BuildRecord,
    FetchedRecord,
    FetchOutcome,
    NoBuildRecord,
    reason_text,
)
from qontinui_studio.launch.video import FilmInputs, ReplaySegment, Still, compose_film
from qontinui_studio.locks import LOCK_NAME
from qontinui_studio.postprocess import sha256_file
from qontinui_studio.publish import JOURNAL_PREFIX

BUNDLE_SCHEMA = "launch-bundle/1"
MANIFEST = "launch-manifest.json"
FOOTER_TEXT = "Built by the Qontinui software factory"
PAGES = ("index.html", "how-it-was-built.html")
#: The headline facts a social card shows, best first (known ones only).
CARD_FACTS = ("work_units_shipped", "prs_landed", "wall_clock", "ci_attempts", "red_ci_fixed")

CSS = """
:root { --bg:#fbfbfc; --fg:#17181c; --muted:#62656f; --card:#fff; --line:#e2e3e8; --accent:#2d6cdf; }
@media (prefers-color-scheme: dark) { :root { --bg:#121419; --fg:#eceef3; --muted:#9a9eab; --card:#1b1e25; --line:#2b2f38; --accent:#6ea8ff; } }
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--fg); font:16px/1.55 system-ui,-apple-system,sans-serif; }
main { max-width:960px; margin:0 auto; padding:24px 16px 48px; }
h1 { font-size:clamp(28px,5vw,44px); line-height:1.15; margin:8px 0; }
h2 { font-size:22px; margin:36px 0 10px; }
p.tagline { font-size:20px; color:var(--muted); margin:0 0 16px; }
a { color:var(--accent); }
figure { margin:16px 0; }
figure img, video { width:100%; height:auto; border:1px solid var(--line); border-radius:8px; display:block; }
figcaption { font-size:13px; color:var(--muted); margin-top:4px; }
.grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(260px,1fr)); gap:16px; }
.facts { display:grid; grid-template-columns:repeat(auto-fill,minmax(200px,1fr)); gap:12px; }
.fact { background:var(--card); border:1px solid var(--line); border-radius:8px; padding:12px; }
.fact .value { font-size:26px; font-weight:600; overflow-wrap:anywhere; }
.fact .value.unknown { font-size:14px; font-weight:400; color:var(--muted); }
.fact .label { font-size:14px; }
.fact .source, .src { font:12px ui-monospace,monospace; color:var(--muted); }
.table-wrap { overflow-x:auto; }
dl.src dd { margin:0 0 6px; overflow-wrap:anywhere; }
main p, li { overflow-wrap:anywhere; }
table { border-collapse:collapse; width:100%; font-size:14px; }
th, td { text-align:left; padding:6px 8px; border-bottom:1px solid var(--line); vertical-align:top; }
td.unknown { color:var(--muted); }
footer { border-top:1px solid var(--line); margin-top:48px; padding:16px; text-align:center; font-size:14px; }
.notice { background:var(--card); border:1px solid var(--line); border-left:4px solid var(--accent); padding:12px; border-radius:6px; }
"""


@dataclass
class BundleResult:
    out_dir: Path
    manifest: dict[str, Any]
    notes: list[str] = field(default_factory=list)

    @property
    def has_record(self) -> bool:
        return bool(self.manifest["record"]["present"])


def _e(value: object) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _page(
    title: str,
    body: str,
    record_url: str | None,
    head_extra: str = "",
    scripts: tuple[str, ...] = (),
) -> str:
    # `publish.page_scripts`, validated as site-root-relative .js paths
    # (product.PAGE_SCRIPT_RE); escaped anyway, as every attribute here is.
    head_extra += "".join(f"<script type='module' src='{_e(src)}'></script>" for src in scripts)
    if record_url:
        footer = f'<a class="factory" href="{_e(record_url)}">{FOOTER_TEXT}</a> — the public build record'
    else:
        footer = f"{FOOTER_TEXT} — no public build record has been published yet"
    return (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>{_e(title)}</title><link rel='stylesheet' href='styles.css'>{head_extra}</head>"
        f"<body><main>{body}</main><footer>{footer}</footer></body></html>\n"
    )


def _fact_card(fact: Fact) -> str:
    cls = "value" if fact.known else "value unknown"
    return (
        f"<div class='fact' data-fact='{_e(fact.key)}' data-source='{_e(fact.source)}'>"
        f"<div class='{cls}'>{_e(fact.display())}</div>"
        f"<div class='label'>{_e(fact.label)}</div>"
        f"<div class='source'>from <code>{_e(fact.source)}</code></div></div>"
    )


def _cell(record: BuildRecord, value: object, source: str) -> str:
    if value is None:
        return f"<td class='unknown' data-source='{_e(source)}'>unknown — {_e(record.unknown_reason(source))}</td>"
    return f"<td data-source='{_e(source)}'>{_e(value)}</td>"


def _table(headers: list[str], rows: list[str]) -> str:
    head = "".join(f"<th>{_e(h)}</th>" for h in headers)
    return f"<div class='table-wrap'><table><thead><tr>{head}</tr></thead><tbody>{''.join(rows)}</tbody></table></div>"


def how_it_was_built(product: LaunchProduct, outcome: FetchOutcome, record_url: str | None) -> str:
    """The page rendered from the build record alone."""
    if isinstance(outcome, NoBuildRecord):
        body = (
            f"<h1>How {_e(product.title)} was built</h1>"
            "<p class='notice' data-record='absent'>No build record is available for "
            f"<code>{_e(product.slug)}</code> yet ({_e(outcome.kind)}): {_e(outcome.detail)}. "
            "This page shows numbers only from the factory's published build record, so it shows none.</p>"
        )
        return _page(
            f"How {product.title} was built", body, record_url, scripts=product.publish.page_scripts
        )
    rec = outcome.record
    parts = [
        f"<h1>How {_e(rec.product.title)} was built</h1>",
        "<p>Everything below is rendered from the product's build record "
        f"(<code>{_e(rec.schema_)}</code>), a document the Qontinui software factory exports from its "
        "digital twin of the development process. Nothing on this page is hand-written; each value names "
        "the document field it came from.</p>",
        "<dl class='src'>",
        (
            f"<dt>record</dt><dd><a href='{_e(record_url)}'>{_e(record_url)}</a></dd>"
            if record_url and outcome.publishable
            else f"<dt>record</dt><dd>preview from a {_e(outcome.source)} record — not "
            "publishable; a launch publishes only from the public snapshot</dd>"
        ),
        f"<dt>generated_at</dt><dd data-source='generated_at'>{_e(rec.generated_at)}</dd>",
        f"<dt>content_sha256</dt><dd>{_e(outcome.content_sha256)}</dd>",
    ]
    if outcome.version is not None:
        parts.append(
            f"<dt>published version</dt><dd>{outcome.version} ({_e(outcome.published_at)})</dd>"
        )
    parts.append("</dl>")
    parts.append(
        "<p>Repositories: "
        + ", ".join(
            f"<a data-source='product.repos' href='https://github.com/{_e(r)}'>{_e(r)}</a>"
            for r in rec.product.repos
        )
        + "</p>"
    )
    parts.append("<h2>In numbers</h2><div class='facts'>")
    parts += [_fact_card(f) for f in derive_facts(rec)]
    parts.append("</div>")

    parts.append("<h2>Plans</h2>")
    parts.append(
        _table(
            ["Work unit", "Title", "Status", "Vetted", "Shipped"],
            [
                "<tr>"
                + _cell(rec, u.slug, "work_units[].slug")
                + _cell(rec, u.title, "work_units[].title")
                + _cell(rec, u.status, "work_units[].status")
                + _cell(rec, u.vetted_at, "work_units[].vetted_at")
                + _cell(rec, u.shipped_at, "work_units[].shipped_at")
                + "</tr>"
                for u in rec.work_units
            ],
        )
        if rec.work_units
        else "<p>The record lists no work units.</p>"
    )
    parts.append("<h2>Pull requests</h2>")
    pr_rows = []
    for p in rec.prs:
        link = (
            f"<a href='https://github.com/{_e(p.repo)}/pull/{p.number}'>{_e(p.repo)}#{p.number}</a>"
        )
        pr_rows.append(
            f"<tr><td data-source='prs[].repo prs[].number'>{link}</td>"
            + _cell(rec, p.title, "prs[].title")
            + _cell(rec, p.opened_at, "prs[].opened_at")
            + _cell(rec, p.landed_at, "prs[].landed_at")
            + _cell(rec, p.ci_duration_secs, "prs[].ci_duration_secs")
            + _cell(rec, p.attempts, "prs[].attempts")
            + "</tr>"
        )
    parts.append(
        _table(["PR", "Title", "Opened", "Landed", "CI seconds", "CI attempts"], pr_rows)
        if pr_rows
        else "<p>The record lists no pull requests.</p>"
    )
    parts.append("<h2>Timeline</h2>")
    parts.append(
        _table(
            ["At", "Work unit", "From", "To"],
            [
                "<tr>"
                + _cell(rec, t.at, "timeline[].at")
                + _cell(rec, t.work_unit, "timeline[].work_unit")
                + (
                    "<td data-source='timeline[].from_status'>(created)</td>"
                    if t.from_status is None
                    else _cell(rec, t.from_status, "timeline[].from_status")
                )
                + _cell(rec, t.to_status, "timeline[].to_status")
                + "</tr>"
                for t in rec.timeline
            ],
        )
        if rec.timeline
        else "<p>The record lists no transitions.</p>"
    )
    parts.append("<h2>What this record does not establish</h2>")
    if rec.unknowns:
        parts.append(
            "<ul>"
            + "".join(
                f"<li data-source='unknowns'><code>{_e(where)}</code>: {_e(reason_text(code))}</li>"
                for where, _, code in (u.partition(": ") for u in rec.unknowns)
            )
            + "</ul>"
        )
    else:
        parts.append("<p data-source='unknowns'>The record lists no unknowns.</p>")
    return _page(
        f"How {rec.product.title} was built",
        "".join(parts),
        record_url,
        scripts=product.publish.page_scripts,
    )


def film_caption(film: dict[str, Any]) -> str:
    """The line under the film. It names a walkthrough only when a walkthrough
    segment is in this film: a capture-less launch holds the build-record
    summary alone, and the page must not claim otherwise (D6)."""
    names = [str(s.get("name") or "") for s in film.get("segments") or []]
    if any(name.startswith("walkthrough") for name in names):
        return "A walkthrough of the running product and its build record."
    return "A summary of the product's build record."


def landing_page(
    product: LaunchProduct,
    facts: list[Fact],
    screenshots: list[tuple[str, str | None, str, tuple[int, int]]],
    hero: tuple[str, str, str, tuple[int, int]] | None,
    film: dict[str, Any] | None,
    record_url: str | None,
    og_image: str,
) -> str:
    og_base = (product.publish.public_url or "").rstrip("/")
    og_url = f"{og_base}/{og_image}" if og_base else og_image
    head = (
        f"<meta name='description' content='{_e(product.tagline)}'>"
        f"<meta property='og:title' content='{_e(product.title)}'>"
        f"<meta property='og:description' content='{_e(product.tagline)}'>"
        f"<meta property='og:image' content='{_e(og_url)}'>"
        "<meta property='og:type' content='website'>"
        "<meta name='twitter:card' content='summary_large_image'>"
    )
    parts = [f"<h1>{_e(product.title)}</h1>", f"<p class='tagline'>{_e(product.tagline)}</p>"]
    if hero is not None:
        src, label, media_id, (w, h) = hero
        parts.append(
            f"<figure class='decorative' data-ai-generated='true'><img src='{_e(src)}' "
            f"loading='eager' data-media-id='{_e(media_id)}' width='{w}' height='{h}' "
            f"alt='{_e(label)}'>"
            f"<figcaption>{_e(label)}</figcaption></figure>"
        )
    parts.append(f"<p>{_e(product.description)}</p>")
    links = []
    if product.homepage:
        links.append(f"<a href='{_e(product.homepage)}'>Open {_e(product.title)}</a>")
    links += [f"<a href='https://github.com/{_e(r)}'>{_e(r)}</a>" for r in product.repos]
    parts.append("<p>" + " · ".join(links) + "</p>")
    video = film.get("path") if film else None
    if film and video:
        parts.append(
            "<h2>Watch</h2>"
            f"<video controls preload='metadata' src='{_e(video)}'></video>"
            f"<p class='src'>{_e(film_caption(film))}</p>"
        )
    if screenshots:
        parts.append("<h2>Screenshots</h2><div class='grid'>")
        for src, caption, media_id, (w, h) in screenshots:
            text = caption or "Screenshot of the running product"
            parts.append(
                f"<figure class='screenshot' data-ai-generated='false'><img src='{_e(src)}' "
                f"loading='eager' data-media-id='{_e(media_id)}' width='{w}' height='{h}' "
                f"alt='{_e(text)}'>"
                f"<figcaption>{_e(text)} (captured from the running product)</figcaption></figure>"
            )
        parts.append("</div>")
    parts.append("<h2>How it was built</h2>")
    if facts:
        parts.append("<div class='facts'>" + "".join(_fact_card(f) for f in facts[:6]) + "</div>")
    else:
        parts.append(
            "<p class='notice' data-record='absent'>No build record has been published yet.</p>"
        )
    parts.append("<p><a href='how-it-was-built.html'>The full build record, rendered</a></p>")
    return _page(product.title, "".join(parts), record_url, head, product.publish.page_scripts)


class BundleRefused(StudioError):
    """Assembly produced something that must never become a bundle."""


def hidden_component(rel: str) -> bool:
    return any(part.startswith(".") for part in Path(rel).parts)


def media_group(rel: str) -> str:
    """The D7 group name of a bundle image: a readable slug of its path plus a
    hash of the EXACT path (``screenshots/home_page.png`` and
    ``screenshots/home-page.png`` slug alike but never collide). Its ``<img>``
    carries ``data-media-id="studio-img-<group>"`` (:func:`gates.slot_media_ids`),
    so UI Bridge registers it under that id and D7 finds it by id, never by
    guessing from file names."""
    stem = rel.rsplit(".", 1)[0]
    readable = re.sub(r"[^a-z0-9]+", "-", stem.lower()).strip("-")
    return f"launch-{readable}-{hashlib.sha256(rel.encode()).hexdigest()[:10]}"


def img_media_id(rel: str) -> str:
    return slot_media_ids(media_group(rel))[1]


def image_size(path: Path) -> tuple[int, int]:
    """An image's pixel size, written as the ``<img>`` width/height so the
    browser lays it out (and the bridge registers it) before it loads."""
    with Image.open(path) as img:
        return img.size


def render_pages(
    out_dir: Path,
    product: LaunchProduct,
    outcome: FetchOutcome,
    images: list[dict[str, Any]],
    film: dict[str, Any] | None,
) -> dict[str, str]:
    """The two pages, from exactly these inputs: the product definition, the
    record (or its absence), the bundle's image entries and the film's manifest
    entry.

    It is the ONE renderer: assembly writes its output, and the public_text
    gate re-runs it — once to prove the bundle's pages are this render, and
    once with every record string replaced by a placeholder, so the local-leak
    scan sees only what the kit itself composed (masking by position, never by
    matching content)."""
    record_url = product.record_url() if isinstance(outcome, FetchedRecord) else None
    facts = derive_facts(outcome.record) if isinstance(outcome, FetchedRecord) else []
    shots = [(str(i["path"]), i.get("caption")) for i in images if i.get("kind") == "screenshot"]
    hero = next(
        (
            (str(i["path"]), str(i.get("label") or ""))
            for i in images
            if i.get("kind") == "decorative" and Path(str(i["path"])).stem == "hero"
        ),
        None,
    )
    return {
        "how-it-was-built.html": how_it_was_built(product, outcome, record_url),
        "index.html": landing_page(
            product,
            facts,
            [
                (image_ref(out_dir, rel), caption, img_media_id(rel), image_size(out_dir / rel))
                for rel, caption in shots
            ],
            None
            if hero is None
            else (
                image_ref(out_dir, hero[0]),
                hero[1],
                img_media_id(hero[0]),
                image_size(out_dir / hero[0]),
            ),
            film,
            record_url,
            image_ref(out_dir, "social/og.png"),
        ),
    }


def image_ref(out_dir: Path, rel: str) -> str:
    """How a page references a bundle image: ``<rel>?sha=<sha256>`` (the
    asset pipeline's cache-buster rule, which D7's image check verifies).
    Every URL naming a bundle image carries it — ``src``, and any ``srcset``
    or ``<picture>`` source a page ever adds — and images render with
    ``loading='eager'`` so D7 can see them load."""
    return f"{rel}?sha={sha256_file(out_dir / rel)}"


def _copy(src: Path, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dest)
    return dest


def assemble(
    product: LaunchProduct,
    outcome: FetchOutcome,
    out_dir: Path,
    launch_id: str,
    clock: Callable[[], datetime],
    capture: CaptureResult | None = None,
    capture_note: str | None = None,
    decor: DecorOutcome | None = None,
    replay: ReplaySegment | None = None,
    make_video: bool = True,
) -> BundleResult:
    """Write the bundle to ``out_dir``.

    ``out_dir`` is replaced only when it is absent, empty, or a previous launch
    bundle (it holds a ``launch-manifest.json`` with schema ``launch-bundle/1``)
    — anything else is refused, never wiped. The bundle is built in a temporary
    sibling and renamed into place, so a failed build leaves ``out_dir`` as it was."""
    check_out_dir(out_dir)
    out_dir.parent.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex
    building = out_dir.with_name(f".{out_dir.name}.building-{token}")
    building.mkdir()
    try:
        result = _assemble_into(
            product,
            outcome,
            building,
            launch_id,
            clock,
            capture,
            capture_note,
            decor,
            replay,
            make_video,
        )
        old = out_dir.with_name(f".{out_dir.name}.old-{token}")
        check_out_dir(out_dir)
        if out_dir.exists():
            os.replace(out_dir, old)
        os.replace(building, out_dir)
        shutil.rmtree(old, ignore_errors=True)
    except BaseException:
        shutil.rmtree(building, ignore_errors=True)
        raise
    return BundleResult(out_dir, result.manifest, result.notes)


def check_out_dir(out_dir: Path) -> None:
    """Refuse to replace a directory that is not a launch bundle."""
    if not out_dir.exists():
        return
    if not out_dir.is_dir():
        raise ConfigError(f"refusing to replace {out_dir}: not a directory")
    if not any(out_dir.iterdir()):
        return
    studio_files = [
        p.name
        for p in out_dir.iterdir()
        if p.name == LOCK_NAME or (p.name.startswith(JOURNAL_PREFIX) and p.name.endswith(".json"))
    ]
    if studio_files:
        raise ConfigError(
            f"refusing to replace {out_dir}: it holds {studio_files[0]}, so it is (or was) a "
            "locked publish/preview/bundle directory"
        )
    manifest = out_dir / MANIFEST
    try:
        schema = json.loads(manifest.read_text(encoding="utf-8")).get("schema")
    except (OSError, ValueError, AttributeError):
        schema = None
    if schema != BUNDLE_SCHEMA:
        raise ConfigError(
            f"refusing to replace {out_dir}: it is not empty and holds no {BUNDLE_SCHEMA} "
            f"{MANIFEST}; choose another --out"
        )


def _assemble_into(
    product: LaunchProduct,
    outcome: FetchOutcome,
    out_dir: Path,
    launch_id: str,
    clock: Callable[[], datetime],
    capture: CaptureResult | None,
    capture_note: str | None,
    decor: DecorOutcome | None,
    replay: ReplaySegment | None,
    make_video: bool,
) -> BundleResult:
    decor = decor or DecorOutcome()
    now = clock()
    notes: list[str] = list(decor.notes)
    images: list[dict[str, Any]] = []
    record_present = isinstance(outcome, FetchedRecord)
    record_url = product.record_url() if record_present else None

    (out_dir / "styles.css").write_text(CSS, encoding="utf-8")

    # ---- the record --------------------------------------------------------------
    if isinstance(outcome, FetchedRecord):
        (out_dir / "build-record.json").write_text(
            json.dumps(
                {
                    "source": outcome.source,
                    "url": outcome.public_url(),
                    "public_slug": outcome.public_slug,
                    "content_sha256": outcome.content_sha256,
                    "version": outcome.version,
                    "published_at": outcome.published_at,
                    "document": outcome.document,
                },
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        facts = derive_facts(outcome.record)
    else:
        notes.append(outcome.describe())
        facts = []

    # ---- screenshots (real pixels from the running product) ---------------------
    walkthrough_stills: list[Still] = []
    walkthrough_video: Path | None = None
    if capture is not None:
        for s in capture.screenshots:
            dest = _copy(s.path, out_dir / "screenshots" / s.path.name)
            rel = f"screenshots/{s.path.name}"
            walkthrough_stills.append(Still(dest, s.hold))
            images.append(
                {
                    "path": rel,
                    "kind": "screenshot",
                    "ai_generated": False,
                    "caption": s.caption,
                }
            )
            # Where it was captured is operator-only (a local URL, a port): a
            # note, never a published field.
            notes.append(f"screenshot {rel} captured from {s.url}")
        if capture.video is not None:
            walkthrough_video = _copy(capture.video, out_dir / "video" / "walkthrough.webm")
        if capture.video_note:
            notes.append(f"walkthrough video: {capture.video_note}")
    if capture_note:
        notes.append(capture_note)

    # ---- decorative assets (Phase 2, already gated) ---------------------------------
    for asset in decor.assets:
        rel = f"assets/{asset.slot}{asset.path.suffix}"
        _copy(asset.path, out_dir / rel)
        images.append(
            {
                "path": rel,
                "kind": "decorative",
                "ai_generated": True,
                "label": asset.label,
                "asset_uid": asset.asset_uid,
                "batch_id": asset.batch_id,
                "provider": asset.provider,
                "model": asset.model,
            }
        )

    # ---- OG / social images -------------------------------------------------------
    by_key = facts_by_key(outcome.record) if isinstance(outcome, FetchedRecord) else {}
    card_facts = [by_key[k] for k in CARD_FACTS if k in by_key and by_key[k].known]
    for card in render.SOCIAL_CARDS:
        rel = f"social/{card.name}.png"
        render.social_card(card, product.title, product.tagline, card_facts, out_dir / rel)
        images.append(
            {
                "path": rel,
                "kind": "social",
                "ai_generated": False,
                "size": [card.width, card.height],
            }
        )

    # ---- posts --------------------------------------------------------------------
    write_posts(out_dir / "posts", product, by_key, record_url)

    # ---- film ---------------------------------------------------------------------------
    film: dict[str, Any] | None = None
    if make_video and isinstance(outcome, FetchedRecord):
        try:
            film = _film(
                product,
                outcome.record,
                facts,
                out_dir,
                walkthrough_video,
                walkthrough_stills,
                decor,
                replay,
                record_url,
            )
        except StudioError as e:
            notes.append(f"film not rendered: {e}")
    elif make_video:
        notes.append("film not rendered: its summary needs a build record")
    else:
        notes.append("film not rendered: disabled for this run")

    # Every image carries its sha256; pages reference images as
    # ``<path>?sha=<sha256>`` so D7 can prove the preview shows THESE bytes.
    for image in images:
        image["sha256"] = sha256_file(out_dir / image["path"])

    # ---- pages ------------------------------------------------------------------------
    for name, text in render_pages(out_dir, product, outcome, images, film).items():
        (out_dir / name).write_text(text, encoding="utf-8")

    manifest: dict[str, Any] = {
        "schema": BUNDLE_SCHEMA,
        "launch_id": launch_id,
        "created_at": now.isoformat(),
        "product": {"slug": product.slug, "title": product.title, "repos": product.repos},
        "record": (
            {
                "present": True,
                "source": outcome.source,
                # Only the public door's URL is ever written (an authed or
                # local URL never reaches a published file).
                "url": outcome.public_url(),
                "public_url": record_url,
                "public_slug": outcome.public_slug,
                "content_sha256": outcome.content_sha256,
                "version": outcome.version,
            }
            if isinstance(outcome, FetchedRecord)
            else {
                "present": False,
                "kind": outcome.kind,
                "detail": outcome.detail,
            }
        ),
        "pages": list(PAGES),
        "images": images,
        "film": film,
    }
    groups: dict[str, str] = {}
    for image in images:
        group = media_group(image["path"])
        if group in groups:
            raise BundleRefused(
                f"images {groups[group]} and {image['path']} share D7 media group {group}"
            )
        groups[group] = image["path"]
    files = sorted(str(p.relative_to(out_dir)) for p in out_dir.rglob("*") if p.is_file())
    hidden = [rel for rel in files if hidden_component(rel)]
    if hidden:
        # A dot-prefixed path is never a bundle file (it is how the studio
        # names staging, backups and journals): refuse rather than publish it.
        raise BundleRefused(f"the bundle holds hidden path(s) {hidden[:3]}; nothing is listed")
    manifest["files"] = {rel: sha256_file(out_dir / rel) for rel in files}
    (out_dir / MANIFEST).write_text(
        json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8"
    )
    return BundleResult(out_dir, manifest, notes)


def _film(
    product: LaunchProduct,
    record: BuildRecord,
    facts: list[Fact],
    out_dir: Path,
    walkthrough_video: Path | None,
    walkthrough_stills: list[Still],
    decor: DecorOutcome,
    replay: ReplaySegment | None,
    record_url: str | None,
) -> dict[str, Any]:
    # Frames are drawn OUTSIDE the bundle and removed whatever happens: no
    # working file can ever land in (and be listed and published from) it.
    with tempfile.TemporaryDirectory(prefix="studio-frames-") as tmp:
        frames = Path(tmp)
        w, h = 1280, 720
        title = render.title_slide(product.title, product.tagline, frames / "title.png", w, h)
        closing = render.title_slide(
            render.FOOTER,
            f"Every number in this film: {record_url or 'the build record'}",
            frames / "closing.png",
            w,
            h,
        )
        summary = [
            render.facts_slide(
                "The build record, in numbers", facts[:7], frames / "summary-1.png", w, h
            ),
            render.facts_slide(
                "The build record, in numbers (cont.)", facts[7:], frames / "summary-2.png", w, h
            ),
        ]
        broll = [
            render.fit_frame(a.path, frames / f"broll-{i}.png", w, h, label=a.label)
            for i, a in enumerate(decor.broll()[:3])
        ]
        plan = compose_film(
            FilmInputs(
                title_frame=title,
                closing_frame=closing,
                summary_frames=summary,
                walkthrough_video=walkthrough_video,
                walkthrough_stills=walkthrough_stills,
                broll_frames=broll,
                replay=replay,
            ),
            record,
            out_dir / "video",
            product.video_seconds,
        )
    return {
        "path": "video/launch.mp4",
        "target_seconds": plan.target_seconds,
        "actual_seconds": plan.actual_seconds,
        "segments": [s.__dict__ for s in plan.segments],
        "omitted": plan.omitted,
    }
