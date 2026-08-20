"""Ports test/instrument-server-events.test.ts (Node) — the default connection
events (`[MCP] Session Initialized` / `[MCP] Tools Listed` / `[MCP] Session
Ended`) emitted by ``instrument_server``.

Adapted to the Python seam: Node fires ``oninitialized`` / ``onclose`` hooks on
a fake server and drives the private ``_requestHandlers`` map by hand; here a
REAL server runs over the in-memory transport, so the handshake happens at
``ClientSession.initialize()`` and "transport close" is the run settling when
the session context exits."""

from __future__ import annotations

import anyio
import pytest
from mcp.server.fastmcp import FastMCP
from mcp.server.lowlevel import Server
from mcp.shared.memory import (
    create_client_server_memory_streams,
    create_connected_server_and_client_session,
)
from mcp.types import Implementation

from amplitude_mcp_analytics import MCPAnalyticsConfig
from amplitude_mcp_analytics.testing import MockAmplitudeMCPAnalytics

pytestmark = pytest.mark.anyio

CLIENT = Implementation(name="cursor", version="0.40")


def make_analytics(config: MCPAnalyticsConfig | None = None) -> MockAmplitudeMCPAnalytics:
    return MockAmplitudeMCPAnalytics(
        server_name="test-mcp", server_version="9.9.9", config=config
    )


def make_fastmcp(tool_names: tuple[str, ...] = ("search", "create")) -> FastMCP:
    mcp = FastMCP("test-mcp")
    for tool_name in tool_names:

        def handler(q: str) -> str:
            return "ok"

        mcp.add_tool(handler, name=tool_name)
    return mcp


async def test_emits_session_initialized_at_the_handshake() -> None:
    analytics = make_analytics()
    mcp = make_fastmcp()
    analytics.instrument_server(mcp, user_id="user-1", auth_type="oauth")

    # Not emitted at bind time — only the real initialize handshake fires it.
    assert analytics.get_events("[MCP] Session Initialized") == []

    async with create_connected_server_and_client_session(
        mcp._mcp_server, client_info=CLIENT
    ) as client:
        await client.send_ping()  # flush the fire-and-forget initialized notification
        events = analytics.get_events("[MCP] Session Initialized")
        assert len(events) == 1
        init = events[0]
        assert init["user_id"] == "user-1"
        props = init["event_properties"]
        assert props["[MCP] Server Name"] == "test-mcp"
        assert props["[MCP] Client Name"] == "cursor"
        assert props["[MCP] Transport"] == "stdio"
        assert props["[MCP] Auth Type"] == "oauth"


async def test_emits_session_ended_once_with_a_duration_on_close() -> None:
    analytics = make_analytics()
    mcp = make_fastmcp()
    analytics.instrument_server(mcp, user_id="user-1")

    async with create_connected_server_and_client_session(
        mcp._mcp_server, client_info=CLIENT
    ):
        assert analytics.get_events("[MCP] Session Ended") == []  # not before close

    ended = analytics.get_events("[MCP] Session Ended")
    assert len(ended) == 1
    duration = ended[0]["event_properties"]["[MCP] Session Duration"]
    assert isinstance(duration, int) and duration >= 0


async def test_no_session_ended_when_no_session_was_initialized() -> None:
    # The run starts and settles without any client handshake — mirrors a
    # stateless / never-initialized connection. Closing must NOT fabricate a
    # session lifecycle it never had.
    analytics = make_analytics()
    mcp = make_fastmcp()
    analytics.instrument_server(mcp, user_id="user-1")
    low = mcp._mcp_server

    async with create_client_server_memory_streams() as (client_streams, server_streams):
        server_read, server_write = server_streams
        _client_read, client_write = client_streams
        async with anyio.create_task_group() as tg:
            tg.start_soon(
                lambda: low.run(
                    server_read, server_write, low.create_initialization_options()
                )
            )
            # Close the client->server direction without ever sending
            # initialize; the server's read loop ends and the run settles.
            await client_write.aclose()

    assert analytics.get_events("[MCP] Session Ended") == []
    assert analytics.get_events("[MCP] Session Initialized") == []


async def test_emits_tools_listed_with_live_count_and_names() -> None:
    analytics = make_analytics()
    mcp = make_fastmcp()
    analytics.instrument_server(mcp, user_id="user-1")

    async with create_connected_server_and_client_session(
        mcp._mcp_server, client_info=CLIENT
    ) as client:
        result = await client.list_tools()
        # Passthrough unchanged — the client sees the real tool set.
        assert [t.name for t in result.tools] == ["search", "create"]

    events = analytics.get_events("[MCP] Tools Listed")
    assert len(events) == 1
    props = events[0]["event_properties"]
    assert props["[MCP] Tool Count"] == 2
    assert props["[MCP] Tool Names"] == ["search", "create"]
    assert props["[MCP] Is Error"] is False
    assert isinstance(props["[MCP] Response Duration"], int)
    assert isinstance(props["[MCP] Response Size"], int)
    assert props["[MCP] Response Size"] > 0


