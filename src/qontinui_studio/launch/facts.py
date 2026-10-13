"""Numbers derived from a build record — the ONLY numbers a launch bundle shows.

Every :class:`Fact` names the document field(s) it was computed from
(``source``), so a reader of any page, slide, image or post can trace it back.
A value the record leaves null is a :class:`Fact` with ``known=False`` and the
record's own reason; renderers print ``unknown — <reason>`` and never 0.
A count over a list carries the record's own reason codes for that list: a
failed reader is unknown, `not_established` on a non-empty list a lower bound.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from qontinui_studio.launch.record import BuildRecord, reason_text


@dataclass(frozen=True)
class Fact:
    key: str
    label: str
    source: str
    value: int | None
    unknown_reason: str | None = None
    lower_bound: bool = False
    unit: str = ""

    @property
    def known(self) -> bool:
        return self.value is not None

    def display(self) -> str:
        """The rendered value: a number (``≥`` when a lower bound), a duration,
        or ``unknown — <reason>``."""
        if self.value is None:
            return f"unknown — {self.unknown_reason}"
        text = format_duration(self.value) if self.unit == "seconds" else str(self.value)
        return f"≥ {text}" if self.lower_bound else text

    def short(self) -> str:
        """The value for space-constrained media (slides, images): no reason text."""
        if self.value is None:
            return "unknown"
        return self.display()


def format_duration(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    minutes, _ = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    days, hours = divmod(hours, 24)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def _known(
    key: str, label: str, source: str, value: int, lower: bool = False, unit: str = ""
) -> Fact:
    return Fact(key, label, source, value, None, lower, unit)


def _maybe(
    record: BuildRecord, key: str, label: str, source: str, value: int | None, unit: str = ""
) -> Fact:
    if value is None:
        return Fact(key, label, source, None, record.unknown_reason(source), unit=unit)
    return _known(key, label, source, value, unit=unit)


def _list_count(
    record: BuildRecord,
    key: str,
    label: str,
    list_path: str,
    source: str,
    count: int,
    field_path: str | None = None,
) -> Fact:
    """A count over a list. Its knownness comes from the record's own codes
    for the list (:meth:`BuildRecord.list_status`): a failed reader is
    UNKNOWN (never 0), ``not_established`` on a non-empty list is a lower
    bound. A count of a field (``shipped_at``) is also a lower bound when the
    record says that field could not be established for some items."""
    status = record.list_status(list_path)
    if status.state == "unknown":
        assert status.code is not None
        return Fact(key, label, source, None, reason_text(status.code))
    lower = status.state == "lower_bound" or bool(field_path and record.unknown_codes(field_path))
    return _known(key, label, source, count, lower)


def _sum_known(
    record: BuildRecord, key: str, label: str, source: str, values: list[int | None], unit: str = ""
) -> Fact:
    """A sum over PRs is known only when the PR list is and every addend is;
    otherwise UNKNOWN with the record's reason (a partial sum would read as a
    total). A sum over a lower-bound list is itself a lower bound."""
    status = record.list_status("prs")
    if status.state == "unknown":
        assert status.code is not None
        return Fact(key, label, source, None, reason_text(status.code), unit=unit)
    if any(v is None for v in values):
        missing = sum(v is None for v in values)
        reason = f"{missing} of {len(values)} PR(s) lack it: {record.unknown_reason(source)}"
        return Fact(key, label, source, None, reason, unit=unit)
    total = sum(v for v in values if v is not None)
    return _known(key, label, source, total, lower=status.state == "lower_bound", unit=unit)


def derive_facts(record: BuildRecord) -> list[Fact]:
    """The headline facts, in display order."""
    prs = record.prs
    shipped = [u for u in record.work_units if u.shipped_at is not None]
    landed = [p for p in prs if p.landed_at is not None]
    facts = [
        _list_count(
            record,
            "work_units",
            "Plans (work units)",
            "work_units",
            "work_units",
            len(record.work_units),
        ),
        _list_count(
            record,
            "work_units_shipped",
            "Plans shipped",
            "work_units",
            "work_units[].shipped_at",
            len(shipped),
            "work_units[].shipped_at",
        ),
        _list_count(record, "prs", "Pull requests", "prs", "prs", len(prs)),
        _list_count(
            record,
            "prs_landed",
            "Pull requests landed",
            "prs",
            "prs[].landed_at",
            len(landed),
            "prs[].landed_at",
        ),
        _sum_known(record, "ci_attempts", "CI runs", "prs[].attempts", [p.attempts for p in prs]),
        _sum_known(
            record,
            "ci_secs",
            "Time in CI",
            "prs[].ci_duration_secs",
            [p.ci_duration_secs for p in prs],
            unit="seconds",
        ),
        _maybe(
            record,
            "wall_clock",
            "Wall-clock time",
            "wall_clock_secs",
            record.wall_clock_secs,
            "seconds",
        ),
        _maybe(
            record,
            "gates_registered",
            "Gates registered",
            "gates.registered",
            record.gates.registered,
        ),
        _maybe(record, "gates_cleared", "Gates cleared", "gates.cleared", record.gates.cleared),
        _maybe(record, "sessions", "Agent sessions", "sessions.count", record.sessions.count),
        _maybe(
            record,
            "red_ci_fixed",
            "Red CI fixed by the factory",
            "self_corrections.red_ci_fixed",
            record.self_corrections.red_ci_fixed,
        ),
        _maybe(
            record,
            "review_findings_fixed",
            "Review findings fixed",
            "self_corrections.review_findings_fixed",
            record.self_corrections.review_findings_fixed,
        ),
        _maybe(
            record,
            "gate_reopens",
            "Gate re-opens",
            "self_corrections.gate_reopens",
            record.self_corrections.gate_reopens,
        ),
    ]
    return facts


def facts_by_key(record: BuildRecord) -> dict[str, Fact]:
    return {f.key: f for f in derive_facts(record)}


def parse_time(value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
