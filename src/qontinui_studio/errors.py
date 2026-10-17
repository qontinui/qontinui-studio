"""Typed errors. Every refusal in the studio names its cause with a type, so a
caller (or the CLI) can branch on it rather than on message text."""

from __future__ import annotations

import math


class StudioError(Exception):
    """Base class for every studio error."""


class ConfigError(StudioError):
    """A providers/sites/brief file is malformed or inconsistent."""


class ProviderError(StudioError):
    """Base class for a provider adapter failure."""


class TransientProviderError(ProviderError):
    """A failure worth retrying (5xx, 429, timeout, transport)."""

    def __init__(self, message: str, retry_after_s: float | None = None) -> None:
        super().__init__(message)
        self.retry_after_s = retry_after_s


class QuotaExhaustedError(ProviderError):
    """The provider says its quota for the current period is spent (e.g. a 429
    RESOURCE_EXHAUSTED / insufficient_quota). Not a retryable glitch and not a
    failure of the job: the provider is marked spent until its quota resets and
    the job is deferred."""


class OperatorHandoff(QuotaExhaustedError):
    """A wrapper driving the operator's own web account stopped at a login
    wall, CAPTCHA, bot challenge, usage limit or an unrecognised page, and
    handed the run to the operator. Typed as a spent quota on purpose: the
    provider leaves the rotation until its reset (no retry loop against the
    site), and an interactive job falls through to the next provider."""

    REASONS = (
        "login_wall",
        "captcha",
        "bot_challenge",
        "rate_limited",
        "ui_changed",
        "state_unreadable",
    )

    def __init__(self, message: str, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


class PermanentProviderError(ProviderError):
    """A failure retrying will not fix (4xx other than 429, safety refusal, no image)."""


class ProviderDisabled(PermanentProviderError):
    """The host answered the typed refusal ``disabled``: image generation is
    switched off on that machine. Nothing ran; this provider is unusable until
    the switch is turned back on."""


# A host's "come back in N seconds" is bounded: at most a day (a value past it
# would only park the job beyond any useful horizon, and an unbounded one
# overflows ``timedelta``), at least a second (a zero would spin the scheduler).
MAX_RETRY_AFTER_S = 24 * 3600.0
MIN_RETRY_AFTER_S = 1.0
DEFAULT_RETRY_AFTER_S = 30.0


def clamp_retry_after(value: object, default: float = DEFAULT_RETRY_AFTER_S) -> float:
    """A usable retry delay from ``value``: a non-number, a non-finite number
    (``inf``, ``nan``) or a negative one is INVALID and yields ``default``; a
    valid one is clamped to [``MIN_RETRY_AFTER_S``, ``MAX_RETRY_AFTER_S``]."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    v = float(value)
    if not math.isfinite(v) or v < 0:
        return default
    return min(MAX_RETRY_AFTER_S, max(MIN_RETRY_AFTER_S, v))


class ProviderRefusal(ProviderError):
    """A host's typed refusal: the job was NOT run and the host says when to
    come back. The scheduler requeues the job at ``retry_after_s`` without
    spending a quota unit or an attempt (plan 2026-10-13 Phase 11 step 1)."""

    kind: str = "refusal"

    def __init__(
        self, message: str, retry_after_s: float, *, code: str | None = None, **detail: object
    ) -> None:
        super().__init__(message)
        self.retry_after_s = clamp_retry_after(retry_after_s)
        self.code = code
        self.detail = detail


class ProviderBusy(ProviderRefusal):
    """``busy``: the host's one-job-at-a-time card is taken (or its VRAM is
    held out of band), or the connection was refused — in every case the job
    provably never started."""

    kind = "busy"


class ProviderDeferred(ProviderRefusal):
    """``deferred``: a bulk job asked outside the host's bulk window."""

    kind = "deferred"


class ProviderOutcomeUnknown(ProviderError):
    """The transport failed AFTER the request was sent (a drop mid-flight, a
    truncated or unreadable answer, a lost job id): the job may or may not have
    run. Never collapsed into failed or done; the scheduler records it as
    ``unknown`` and retries as a fresh attempt."""


class GatewayRefusal(StudioError):
    """Image calls refused because this install routes model calls through a
    gateway that declares no images-capable route, or the runner cannot say
    whether a gateway is declared (decision record
    ``gateway-tenants-route-every-model-call-through-the-gateway``). No vendor
    or local call is made."""

    code = "gateway_has_no_image_route"

    def __init__(self, message: str, state: str) -> None:
        super().__init__(message)
        self.state = state


class MissingCredentialError(ProviderError):
    """The adapter's API key env var is unset."""


class AdapterDisabledError(ProviderError):
    """D4 keeps this provider's adapter off (terms unreviewed, automation not allowed,
    or no adapter exists for it)."""


class PostProcessError(StudioError):
    """Base class for a local post-processing failure."""


class BackgroundRemovalUnavailable(PostProcessError):
    """The optional `rembg` dependency is not installed (`poetry install -E bg`)."""


class FfmpegUnavailable(PostProcessError):
    """`ffmpeg` is not on PATH."""


class FfmpegFailed(PostProcessError):
    """`ffmpeg` exited non-zero."""


class FormatUnsupported(PostProcessError):
    """This Pillow build cannot encode the requested format (e.g. AVIF)."""


class PublicationRefused(StudioError):
    """A D5/D6/D7 gate refused a batch. Carries the failing gate results."""

    def __init__(self, message: str, failures: list[str]) -> None:
        super().__init__(message)
        self.failures = failures


class JournalError(StudioError):
    """A leftover journal is unreadable or names paths outside its site's
    publish dir. Recovery refuses to act on it and reports the site."""


class LeaseLost(StudioError):
    """This process no longer holds the batch lease (it expired and another
    driver may have taken over); it must stop driving the batch."""


class ImportRefused(StudioError):
    """``studio library import`` refused an inbox manifest. Nothing was imported;
    ``errors`` names every problem found, each prefixed with its entry index."""

    def __init__(self, message: str, errors: list[str]) -> None:
        super().__init__(message)
        self.errors = errors


class ApprovalRefused(StudioError):
    """``studio library approve``/``reject`` refused: unknown asset, a reviewer
    alias that is not an alias, or (D5) approving an asset whose licence does
    not allow commercial use."""


class TagRefused(StudioError):
    """``studio library tag`` refused: unknown asset, a malformed tag, or a
    tagger that is neither an alias nor ``brief:<brief id>``."""
