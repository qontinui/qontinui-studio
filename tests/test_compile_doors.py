"""The doors ``studio compile`` reads and writes through (plan 2026-10-13
Phase 19): qontinui-web's content-addressed image store, coord's imagery
treatment, the image licence manifest, the runner scheduler task for the
overnight re-render, and the SVG marker. Every HTTP door is mocked (respx);
an unanswered door is UNKNOWN, never a success."""

from __future__ import annotations

import base64
import dataclasses
import json
import textwrap
import time
from pathlib import Path

import httpx
import pytest
import respx
from click.testing import CliRunner

from qontinui_studio import marker
from qontinui_studio.compiler import schedule as cs
from qontinui_studio.compiler.image_store import (
    MAX_PROVENANCE_BYTES,
    HttpImageStore,
    fit_provenance,
)
from qontinui_studio.compiler.inputs import (
    ImagegenManifest,
    ImageryReader,
    exemplar_digest,
    heading_slug,
    section_text,
)
from qontinui_studio.compiler.target import load_target
from qontinui_studio.library import Library
from qontinui_studio.locks import LOCK_NAME, lock_path
from qontinui_studio.postprocess import sha256_file, trace_svg
from qontinui_studio.providers import Provider
from qontinui_studio.store import connect

from .compile_support import TARGET
from .test_pulse_schedule import FakeRunner

WEB = "https://web.test"
COORD = "https://coord.test"
REPO = "qontinui/qontinui-web"
OWNER = f"{COORD}/pr-merge/qontinui%2Fqontinui-web/0/author-session"


def _jwt(exp: float) -> str:
    def part(d: dict[str, object]) -> str:
        return base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")

    return f"{part({'alg': 'none'})}.{part({'exp': exp})}.sig"


TOKEN = _jwt(time.time() + 3600)


@pytest.fixture(autouse=True)
def _token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COORD_DEVICE_JWT", TOKEN)


def _png(tmp_path: Path, name: str = "c.png") -> Path:
    from PIL import Image

    path = tmp_path / name
    Image.new("RGB", (8, 8), (10, 20, 30)).save(path)
    return path


# ---- the image store -------------------------------------------------------------


@respx.mock
def test_an_upload_is_confirmed_only_by_an_answer_naming_its_hash(tmp_path: Path) -> None:
    img = _png(tmp_path)
    sha = sha256_file(img)
    respx.get(OWNER).mock(return_value=httpx.Response(200, json={}))
    route = respx.post(f"{WEB}/api/v1/image-assets")
    store = HttpImageStore(REPO, WEB, COORD, jwt_file=tmp_path / "none")
    route.mock(return_value=httpx.Response(201, json={"sha256": sha, "kind": "candidate"}))
    out = store.upload(img, "candidate", {"provider": {"id": "fake"}})
    assert (out.status, out.confirmed) == ("stored", True)
    sent = route.calls.last.request
    assert sent.headers["Authorization"] == f"Bearer {TOKEN}"
    body = sent.content
    assert b'name="kind"' in body and b"candidate" in body and b'name="provenance"' in body
    route.mock(
        return_value=httpx.Response(
            200, json={"sha256": sha, "kind": "candidate", "conflicts": [{"path": "kind"}]}
        )
    )
    out = store.upload(img, "derivative", None)
    assert out.status == "existing" and "1 provenance conflict" in out.detail
    # An answer for OTHER bytes is not a confirmation.
    route.mock(return_value=httpx.Response(201, json={"sha256": "0" * 64}))
    assert store.upload(img, "candidate", None).status == "unknown"
    route.mock(
        return_value=httpx.Response(415, json={"detail": {"error": "image_unsupported_format"}})
    )
    out = store.upload(img, "candidate", None)
    assert out.status == "refused" and "image_unsupported_format" in out.detail
    route.mock(return_value=httpx.Response(503))
    assert store.upload(img, "candidate", None).status == "unknown"
    route.mock(side_effect=httpx.ReadTimeout("slow"))
    assert store.upload(img, "candidate", None).status == "unknown"


