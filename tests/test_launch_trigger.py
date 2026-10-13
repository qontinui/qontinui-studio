"""`studio launch register-trigger` against mocked coord doors. No live gate is registered."""

from __future__ import annotations

import json

import httpx
import pytest
import respx
from click.testing import CliRunner

from qontinui_studio.cli import main
from qontinui_studio.launch.trigger import TriggerError, gate_body, register_trigger

COORD = "https://coord.qontinui.io"
SLUG = "2026-10-09-factory-built-product-portfolio-and-launch-kit"
WU = "6f1c5c2e-0b8a-4d43-9a3e-3f3e0c1d2b4a"
GATE = "9b2f4a1e-77aa-4c3d-8e5f-0a1b2c3d4e5f"


@pytest.fixture(autouse=True)
def _bearer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STUDIO_COORD_BEARER", "jwt-abc")
    monkeypatch.delenv("STUDIO_COORD_BEARER_FILE", raising=False)


def _mock(
    listed: list[dict[str, object]] | None = None, list_status: int = 200
) -> dict[str, respx.Route]:
    routes = {
        "resolve": respx.get(f"{COORD}/coord/agent-work-units/{SLUG}").mock(
            return_value=httpx.Response(
                200, json={"work_unit": {"id": WU, "slug": SLUG, "status": "in_progress"}}
            )
        ),
        "register": respx.post(f"{COORD}/coord/work-units/{SLUG}/register-gate").mock(
            return_value=httpx.Response(
                201,
                json={
                    "gate_id": GATE,
                    "work_unit_id": WU,
                    "warnings": [],
                    "initial_verdict": "open",
                },
            )
        ),
        "list": respx.get(f"{COORD}/coord/agent-gates").mock(
            return_value=httpx.Response(
                list_status,
                json={"gates": listed if listed is not None else [{"gate_id": GATE}], "total": 1},
            )
        ),
    }
    return routes


def test_gate_body_shape() -> None:
    body = gate_body(WU, "qontinui-studio")
    assert body["predicate"] == {"kind": "unit_status", "work_unit_id": WU, "status": "shipped"}
    cont = body["continuation_spawn"]
    assert cont["action"] == "run_skill" and cont["skill"] == "launch-kit"
    assert cont["args"] == ["qontinui-studio"] and cont["repos"] == ["qontinui-studio"]
    assert body["phase_name"] == "launch-kit"


@respx.mock
def test_register_resolves_registers_and_reads_back() -> None:
    routes = _mock()
    result = register_trigger(SLUG, "qontinui-studio", base_url=COORD)
    assert result.gate_id == GATE and result.work_unit_id == WU
    assert result.read_back == "confirmed"
    # The unit is resolved through the READ door: a GET, nothing written.
    assert routes["resolve"].calls.last.request.method == "GET"
    sent = json.loads(routes["register"].calls.last.request.content)
    assert sent["predicate"]["work_unit_id"] == WU
    assert routes["register"].calls.last.request.headers["Authorization"] == "Bearer jwt-abc"
    assert routes["list"].calls.last.request.url.params["work_unit_id"] == WU


@respx.mock
def test_read_back_that_omits_the_gate_is_absent_not_assumed() -> None:
    _mock(listed=[{"gate_id": "other"}])
    assert register_trigger(SLUG, "qontinui-studio", base_url=COORD).read_back == "absent"


@respx.mock
def test_read_back_failure_is_unreadable() -> None:
    _mock(list_status=503)
    result = register_trigger(SLUG, "qontinui-studio", base_url=COORD)
    assert result.read_back == "unreadable" and "503" in result.read_back_detail


@respx.mock
def test_refusals_name_their_stage() -> None:
    respx.get(f"{COORD}/coord/agent-work-units/{SLUG}").mock(
        return_value=httpx.Response(401, text="expired")
    )
    with pytest.raises(TriggerError) as e:
        register_trigger(SLUG, "qontinui-studio", base_url=COORD)
    assert e.value.stage == "resolve"


@respx.mock
def test_an_unknown_slug_is_refused_and_nothing_is_written() -> None:
    """Review finding 8: registration never creates a work unit."""
    respx.get(f"{COORD}/coord/agent-work-units/{SLUG}").mock(
        return_value=httpx.Response(404, json={"error": "no work-unit with that slug"})
    )
    upsert = respx.post(f"{COORD}/coord/work-units/upsert").mock(
        return_value=httpx.Response(200, json={})
    )
    register = respx.post(f"{COORD}/coord/work-units/{SLUG}/register-gate").mock(
        return_value=httpx.Response(201, json={})
    )
    with pytest.raises(TriggerError, match="never creates one") as e:
        register_trigger(SLUG, "qontinui-studio", base_url=COORD)
    assert e.value.stage == "resolve"
    assert not upsert.called and not register.called


