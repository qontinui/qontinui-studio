"""Provider adapters and the D4-gated registry that constructs them."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from qontinui_studio.adapters.base import (
    GenerationRequest,
    PollResult,
    ProviderAdapter,
    SubmittedJob,
)
from qontinui_studio.adapters.ui_bridge_wrapper import HandoffLatch
from qontinui_studio.errors import (
    AdapterDisabledError,
    GatewayRefusal,
    MissingCredentialError,
    PermanentProviderError,
)
from qontinui_studio.gateway import GATEWAY_CREDENTIAL_ENV, GatewayState, credential_env_acceptable
from qontinui_studio.notify import Notifier
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


def build_adapter(
    provider: Provider,
    gateway_route: str | None = None,
    gateway_credential_env: str | None = None,
    state_dir: Path | None = None,
    notifier: Notifier | None = None,
) -> ProviderAdapter:
    """Construct one adapter, refusing with a typed error when D4 keeps it off.

    With a ``gateway_route`` (a declared gateway's images route) an
    OpenAI-images-wire adapter is pointed at it and authenticates with the
    GATEWAY's own credential (``gateway_credential_env``, which must be a
    ``QONTINUI_GATEWAY_*`` name other than this row's own key) — never with the
    row's vendor or facade key. An adapter that needs a key gets none when the
    gateway declares none: it is refused, not handed the vendor key."""
    gate = provider.adapter_gate()
    if gate is not None:
        raise AdapterDisabledError(f"{provider.id}: {gate}")
    model = provider.model or ""
    if gateway_route is not None:
        return _through_gateway(provider, model, gateway_route, gateway_credential_env)
    base = provider.resolved_base_url()
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
    if provider.adapter == "ui_bridge_wrapper":
        from qontinui_studio.adapters.ui_bridge_wrapper import (
            DEFAULT_RUNNER_URL,
            RUNNER_URL_ENV,
            UIBridgeWrapperAdapter,
        )

        runner = base or os.environ.get(RUNNER_URL_ENV, "").strip() or DEFAULT_RUNNER_URL
        if state_dir is None:
            raise AdapterDisabledError(
                f"{provider.id}: a ui_bridge_wrapper needs the workspace state dir (its hand-off "
                "latch and pace record live there)"
            )
        if provider.operator_override is None:  # the loader already refuses this
            raise AdapterDisabledError(f"{provider.id}: a ui_bridge_wrapper needs its override")
        try:
            return UIBridgeWrapperAdapter(
                provider.id,
                model,
                provider.wrapper_id or "",
                runner,
                override=provider.operator_override,
                state_dir=state_dir,
                notifier=notifier,
                known_host=provider.domains[0].partition("/")[0],
            )
        except PermanentProviderError as e:  # a non-local runner URL
            raise AdapterDisabledError(str(e)) from e
    if provider.adapter == "fake":
        from qontinui_studio.adapters.fake import FakeImageAdapter

        return FakeImageAdapter(provider.id, model or "fake-v1")
    raise AdapterDisabledError(f"{provider.id}: no adapter for kind '{provider.adapter}'")


def _through_gateway(
    provider: Provider, model: str, route: str, credential_env: str | None
) -> ProviderAdapter:
    own = frozenset({provider.api_key_env} if provider.api_key_env else ())
    if credential_env is not None and not credential_env_acceptable(credential_env, own):
        raise AdapterDisabledError(
            f"{provider.id}: the gateway's credential variable {credential_env!r} is not a "
            f"{GATEWAY_CREDENTIAL_ENV.pattern} name distinct from every vendor key"
        )
    if provider.adapter == "openai_images":
        from qontinui_studio.adapters.openai_images import OpenAIImageAdapter

        if credential_env is None:
            raise AdapterDisabledError(
                f"{provider.id}: the gateway declares no credential for its images route, and a "
                "vendor key is never sent to a gateway route"
            )
        return OpenAIImageAdapter(provider.id, model, credential_env, api_base=route)
    if provider.adapter == "openai_compatible_images":
        from qontinui_studio.adapters.openai_compatible_images import (
            OpenAICompatibleImageAdapter,
        )

        return OpenAICompatibleImageAdapter(provider.id, model, route, credential_env)
    raise AdapterDisabledError(
        f"{provider.id}: the declared gateway's images route speaks the OpenAI images wire; "
        f"a '{provider.adapter}' adapter cannot go through it"
    )


@dataclass
class AdapterSet:
    """The adapters that could be built, and why each other provider is off.
    ``refusal`` is set when NO image call may be made at all (a gateway with no
    images route, or an unknown gateway state). ``route`` is the declared
    gateway's images route every adapter here was pointed at, or None when they
    call their own hosts — the image-generation dial's ``local_only`` refuses
    every adapter riding a gateway route (:mod:`qontinui_studio.imagegen_policy`)."""

    adapters: dict[str, ProviderAdapter] = field(default_factory=dict)
    unavailable: dict[str, str] = field(default_factory=dict)
    refusal: GatewayRefusal | None = None
    route: str | None = None
    # Adapters that may only COLLECT jobs already submitted to them (poll and
    # download), never take a new one: the image-generation dial's
    # ``local_only`` puts cloud and gateway adapters here.
    poll_only: dict[str, ProviderAdapter] = field(default_factory=dict)


def build_adapters(
    inventory: ProviderInventory,
    only: list[str] | None = None,
    gateway: GatewayState | None = None,
    state_dir: Path | None = None,
    notifier: Notifier | None = None,
) -> AdapterSet:
    """Build every provider's adapter. With a ``gateway`` reading, a refusing
    state builds none, and a declared images route carries every OpenAI-images
    -wire adapter (other wires cannot go through it)."""
    result = AdapterSet()
    latch = HandoffLatch(state_dir / "handoffs") if state_dir is not None else None
    refusal = gateway.refusal() if gateway is not None else None
    if refusal is None and gateway is not None and gateway.images_credential_env is not None:
        vendor = frozenset(p.api_key_env for p in inventory.providers if p.api_key_env)
        if not credential_env_acceptable(gateway.images_credential_env, vendor):
            # The gateway nominated a vendor key the inventory names (or a name
            # outside the gateway prefix): no image call is made at all.
            refusal = GatewayRefusal(
                f"{GatewayRefusal.code}: the declared gateway names "
                f"{gateway.images_credential_env!r} as its credential, which is a vendor key "
                f"or not a {GATEWAY_CREDENTIAL_ENV.pattern} name; no image call is made",
                gateway.state,
            )
    route = gateway.images_base_url if gateway is not None and refusal is None else None
    if gateway is not None and gateway.state != "declared":
        route = None
    for provider in inventory.providers:
        if only is not None and provider.id not in only:
            continue
        if refusal is not None:
            result.unavailable[provider.id] = str(refusal)
            continue
        if latch is not None and (held := latch.describe(provider.id)) is not None:
            # An operator hand-off keeps the provider out of EVERY run until the
            # operator clears it (not only until the daily reset).
            result.unavailable[provider.id] = f"{provider.id}: {held}"
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
            result.adapters[provider.id] = build_adapter(
                provider,
                state_dir=state_dir,
                notifier=notifier,
                gateway_route=route,
                gateway_credential_env=gateway.images_credential_env
                if gateway is not None and route is not None
                else None,
            )
        except (AdapterDisabledError, MissingCredentialError) as e:
            result.unavailable[provider.id] = str(e)
    result.refusal = refusal
    result.route = route
    return result
