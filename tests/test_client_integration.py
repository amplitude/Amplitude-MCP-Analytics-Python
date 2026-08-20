"""Ports the Node repo's test/tracking/client-integration.test.ts — the
MockAmplitudeMCPAnalytics end-to-end through the real client stack (custom
event API, instrument_tool, and the delivery hooks the mock's capturing client
sits behind)."""

from __future__ import annotations

import logging
from typing import Any

import pytest

from amplitude_mcp_analytics import (
    MCPAnalyticsConfig,
    McpIdentity,
    McpServerInfo,
    McpTenant,
    McpToolMeta,
    create_server_context,
    create_tool_context,
    get_current_context,
)
from amplitude_mcp_analytics.context.types import McpToolContext
from amplitude_mcp_analytics.core.delivery import get_global_unflushed_count
from amplitude_mcp_analytics.testing import MockAmplitudeMCPAnalytics
from conftest import server_ctx


def make_mock(config: MCPAnalyticsConfig | None = None) -> MockAmplitudeMCPAnalytics:
    return MockAmplitudeMCPAnalytics(
        server_name="test-server", server_version="0.0.0", config=config
    )


def ok(text: str = "ok") -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}]}


class TestCustomEventApi:
    def test_track_server_event_emits_through_the_underlying_client(self) -> None:
        mock = make_mock()
        ctx = create_server_context(
            server=McpServerInfo(name="test-server", version="0.0.0"),
            transport="streamable-http",
            identity=McpIdentity(user_id="u1", resolved_from="explicit"),
        )

        mock.track_server_event(ctx, "mcp: custom event", {"foo": "bar"})

        assert len(mock.events) == 1
        assert mock.events[0]["event_type"] == "mcp: custom event"
        assert mock.events[0]["user_id"] == "u1"
        assert mock.events[0]["event_properties"]["foo"] == "bar"

    def test_track_tool_event_inherits_tool_metadata(self) -> None:
        mock = make_mock()
        ctx = create_tool_context(
            create_server_context(
                server=McpServerInfo(name="test-server"),
                transport="streamable-http",
                identity=McpIdentity(user_id="u1", resolved_from="explicit"),
            ),
            McpToolMeta(name="search_docs", owner="docs-team"),
        )

        mock.track_tool_event(ctx, "mcp: tool query", {"query text": "hi"})

        assert len(mock.events) == 1
        properties = mock.events[0]["event_properties"]
        assert properties["[MCP] Tool Name"] == "search_docs"
        assert properties["[MCP] Tool Owner"] == "docs-team"
        assert properties["query text"] == "hi"

    @pytest.mark.anyio
    async def test_instrument_tool_emits_the_response_event_around_the_handler(self) -> None:
        mock = make_mock()
        # Bind a server scope with a tenant so the event is emitted (events with
        # neither an identity nor a tenant are dropped; identity is floored here).
        mock._server_ctx = create_server_context(
            server=McpServerInfo(name="test-server", version="0.0.0"),
            transport="streamable-http",
            tenant=McpTenant(group_type="org id", group_value="36958"),
        )

        seen: dict[str, Any] = {}

        async def handler(q: str) -> dict[str, Any]:
            seen["ctx"] = get_current_context()
            return ok(q)

        wrapped = mock.instrument_tool(handler, McpToolMeta(name="search_docs"))

        result = await wrapped(q="hi")

        assert result == ok("hi")
        received_ctx: McpToolContext = seen["ctx"]
        assert received_ctx.tool.name == "search_docs"

        response = mock.get_events("[MCP] Tool Call Response")
        assert len(response) == 1
        properties = response[0]["event_properties"]
        assert properties["[MCP] Is Error"] is False
        assert properties["[MCP] Tool Name"] == "search_docs"

    @pytest.mark.anyio
    async def test_instrument_tool_is_a_no_op_passthrough_without_instrument_server(
        self,
    ) -> None:
        mock = make_mock()

        # No instrument_server() → no server scope bound → analytics is off.
        ran: dict[str, bool] = {"value": False}

        async def handler(**_kwargs: Any) -> dict[str, Any]:
            ran["value"] = True
            return ok()

        wrapped = mock.instrument_tool(handler, McpToolMeta(name="search_docs"))
        await wrapped(a=1)

        assert ran["value"] is True  # the tool still runs
        assert mock.events == []  # nothing emitted


class TestDeliveryHooksThroughTheClientStack:
    """The mock rides the real client stack, so the delivery hooks (dry-run,
    debug, unflushed accounting) apply to it too."""

    def test_dry_run_drops_delivery_and_does_not_count(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        mock = make_mock(MCPAnalyticsConfig(dry_run=True))

        mock.track_server_event(server_ctx(), "mcp: custom event", {"foo": "bar"})

        assert mock.events == []
        # Dry-run short-circuits before the counter, so nothing reads as unflushed.
        assert get_global_unflushed_count() == 0
        # The event JSON still lands on stderr so a dry run is observable.
        assert "mcp: custom event" in capsys.readouterr().err

    def test_debug_logs_a_line_and_still_delivers(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.DEBUG, logger="amplitude_mcp_analytics")
        mock = make_mock(MCPAnalyticsConfig(debug=True))

        mock.track_server_event(server_ctx(), "mcp: custom event")

        assert len(mock.events) == 1
        logged = caplog.text
        assert "[amplitude-mcp-analytics]" in logged
        assert "mcp: custom event" in logged

    def test_events_get_events_and_reset(self) -> None:
        mock = make_mock()
        ctx = server_ctx()

        mock.track_server_event(ctx, "mcp: event a")
        mock.track_server_event(ctx, "mcp: event b")

        assert [e["event_type"] for e in mock.events] == ["mcp: event a", "mcp: event b"]
        assert [e["event_type"] for e in mock.get_events()] == ["mcp: event a", "mcp: event b"]
        assert len(mock.get_events("mcp: event a")) == 1

        mock.reset()
        assert mock.events == []
        assert mock.get_events() == []

    def test_flush_settles_the_global_unflushed_counter(self) -> None:
        mock = make_mock()
        ctx = server_ctx()

        mock.track_server_event(ctx, "mcp: event a")
        mock.track_server_event(ctx, "mcp: event b")
        assert get_global_unflushed_count() == 2

        mock.flush()

        assert get_global_unflushed_count() == 0
        # A second flush stays at zero (clamped — never goes negative).
        mock.flush()
        assert get_global_unflushed_count() == 0
