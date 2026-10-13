"""The build-record client: D3 allowlist pinned key for key, typed
no-record outcomes, digest verification, unknowns carried through."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

from qontinui_studio.launch.facts import derive_facts, facts_by_key, format_duration
from qontinui_studio.launch.record import (
    ALLOWED_PATHS,
    BuildRecordClient,
    BuildRecordInvalid,
    FetchedRecord,
    NoBuildRecord,
    allowlist_violations,
    canonical_sha256,
    load_record_file,
    parse_document,
)

from .launch_support import REPO, SLUG, document

WEB = "https://api.qontinui.io"
PUBLIC = f"{WEB}/api/v1/public/build-records/{SLUG}"
AUTHED = f"{WEB}/api/v1/build-records/{SLUG}"

#: The Phase 1 contract's build-record/1 key set, written out by hand: a change
#: to the allowlist in either direction must change this test too.
CONTRACT_PATHS = {
    "schema",
    "generated_at",
    "product",
    "product.slug",
    "product.title",
    "product.repos",
    "product.window_start",
    "product.window_end",
    "work_units",
    "work_units[].slug",
    "work_units[].title",
    "work_units[].status",
    "work_units[].vetted_at",
    "work_units[].shipped_at",
    "timeline",
    "timeline[].at",
    "timeline[].work_unit",
    "timeline[].from_status",
    "timeline[].to_status",
    "prs",
    "prs[].repo",
    "prs[].number",
    "prs[].title",
    "prs[].opened_at",
    "prs[].landed_at",
    "prs[].ci_duration_secs",
    "prs[].attempts",
    "gates",
    "gates.registered",
    "gates.cleared",
    "sessions",
    "sessions.count",
    "sessions.unknown_reason",
    "wall_clock_secs",
    "self_corrections",
    "self_corrections.red_ci_fixed",
    "self_corrections.review_findings_fixed",
    "self_corrections.gate_reopens",
    "unknowns",
}


@pytest.fixture(autouse=True)
def _no_web_bearer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("STUDIO_WEB_BEARER", raising=False)
    monkeypatch.delenv("STUDIO_WEB_BEARER_FILE", raising=False)


def test_allowlist_is_the_contract_key_set() -> None:
    assert ALLOWED_PATHS == CONTRACT_PATHS


def test_fixture_document_passes() -> None:
    assert allowlist_violations(document()) == []
    record = parse_document(document())
    assert record.product.repos == [REPO]


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (lambda d: d.update(transcript="..."), "<unlisted key>: key is not in"),
        (lambda d: d["product"].update(owner_email="a@b.c"), "product.<unlisted key>"),
        (lambda d: d["prs"][0].update(device_id="x"), "prs[0].<unlisted key>"),
        (lambda d: d["sessions"].update(names=["alice"]), "sessions.<unlisted key>"),
        (
            lambda d: d["gates"].update(registered={"nested": 1}),
            "gates.registered: expected a scalar",
        ),
        (
            lambda d: d["prs"].append({**d["prs"][0], "repo": "acme/private"}),
            "prs[2].repo: not one of",
        ),
        (lambda d: d.update(schema="build-record/2"), "schema: not a valid schema value"),
    ],
)
def test_any_extra_key_or_private_repo_is_refused(mutate: object, expected: str) -> None:
    doc = document()
    mutate(doc)  # type: ignore[operator]
    violations = allowlist_violations(doc)
    assert any(expected in v for v in violations), violations
    with pytest.raises(BuildRecordInvalid):
        parse_document(doc)


def test_type_errors_are_refused_not_coerced() -> None:
    doc = document()
    doc["prs"][0]["attempts"] = "2"
    with pytest.raises(BuildRecordInvalid, match="attempts"):
        parse_document(doc)


def _wrapper(doc: dict[str, object], digest: str | None = None) -> dict[str, object]:
    return {
        "public_slug": SLUG,
        "version": 3,
        "published_at": "2026-10-09T18:05:00Z",
        "content_sha256": digest or canonical_sha256(doc),
        "document": doc,
    }


@respx.mock
def test_public_snapshot_fetched_and_digest_verified() -> None:
    doc = document()
    respx.get(PUBLIC).mock(return_value=httpx.Response(200, json=_wrapper(doc)))
    outcome = BuildRecordClient(web_base=WEB).fetch_public(SLUG)
    assert isinstance(outcome, FetchedRecord)
    assert outcome.version == 3 and outcome.source == "public"
    assert outcome.content_sha256 == canonical_sha256(doc)


@respx.mock
def test_public_snapshot_with_wrong_digest_is_refused() -> None:
    respx.get(PUBLIC).mock(return_value=httpx.Response(200, json=_wrapper(document(), "0" * 64)))
    with pytest.raises(BuildRecordInvalid, match="content_sha256"):
        BuildRecordClient(web_base=WEB).fetch_public(SLUG)


@respx.mock
def test_public_snapshot_with_extra_key_is_refused() -> None:
    doc = document()
    doc["operator"] = "someone"
    respx.get(PUBLIC).mock(return_value=httpx.Response(200, json=_wrapper(doc)))
    with pytest.raises(BuildRecordInvalid, match="unlisted key"):
        BuildRecordClient(web_base=WEB).fetch_public(SLUG)


@respx.mock
def test_404_is_a_typed_no_record_outcome() -> None:
    respx.get(PUBLIC).mock(return_value=httpx.Response(404, json={"detail": "Not Found"}))
    outcome = BuildRecordClient(web_base=WEB).fetch_public(SLUG)
    assert isinstance(outcome, NoBuildRecord)
    assert outcome.kind == "not_found"
    assert "no build record yet" in outcome.describe()


@respx.mock
def test_unreachable_is_a_typed_no_record_outcome() -> None:
    respx.get(PUBLIC).mock(side_effect=httpx.ConnectError("refused"))
    outcome = BuildRecordClient(web_base=WEB).fetch_public(SLUG)
    assert isinstance(outcome, NoBuildRecord) and outcome.kind == "unreachable"
    respx.get(PUBLIC).mock(return_value=httpx.Response(503, text="down"))
    outcome = BuildRecordClient(web_base=WEB).fetch_public(SLUG)
    assert isinstance(outcome, NoBuildRecord) and outcome.kind == "unreachable"


def test_authed_door_without_a_bearer_is_no_credential() -> None:
    outcome = BuildRecordClient(web_base=WEB).fetch_authed(SLUG)
    assert isinstance(outcome, NoBuildRecord) and outcome.kind == "no_credential"


@respx.mock
def test_authed_door_sends_bearer_and_reads_bare_document(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    token_file = tmp_path / "token"
    token_file.write_text("tok-123\n")
    monkeypatch.setenv("STUDIO_WEB_BEARER_FILE", str(token_file))
    route = respx.get(AUTHED).mock(return_value=httpx.Response(200, json=document()))
    outcome = BuildRecordClient(web_base=WEB).fetch_authed(SLUG)
    assert isinstance(outcome, FetchedRecord) and outcome.source == "authed"
    assert route.calls.last.request.headers["Authorization"] == "Bearer tok-123"

    respx.get(AUTHED).mock(return_value=httpx.Response(401, text="expired"))
    outcome = BuildRecordClient(web_base=WEB).fetch_authed(SLUG)
    assert isinstance(outcome, NoBuildRecord) and outcome.kind == "unauthorized"


def test_record_file_bare_or_wrapped(tmp_path: Path) -> None:
    bare = tmp_path / "bare.json"
    bare.write_text(json.dumps(document()))
    wrapped = tmp_path / "wrapped.json"
    wrapped.write_text(json.dumps(_wrapper(document())))
    assert load_record_file(bare).content_sha256 == load_record_file(wrapped).content_sha256
    assert load_record_file(wrapped).version == 3


def test_unknowns_are_never_zero() -> None:
    record = parse_document(document())
    facts = facts_by_key(record)
    # Null in the record -> unknown with the record's own reason code, in words.
    assert facts["sessions"].value is None
    assert facts["sessions"].display() == (
        "unknown — the session census is still provisional (census_provisional)"
    )
    assert facts["review_findings_fixed"].display() == (
        "unknown — the factory could not establish it (not_established)"
    )
    assert facts["gate_reopens"].display() == (
        "unknown — the factory's reader for it was unavailable (door_unavailable)"
    )
    # A sum over PRs where one addend is null is UNKNOWN, not a partial total.
    assert facts["ci_attempts"].value is None
    assert "1 of 2 PR(s) lack it" in facts["ci_attempts"].display()
    assert "(not_established)" in facts["ci_secs"].display()
    for fact in derive_facts(record):
        if not fact.known:
            assert "0" != fact.display() and fact.display().startswith("unknown — ")
    # Known values pass straight through.
    assert facts["prs_landed"].display() == "2"
    assert facts["red_ci_fixed"].display() == "1"
    assert facts["wall_clock"].display() == format_duration(32400) == "9h 0m"


#: coord's reason codes (UNKNOWN_REASON_CODES in qontinui-web's validator).
CODES = (
    "not_established",
    "door_unavailable",
    "excluded_not_known_public",
    "census_provisional",
    "not_applicable",
)


def test_reason_codes_are_coords() -> None:
    from qontinui_studio.launch.allowlist import UNKNOWN_REASON_CODES
    from qontinui_studio.launch.record import REASON_PHRASES

    assert set(CODES) == UNKNOWN_REASON_CODES == set(REASON_PHRASES)


def test_review_d1_failed_list_readers_are_unknown_never_zero() -> None:
    """Review finding 1 (scripts/d1.py): what coord emits when the work-unit
    membership reader FAILED — empty lists with door_unavailable — must render
    as unknown, never as 0 plans / 0 PRs."""
    doc = document()
    doc["work_units"], doc["timeline"], doc["prs"] = [], [], []
    doc["gates"] = {"registered": None, "cleared": None}
    doc["wall_clock_secs"] = None
    doc["unknowns"] = [
        "work_units: door_unavailable",
        "prs: door_unavailable",
        "timeline: door_unavailable",
        "gates.registered: not_established",
        "gates.cleared: not_established",
    ]
    facts = facts_by_key(parse_document(doc))
    for key in ("work_units", "work_units_shipped", "prs", "prs_landed", "ci_attempts", "ci_secs"):
        assert facts[key].value is None, key
        assert facts[key].display().endswith("(door_unavailable)"), facts[key].display()
    assert facts["gates_registered"].display().endswith("(not_established)")


@pytest.mark.parametrize("code", [c for c in CODES if c != "not_established"])
def test_any_hard_code_on_a_list_makes_it_unknown(code: str) -> None:
    doc = document()
    doc["unknowns"].append(f"prs: {code}")
    facts = facts_by_key(parse_document(doc))
    assert facts["prs"].value is None and facts["prs"].display().endswith(f"({code})")


def test_not_established_on_a_non_empty_list_is_a_lower_bound() -> None:
    doc = document()
    doc["unknowns"].append("work_units: not_established")
    facts = facts_by_key(parse_document(doc))
    assert facts["work_units"].display() == "≥ 1"
    assert facts["work_units_shipped"].display() == "≥ 1"
    empty = document()
    empty["work_units"], empty["timeline"] = [], []
    empty["unknowns"].append("work_units: not_established")
    facts = facts_by_key(parse_document(empty))
    assert facts["work_units"].value is None


@pytest.mark.parametrize(
    "web_base",
    ["http://api.qontinui.io", "https://evil.example", "http://127.0.0.1:47999"],
)
def test_review_bearer_goes_only_to_https_allowlisted_hosts(
    monkeypatch: pytest.MonkeyPatch, web_base: str
) -> None:
    """Review finding 7 (scripts/bearer.py): with a web bearer configured, an
    authed door on plain http or another host must not receive it."""
    monkeypatch.setenv("STUDIO_WEB_BEARER", "fake-web-bearer-xyz")
    with respx.mock(assert_all_called=False) as mock:
        route = mock.route().mock(return_value=httpx.Response(200, json=document()))
        outcome = BuildRecordClient(web_base=web_base).fetch_authed(SLUG)
    assert isinstance(outcome, NoBuildRecord) and outcome.kind == "refused_door"
    assert not route.called


def test_review_d3_the_web_validator_is_applied() -> None:
    """Review finding 3 (scripts/d3.py): a document qontinui-web would refuse
    (private repo name, operator email, device UUID, negative CI duration,
    free-text unknowns) is refused here too — never rendered."""
    doc = document()
    doc["work_units"][0]["title"] = "port acme/secret-billing for joshua@spaceship"
    doc["prs"][0]["title"] = "device 0b7c2a1e-1111-4222-8333-444455556666"
    doc["prs"][0]["ci_duration_secs"] = -5
    doc["sessions"]["unknown_reason"] = "operator joshua on host spaceship never ran the census"
    doc["unknowns"] = ["free text: the findings body says acme/secret-billing is broken"]
    with pytest.raises(BuildRecordInvalid) as e:
        parse_document(doc)
    joined = "; ".join(e.value.violations)
    for slot in (
        "work_units[0].title: names an owner/name not in product.repos",
        "work_units[0].title: contains an email-shaped identity",
        "prs[0].title: contains a UUID-shaped identifier",
        "prs[0].ci_duration_secs: not a valid count value",
        "sessions.unknown_reason: not a valid unknown_or_null value",
        "unknowns[0]: not a valid unknown value",
    ):
        assert slot in joined
    # Messages name slots, never the refused values.
    assert "acme/secret-billing" not in joined and "joshua" not in joined


def test_review_squat_a_record_for_another_product_is_refused() -> None:
    """Review finding 4 (scripts/squat.py): the public door serving another
    product's document under our slug, or listing repos we do not declare."""
    from qontinui_studio.launch.record import identity_violations

    from .launch_support import wrapper

    doc = document()
    doc["product"]["slug"] = "someone-else"
    with respx.mock:
        respx.get(PUBLIC).mock(return_value=httpx.Response(200, json=wrapper(doc)))
        outcome = BuildRecordClient(web_base=WEB).fetch_public(SLUG)
    assert isinstance(outcome, FetchedRecord)
    problems = identity_violations(outcome, SLUG, [REPO])
    assert any("record is for product 'someone-else'" in p for p in problems)

    doc = document()
    doc["product"]["repos"] = [REPO, "mallory/fake"]
    with respx.mock:
        respx.get(PUBLIC).mock(
            return_value=httpx.Response(200, json=wrapper(doc, public_slug="other"))
        )
        outcome = BuildRecordClient(web_base=WEB).fetch_public(SLUG)
    assert isinstance(outcome, FetchedRecord)
    problems = identity_violations(outcome, SLUG, [REPO])
    assert any("published as 'other'" in p for p in problems)
    assert any("does not declare" in p for p in problems)