def _tenant_jwt(tenant: str) -> str:
    def part(d: dict[str, object]) -> str:
        return base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")

    return f"{part({'alg': 'none'})}.{part({'exp': time.time() + 600, 'tenant_id': tenant})}.sig"


@respx.mock
def test_writes_go_only_to_the_tenant_proven_to_own_the_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    img = _png(tmp_path)
    other, owner = _tenant_jwt("other-tenant"), _tenant_jwt("owner-tenant")
    monkeypatch.setenv("COORD_DEVICE_JWT", other)
    jwt_file = tmp_path / "jwt"
    jwt_file.write_text(owner)
    probed: list[str] = []

    def probe(request: httpx.Request) -> httpx.Response:
        probed.append(request.headers["Authorization"])
        return httpx.Response(200 if request.headers["Authorization"] == f"Bearer {owner}" else 404)

    respx.get(OWNER).mock(side_effect=probe)
    upload = respx.post(f"{WEB}/api/v1/image-assets").mock(
        return_value=httpx.Response(201, json={"sha256": sha256_file(img)})
    )
    store = HttpImageStore(REPO, WEB, COORD, jwt_file=jwt_file)
    assert store.upload(img, "candidate", None).status == "stored"
    assert store.upload(img, "candidate", None).status == "stored"
    # The other tenant was refuted by the probe and never wrote; the proof ran once.
    assert probed == [f"Bearer {other}", f"Bearer {owner}"]
    assert {c.request.headers["Authorization"] for c in upload.calls} == {f"Bearer {owner}"}
    # The proven credential is the only one a write may use: refused there is
    # UNKNOWN, never a fall-through to an unproven credential.
    upload.mock(return_value=httpx.Response(401))
    assert store.upload(img, "candidate", None).status == "unknown"


@respx.mock
@pytest.mark.parametrize(
    ("answer", "why"),
    [
        (httpx.Response(404), "no credential is proven"),
        (httpx.Response(500), "UNKNOWN"),
        (httpx.ConnectError("refused"), "UNKNOWN"),
    ],
)
def test_an_unproven_owner_gets_no_write(answer: object, why: str, tmp_path: Path) -> None:
    img = _png(tmp_path)
    route = respx.get(OWNER)
    if isinstance(answer, Exception):
        route.mock(side_effect=answer)
    else:
        route.mock(return_value=answer)
    upload = respx.post(f"{WEB}/api/v1/image-assets")
    store = HttpImageStore(REPO, WEB, COORD, jwt_file=tmp_path / "none")
    out = store.upload(img, "candidate", None)
    assert out.status == "refused" and why in out.detail
    out = store.propose(
        repo=REPO, slot_id="a", asset_sha256="1" * 64, status="chosen", recipe_hash=None, judge={}
    )
    assert out.status == "refused" and upload.call_count == 0


@respx.mock
def test_no_token_goes_to_a_door_that_is_neither_https_nor_loopback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    img = _png(tmp_path)
    probe = respx.get(url__regex=r".*author-session").mock(return_value=httpx.Response(200))
    monkeypatch.setenv("STUDIO_WEB_URL", "http://web.example")
    out = HttpImageStore(REPO, coord_url=COORD, jwt_file=tmp_path / "none").upload(
        img, "candidate", None
    )
    assert out.status == "refused" and "neither https nor loopback" in out.detail
    out = HttpImageStore(REPO, WEB, "http://coord.example", jwt_file=tmp_path / "none").upload(
        img, "candidate", None
    )
    assert out.status == "refused" and "neither https nor loopback" in out.detail
    assert probe.call_count == 0
    monkeypatch.delenv("COORD_DEVICE_JWT")
    out = HttpImageStore(REPO, WEB, COORD, jwt_file=tmp_path / "none").upload(
        img, "candidate", None
    )
    # The runner mint is offered to production only.
    assert out.status == "refused" and "not the production web API" in out.detail


