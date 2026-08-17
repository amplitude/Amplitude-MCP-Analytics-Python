"""Shared fixtures for the test suite.

Conventions (mirroring the Node repo's test style):
- The dominant double is the list-capturing amplitude fake (`make_amplitude`),
  asserted against with the literal `[MCP] ...` wire names — the tests are a
  de facto schema contract.
- Contexts are built through the public factories, always with an identity or
  tenant attached (without one the `should_emit` skip rule silently drops the
  event and a test would pass vacuously).
- Async tests use the anyio pytest plugin on the asyncio backend (the mcp SDK
  is anyio-based; do not mix in pytest-asyncio).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from amplitude_mcp_analytics.context.factory import create_server_context, create_tool_context
from amplitude_mcp_analytics.context.types import (
    McpIdentity,
    McpServerContext,
    McpServerInfo,
    McpTenant,
    McpToolContext,
    McpToolMeta,
)
from amplitude_mcp_analytics.core import delivery
from amplitude_mcp_analytics.types import AmplitudeEvent


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@dataclass
class CapturingAmplitude:
    """Structural AmplitudeClientLike that records every tracked event."""

    tracked: list[AmplitudeEvent] = field(default_factory=list)
    flushed: int = 0

    def track(self, event: AmplitudeEvent) -> None:
        self.tracked.append(event)

    def flush(self) -> list[Any]:
        self.flushed += 1
        return []

    def shutdown(self) -> None:
        pass

    def events_of(self, event_type: str) -> list[AmplitudeEvent]:
        return [e for e in self.tracked if e.get("event_type") == event_type]


@pytest.fixture
def amplitude() -> CapturingAmplitude:
    return CapturingAmplitude()


def make_amplitude() -> CapturingAmplitude:
    """Inline-helper spelling for tests that need more than one client."""
    return CapturingAmplitude()


class ListLogger:
    """Logger double capturing warning/error lines (duck-typed for get_logger)."""

    def __init__(self) -> None:
        self.warnings: list[str] = []
        self.errors: list[str] = []

    def warning(self, msg: str, *args: Any) -> None:
        self.warnings.append(msg % args if args else msg)

    def error(self, msg: str, *args: Any) -> None:
        self.errors.append(msg % args if args else msg)

    def debug(self, msg: str, *args: Any) -> None:
        pass

    def info(self, msg: str, *args: Any) -> None:
        pass


@pytest.fixture
def list_logger() -> ListLogger:
    return ListLogger()


def server_ctx(**overrides: Any) -> McpServerContext:
    """A server-scope ctx with an explicit identity so events emit (the skip
    rule drops anonymous+tenantless events)."""
    defaults: dict[str, Any] = {
        "server": McpServerInfo(name="test-server", version="1.0.0"),
        "transport": "stdio",
        "identity": McpIdentity(resolved_from="explicit", user_id="user-12345"),
    }
    defaults.update(overrides)
    return create_server_context(**defaults)


def tool_ctx(
    tool: McpToolMeta | None = None, /, **server_overrides: Any
) -> McpToolContext:
    """A tool-scope ctx over :func:`server_ctx` defaults."""
    return create_tool_context(
        server_ctx(**server_overrides),
        tool if tool is not None else McpToolMeta(name="test-tool"),
    )


def tenant() -> McpTenant:
    return McpTenant(group_type="org id", group_value="org-123")


@pytest.fixture(autouse=True)
def _reset_delivery_state() -> Any:
    """Module-level delivery state (unflushed counter, short-id warned set,
    serverless cache) must not leak between tests."""
    delivery._reset_unflushed_state()
    delivery._reset_short_id_warned()
    delivery._reset_serverless_cache()
    yield
    delivery._reset_unflushed_state()
    delivery._reset_short_id_warned()
    delivery._reset_serverless_cache()
