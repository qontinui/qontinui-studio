"""PR evidence for a compile run (plan 2026-10-13 Phase 19 step 5): what the
calling session pastes as the PR body (``gh pr create --body-file``).

Per compiled slot: the route and where it came from, the pick and why, the
binding (spec, recipe, imagery, exemplar and LoRA digests), every candidate
with its store outcome, every derivative (format, bytes, size, sha256, IPTC
marker) and the D5/D6/D7 gate record.

Two parts of the evidence the plan specifies come from phases that have not
landed. Each is a typed UNKNOWN naming the missing phase — never an empty
section, never a pass:

* **before/after per breakpoint x theme** needs the in-situ asset override
  (Phase 18, ``POST /control/media/override``) on Phase 9's capture matrix;
* **per-slot values** (resolution, palette, busyness, hashes, crop safety ...)
  need the vision-core ``image`` analyzer (Phase 15).

Their UNKNOWN code is the observation envelope's
``capability_unavailable_in_build``: the capability exists in no build this
run can call.
"""

from __future__ import annotations

from typing import Any

from qontinui_studio.compiler.run import CompileReport, SlotReport

UNKNOWN_CODE = "capability_unavailable_in_build"
BEFORE_AFTER_NEEDS = (
    "Phase 18 (the in-situ asset override, POST /control/media/override, which swaps the "
    "candidate into the page) on Phase 9's capture matrix; Phase 18 has not landed"
)
VALUES_NEED = (
    "Phase 15 (the vision-core `image` analyzer: effective resolution, sharpness, palette, "
    "busyness, pHash/dHash, placeholders, crop safety, saliency); it has not landed"
)
COMPILED_LIKE = ("compiled", "refused", "generating", "failed", "awaiting_alt_text")


def cells(rep: SlotReport) -> list[str]:
    """The before/after matrix's cells: breakpoint x theme (each at every DPR)."""
    dprs = ",".join(f"{d:g}" for d in rep.contexts.get("dpr", []))
    return [
        f"{bp}px {theme} @{dprs}x"
        for bp in rep.contexts.get("breakpoints", [])
        for theme in rep.contexts.get("themes", [])
    ]


def slot_evidence(rep: SlotReport) -> dict[str, Any]:
    """The typed-UNKNOWN half of a slot's evidence, as data (JSON report)."""
    return {
        "before_after": {
            "status": "unknown",
            "code": UNKNOWN_CODE,
            "needs": BEFORE_AFTER_NEEDS,
            "cells": cells(rep),
        },
        "values": {"status": "unknown", "code": UNKNOWN_CODE, "needs": VALUES_NEED},
    }


def _short(sha: object) -> str:
    return f"`{str(sha)[:12]}…`" if isinstance(sha, str) and sha else "—"


def _store(row: dict[str, Any] | None) -> str:
    if not row:
        return "—"
    return f"{row.get('status')}: {row.get('detail')}"