@respx.mock
def test_a_proposal_is_confirmed_by_its_read_back(tmp_path: Path) -> None:
    respx.get(OWNER).mock(return_value=httpx.Response(200, json={}))
    route = respx.put(f"{WEB}/api/v1/image-assets/slot-candidates")
    store = HttpImageStore(REPO, WEB, COORD, jwt_file=tmp_path / "none")
    kw: dict[str, object] = {
        "repo": "qontinui/qontinui-web",
        "slot_id": "a.b",
        "asset_sha256": "1" * 64,
        "recipe_hash": "2" * 64,
        "judge": {"pick": "x"},
    }
    route.mock(
        return_value=httpx.Response(
            200,
            json={
                "candidate": {"asset_sha256": "1" * 64, "status": "chosen"},
                "demoted": [{"asset_sha256": "3" * 64}],
            },
        )
    )
    out = store.propose(status="chosen", **kw)  # type: ignore[arg-type]
    assert out.status == "proposed" and "1 earlier choice" in out.detail
    assert json.loads(route.calls.last.request.content) == {**kw, "status": "chosen"}
    route.mock(
        return_value=httpx.Response(
            200, json={"candidate": {"asset_sha256": "1" * 64, "status": "candidate"}}
        )
    )
    assert store.propose(status="chosen", **kw).status == "unknown"  # type: ignore[arg-type]
    route.mock(
        return_value=httpx.Response(404, json={"detail": {"error": "image_asset_not_found"}})
    )
    out = store.propose(status="chosen", **kw)  # type: ignore[arg-type]
    assert out.status == "refused" and "image_asset_not_found" in out.detail


def test_a_provenance_record_over_the_cap_keeps_its_essentials() -> None:
    record = {
        "provider": {"id": "p"},
        "file": {"sha256": "x"},
        "prompt": "p" * (MAX_PROVENANCE_BYTES + 10),
    }
    fitted = fit_provenance(record)
    assert fitted is not None and "prompt" not in fitted and fitted["provider"] == {"id": "p"}
    assert "truncated" in fitted
    small = {"provider": {"id": "p"}}
    assert fit_provenance(small) is small


# ---- the imagery treatment -----------------------------------------------------------

BODY = textwrap.dedent(
    """\
    # Visual system

    ## Imagery & iconography

    Rules here.

    ```
    ## not a heading
    ```

    ### Inherited rules

    - one

    ## Typography

    Other.
    """
)


def test_a_fragment_names_one_section_by_whole_word_prefix() -> None:
    assert heading_slug("Imagery & iconography") == "imagery-iconography"
    text = section_text(BODY, "imagery")
    assert text is not None
    assert text.startswith("## Imagery & iconography") and "### Inherited rules" in text
    assert "## not a heading" in text and "Typography" not in text
    assert section_text(BODY, "imag") is None  # not a whole word
    assert section_text(BODY + "\n## Imagery notes\n", "imagery") is None  # ambiguous
    assert section_text(BODY, "inherited-rules") == "### Inherited rules\n\n- one\n"


@respx.mock
def test_the_imagery_digest_is_the_named_sections_sha256(tmp_path: Path) -> None:
    import hashlib

    route = respx.get(f"{COORD}/coord/agent-prompt-documents/domain_spec/visual-system")
    route.mock(
        return_value=httpx.Response(
            200, json={"body": BODY, "current_version": 3, "withdrawn": False}
        )
    )
    reader = ImageryReader(COORD, jwt_file=tmp_path / "none")
    d = reader("domain_spec/visual-system#imagery")
    section = section_text(BODY, "imagery")
    assert section is not None
    assert d.value == hashlib.sha256(section.encode()).hexdigest()
    assert d.source == "domain_spec/visual-system@v3#imagery"
    reader("domain_spec/visual-system#imagery")
    assert route.call_count == 1  # read once per run
    # The whole body when no section is named; edits OUTSIDE the section do not
    # move a section digest.
    whole = ImageryReader(COORD, jwt_file=tmp_path / "none")("domain_spec/visual-system")
    assert whole.value == hashlib.sha256(BODY.encode()).hexdigest()
    route.mock(
        return_value=httpx.Response(
            200, json={"body": BODY.replace("Other.", "Changed."), "current_version": 4}
        )
    )
    again = ImageryReader(COORD, jwt_file=tmp_path / "none")("domain_spec/visual-system#imagery")
    assert again.value == d.value


