"""sites/*.yaml — publication targets and their slots.

A site declares what KIND of site it is (the D6 rules key off it), whether it
is commercial (the D5 licence rule keys off it), and the exact pixel size and
content category of every slot a brief may target.
"""

from __future__ import annotations

import ipaddress
import re
import socket
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

import httpx
import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from qontinui_studio.errors import ConfigError

SiteKind = Literal["demo", "real_business", "product"]
# `food` / `premises` matter for D6 on a real business; `product_screenshot` is
# never AI (screenshots come from the running product via UI Bridge).
SlotCategory = Literal[
    "decorative", "illustration", "food", "premises", "people", "product_screenshot"
]
ExportFormat = Literal["webp", "avif", "png", "jpeg"]

ID_PATTERN = r"^[a-z0-9][a-z0-9_-]*$"
# GitHub ``owner/name``: coord refuses any other spelling of a repo for the
# image_generation dial (``repo_not_owner_name``).
REPO_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9._-]+$"

# The supervisor (:9875) and the runner (:9876) — dev tooling UIs, never a
# site preview. Refused on any host that resolves to this machine.
FORBIDDEN_LOCAL_PORTS = frozenset({9875, 9876})


def _local_addresses() -> set[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    """This machine's own interface addresses (best effort, no DNS lookups
    beyond the local host name)."""
    out: set[ipaddress.IPv4Address | ipaddress.IPv6Address] = set()
    try:
        name = socket.gethostname()
        for info in socket.getaddrinfo(name, None):
            out.add(ipaddress.ip_address(str(info[4][0]).split("%")[0]))
    except (OSError, ValueError):
        pass
    return out


def _host_address(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """``host`` as an IP address, accepting the IPv4 shorthands a browser or
    the OS accepts (``127.1``, ``2130706433``) and unwrapping IPv4-mapped IPv6."""
    try:
        addr: ipaddress.IPv4Address | ipaddress.IPv6Address = ipaddress.ip_address(host)
    except ValueError:
        try:
            addr = ipaddress.IPv4Address(socket.inet_aton(host))
        except OSError:
            return None
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        return addr.ipv4_mapped
    return addr


def _resolves_local(host: str) -> bool | None:
    """Whether ANY address ``host`` resolves to is this machine's; None when it
    does not resolve at all (UNKNOWN)."""
    try:
        infos = socket.getaddrinfo(host, None)
    except (OSError, UnicodeError):
        return None
    for info in infos:
        addr = _host_address(str(info[4][0]).split("%")[0])
        if addr is not None and (
            addr.is_loopback or addr.is_unspecified or addr in _local_addresses()
        ):
            return True
    return False


def forbidden_bridge_reason(url: str) -> str | None:
    """Why ``url`` may not be used as a D7 UI Bridge, or None.

    The host is taken exactly as httpx — which makes the request — will see it
    (IDNA-normalised, e.g. ``127。0。0。1`` -> ``127.0.0.1``). On the runner's
    and supervisor's ports (9875/9876) the host is refused if it is local in
    any spelling, if ANY address it resolves to is local, or if it does not
    resolve at all (UNKNOWN refuses)."""
    try:
        parsed = httpx.URL(url)
    except (httpx.InvalidURL, ValueError) as e:
        return f"ui_bridge_url is not a valid URL ({e})"
    host, port = parsed.host, parsed.port
    if parsed.scheme not in ("http", "https") or not host:
        return f"ui_bridge_url must be an http(s) URL, got '{url}'"
    if port not in FORBIDDEN_LOCAL_PORTS:
        return None
    local = True if is_local_host(host) else _resolves_local(host.rstrip("."))
    if local is None:
        return (
            f"ui_bridge_url host {host!r} on port {port} does not resolve, so it cannot be "
            "shown not to be the runner/supervisor's own UI; refused"
        )
    if local:
        return (
            f"ui_bridge_url points at the runner/supervisor's own UI (port {port} on this "
            "machine); D7 must check the site's PREVIEW page, through the preview's bridge"
        )
    return None


def _origin(url: str) -> tuple[str, str, int | None]:
    """(scheme, host, port) with the default port dropped."""
    p = urlparse(url)
    scheme = p.scheme.lower()
    port = p.port
    if port == {"http": 80, "https": 443}.get(scheme):
        port = None
    return scheme, (p.hostname or "").lower(), port


def is_local_host(host: str) -> bool:
    """Whether ``host`` names this machine: localhost names, loopback or
    unspecified addresses (any spelling), or one of this machine's interface
    addresses."""
    host = host.rstrip(".").lower()
    if host in ("localhost", socket.gethostname().lower()) or host.endswith(".localhost"):
        return True
    addr = _host_address(host)
    if addr is None:
        return False
    return addr.is_loopback or addr.is_unspecified or addr in _local_addresses()


DEFAULT_ILLUSTRATIVE_LABEL = "Illustrative image (AI-generated)"
DEFAULT_AI_LABEL = "AI-generated image"


def _default_formats() -> list[ExportFormat]:
    return ["webp"]


class Slot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    width: int = Field(gt=0)
    height: int = Field(gt=0)
    # Required. A Site fills ``decorative`` for an omitted category on demo and
    # product sites; a real business must state every slot's category, because
    # D6 refuses AI food/premises imagery there and a silent default would
    # disable that check.
    category: SlotCategory
    formats: list[ExportFormat] = Field(default_factory=_default_formats, min_length=1)
    remove_background: bool = False
    # False for a slot that is never an <img> on the page (meta only, e.g. an
    # og:image): D7 then verifies it by fetching it from the preview server.
    rendered: bool = True
    focal_x: float = Field(default=0.5, ge=0.0, le=1.0)
    focal_y: float = Field(default=0.5, ge=0.0, le=1.0)


class Site(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=ID_PATTERN)
    name: str
    kind: SiteKind
    commercial: bool
    publish_dir: Path
    public_url: str | None = None
    # The repository this site's assets ship into, as GitHub ``owner/name``.
    # Studio names it when it reads coord's ``image_generation`` dial
    # (``GET /coord/fleet-policy?domain=image_generation&repo=<repo>``), so a
    # per-repo override governs generation for this site. Unset reads the
    # tenant-wide level (plan 2026-10-13 Phase 12 step 3).
    repo: str | None = Field(default=None, pattern=REPO_PATTERN)
    # The label a demo/template site must show beside AI imagery (D6). On other
    # site kinds AI imagery still carries ``ai_label`` (EU synthetic-content
    # transparency).
    illustrative_label: str = DEFAULT_ILLUSTRATIVE_LABEL
    ai_label: str = DEFAULT_AI_LABEL
    # D7 runs against a PREVIEW of this site, before the live swap. The studio
    # writes the site's live files plus the batch's files and merged manifest
    # into ``preview_dir``; a preview server serves that directory at
    # ``preview_url`` (the page) and ``preview_manifest_url`` (the manifest).
    # The page runs the UI Bridge React auto-registration provider with media
    # discovery (an injected/seeded bridge registers no media, which D7
    # refuses); ``ui_bridge_url`` is the prefix its ``/control/*`` routes hang
    # off. It must be the PREVIEW's bridge — never the runner's or the
    # supervisor's own UI (ports 9876/9875 on this machine), which is refused.
    # D7 is REQUIRED with no waiver: a site without all four is never published to.
    ui_bridge_url: str | None = None
    preview_url: str | None = None
    preview_dir: Path | None = None
    preview_manifest_url: str | None = None
    slots: dict[str, Slot] = Field(min_length=1)

    @model_validator(mode="before")
    @classmethod
    def _slot_categories(cls, data: Any) -> Any:
        if not isinstance(data, dict) or not isinstance(data.get("slots"), dict):
            return data
        kind = data.get("kind")
        slots: dict[str, Any] = {}
        for name, slot in data["slots"].items():
            if isinstance(slot, dict) and "category" not in slot:
                if kind == "real_business":
                    raise ValueError(
                        f"slot '{name}': a real_business site must state every slot's category"
                    )
                slot = {**slot, "category": "decorative"}
            slots[name] = slot
        return {**data, "slots": slots}

    @model_validator(mode="after")
    def _checks(self) -> Site:
        for name in self.slots:
            if not re.fullmatch(ID_PATTERN, name):
                raise ValueError(f"bad slot name '{name}' (must match {ID_PATTERN})")
        preview_fields = {
            "ui_bridge_url": self.ui_bridge_url,
            "preview_url": self.preview_url,
            "preview_dir": self.preview_dir,
            "preview_manifest_url": self.preview_manifest_url,
        }
        missing = [k for k, v in preview_fields.items() if not v]
        if 0 < len(missing) < len(preview_fields):
            raise ValueError(
                f"a D7 preview needs all of ui_bridge_url, preview_url, preview_dir and "
                f"preview_manifest_url; missing {missing}"
            )
        if self.ui_bridge_url:
            bridge = urlparse(self.ui_bridge_url)
            if bridge.scheme not in ("http", "https") or not bridge.hostname:
                raise ValueError(
                    f"ui_bridge_url must be an http(s) URL, got '{self.ui_bridge_url}'"
                )
            why = forbidden_bridge_reason(self.ui_bridge_url)
            if why is not None:
                raise ValueError(why)
        if self.preview_url and urlparse(self.preview_url).scheme not in ("http", "https"):
            raise ValueError(f"preview_url must be an http(s) URL, got '{self.preview_url}'")
        if (
            self.preview_url
            and self.preview_manifest_url
            and _origin(self.preview_url) != _origin(self.preview_manifest_url)
        ):
            raise ValueError(
                "preview_manifest_url must be on the preview_url's origin: the slot files "
                "are served beside the manifest, and D7 requires the page to show them "
                f"from there (got {self.preview_manifest_url} for {self.preview_url})"
            )
        for field_name in ("ui_bridge_url", "preview_url", "preview_manifest_url"):
            value = getattr(self, field_name)
            if value and "#" in value:
                # A hash-routed URL puts D7's batch/attempt binding inside the
                # fragment: navigation becomes a same-document change that keeps
                # the old DOM, and the query D7 checks is empty.
                raise ValueError(
                    f"{field_name} must not contain a #fragment (got '{value}'); D7 binds "
                    "the preview to a batch through the real query string"
                )
        if self.preview_dir is not None:
            preview, live = self.preview_dir.resolve(), self.publish_dir.resolve()
            if preview == live or preview.is_relative_to(live) or live.is_relative_to(preview):
                raise ValueError(
                    "preview_dir must be neither publish_dir nor inside it nor a parent of it"
                )
        return self

    def check_paths(self, studio_root: Path) -> None:
        """The preview dir is rewritten on every publish, so it must sit OUTSIDE
        the studio root and must not contain it."""
        if self.preview_dir is None:
            return
        preview, root = self.preview_dir.resolve(), studio_root.resolve()
        if preview == root or preview.is_relative_to(root) or root.is_relative_to(preview):
            raise ValueError(
                f"preview_dir {preview} must be outside the studio root {root} and not contain it"
            )

    def label_for_ai(self) -> str:
        return self.illustrative_label if self.kind == "demo" else self.ai_label


def load_site(path: Path) -> Site:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as e:
        raise ConfigError(f"cannot read {path}: {e}") from e
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: expected a mapping")
    for key in ("publish_dir", "preview_dir"):
        value = raw.get(key)
        if isinstance(value, str) and not Path(value).is_absolute():
            raw[key] = str((path.parent / value).resolve())
    try:
        return Site.model_validate(raw)
    except ValueError as e:
        raise ConfigError(f"{path}: {e}") from e


def load_sites(directory: Path, root: Path | None = None) -> dict[str, Site]:
    """Load every ``*.yaml`` site under ``directory``. ``root`` is the studio
    root (default: ``directory.parent``, the standard ``<root>/sites`` layout)."""
    root = root if root is not None else directory.parent
    sites: dict[str, Site] = {}
    if not directory.is_dir():
        return sites
    for path in sorted(directory.glob("*.yaml")):
        site = load_site(path)
        try:
            site.check_paths(root)
        except ValueError as e:
            raise ConfigError(f"{path}: {e}") from e
        if site.id in sites:
            raise ConfigError(f"duplicate site id '{site.id}' ({path})")
        sites[site.id] = site
    _check_cross_site(sites)
    return sites


def _overlaps(a: Path, b: Path) -> bool:
    a, b = a.resolve(), b.resolve()
    return a == b or a.is_relative_to(b) or b.is_relative_to(a)


def _check_cross_site(sites: dict[str, Site]) -> None:
    """A preview dir is rewritten on every publish of its site, under that
    site's lock only, so it must not overlap ANY site's publish dir or another
    site's preview dir; publish dirs must not overlap each other either."""
    items = sorted(sites.items())
    for i, (a_id, a) in enumerate(items):
        for b_id, b in items[i + 1 :]:
            if _overlaps(a.publish_dir, b.publish_dir):
                raise ConfigError(f"sites '{a_id}' and '{b_id}' have overlapping publish_dir")
        if a.preview_dir is None:
            continue
        for b_id, b in items:
            if _overlaps(a.preview_dir, b.publish_dir):
                raise ConfigError(
                    f"site '{a_id}' preview_dir overlaps site '{b_id}' publish_dir "
                    f"({a.preview_dir} / {b.publish_dir})"
                )
            if (
                b_id != a_id
                and b.preview_dir is not None
                and _overlaps(a.preview_dir, b.preview_dir)
            ):
                raise ConfigError(f"sites '{a_id}' and '{b_id}' have overlapping preview_dir")
