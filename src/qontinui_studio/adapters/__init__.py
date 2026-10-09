"""Provider adapters and the D4-gated registry that constructs them."""

from __future__ import annotations

from dataclasses import dataclass, field

from qontinui_studio.adapters.base import (
    GenerationRequest,
    PollResult,
    ProviderAdapter,
    SubmittedJob,
)
from qontinui_studio.errors import AdapterDisabledError, MissingCredentialError
from qontinui_studio.providers import Provider, ProviderInventory

__all__ = [
    "AdapterSet",
    "GenerationRequest",
    "PollResult",
    "ProviderAdapter",
    "SubmittedJob",
    "build_adapter",
    "build_adapters",
]


def build_adapter(provider: Provider) -> ProviderAdapter:
    """Construct one adapter, refusing with a typed error when D4 keeps it off."""
    gate = provider.adapter_gate()
    if gate is not None:
        raise AdapterDisabledError(f"{provider.id}: {gate}")
    model = provider.model or ""
    if provider.adapter == "gemini_api":
        from qontinui_studio.adapters.gemini import GeminiImageAdapter

        return GeminiImageAdapter(provider.id, model, provider.api_key_env or "GEMINI_API_KEY")
    if provider.adapter == "openai_images":
        from qontinui_studio.adapters.openai_images import OpenAIImageAdapter

        return OpenAIImageAdapter(provider.id, model, provider.api_key_env or "OPENAI_API_KEY")
    if provider.adapter == "fake":
        from qontinui_studio.adapters.fake import FakeImageAdapter

        return FakeImageAdapter(provider.id, model or "fake-v1")
    raise AdapterDisabledError(f"{provider.id}: no adapter for kind '{provider.adapter}'")


@dataclass
class AdapterSet:
    """The adapters that could be built, and why each other provider is off."""

    adapters: dict[str, ProviderAdapter] = field(default_factory=dict)
    unavailable: dict[str, str] = field(default_factory=dict)


def build_adapters(inventory: ProviderInventory, only: list[str] | None = None) -> AdapterSet:
    result = AdapterSet()
    for provider in inventory.providers:
        if only is not None and provider.id not in only:
            continue
        try:
            result.adapters[provider.id] = build_adapter(provider)
        except (AdapterDisabledError, MissingCredentialError) as e:
            result.unavailable[provider.id] = str(e)
    return result
