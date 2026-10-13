"""The social-post set: one text file per network, factual, generated from the
build record. A post states only KNOWN facts (an unknown is left out, never
rounded to 0) and always links the public build record, so every claim in it
can be checked."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from qontinui_studio.launch.facts import Fact
from qontinui_studio.launch.product import LaunchProduct


@dataclass(frozen=True)
class Network:
    name: str
    limit: int


NETWORKS: tuple[Network, ...] = (
    Network("x", 280),
    Network("bluesky", 300),
    Network("mastodon", 500),
    Network("linkedin", 3000),
)

_HEADLINE_KEYS = ("work_units_shipped", "prs_landed", "ci_attempts", "wall_clock", "red_ci_fixed")


def _phrase(fact: Fact) -> str:
    value = fact.display().replace("≥ ", "at least ")
    return f"{fact.label}: {value}"


def compose(
    network: Network, product: LaunchProduct, facts: dict[str, Fact], record_url: str | None
) -> str:
    """The most factual post that fits ``network.limit`` characters: facts are
    kept before the homepage link, and the build-record link is never dropped."""
    known = [facts[k] for k in _HEADLINE_KEYS if k in facts and facts[k].known]
    head = f"{product.title}: {product.tagline}"
    if record_url:
        record_line = (
            f"How it was built, every number from its build record: {record_url}"
            if network.limit >= 1000
            else f"Build record: {record_url}"
        )
    else:
        record_line = ""
    for n in range(len(known), -1, -1):
        body = ""
        if n:
            body = (
                "Built by the Qontinui software factory. "
                + "; ".join(_phrase(f) for f in known[:n])
                + "."
            )
        if network.limit >= 1000:
            body = (body + "\n\n" + product.description).strip()
        for homepage in (product.homepage, None):
            tail = "\n".join(p for p in (record_line, homepage) if p)
            text = "\n\n".join(p for p in (head, body, tail) if p)
            if len(text) <= network.limit:
                return text
    # Even the bare post is too long: shorten the tagline, keep the record link.
    room = network.limit - len(record_line) - len(product.title) - 6
    short = product.tagline[: max(room, 0)].rstrip() + "…" if room > 1 else ""
    text = "\n\n".join(
        p for p in (f"{product.title}: {short}" if short else product.title, record_line) if p
    )
    return text[: network.limit]


def write_posts(
    out_dir: Path, product: LaunchProduct, facts: dict[str, Fact], record_url: str | None
) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for network in NETWORKS:
        path = out_dir / f"{network.name}.txt"
        path.write_text(compose(network, product, facts, record_url) + "\n", encoding="utf-8")
        paths.append(path)
    return paths