@respx.mock
@pytest.mark.parametrize(
    ("response", "why"),
    [
        (httpx.Response(200, json={"body": BODY, "unedited_seed": True}), "unedited skeleton"),
        (
            httpx.Response(
                200, json={"body": BODY, "default_source": "seed", "current_version": 1}
            ),
            "unedited skeleton",
        ),
        (
            httpx.Response(
                200, json={"body": BODY, "default_source": "seed", "current_version": 2}
            ),
            "UNKNOWN",
        ),
        (httpx.Response(404), "no such document"),
        (httpx.Response(500), "HTTP 500"),
        (httpx.Response(200, json={"body": BODY, "withdrawn": True}), "withdrawn"),
        (httpx.Response(200, json={"body": "# nothing"}), "no single section"),
        (httpx.Response(200, text="garbage"), "unreadable"),
    ],
)
def test_an_unreadable_treatment_is_unknown(
    response: httpx.Response, why: str, tmp_path: Path
) -> None:
    respx.get(f"{COORD}/coord/agent-prompt-documents/domain_spec/visual-system").mock(
        return_value=response
    )
    d = ImageryReader(COORD, jwt_file=tmp_path / "none")("domain_spec/visual-system#imagery")
    assert d.value is None and why in d.source


# ---- the licence manifest and recipes ---------------------------------------------------


def _imagegen(tmp_path: Path, exit_code: int = 0) -> Path:
    root = tmp_path / "imagegen"
    pkg = root / "qontinui_imagegen"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "manifest.py").write_text(
        textwrap.dedent(
            f"""\
            import sys
            if "--hashes" in sys.argv:
                print("hero v1 {"d" * 64}")
                print("cut-out v2 {"e" * 64}")
            print("manifest and recipes admitted", file=sys.stderr)
            sys.exit({exit_code})
            """
        )
    )
    (root / "manifest.yaml").write_text(
        textwrap.dedent(
            f"""\
            components:
              - id: qontinui-house-v1
                files: [{{path: lora.safetensors, sha256: {"c" * 64}, bytes: 1}}]
              - id: two-files
                files: [{{path: a, sha256: {"1" * 64}}}, {{path: b, sha256: {"2" * 64}}}]
            """
        )
    )
    return root


def test_recipe_hashes_come_from_the_manifests_own_cli(tmp_path: Path) -> None:
    m = ImagegenManifest(_imagegen(tmp_path))
    assert m.recipe("hero").value == "d" * 64 and m.recipe("cut-out").value == "e" * 64
    assert m.recipe("upscale").value is None
    assert not list((tmp_path / "imagegen").rglob("__pycache__"))  # read-only use


def test_a_manifest_that_does_not_check_is_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    d = ImagegenManifest(_imagegen(tmp_path, exit_code=1)).recipe("hero")
    assert d.value is None and "does not check" in d.source
    monkeypatch.delenv("STUDIO_IMAGEGEN_DIR", raising=False)
    unconfigured = ImagegenManifest(None)
    assert unconfigured.recipe("hero").value is None
    assert "STUDIO_IMAGEGEN_DIR" in unconfigured.lora("x").source
    monkeypatch.setenv("STUDIO_IMAGEGEN_DIR", str(_imagegen(tmp_path / "env")))
    assert ImagegenManifest(None).recipe("hero").value == "d" * 64


def test_a_lora_digest_is_its_one_weights_file(tmp_path: Path) -> None:
    m = ImagegenManifest(_imagegen(tmp_path))
    assert m.lora("qontinui-house-v1").value == "c" * 64
    assert m.lora("two-files").value is None
    assert "not in the image licence manifest" in m.lora("absent").source


def test_an_unseen_exemplar_set_is_unknown_not_empty(tmp_path: Path) -> None:
    conn = connect(tmp_path / "s.db")
    unseen = exemplar_digest(Library(conn), "console-empty-states")
    assert unseen.value is None and "no member" in unseen.source


# ---- the overnight task ----------------------------------------------------------


