"""``studio compile`` — the asset compile step (plan 2026-10-13 Phase 19).

One run over one checkout of one compile target:

1. **What needs compiling.** Every spec under ``--slots`` is loaded and routed.
   An asset-route slot (``diffusion``/``traced``) whose checkout manifest holds
   no binding is ``missing``; one whose binding was made for another spec hash,
   recipe, imagery treatment, exemplar set or LoRA is ``stale``; one whose
   inputs cannot be read is UNKNOWN — never treated as fresh, never
   re-rendered on a guess. ``--changed`` (the overnight bulk run) compiles only
   the stale ones.
2. **Select before generating** (:mod:`~qontinui_studio.compiler.target`):
   lucide and agent-written SVG slots generate nothing.
3. **Generate and choose.** A slot's N candidates are one studio batch,
   generated through the provider interface under the image-generation
   dial (read before any batch is created and again before every provider
   call). Every candidate is uploaded to qontinui-web's content-addressed
   store. The pick is the launch kit's lowest index, recorded as ``pick:
   unjudged (Phase 20 not landed)``.
4. **Write through the existing publish path.** The target becomes a
   ``kind: product`` Site whose ``publish_dir`` is inside the checkout; the
   batch is published by :func:`~qontinui_studio.publish.publish_batch` —
   D5/D6/D7, the journalled swap, ``studio-manifest.json`` — with the slot's
   spec hash and its binding (an ``ImageSlotProvenance``) added to the
   manifest entry. Studio runs no git or gh: the calling session commits the
   checkout and opens the PR.
5. **Evidence** for that PR (:mod:`~qontinui_studio.compiler.evidence`).

A batch that cannot finish in one run (a host answering ``busy``/``deferred``,
an ``alt: required`` slot waiting for its alt text) is resumed by the next run
while its spec and inputs are unchanged, and failed once they are not.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, Literal

import yaml
from qontinui_schemas.generated.per_type.image_slot_spec import (
    ImageSlotAssetSource,
    ImageSlotProvenance,
)

from qontinui_studio import marker, provenance
from qontinui_studio.briefs import load_brief, validate_brief
from qontinui_studio.compiler import selected
from qontinui_studio.compiler.image_store import CandidateStatus, ImageStore, StoreOutcome
from qontinui_studio.compiler.inputs import Digest, ImagegenManifest, exemplar_digest
from qontinui_studio.compiler.jcs import canonical_sha256
from qontinui_studio.compiler.recipe import FACADE_ADAPTER, compile_recipe_sha256, prompt_for
from qontinui_studio.compiler.specs import LoadedSpec, binding_problems, brief_slug, load_specs
from qontinui_studio.compiler.target import (
    CompileTarget,
    Route,
    route_for,
)
from qontinui_studio.errors import ConfigError, ImageGenerationRefused, StudioError
from qontinui_studio.library import AssetRecord, Library, VariantRecord
from qontinui_studio.notify import Delivery, FileNotifier, Notice
from qontinui_studio.pipeline import Context, drive_batch, new_batch, policy_adapters
from qontinui_studio.postprocess import sha256_file
from qontinui_studio.publish import MANIFEST, lowest_index_pick, publish_batch, update_entries
from qontinui_studio.sites import Site, Slot, check_cross_site

PICK_UNJUDGED = "unjudged (Phase 20 not landed)"
# The site slot of a spec no image file is published for: a manifest key only.
MANIFEST_ONLY_SLOT = Slot(width=1, height=1, category="decorative", formats=["png"], rendered=False)
RESUMABLE = ("generating", "generated", "refused")

SlotStatus = Literal[
    "not_compiled",
    "selected",
    "icon_unknown",
    "unverified",
    "awaiting_authoring",
    "authored",
    "authored_stale",
    "authored_invalid",
    "fresh",
    "unknown",
    "bound_elsewhere",
    "missing",
    "planned",
    "compiled",
    "generating",
    "failed",
    "refused",
    "awaiting_alt_text",
    "generation_refused",
]
# What the checkout holds for an asset-route slot.
BindingState = Literal["missing", "fresh", "stale", "unknown", "bound_elsewhere"]
# Statuses that leave work for the calling session (exit 7).
SESSION_WORK = frozenset(
    {
        "awaiting_authoring",
        "authored_stale",
        "authored_invalid",
        "awaiting_alt_text",
        "icon_unknown",
    }
)


@dataclass
class SlotReport:
    slot_id: str
    site_slot: str
    role: str
    route: Route | None
    route_source: str
    spec_sha256: str
    status: SlotStatus = "fresh"
    reasons: list[str] = field(default_factory=list)
    # The binding the checkout manifest holds after this run (a dict), if any.
    binding: dict[str, Any] | None = None
    pick: dict[str, Any] | None = None
    batch_id: str | None = None
    candidates: list[dict[str, Any]] = field(default_factory=list)
    derivatives: list[dict[str, Any]] = field(default_factory=list)
    gates: list[dict[str, str]] = field(default_factory=list)
    inputs: dict[str, dict[str, Any]] = field(default_factory=dict)
    lucide: dict[str, str] | None = None
    authoring: dict[str, Any] | None = None
    # True when this run wrote the slot's manifest-only entry (a lucide pick).
    manifest_written: bool = False
    # The spec's render contexts (breakpoints x themes x dpr): the cells the
    # PR evidence's before/after covers.
    contexts: dict[str, list[Any]] = field(default_factory=dict)

    def as_json(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass
class CompileReport:
    target: str
    repo: str
    checkout: str
    publish_dir: str
    changed_only: bool
    dry_run: bool
    slots: list[SlotReport] = field(default_factory=list)
    imagegen: str | None = None
    refusal: dict[str, str] | None = None
    notices: list[str] = field(default_factory=list)
    # Manifest entries (site slot names) no spec declares any more: their files
    # stay until a session removes them with the spec's removal.
    orphans: list[str] = field(default_factory=list)
    # What this run removed for them: the entries and the files.
    retired: list[str] = field(default_factory=list)
    kept: list[str] = field(default_factory=list)

    @property
    def files_written(self) -> bool:
        return any(s.status == "compiled" or s.manifest_written for s in self.slots) or bool(
            self.retired
        )

    def exit_code(self) -> int:
        statuses = {s.status for s in self.slots}
        if self.refusal is not None or "generation_refused" in statuses:
            return 6
        if "refused" in statuses:
            return 2
        if statuses & {"generating", "failed"}:
            return 3
        if statuses & SESSION_WORK:
            return 7
        if statuses & {"unknown", "unverified"}:
            return 8
        return 0

    def as_json(self) -> dict[str, Any]:
        return {
            "target": self.target,
            "repo": self.repo,
            "checkout": self.checkout,
            "publish_dir": self.publish_dir,
            "changed_only": self.changed_only,
            "dry_run": self.dry_run,
            "files_written": self.files_written,
            "exit_code": self.exit_code(),
            "imagegen": self.imagegen,
            "refusal": self.refusal,
            "notices": self.notices,
            "orphans": self.orphans,
            "retired": self.retired,
            "kept": self.kept,
            "slots": [s.as_json() for s in self.slots],
        }


@dataclass(frozen=True)
class AltText:
    """An alt text written for one asset (``--alt-texts``): applied only to the
    pick it was written for."""

    asset_sha256: str
    text: str


@dataclass
class CompileOptions:
    slots_dir: Path
    target: CompileTarget
    changed_only: bool = False
    dry_run: bool = False
    only: frozenset[str] | None = None
    alt_texts: dict[str, AltText] = field(default_factory=dict)


@dataclass
class Readers:
    """Where the compile inputs come from (replaceable in tests)."""

    imagery: Callable[[str], Digest]
    imagegen: ImagegenManifest
    store: ImageStore


# ---------------------------------------------------------------------------
# The checkout
# ---------------------------------------------------------------------------


def checkout_of(slots_dir: Path, target: CompileTarget) -> Path:
    """The checkout root ``slots_dir`` belongs to: ``slots_dir`` must END with
    the target's ``slots_dir``, and the root must be a LINKED worktree (its
    ``.git`` a file naming ``<common dir>/worktrees/<name>``) — studio writes only
    into a worktree a session allocated, never into a shared primary checkout or
    an arbitrary directory."""
    resolved = slots_dir.resolve()
    tail = PurePosixPath(target.slots_dir.as_posix()).parts
    if tuple(resolved.parts[-len(tail) :]) != tail:
        raise ConfigError(
            f"{slots_dir} does not end with target '{target.id}' slots_dir "
            f"'{target.slots_dir}'; pass <checkout>/{target.slots_dir}"
        )
    root = Path(*resolved.parts[: -len(tail)])
    dot_git = root / ".git"
    if dot_git.is_dir():
        raise ConfigError(
            f"{root} is a primary checkout (its .git is a directory), shared with other "
            "sessions; studio compile writes only into a linked worktree the calling session "
            "allocated (allocate-worktree.sh)"
        )
    try:
        pointer = dot_git.read_text(encoding="utf-8").strip()
    except OSError:
        pointer = ""
    gitdir = pointer.removeprefix("gitdir:").strip() if pointer.startswith("gitdir:") else ""
    if not gitdir or "worktrees" not in Path(gitdir).parts[:-1]:
        raise ConfigError(
            f"{root} is not a linked git worktree; studio compile writes only into a worktree "
            "the calling session allocated (allocate-worktree.sh)"
        )
    return root


def _read_manifest(publish_dir: Path) -> dict[str, Any]:
    path = publish_dir / MANIFEST
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise ConfigError(f"{path} is unreadable: {e}") from e
    slots = data.get("slots") if isinstance(data, dict) else None
    return slots if isinstance(slots, dict) else {}


# ---------------------------------------------------------------------------
# Inputs and staleness
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SlotInputs:
    imagery: Digest
    exemplars: Digest | None
    lora: Digest | None
    look_sha256: str | None

    def unknown(self) -> list[str]:
        out = [] if self.imagery.known else [f"imagery: UNKNOWN ({self.imagery.source})"]
        for name, d in (("exemplars", self.exemplars), ("lora", self.lora)):
            if d is not None and not d.known:
                out.append(f"{name}: UNKNOWN ({d.source})")
        return out

    def as_json(self) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {
            "imagery": {"sha256": self.imagery.value, "source": self.imagery.source}
        }
        for name, d in (("exemplars", self.exemplars), ("lora", self.lora)):
            if d is not None:
                out[name] = {"sha256": d.value, "source": d.source}
        out["look"] = {"sha256": self.look_sha256}
        return out


def _file_sha(path: Path | None) -> str | None:
    return sha256_file(path) if path is not None and path.is_file() else None


def _inputs(
    spec: LoadedSpec, target: CompileTarget, library: Library, readers: Readers
) -> SlotInputs:
    style = spec.spec.style
    return SlotInputs(
        imagery=readers.imagery(style.imagery),
        exemplars=exemplar_digest(library, style.exemplars) if style.exemplars else None,
        lora=readers.imagegen.lora(style.lora) if style.lora else None,
        look_sha256=_file_sha(target.look_for(spec.slot_id)),
    )


def _current_recipe(
    ctx: Context,
    target: CompileTarget,
    spec: LoadedSpec,
    route: Route,
    slot: Slot,
    provider_id: str,
    look_sha256: str | None,
    readers: Readers,
) -> Digest:
    """The compile recipe hash a binding made NOW by ``provider_id`` would
    record, or UNKNOWN."""
    try:
        provider = ctx.inventory.get(provider_id)
    except ConfigError:
        return Digest(None, f"provider '{provider_id}' is no longer in the inventory")
    recipe_id = target.recipe_for(spec, route)
    engine: str | None = None
    if provider.adapter == FACADE_ADAPTER and recipe_id is not None:
        d = readers.imagegen.recipe(recipe_id)
        if not d.known:
            return Digest(None, f"the facade recipe's current hash: {d.source}")
        engine = d.value
    value = compile_recipe_sha256(
        route=route,
        provider_id=provider_id,
        adapter=provider.adapter,
        model=provider.model or "",
        recipe_id=recipe_id,
        engine_recipe_sha256=engine,
        look_sha256=look_sha256,
        slot=slot,
    )
    return Digest(value, f"compile recipe for provider '{provider_id}'")


def _staleness(
    ctx: Context,
    target: CompileTarget,
    spec: LoadedSpec,
    route: Route,
    slot: Slot,
    entry: dict[str, Any] | None,
    inputs: SlotInputs,
    publish_dir: Path,
    readers: Readers,
) -> tuple[BindingState, list[str], dict[str, Any] | None]:
    """(missing|fresh|stale|unknown|bound_elsewhere, reasons, binding) for an
    asset-route slot, from the checkout manifest entry (or the spec's inline
    binding when the manifest has none)."""
    if entry is None:
        if spec.spec.provenance is not None:
            b = spec.spec.provenance
            note = "current" if b.spec_sha256 == spec.sha256 else "stale (the spec changed since)"
            return (
                "bound_elsewhere",
                [
                    f"the spec carries an inline binding ({b.source.value}, {note}); studio "
                    "never replaces a binding it did not make"
                ],
                b.model_dump(mode="json", exclude_none=True),
            )
        return "missing", ["no chosen asset"], None
    raw = entry.get("binding")
    try:
        binding = ImageSlotProvenance.model_validate(raw)
    except ValueError:
        return (
            "unknown",
            [
                "the manifest entry carries no readable binding: what it binds is UNKNOWN, so it "
                "is neither re-rendered nor called fresh"
            ],
            None,
        )
    raw_files, raw_shas = entry.get("files"), entry.get("sha256")
    files: dict[str, Any] = raw_files if isinstance(raw_files, dict) else {}
    shas: dict[str, Any] = raw_shas if isinstance(raw_shas, dict) else {}
    if not files:
        return "missing", ["the manifest entry lists no file"], None
    for fmt, name in files.items():
        if _file_sha(publish_dir / str(name)) != shas.get(fmt):
            return "missing", [f"{name} is absent or not the bytes the manifest records"], None
    reasons: list[str] = []
    unknown: list[str] = []
    if binding.spec_sha256 != spec.sha256:
        reasons.append("spec changed")
    if inputs.imagery.known:
        if binding.imagery_sha256 != inputs.imagery.value:
            reasons.append(f"imagery changed ({inputs.imagery.source})")
    else:
        unknown.append(f"imagery: UNKNOWN ({inputs.imagery.source})")
    for name, current, recorded in (
        ("exemplar set", inputs.exemplars, binding.exemplars_sha256),
        ("LoRA", inputs.lora, binding.lora_sha256),
    ):
        if current is None:
            continue
        if not current.known:
            unknown.append(f"{name}: UNKNOWN ({current.source})")
        elif recorded != current.value:
            reasons.append(f"{name} changed ({current.source})")
    provider_id = entry.get("provider")
    recipe = (
        _current_recipe(
            ctx, target, spec, route, slot, str(provider_id), inputs.look_sha256, readers
        )
        if isinstance(provider_id, str)
        else Digest(None, "the manifest entry names no provider")
    )
    if recipe.known:
        if binding.recipe_sha256 != recipe.value:
            reasons.append("recipe changed")
    elif "no longer in the inventory" in recipe.source:
        reasons.append(recipe.source)
    else:
        unknown.append(f"recipe: UNKNOWN ({recipe.source})")
    as_json = binding.model_dump(mode="json", exclude_none=True)
    if reasons:
        return "stale", reasons + unknown, as_json
    if unknown:
        return "unknown", unknown, as_json
    return "fresh", ["bound and current"], as_json


# ---------------------------------------------------------------------------
# Batches
# ---------------------------------------------------------------------------


def _signature(
    route: Route,
    recipe_id: str | None,
    slot: Slot,
    inputs: SlotInputs,
    providers: list[str],
    n: int,
) -> str:
    return json.dumps(
        {
            "route": route,
            "recipe": recipe_id,
            "slot": slot.model_dump(mode="json"),
            "inputs": inputs.as_json(),
            "providers": providers,
            "candidates": n,
        },
        sort_keys=True,
    )


def _write_brief(
    ctx: Context, target: CompileTarget, spec: LoadedSpec, route: Route, priority: str
) -> Path:
    prompt, negative = prompt_for(spec, route)
    w, h = (int(v) for v in spec.spec.aspect.preferred.split("/"))
    look = target.look_for(spec.slot_id)
    brief: dict[str, Any] = {
        "id": f"compile-{target.id}-{brief_slug(spec.slot_id)}",
        "purpose": f"studio compile: image slot {spec.slot_id} of {target.repo}",
        "targets": [{"site": target.id, "slot": spec.site_slot}],
        "aspect_ratio": f"{w}:{h}",
        "prompt": prompt,
        "provider_preference": list(target.providers),
        "variants": target.candidates,
        "priority": priority,
    }
    if negative:
        brief["negative_prompt"] = negative
    if look is not None:
        brief["style_guide"] = str(look)
    recipe_id = target.recipe_for(spec, route)
    if recipe_id is not None:
        brief["recipe"] = recipe_id
    directory = ctx.workspace.state_dir / "compile" / target.id / "briefs"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{brief_slug(spec.slot_id)}-{spec.sha256[:16]}-{priority}.yaml"
    text = yaml.safe_dump(brief, sort_keys=True, allow_unicode=True)
    if not path.is_file() or path.read_text(encoding="utf-8") != text:
        path.write_text(text, encoding="utf-8")
    return path


def _existing_batch(
    conn: sqlite3.Connection,
    target: CompileTarget,
    spec: LoadedSpec,
    signature: str,
    now: datetime,
) -> sqlite3.Row | None:
    """The compile batch this slot can resume, if any. One made for another
    spec or other inputs is failed (unless another process is driving it)."""
    rows = conn.execute(
        "SELECT * FROM batches WHERE compile_target = ? AND compile_slot = ? "
        "AND status IN ('generating', 'generated', 'refused', 'published') ORDER BY created_at",
        (target.id, spec.slot_id),
    ).fetchall()
    keep: sqlite3.Row | None = None
    for row in rows:
        same = row["compile_spec_sha256"] == spec.sha256 and row["compile_inputs_json"] == signature
        if same:
            keep = row
            continue
        if row["status"] == "published":
            continue  # its publication stands in its own checkout
        conn.execute(
            "UPDATE batches SET status = 'failed', reason = ? WHERE id = ? "
            "AND status IN ('generating', 'generated', 'refused') "
            "AND (driver IS NULL OR driver_until < ?)",
            ("superseded: the slot's spec or inputs changed", row["id"], now.isoformat()),
        )
    conn.commit()
    return keep


def _rehome(conn: sqlite3.Connection, batch_id: str) -> None:
    """A batch published into ANOTHER checkout, which this one does not hold.
    Its publication rows stay live — that checkout still holds the files —
    and the batch is made publishable again so it can be published into this
    checkout without regenerating it."""
    conn.execute(
        "UPDATE batches SET status = 'unpublished', reason = ? WHERE id = ? AND status = 'published'",
        ("published into another checkout; publishing it into this one too", batch_id),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Candidates, the pick and the binding
# ---------------------------------------------------------------------------


def _index(asset: AssetRecord) -> int:
    idx = (asset.job_id or "").rsplit("-v", 1)[-1]
    return int(idx) if idx.isdigit() else 0


def _engine_recipe(raw_sidecar: dict[str, Any] | None) -> str | None:
    facade = raw_sidecar.get("qontinui_provenance") if raw_sidecar else None
    recipe = facade.get("recipe") if isinstance(facade, dict) else None
    value = recipe.get("sha256") if isinstance(recipe, dict) else None
    return value if isinstance(value, str) else None


def _applied_weights(raw_sidecar: dict[str, Any] | None) -> set[str]:
    facade = raw_sidecar.get("qontinui_provenance") if raw_sidecar else None
    out: set[str] = set()
    for model in (facade.get("models") if isinstance(facade, dict) else None) or []:
        for w in (model.get("weights") if isinstance(model, dict) else None) or []:
            if isinstance(w, dict) and isinstance(w.get("sha256"), str):
                out.add(w["sha256"])
    return out


@dataclass
class _Candidate:
    asset: AssetRecord
    sidecar: dict[str, Any] | None
    recipe_sha256: str
    usable: bool
    why_not: str | None


def _candidates(
    ctx: Context,
    target: CompileTarget,
    spec: LoadedSpec,
    route: Route,
    slot: Slot,
    inputs: SlotInputs,
    assets: list[AssetRecord],
) -> list[_Candidate]:
    out: list[_Candidate] = []
    for asset in sorted(assets, key=_index):
        sidecar = provenance.read(Path(asset.raw_path))
        adapter = (
            str(sidecar["provider"]["adapter"])
            if sidecar and isinstance(sidecar.get("provider"), dict)
            else ctx.inventory.get(asset.provider).adapter
        )
        engine = _engine_recipe(sidecar) if adapter == FACADE_ADAPTER else None
        recipe = compile_recipe_sha256(
            route=route,
            provider_id=asset.provider,
            adapter=adapter,
            model=asset.model,
            recipe_id=target.recipe_for(spec, route),
            engine_recipe_sha256=engine,
            look_sha256=inputs.look_sha256,
            slot=slot,
        )
        why_not: str | None = None
        if adapter == FACADE_ADAPTER and engine is None:
            why_not = "the facade reported no recipe hash in qontinui_provenance"
        if inputs.lora is not None and inputs.lora.value not in _applied_weights(sidecar):
            why_not = (
                f"the slot names LoRA '{spec.spec.style.lora}', and this candidate's "
                "provenance does not show those weights applied"
            )
        out.append(_Candidate(asset, sidecar, recipe, why_not is None, why_not))
    return out


def _binding(
    spec: LoadedSpec, pick: _Candidate, inputs: SlotInputs, alt: AltText | None
) -> ImageSlotProvenance:
    return ImageSlotProvenance(
        asset_sha256=pick.asset.sha256,
        spec_sha256=spec.sha256,
        source=ImageSlotAssetSource.generated,
        ai_generated=True,
        recipe_sha256=pick.recipe_sha256,
        imagery_sha256=inputs.imagery.value,
        exemplars_sha256=inputs.exemplars.value if inputs.exemplars else None,
        lora_sha256=inputs.lora.value if inputs.lora else None,
        alt_text=alt.text if alt else None,
    )


class _CompileNotifier:
    """Publication notices for a compile batch: recorded as files (the PR is
    how the operator hears of it), with the one-step unpublish replaced by what
    reverses a checkout write — reverting the PR."""

    def __init__(self, inner: FileNotifier, checkout: Path, report: CompileReport) -> None:
        self.inner = inner
        self.checkout = checkout
        self.report = report

    def notify(self, notice: Notice) -> Delivery:
        body = (
            f"{notice.body}\n\nWritten into the checkout {self.checkout}: it goes live when "
            "that checkout's PR lands, and is reversed by reverting the PR."
        )
        delivery = self.inner.notify(replace(notice, body=body, unpublish_command=None))
        self.report.notices.append(f"{notice.outcome}: {delivery.location}")
        return delivery


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------


def compile_slots(ctx: Context, options: CompileOptions, readers: Readers) -> CompileReport:
    """One compile run. Raises :class:`ConfigError` for a configuration that
    cannot be compiled at all (no checkout, an invalid spec, a target that
    collides with a site); everything per slot is reported, never raised."""
    target = options.target
    clash = ctx.sites.get(target.id)
    if clash is not None and not clash.checkout:
        raise ConfigError(f"target id '{target.id}' is also a site id under sites/")
    checkout = checkout_of(options.slots_dir, target)
    publish_dir = target.publish_path(checkout)
    all_specs = load_specs(options.slots_dir)
    specs = all_specs
    if options.only is not None:
        unknown = sorted(options.only - {s.slot_id for s in all_specs})
        if unknown:
            raise ConfigError(f"no spec for slot(s) {unknown} under {options.slots_dir}")
        specs = [s for s in all_specs if s.slot_id in options.only]
    for path in {target.look_for(s.slot_id) for s in specs} - {None}:
        if path is not None and not path.is_file():
            raise ConfigError(f"target '{target.id}': look {path} does not exist")
    report = CompileReport(
        target=target.id,
        repo=target.repo,
        checkout=str(checkout),
        publish_dir=str(publish_dir),
        changed_only=options.changed_only,
        dry_run=options.dry_run,
    )
    library = Library(ctx.conn)
    manifest = _read_manifest(publish_dir)

    # Every spec is a slot of the Site, filtered by --slot or not: the journal
    # then recognises every slot file the checkout holds. A slot no file is
    # published for (lucide, an authored SVG, a role never compiled) is a
    # meta-only placeholder: it names a manifest key, never a file.
    site_slots: dict[str, Slot] = {}
    for spec in all_specs:
        route, _ = route_for(target, spec)
        if route == "diffusion" or route == "traced":
            site_slots[spec.site_slot] = target.slot_for(spec, route)
        else:
            site_slots[spec.site_slot] = MANIFEST_ONLY_SLOT
    report.orphans = sorted(set(manifest) - {s.site_slot for s in all_specs})
    routed: list[tuple[LoadedSpec, SlotReport, Route | None]] = []
    for spec in specs:
        route, source = route_for(target, spec)
        rep = SlotReport(
            slot_id=spec.slot_id,
            site_slot=spec.site_slot,
            role=spec.spec.role.value,
            route=route,
            route_source=source,
            spec_sha256=spec.sha256,
            contexts={
                "breakpoints": [b.root for b in spec.spec.contexts.breakpoints],
                "themes": [t.value for t in spec.spec.contexts.themes],
                "dpr": [d.root for d in spec.spec.contexts.dpr],
            },
        )
        report.slots.append(rep)
        routed.append((spec, rep, route))
    site = target.site(checkout, site_slots) if site_slots else None
    if site is not None:
        try:
            site.check_paths(ctx.workspace.root)
            check_cross_site(
                {**{k: v for k, v in ctx.sites.items() if not v.checkout}, site.id: site}
            )
        except (ValueError, StudioError) as e:
            raise ConfigError(f"target '{target.id}': {e}") from e

    to_compile: list[tuple[LoadedSpec, SlotReport, Route, Slot, SlotInputs]] = []
    entries: dict[str, dict[str, Any] | None] = {}
    for spec, rep, route in routed:
        if route is None:
            rep.status, rep.reasons = "not_compiled", [rep.route_source]
        elif route == "lucide":
            entry = _lucide(target, spec, rep, checkout, options.slots_dir, options.alt_texts)
            current = manifest.get(spec.site_slot)
            if entry is not None and entry != current:
                if options.dry_run:
                    rep.reasons.append("its manifest entry would be written")
                else:
                    entries[spec.site_slot] = entry
                    rep.manifest_written = True
                    rep.reasons.append("its manifest entry is written")
        elif route == "svg":
            _authored(spec, rep, options.slots_dir)
        else:
            slot = site_slots[spec.site_slot]
            inputs = _inputs(spec, target, library, readers)
            rep.inputs = inputs.as_json()
            entry = manifest.get(spec.site_slot)
            status, reasons, binding = _staleness(
                ctx,
                target,
                spec,
                route,
                slot,
                entry if isinstance(entry, dict) else None,
                inputs,
                publish_dir,
                readers,
            )
            rep.reasons, rep.binding = reasons, binding
            wanted = status == "stale" or (status == "missing" and not options.changed_only)
            if status == "stale" or (status == "missing" and wanted):
                rep.status = "planned"
            elif status == "missing":
                rep.status = "missing"
                rep.reasons = [
                    *reasons,
                    "left for an interactive compile: --changed re-renders stale bindings only",
                ]
            else:
                rep.status = status
            if wanted:
                missing = inputs.unknown()
                if missing:
                    rep.status = "unknown"
                    rep.reasons = [
                        *reasons,
                        *missing,
                        "a binding must record every input it was made from; nothing is "
                        "generated while one is unreadable",
                    ]
                elif not options.dry_run:
                    to_compile.append((spec, rep, route, slot, inputs))

    notifier = _CompileNotifier(FileNotifier(ctx.workspace.notices_dir), checkout, report)
    if not options.dry_run:
        # Studio owns the manifest: an entry no spec declares any more goes,
        # with the files it names, in this same compile.
        entries.update(dict.fromkeys(report.orphans))
    if entries:
        assert site is not None
        update = update_entries(
            ctx.conn, site, entries, notifier, ctx.clock, root=ctx.workspace.root
        )
        report.retired = [n for n in update.written if n in report.orphans] + [
            str(p) for p in update.removed_files
        ]
        report.kept = update.kept
    if to_compile:
        assert site is not None
        _generate_and_publish(ctx, options, readers, report, site, to_compile, checkout, notifier)
    return report


LUCIDE_PACKAGE = "lucide-react"


def lucide_selection_sha256(icon: str) -> str:
    """The content address of a lucide selection: sha256 of the RFC 8785
    canonical JSON ``{"icon": <name>, "package": "lucide-react"}``. An icon is
    selected, not made, so its binding addresses the SELECTION, the same in
    every lucide-react version."""
    return canonical_sha256({"icon": icon, "package": LUCIDE_PACKAGE})


def _lucide(
    target: CompileTarget,
    spec: LoadedSpec,
    rep: SlotReport,
    checkout: Path,
    slots_dir: Path,
    alt_texts: dict[str, AltText],
) -> dict[str, Any] | None:
    """Check a lucide slot and return the manifest entry it should have, or
    None when no entry may be written (no icon named, the icon unverified or
    unknown, an alt text owed)."""
    icon = target.override(spec.slot_id).icon
    if icon is None:
        rep.status = "icon_unknown"
        rep.reasons = [
            f"a lucide slot names its icon: add `icon:` for '{spec.slot_id}' in target "
            f"'{target.id}' (icons are selected, never generated)"
        ]
        return None
    found = selected.check_lucide(icon, checkout, slots_dir)
    rep.status = found.status
    rep.lucide = {"icon": found.icon, "component": found.component, "detail": found.detail}
    rep.reasons = [
        f"render lucide-react's <{found.component} /> in this slot; studio publishes no image "
        f"file ({found.detail})"
    ]
    if found.status != "selected":
        return None
    asset = lucide_selection_sha256(icon)
    alt: AltText | None = None
    if spec.spec.alt.root.kind == "required":
        alt = alt_texts.get(spec.slot_id)
        if alt is None or alt.asset_sha256 != asset:
            rep.status = "awaiting_alt_text"
            rep.reasons.append(
                f'the slot is alt: required: run again with --alt-texts ({{"{spec.slot_id}": '
                f'{{"asset_sha256": "{asset}", "text": "..."}}}})'
            )
            return None
    binding = ImageSlotProvenance(
        asset_sha256=asset,
        spec_sha256=spec.sha256,
        source=ImageSlotAssetSource.selected,
        ai_generated=False,
        alt_text=alt.text if alt else None,
    )
    problems = binding_problems(spec.spec, binding, spec.sha256)
    if problems:
        rep.status, rep.reasons = "refused", [*rep.reasons, *problems]
        return None
    binding_json = binding.model_dump(mode="json", exclude_none=True)
    rep.binding = binding_json
    return {
        "route": "lucide",
        "slot_id": spec.slot_id,
        "slot_spec_sha256": spec.sha256,
        "lucide": {"package": LUCIDE_PACKAGE, "icon": icon, "component": found.component},
        "files": {},
        "sha256": {},
        "ai_generated": False,
        "binding": binding_json,
    }


def _authored(spec: LoadedSpec, rep: SlotReport, slots_dir: Path) -> None:
    found = selected.check_authored_svg(slots_dir, spec)
    rep.status = found.status
    rep.reasons = list(found.problems) or [
        "an agent-written inline SVG beside the spec"
        if found.status == "authored"
        else f"write {found.path}"
    ]
    if found.status != "authored":
        prompt, _ = prompt_for(spec, "svg")
        rep.authoring = selected.authoring_brief(spec, prompt)
        rep.authoring["write_to"] = str(found.path)


def _generate_and_publish(
    ctx: Context,
    options: CompileOptions,
    readers: Readers,
    report: CompileReport,
    site: Site,
    work: list[tuple[LoadedSpec, SlotReport, Route, Slot, SlotInputs]],
    checkout: Path,
    notifier: _CompileNotifier,
) -> None:
    target = options.target
    ctx.sites = {**ctx.sites, site.id: site}
    if ctx.adapters.refusal is not None:
        refusal = ctx.adapters.refusal
        report.refusal = {"code": refusal.code, "detail": str(refusal)}
        for _, rep, *_ in work:
            rep.status, rep.reasons = "generation_refused", [f"{refusal.code}: {refusal}"]
        return
    # The image-generation dial (and QONTINUI_IMAGEGEN), read BEFORE any batch
    # exists: `off`, or `local_only` with none of the target's providers the
    # fleet's own, refuses every slot with no provider call.
    try:
        _, decision = policy_adapters(ctx, {target.repo}, list(target.providers))
        report.imagegen = decision.describe()
    except ImageGenerationRefused as e:
        report.refusal = {"code": e.code, "detail": str(e)}
        for _, rep, *_ in work:
            rep.status, rep.reasons = "generation_refused", [f"{e.code}: {e}"]
        return
    priority = "bulk" if options.changed_only else "interactive"
    for spec, rep, route, slot, inputs in work:
        try:
            _compile_one(
                ctx, options, readers, site, notifier, spec, rep, route, slot, inputs, priority
            )
        except ImageGenerationRefused as e:
            # The dial moved mid-run: no further provider call for this slot.
            ctx.conn.rollback()
            rep.status, rep.reasons = "generation_refused", [f"{e.code}: {e}"]
            report.refusal = {"code": e.code, "detail": str(e)}


def _compile_one(
    ctx: Context,
    options: CompileOptions,
    readers: Readers,
    site: Site,
    notifier: _CompileNotifier,
    spec: LoadedSpec,
    rep: SlotReport,
    route: Route,
    slot: Slot,
    inputs: SlotInputs,
    priority: str,
) -> None:
    target = options.target
    now = ctx.clock()
    signature = _signature(
        route,
        target.recipe_for(spec, route),
        slot,
        inputs,
        list(target.providers),
        target.candidates,
    )
    batch = _existing_batch(ctx.conn, target, spec, signature, now)
    if batch is None:
        brief_path = _write_brief(ctx, target, spec, route, priority)
        loaded = load_brief(brief_path, root=ctx.workspace.root)
        problems = validate_brief(loaded.brief, ctx.sites, ctx.inventory)
        if problems:
            rep.status, rep.reasons = "refused", [f"brief: {p}" for p in problems]
            return
        batch_id = new_batch(ctx, loaded, publish=False)
        ctx.conn.execute(
            "UPDATE batches SET compile_target = ?, compile_slot = ?, compile_spec_sha256 = ?, "
            "compile_inputs_json = ? WHERE id = ?",
            (target.id, spec.slot_id, spec.sha256, signature, batch_id),
        )
        ctx.conn.commit()
        status = "generating"
    else:
        batch_id, status = str(batch["id"]), str(batch["status"])
        rep.reasons.append(f"resumed batch {batch_id} ({status})")
    rep.batch_id = batch_id
    if status == "generating":
        outcome = drive_batch(ctx, batch_id, publish=False)
        status = outcome.status
        if status == "generating":
            rep.status = "generating"
            rep.reasons += (
                [
                    f"job {j}: {outcome.last_attempts.get(j, 'deferred (quota or backoff)')}"
                    for j in outcome.deferred_jobs
                ]
                + ([outcome.note] if outcome.note else [])
                # The scheduler names `studio jobs run`; a compile batch resumes
                # through `studio compile` only.
                + (
                    [outcome.stopped_waiting.replace("studio jobs run", "studio compile")]
                    if outcome.stopped_waiting
                    else []
                )
            )
            rep.reasons.append("run studio compile again to resume this batch")
            return
        if status == "failed":
            rep.status = "failed"
            rep.reasons += [
                f"job {j}: {outcome.last_attempts.get(j, 'failed')}" for j in outcome.failed_jobs
            ]
            return
    library = Library(ctx.conn)
    candidates = _candidates(ctx, target, spec, route, slot, inputs, library.assets(batch_id))
    confirmed = _upload_candidates(readers.store, candidates, rep)
    pick = _publish_pick(
        ctx,
        options,
        readers,
        site,
        notifier,
        spec,
        rep,
        route,
        slot,
        inputs,
        candidates,
        library,
        batch_id,
        published_before=batch is not None and status == "published",
    )
    _propose(readers.store, target, spec, candidates, pick, rep, confirmed)


def _publish_pick(
    ctx: Context,
    options: CompileOptions,
    readers: Readers,
    site: Site,
    notifier: _CompileNotifier,
    spec: LoadedSpec,
    rep: SlotReport,
    route: Route,
    slot: Slot,
    inputs: SlotInputs,
    candidates: list[_Candidate],
    library: Library,
    batch_id: str,
    *,
    published_before: bool,
) -> _Candidate | None:
    """Choose among the candidates and publish the pick into the checkout.
    Returns the pick (None when no candidate could be picked); ``rep`` says
    what happened."""
    target = options.target
    usable = [c for c in candidates if c.usable]
    if not usable:
        rep.status = "refused"
        rep.reasons += ["no usable candidate: " + "; ".join(c.why_not or "" for c in candidates)]
        return None
    pairs = [
        (c.asset, v)
        for c in usable
        for v in library.variants(c.asset.uid)
        if v.site == site.id and v.slot == spec.site_slot
    ]
    pick_uid = lowest_index_pick(pairs).get((site.id, spec.site_slot))
    pick = next((c for c in usable if c.asset.uid == pick_uid), None)
    if pick is None:
        rep.status = "refused"
        rep.reasons.append("no usable candidate has a derivative for this slot")
        return None
    rep.pick = {
        "pick": PICK_UNJUDGED,
        "asset_sha256": pick.asset.sha256,
        "index": _index(pick.asset),
        "of": len(candidates),
        "provider": pick.asset.provider,
        "model": pick.asset.model,
    }
    variants = [v for v in library.variants(pick.asset.uid) if v.slot == spec.site_slot]
    alt: AltText | None = None
    if spec.spec.alt.root.kind == "required":
        alt = options.alt_texts.get(spec.slot_id)
        if alt is None or alt.asset_sha256 != pick.asset.sha256:
            rep.status = "awaiting_alt_text"
            rep.reasons.append(
                "the slot is alt: required: write an alt text for the picked asset and run "
                f'again with --alt-texts ({{"{spec.slot_id}": {{"asset_sha256": '
                f'"{pick.asset.sha256}", "text": "..."}}}})'
            )
            rep.derivatives = _derivative_rows(variants)
            return pick
    binding = _binding(spec, pick, inputs, alt)
    problems = binding_problems(spec.spec, binding, spec.sha256)
    if problems:
        rep.status, rep.reasons = "refused", [*rep.reasons, *problems]
        return pick
    if published_before:
        _rehome(ctx.conn, batch_id)
        rep.reasons.append("its earlier publication went to another checkout; re-homed here")
    key = (site.id, spec.site_slot)
    binding_json = binding.model_dump(mode="json", exclude_none=True)
    extra: dict[str, Any] = {
        "slot_id": spec.slot_id,
        "slot_spec_sha256": spec.sha256,
        "route": route,
        "recipe": target.recipe_for(spec, route),
        "binding": binding_json,
        "pick": dict(rep.pick),
    }
    if alt is not None:
        extra["alt_text"] = alt.text
    result = publish_batch(
        ctx.conn,
        ctx.sites,
        batch_id,
        notifier,
        ctx.clock,
        bridge=ctx.bridge,
        fetch=ctx.fetch,
        root=ctx.workspace.root,
        sleep=ctx.sleep,
        pick={key: pick.asset.uid},
        manifest_extra={key: extra},
    )
    rep.gates = [
        {"gate": g.gate, "subject": g.subject, "outcome": g.outcome, "detail": g.detail}
        for g in result.gate_results
    ]
    if not result.published:
        rep.status = "refused"
        rep.reasons += [f"{g.gate} {g.outcome}: {g.detail}" for g in result.failures]
        return pick
    written = _read_manifest(site.publish_dir).get(spec.site_slot)
    if not isinstance(written, dict) or written.get("binding") != binding_json:
        rep.status = "refused"
        rep.reasons.append("the manifest does not read back with this binding")
        return pick
    rep.status = "compiled"
    rep.binding = binding_json
    rep.derivatives = _derivative_rows(variants, site.publish_dir, written)
    for v in variants:
        stored = readers.store.upload(Path(v.path), "derivative", provenance.read(Path(v.path)))
        for row in rep.derivatives:
            if row["format"] == v.format:
                row["store"] = _outcome(stored)
    return pick


def _propose(
    store: ImageStore,
    target: CompileTarget,
    spec: LoadedSpec,
    candidates: list[_Candidate],
    pick: _Candidate | None,
    rep: SlotReport,
    confirmed: set[str],
) -> None:
    """Propose every candidate the store confirmed for the slot: the others
    as ``candidate`` first, then the pick — ``chosen`` once it is published
    into the checkout, else ``candidate`` (it is not chosen anywhere yet)."""
    rows = {row["asset_sha256"]: row for row in rep.candidates}
    others = [c for c in candidates if pick is None or c.asset.uid != pick.asset.uid]
    for c in others:
        if c.asset.sha256 in confirmed:
            out = store.propose(
                repo=target.repo,
                slot_id=spec.slot_id,
                asset_sha256=c.asset.sha256,
                status="candidate",
                recipe_hash=c.recipe_sha256,
                judge=_judge(pick, candidates) | {"index": _index(c.asset)},
            )
            rows[c.asset.sha256]["proposal"] = _outcome(out)
    if pick is None or rep.pick is None:
        return
    if pick.asset.sha256 not in confirmed:
        rep.pick["store"] = {
            "status": "unknown",
            "detail": "not proposed: the store never confirmed the candidate's upload",
        }
        return
    status: CandidateStatus = "chosen" if rep.status == "compiled" else "candidate"
    out = store.propose(
        repo=target.repo,
        slot_id=spec.slot_id,
        asset_sha256=pick.asset.sha256,
        status=status,
        recipe_hash=pick.recipe_sha256,
        judge=_judge(pick, candidates),
    )
    rep.pick["store"] = _outcome(out)


def _outcome(o: StoreOutcome) -> dict[str, str]:
    return {"status": o.status, "detail": o.detail}


def _judge(pick: _Candidate | None, candidates: list[_Candidate]) -> dict[str, Any]:
    return {
        "pick": PICK_UNJUDGED,
        "picked_index": _index(pick.asset) if pick else None,
        "candidates": len(candidates),
        "rule": "lowest index among usable candidates (the launch kit's pick)",
    }


def _upload_candidates(
    store: ImageStore, candidates: list[_Candidate], rep: SlotReport
) -> set[str]:
    """Upload every candidate (the store holds what was made, picked or not);
    returns the sha256 of every upload the store confirmed."""
    confirmed: set[str] = set()
    for c in candidates:
        up = store.upload(Path(c.asset.raw_path), "candidate", c.sidecar)
        if up.confirmed:
            confirmed.add(c.asset.sha256)
        row: dict[str, Any] = {
            "index": _index(c.asset),
            "asset_sha256": c.asset.sha256,
            "provider": c.asset.provider,
            "model": c.asset.model,
            "recipe_sha256": c.recipe_sha256,
            "usable": c.usable,
            "store": _outcome(up),
        }
        if c.why_not:
            row["why_not"] = c.why_not
        rep.candidates.append(row)
    return confirmed


def _derivative_rows(
    variants: list[VariantRecord],
    publish_dir: Path | None = None,
    entry: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    rows = []
    files = (entry or {}).get("files", {}) if entry else {}
    for v in sorted(variants, key=lambda v: v.format):
        path = (
            (publish_dir / files[v.format]) if publish_dir and v.format in files else Path(v.path)
        )
        rows.append(
            {
                "format": v.format,
                "file": str(path),
                "bytes": path.stat().st_size if path.is_file() else None,
                "width": v.width,
                "height": v.height,
                "sha256": v.sha256,
                "marked": marker.is_marked(path),
            }
        )
    return rows
