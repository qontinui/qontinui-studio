"""The fourth independent review of the launch kit (253fd01), as regression
tests (scratchpad launch-rev4/scripts/pubtext.py, review_imgs.py)."""

from __future__ import annotations

import datetime
import re
from collections.abc import Callable
from pathlib import Path

import pytest
from PIL import Image

from qontinui_studio.launch.allowlist import build_record_violations
from qontinui_studio.launch.kit import KitOptions, run_kit
from qontinui_studio.launch.publish import gate_public_text
from qontinui_studio.pipeline import Context

from .launch_support import (
    document,
    preview_ctx,
    public_client,
    use_public_door,
    write_product,
)


@pytest.fixture(autouse=True)
def _public_door(monkeypatch: pytest.MonkeyPatch) -> None:
    use_public_door(monkeypatch)


@pytest.mark.parametrize(
    "title",
    [
        "fix: bind the runner API to 127.0.0.1 only",
        "fix: never probe localhost, IPv6 first",
        "feat: support C:\\Users paths on Windows",
    ],
)
def test_rev4_record_strings_web_accepts_are_published(
    make_context: Callable[..., Context], studio_root: Path, title: str
) -> None:
    """Finding 1 (pubtext.py): the record is judged by the D3 validator; the
    local-leak gate must not refuse what the factory's rules accept."""
    doc = document()
    doc["prs"][0]["title"] = title
    assert build_record_violations(doc) == []
    ctx = make_context(**preview_ctx(studio_root))
    product_path = write_product(studio_root, capture=False)
    run = run_kit(ctx, product_path, KitOptions(video=False), public_client(doc), publish=True)
    assert run.publish is not None and run.publish.published, run.publish.failures
    page = (studio_root / "published-launch" / "how-it-was-built.html").read_text()
    assert "fix:" in page or "feat:" in page


def test_rev4_a_home_path_in_a_local_input_is_still_refused(
    make_context: Callable[..., Context], studio_root: Path
) -> None:
    """Text the kit composes from local inputs (here the product description)
    is still scanned; a page edited outside the renderer is refused too."""
    ctx = make_context(**preview_ctx(studio_root))
    product_path = write_product(studio_root, capture=False)
    product_path.write_text(
        product_path.read_text().replace(
            "description: A test product description.",
            f"description: 'Built in {Path.home()}/projects/demo.'",
        )
    )
    run = run_kit(ctx, product_path, KitOptions(video=False), public_client())
    out = run.bundle.out_dir
    result = gate_public_text(out, run.bundle.manifest, run.product)
    assert result.failed and "index.html: contains the operator's home directory" in result.detail

    clean = write_product(studio_root, capture=False)
    ctx.clock.sleep(1)
    run = run_kit(ctx, clean, KitOptions(video=False), public_client())
    index = run.bundle.out_dir / "index.html"
    index.write_text(index.read_text().replace("</main>", "<p>edited</p></main>"))
    result = gate_public_text(run.bundle.out_dir, run.bundle.manifest, run.product)
    assert result.failed and "index.html: is not the kit's render" in result.detail


def test_rev4_record_strings_are_masked_only_where_they_are_the_records(
    make_context: Callable[..., Context], studio_root: Path
) -> None:
    """A record string is masked; the same leak in a post (composed by the
    kit) is not."""
    doc = document()
    doc["prs"][0]["title"] = "fix: bind to 127.0.0.1"
    ctx = make_context(**preview_ctx(studio_root))
    run = run_kit(
        ctx, write_product(studio_root, capture=False), KitOptions(video=False), public_client(doc)
    )
    out = run.bundle.out_dir
    assert gate_public_text(out, run.bundle.manifest, run.product).outcome == "pass"
    (out / "posts" / "x.txt").write_text("we now listen on http://127.0.0.1:8080\n")
    result = gate_public_text(out, run.bundle.manifest, run.product)
    assert result.failed and "posts/x.txt: names a machine-internal host" in result.detail


