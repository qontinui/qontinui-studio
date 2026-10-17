"""Where a studio's config and state live, relative to one root directory.

``<root>/providers.yaml``, ``<root>/sites/*.yaml``, ``<root>/briefs/*.yaml`` and
``<root>/products/*.yaml``
are tracked config. ``<root>/.studio/`` is local state (gitignored): the SQLite
file, raw downloads, processed variants, notices, the review page and
assembled launch bundles.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Workspace:
    root: Path

    @property
    def providers_path(self) -> Path:
        return self.root / "providers.yaml"

    @property
    def sites_dir(self) -> Path:
        return self.root / "sites"

    @property
    def state_dir(self) -> Path:
        return self.root / ".studio"

    @property
    def db_path(self) -> Path:
        return self.state_dir / "studio.db"

    @property
    def staging_dir(self) -> Path:
        return self.state_dir / "staging"

    @property
    def raw_dir(self) -> Path:
        return self.state_dir / "raw"

    @property
    def variants_dir(self) -> Path:
        return self.state_dir / "variants"

    @property
    def notices_dir(self) -> Path:
        return self.state_dir / "notices"

    @property
    def handoffs_dir(self) -> Path:
        """Operator hand-off latches, one file per provider (ui_bridge_wrapper)."""
        return self.state_dir / "handoffs"

    @property
    def review_dir(self) -> Path:
        return self.state_dir / "review"

    @property
    def products_dir(self) -> Path:
        return self.root / "products"

    @property
    def launches_dir(self) -> Path:
        return self.state_dir / "launches"