def _task(tmp_path: Path) -> cs.CompileTask:
    (tmp_path / "targets").mkdir()
    (tmp_path / "targets" / "web.yaml").write_text(TARGET)
    target = load_target(tmp_path / "targets" / "web.yaml", tmp_path)
    return cs.CompileTask(
        target=target,
        working_directory="/work/qontinui-studio",
        studio_root="/work/qontinui-studio",
        workspace_root="/work",
    )


def test_the_task_runs_changed_in_an_allocated_worktree_and_studio_runs_no_git(
    tmp_path: Path,
) -> None:
    task = _task(tmp_path)
    body = task.create_body()
    assert body["name"] == "image-compile-web"
    assert body["task"]["task_type"] == "RemoteAgent"
    prompt = body["task"]["prompt"]
    assert "/work/qontinui-claude-config/scripts/allocate-worktree.sh" in prompt
    assert "--repo qontinui-web" in prompt
    assert (
        "compile --target web --slots <W>/frontend/image-slots --changed "
        "--imagegen-dir <Q>/docker/imagegen --json" in prompt
    )
    # The recipe and LoRA axes are read from a qontinui worktree at origin/main,
    # allocated beside W and handed back at the end.
    assert "--repo qontinui " in prompt and "--done --path <Q> --abandon" in prompt
    # Every stop path ends with the hand-back step, exit 6 included.
    assert "never stop without it" in prompt
    # Alt texts are written BEFORE routing; routing reads W against its HEAD.
    alt = prompt.index("FIRST, if the report's exit_code is 7")
    route = prompt.index("git status --porcelain -- frontend/public/image-slots")
    assert alt < route
    assert (
        "If it prints nothing, W's publish dir is the same as its HEAD: go to the final step"
        in prompt
    )
    assert "it does not decide what you do next" in prompt
    assert "files_written" not in prompt
    final = prompt[prompt.index("FINAL step") :]
    assert "--done --path <W> --abandon" in final and "--done --path <Q> --abandon" in final
    assert "gh pr create" in prompt and "--body-file" in prompt
    assert "Never merge" in prompt and "--done --path <W> --abandon" in prompt
    assert body["schedule"] == {"type": "Cron", "value": "30 2 * * *"}
    assert cs.nightly_cron("01:05") == "5 1 * * *"
    # A pinned manifest allocates nothing for it.
    pinned = dataclasses.replace(task, imagegen_dir="/pinned/docker/imagegen").prompt()
    assert "--imagegen-dir /pinned/docker/imagegen --json" in pinned
    assert "--repo qontinui-web" in pinned and "--repo qontinui " not in pinned
    assert "<Q>" not in pinned


def test_the_task_installs_by_name_and_reports_drift(tmp_path: Path) -> None:
    from qontinui_studio import pulse_schedule as ps

    runner = FakeRunner([{"id": "other", "name": "factory-pulse", "enabled": True}])
    task = _task(tmp_path)

    def client() -> ps.RunnerScheduler:
        return ps.RunnerScheduler(
            "http://runner.test",
            httpx.Client(transport=httpx.MockTransport(runner.handle)),
            name=task.name,
        )

    assert ps.install(client(), task).startswith("CREATED")
    assert ps.install(client(), task).startswith("UNCHANGED")
    code, text = ps.check(client(), task)
    assert code == ps.EXIT_OK and "drift: none" in text
    # A task of another name is never touched.
    assert [t["name"] for t in runner.tasks] == ["factory-pulse", "image-compile-web"]
    assert ps.uninstall(client()) == "REMOVED 1 task(s) named image-compile-web"
    assert [t["name"] for t in runner.tasks] == ["factory-pulse"]


def test_compile_schedule_dry_run_cli(tmp_path: Path, studio_root: Path) -> None:
    from qontinui_studio.cli import main

    (studio_root / "targets").mkdir()
    (studio_root / "targets" / "web.yaml").write_text(TARGET)
    ws = tmp_path / "ws"
    (ws / "qontinui-claude-config" / "scripts").mkdir(parents=True)
    (ws / cs.ALLOCATE).write_text("#!/bin/sh\n")
    r = CliRunner().invoke(
        main,
        [
            "--root",
            str(studio_root),
            "compile-schedule",
            "--dry-run",
            "--workdir",
            str(tmp_path),
            "--workspace-root",
            str(ws),
        ],
    )
    assert r.exit_code == 0, r.output
    assert "image-compile-web" in r.output and "--changed" in r.output
    missing = CliRunner().invoke(
        main,
        [
            "--root",
            str(studio_root),
            "compile-schedule",
            "--dry-run",
            "--workdir",
            str(tmp_path),
            "--workspace-root",
            str(tmp_path),
        ],
    )
    assert missing.exit_code == 4 and "allocate-worktree.sh" in missing.output