@pytest.mark.local_http
def test_rev4_public_review_page_images_resolve_under_the_capture_server(
    tmp_path: Path,
) -> None:
    """Finding 2 (review_imgs.py): the self-launch serves .studio/review; every
    image on the public review page must be served from there and decode."""
    import httpx

    from qontinui_studio.launch.capture import serve_directory
    from qontinui_studio.review import build_review_page
    from qontinui_studio.store import connect
    from qontinui_studio.workspace import Workspace

    ws = Workspace(tmp_path)
    ws.variants_dir.mkdir(parents=True)
    img = ws.variants_dir / "hero.png"
    Image.new("RGB", (32, 32), (200, 0, 0)).save(img)
    conn = connect(ws.db_path)
    t = "2026-10-09T00:00:00"
    conn.execute(
        "INSERT INTO batches (id,brief_id,brief_path,brief_sha256,targets_json,created_at,"
        "status,no_publish) VALUES ('b1','x',?,?,?,?,'published',0)",
        (str(tmp_path / "briefs/x.yaml"), "0" * 64, "[]", t),
    )
    conn.execute(
        "INSERT INTO jobs (id,batch_id,variant_index,status,next_attempt_at,updated_at) "
        "VALUES ('j1','b1',0,'ingested',?,?)",
        (t, t),
    )
    conn.execute(
        "INSERT INTO assets (uid,asset_id,brief_id,batch_id,job_id,provider,provider_name,model,"
        "prompt,created_at,raw_path,sha256,width,height,licence_commercial_use,"
        "automation_allowed,watermark) VALUES ('a1','hero','x','b1','j1','fake','Fake','m','p',"
        "?,?,?,32,32,'yes','yes','none')",
        (t, str(img), "0" * 64),
    )
    conn.execute(
        "INSERT INTO variants (id,asset_uid,site,slot,format,path,width,height,sha256,created_at) "
        "VALUES ('v1','a1','s','hero','png',?,32,32,?,?)",
        (str(img), "0" * 64, t),
    )
    conn.commit()
    page = build_review_page(
        conn, ws.review_dir, datetime.datetime(2026, 10, 9), root=tmp_path, public=True
    )
    srcs = re.findall(r"<img src='([^']*)'", page.read_text())
    assert srcs and all(not s.startswith("..") for s in srcs)
    import io
    from urllib.parse import urljoin

    with serve_directory(ws.review_dir) as base, httpx.Client() as client:
        for src in srcs:
            resp = client.get(urljoin(base, src))
            assert resp.status_code == 200, urljoin(base, src)
            with Image.open(io.BytesIO(resp.content)) as decoded:
                decoded.load()


# ---- fifth review: masking by position, never by content -------------------------------------


@pytest.mark.parametrize(
    ("homepage", "description"),
    [
        (
            "http://qontinui-studio.internal:8080/",
            "Try it at http://qontinui-studio.internal:8080/admin",
        ),
        ("http://qontinui-studio.lan/", "dev box: qontinui-studio.corp"),
        ("http://studio-dev.internal:8080/", "Try it at http://studio-dev.internal:8080/admin"),
    ],
    ids=["slug-host-internal", "slug-host-lan", "other-host"],
)
def test_rev5_a_record_string_cannot_hide_a_leak_in_kit_text(
    make_context: Callable[..., Context], studio_root: Path, homepage: str, description: str
) -> None:
    """mask_smuggle.py: the product slug is also a record string; masking by
    content would have hidden ``qontinui-studio.internal``."""
    ctx = make_context(**preview_ctx(studio_root))
    product_path = write_product(studio_root, capture=False)
    product_path.write_text(
        product_path.read_text()
        .replace("homepage: https://github.com/qontinui/qontinui-studio", f"homepage: '{homepage}'")
        .replace("description: A test product description.", f"description: '{description}'")
    )
    run = run_kit(ctx, product_path, KitOptions(video=False), public_client(), publish=True)
    assert run.publish is not None and not run.publish.published
    gate = next(g for g in run.publish.failures if g.gate == "public_text")
    assert "names a machine-internal host" in gate.detail


