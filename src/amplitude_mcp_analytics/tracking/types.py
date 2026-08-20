"""Typed payload shapes produced by the ctx -> Amplitude lowering."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

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
    """Options for ``track_server_event`` / ``track_tool_event``."""

    drop_extra_props: bool = False
    """Omit the ctx ``extra`` (and ``tool.extra``) bags from the event."""
