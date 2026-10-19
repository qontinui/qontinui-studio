"""The inputs an image slot's binding records and its staleness is judged on
(``ImageSlotProvenance``: ``imagery_sha256``, ``exemplars_sha256``,
``lora_sha256``, ``recipe_sha256``), each read from where it is declared:

* **imagery** — the treatment a slot's ``style.imagery`` names, a coord prompt
  document (``domain_spec/visual-system#imagery``), read with the fleet's
  device credential. The digest is the sha256 of the named SECTION's text (the
  whole body when the reference names no section).
* **exemplars** — the exemplar set a slot's ``style.exemplars`` names: the
  studio library's set of that tag (``studio library tag``), with each member's
  approval state. Its digest identifies the membership in force; a set with no
  member here is UNKNOWN, never a known empty set. Plan Phase 22 owns versioned exemplar sets in qontinui-web; until
  it lands, the library tag is the set studio can read.
* **LoRA** and **engine recipes** — the image licence manifest and recipes
  (qontinui ``docker/imagegen``, plan Phase 2), from a checkout named by
  ``$STUDIO_IMAGEGEN_DIR``. Recipe hashes come from that manifest's own CLI
  (``python -m qontinui_imagegen.manifest --hashes``), never a re-implementation.

Every reader answers a :class:`Digest`: a value, or None with why it could not
be read. An unreadable input is UNKNOWN — never a match, never a mismatch.
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import httpx
import yaml

from qontinui_studio.compiler.jcs import canonical_sha256
from qontinui_studio.device_auth import DeviceCredentials, https_or_loopback
from qontinui_studio.gateway import runner_url
from qontinui_studio.imagegen_policy import PRODUCTION_COORD, coord_base_url
from qontinui_studio.library import Library

IMAGEGEN_DIR_ENV = "STUDIO_IMAGEGEN_DIR"
PROMPT_DOCUMENTS_PATH = "/coord/agent-prompt-documents"
READ_TIMEOUT_S = 20.0
HASHES_TIMEOUT_S = 120.0

_IMAGERY_REF = re.compile(
    r"^(domain_spec|decision_record)/([a-z0-9]+(?:-[a-z0-9]+)*)(?:#([a-z0-9]+(?:-[a-z0-9]+)*))?$"
)
_HEADING = re.compile(r"^(#{1,6})[ \t]+(.*?)[ \t]*#*[ \t]*$")
_FENCE = re.compile(r"^[ \t]{0,3}(```|~~~)")
_HASH_LINE = re.compile(r"^([A-Za-z0-9._-]+) v(\d+) ([0-9a-f]{64})$")


@dataclass(frozen=True)
class Digest:
    """An input's digest, or UNKNOWN (``value`` None) with ``source`` saying
    why. ``source`` names where a known value was read (shown in the report)."""

    value: str | None
    source: str

    @property
    def known(self) -> bool:
        return self.value is not None


def heading_slug(text: str) -> str:
    """A heading's anchor: lowercase, every run of other characters one '-'
    (``Imagery & iconography`` -> ``imagery-iconography``)."""
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def section_text(body: str, fragment: str) -> str | None:
    """The section a ``#fragment`` names: the ONE heading whose anchor is the
    fragment or begins with it as a whole word (``imagery`` names ``Imagery &
    iconography``), through the line before the next heading of the same or a
    higher level. Headings inside fenced code are not headings. None when no
    heading, or more than one, matches."""
    lines = body.replace("\r\n", "\n").split("\n")
    headings: list[tuple[int, int, str]] = []  # (line, level, slug)
    fenced = False
    for i, line in enumerate(lines):
        if _FENCE.match(line):
            fenced = not fenced
            continue
        m = None if fenced else _HEADING.match(line)
        if m:
            headings.append((i, len(m.group(1)), heading_slug(m.group(2))))
    matches = [h for h in headings if h[2] == fragment or h[2].startswith(fragment + "-")]
    if len(matches) != 1:
        return None
    start, level, _ = matches[0]
    end = next((i for i, lv, _ in headings if i > start and lv <= level), len(lines))
    return "\n".join(lines[start:end]).rstrip("\n") + "\n"


class ImageryReader:
    """Reads imagery treatments from coord, once per reference per run."""

    def __init__(
        self,
        base_url: str | None = None,
        http: httpx.Client | None = None,
        jwt_file: Path | None = None,
        runner: Callable[[], str] = runner_url,
    ) -> None:
        self.base_url = (base_url or coord_base_url()).rstrip("/")
        self.http = http or httpx.Client(timeout=READ_TIMEOUT_S)
        refusal = (
            None
            if self.base_url == PRODUCTION_COORD
            else f"the runner mints for production coord and {self.base_url} is not it"
        )
        self.credentials = DeviceCredentials(self.http, refusal, jwt_file=jwt_file, runner=runner)
        self._cache: dict[str, Digest] = {}

    def __call__(self, ref: str) -> Digest:
        if ref not in self._cache:
            self._cache[ref] = self._read(ref)
        return self._cache[ref]

    def _read(self, ref: str) -> Digest:
        if not https_or_loopback(self.base_url):
            return Digest(
                None, f"coord {self.base_url} is neither https nor loopback; no token is sent"
            )
        m = _IMAGERY_REF.fullmatch(ref)
        if m is None:
            return Digest(None, f"imagery reference '{ref}' is not <kind>/<name>[#section]")
        kind, name, fragment = m.group(1), m.group(2), m.group(3)
        url = f"{self.base_url}{PROMPT_DOCUMENTS_PATH}/{kind}/{name}"
        skipped: list[str] = []
        refused: list[str] = []
        for cred in self.credentials.credentials(skipped):
            try:
                resp = self.http.get(url, headers={"Authorization": f"Bearer {cred.token}"})
            except httpx.HTTPError as e:
                return Digest(None, f"GET {url}: {type(e).__name__}")
            if resp.status_code in (401, 403):
                refused.append(f"HTTP {resp.status_code} to {cred.source}")
                continue
            if resp.status_code == 404:
                return Digest(None, f"{kind}/{name}: coord serves no such document (404)")
            if resp.status_code != 200:
                return Digest(None, f"GET {url} answered HTTP {resp.status_code}")
            try:
                doc = resp.json()
            except ValueError:
                return Digest(None, f"GET {url}: unreadable body")
            return _imagery_digest(doc, kind, name, fragment)
        return Digest(None, "no usable device credential (" + "; ".join([*skipped, *refused]) + ")")


def _imagery_digest(doc: object, kind: str, name: str, fragment: str | None) -> Digest:
    if not isinstance(doc, dict) or not isinstance(doc.get("body"), str):
        return Digest(None, f"{kind}/{name}: the answer carries no body")
    if doc.get("withdrawn") is True:
        return Digest(None, f"{kind}/{name} is withdrawn")
    version = doc.get("current_version")
    seed = doc.get("unedited_seed")
    if seed is True:
        return Digest(None, f"{kind}/{name} is an unedited skeleton, not an authored treatment")
    if seed is None and doc.get("default_source") is not None and version == 1:
        # A coord serving no `unedited_seed`: a seeded document still at v1 is
        # the skeleton; past v1 the body alone could tell, so it is UNKNOWN.
        return Digest(None, f"{kind}/{name} may be an unedited skeleton (seeded, v1)")
    if seed is None and doc.get("default_source") is not None:
        return Digest(
            None,
            f"{kind}/{name}: coord serves no unedited_seed and the document was seeded; "
            "authored or skeleton is UNKNOWN",
        )
    where = f"{kind}/{name}@v{version}" if isinstance(version, int) else f"{kind}/{name}"
    body: str = doc["body"]
    if fragment is None:
        text = body.replace("\r\n", "\n")
    else:
        found = section_text(body, fragment)
        if found is None:
            return Digest(
                None, f"{where}: no single section is named '#{fragment}' (none, or ambiguous)"
            )
        text, where = found, f"{where}#{fragment}"
    return Digest(hashlib.sha256(text.encode("utf-8")).hexdigest(), where)


def exemplar_digest(library: Library, set_id: str) -> Digest:
    """The membership of library set ``set_id`` (its tag), as a digest."""
    pairs = sorted((a.sha256, a.approval_status) for a in library.tagged(set_id))
    if not pairs:
        # A set this machine never saw is not a known EMPTY set: its version is
        # UNKNOWN until a member is in the library here (Phase 22 owns sets).
        return Digest(
            None,
            f"exemplar set '{set_id}' has no member in this studio's library (tag assets with "
            "`studio library tag`)",
        )
    members = [{"asset_sha256": sha, "approval": approval} for sha, approval in pairs]
    digest = canonical_sha256(
        {"schema": "qontinui-studio/exemplar-set/v1", "set": set_id, "members": members}
    )
    return Digest(digest, f"library set '{set_id}' ({len(members)} member(s))")


class ImagegenManifest:
    """The image licence manifest and recipes of a qontinui checkout's
    ``docker/imagegen`` (``$STUDIO_IMAGEGEN_DIR``), read-only."""

    def __init__(self, directory: Path | None = None) -> None:
        raw = os.environ.get(IMAGEGEN_DIR_ENV, "").strip()
        self.directory = directory if directory is not None else (Path(raw) if raw else None)
        self._hashes: dict[str, str] | Digest | None = None
        self._components: dict[str, object] | Digest | None = None

    def _unconfigured(self) -> Digest | None:
        if self.directory is None:
            return Digest(
                None,
                f"no image licence manifest configured (set ${IMAGEGEN_DIR_ENV} to a qontinui "
                "checkout's docker/imagegen)",
            )
        if not (self.directory / "manifest.yaml").is_file():
            return Digest(None, f"{self.directory}: no manifest.yaml")
        return None

    def recipe(self, recipe_id: str) -> Digest:
        """The current hash of facade recipe ``recipe_id``."""
        if self._hashes is None:
            self._hashes = self._read_hashes()
        if isinstance(self._hashes, Digest):
            return self._hashes
        value = self._hashes.get(recipe_id)
        if value is None:
            return Digest(None, f"recipe '{recipe_id}' is not in {self.directory}/recipes")
        return Digest(value, f"recipe '{recipe_id}' ({self.directory})")

    def _read_hashes(self) -> dict[str, str] | Digest:
        missing = self._unconfigured()
        if missing is not None:
            return missing
        assert self.directory is not None
        try:
            proc = subprocess.run(
                [sys.executable, "-m", "qontinui_imagegen.manifest", "--hashes"],
                cwd=self.directory,
                capture_output=True,
                text=True,
                timeout=HASHES_TIMEOUT_S,
                check=False,
                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            )
        except (OSError, subprocess.TimeoutExpired) as e:
            return Digest(None, f"the recipe-hash CLI did not run: {type(e).__name__}")
        if proc.returncode != 0:
            tail = (proc.stdout.strip().splitlines() or ["no output"])[-1][:200]
            return Digest(
                None, f"the image licence manifest does not check (exit {proc.returncode}: {tail})"
            )
        hashes: dict[str, str] = {}
        for line in proc.stdout.splitlines():
            m = _HASH_LINE.fullmatch(line.strip())
            if m:
                hashes[m.group(1)] = m.group(3)
        return hashes

    def lora(self, lora_id: str) -> Digest:
        """The sha256 of LoRA ``lora_id``'s one weights file, by its id in the
        licence manifest."""
        if self._components is None:
            self._components = self._read_components()
        if isinstance(self._components, Digest):
            return self._components
        entry = self._components.get(lora_id)
        if not isinstance(entry, dict):
            return Digest(None, f"LoRA '{lora_id}' is not in the image licence manifest")
        files = entry.get("files")
        shas = [
            f["sha256"]
            for f in (files if isinstance(files, list) else [])
            if isinstance(f, dict) and isinstance(f.get("sha256"), str)
        ]
        if len(shas) != 1 or not re.fullmatch(r"[0-9a-f]{64}", shas[0]):
            return Digest(None, f"LoRA '{lora_id}' does not pin exactly one weights file")
        return Digest(shas[0], f"LoRA '{lora_id}' (licence manifest)")

    def _read_components(self) -> dict[str, object] | Digest:
        missing = self._unconfigured()
        if missing is not None:
            return missing
        assert self.directory is not None
        try:
            raw = yaml.safe_load((self.directory / "manifest.yaml").read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as e:
            return Digest(None, f"{self.directory}/manifest.yaml unreadable: {type(e).__name__}")
        comps = raw.get("components") if isinstance(raw, dict) else None
        if not isinstance(comps, list):
            return Digest(None, f"{self.directory}/manifest.yaml has no components list")
        return {str(c["id"]): c for c in comps if isinstance(c, dict) and "id" in c}
