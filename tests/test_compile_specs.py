"""Image-slot specs as ``studio compile`` reads them (plan 2026-10-13 Phase 19
step 1): the generated ImageSlotSpec model validated strictly, the cross-field
rules mirrored from qontinui-schemas, and the RFC 8785 content hash checked
against every golden the Rust type pins.

The fixtures under ``fixtures/image_slot`` are byte copies of qontinui-schemas
``rust/tests/fixtures/image_slot`` at c415a432 (the commit pyproject pins)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from qontinui_studio.compiler.jcs import CanonicalizationError, canonicalize
from qontinui_studio.compiler.specs import brief_slug, load_spec, load_specs, read_spec
from qontinui_studio.errors import ConfigError

FIXTURES = Path(__file__).parent / "fixtures" / "image_slot"
VALID = sorted((FIXTURES / "valid").glob("*.yaml"))
INVALID = sorted((FIXTURES / "invalid").glob("*.yaml"))


def pinned(path: Path) -> str:
    found = re.findall(r"^# content-sha256: ([0-9a-f]{64})$", path.read_text(), re.M)
    assert len(found) == 1, path
    return str(found[0])


@pytest.mark.parametrize("path", VALID, ids=lambda p: p.stem)
def test_every_valid_fixture_hashes_to_the_rust_golden(path: Path) -> None:
    loaded = load_spec(path)
    assert loaded.sha256 == pinned(path)
    assert loaded.slot_id == path.stem


def test_the_golden_set_is_not_empty() -> None:
    # A parametrised test over an empty glob passes vacuously.
    assert len(VALID) == 7 and len(INVALID) == 32


# Every invalid fixture is refused, for ITS reason (a refusal for some other
# reason would hide a rule the mirror lost).
REASONS = {
    "ai-generated-logo": "a logo is never AI-generated",
    "blank-job": "job",
    "colon-aspect-ratio": "aspect.preferred",
    "contradictory-content": "contradictory",
    "current-binding-without-alt-text": "alt_text",
    "current-binding-without-exemplars-digest": "exemplars_sha256",
    "current-binding-without-lora-digest": "lora_sha256",
    "dpr-not-ascending": "contexts.dpr: not strictly ascending",
    "dpr-out-of-range": "contexts.dpr",
    "duplicate-aspect-ratio": "duplicate (equal in lowest terms)",
    "focal-region-past-the-edge": "focal.region",
    "infinite-threshold": "max_aspect_delta",
    "malformed-locale": "locale",
    "max-bytes-above-safe-integer": "max_bytes",
    "missing-aspect-allowed": "aspect.allowed",
    "missing-expected-text": "text",
    "missing-job": "job",
    "missing-union-kind": "alt",
    "misspelt-nested-field": "must_survive_crops",
    "no-breakpoints": "breakpoints",
    "overrides-naming-nothing": "overrides names no threshold",
    "short-asset-digest": "asset_sha256",
    "stray-key-on-data-less-arm": "alt.decorative.text",
    "uncaptured-screenshot": "a screenshot is captured",
    "unknown-role": "role",
    "unknown-theme": "themes",
    "unknown-top-level-field": "colour",
    "unknown-union-kind": "text_in_image",
    "unmarked-generated-asset": "a model-made asset must be marked",
    "unprefixed-audience": "audience",
    "uppercase-slot-id": "slot",
    "wrong-value-type": "must_survive_crop",
}


@pytest.mark.parametrize("path", INVALID, ids=lambda p: p.stem)
def test_every_invalid_fixture_is_refused_for_its_reason(path: Path) -> None:
    with pytest.raises(ConfigError) as e:
        read_spec(path)
    assert REASONS[path.stem] in str(e.value), str(e.value)


def test_a_yaml_1_1_boolean_is_refused_not_coerced(tmp_path: Path) -> None:
    text = (FIXTURES / "valid" / "sessions.empty-state.yaml").read_text()
    # PyYAML reads an unquoted `yes` as True; strict validation refuses it
    # where the contract wants a boolean only because it IS one — but `job`
    # wants a string, and True is not one.
    bad = tmp_path / "sessions.empty-state.yaml"
    bad.write_text(re.sub(r"^job: .*$", "job: yes", text, flags=re.M))
    with pytest.raises(ConfigError, match="job"):
        load_spec(bad)


def test_a_file_is_named_after_its_slot(tmp_path: Path) -> None:
    src = FIXTURES / "valid" / "sessions.empty-state.yaml"
    (tmp_path / "renamed.yaml").write_text(src.read_text())
    with pytest.raises(ConfigError, match="named after its slot"):
        load_spec(tmp_path / "renamed.yaml")
    assert read_spec(tmp_path / "renamed.yaml").slot_id == "sessions.empty-state"


def test_load_specs_refuses_a_missing_directory_and_sorts(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="no image-slots directory"):
        load_specs(tmp_path / "absent")
    for name in ("sessions.empty-state", "marketing.home.hero"):
        (tmp_path / f"{name}.yaml").write_text((FIXTURES / "valid" / f"{name}.yaml").read_text())
    assert [s.slot_id for s in load_specs(tmp_path)] == [
        "marketing.home.hero",
        "sessions.empty-state",
    ]


SLUG = r"^[a-z0-9]+(?:[._-][a-z0-9]+)*$"  # qontinui-schemas SLUG_PATTERN


@pytest.mark.parametrize(
    "slot", ["runs.active.idle", "sessions.empty-state", "a_b.c-d", "x", "a1.b2.c3"]
)
def test_every_spec_slot_id_is_a_site_slot_name(slot: str) -> None:
    """PD3: studio accepts every slot id the ImageSlotSpec contract allows, as
    the site slot, the file stem and the manifest key."""
    from qontinui_studio.sites import SLOT_PATTERN, Slot

    assert re.fullmatch(SLUG, slot)
    assert re.fullmatch(SLOT_PATTERN, slot)
    site = _site({slot: Slot(width=8, height=8, category="decorative")})
    assert slot in site.slots
    # The brief/asset id vocabulary stays dot-free, injectively.
    assert re.fullmatch(r"^[a-z0-9][a-z0-9_-]*$", brief_slug(slot))
    assert brief_slug("a.b") != brief_slug("a-b")


@pytest.mark.parametrize(
    "bad", ["../x", "a/b", "a\\b", ".hidden", "a..b", "a.", "A", "a.-b", "", "a/../b"]
)
def test_a_slot_name_is_one_safe_path_component(bad: str) -> None:
    from qontinui_studio.sites import Slot

    with pytest.raises(ValueError):
        _site({bad: Slot(width=8, height=8, category="decorative")})


def test_a_journal_naming_a_path_outside_the_publish_dir_is_refused(tmp_path: Path) -> None:
    import json as _json

    from qontinui_studio.errors import JournalError
    from qontinui_studio.publish import Journal
    from qontinui_studio.sites import Slot

    site = _site({"runs.active.idle": Slot(width=8, height=8, category="decorative")}, tmp_path)
    live = site.publish_dir
    live.mkdir(parents=True)
    ok_dest = live / "runs.active.idle.webp"
    sha = "a" * 64

    def journal(dest: str, staged: str) -> Path:
        path = live / ".studio-journal-b1.json"
        path.write_text(
            _json.dumps(
                {
                    "kind": "publish",
                    "batch_id": "b1",
                    "site": site.id,
                    "placed": [[staged, dest, sha]],
                    "moved": [],
                    "manifest_before": None,
                    "manifest_after": "",
                }
            )
        )
        return path

    good = Journal.read(
        journal(str(ok_dest), str(live / f".runs.active.idle.webp.staged-{'0' * 32}")), site
    )
    assert good.placed[0][1] == ok_dest
    for dest in (
        str(live / ".." / "runs.active.idle.webp"),
        str(tmp_path / "runs.active.idle.webp"),
        str(live / "sub" / "runs.active.idle.webp"),
        str(live / "runs.other.webp"),
    ):
        with pytest.raises(JournalError):
            Journal.read(journal(dest, str(live / f".x.staged-{'0' * 32}")), site)


def _site(slots: dict[str, object], root: Path | None = None) -> object:
    from qontinui_studio.sites import Site

    base = root or Path("/tmp/studio-test-site")
    return Site.model_validate(
        {
            "id": "web",
            "name": "web",
            "kind": "product",
            "commercial": False,
            "publish_dir": base / "public",
            "slots": slots,
        }
    )


# ---- RFC 8785 -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "text"),
    [
        (1.0, "1"),
        (0.5, "0.5"),
        (-0.0, "0"),
        (1e-7, "1e-7"),
        (0.000001, "0.000001"),
        (1e21, "1e+21"),
        (1e20, "100000000000000000000"),
        (123.456, "123.456"),
        (1.5e300, "1.5e+300"),
        (5e-324, "5e-324"),
        (12345678901234567890.0, "12345678901234567000"),
        (-2.5, "-2.5"),
        (2**53 - 1, "9007199254740991"),
    ],
)
def test_numbers_serialise_as_ecmascript_does(value: float, text: str) -> None:
    assert canonicalize(value) == text


def test_strings_keys_and_structure() -> None:
    assert canonicalize({"b": 1, "a": [True, None, "x"]}) == '{"a":[true,null,"x"],"b":1}'
    assert canonicalize('q"\\\n\t\x01é€') == '"q\\"\\\\\\n\\t\\u0001é€"'
    # Keys sort by UTF-16 code units: U+1F600 (a surrogate pair, D83D...) sorts
    # BEFORE U+FB01 (one unit, FB01), which code-point order reverses.
    assert canonicalize({"ﬁ": 1, "\U0001f600": 2}) == '{"\U0001f600":2,"ﬁ":1}'


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), 2**53, "\ud800"])
def test_what_rfc_8785_cannot_carry_is_refused(bad: object) -> None:
    with pytest.raises(CanonicalizationError):
        canonicalize({"v": bad})
