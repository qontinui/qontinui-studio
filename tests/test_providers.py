from __future__ import annotations

from pathlib import Path

import pytest

from qontinui_studio.adapters import build_adapter, build_adapters
from qontinui_studio.errors import AdapterDisabledError, ConfigError
from qontinui_studio.providers import ProviderInventory, SourcedVerdict, load_providers

REPO = Path(__file__).resolve().parent.parent

REQUIRED = {
    "gemini",
    "openai",
    "bing",
    "leonardo",
    "ideogram",
    "firefly",
    "runway",
    "pika",
    "luma",
    "kling",
}


def test_shipped_inventory_covers_every_provider_with_sourced_verdicts() -> None:
    inv = load_providers(REPO / "providers.yaml")
    assert inv.operator_accounts_owed is True
    assert REQUIRED <= {p.id for p in inv.providers}
    for p in inv.providers:
        for verdict in (p.automation_allowed, p.commercial_use, p.api_available):
            assert verdict.value in ("yes", "no", "unknown")
            if verdict.value != "unknown":
                assert verdict.source and verdict.source.startswith("https://")
        assert p.tos_reviewed is not None
        assert p.sources


def test_d4_unknown_or_no_automation_keeps_the_adapter_off() -> None:
    inv = load_providers(REPO / "providers.yaml")
    for p in inv.providers:
        if p.automation_allowed.value != "yes":
            assert p.adapter_gate() is not None, p.id
            with pytest.raises(AdapterDisabledError):
                build_adapter(p)


def test_no_gui_adapter_is_shipped() -> None:
    """Every no-API provider's terms forbid (or do not confirm) UI automation,
    so the honest D4 outcome is that no GUI-automation adapter exists."""
    inv = load_providers(REPO / "providers.yaml")
    for p in inv.providers:
        if p.api_available.value != "yes":
            assert p.adapter == "none", p.id
            assert p.automation_allowed.value in ("no", "unknown"), p.id