# ---- the SVG marker and the checkout lock -------------------------------------------


def test_a_trace_is_marked_in_its_metadata_and_only_there(tmp_path: Path) -> None:
    from PIL import Image, ImageDraw

    img = Image.new("RGBA", (40, 30), (0, 0, 0, 0))
    ImageDraw.Draw(img).ellipse((5, 5, 35, 25), fill=(200, 40, 40, 255))
    svg = trace_svg(img)
    assert not marker.bytes_are_marked(svg.encode())
    marked = marker.mark_svg(svg)
    assert marker.bytes_are_marked(marked.encode())
    assert marker.mark_svg(marked).count("x:xmpmeta>") == 2  # one packet: open + close tag
    # A packet outside the root's <metadata> is not the container.
    packet = marked[marked.index("<x:xmpmeta") : marked.index("</x:xmpmeta>") + 12]
    stray = marked.replace(f"<metadata>{packet}</metadata>", f"<g>{packet}</g>")
    assert stray != marked and marker.svg_root(stray.encode()) is not None
    assert not marker.bytes_are_marked(stray.encode())
    # Two packets are ambiguous.
    doubled = marked.replace("</metadata>", f"{packet}</metadata>")
    assert not marker.bytes_are_marked(doubled.encode())
    with pytest.raises(marker.MarkerError):
        marker.mark_svg("<html/>")


def test_a_checkout_lock_lives_in_a_per_user_dir_outside_the_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os
    import stat
    import tempfile

    runtime = tmp_path / "run-user"
    runtime.mkdir(mode=0o700)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    assert lock_path(tmp_path) == tmp_path / LOCK_NAME
    outside = lock_path(tmp_path, in_checkout=True)
    assert outside.parent == runtime / "qontinui-studio-locks"
    assert stat.S_IMODE(os.stat(outside.parent).st_mode) == 0o700
    assert lock_path(tmp_path, in_checkout=True) == outside  # stable per directory
    monkeypatch.delenv("XDG_RUNTIME_DIR")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    fallback = lock_path(tmp_path / "x", in_checkout=True)
    assert fallback.is_relative_to(tmp_path / "home" / ".cache")
    assert not fallback.is_relative_to(Path(tempfile.gettempdir()) / "qontinui-studio-locks")


def test_the_shipped_targets_load() -> None:
    from qontinui_studio.compiler.target import load_targets

    repo = Path(__file__).resolve().parent.parent
    targets = load_targets(repo / "targets", repo)
    assert targets["qontinui-web"].repo == "qontinui/qontinui-web"
    assert targets["qontinui-web"].providers == ["local-gpu"]


def test_a_lock_dir_must_be_this_users_and_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os
    import stat

    from qontinui_studio.locks import user_lock_dir

    runtime = tmp_path / "run"
    (runtime / "qontinui-studio-locks").mkdir(parents=True)
    os.chmod(runtime / "qontinui-studio-locks", 0o777)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    base = user_lock_dir()  # ours but open: closed to 0700
    assert stat.S_IMODE(os.stat(base).st_mode) == 0o700
    monkeypatch.setattr(os, "getuid", lambda: os.stat(base).st_uid + 1)
    with pytest.raises(OSError, match="not owned by this user"):
        user_lock_dir()


def _facade_provider() -> Provider:
    return Provider.model_validate(
        {
            "id": "local-gpu",
            "provider": "facade",
            "plan": "t",
            "api_available": {"value": "yes", "source": "https://f.test/a"},
            "automation_allowed": {"value": "yes", "source": "https://f.test/t"},
            "commercial_use": {"value": "yes", "source": "https://f.test/c"},
            "watermark": "IPTC",
            "adapter": "openai_compatible_images",
            "model": "z-image-turbo",
            "base_url": "http://127.0.0.1:8100/v1",
            "capacity": "unmetered",
            "tos_reviewed": "2026-10-15",
            "domains": ["f.test"],
        }
    )


