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
    return httpx.Response(200, json={"success": True, "data": {"result": result}, "error": None})


# --- the override in the inventory ---------------------------------------------


def test_a_wrapper_row_runs_only_under_its_override() -> None:
    with_override = _inventory(_row()).providers[0]
    assert with_override.automation_allowed.value == "no"  # the verdict stays truthful
    assert with_override.adapter_gate() is None
    assert with_override.schedulable_reason() is None

    bare = _inventory(_row(operator_override=None, daily_quota=10)).providers[0]
    assert bare.adapter_gate() == "automation_allowed is 'no'"


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
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _inventory(_row()).providers[0]
    monkeypatch.setenv("STUDIO_RUNNER_URL", "https://runner.example.com")
    with pytest.raises(AdapterDisabledError, match="not on this machine or the local network"):
        build_adapter(provider)
    monkeypatch.setenv("STUDIO_RUNNER_URL", RUNNER)
    adapter = build_adapter(provider)
    assert isinstance(adapter, UIBridgeWrapperAdapter)
    assert adapter.runner_url == RUNNER


@respx.mock
def test_done_stages_marked_images(tmp_path: Path) -> None:
    route = respx.post(DISPATCH).mock(
        return_value=_answer({"status": "done", "images": [{"b64_json": _png()}]})
    )
    adapter = UIBridgeWrapperAdapter("gemini-web", "gemini-web-default", "gemini-web", RUNNER)
    job = adapter.submit(_request(), tmp_path / "stage")
    sent = route.calls.last.request
    assert sent.headers["X-Wrapper-Dispatch-Timeout"] == "300"
    assert json.loads(sent.content) == {
        "action": "generate-image",
        "params": {"prompt": "a red square", "aspectRatio": "1:1"},
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
    adapter = UIBridgeWrapperAdapter("gemini-web", "m", "gemini-web", RUNNER)
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
    adapter = UIBridgeWrapperAdapter("gemini-web", "m", "gemini-web", RUNNER)
    with pytest.raises(error):
        adapter.submit(_request(), tmp_path)


@respx.mock
def test_transport_failures_split_on_whether_the_prompt_left(tmp_path: Path) -> None:
    adapter = UIBridgeWrapperAdapter("gemini-web", "m", "gemini-web", RUNNER)
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
