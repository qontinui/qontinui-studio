"""Typed errors. Every refusal in the studio names its cause with a type, so a
caller (or the CLI) can branch on it rather than on message text."""

from __future__ import annotations


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


class PermanentProviderError(ProviderError):
    """A failure retrying will not fix (4xx other than 429, safety refusal, no image)."""


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
