"""Provenance sidecars: ``<file>.provenance.json`` beside every image file the
studio generates or derives (plan 2026-10-13 Phase 11 step 4; decision record
``image-generation-weights-are-permissively-licensed``, a5ec7670).

A sidecar records the provider, the model, the terms as reviewed (launch-kit
D5), the prompt actually sent, the references, the seed when known and
``created_at``. For the local provider it also embeds the facade's
``qontinui_provenance`` VERBATIM; :func:`facade_missing` names what that record
must carry — the SPDX licence, the recipe hash, the weight sha256s and the
seed — and :func:`missing` is the completeness check every sidecar passes
before it is written.

The facade record's shape — the Phase 10 contract as landed (qontinui
``docker/imagegen/qontinui_imagegen/provenance.py``, schema
``qontinui.imagegen.provenance/1``); the fields this module reads::

    {"recipe": {"id": "hero", "version": 3, "hash": "<64 hex>"},
     "models": [{"id": "z-image-turbo", "spdx": "Apache-2.0",
                 "weights": [{"path": "...", "sha256": "<64 hex>"}]}],
     "licence": {"spdx": "Apache-2.0"},   # an AND of the models' licences
     "seed": 1234, ...}

``recipe.hash`` is ``recipe_hash(recipe, manifest)``, the same value the
facade's ``GET /qontinui/status`` serves under ``recipes``.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from qontinui_studio.marker import DIGITAL_SOURCE_TYPE

SCHEMA = "qontinui-studio/provenance/v1"
SIDECAR_SUFFIX = ".provenance.json"
# The adapter kind whose results must carry the facade's own record.
LOCAL_ADAPTER = "openai_compatible_images"

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
# The SPDX ids decision record a5ec7670 admits (Apache-2.0, MIT, BSD family).
ADMITTED_SPDX = frozenset(
    {"Apache-2.0", "MIT", "MIT-0", "MIT-CMU", "BSD-2-Clause", "BSD-3-Clause", "0BSD"}
)
C2PA_ABSENT = {
    "signed": False,
    "reason": "no C2PA signing credential is configured; the IPTC marker is the mandatory part",
}


def sidecar_path(path: Path) -> Path:
    return path.with_name(path.name + SIDECAR_SUFFIX)


def _is_sha(value: object) -> bool:
    return isinstance(value, str) and bool(_SHA256.fullmatch(value))


def spdx_terms(expression: str) -> list[str]:
    """The licence ids of an SPDX expression the facade writes: an AND of
    ids, an id with a space parenthesised. Any other operator (OR, WITH) is
    returned as itself, so it is never read as admitted."""
    out: list[str] = []
    for part in expression.split(" AND "):
        term = part.strip()
        if term.startswith("(") and term.endswith(")"):
            term = term[1:-1].strip()
        out.append(term)
    return out


def facade_recipe_hash(record: object) -> str | None:
    """The recipe hash a facade result was made under (``recipe.hash``)."""
    recipe = record.get("recipe") if isinstance(record, Mapping) else None
    value = recipe.get("hash") if isinstance(recipe, Mapping) else None
    return value if _is_sha(value) else None


def facade_missing(record: object) -> list[str]:
    """What the facade's ``qontinui_provenance`` lacks of the set a5ec7670
    requires: SPDX licence (admitted), recipe hash, weight sha256s, seed."""
    if not isinstance(record, Mapping):
        return ["qontinui_provenance (absent or not an object)"]
    problems: list[str] = []
    licence = record.get("licence")
    spdx = licence.get("spdx") if isinstance(licence, Mapping) else None
    if not isinstance(spdx, str) or not spdx:
        problems.append("licence.spdx")
    else:
        refused = [term for term in spdx_terms(spdx) if term not in ADMITTED_SPDX]
        if refused:
            problems.append(f"licence.spdx '{spdx}' names a licence not admitted: {refused}")
    recipe = record.get("recipe")
    if not isinstance(recipe, Mapping) or not _is_sha(recipe.get("hash")):
        problems.append("recipe.hash")
    models = record.get("models")
    weights: list[object] = []
    for m in models if isinstance(models, list) else []:
        listed = m.get("weights") if isinstance(m, Mapping) else None
        if isinstance(listed, list):
            weights.extend(listed)
        else:
            weights.append(None)  # a model with no weight list is a gap
    if not weights or not all(isinstance(w, Mapping) and _is_sha(w.get("sha256")) for w in weights):
        problems.append("models[].weights[].sha256")
    seed = record.get("seed")
    if not isinstance(seed, int) or isinstance(seed, bool):
        problems.append("seed")
    return problems


_REQUIRED = ("provider", "model", "terms", "prompt", "references", "created_at", "file")


def missing(record: Mapping[str, Any]) -> list[str]:
    """Every required field a sidecar lacks (empty == complete)."""
    problems = [k for k in _REQUIRED if record.get(k) in (None, "")]
    if record.get("ai_generated") is not True:
        problems.append("ai_generated")
    if record.get("digital_source_type") != DIGITAL_SOURCE_TYPE:
        problems.append("digital_source_type")
    provider = record.get("provider")
    if not isinstance(provider, Mapping) or not provider.get("id") or not provider.get("adapter"):
        problems.append("provider.id/adapter")
    elif provider.get("adapter") == LOCAL_ADAPTER:
        problems += [
            f"qontinui_provenance.{p}" for p in facade_missing(record.get("qontinui_provenance"))
        ]
    terms = record.get("terms")
    if not isinstance(terms, Mapping) or not terms.get("commercial_use"):
        problems.append("terms.commercial_use")
    file = record.get("file")
    if not isinstance(file, Mapping) or not _is_sha(file.get("sha256")):
        problems.append("file.sha256")
    return problems


def build(
    *,
    file_sha256: str,
    file_name: str,
    role: str,
    provider_id: str,
    provider_name: str,
    adapter: str,
    model: str,
    terms: Mapping[str, Any],
    prompt: str,
    negative_prompt: str | None,
    references: Sequence[Mapping[str, str]],
    seed: int | None,
    recipe: str | None,
    created_at: str,
    job_id: str,
    batch_id: str,
    brief_id: str,
    facade: Mapping[str, Any] | None,
    derived_from: str | None = None,
    transform: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """One sidecar record. ``seed`` falls back to the facade's own seed: the
    seed is recorded whenever it is KNOWN, and only then."""
    if seed is None and facade is not None and isinstance(facade.get("seed"), int):
        seed = int(facade["seed"])
    record: dict[str, Any] = {
        "schema": SCHEMA,
        "ai_generated": True,
        "digital_source_type": DIGITAL_SOURCE_TYPE,
        "file": {"name": file_name, "sha256": file_sha256, "role": role},
        "provider": {"id": provider_id, "name": provider_name, "adapter": adapter},
        "model": model,
        "terms": dict(terms),
        "prompt": prompt,
        "negative_prompt": negative_prompt,
        "references": [dict(r) for r in references],
        "seed": seed,
        "recipe": recipe,
        "created_at": created_at,
        "job_id": job_id,
        "batch_id": batch_id,
        "brief_id": brief_id,
        "c2pa": dict(C2PA_ABSENT),
    }
    if derived_from is not None:
        record["file"]["derived_from"] = derived_from
    if transform is not None:
        record["file"]["transform"] = dict(transform)
    if facade is not None:
        record["qontinui_provenance"] = facade
    return record


class IncompleteProvenance(ValueError):
    """A sidecar would be written without a field a5ec7670 requires."""


def write(path: Path, record: Mapping[str, Any]) -> Path:
    """Write ``record`` as ``path``'s sidecar, atomically. Refuses an
    incomplete record: an asset is never left looking provenanced when it is not."""
    problems = missing(record)
    if problems:
        raise IncompleteProvenance(f"provenance for {path.name} is incomplete: {problems}")
    dest = sidecar_path(path)
    tmp = dest.with_name(f".{dest.name}.tmp-{uuid.uuid4().hex}")
    tmp.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, dest)
    return dest


def read(path: Path) -> dict[str, Any] | None:
    """``path``'s sidecar, or None when it is absent or unreadable."""
    try:
        data = json.loads(sidecar_path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None
