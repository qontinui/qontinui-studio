"""Operator notices (D7: the operator is notified, not asked).

``FileNotifier`` always works. ``CoordFindingNotifier`` posts the notice to
coord's agent findings door (``POST /coord/agent-findings`` — the write door;
``/coord/findings`` is the operator READ route) when a bearer is configured, and
falls back to the file notice — saying why inside it — when it is not or when
the post fails. A notice is never silently dropped.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

import httpx

COORD_BASE = "https://coord.qontinui.io"
FINDINGS_PATH = "/coord/agent-findings"
BEARER_ENV = "STUDIO_COORD_BEARER"
BEARER_FILE_ENV = "STUDIO_COORD_BEARER_FILE"


@dataclass(frozen=True)
class Notice:
    batch_id: str
    outcome: str  # "published" | "refused" | "unpublished"
    title: str
    body: str
    urls: list[str] = field(default_factory=list)
    unpublish_command: str | None = None


@dataclass(frozen=True)
class Delivery:
    channel: str  # "file" | "coord"
    location: str
    note: str | None = None


class Notifier(Protocol):
    def notify(self, notice: Notice) -> Delivery: ...


def render_markdown(notice: Notice) -> str:
    lines = [
        f"# {notice.title}",
        "",
        f"- batch: `{notice.batch_id}`",
        f"- outcome: {notice.outcome}",
    ]
    for url in notice.urls:
        lines.append(f"- url: {url}")
    if notice.unpublish_command:
        lines += ["", "Unpublish in one step:", "", "```", notice.unpublish_command, "```"]
    lines += ["", notice.body, ""]
    return "\n".join(lines)


class FileNotifier:
    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def notify(self, notice: Notice, note: str | None = None) -> Delivery:
        """Write ``<batch>-<outcome>-<UTC timestamp>.md`` (+ ``.json``). Never
        overwrites an earlier notice: a batch published, unpublished and
        published again leaves three notices."""
        self.directory.mkdir(parents=True, exist_ok=True)
        md = render_markdown(notice)
        if note:
            md += f"\n> Delivery note: {note}\n"
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        base = f"{notice.batch_id}-{notice.outcome}-{stamp}"
        n = 0
        while True:
            stem = self.directory / (base if n == 0 else f"{base}-{n}")
            try:
                with stem.with_suffix(".md").open("x", encoding="utf-8") as f:
                    f.write(md)
                break
            except FileExistsError:
                n += 1
        payload = {**asdict(notice), "delivery_note": note}
        stem.with_suffix(".json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return Delivery(channel="file", location=str(stem.with_suffix(".md")), note=note)


def resolve_bearer() -> str | None:
    token = os.environ.get(BEARER_ENV)
    if token:
        return token.strip()
    path = os.environ.get(BEARER_FILE_ENV)
    if path and Path(path).is_file():
        return Path(path).read_text(encoding="utf-8").strip() or None
    return None


class CoordFindingNotifier:
    def __init__(
        self,
        fallback: FileNotifier,
        bearer: str | None = None,
        base_url: str = COORD_BASE,
        http: httpx.Client | None = None,
    ) -> None:
        self.fallback = fallback
        self.bearer = bearer if bearer is not None else resolve_bearer()
        self.base_url = base_url.rstrip("/")
        self.http = http or httpx.Client(timeout=20.0)

    def notify(self, notice: Notice) -> Delivery:
        if not self.bearer:
            return self.fallback.notify(
                notice, note=f"no coord bearer configured ({BEARER_ENV} / {BEARER_FILE_ENV})"
            )
        body = {
            "title": notice.title[:200],
            "body": render_markdown(notice),
            "topic": "studio-publication",
            "resource_keys": [f"qontinui-studio:batch:{notice.batch_id}"],
            "artifact_refs": {
                "batch_id": notice.batch_id,
                "outcome": notice.outcome,
                "urls": notice.urls,
                "unpublish_command": notice.unpublish_command,
            },
        }
        try:
            resp = self.http.post(
                f"{self.base_url}{FINDINGS_PATH}",
                json=body,
                headers={"Authorization": f"Bearer {self.bearer}"},
            )
        except httpx.HTTPError as e:
            return self.fallback.notify(notice, note=f"coord post failed: {e}")
        if resp.status_code >= 300:
            return self.fallback.notify(
                notice, note=f"coord answered HTTP {resp.status_code}: {resp.text[:300]}"
            )
        finding_id = ""
        try:
            parsed = resp.json()
            if isinstance(parsed, dict):
                finding_id = str(parsed.get("id") or parsed.get("finding_id") or "")
        except ValueError:
            pass
        # Keep a local copy too: the review page and `studio unpublish` read it.
        self.fallback.notify(
            notice, note=f"also posted to coord finding {finding_id or '(id not returned)'}"
        )
        return Delivery(channel="coord", location=f"{self.base_url}{FINDINGS_PATH}#{finding_id}")
