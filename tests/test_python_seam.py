"""Python-only seam tests — behavior with no Node counterpart, arising from
wrapping ``Server.run`` and the public ``request_handlers`` dict instead of
``connect``/``_requestHandlers``:

(a) the SDK's internal tools/list cache refresh must not emit phantom events;
(b) sessionless runs emit initialization but no meaningless end duration;
(c) the tools/call dispatch marker survives ``anyio.to_thread`` context copies;
(d) ``functools.wraps`` keeps the original signature so FastMCP still builds
    the real input schema through the wrapper;
(e) transport detection defaults to stdio and honors the explicit override."""

from __future__ import annotations

from typing import Any

import anyio
import pytest
from mcp.client.session import ClientSession
from mcp.server.fastmcp import FastMCP
from mcp.server.lowlevel import Server
from mcp.shared.memory import (
    create_client_server_memory_streams,
    create_connected_server_and_client_session,
)
from mcp.types import Implementation

from amplitude_mcp_analytics.testing import MockAmplitudeMCPAnalytics

pytestmark = pytest.mark.anyio

CLIENT = Implementation(name="test-client", version="1.0.0")


def make_analytics() -> MockAmplitudeMCPAnalytics:
    return MockAmplitudeMCPAnalytics(server_name="seam-server", server_version="1.2.3")


def make_instrumented_fastmcp(
    analytics: MockAmplitudeMCPAnalytics,
) -> FastMCP:
    mcp = FastMCP("seam-server")

    @mcp.tool()
    @analytics.instrument_tool(name="add", owner="math-team")
    def add(a: int, b: int) -> int:
        return a + b

    analytics.instrument_server(mcp, user_id="user-12345")
    return mcp


async def raw_call_tool(session: ClientSession, name: str, arguments: dict) -> Any:
    """tools/call over the wire WITHOUT ClientSession.call_tool, whose
    client-side output-schema validation sends its own (genuine, legitimately
    emitting) tools/list request and would muddy the phantom-event count."""
    import mcp.types as types

    return await session.send_request(
        types.ClientRequest(
            types.CallToolRequest(
                params=types.CallToolRequestParams(name=name, arguments=arguments)
            )
        ),
        types.CallToolResult,
    )


async def test_internal_tools_list_cache_refresh_emits_no_phantom_event() -> None:
    # The low-level server invokes the tools/list handler internally with
    # req=None to refresh its tool cache during tools/call dispatch
    # (_get_cached_tool_definition). Those internal calls are not client
    # requests and must not emit `[MCP] Tools Listed` — the count must track
    # actual client list requests ONLY.
    analytics = make_analytics()
    mcp = make_instrumented_fastmcp(analytics)
    low = mcp._mcp_server

    async with create_connected_server_and_client_session(
        low, client_info=CLIENT
    ) as client:
        result = await raw_call_tool(client, "add", {"a": 2, "b": 3})
        assert result.isError is False
        # Non-vacuous: the dispatch DID refresh the cache through the wrapped
        # handler (req=None), which is exactly the call that must stay silent.
        assert "add" in low._tool_cache
        assert analytics.get_events("[MCP] Tools Listed") == []

        await client.list_tools()
        assert len(analytics.get_events("[MCP] Tools Listed")) == 1

        await raw_call_tool(client, "add", {"a": 1, "b": 1})
        await client.list_tools()
        assert len(analytics.get_events("[MCP] Tools Listed")) == 2


async def test_stateless_run_emits_initialized_but_not_ended() -> None:
    # stateless=True means the manager spins one run per HTTP request: there is
    # no persistent protocol session. The initialize handshake is still real
    # and useful (it carries clientInfo), but an end duration for one HTTP
    # request would be meaningless.
    analytics = make_analytics()
    server = Server("seam-server")

    @server.list_tools()
    async def _list_tools():  # pyright: ignore[reportUnusedFunction]
        return []

    analytics.instrument_server(server, user_id="user-12345")

    async with create_client_server_memory_streams() as (client_streams, server_streams):
        server_read, server_write = server_streams
        client_read, client_write = client_streams
        async with anyio.create_task_group() as tg:
            tg.start_soon(
                lambda: server.run(
                    server_read,
                    server_write,
                    server.create_initialization_options(),
                    raise_exceptions=True,
                    stateless=True,
                )
            )
            async with ClientSession(
                read_stream=client_read, write_stream=client_write
            ) as session:
                await session.initialize()
                await session.list_tools()
            tg.cancel_scope.cancel()

    types_seen = [e["event_type"] for e in analytics.events]
    assert "[MCP] Session Initialized" in types_seen
    assert "[MCP] Session Ended" not in types_seen
    init = analytics.get_events("[MCP] Session Initialized")[0]
    assert init["event_properties"]["[MCP] Client Name"] == "mcp"
    assert init["event_properties"]["[MCP] Session ID"] == "no-session"
    # The run still carried a scope: capability events keep working.
    assert "[MCP] Tools Listed" in types_seen


