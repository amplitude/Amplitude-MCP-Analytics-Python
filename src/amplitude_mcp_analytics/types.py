"""Structural types for the injected Amplitude client.

This is the entire coupling to the Amplitude client: anything satisfying
:class:`AmplitudeClientLike` works, including ``amplitude.Amplitude`` from the
``amplitude-analytics`` package and the in-memory stub used by
:class:`~amplitude_mcp_analytics.testing.MockAmplitudeMCPAnalytics`.
"""

from __future__ import annotations

from typing import Any, Protocol, TypedDict, runtime_checkable

__all__ = ["AmplitudeClientLike", "AmplitudeEvent"]


class AmplitudeEvent(TypedDict, total=False):
    """Event payload handed to :meth:`AmplitudeClientLike.track`.

    ``event_type`` is always present; the rest mirror the Amplitude HTTP API /
    ``BaseEvent`` fields the SDK populates.
    """

    event_type: str
    user_id: str
    device_id: str
    event_properties: dict[str, Any]
    user_properties: dict[str, Any]
    groups: dict[str, Any]


@runtime_checkable
class AmplitudeClientLike(Protocol):
    """Duck-typed Amplitude client: ``track``/``flush`` (+ optional ``shutdown``)."""

    def track(self, event: Any) -> Any: ...

    def flush(self) -> Any: ...