@pytest.mark.parametrize(
    ("title", "description", "expected"),
    [
        ("localhost", "Try it at http://localhost:3001/admin", "names a machine-internal host"),
        ("tmp", "Logs: /tmp/qontinui-studio/run.log", "contains an absolute local path"),
        (
            "feat: launch kit",
            "Try it at http://localhost:3001/admin",
            "names a machine-internal host",
        ),
    ],
    ids=["localhost-title", "tmp-title", "control"],
)
def test_rev5_a_record_title_equal_to_the_leak_does_not_mask_it(
    make_context: Callable[..., Context],
    studio_root: Path,
    title: str,
    description: str,
    expected: str,
) -> None:
    """mask_title.py: a PR titled "localhost" must not hide the product
    description's localhost URL."""
    doc = document()
    doc["prs"][1]["title"] = title
    ctx = make_context(**preview_ctx(studio_root))
    product_path = write_product(studio_root, capture=False)
    product_path.write_text(
        product_path.read_text().replace(
            "description: A test product description.", f"description: '{description}'"
        )
    )
    run = run_kit(ctx, product_path, KitOptions(video=False), public_client(doc), publish=True)
    assert run.publish is not None and not run.publish.published
    gate = next(g for g in run.publish.failures if g.gate == "public_text")
    assert f"index.html: {expected}" in gate.detail


# ---- sixth review: leak patterns are case- and compatibility-insensitive ----------------------


@pytest.mark.parametrize(
    ("homepage", "description", "expected"),
    [
        (
            "http://localhost:3001/",
            "Try it at http://localhost:3001/admin",
            "machine-internal host",
        ),
        (
            "http://LOCALHOST:3001/",
            "Try it at http://LOCALHOST:3001/admin",
            "machine-internal host",
        ),
        (
            "http://Localhost:3001/",
            "Try it at http://Localhost:3001/admin",
            "machine-internal host",
        ),
        (
            "http://Studio-Dev.INTERNAL:8080/",
            "dev box: Studio-Dev.Internal",
            "machine-internal host",
        ),
        ("FILE:///srv/app/index.html", "Open FILE:///srv/app/index.html", "machine-internal host"),
        (
            "http://\\uff2c\\uff2f\\uff23\\uff21\\uff2c\\uff28\\uff2f\\uff33\\uff34:3001/",
            "A neutral description.",
            "machine-internal host",
        ),
        (
            "https://example.test/",
            "Dev box: http://\\uff4c\\uff4f\\uff43\\uff41\\uff4c\\uff48\\uff4f\\uff53\\uff54:3001/",
            "machine-internal host",
        ),
        ("https://example.test/", "Logs live in /HOME/someone/logs", "absolute local path"),
        ("https://example.test/", "Built under c:\\\\users\\\\joshua", "absolute local path"),
    ],
    ids=[
        "control",
        "upper-loopback",
        "title-loopback",
        "upper-zone",
        "upper-file",
        "fullwidth-homepage",
        "fullwidth-description",
        "upper-home-root",
        "lower-drive",
    ],
)
def test_rev6_leaks_are_found_in_any_case(
    make_context: Callable[..., Context],
    studio_root: Path,
    homepage: str,
    description: str,
    expected: str,
) -> None:
    """case_host.py: LOCALHOST / Localhost / *.INTERNAL / FILE:// passed."""
    ctx = make_context(**preview_ctx(studio_root))
    product_path = write_product(studio_root, capture=False)
    product_path.write_text(
        product_path.read_text()
        .replace("homepage: https://github.com/qontinui/qontinui-studio", f'homepage: "{homepage}"')
        .replace("description: A test product description.", f'description: "{description}"')
    )
    run = run_kit(ctx, product_path, KitOptions(video=False), public_client())
    result = gate_public_text(run.bundle.out_dir, run.bundle.manifest, run.product)
    assert result.failed, result.detail
    assert (
        f"index.html: names a {expected}" in result.detail
        or f"index.html: contains an {expected}" in result.detail
    ), result.detail


