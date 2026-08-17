"""SDK-own exceptions, distinct from tool errors (see ``errors``)."""

from __future__ import annotations

__all__ = ["AmplitudeMCPAnalyticsError", "ConfigurationError"]


class AmplitudeMCPAnalyticsError(Exception):
    """Base class for errors raised by the SDK itself."""


class ConfigurationError(AmplitudeMCPAnalyticsError):
    """The SDK was constructed or configured incorrectly."""