async def test_dispatch_marker_survives_anyio_to_thread() -> None:
    # The de-dup marker is a MUTATED object retrieved from a ContextVar, not a
    # ContextVar.set() — a set() would be stranded in the context copies made
    # by anyio.to_thread / nested task groups, and a failing tool that hopped
    # threads would then double-report as `[MCP] Tool Call Rejected`.
    analytics = make_analytics()
    mcp = FastMCP("seam-server")

    @mcp.tool()
    @analytics.instrument_tool(name="threaded_boom")
    async def threaded_boom(x: str) -> str:  # pyright: ignore[reportUnusedFunction]
        await anyio.to_thread.run_sync(lambda: None)  # context-copying hop
        raise ValueError("after thread hop")

    analytics.instrument_server(mcp, user_id="user-12345")

    async with create_connected_server_and_client_session(
        mcp._mcp_server, client_info=CLIENT
    ) as client:
        result = await client.call_tool("threaded_boom", {"x": "y"})
        assert result.isError is True  # the SDK's in-band failure conversion

    # The dispatched failure lands ONLY on Tool Call Response.
    assert analytics.get_events("[MCP] Tool Call Rejected") == []
    responses = analytics.get_events("[MCP] Tool Call Response")
    assert len(responses) == 1
    props = responses[0]["event_properties"]
    assert props["[MCP] Is Error"] is True
    assert props["[MCP] Error Type"] == "thrown_exception"


async def test_decorator_order_preserves_the_original_input_schema() -> None:
    # @mcp.tool() sits OVER @analytics.instrument_tool(...): FastMCP builds the
    # input schema from the wrapped function's signature, which functools.wraps
    # exposes through __wrapped__. A wrapper that hid the signature would
    # collapse the schema to *args/**kwargs.
    analytics = make_analytics()
    mcp = make_instrumented_fastmcp(analytics)

    async with create_connected_server_and_client_session(
        mcp._mcp_server, client_info=CLIENT
    ) as client:
        tools = await client.list_tools()
        assert [t.name for t in tools.tools] == ["add"]
        schema = tools.tools[0].inputSchema
        assert schema["properties"]["a"]["type"] == "integer"
        assert schema["properties"]["b"]["type"] == "integer"
        assert set(schema["required"]) == {"a", "b"}

        # And the schema is not just advertised — typed dispatch works.
        result = await client.call_tool("add", {"a": 2, "b": 3})
        assert result.isError is False


async def test_transport_defaults_to_stdio_with_the_process_anchor() -> None:
    # Memory streams (like a real stdio pipe) carry no HTTP evidence, so the
    # transport stays the stdio default and the anchor is the process lifetime.
    analytics = make_analytics()
    mcp = make_instrumented_fastmcp(analytics)

    async with create_connected_server_and_client_session(
        mcp._mcp_server, client_info=CLIENT
    ) as client:
        await client.call_tool("add", {"a": 2, "b": 3})

    for event_type in ("[MCP] Session Initialized", "[MCP] Tool Call Response"):
        events = analytics.get_events(event_type)
        assert len(events) == 1, event_type
        props = events[0]["event_properties"]
        assert props["[MCP] Transport"] == "stdio"
        assert props["[MCP] Anchor Type"] == "process"


async def test_instrument_server_transport_override_is_reflected_on_events() -> None:
    # instrument_server(transport=...) overrides auto-detection for hosts that
    # know their transport out-of-band (e.g. behind a proxy that strips
    # evidence). Every event from the binding reflects it.
    analytics = make_analytics()
    mcp = FastMCP("seam-server")

    @mcp.tool()
    @analytics.instrument_tool(name="add")
    def add(a: int, b: int) -> int:  # pyright: ignore[reportUnusedFunction]
        return a + b

    analytics.instrument_server(mcp, user_id="user-12345", transport="streamable-http")

    async with create_connected_server_and_client_session(
        mcp._mcp_server, client_info=CLIENT
    ) as client:
        await client.call_tool("add", {"a": 2, "b": 3})

    for event_type in ("[MCP] Session Initialized", "[MCP] Tool Call Response"):
        events = analytics.get_events(event_type)
        assert len(events) == 1, event_type
        assert events[0]["event_properties"]["[MCP] Transport"] == "streamable-http"
