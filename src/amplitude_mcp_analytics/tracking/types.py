"""Typed payload shapes produced by the ctx -> Amplitude lowering."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from ..core.privacy import PrivacyConfig

__all__ = ["AmplitudeFields", "DefaultServerFields", "DefaultToolFields", "TrackEventOptions"]

# Reserved (SDK-derived) fields use snake_case keys internally and are mapped
# to their `[MCP] ` wire names by `reserved_fields_to_properties`. They are
# plain dicts rather than TypedDicts because tool metadata is forward-
# compatible (server-specific fields may appear).
DefaultServerFields = dict[str, Any]
DefaultToolFields = dict[str, Any]


@dataclass
class AmplitudeFields:
    """Lowered Amplitude payload fields for one event."""

    event_properties: dict[str, Any] = field(default_factory=dict)
    """Reserved fields (snake_case keys, pre-wire-mapping)."""
    extra_properties: dict[str, Any] = field(default_factory=dict)
    """The ctx ``extra`` enrichment bag (+ ``tool.extra`` for tool scope) —
    already wire-named by the host."""
    user_id: str | None = None
    device_id: str | None = None
    groups: dict[str, Any] | None = None


@dataclass(frozen=True)
class TrackEventOptions:
    """Options for ``track_server_event`` / ``track_tool_event``.

    ``redact_pii``, ``custom_redaction_patterns``, and ``custom_redaction_fn``
    apply to the module-level functions. ``AmplitudeMCPAnalytics`` methods
    ignore them and use the client's ``MCPAnalyticsConfig``.
    """

    drop_extra_props: bool = False
    """Omit the ctx ``extra`` (and ``tool.extra``) bags from the event."""
    redact_pii: bool | None = None
    """Built-in PII patterns for this standalone call. ``None`` leaves them on.

    Ignored when ``privacy`` is set, which is how the client threads its config.
    """
    custom_redaction_patterns: Sequence[str | Mapping[str, str]] | None = None
    """Extra redaction rules for this standalone call, after the built-in patterns.

    Same validation as ``MCPAnalyticsConfig``: an invalid entry raises
    ``ConfigurationError`` from the call. Ignored when ``privacy`` is set.
    """
    custom_redaction_fn: Callable[[str], str] | None = None
    """Final redaction pass for this standalone call.

    A throw or a non-string return replaces that value with
    ``[content redacted]``. Ignored when ``privacy`` is set.
    """
    privacy: PrivacyConfig | None = None
    """Resolved redaction policy. Supplied by the client from
    ``MCPAnalyticsConfig``. When set, the public redaction fields on this
    options object are ignored.

    @internal Not part of the supported caller API.
    """
