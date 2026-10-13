"""Regenerate ``build_record_corpus.json`` from qontinui-web's validator.

    python -I tests/fixtures/make_build_record_corpus.py <qontinui-web>/backend

Every case is a ``build-record/1`` document (a valid coord-shaped base, or a
mutation of it) paired with the violations qontinui-web's
``build_record_violations`` reports for it. The launch kit's port must report
exactly the same list (``tests/test_launch_allowlist_parity.py``).
"""

from __future__ import annotations

import copy
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

BASE: dict[str, Any] = {
    "schema": "build-record/1",
    "generated_at": "2026-10-09T18:00:00Z",
    "product": {
        "slug": "qontinui-studio",
        "title": "qontinui-studio",
        "repos": ["qontinui/qontinui-studio"],
        "window_start": "2026-10-09T00:00:00Z",
        "window_end": None,
    },
    "work_units": [
        {
            "slug": "2026-10-09-factory-built-product-portfolio-and-launch-kit",
            "title": "Factory-built product portfolio and launch kit",
            "status": "shipped",
            "vetted_at": "2026-10-09T09:00:00Z",
            "shipped_at": "2026-10-09T17:00:00Z",
        }
    ],
    "timeline": [
        {
            "at": "2026-10-09T08:00:00Z",
            "work_unit": "2026-10-09-factory-built-product-portfolio-and-launch-kit",
            "from_status": None,
            "to_status": "draft",
        }
    ],
    "prs": [
        {
            "repo": "qontinui/qontinui-studio",
            "number": 1,
            "title": "feat: asset pipeline",
            "opened_at": "2026-10-09T10:00:00Z",
            "landed_at": "2026-10-09T12:00:00Z",
            "ci_duration_secs": 420,
            "attempts": 2,
        }
    ],
    "gates": {"registered": 3, "cleared": 2},
    "sessions": {"count": None, "unknown_reason": "sessions.count: census_provisional"},
    "wall_clock_secs": 32400,
    "self_corrections": {"red_ci_fixed": 1, "review_findings_fixed": None, "gate_reopens": None},
    "unknowns": [
        "self_corrections.review_findings_fixed: not_established",
        "self_corrections.gate_reopens: door_unavailable",
        "sessions.count: census_provisional",
    ],
}


def _set(doc: dict[str, Any], path: tuple[Any, ...], value: Any) -> None:
    target: Any = doc
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value


MUTATIONS: list[tuple[str, tuple[Any, ...], Any]] = [
    ("extra-root-key", ("transcript",), "leak"),
    ("extra-nested-key", ("product", "owner_email"), "x@y.z"),
    ("extra-pr-key", ("prs", 0, "device_id"), "x"),
    ("bool-count", ("gates", "cleared"), True),
    ("negative-count", ("gates", "cleared"), -1),
    ("negative-ci", ("prs", 0, "ci_duration_secs"), -5),
    ("float-count", ("wall_clock_secs",), 1.5),
    ("string-number", ("prs", 0, "number"), "12"),
    ("bad-status", ("work_units", 0, "status"), "done"),
    ("null-to-status", ("timeline", 0, "to_status"), None),
    ("bad-timestamp-shape", ("generated_at",), "2026-10-09 12:00"),
    ("bad-timestamp-value", ("generated_at",), "2026-13-40T12:00:00Z"),
    ("bad-landed", ("prs", 0, "landed_at"), "2026-02-30T25:61:00Z"),
    ("bad-repo", ("product", "repos", 0), "not-a-repo"),
    ("bad-slug", ("product", "slug"), "Bad_Slug"),
    ("free-text-unknown", ("unknowns", 0), "sessions.count: because reasons"),
    ("unknown-bad-path", ("unknowns", 0), "not.a.path: not_established"),
    ("free-text-session-reason", ("sessions", "unknown_reason"), "host spaceship never ran"),
    ("private-repo-in-title", ("work_units", 0, "title"), "port acme/secret-billing"),
    ("email-in-title", ("work_units", 0, "title"), "owner jane.doe@example.com shipped it"),
    ("user-at-host", ("work_units", 0, "title"), "owner ops@spaceship"),
    ("uuid-in-title", ("prs", 0, "title"), "device 0b7c2a1e-1111-4222-8333-444455556666"),
    ("hex32-in-title", ("prs", 0, "title"), "id " + "0123456789abcdef" * 2),
    ("hex40-ok", ("prs", 0, "title"), "revert " + "0123456789" * 4),
    ("division-slash", ("prs", 0, "title"), "port from acme ∕ secret-repo"),
    ("fraction-slash", ("prs", 0, "title"), "port from acme⁄secret-repo"),
    ("fullwidth", ("prs", 0, "title"), "port from ａcme／secret-repo"),
    ("spaced-slash", ("prs", 0, "title"), "port from acme / secret-repo"),
    ("zwsp", ("prs", 0, "title"), "port from acme​/secret"),
    ("soft-hyphen", ("prs", 0, "title"), "port from acme­/secret"),
    ("word-joiner", ("prs", 0, "title"), "port from acme/⁠secret"),
    ("overlay-solidus", ("prs", 0, "title"), "port from acme̸secret"),
    ("cgj", ("prs", 0, "title"), "port fix from acme/͏secret-billing"),
    ("vs16", ("prs", 0, "title"), "port fix from acme/️secret-billing"),
    ("hangul-filler", ("prs", 0, "title"), "port fix from acme/ㅤsecret-billing"),
    ("rising-diagonal", ("prs", 0, "title"), "port fix from acme⟋secret-billing"),
    ("cyrillic-confusable", ("prs", 0, "title"), "port from аcme/secret"),
    ("ci-cd-false-positive", ("prs", 0, "title"), "fix CI/CD"),
    ("dates-ok", ("prs", 0, "title"), "release 2026/10"),
    ("private-pr-repo", ("prs", 0, "repo"), "acme/private"),
    ("plan-stem-ok", ("work_units", 0, "slug"), "PLAN_2026_06_17_auto_tenant_creation"),
    ("hex32-slug", ("work_units", 0, "slug"), "deploy-" + "ab" * 16),
    ("null-title-ok", ("prs", 0, "title"), None),
    ("schema-v2", ("schema",), "build-record/2"),
    # Size caps and linear-time regexes (qontinui-web's thirteenth review).
    ("title-512-ok", ("prs", 0, "title"), "feat: " + "x" * 506),
    ("title-513", ("prs", 0, "title"), "feat: " + "x" * 507),
    ("title-600", ("prs", 0, "title"), "feat: " + "x" * 600),
    ("spaces-20k", ("prs", 0, "title"), " " * 20000 + "x"),
    ("spaces-100k", ("prs", 0, "title"), " " * 100000 + "x"),
    ("local-part-40k", ("prs", 0, "title"), "a" * 40000),
    ("spaced-slashes-500", ("prs", 0, "title"), " /" * 250),
    ("at-run-500", ("prs", 0, "title"), "a@" * 250),
    # NFKD expansion (qontinui-web fourteenth review): measured decomposed too.
    ("nfkd-one-fdfa", ("prs", 0, "title"), "x" * 20 + "\ufdfa"),
    ("nfkd-two-fdfa", ("prs", 0, "title"), "\ufdfa\ufdfa"),
    ("nfkd-ligatures-300", ("prs", 0, "title"), "\ufb01" * 300),
    ("nfkd-ligatures-200", ("prs", 0, "title"), "\ufb01" * 200),
    ("nfkd-accents-ok", ("prs", 0, "title"), "caf\u00e9 r\u00e9sum\u00e9"),
]

