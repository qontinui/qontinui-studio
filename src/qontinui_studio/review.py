"""A static review page: what was generated, under which terms, where it is
published, and the one command that regenerates or unpublishes it.

It is NOT an approval queue (D7): publication already happened, or was
refused, by the automated gates. The page is for after-the-fact correction.
"""

from __future__ import annotations

import html
import os
import sqlite3
from datetime import datetime
from pathlib import Path

from qontinui_studio.library import Library
from qontinui_studio.publish import unpublish_command

CSS = """
:root { --bg:#fafafa; --fg:#1d1d1f; --muted:#6b6b70; --card:#fff; --line:#e3e3e6; --ok:#1a7f37; --bad:#c62828; }
@media (prefers-color-scheme: dark) { :root { --bg:#141416; --fg:#ececef; --muted:#9a9aa2; --card:#1d1d21; --line:#2c2c31; --ok:#4cc26a; --bad:#ff6b6b; } }
body { margin:0; padding:16px; background:var(--bg); color:var(--fg); font:14px/1.45 system-ui,sans-serif; }
h1 { font-size:20px; } h2 { font-size:16px; margin:0 0 6px; }
.batch { background:var(--card); border:1px solid var(--line); border-radius:8px; padding:12px; margin:0 0 16px; }
.assets { display:grid; grid-template-columns:repeat(auto-fill,minmax(260px,1fr)); gap:12px; }
.asset img { width:100%; height:auto; border-radius:4px; border:1px solid var(--line); }
dl { display:grid; grid-template-columns:max-content 1fr; gap:2px 8px; margin:6px 0; font-size:12px; }
dt { color:var(--muted); } dd { margin:0; overflow-wrap:anywhere; }
code { display:block; background:var(--bg); border:1px solid var(--line); padding:4px 6px; border-radius:4px; font-size:12px; overflow-x:auto; }
.pass { color:var(--ok); } .fail { color:var(--bad); } .not_configured { color:var(--muted); }
"""


def _e(value: object) -> str:
    return html.escape("" if value is None else str(value))


def build_review_page(
    conn: sqlite3.Connection, out_dir: Path, generated_at: datetime, root: Path | None = None
) -> Path:
    """Write the review page. ``root`` (the studio root) makes every printed
    command runnable from any directory."""
    prefix = f"studio --root {root.resolve()} " if root is not None else "studio "
    library = Library(conn)
    out_dir.mkdir(parents=True, exist_ok=True)
    parts = [
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>",
        "<meta name='viewport' content='width=device-width,initial-scale=1'>",
        f"<title>Studio review</title><style>{CSS}</style></head><body>",
        "<h1>qontinui-studio — review</h1>",
        f"<p>Generated {_e(generated_at.isoformat())}. Publication is decided by the automated "
        "D5/D6/D7 gates; this page is for regenerating or unpublishing after the fact.</p>",
    ]
    batches = conn.execute("SELECT * FROM batches ORDER BY created_at DESC").fetchall()
    if not batches:
        parts.append("<p>No batches yet.</p>")
    for b in batches:
        parts.append(f"<section class='batch'><h2>{_e(b['id'])} — {_e(b['status'])}</h2>")
        parts.append(f"<dl><dt>brief</dt><dd>{_e(b['brief_path'])}</dd></dl>")
        parts.append("<p>Regenerate:</p>")
        parts.append(f"<code>{_e(prefix)}brief run {_e(b['brief_path'])}</code>")
        if b["status"] == "published":
            parts.append("<p>Unpublish:</p>")
            parts.append(f"<code>{_e(unpublish_command(b['id'], root))}</code>")
        gates = conn.execute(
            "SELECT gate, subject, outcome, detail FROM gate_results WHERE batch_id = ? ORDER BY rowid",
            (b["id"],),
        ).fetchall()
        if gates:
            parts.append("<details><summary>Gate results</summary><ul>")
            for g in gates:
                parts.append(
                    f"<li class='{_e(g['outcome'])}'>{_e(g['outcome'])} — {_e(g['gate'])} — "
                    f"{_e(g['subject'])}: {_e(g['detail'])}</li>"
                )
            parts.append("</ul></details>")
        parts.append("<div class='assets'>")
        for asset in library.assets(b["id"]):
            variants = library.variants(asset.uid)
            preview = next((v for v in variants if v.format in ("webp", "png", "jpeg")), None)
            parts.append("<div class='asset'>")
            if preview is not None:
                rel = os.path.relpath(preview.path, out_dir)
                parts.append(
                    f"<img src='{_e(rel)}' alt='{_e(asset.asset_id)} preview' loading='lazy'>"
                )
            published = conn.execute(
                "SELECT p.site, p.slot, p.published_path FROM publications p JOIN variants v ON v.id = p.variant_id "
                "WHERE v.asset_uid = ? AND p.unpublished_at IS NULL",
                (asset.uid,),
            ).fetchall()
            parts.append("<dl>")
            for k, v in (
                ("asset", asset.uid),
                ("provider", f"{asset.provider_name} ({asset.provider})"),
                ("model", asset.model),
                ("created", asset.created_at),
                ("ai_generated", "true" if asset.ai_generated else "false"),
                ("commercial use", asset.licence_commercial_use),
                ("terms reviewed", asset.licence_tos_reviewed or "UNREVIEWED"),
                ("terms source", asset.licence_source),
                ("watermark", asset.watermark),
                ("prompt", asset.prompt),
                (
                    "variants",
                    ", ".join(
                        f"{v.site or v.campaign}/{v.slot or ''} {v.format} {v.width}x{v.height}"
                        for v in variants
                    ),
                ),
                ("published", ", ".join(f"{p['site']}/{p['slot']}" for p in published) or "—"),
            ):
                parts.append(f"<dt>{_e(k)}</dt><dd>{_e(v)}</dd>")
            parts.append("</dl></div>")
        parts.append("</div></section>")
    parts.append("</body></html>")
    page = out_dir / "index.html"
    page.write_text("".join(parts), encoding="utf-8")
    return page
