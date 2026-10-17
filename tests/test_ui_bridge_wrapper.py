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


def _png() -> str:
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), (200, 40, 40)).save(buf, format="PNG")
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
        (httpx.Response(503, json={"success": False, "error": "spawn failed"}), ProviderBusy),
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


# --- review round 1: bounds on the wire, the latch, the notice, redaction ------


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


def test_redact_strips_paths_queries_and_emails_and_bounds_length() -> None:
    from qontinui_studio.adapters.ui_bridge_wrapper import MAX_DETAIL_CHARS, redact

    out = redact("see https://accounts.google.com/signin?continue=x#f for me@mail.example")
    assert out == "see https://accounts.google.com for <email>"
    assert len(redact("x" * 5000)) == MAX_DETAIL_CHARS


@respx.mock
def test_a_runner_503_parks_past_the_run_horizon(tmp_path: Path) -> None:
    respx.post(DISPATCH).mock(return_value=httpx.Response(503, json={"error": "spawn failed"}))
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
        assert "https://gemini.google.com" in text


def test_redact_keeps_only_scheme_and_host() -> None:
    from qontinui_studio.adapters.ui_bridge_wrapper import redact

    assert redact("at https://chatgpt.com/c/abc-123?x=1 now") == "at https://chatgpt.com now"


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
