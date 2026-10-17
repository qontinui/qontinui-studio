"""products/*.yaml — a portfolio product the launch kit can launch.

The product's numbers do NOT live here: they come from the factory's build
record for ``slug``. This file says only where to read that record, how to
reach the running product for capture, which brief decorates it, and where the
bundle publishes.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from qontinui_studio.errors import ConfigError
from qontinui_studio.gates import origin
from qontinui_studio.launch.record import SLUG_PATTERN, WEB_BASE, public_record_url
from qontinui_studio.sites import Site, forbidden_bridge_reason

#: A page script: one or more `/segment`s ending in .js or .mjs. Every segment
#: is non-empty and starts with a non-dot URL-safe character, so `//host`,
#: empty segments and every dot-led segment (`.`, `..`, `...`, `.hidden`) are
#: refused, as are schemes, queries, fragments and ports (no `:`).
PAGE_SCRIPT_RE = re.compile(r"(?:/[A-Za-z0-9_~-][A-Za-z0-9._~-]*)+\.m?js")
#: A segment that names a machine rather than a site path: loopback, a private
#: zone, an IPv4 literal, or an operator home/system root. The publish leak
#: scan would catch these later; refusing at load says so where it is written.
_PAGE_SCRIPT_MACHINE_RE = re.compile(
    r"(?:^|/)(?:localhost|home|users|root|tmp|var|etc|mnt|private)(?=/|\.m?js$)"
    r"|(?:^|/)\d{1,3}(?:\.\d{1,3}){3}(?=/|\.m?js$)"
    r"|\.(?:local|internal|lan|corp)(?=/|\.m?js$)",
    re.IGNORECASE,
)


class RecordSource(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    # `public` reads the frozen snapshot anyone can read (what the bundle links
    # to); `authed` reads the live document through the authed proxy.
    door: Literal["public", "authed"] = "public"
    web_base: str = WEB_BASE
    # Where readers of the bundle go to check the record. Defaults to the
    # public snapshot route; set it once the public org/domain is decided.
    public_url: str | None = None
    # Tenant binding (a public slug belongs to whichever tenant published it
    # first). Publication proves the snapshot is THIS operator's either through
    # the authed product listing (a web bearer is configured) or, without one,
    # by a pin here: the snapshot's content_sha256 and/or version as published.
    expected_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    expected_version: int | None = Field(default=None, ge=1)


class CaptureConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    script: Path
    # Exactly one of: the running product's URL, or a static directory the kit
    # serves on 127.0.0.1 for the duration of the capture.
    base_url: str | None = None
    serve_dir: Path | None = None
    # Build the studio's own review page into serve_dir first (the studio's UI,
    # used by its self-launch).
    build_review_page: bool = False
    record_video: bool = True

    @model_validator(mode="after")
    def _one_target(self) -> CaptureConfig:
        if (self.base_url is None) == (self.serve_dir is None):
            raise ValueError("capture needs exactly one of base_url or serve_dir")
        return self


class PublishTarget(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    publish_dir: Path
    public_url: str | None = None
    # D7, preview first (the asset pipeline's order and binding): the bundle is
    # written to ``preview_dir``; a preview server serves it at ``preview_url``
    # (the landing page) and ``preview_manifest_url`` (its launch-manifest.json);
    # the page is loaded in a browser carrying the UI Bridge whose ``/control/*``
    # routes hang off ``ui_bridge_url`` — the PREVIEW's bridge, never the
    # runner's own UI (:9876). All four or none; none refuses (no waiver).
    ui_bridge_url: str | None = None
    preview_url: str | None = None
    preview_dir: Path | None = None
    preview_manifest_url: str | None = None
    commercial: bool = False
    # Site-root-relative scripts every bundle page loads, in order — e.g. the
    # host site's UI Bridge boot (`/_hub/ui-bridge.js`), which mounts the React
    # auto-registration provider with media discovery so D7 can see the page's
    # images. Same-origin by construction: a path, never a URL, so a published
    # page can only run code its own host serves.
    page_scripts: tuple[str, ...] = ()

    @property
    def has_preview(self) -> bool:
        return bool(
            self.ui_bridge_url
            and self.preview_url
            and self.preview_dir
            and self.preview_manifest_url
        )

    @field_validator("page_scripts")
    @classmethod
    def _page_scripts(cls, scripts: tuple[str, ...]) -> tuple[str, ...]:
        for path in scripts:
            if not PAGE_SCRIPT_RE.fullmatch(path):
                raise ValueError(
                    f"page_scripts entry {path!r} must be a site-root-relative path to a .js "
                    "or .mjs file (e.g. /_hub/ui-bridge.js): no scheme, host, port, query, "
                    "fragment, empty segment or dot-led segment"
                )
            if _PAGE_SCRIPT_MACHINE_RE.search(path):
                raise ValueError(
                    f"page_scripts entry {path!r} names a machine (loopback, private zone, IP "
                    "literal or operator path), not a public site path"
                )
        if len(set(scripts)) != len(scripts):
            raise ValueError("page_scripts lists a path twice")
        return scripts

    @model_validator(mode="after")
    def _preview(self) -> PublishTarget:
        fields = {
            "ui_bridge_url": self.ui_bridge_url,
            "preview_url": self.preview_url,
            "preview_dir": self.preview_dir,
            "preview_manifest_url": self.preview_manifest_url,
        }
        missing = [k for k, v in fields.items() if not v]
        if 0 < len(missing) < len(fields):
            raise ValueError(
                "a D7 preview needs all of ui_bridge_url, preview_url, preview_dir and "
                f"preview_manifest_url; missing {missing}"
            )
        if self.ui_bridge_url:
            bridge = urlparse(self.ui_bridge_url)
            if bridge.scheme not in ("http", "https") or not bridge.hostname:
                raise ValueError(
                    f"ui_bridge_url must be an http(s) URL, got '{self.ui_bridge_url}'"
                )
            # The asset pipeline's guard: the host httpx will request,
            # resolved; ports 9875/9876 refused when local or unresolvable.
            why = forbidden_bridge_reason(self.ui_bridge_url)
            if why is not None:
                raise ValueError(why)
        for name in ("preview_url", "preview_manifest_url"):
            url = getattr(self, name)
            if url and urlparse(url).scheme not in ("http", "https"):
                raise ValueError(f"{name} must be an http(s) URL, got '{url}'")
        if (
            self.preview_url
            and self.preview_manifest_url
            and origin(self.preview_url) != origin(self.preview_manifest_url)
        ):
            # D7 requires every image on the page to be served beside the
            # launch manifest, at exactly urljoin(preview_manifest_url, path):
            # a manifest on another origin could never match a page image.
            raise ValueError(
                "preview_manifest_url must be on the same origin as preview_url "
                f"({self.preview_manifest_url} vs {self.preview_url})"
            )
        for name in ("ui_bridge_url", "preview_url", "preview_manifest_url"):
            url = getattr(self, name)
            if url and "#" in url:
                # A hash-routed URL would put D7's launch/attempt binding inside
                # the fragment (a same-document navigation with an empty query).
                raise ValueError(
                    f"{name} must not contain a #fragment (got '{url}'); D7 binds the "
                    "preview to a launch through the real query string"
                )
        if self.preview_dir is not None:
            preview, publish = self.preview_dir.resolve(), self.publish_dir.resolve()
            if (
                preview == publish
                or preview.is_relative_to(publish)
                or publish.is_relative_to(preview)
            ):
                raise ValueError(
                    "preview_dir must be neither publish_dir, inside it, nor a parent of it"
                )
        return self

    def check_sites(self, sites: dict[str, Site]) -> None:
        """The launch's publish and preview dirs are rewritten under the
        LAUNCH's lock only, so they must not overlap any site's publish or
        preview dir (the same rule ``load_sites`` applies between sites)."""
        mine = [("publish_dir", self.publish_dir)]
        if self.preview_dir is not None:
            mine.append(("preview_dir", self.preview_dir))
        for site in sites.values():
            theirs = [("publish_dir", site.publish_dir)]
            if site.preview_dir is not None:
                theirs.append(("preview_dir", site.preview_dir))
            for my_name, my_dir in mine:
                for their_name, their_dir in theirs:
                    a, b = my_dir.resolve(), their_dir.resolve()
                    if a == b or a.is_relative_to(b) or b.is_relative_to(a):
                        raise ConfigError(
                            f"launch {my_name} {my_dir} overlaps site '{site.id}' "
                            f"{their_name} {their_dir}"
                        )

    def check_paths(self, studio_root: Path) -> None:
        """The preview dir is rewritten on every publish, so — like a site's —
        it must sit OUTSIDE the studio root and must not contain it."""
        if self.preview_dir is None:
            return
        preview, root = self.preview_dir.resolve(), studio_root.resolve()
        if preview == root or preview.is_relative_to(root) or root.is_relative_to(preview):
            raise ValueError(
                f"preview_dir {preview} must be outside the studio root {root} and not contain it"
            )


class LaunchProduct(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    slug: str = Field(pattern=SLUG_PATTERN)
    title: str = Field(min_length=1)
    tagline: str = Field(min_length=1)
    description: str = Field(min_length=1)
    repos: list[str] = Field(min_length=1)
    homepage: str | None = None
    record: RecordSource = Field(default_factory=RecordSource)
    capture: CaptureConfig | None = None
    # Phase 2 decoration: a brief whose targets are slots on `assets_site`.
    brief: Path | None = None
    assets_site: str | None = None
    publish: PublishTarget
    # Target film length; the walkthrough absorbs whatever the fixed segments leave.
    video_seconds: float = Field(default=60.0, gt=3.0, le=180.0)

    @model_validator(mode="after")
    def _repos(self) -> LaunchProduct:
        for repo in self.repos:
            owner, sep, name = repo.partition("/")
            if not sep or not owner or not name or "/" in name:
                raise ValueError(f"repo '{repo}' must be owner/name")
        if self.brief is not None and self.assets_site is None:
            raise ValueError("a product with a brief must name its assets_site")
        return self

    def record_url(self) -> str:
        return self.record.public_url or public_record_url(self.slug, self.record.web_base)


def _resolve(raw: dict[str, object], key: str, base: Path) -> None:
    value = raw.get(key)
    if isinstance(value, str) and not Path(value).is_absolute():
        raw[key] = str((base / value).resolve())


def load_product(path: Path, root: Path | None = None) -> LaunchProduct:
    """Load ``products/<slug>.yaml``. ``root`` is the studio root (default:
    ``path.parent.parent``, the standard ``<root>/products`` layout); the
    preview dir's limits are checked against it."""
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as e:
        raise ConfigError(f"cannot read {path}: {e}") from e
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: expected a mapping")
    base = path.parent
    _resolve(raw, "brief", base)
    for section, keys in (
        ("capture", ("script", "serve_dir")),
        ("publish", ("publish_dir", "preview_dir")),
    ):
        sub = raw.get(section)
        if isinstance(sub, dict):
            for key in keys:
                _resolve(sub, key, base)
    try:
        product = LaunchProduct.model_validate(raw)
        product.publish.check_paths(root if root is not None else path.resolve().parent.parent)
    except ValueError as e:
        raise ConfigError(f"{path}: {e}") from e
    return product
