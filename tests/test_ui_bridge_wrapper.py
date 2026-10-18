"""The operator-account wrapper route: the override, the adapter and the hand-off
(plan 2026-10-17-free-image-generation-local-gpu-and-private-ui-bridge-wrappers)."""

from __future__ import annotations

import base64
import io
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
import yaml
from PIL import Image

from qontinui_studio.adapters import AdapterSet, GenerationRequest, build_adapter
from qontinui_studio.adapters.fake import FakeImageAdapter
from qontinui_studio.adapters.ui_bridge_wrapper import UIBridgeWrapperAdapter
from qontinui_studio.briefs import load_brief
from qontinui_studio.errors import (
    AdapterDisabledError,
    ConfigError,
    OperatorHandoff,
    PermanentProviderError,
    ProviderBusy,
    ProviderOutcomeUnknown,
    QuotaExhaustedError,
)
from qontinui_studio.library import TermsSnapshot
from qontinui_studio.pipeline import Context, drive_batch, new_batch
from qontinui_studio.providers import ProviderInventory, load_providers

RUNNER = "http://127.0.0.1:9876"
DISPATCH = f"{RUNNER}/wrappers/gemini-web/dispatch"
REPO = Path(__file__).resolve().parent.parent

OVERRIDE = {
    "decided": "2026-10-17",
    "record": "plans/2026-10-09-factory-built-product-portfolio-and-launch-kit.md",
    "max_per_day": 10,
    "min_interval_s": 90,
    "on_challenge": "stop_and_hand_off",
}


def _row(**changes: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": "gemini-web",
        "provider": "Gemini web (test)",
        "plan": "test",
        "api_available": {"value": "no", "source": "https://gemini.example/robots.txt"},
        "automation_allowed": {"value": "no", "source": "https://gemini.example/robots.txt"},
        "commercial_use": {"value": "yes", "source": "https://gemini.example/terms"},
        "watermark": "none",
        "adapter": "ui_bridge_wrapper",
        "wrapper_id": "gemini-web",
        "model": "gemini-web-default",
        "daily_quota": 10,
        "operator_override": dict(OVERRIDE),
        "tos_reviewed": "2026-10-17",
        "domains": ["gemini.example"],
    }
    row.update(changes)
    return {k: v for k, v in row.items() if v is not None}


def _inventory(*rows: dict[str, Any]) -> ProviderInventory:
    return ProviderInventory.model_validate(
        {"note": "test", "operator_accounts_owed": False, "providers": list(rows)}
    )


def _png(width: int = 8, height: int = 8) -> str:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), (200, 40, 40)).save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


def _request() -> GenerationRequest:
    return GenerationRequest(job_id="job1", prompt="a red square", aspect_ratio="1:1")


def _answer(result: dict[str, Any]) -> httpx.Response:
    """The REAL two-layer envelope. The @qontinui/ui-bridge-wrapper@0.8.0
    dispatch server answers ``{"success": true, "data": <handler result>}``
    (captured from the running server 2026-10-17); the runner's dispatch.rs
    forwards that body as ``result`` because it has no ``result`` key."""
    wrapper_body = {"success": True, "data": result}
    return httpx.Response(
        200, json={"success": True, "data": {"result": wrapper_body}, "error": None}
    )


def _adapter(state: Path, **kw: Any) -> UIBridgeWrapperAdapter:
    """An adapter whose clock moves 1000 s per read, so consecutive submits in
    one test are never refused by studio's own pace interval."""
    from qontinui_studio.providers import OperatorOverride

    ticks = iter(range(10**6))
    kw.setdefault("override", OperatorOverride.model_validate(OVERRIDE))
    kw.setdefault("clock", lambda: 1_000_000.0 + 1000.0 * next(ticks))
    return UIBridgeWrapperAdapter("gemini-web", "m", "gemini-web", RUNNER, state_dir=state, **kw)


# --- the override in the inventory ---------------------------------------------