def test_no_bearer_is_a_typed_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("STUDIO_COORD_BEARER")
    with pytest.raises(TriggerError, match="no coord bearer"):
        register_trigger(SLUG, "qontinui-studio", base_url=COORD)


@respx.mock
def test_cli_prints_gate_id_and_read_back() -> None:
    _mock()
    r = CliRunner().invoke(
        main,
        [
            "launch",
            "register-trigger",
            "--work-unit-slug",
            SLUG,
            "--product",
            "qontinui-studio",
            "--coord-base",
            COORD,
        ],
    )
    assert r.exit_code == 0, r.output
    assert f"gate_id {GATE}" in r.output and "read_back confirmed" in r.output

    _mock(listed=[])
    r = CliRunner().invoke(
        main,
        [
            "launch",
            "register-trigger",
            "--work-unit-slug",
            SLUG,
            "--product",
            "x",
            "--coord-base",
            COORD,
        ],
    )
    assert r.exit_code == 5 and "read_back absent" in r.output


#: coord's ``UnitGateRequest`` (qontinui-coord ``crates/coord/src/api/gate_routes.rs``
#: at origin/main e22796152: ``#[serde(deny_unknown_fields)]``, fields predicate,
#: phase_name, continuation_spawn, clearance_audience, expires_at, gate_class) and
#: its ``GatePredicate::UnitStatus { work_unit_id: Uuid, status: String }``
#: (``gates.rs``, ``#[serde(tag = "kind", rename_all = "snake_case")]``).
UNIT_GATE_REQUEST_FIELDS = frozenset(
    {
        "predicate",
        "phase_name",
        "continuation_spawn",
        "clearance_audience",
        "expires_at",
        "gate_class",
    }
)
REQUIRED_FIELDS = frozenset({"predicate", "phase_name"})


@respx.mock
def test_cli_register_trigger_sends_coords_unit_gate_request() -> None:
    """The detect_deliver arming test: enters through ``studio launch
    register-trigger`` and checks the body coord receives against its real
    UnitGateRequest (deny_unknown_fields), the unit_status predicate and the
    run_skill continuation coord parses at clearance."""
    import uuid

    routes = _mock()
    r = CliRunner().invoke(
        main,
        [
            "launch", "register-trigger", "--work-unit-slug", SLUG,
            "--product", "qontinui-studio", "--coord-base", COORD,
        ],
    )  # fmt: skip
    assert r.exit_code == 0, r.output
    body = json.loads(routes["register"].calls.last.request.content)
    assert set(body) <= UNIT_GATE_REQUEST_FIELDS and REQUIRED_FIELDS <= set(body)
    predicate = body["predicate"]
    assert set(predicate) == {"kind", "work_unit_id", "status"}
    assert predicate["kind"] == "unit_status" and predicate["status"] == "shipped"
    assert uuid.UUID(predicate["work_unit_id"]) == uuid.UUID(WU)
    assert isinstance(body["phase_name"], str) and body["phase_name"]
    cont = body["continuation_spawn"]
    assert cont["action"] == "run_skill" and cont["skill"] == "launch-kit"
    assert all(isinstance(a, str) for a in cont["args"]) and isinstance(cont["hint"], str)
    assert "clearance_audience" not in body or body["clearance_audience"] in ("operator", "agent")


@pytest.mark.parametrize(
    "base", ["http://coord.qontinui.io", "https://coord.example.test", "https://127.0.0.1:9876"]
)
def test_a_coord_base_off_the_allowlist_never_gets_the_bearer(base: str) -> None:
    with respx.mock(assert_all_called=False) as mock:
        route = mock.route().mock(return_value=httpx.Response(200, json={}))
        with pytest.raises(TriggerError, match="only over https"):
            register_trigger(SLUG, "qontinui-studio", base_url=base)
    assert not route.called


def test_the_slug_is_quoted_in_every_path() -> None:
    odd = "plan/../x y"
    seen: list[bytes] = []

    def answer(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.raw_path)
        if request.url.raw_path.startswith(b"/coord/agent-work-units/"):
            return httpx.Response(200, json={"work_unit": {"id": WU}})
        if request.url.raw_path.endswith(b"/register-gate"):
            return httpx.Response(201, json={"gate_id": GATE, "work_unit_id": WU})
        return httpx.Response(200, json={"gates": [{"gate_id": GATE}]})

    client = httpx.Client(transport=httpx.MockTransport(answer))
    result = register_trigger(odd, "qontinui-studio", base_url=COORD, http=client)
    assert result.read_back == "confirmed"
    assert seen[0] == b"/coord/agent-work-units/plan%2F..%2Fx%20y"
    assert seen[1] == b"/coord/work-units/plan%2F..%2Fx%20y/register-gate"