#: (name, excluded repo names, title) — a repo the product defines but coord
#: left out of the document (not known public) must not be named anywhere.
EXCLUDED: list[tuple[str, list[str], str]] = [
    ("excluded-named", ["secret-billing"], "port the secret-billing engine"),
    ("excluded-fullwidth", ["secret-billing"], "port \uff53ecret-billing"),
    ("excluded-substring-ok", ["billing"], "port the billingual docs"),
    ("excluded-many", [f"repo-{i}" for i in range(50)], "touches repo-49 too"),
]


def corpus() -> list[dict[str, Any]]:
    cases = [{"name": "base", "document": copy.deepcopy(BASE)}]
    for name, path, value in MUTATIONS:
        doc = copy.deepcopy(BASE)
        _set(doc, path, value)
        cases.append({"name": name, "document": doc})
    doc = copy.deepcopy(BASE)
    del doc["schema"]
    cases.append({"name": "missing-schema", "document": doc})
    doc = copy.deepcopy(BASE)
    doc["unknowns"] = ["work_units: door_unavailable", "prs[0].title: not_established"]
    cases.append({"name": "indexed-unknown-ok", "document": doc})
    cases.append({"name": "not-an-object", "document": ["build-record/1"]})
    cases += [sized_case("document-over-256kib", 256 * 1024 + 1)]
    cases += [sized_case("document-at-256kib", 256 * 1024)]
    cases += [sized_case("document-under-256kib", 256 * 1024 - 1)]
    for name, excluded, title in EXCLUDED:
        doc = copy.deepcopy(BASE)
        doc["prs"][0]["title"] = title
        cases.append({"name": name, "document": doc, "excluded_names": excluded})
    return cases


def sized_case(name: str, size: int) -> dict[str, Any]:
    """A valid-shaped document whose serialized size (the web's measure:
    compact JSON, UTF-8) is exactly ``size`` bytes."""
    doc = copy.deepcopy(BASE)
    unit = doc["work_units"][0]
    doc["work_units"] = [dict(unit, slug=f"w-{i}", title="t" * 400) for i in range(500)]
    while _size(doc) > size:
        doc["work_units"].pop()
    pad = size - _size(doc)
    doc["work_units"][-1]["title"] = "t" * (400 + pad)
    if _size(doc) != size or len(doc["work_units"][-1]["title"]) > 512:
        # Spread the padding over the last few titles (each stays <= 512).
        doc["work_units"][-1]["title"] = "t" * 400
        i = len(doc["work_units"]) - 1
        while pad > 0:
            add = min(pad, 112)
            doc["work_units"][i]["title"] += "t" * add
            pad -= add
            i -= 1
    assert _size(doc) == size, (name, _size(doc), size)
    return {"name": name, "document": doc}


def _size(doc: Any) -> int:
    """The web validator's measure (``_too_large``): compact JSON, UTF-8."""
    return len(json.dumps(doc, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def main(web_backend: Path) -> None:
    sys.path.insert(0, str(web_backend))
    from app.services import build_record_allowlist as web  # type: ignore[import-not-found]

    source = Path(web.__file__)
    commit = subprocess.run(
        ["git", "-C", str(source.parent), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    # Documents are rebuilt by corpus() in the test (some are ~1 MiB); the file
    # keeps only each case's name and the web validator's verdict.
    verdicts = {
        c["name"]: web.build_record_violations(
            c["document"], excluded_names=c.get("excluded_names", ())
        )
        for c in corpus()
    }
    out = {
        "source": "qontinui-web backend/app/services/build_record_allowlist.py",
        "commit": commit,
        "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "violations": verdicts,
    }
    target = Path(__file__).with_name("build_record_corpus.json")
    target.write_text(json.dumps(out, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {len(verdicts)} cases to {target}")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
