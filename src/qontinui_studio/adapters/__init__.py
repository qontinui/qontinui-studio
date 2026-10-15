"""Provider adapters and the D4-gated registry that constructs them."""

from __future__ import annotations

from dataclasses import dataclass, field

from qontinui_studio.adapters.base import (
    GenerationRequest,
    PollResult,
    ProviderAdapter,
    SubmittedJob,
)
from qontinui_studio.errors import AdapterDisabledError, GatewayRefusal, MissingCredentialError
from qontinui_studio.gateway import GatewayState
from qontinui_studio.providers import Provider, ProviderInventory, is_local_network

__all__ = [
    "AdapterSet",
    "GenerationRequest",
    "PollResult",
    "ProviderAdapter",
    "SubmittedJob",
    "build_adapter",
    "build_adapters",
]


# Adapters whose wire is OpenAI's images API: the only wire a gateway's images
# route is expected to speak.
OPENAI_IMAGES_WIRE = ("openai_images", "openai_compatible_images")


def build_adapter(provider: Provider, base_url: str | None = None) -> ProviderAdapter:
    """Construct one adapter, refusing with a typed error when D4 keeps it off.
    ``base_url`` (a gateway's images route) overrides the row's own."""
    gate = provider.adapter_gate()
    if gate is not None:
        raise AdapterDisabledError(f"{provider.id}: {gate}")
    model = provider.model or ""
    base = base_url or provider.resolved_base_url()
    if provider.adapter == "gemini_api":
        from qontinui_studio.adapters.gemini import GeminiImageAdapter

        return GeminiImageAdapter(
            provider.id, model, provider.api_key_env or "GEMINI_API_KEY", base_url=base
        )
    if provider.adapter == "openai_images":
        from qontinui_studio.adapters.openai_images import API_BASE, OpenAIImageAdapter

        return OpenAIImageAdapter(
            provider.id, model, provider.api_key_env or "OPENAI_API_KEY", api_base=base or API_BASE
        )
    if provider.adapter == "openai_compatible_images":
        from qontinui_studio.adapters.openai_compatible_images import (
            OpenAICompatibleImageAdapter,
        )

        if not base:
            raise AdapterDisabledError(f"{provider.id}: no base_url configured")
        return OpenAICompatibleImageAdapter(provider.id, model, base, provider.api_key_env)
    if provider.adapter == "fake":
        from qontinui_studio.adapters.fake import FakeImageAdapter

        return FakeImageAdapter(provider.id, model or "fake-v1")
    raise AdapterDisabledError(f"{provider.id}: no adapter for kind '{provider.adapter}'")


@dataclass
class AdapterSet:
    """The adapters that could be built, and why each other provider is off.
    ``refusal`` is set when NO image call may be made at all (a gateway with no
    images route, or an unknown gateway state)."""

    adapters: dict[str, ProviderAdapter] = field(default_factory=dict)
    unavailable: dict[str, str] = field(default_factory=dict)
    refusal: GatewayRefusal | None = None


def build_adapters(
    inventory: ProviderInventory,
    only: list[str] | None = None,
    gateway: GatewayState | None = None,
) -> AdapterSet:
    """Build every provider's adapter. With a ``gateway`` reading, a refusing
    state builds none, and a declared images route carries every OpenAI-images
    -wire adapter (other wires cannot go through it)."""
    result = AdapterSet()
    refusal = gateway.refusal() if gateway is not None else None
    route = gateway.images_base_url if gateway is not None and refusal is None else None
    if gateway is not None and gateway.state != "declared":
        route = None
    for provider in inventory.providers:
        if only is not None and provider.id not in only:
            continue
        if refusal is not None:
            result.unavailable[provider.id] = str(refusal)
            continue
        if route is not None and provider.adapter not in OPENAI_IMAGES_WIRE:
            result.unavailable[provider.id] = (
                f"{provider.id}: the declared gateway's images route speaks the OpenAI images "
                f"wire; a '{provider.adapter}' adapter cannot go through it"
            )
            continue
        if route is not None and provider.capacity == "unmetered" and not is_local_network(route):
            # Unmetered means "costs nothing per image": only true on the local
            # network. Through a remote gateway it is not, so the row is off.
            result.unavailable[provider.id] = (
                f"{provider.id}: unmetered capacity cannot ride the gateway route {route}, "
                "which is not on the local network"
            )
            continue
        try:
            result.adapters[provider.id] = build_adapter(provider, base_url=route)
        except (AdapterDisabledError, MissingCredentialError) as e:
            result.unavailable[provider.id] = str(e)
    result.refusal = refusal
    return result