def _slot_section(rep: SlotReport) -> list[str]:
    out = [f"### `{rep.slot_id}` — {rep.status}", ""]
    out.append(f"- Route: **{rep.route}** ({rep.route_source})")
    out.append(f"- Spec: `{rep.spec_sha256}`")
    if rep.batch_id:
        out.append(f"- Batch: `{rep.batch_id}`")
    for reason in rep.reasons:
        out.append(f"- {reason}")
    if rep.pick:
        p = rep.pick
        out.append(
            f"- Pick: **{p.get('pick')}** — candidate {p.get('index')} of {p.get('of')}, asset "
            f"`{p.get('asset_sha256')}` ({p.get('provider')} / {p.get('model')}); store: "
            f"{_store(p.get('store'))}"
        )
    if rep.binding:
        b = rep.binding
        out.append(
            "- Binding: "
            + ", ".join(
                f"{k} {_short(b.get(k))}"
                for k in (
                    "asset_sha256",
                    "recipe_sha256",
                    "imagery_sha256",
                    "exemplars_sha256",
                    "lora_sha256",
                )
                if b.get(k)
            )
            + (f", alt text “{b['alt_text']}”" if b.get("alt_text") else "")
        )
    if rep.inputs:
        out.append(
            "- Inputs: "
            + "; ".join(
                f"{name} {_short(v.get('sha256'))} ({v.get('source', 'file')})"
                for name, v in rep.inputs.items()
            )
        )
    if rep.candidates:
        out += ["", "| # | asset | provider / model | usable | store |", "|---|---|---|---|---|"]
        for c in rep.candidates:
            usable = "yes" if c.get("usable") else f"no ({c.get('why_not')})"
            out.append(
                f"| {c.get('index')} | {_short(c.get('asset_sha256'))} | {c.get('provider')} / "
                f"{c.get('model')} | {usable} | {_store(c.get('store'))} |"
            )
    if rep.derivatives:
        out += [
            "",
            "| format | file | bytes | size | sha256 | IPTC marker | store |",
            "|---|---|---|---|---|---|---|",
        ]
        for d in rep.derivatives:
            out.append(
                f"| {d.get('format')} | `{str(d.get('file')).rsplit('/', 1)[-1]}` | "
                f"{d.get('bytes')} | {d.get('width')}×{d.get('height')} | "
                f"{_short(d.get('sha256'))} | {'yes' if d.get('marked') else '**NO**'} | "
                f"{_store(d.get('store'))} |"
            )
    if rep.gates:
        out += ["", "| gate | outcome | subject | detail |", "|---|---|---|---|"]
        for g in rep.gates:
            out.append(f"| {g['gate']} | {g['outcome']} | {g['subject']} | {g['detail'][:160]} |")
    ev = slot_evidence(rep)
    out += [
        "",
        f"- Before/after per breakpoint × theme: **UNKNOWN** (`{UNKNOWN_CODE}`): needs "
        f"{BEFORE_AFTER_NEEDS}. Cells: " + ", ".join(ev["before_after"]["cells"]) + ".",
        f"- Values: **UNKNOWN** (`{UNKNOWN_CODE}`): needs {VALUES_NEED}.",
        "",
    ]
    return out


def render_markdown(report: CompileReport) -> str:
    lines = [
        "## Image slots — `studio compile`",
        "",
        f"Target `{report.target}` (`{report.repo}`); derivatives and `studio-manifest.json` "
        f"written under `{report.publish_dir}` in this checkout"
        + (" (dry run: nothing written)" if report.dry_run else "")
        + ".",
        f"Mode: {'overnight re-render (`--changed`, bulk)' if report.changed_only else 'interactive'}.",
    ]
    if report.runs > 1 and report.head:
        lines.append(
            f"Covers {report.runs} runs on checkout HEAD `{report.head[:12]}`: a slot an earlier "
            "run compiled keeps that run's evidence."
        )
    if report.imagegen:
        lines.append(f"Image generation: {report.imagegen}.")
    if report.refusal:
        lines.append(
            f"**Generation refused:** `{report.refusal['code']}` — {report.refusal['detail']}"
        )
    lines += ["", "| slot | route | status | detail |", "|---|---|---|---|"]
    for s in report.slots:
        detail = "; ".join(s.reasons)[:200].replace("|", "\\|")
        lines.append(f"| `{s.slot_id}` | {s.route or '—'} | {s.status} | {detail} |")
    lines.append("")
    for s in report.slots:
        if s.status in COMPILED_LIKE:
            lines += _slot_section(s)
        elif s.lucide:
            lines += [
                f"### `{s.slot_id}` — {s.status}",
                "",
                f"- lucide icon `{s.lucide['icon']}` → `<{s.lucide['component']} />` "
                f"({s.lucide['detail']}); no file is written.",
                "",
            ]
        elif s.authoring:
            lines += [
                f"### `{s.slot_id}` — {s.status}",
                "",
                f"- Agent-written SVG owed at `{s.authoring.get('write_to')}`: "
                f'root `data-slot-spec-sha256="{s.spec_sha256}"`, colours '
                f"{s.authoring.get('colours')}.",
                "",
            ]
    if report.orphans:
        lines += [
            "Manifest entries no spec declares any more (their files stay until a session "
            "removes them with the spec): " + ", ".join(f"`{o}`" for o in report.orphans) + ".",
            "",
        ]
    lines += [
        "Studio ran no git or gh: the session that ran it commits this checkout and opens the "
        "PR, which goes through the merge train like any other. Reverting the PR reverses "
        "every slot it wrote.",
        "",
    ]
    return "\n".join(lines)