def test_rev6_the_home_directory_is_found_in_another_case(
    make_context: Callable[..., Context], studio_root: Path
) -> None:
    ctx = make_context(**preview_ctx(studio_root))
    product_path = write_product(studio_root, capture=False)
    product_path.write_text(
        product_path.read_text().replace(
            "description: A test product description.",
            f'description: "Built in {str(Path.home()).upper()}/x"',
        )
    )
    run = run_kit(ctx, product_path, KitOptions(video=False), public_client())
    result = gate_public_text(run.bundle.out_dir, run.bundle.manifest, run.product)
    assert result.failed and "contains the operator's home directory" in result.detail


# ---- seventh review: drive paths with '/', private and link-local IPs ---------------------------


@pytest.mark.parametrize(
    ("description", "expected"),
    [
        ("Logs: D:/qontinui-root/qontinui-studio/out/run.log", "contains an absolute local path"),
        ("Logs: d://share/run.log", "contains an absolute local path"),
        ("Try it at http://10.0.0.5:3001/admin", "names a machine-internal host"),
        ("Try it at http://172.20.4.1/", "names a machine-internal host"),
        ("Try it at http://192.168.1.20:3001/admin", "names a machine-internal host"),
        ("Try it at http://169.254.10.2/", "names a machine-internal host"),
        ("Try it at http://[0:0:0:0:0:0:0:1]:3001/", "names a machine-internal host"),
        ("Listening on ::1 port 3001", "names a machine-internal host"),
        ("Try it at http://[fe80::1%25eth0]/", "names a machine-internal host"),
        ("Try it at http://[fd12:3456::1]/", "names a machine-internal host"),
        ("Try it at http://[::ffff:127.0.0.1]/", "names a machine-internal host"),
        ("Try it at http://[::]:3001/", "names a machine-internal host"),
    ],
    ids=[
        "drive-fwd-slash",
        "drive-double-slash",
        "rfc1918-10",
        "rfc1918-172-16",
        "rfc1918-192-168",
        "link-local-v4",
        "ipv6-long-loopback",
        "ipv6-bare-loopback",
        "ipv6-link-local",
        "ipv6-ula",
        "ipv4-mapped-loopback",
        "ipv6-unspecified",
    ],
)
def test_rev7_drive_paths_and_internal_ips(
    make_context: Callable[..., Context], studio_root: Path, description: str, expected: str
) -> None:
    """gaps.py: one case per family."""
    ctx = make_context(**preview_ctx(studio_root))
    product_path = write_product(studio_root, capture=False)
    product_path.write_text(
        product_path.read_text().replace(
            "description: A test product description.", f'description: "{description}"'
        )
    )
    run = run_kit(ctx, product_path, KitOptions(video=False), public_client())
    result = gate_public_text(run.bundle.out_dir, run.bundle.manifest, run.product)
    assert result.failed and f"index.html: {expected}" in result.detail, result.detail


@pytest.mark.parametrize(
    "description",
    [
        "Served from 8.8.8.8 and 2001:4860:4860::8888.",
        "Version 1.2.3.4 shipped at 12:30:45; see https://example.test/a:b.",
        "CSS a::before and std::vector are fine.",
    ],
)
def test_rev7_public_addresses_and_lookalikes_pass(
    make_context: Callable[..., Context], studio_root: Path, description: str
) -> None:
    ctx = make_context(**preview_ctx(studio_root))
    product_path = write_product(studio_root, capture=False)
    product_path.write_text(
        product_path.read_text().replace(
            "description: A test product description.", f'description: "{description}"'
        )
    )
    run = run_kit(ctx, product_path, KitOptions(video=False), public_client())
    result = gate_public_text(run.bundle.out_dir, run.bundle.manifest, run.product)
    assert result.outcome == "pass", result.detail


