"""In-memory test double for :class:`AmplitudeMCPAnalytics`."""

from __future__ import annotations

from typing import Any

from .client import AmplitudeMCPAnalytics
from .config import MCPAnalyticsConfig
from .types import AmplitudeEvent

__all__ = ["MockAmplitudeMCPAnalytics"]


class _CapturingClient:
    """Structural Amplitude client that appends every event to a list."""

    def __init__(self, captured: list[AmplitudeEvent]) -> None:
        self._captured = captured

    def track(self, event: AmplitudeEvent) -> None:
        self._captured.append(event)

    def flush(self) -> list[Any]:
        return []

    def shutdown(self) -> None:
        pass


class MockAmplitudeMCPAnalytics(AmplitudeMCPAnalytics):
    """Behaves like the real client but captures every emitted event in
    :attr:`events` instead of delivering to Amplitude. Use this in unit tests
    to assert against the event stream without network I/O or an API key.

    Example::

        from amplitude_mcp_analytics.testing import MockAmplitudeMCPAnalytics

        mock = MockAmplitudeMCPAnalytics(server_name="test-server", server_version="0.0.0")
        # ... pass `mock` wherever an AmplitudeMCPAnalytics instance is expected ...
        assert len(mock.get_events("[MCP] Tool Call Response")) == 1
    """

    def __init__(
        self,
        *,
        server_name: str,
        server_version: str,
        config: MCPAnalyticsConfig | None = None,
    ) -> None:
        captured: list[AmplitudeEvent] = []
        super().__init__(
            server_name=server_name,
            server_version=server_version,
            amplitude=_CapturingClient(captured),
            config=config,
        )
        #: Every event emitted so far, in order.
        self.events = captured

    def get_events(self, event_type: str | None = None) -> list[AmplitudeEvent]:
        """Return events captured so far, optionally filtered by event_type."""
        if event_type is None:
            return list(self.events)
        return [e for e in self.events if e.get("event_type") == event_type]

    def reset(self) -> None:
        """Drop all captured events. Useful between test cases."""
        self.events.clear()