SWAP = "http://127.0.0.1:8100"
FACADE_MODELS = {
    "data": [
        {
            "id": "qontinui-imagegen",
            "meta": {"llamaswap": {"qontinui_capability": "image_generation"}},
        },
        {"id": "gemma-4-31b", "meta": {"llamaswap": {"qontinui_capability": "text"}}},
    ]
}


def _running(*models: str) -> httpx.Response:
    return httpx.Response(200, json={"running": [{"model": m, "state": "ready"} for m in models]})


@respx.mock
def test_the_facade_probe_asks_only_a_resident_facade() -> None:
    """Behind llama-swap, ``/upstream/<model>/...`` loads the model it names
    and evicts the resident one. So the probe reads ``/running`` (loads
    nothing) first and reaches the facade's status only when its entry is
    already resident; otherwise it says UNKNOWN without asking."""
    from qontinui_studio.compiler.facade import HttpFacadeProbe

    provider = _facade_provider()
    running = respx.get(f"{SWAP}/running").mock(return_value=_running("gemma-4-31b"))
    respx.get(f"{SWAP}/v1/models").mock(return_value=httpx.Response(200, json=FACADE_MODELS))
    upstream = respx.get(url__regex=r".*/upstream/.*").mock(
        return_value=httpx.Response(200, json={"recipes": {"hero": "d" * 64, "x": "bad"}})
    )

    asleep = HttpFacadeProbe()(provider, wake=False)
    assert asleep.state == "unknown" and "not resident" in asleep.detail
    assert upstream.call_count == 0  # the gemma model stayed resident

    running.mock(return_value=_running("gemma-4-31b", "qontinui-imagegen"))
    probe = HttpFacadeProbe()
    served = probe(provider, wake=False)
    assert served.state == "map" and served.recipes == {"hero": "d" * 64}
    assert upstream.calls.last.request.url.path == "/upstream/qontinui-imagegen/qontinui/status"
    assert upstream.calls.last.request.extensions["timeout"]["read"] == 15.0


@respx.mock
def test_the_facade_probe_wakes_the_facade_only_when_asked_to() -> None:
    """``wake``: a generation is about to load the facade anyway, so the probe
    may load it now — with the longer timeout a cold start needs."""
    from qontinui_studio.compiler.facade import WAKE_TIMEOUT_S, HttpFacadeProbe

    provider = _facade_provider()
    respx.get(f"{SWAP}/running").mock(return_value=_running("gemma-4-31b"))
    respx.get(f"{SWAP}/v1/models").mock(return_value=httpx.Response(200, json=FACADE_MODELS))
    upstream = respx.get(f"{SWAP}/upstream/qontinui-imagegen/qontinui/status").mock(
        return_value=httpx.Response(200, json={"engine": "diffusers"})
    )
    served = HttpFacadeProbe()(provider, wake=True)
    assert served.state == "absent"  # an older facade: no map
    assert upstream.call_count == 1
    assert upstream.calls.last.request.extensions["timeout"]["read"] == WAKE_TIMEOUT_S


@respx.mock
def test_the_facade_probe_answers_each_provider_once_per_run() -> None:
    from qontinui_studio.compiler.facade import HttpFacadeProbe

    provider = _facade_provider()
    running = respx.get(f"{SWAP}/running").mock(return_value=_running("qontinui-imagegen"))
    respx.get(f"{SWAP}/v1/models").mock(return_value=httpx.Response(200, json=FACADE_MODELS))
    upstream = respx.get(url__regex=r".*/upstream/.*").mock(
        return_value=httpx.Response(200, json={"recipes": {"hero": "d" * 64}})
    )
    probe = HttpFacadeProbe()
    for _ in range(4):
        assert probe(provider, wake=True).recipes == {"hero": "d" * 64}
    assert (running.call_count, upstream.call_count) == (1, 1)

    # An UNKNOWN that was not allowed to wake the facade is asked again by a
    # caller that may (a generation is about to run); a woken answer is final.
    running.mock(return_value=_running("gemma-4-31b"))
    cold = HttpFacadeProbe()
    assert cold(provider, wake=False).state == "unknown"
    assert cold(provider, wake=False).state == "unknown"
    assert upstream.call_count == 1
    assert cold(provider, wake=True).state == "map"
    assert cold(provider, wake=True).state == "map"
    assert upstream.call_count == 2