def test_a_wrapper_row_runs_only_under_its_override() -> None:
    with_override = _inventory(_row()).providers[0]
    assert with_override.automation_allowed.value == "no"  # the verdict stays truthful
    assert with_override.adapter_gate() is None
    assert with_override.schedulable_reason() is None

    with pytest.raises(ValueError, match="needs an operator_override"):
        _inventory(_row(operator_override=None, daily_quota=10))


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"adapter": "fake", "wrapper_id": None}, "applies only to"),
        ({"wrapper_id": None}, "needs a wrapper_id"),
        ({"daily_quota": 25}, "must equal operator_override.max_per_day"),
        ({"operator_override": {**OVERRIDE, "on_challenge": "retry"}}, "on_challenge"),
        ({"operator_override": {**OVERRIDE, "min_interval_s": 5}}, "min_interval_s"),
        ({"operator_override": {**OVERRIDE, "max_per_day": 0}, "daily_quota": 0}, "max_per_day"),
    ],
)
def test_the_override_is_bounded(changes: dict[str, Any], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        _inventory(_row(**changes))


def test_wrapper_id_belongs_to_wrapper_rows_only() -> None:
    row = _row(adapter="fake", operator_override=None, wrapper_id="x")
    row["automation_allowed"] = {"value": "yes", "source": "https://gemini.example/tos"}
    with pytest.raises(ValueError, match="wrapper_id is only for"):
        _inventory(row)


def test_the_terms_snapshot_records_the_override_beside_the_verdict() -> None:
    snap = TermsSnapshot.of(_inventory(_row()).providers[0])
    assert snap.automation_allowed == "no"
    assert snap.operator_override is not None
    assert snap.operator_override["on_challenge"] == "stop_and_hand_off"
    # Older job rows carry no override key and still load.
    old = json.loads(snap.to_json())
    del old["operator_override"]
    assert TermsSnapshot.from_json(json.dumps(old)).operator_override is None


def test_the_shipped_inventory_spends_nothing() -> None:
    inventory = load_providers(REPO / "providers.yaml")
    assert not inventory.operator_accounts_owed
    for p in inventory.providers:
        if p.api_key_env:  # every billed API row stays unscheduled
            assert p.schedulable_reason() is not None, p.id
    schedulable = {p.id for p in inventory.providers if p.schedulable_reason() is None}
    assert schedulable == {"local-gpu", "gemini-web", "chatgpt-web"}
    for pid in ("gemini-web", "chatgpt-web"):
        row = inventory.get(pid)
        assert row.automation_allowed.value == "no"
        assert row.operator_override is not None
        assert row.operator_override.on_challenge == "stop_and_hand_off"
    for brief in ("example-hero.yaml", "qontinui-studio-launch.yaml"):
        prefs = yaml.safe_load((REPO / "briefs" / brief).read_text())["provider_preference"]
        assert prefs == ["local-gpu", "gemini-web", "chatgpt-web"]


# --- the adapter ---------------------------------------------------------------


def test_build_adapter_refuses_a_runner_off_the_local_network(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    provider = _inventory(_row()).providers[0]
    monkeypatch.setenv("STUDIO_RUNNER_URL", "https://runner.example.com")
    with pytest.raises(AdapterDisabledError, match="not on this machine or the local network"):
        build_adapter(provider, state_dir=tmp_path)
    monkeypatch.setenv("STUDIO_RUNNER_URL", RUNNER)
    adapter = build_adapter(provider, state_dir=tmp_path)
    assert isinstance(adapter, UIBridgeWrapperAdapter)
    assert adapter.runner_url == RUNNER


@respx.mock
def test_done_stages_marked_images(tmp_path: Path) -> None:
    route = respx.post(DISPATCH).mock(
        return_value=_answer({"status": "done", "images": [{"b64_json": _png()}]})
    )
    adapter = _adapter(tmp_path / "state")
    job = adapter.submit(_request(), tmp_path / "stage")
    sent = route.calls.last.request
    assert sent.headers["X-Wrapper-Dispatch-Timeout"] == "300"
    assert json.loads(sent.content) == {
        "action": "generate-image",
        "params": {
            "prompt": "a red square",
            "aspectRatio": "1:1",
            "maxPerDay": 10,
            "minIntervalS": 90,
            "budgetS": 280,
        },
    }
    assert adapter.poll(job).status == "done"
    files = adapter.download(job, tmp_path / "out")
    assert len(files) == 1 and files[0].suffix == ".png"


@pytest.mark.parametrize(
    ("result", "error", "check"),
    [
        (
            {"status": "handoff", "reason": "captcha"},
            OperatorHandoff,
            lambda e: e.reason == "captcha",
        ),
        (
            {"status": "handoff", "reason": "something-new"},
            OperatorHandoff,
            lambda e: e.reason == "ui_changed",
        ),
        (
            {"status": "paced", "retry_after_s": 45},
            ProviderBusy,
            lambda e: e.retry_after_s == 45 and e.code == "paced",
        ),
        ({"status": "cap_reached"}, QuotaExhaustedError, lambda e: True),
        ({"status": "done", "images": []}, PermanentProviderError, lambda e: True),
        (
            {"status": "done", "images": [{"b64_json": "%%%"}]},
            PermanentProviderError,
            lambda e: True,
        ),
        ({"status": "weird"}, ProviderOutcomeUnknown, lambda e: True),
    ],
)
@respx.mock
def test_wrapper_outcomes_are_typed(
    tmp_path: Path, result: dict[str, Any], error: type[Exception], check: Callable[[Any], bool]
) -> None:
    respx.post(DISPATCH).mock(return_value=_answer(result))
    adapter = _adapter(tmp_path / "state")
    with pytest.raises(error) as info:
        adapter.submit(_request(), tmp_path)
    assert check(info.value)
    assert not list(tmp_path.rglob("*.png"))  # nothing staged


@pytest.mark.parametrize(
    ("response", "error"),
    [
        (
            httpx.Response(404, json={"success": False, "error": "not installed"}),
            PermanentProviderError,
        ),
        (
            httpx.Response(
                503,
                json={"success": False, "error": "x", "code": "wrapper_spawn_failed"},
            ),
            ProviderBusy,
        ),
        # An untyped 503 is not the runner's "nothing started".
        (
            httpx.Response(503, json={"success": False, "error": "spawn failed"}),
            ProviderOutcomeUnknown,
        ),
        (
            httpx.Response(504, json={"success": False, "error": "timed out"}),
            ProviderOutcomeUnknown,
        ),
        (
            httpx.Response(502, json={"success": False, "error": "dispatch failed"}),
            ProviderOutcomeUnknown,
        ),
        (httpx.Response(200, text="<html>"), ProviderOutcomeUnknown),
        (httpx.Response(200, json={"success": True, "data": {}}), ProviderOutcomeUnknown),
    ],
)
@respx.mock
def test_runner_outcomes_are_typed(
    tmp_path: Path, response: httpx.Response, error: type[Exception]
) -> None:
    respx.post(DISPATCH).mock(return_value=response)
    adapter = _adapter(tmp_path / "state")
    with pytest.raises(error):
        adapter.submit(_request(), tmp_path)


@respx.mock
def test_transport_failures_split_on_whether_the_prompt_left(tmp_path: Path) -> None:
    adapter = _adapter(tmp_path / "state")
    respx.post(DISPATCH).mock(side_effect=httpx.ConnectError("refused"))
    with pytest.raises(ProviderBusy) as busy:
        adapter.submit(_request(), tmp_path)
    # Parked past the scheduler's 30-minute run horizon: a dead runner is not
    # re-dialled for the whole run.
    assert busy.value.retry_after_s == 3600
    respx.post(DISPATCH).mock(side_effect=httpx.ReadError("dropped"))
    with pytest.raises(ProviderOutcomeUnknown):
        adapter.submit(_request(), tmp_path)


# --- the hand-off in the scheduler ---------------------------------------------


class HandsOff(FakeImageAdapter):
    def __init__(self, provider_id: str) -> None:
        super().__init__(provider_id, "fake-v1")
        self.calls = 0

    def submit(self, request: GenerationRequest, staging_dir: Path):  # type: ignore[no-untyped-def]
        self.calls += 1
        raise OperatorHandoff("operator hand-off needed (login_wall)", "login_wall")


def test_a_handoff_takes_the_provider_out_for_the_day_and_the_job_moves_on(
    make_context: Callable[..., Context], write_brief: Callable[..., Path]
) -> None:
    first = HandsOff("fake")
    ctx = make_context(
        adapters=AdapterSet(adapters={"fake": first, "fake2": FakeImageAdapter("fake2", "fake-v2")})
    )
    loaded = load_brief(write_brief(providers="fake, fake2", variants=2))
    out = drive_batch(ctx, new_batch(ctx, loaded), publish=False)
    assert out.status == "generated"
    assert {r["provider"] for r in ctx.conn.execute("SELECT provider FROM jobs")} == {"fake2"}
    # One hand-off, then the provider is spent: the site is not asked again.
    assert first.calls == 1


def test_the_inventory_loader_reports_a_bad_override_as_config(tmp_path: Path) -> None:
    path = tmp_path / "providers.yaml"
    bad = _row(daily_quota=3)
    path.write_text(
        yaml.safe_dump({"note": "t", "operator_accounts_owed": False, "providers": [bad]})
    )
    with pytest.raises(ConfigError, match="must equal"):
        load_providers(path)


# --- review round 1: bounds on the wire, the latch, the notice -------------------


@respx.mock
def test_the_override_bounds_ride_every_dispatch(tmp_path: Path) -> None:
    route = respx.post(DISPATCH).mock(
        return_value=_answer({"status": "done", "images": [{"b64_json": _png()}]})
    )
    adapter = _adapter(tmp_path / "state")
    adapter.submit(_request(), tmp_path)
    params = json.loads(route.calls.last.request.content)["params"]
    assert params["maxPerDay"] == 10 and params["minIntervalS"] == 90


def test_the_override_cap_has_a_manual_equivalent_ceiling() -> None:
    with pytest.raises(ValueError, match="max_per_day"):
        _inventory(_row(operator_override={**OVERRIDE, "max_per_day": 26}, daily_quota=26))
    assert _inventory(_row(operator_override={**OVERRIDE, "max_per_day": 25}, daily_quota=25))


def test_every_wrapper_row_needs_an_override_even_when_automation_is_allowed() -> None:
    row = _row(operator_override=None)
    row["automation_allowed"] = {"value": "yes", "source": "https://gemini.example/tos"}
    with pytest.raises(ValueError, match="needs an operator_override"):
        _inventory(row)


def test_a_wrapper_adapter_needs_the_state_dir() -> None:
    with pytest.raises(AdapterDisabledError, match="needs the workspace state dir"):
        build_adapter(_inventory(_row()).providers[0])


class _Notices:
    def __init__(self) -> None:
        self.sent: list[Any] = []

    def notify(self, notice: Any) -> Any:
        self.sent.append(notice)
        return None


@respx.mock
def test_a_handoff_latches_until_the_operator_clears_it(tmp_path: Path) -> None:
    from qontinui_studio.adapters import build_adapters
    from qontinui_studio.adapters.ui_bridge_wrapper import HandoffLatch

    route = respx.post(DISPATCH).mock(
        return_value=_answer(
            {
                "status": "handoff",
                "reason": "captcha",
                "detail": "frame https://www.google.com/recaptcha/api2?k=SECRET for a@b.example",
            }
        )
    )
    latch = HandoffLatch(tmp_path / "handoffs")
    notices = _Notices()
    adapter = _adapter(tmp_path, notifier=notices)
    with pytest.raises(OperatorHandoff) as first:
        adapter.submit(_request(), tmp_path / "s")
    assert "SECRET" not in str(first.value) and "a@b.example" not in str(first.value)
    assert len(notices.sent) == 1
    assert "studio providers clear-handoff gemini-web" in notices.sent[0].body

    # A second submit never dials and sends no second notice.
    with pytest.raises(OperatorHandoff):
        adapter.submit(_request(), tmp_path / "s")
    assert route.call_count == 1 and len(notices.sent) == 1

    # Every later run builds the provider as unavailable — not only until reset.
    built = build_adapters(_inventory(_row()), state_dir=tmp_path)
    assert "gemini-web" not in built.adapters
    assert "operator hand-off latched (captcha)" in built.unavailable["gemini-web"]

    assert latch.clear("gemini-web")
    assert "gemini-web" in build_adapters(_inventory(_row()), state_dir=tmp_path).adapters


def test_an_unreadable_latch_fails_closed(tmp_path: Path) -> None:
    from qontinui_studio.adapters.ui_bridge_wrapper import HandoffLatch

    latch = HandoffLatch(tmp_path)
    (tmp_path / "gemini-web.json").write_text("{not json")
    assert latch.describe("gemini-web") is not None


def test_clear_handoff_command(tmp_path: Path) -> None:
    from click.testing import CliRunner

    from qontinui_studio.adapters.ui_bridge_wrapper import HandoffLatch
    from qontinui_studio.cli import main
    from qontinui_studio.workspace import Workspace

    root = tmp_path / "studio"
    root.mkdir()
    (root / "providers.yaml").write_text(
        yaml.safe_dump({"note": "t", "operator_accounts_owed": False, "providers": [_row()]})
    )
    ws = Workspace(root)
    HandoffLatch(ws.handoffs_dir).set("gemini-web", "login_wall", "signed out")
    listed = CliRunner().invoke(main, ["--root", str(root), "providers", "list"])
    assert "operator hand-off latched (login_wall)" in listed.output
    r = CliRunner().invoke(main, ["--root", str(root), "providers", "clear-handoff", "gemini-web"])
    assert r.exit_code == 0, r.output
    assert "cleared the hand-off latch for gemini-web" in r.output
    assert "dist/cli.js clear-handoff" in r.output
    assert HandoffLatch(ws.handoffs_dir).get("gemini-web") is None


@respx.mock
def test_a_runner_503_parks_past_the_run_horizon(tmp_path: Path) -> None:
    respx.post(DISPATCH).mock(
        return_value=httpx.Response(
            503, json={"error": "spawn failed", "code": "wrapper_spawn_failed"}
        )
    )
    adapter = _adapter(tmp_path / "state")
    with pytest.raises(ProviderBusy) as busy:
        adapter.submit(_request(), tmp_path)
    assert busy.value.retry_after_s == 3600


@respx.mock
def test_no_conversation_url_reaches_the_handle(tmp_path: Path) -> None:
    respx.post(DISPATCH).mock(
        return_value=_answer(
            {
                "status": "done",
                "images": [{"b64_json": _png()}],
                "conversation_url": "https://chatgpt.com/c/abc",
            }
        )
    )
    job = _adapter(tmp_path / "state").submit(_request(), tmp_path)
    assert set(job.data) == {"staged"}


class Paced(FakeImageAdapter):
    def __init__(self, provider_id: str) -> None:
        super().__init__(provider_id, "fake-v1")
        self.calls = 0

    def submit(self, request: GenerationRequest, staging_dir: Path):  # type: ignore[no-untyped-def]
        self.calls += 1
        raise ProviderBusy("human-pace interval not yet elapsed", 45, code="paced")


def test_a_paced_answer_refunds_the_unit_and_falls_back(
    make_context: Callable[..., Context], write_brief: Callable[..., Path]
) -> None:
    paced = Paced("fake")
    ctx = make_context(
        adapters=AdapterSet(adapters={"fake": paced, "fake2": FakeImageAdapter("fake2", "fake-v2")})
    )
    out = drive_batch(
        ctx, new_batch(ctx, load_brief(write_brief(providers="fake, fake2"))), publish=False
    )
    assert out.status == "generated"
    assert paced.calls == 1
    assert ctx.conn.execute("SELECT provider FROM jobs").fetchone()["provider"] == "fake2"
    used = ctx.conn.execute(
        "SELECT COALESCE(SUM(used), 0) AS u FROM quota_usage WHERE provider='fake'"
    ).fetchone()
    assert used["u"] == 0  # the refused submission's unit was refunded


# --- review round 2: the real envelope, host-only URLs, studio-side spacing ----


@pytest.mark.parametrize(
    "body",
    [
        # the real two-layer shape (runner around the wrapper dispatch server)
        {"success": True, "data": {"result": {"success": True, "data": {"status": "cap_reached"}}}},
        # a wrapper that answers {"result": ...} itself: the runner unwraps it
        {"success": True, "data": {"result": {"status": "cap_reached"}}},
    ],
)
@respx.mock
def test_both_runner_envelopes_reach_the_handler_result(
    tmp_path: Path, body: dict[str, Any]
) -> None:
    respx.post(DISPATCH).mock(return_value=httpx.Response(200, json=body))
    with pytest.raises(QuotaExhaustedError):
        _adapter(tmp_path / "state").submit(_request(), tmp_path)


@respx.mock
def test_a_wrapper_failure_inside_a_2xx_is_unknown(tmp_path: Path) -> None:
    respx.post(DISPATCH).mock(
        return_value=httpx.Response(
            200,
            json={"success": True, "data": {"result": {"success": False, "error": "boom"}}},
        )
    )
    with pytest.raises(ProviderOutcomeUnknown, match="wrapper reported failure"):
        _adapter(tmp_path / "state").submit(_request(), tmp_path)


@respx.mock
def test_a_real_envelope_handoff_latches(tmp_path: Path) -> None:
    respx.post(DISPATCH).mock(
        return_value=_answer(
            {
                "status": "handoff",
                "reason": "login_wall",
                "detail": "went to https://gemini.google.com/app/8f3a2c1d9e?hl=en",
                "latched": True,
            }
        )
    )
    adapter = _adapter(tmp_path)
    with pytest.raises(OperatorHandoff) as e:
        adapter.submit(_request(), tmp_path / "s")
    assert e.value.reason == "login_wall"
    latch_text = (tmp_path / "handoffs" / "gemini-web.json").read_text()
    for text in (str(e.value), latch_text):
        assert "8f3a2c1d9e" not in text and "/app/" not in text
        assert "login_wall" in text


@respx.mock
def test_studio_alone_holds_the_pace_interval(tmp_path: Path) -> None:
    route = respx.post(DISPATCH).mock(
        return_value=_answer({"status": "done", "images": [{"b64_json": _png()}]})
    )
    now = [1_000_000.0]
    adapter = _adapter(tmp_path / "state", clock=lambda: now[0])
    adapter.submit(_request(), tmp_path / "a")
    now[0] += 30  # inside the row's 90 s interval
    with pytest.raises(ProviderBusy) as busy:
        adapter.submit(_request(), tmp_path / "b")
    assert busy.value.code == "paced" and busy.value.retry_after_s == 60
    assert route.call_count == 1  # refused without dialling
    now[0] += 61
    adapter.submit(_request(), tmp_path / "c")
    assert route.call_count == 2


# --- review round 3: the real gate, the real paced path ------------------------


def _chatgpt_row() -> dict[str, Any]:
    row = _row(
        id="chatgpt-web",
        provider="ChatGPT web (test)",
        wrapper_id="chatgpt-web",
        domains=["chatgpt.example"],
    )
    for k in ("api_available", "automation_allowed"):
        row[k] = {"value": "no", "source": "https://chatgpt.example/robots.txt"}
    row["commercial_use"] = {
        "value": "unknown",
        "source": "https://chatgpt.example/robots.txt",
        "note": "first-party terms unread (403)",
    }
    return row


def test_the_chatgpt_terms_snapshot_round_trips_its_unknown_licence() -> None:
    snap = TermsSnapshot.of(_inventory(_chatgpt_row()).providers[0])
    back = TermsSnapshot.from_json(snap.to_json())
    assert back == snap
    assert back.commercial_use == "unknown" and back.automation_allowed == "no"
    assert back.operator_override is not None


def _studio_with(studio_root: Path, *rows: dict[str, Any]) -> None:
    path = studio_root / "providers.yaml"
    inv = yaml.safe_load(path.read_text())
    inv["providers"].extend(rows)
    path.write_text(yaml.safe_dump(inv))


@respx.mock
def test_a_chatgpt_asset_is_refused_by_the_real_publication_gate(
    make_context: Callable[..., Context],
    write_brief: Callable[..., Path],
    studio_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from qontinui_studio.adapters import build_adapters
    from qontinui_studio.pipeline import run_brief

    monkeypatch.setenv("STUDIO_RUNNER_URL", RUNNER)  # respx answers it; no socket

    _studio_with(studio_root, _chatgpt_row())
    respx.post(f"{RUNNER}/wrappers/chatgpt-web/dispatch").mock(
        return_value=_answer({"status": "done", "images": [{"b64_json": _png(64, 36)}]})
    )
    ctx = make_context()
    ctx.adapters = build_adapters(
        ctx.inventory, only=["chatgpt-web"], state_dir=ctx.workspace.state_dir
    )
    assert "chatgpt-web" in ctx.adapters.adapters, ctx.adapters.unavailable
    outcome = run_brief(ctx, write_brief(providers="chatgpt-web"))
    assert outcome.status == "refused"
    assert outcome.publish is not None
    failing = [g for g in outcome.publish.failures if g.gate == "d5_licence"]
    assert failing and "commercial terms unknown" in failing[0].detail
    assert not (studio_root / "published" / "cafe" / "hero.webp").exists()


@respx.mock
def test_the_real_adapter_paced_refusal_falls_back_through_the_scheduler(
    make_context: Callable[..., Context],
    write_brief: Callable[..., Path],
    studio_root: Path,
    tmp_path: Path,
) -> None:
    from qontinui_studio.providers import OperatorOverride

    _studio_with(studio_root, _row())
    route = respx.post(DISPATCH).mock(
        return_value=_answer({"status": "done", "images": [{"b64_json": _png(64, 36)}]})
    )
    real = UIBridgeWrapperAdapter(
        "gemini-web",
        "m",
        "gemini-web",
        RUNNER,
        override=OperatorOverride.model_validate(OVERRIDE),
        state_dir=tmp_path / "state",
        clock=lambda: 1_000_000.0,  # frozen: the second job lands inside 90 s
    )
    ctx = make_context(
        adapters=AdapterSet(
            adapters={"gemini-web": real, "fake": FakeImageAdapter("fake", "fake-v1")}
        )
    )
    out = drive_batch(
        ctx,
        new_batch(ctx, load_brief(write_brief(providers="gemini-web, fake", variants=2))),
        publish=False,
    )
    assert out.status == "generated"
    assert route.call_count == 1  # the paced job never dialled
    providers = sorted(r["provider"] for r in ctx.conn.execute("SELECT provider FROM jobs"))
    assert providers == ["fake", "gemini-web"]
    used = ctx.conn.execute(
        "SELECT COALESCE(SUM(used), 0) AS u FROM quota_usage WHERE provider='gemini-web'"
    ).fetchone()
    assert used["u"] == 1  # the paced refusal's unit was refunded


# --- review round 4: nothing the wrapper or runner says is persisted ------------

LEAKS = [
    "https://gemini.google.com/app/8f3a2c1d9e?hl=en",
    "gemini.google.com/app/8f3a2c1d9e",
    "gemini.google.com:443/app/xyzconv",
    "HTTPS://chatgpt.com/c/abcconv?x=1",
    "chatgpt.com?next=/c/nextconv&acct=bobacct",
    "chatgpt.com#/c/fragconv",
    "//chatgpt.com/c/slashconv",
    "bücher.de/pfad/umlautsecret",
    "user:pwsecret@chatgpt.com/c/userinfoconv",
    "https://user@evil@chatgpt.com/c/doubleat",
    "[link](https://chatgpt.com/c/mdconv) trailing",
    "https://www.google.com/recaptcha/api2?k=SECRETKEY",
    "signed in as someone@mail.example",
]
# Every substring that must never reach a message, latch file or notice.
FORBIDDEN = [
    "8f3a2c1d9e",
    "xyzconv",
    "abcconv",
    "nextconv",
    "bobacct",
    "fragconv",
    "slashconv",
    "umlautsecret",
    "pwsecret",
    "userinfoconv",
    "doubleat",
    "mdconv",
    "SECRETKEY",
    "someone@mail.example",
    "/app/",
    "/c/",
    "evil",
    "bücher",
]


def _persisted(tmp_path: Path, *extra: str) -> str:
    texts = [p.read_text() for p in tmp_path.rglob("*.json")]
    return "\n".join([*texts, *extra])


@pytest.mark.parametrize("leak", LEAKS)
@respx.mock
def test_no_wrapper_handoff_detail_is_persisted(tmp_path: Path, leak: str) -> None:
    respx.post(DISPATCH).mock(
        return_value=_answer({"status": "handoff", "reason": "captcha", "detail": leak})
    )
    notices = _Notices()
    adapter = _adapter(tmp_path / "state", notifier=notices, known_host="gemini.google.com")
    with pytest.raises(OperatorHandoff) as e:
        adapter.submit(_request(), tmp_path / "s")
    notice = notices.sent[0]
    out = _persisted(tmp_path, str(e.value), notice.title, notice.body)
    for bad in FORBIDDEN:
        assert bad not in out, (leak, bad)
    assert "captcha" in out and "gemini.google.com" in str(e.value)  # the KNOWN host only
    latch = json.loads((tmp_path / "state" / "handoffs" / "gemini-web.json").read_text())
    assert set(latch) == {"reason", "at", "detail_omitted"}


@pytest.mark.parametrize("leak", LEAKS)
@pytest.mark.parametrize("status", [404, 500, 502, 503])
@respx.mock
def test_no_runner_error_text_is_persisted(tmp_path: Path, leak: str, status: int) -> None:
    respx.post(DISPATCH).mock(
        return_value=httpx.Response(status, json={"success": False, "error": leak})
    )
    with pytest.raises(Exception) as e:
        _adapter(tmp_path / "state").submit(_request(), tmp_path / "s")
    for bad in FORBIDDEN:
        assert bad not in str(e.value), (leak, bad)
    assert f"HTTP {status}" in str(e.value)


@pytest.mark.parametrize("leak", LEAKS)
@respx.mock
def test_no_wrapper_failure_or_status_text_is_persisted(tmp_path: Path, leak: str) -> None:
    for i, body in enumerate(
        (
            {"success": True, "data": {"result": {"success": False, "error": leak}}},
            {"success": True, "data": {"result": {"success": True, "data": {"status": leak}}}},
        )
    ):
        respx.post(DISPATCH).mock(return_value=httpx.Response(200, json=body))
        with pytest.raises(ProviderOutcomeUnknown) as e:
            _adapter(tmp_path / f"state{i}").submit(_request(), tmp_path / "s")
        for bad in FORBIDDEN:
            assert bad not in str(e.value), (leak, bad)
        assert "detail omitted" in str(e.value)


@respx.mock
def test_job_rows_carry_no_wrapper_text(
    make_context: Callable[..., Context],
    write_brief: Callable[..., Path],
    studio_root: Path,
    tmp_path: Path,
) -> None:
    from qontinui_studio.providers import OperatorOverride

    _studio_with(studio_root, _row())
    respx.post(DISPATCH).mock(
        return_value=_answer(
            {"status": "handoff", "reason": "login_wall", "detail": " ".join(LEAKS)}
        )
    )
    real = UIBridgeWrapperAdapter(
        "gemini-web",
        "m",
        "gemini-web",
        RUNNER,
        override=OperatorOverride.model_validate(OVERRIDE),
        state_dir=tmp_path / "state",
    )
    ctx = make_context(
        adapters=AdapterSet(
            adapters={"gemini-web": real, "fake": FakeImageAdapter("fake", "fake-v1")}
        )
    )
    drive_batch(
        ctx, new_batch(ctx, load_brief(write_brief(providers="gemini-web, fake"))), publish=False
    )
    rows = [
        " ".join(str(v) for v in r)
        for table in ("jobs", "job_attempts")
        for r in ctx.conn.execute(f"SELECT * FROM {table}")
    ]
    joined = "\n".join(rows)
    assert (
        json.loads((tmp_path / "state" / "handoffs" / "gemini-web.json").read_text())["reason"]
        == "login_wall"
    )  # the hand-off happened
    for bad in FORBIDDEN:
        assert bad not in joined, bad


# --- review round 5 -----------------------------------------------------------


@pytest.mark.parametrize("leak", LEAKS)
def test_the_gateway_refusal_never_prints_runner_text(leak: str) -> None:
    from qontinui_studio.gateway import GatewayState

    refusal = GatewayState("unknown", "unreadable", leak).refusal()
    assert refusal is not None
    for bad in FORBIDDEN:
        assert bad not in str(refusal), (leak, bad)
    assert "unreadable" in str(refusal) and "detail omitted" in str(refusal)


@respx.mock
def test_a_body_that_fails_decoding_is_unknown(tmp_path: Path) -> None:
    respx.post(DISPATCH).mock(side_effect=httpx.DecodingError("bad gzip"))
    with pytest.raises(ProviderOutcomeUnknown, match="DecodingError"):
        _adapter(tmp_path / "state").submit(_request(), tmp_path)


@respx.mock
def test_a_missing_error_reads_no_detail(tmp_path: Path) -> None:
    respx.post(DISPATCH).mock(return_value=httpx.Response(404, json={"success": False}))
    with pytest.raises(PermanentProviderError, match="no detail"):
        _adapter(tmp_path / "state").submit(_request(), tmp_path)


# --- review of the time budget -------------------------------------------------


@respx.mock
def test_an_untyped_timeout_is_a_refunded_refusal_not_a_handoff(tmp_path: Path) -> None:
    respx.post(DISPATCH).mock(
        return_value=_answer({"status": "timeout", "retry_after_s": 60, "typed": False})
    )
    adapter = _adapter(tmp_path / "state")
    with pytest.raises(ProviderBusy) as busy:
        adapter.submit(_request(), tmp_path)
    assert busy.value.code == "timeout" and busy.value.retry_after_s == 60
    assert not (tmp_path / "state" / "handoffs" / "gemini-web.json").exists()


@respx.mock
def test_a_typed_timeout_costs_an_attempt(tmp_path: Path) -> None:
    respx.post(DISPATCH).mock(
        return_value=_answer({"status": "timeout", "retry_after_s": 60, "typed": True})
    )
    with pytest.raises(ProviderOutcomeUnknown, match="after the prompt was sent"):
        _adapter(tmp_path / "state").submit(_request(), tmp_path)
    assert not (tmp_path / "state" / "handoffs" / "gemini-web.json").exists()


@respx.mock
def test_a_typed_timeout_spends_attempts_through_the_scheduler(
    make_context: Callable[..., Context],
    write_brief: Callable[..., Path],
    studio_root: Path,
    tmp_path: Path,
) -> None:
    """A site that is always too slow must not be retried for free: each typed
    timeout is an attempt, so the job moves on (to the next provider) once the
    provider's attempts are spent, and every one of those dispatches is on the
    record."""
    from qontinui_studio.providers import OperatorOverride
    from qontinui_studio.scheduler import RetryPolicy

    _studio_with(studio_root, _row())
    route = respx.post(DISPATCH).mock(
        return_value=_answer({"status": "timeout", "retry_after_s": 60, "typed": True})
    )
    ticks = iter(range(10**6))
    real = UIBridgeWrapperAdapter(
        "gemini-web",
        "m",
        "gemini-web",
        RUNNER,
        override=OperatorOverride.model_validate(OVERRIDE),
        state_dir=tmp_path / "state",
        clock=lambda: 1_000_000.0 + 1000.0 * next(ticks),
    )
    ctx = make_context(
        adapters=AdapterSet(
            adapters={"gemini-web": real, "fake": FakeImageAdapter("fake", "fake-v1")}
        ),
        policy=RetryPolicy(max_attempts_per_provider=2),
    )
    out = drive_batch(
        ctx,
        new_batch(ctx, load_brief(write_brief(providers="gemini-web, fake"))),
        publish=False,
    )
    assert out.status == "generated"
    rows = ctx.conn.execute(
        "SELECT provider, outcome FROM job_attempts WHERE provider='gemini-web'"
    ).fetchall()
    assert len(rows) == route.call_count >= 1
    assert all(r["outcome"] != "busy" for r in rows), [dict(r) for r in rows]
    assert ctx.conn.execute("SELECT provider FROM jobs").fetchone()["provider"] == "fake"


@respx.mock
def test_the_budget_is_never_below_one_second(tmp_path: Path) -> None:
    route = respx.post(DISPATCH).mock(
        return_value=_answer({"status": "done", "images": [{"b64_json": _png()}]})
    )
    _adapter(tmp_path / "state", dispatch_timeout_s=10.0).submit(_request(), tmp_path)
    assert json.loads(route.calls.last.request.content)["params"]["budgetS"] == 1


@respx.mock
def test_the_read_timeout_covers_everything_before_the_dispatch_clock(tmp_path: Path) -> None:
    """The timeout actually put on the wire leaves room for the runner's
    pre-dispatch ceiling on top of the dispatch timeout — read from the
    request httpx sent. The `== 100` line is a REGRESSION PIN of the shared
    contract value, not a behaviour test: qontinui-runner pins the same 100 s
    (`the_pre_dispatch_ceiling_is_the_documented_100s`) and no test can read
    both repos, so each side fails first when only one moves."""
    route = respx.post(DISPATCH).mock(
        return_value=_answer({"status": "done", "images": [{"b64_json": _png()}]})
    )
    adapter = _adapter(tmp_path / "state")
    adapter.submit(_request(), tmp_path)
    from qontinui_studio.adapters.ui_bridge_wrapper import RUNNER_PRE_DISPATCH_CEILING_S

    sent = route.calls.last.request.extensions["timeout"]
    # The runner's ceiling, as the single documented constant (pinned at 100 s
    # on the runner side too).
    assert RUNNER_PRE_DISPATCH_CEILING_S == 100
    assert sent["read"] >= adapter.dispatch_timeout_s + RUNNER_PRE_DISPATCH_CEILING_S
    assert sent["connect"] <= 10
    assert adapter.hard_timeout_s > sent["read"]


# The runner's busy refusal, byte for byte as qontinui-runner's
# wrappers/dispatch.rs `turn_and_spawn` builds it (BUSY_CODE "wrapper_busy",
# HTTP 429) for both of its causes.
RUNNER_BUSY_BODIES = [
    "wrapper_busy: wrapper 'gemini-web' action 'generate-image' is in use by another call; "
    "nothing was started",
    "wrapper_busy: wrapper 'gemini-web' is being started by another call; nothing was started",
]


@pytest.mark.parametrize(
    "body",
    [
        # the runner's envelope: the typed `code` field
        *({"success": False, "error": m, "code": "wrapper_busy"} for m in RUNNER_BUSY_BODIES),
        # a runner predating the field: the message prefix alone
        *({"success": False, "error": m} for m in RUNNER_BUSY_BODIES),
    ],
)
@respx.mock
def test_the_runners_busy_refusal_is_a_short_busy_not_a_parked_wrapper(
    tmp_path: Path, body: dict[str, Any]
) -> None:
    respx.post(DISPATCH).mock(return_value=httpx.Response(429, json=body))
    with pytest.raises(ProviderBusy) as busy:
        _adapter(tmp_path / "state").submit(_request(), tmp_path)
    assert busy.value.code == "wrapper_busy"
    assert busy.value.retry_after_s == 30  # like `paced`, never the 3600 s park
    assert "gemini-web' action" not in str(busy.value)  # the text is not echoed
    assert not (tmp_path / "state" / "handoffs" / "gemini-web.json").exists()


@respx.mock
def test_a_legacy_runners_spawn_failure_is_still_parked(tmp_path: Path) -> None:
    """A runner that predates the typed code: its own message prefix decides."""
    respx.post(DISPATCH).mock(
        return_value=httpx.Response(
            503, json={"success": False, "error": "failed to spawn wrapper 'gemini-web': x"}
        )
    )
    with pytest.raises(ProviderBusy) as busy:
        _adapter(tmp_path / "state").submit(_request(), tmp_path)
    assert busy.value.code == "wrapper_unavailable" and busy.value.retry_after_s == 3600


@respx.mock
def test_an_untyped_429_is_not_refunded(tmp_path: Path) -> None:
    """Only the runner's typed busy means "nothing started": any other 429 came
    from the wrapper, which may have sent the prompt — an outcome unknown
    (counted), neither a free retry nor a permanent refusal."""
    respx.post(DISPATCH).mock(
        return_value=httpx.Response(429, json={"success": False, "error": "too many requests"})
    )
    with pytest.raises(ProviderOutcomeUnknown):
        _adapter(tmp_path / "state").submit(_request(), tmp_path)


@respx.mock
def test_the_typed_code_alone_is_a_busy_whatever_the_message(tmp_path: Path) -> None:
    """The runner's `code` field decides, not the wording: a message without
    the old prefix is still the typed busy."""
    respx.post(DISPATCH).mock(
        return_value=httpx.Response(
            429,
            json={"success": False, "error": "try again shortly", "code": "wrapper_busy"},
        )
    )
    with pytest.raises(ProviderBusy) as busy:
        _adapter(tmp_path / "state").submit(_request(), tmp_path)
    assert busy.value.code == "wrapper_busy" and busy.value.retry_after_s == 30


@respx.mock
def test_a_wrapper_busy_code_from_the_wrapper_is_not_runner_busy(tmp_path: Path) -> None:
    """The runner never lets a wrapper's own body set the envelope `code`: it
    reports it as `wrapper_code=` in the message. A wrapper that says
    "wrapper_busy" is therefore NOT the runner's typed busy — not refunded,
    not retried for free: the prompt may already have been sent, so it is an
    outcome unknown, counted."""
    respx.post(DISPATCH).mock(
        return_value=httpx.Response(
            429,
            json={
                "success": False,
                "error": "wrapper 'gemini-web' returned 429 wrapper_code=wrapper_busy; "
                "body omitted(len=40, hmac=0123456789ab)",
            },
        )
    )
    with pytest.raises(ProviderOutcomeUnknown):
        _adapter(tmp_path / "state").submit(_request(), tmp_path)


@pytest.mark.parametrize(
    "body",
    [
        # A wrapper's own 503 through a legacy runner that passed statuses on.
        {
            "success": False,
            "error": "wrapper 'gemini-web' returned 503; body omitted(len=4, hmac=0)",
        },
        # A wrapper body that NAMES the runner's code: the runner reports it in
        # the message, never as the envelope code.
        {
            "success": False,
            "error": "wrapper 'gemini-web' returned 503 wrapper_code=wrapper_spawn_failed",
        },
    ],
)
@respx.mock
def test_an_untyped_503_is_an_outcome_unknown_not_a_refund(
    tmp_path: Path, body: dict[str, object]
) -> None:
    """Only the runner's typed spawn failure means "nothing reached the
    wrapper". A 503 without it may be a wrapper that had already sent the
    prompt: counted, never refunded and parked."""
    respx.post(DISPATCH).mock(return_value=httpx.Response(503, json=body))
    with pytest.raises(ProviderOutcomeUnknown):
        _adapter(tmp_path / "state").submit(_request(), tmp_path)


@pytest.mark.parametrize(
    "code", ["wrapper_spawn_failed", "wrappers_not_initialized", "primary_unreachable"]
)
@respx.mock
def test_every_typed_nothing_started_503_parks(tmp_path: Path, code: str) -> None:
    """A booting runner and a secondary whose request never reached the
    primary are "nothing started" too: refunded and parked, not counted."""
    respx.post(DISPATCH).mock(
        return_value=httpx.Response(503, json={"success": False, "error": "x", "code": code})
    )
    with pytest.raises(ProviderBusy) as busy:
        _adapter(tmp_path / "state").submit(_request(), tmp_path)
    assert busy.value.code == "wrapper_unavailable" and busy.value.retry_after_s == 3600


@respx.mock
def test_a_proxy_post_send_failure_is_counted(tmp_path: Path) -> None:
    """A secondary whose request reached the primary and then failed answers
    502: the prompt may have run, so it is counted."""
    respx.post(DISPATCH).mock(
        return_value=httpx.Response(
            502, json={"success": False, "error": "primary runner gave no usable answer"}
        )
    )
    with pytest.raises(ProviderOutcomeUnknown):
        _adapter(tmp_path / "state").submit(_request(), tmp_path)


@respx.mock
def test_the_typed_spawn_failure_parks_whatever_the_message(tmp_path: Path) -> None:
    respx.post(DISPATCH).mock(
        return_value=httpx.Response(
            503, json={"success": False, "error": "could not start", "code": "wrapper_spawn_failed"}
        )
    )
    with pytest.raises(ProviderBusy) as busy:
        _adapter(tmp_path / "state").submit(_request(), tmp_path)
    assert busy.value.code == "wrapper_unavailable" and busy.value.retry_after_s == 3600