async def test_tools_listed_reflects_tools_added_after_connect() -> None:
    # The wrapped handler enumerates the LIVE tool set per call, so counts stay
    # correct as tools are added after the server is already running.
    analytics = make_analytics()
    mcp = make_fastmcp()
    analytics.instrument_server(mcp, user_id="user-1")

    async with create_connected_server_and_client_session(
        mcp._mcp_server, client_info=CLIENT
    ) as client:
        mcp.add_tool(lambda q: "ok", name="third")  # registered mid-session
        result = await client.list_tools()
        assert len(result.tools) == 3

    events = analytics.get_events("[MCP] Tools Listed")
    assert events[-1]["event_properties"]["[MCP] Tool Count"] == 3


async def test_tools_listed_error_preserves_the_throw_and_sanitizes() -> None:
    # A raising tools/list needs the low-level server (FastMCP's handler cannot
    # fail on demand). The handler's raise must reach the client untouched, and
    # `sanitize_error_message` must apply to the emitted message — the wiring
    # the Node test pins with `toolsListThrows`.
    analytics = make_analytics(
        MCPAnalyticsConfig(sanitize_error_message=lambda _m: "<redacted>")
    )
    server = Server("test-mcp")

    @server.list_tools()
    async def _list_tools():  # pyright: ignore[reportUnusedFunction]
        raise ValueError("kaboom secret=xyz")

    analytics.instrument_server(server, user_id="user-1")

    async with create_connected_server_and_client_session(
        server, client_info=CLIENT
    ) as client:
        with pytest.raises(Exception, match="kaboom"):  # handler behavior preserved
            await client.list_tools()

    events = analytics.get_events("[MCP] Tools Listed")
    assert len(events) == 1
    props = events[0]["event_properties"]
    assert props["[MCP] Is Error"] is True
    assert props["[MCP] Tool Count"] == 0
    # The raw message ("kaboom secret=xyz") never reaches the event stream.
    assert props["[MCP] Error Message"] == "<redacted>"
    assert props["[MCP] Error Type"] == "thrown_exception"


async def test_tools_listed_error_message_is_unsanitized_by_default() -> None:
    analytics = make_analytics()
    server = Server("test-mcp")

    @server.list_tools()
    async def _list_tools():  # pyright: ignore[reportUnusedFunction]
        raise ValueError("kaboom")

    analytics.instrument_server(server, user_id="user-1")
    async with create_connected_server_and_client_session(
        server, client_info=CLIENT
    ) as client:
        with pytest.raises(Exception, match="kaboom"):
            await client.list_tools()

    props = analytics.get_events("[MCP] Tools Listed")[0]["event_properties"]
    assert props["[MCP] Error Message"] == "kaboom"


async def _run_full_lifecycle(analytics: MockAmplitudeMCPAnalytics) -> None:
    """One connection doing everything: handshake, tools/list, close."""
    mcp = make_fastmcp()
    analytics.instrument_server(mcp, user_id="user-1")
    async with create_connected_server_and_client_session(
        mcp._mcp_server, client_info=CLIENT
    ) as client:
        await client.list_tools()


async def test_autocapture_server_events_false_silences_all_three() -> None:
    analytics = make_analytics(
        MCPAnalyticsConfig(autocapture={"server_events": False})
    )
    await _run_full_lifecycle(analytics)
    assert analytics.events == []


async def test_autocapture_split_tools_listed_only() -> None:
    # Per-request-server hosts typically want capability events but no
    # per-connection session lifecycle — the sub-family override.
    analytics = make_analytics(
        MCPAnalyticsConfig(
            autocapture={"session_lifecycle": False, "tools_listed": True}
        )
    )
    await _run_full_lifecycle(analytics)

    types_seen = [e["event_type"] for e in analytics.events]
    assert "[MCP] Tools Listed" in types_seen
    assert "[MCP] Session Initialized" not in types_seen
    assert "[MCP] Session Ended" not in types_seen


async def test_autocapture_split_session_lifecycle_only() -> None:
    analytics = make_analytics(
        MCPAnalyticsConfig(autocapture={"tools_listed": False})
    )
    await _run_full_lifecycle(analytics)

    types_seen = [e["event_type"] for e in analytics.events]
    assert "[MCP] Tools Listed" not in types_seen
    assert "[MCP] Session Initialized" in types_seen
    assert "[MCP] Session Ended" in types_seen


async def test_instrument_server_extra_rides_on_connection_events() -> None:
    analytics = make_analytics()
    mcp = make_fastmcp()
    analytics.instrument_server(
        mcp, user_id="user-1", extra={"org url": "acme", "region": "us"}
    )
    async with create_connected_server_and_client_session(
        mcp._mcp_server, client_info=CLIENT
    ) as client:
        await client.list_tools()

    for event_type in ("[MCP] Session Initialized", "[MCP] Tools Listed"):
        events = analytics.get_events(event_type)
        assert len(events) == 1, event_type
        props = events[0]["event_properties"]
        assert props["org url"] == "acme"
        assert props["region"] == "us"