@respx.mock
def test_a_failed_wake_is_cached_for_the_run() -> None:
    """A wake that produced no answer (the facade did not come up) is not
    paid again by every later slot: up to WAKE_TIMEOUT_S each."""
    from qontinui_studio.compiler.facade import HttpFacadeProbe

    provider = _facade_provider()
    respx.get(f"{SWAP}/running").mock(return_value=_running("gemma-4-31b"))
    respx.get(f"{SWAP}/v1/models").mock(return_value=httpx.Response(200, json=FACADE_MODELS))
    upstream = respx.get(url__regex=r".*/upstream/.*").mock(return_value=httpx.Response(503))
    probe = HttpFacadeProbe()
    first = probe(provider, wake=True)
    assert first.state == "unknown" and first.evicted == ("gemma-4-31b",)
    for _ in range(3):
        again = probe(provider, wake=True)
        assert again.state == "unknown" and again.evicted == ()  # reported once
    assert upstream.call_count == 1


@respx.mock
def test_the_facade_probe_without_llama_swap_asks_the_facade_and_unreadable_is_unknown() -> None:
    from qontinui_studio.compiler.facade import HttpFacadeProbe

    provider = _facade_provider()
    running = respx.get(f"{SWAP}/running").mock(return_value=httpx.Response(404))
    direct = respx.get(f"{SWAP}/qontinui/status").mock(
        return_value=httpx.Response(200, json={"recipes": {"hero": "e" * 64}})
    )
    assert HttpFacadeProbe()(provider, wake=False).recipes == {"hero": "e" * 64}
    direct.mock(return_value=httpx.Response(503))
    assert HttpFacadeProbe()(provider, wake=False).state == "unknown"
    running.mock(side_effect=httpx.ConnectError("refused"))
    assert HttpFacadeProbe()(provider, wake=True).state == "unknown"
    running.mock(return_value=httpx.Response(200, json={"nope": 1}))
    assert HttpFacadeProbe()(provider, wake=True).state == "unknown"
    assert direct.call_count == 2


def test_a_record_in_the_facades_landed_shape_is_complete() -> None:
    """The shape qontinui's facade writes (docker/imagegen/qontinui_imagegen/
    provenance.py, schema qontinui.imagegen.provenance/1): studio reads
    recipe.hash and licence.spdx from it, and nothing it requires is absent."""
    from qontinui_studio import provenance

    record = {
        "schema": "qontinui.imagegen.provenance/1",
        "job_id": "j1",
        "created_at": "2026-10-20T01:00:00Z",
        "recipe": {"id": "hero", "version": 1, "hash": "d" * 64},
        "models": [
            {
                "id": "qwen-image-2512",
                "role": "generator",
                "source": {"type": "huggingface"},
                "spdx": "Apache-2.0",
                "weights": [{"path": "transformer.safetensors", "sha256": "a" * 64}],
            },
            {
                "id": "real-esrgan-x2plus",
                "role": "upscaler",
                "source": {"type": "github"},
                "spdx": "BSD-3-Clause",
                "weights": [{"path": "x2.pth", "sha256": "b" * 64}],
            },
        ],
        "licence": {"spdx": "Apache-2.0 AND BSD-3-Clause", "code": {}, "decision_record": {}},
        "seed": 7,
        "candidate_index": 0,
        "prompt": "p",
        "references": [],
        "parent_asset": None,
        "engine": {"facade": "0.1.0"},
        "marker": {"iptc_digital_source_type": "x"},
    }
    assert provenance.facade_missing(record) == []
    assert provenance.facade_recipe_hash(record) == "d" * 64