def test_billed_apis_are_not_scheduled_without_an_operator_quota(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inv = load_providers(REPO / "providers.yaml")
    for pid in ("gemini", "openai"):
        p = inv.get(pid)
        assert p.adapter_gate() is None
        assert p.schedulable_reason() is not None
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    built = build_adapters(inv)
    assert not built.adapters
    assert "env var GEMINI_API_KEY is unset" in built.unavailable["gemini"]
    assert "automation_allowed is 'no'" in built.unavailable["bing"]


def test_one_account_per_provider_is_enforced() -> None:
    row = {
        "id": "x",
        "provider": "X",
        "plan": "free",
        "api_available": {"value": "unknown", "source": "https://x.test/api", "note": "403"},
        "automation_allowed": {"value": "unknown", "source": "https://x.test/tos", "note": "403"},
        "commercial_use": {"value": "unknown", "source": "https://x.test/tos", "note": "403"},
        "watermark": "none",
        "domains": ["x.test"],
    }
    with pytest.raises(ValueError, match="one account per provider"):
        ProviderInventory.model_validate(
            {"note": "n", "operator_accounts_owed": True, "providers": [row, row]}
        )


def test_a_yes_needs_a_source_and_bare_yaml_bools_coerce() -> None:
    with pytest.raises(ValueError, match="must cite the source URL"):
        SourcedVerdict(value="yes")
    with pytest.raises(ValueError, match="must cite the source URL"):
        SourcedVerdict(value="unknown", note="could not read it")
    with pytest.raises(ValueError, match="needs a note"):
        SourcedVerdict(value="unknown", source="https://x.test/tos")
    assert SourcedVerdict.model_validate({"value": True, "source": "https://x"}).value == "yes"
    assert SourcedVerdict.model_validate({"value": False, "source": "https://x"}).value == "no"


def test_malformed_file_is_a_config_error(tmp_path: Path) -> None:
    bad = tmp_path / "providers.yaml"
    bad.write_text("note: n\nproviders: [{id: Bad Id}]\n")
    with pytest.raises(ConfigError):
        load_providers(bad)


def _row(**kw: object) -> dict[str, object]:
    base: dict[str, object] = {
        "id": "x",
        "provider": "X",
        "plan": "free",
        "api_available": {"value": "unknown", "source": "https://x.test/api", "note": "403"},
        "automation_allowed": {"value": "unknown", "source": "https://x.test/tos", "note": "403"},
        "commercial_use": {"value": "unknown", "source": "https://x.test/tos", "note": "403"},
        "watermark": "none",
        "domains": ["x.test"],
    }
    base.update(kw)
    host = str(base["domains"][0]).split("/")[0]  # type: ignore[index]
    for key in ("api_available", "automation_allowed", "commercial_use"):
        if key not in kw:
            base[key] = {"value": "unknown", "source": f"https://{host}/tos", "note": "403"}
    return base


@pytest.mark.parametrize(
    ("a", "b", "shared"),
    [
        (
            _row(id="a", provider="Leonardo.ai", domains=["a.test"]),
            _row(id="b", provider="leonardo AI", domains=["b.test"]),
            "provider",
        ),
        (
            _row(id="a", provider="A", api_key_env="KEY", domains=["a.test"]),
            _row(id="b", provider="B", api_key_env="KEY", domains=["b.test"]),
            "api_key_env",
        ),
        (
            _row(id="a", provider="A", adapter="gemini_api", domains=["a.test"]),
            _row(id="b", provider="B", adapter="gemini_api", domains=["b.test"]),
            "adapter",
        ),
        (
            _row(id="a", provider="A", domains=["shared.test"]),
            _row(id="b", provider="B", domains=["shared.test"]),
            "domain",
        ),
    ],
)
def test_one_account_per_provider_by_name_and_key(
    a: dict[str, object], b: dict[str, object], shared: str
) -> None:
    with pytest.raises(ValueError, match=f"share {shared}"):
        ProviderInventory.model_validate(
            {"note": "n", "operator_accounts_owed": True, "providers": [a, b]}
        )


def test_mirror_domains_are_not_first_party_sources() -> None:
    with pytest.raises(ValueError, match="third-party mirror"):
        SourcedVerdict(value="yes", source="https://open.windriver.com/x/openai-tou.html")
    with pytest.raises(ValueError, match="https URL"):
        SourcedVerdict(value="no", source="http://example.com/tos")
    with pytest.raises(ValueError, match="third-party mirror"):
        ProviderInventory.model_validate(
            {
                "note": "n",
                "operator_accounts_owed": True,
                "providers": [_row(sources=["https://tosdr.org/en/service/1"])],
            }
        )


def test_openai_commercial_use_is_unknown_until_first_party_terms_are_read() -> None:
    p = load_providers(REPO / "providers.yaml").get("openai")
    assert p.commercial_use.value == "unknown"
    assert p.commercial_use.note and "403" in p.commercial_use.note


def test_sources_must_be_on_the_rows_declared_domains() -> None:
    with pytest.raises(ValueError, match="not on a declared provider domain"):
        ProviderInventory.model_validate(
            {
                "note": "n",
                "operator_accounts_owed": True,
                "providers": [
                    _row(
                        domains=["openai.com"],
                        commercial_use={"value": "yes", "source": "https://blog.example.com/x"},
                    )
                ],
            }
        )
    ok = ProviderInventory.model_validate(
        {
            "note": "n",
            "operator_accounts_owed": True,
            "providers": [
                _row(
                    domains=["openai.com", "intercom.help/openai"],
                    commercial_use={"value": "yes", "source": "https://help.openai.com/x"},
                    sources=["https://intercom.help/openai/articles/1"],
                )
            ],
        }
    )
    assert ok.providers[0].owns("https://platform.openai.com/docs")
    assert not ok.providers[0].owns("https://intercom.help/someone-else/x")
    assert not ok.providers[0].owns("https://notopenai.com/x")