# ---- eighth review: IP literals at sentence end, exact internal families -------------------------


def _gate_for(make_context: Callable[..., Context], studio_root: Path, description: str) -> object:
    ctx = make_context(**preview_ctx(studio_root))
    product_path = write_product(studio_root, capture=False)
    product_path.write_text(
        product_path.read_text().replace(
            "description: A test product description.", f'description: "{description}"'
        )
    )
    run = run_kit(ctx, product_path, KitOptions(video=False), public_client())
    return gate_public_text(run.bundle.out_dir, run.bundle.manifest, run.product)


@pytest.mark.parametrize(
    "description",
    [
        "The dev server listens on 127.0.0.1.",
        "Bind it to 0.0.0.0.",
        "Try http://10.0.0.5.",
        "Connection refused (192.168.1.20).",
        "Try 127.000.000.001 first",
        "addr:fe80::1",
        "ip:::1",
        "host=fe80::1",
        "listen on 127.1:8080",
        "mapped ::ffff:7f00:1",
    ],
)
def test_rev8_internal_ips_in_any_position_are_refused(
    make_context: Callable[..., Context], studio_root: Path, description: str
) -> None:
    """ipgate.py / ipprobe.py: sentence-end dots, leading zeros, a colon
    before a bare IPv6 literal, a short host:port form, IPv4-mapped hex."""
    result = _gate_for(make_context, studio_root, description)
    assert result.failed and "index.html: names a machine-internal host" in result.detail


@pytest.mark.parametrize(
    "description",
    [
        "netmask 255.255.255.0",
        "such as 203.0.113.7",
        "docs: 192.0.2.10",
        "e.g. 2001:db8::1",
        "CSS ::selection and p ::after",
        "map :: (a -> b)",
        "v0.4.1.2 released",
        "CGNAT 100.64.0.1",
        "starts at 0:30, ratio 4:3",
    ],
)
def test_rev8_public_and_special_purpose_addresses_pass(
    make_context: Callable[..., Context], studio_root: Path, description: str
) -> None:
    """Only loopback, link-local, unspecified, RFC 1918 and fc00::/7 are
    internal — not documentation ranges or other is_private space."""
    result = _gate_for(make_context, studio_root, description)
    assert result.outcome == "pass", result.detail


# ---- ninth review: ::1:port, short hosts before quotes/space/query --------------------------------


@pytest.mark.parametrize(
    "description",
    [
        "ECONNREFUSED ::1:3000",
        "listening on ::1:8080",
        "::1:5432",
        "fd00::1:8080",
        "href='http://127.1'",
        "http://127.1 is up",
        "http://10.1?x=1",
        "http://10.1#top",
        "Try http://127.1.",
        "see http://10.0.0.0.",
    ],
)
def test_rev9_port_suffixed_v6_and_short_hosts_are_refused(
    make_context: Callable[..., Context], studio_root: Path, description: str
) -> None:
    """ipprobe9.py / ipgate9.py: ::1:3000 parses as a valid NON-loopback
    address, so the candidate is judged with its :port removed too."""
    result = _gate_for(make_context, studio_root, description.replace('"', "'"))
    assert result.failed and "index.html: names a machine-internal host" in result.detail


@pytest.mark.parametrize(
    "description",
    [
        "User-Agent: Mozilla/5.0 Chrome/127.0.0.0 Safari/537.36",
        "Version=10.0.0.0, Culture=neutral",
        "contrast ratio 10.5:1",
        "WCAG 4.5:1",
        "std::vector<int> and fc::new() and a::b::c",
        "MAC fe:80:00:12:34:56 at 12:30:45.123",
    ],
)
def test_rev9_version_strings_and_ratios_pass(
    make_context: Callable[..., Context], studio_root: Path, description: str
) -> None:
    """A network address (x.y.z.0) is not a host — it is what version strings
    look like; a one-digit 'port' is a ratio."""
    result = _gate_for(make_context, studio_root, description)
    assert result.outcome == "pass", result.detail
