"""Ports test/instrument-server.test.ts (Node) — the ``instrument_server``
binding itself.

Adapted to the Python seam: Node wraps ``server.connect(transport)`` and hooks
the private ``oninitialized`` callback; Python wraps ``Server.run(read_stream,
write_stream, init_options, ...)`` — one run is one connection — and observes
the ``initialize`` handshake through a read-stream proxy. Fake servers with a
``connect`` therefore become REAL MCP servers driven over the in-memory
transport (``mcp.shared.memory``)."""

from __future__ import annotations

import pytest
from mcp.server.fastmcp import FastMCP
from mcp.server.lowlevel import Server
from mcp.shared.memory import create_connected_server_and_client_session
from mcp.types import Implementation

from amplitude_mcp_analytics.testing import MockAmplitudeMCPAnalytics

pytestmark = pytest.mark.anyio


def make_analytics() -> MockAmplitudeMCPAnalytics:
    return MockAmplitudeMCPAnalytics(server_name="test-mcp", server_version="9.9.9")


CLIENT = Implementation(name="claude", version="3.0")


async def test_run_detects_stdio_and_sets_the_server_scope() -> None:
    analytics = make_analytics()
    mcp = FastMCP("test-mcp")

    assert analytics._server_ctx is None  # nothing before instrumenting
    analytics.instrument_server(mcp, user_id="user-12345")
    # The scope is created per run, not at bind time (transport/handshake are
    # not known until the server actually runs).
    assert analytics._server_ctx is None

    async with create_connected_server_and_client_session(
        mcp._mcp_server, client_info=CLIENT
    ):
        pass

    ctx = analytics._server_ctx
    assert ctx is not None
    # Memory streams carry no HTTP evidence, so the transport stays the stdio
    # default — same classification a real stdio pipe gets.
    assert ctx.transport == "stdio"
    assert ctx.server.name == "test-mcp"
    assert ctx.server.version == "9.9.9"


async def test_captures_handshake_client_info_into_the_server_scope() -> None:
    analytics = make_analytics()
    mcp = FastMCP("test-mcp")
    analytics.instrument_server(mcp, user_id="user-12345")

    async with create_connected_server_and_client_session(
        mcp._mcp_server, client_info=CLIENT
    ):
        # The read-stream proxy observed the initialize request and stored its
        # clientInfo on the run's scope (mirrored to the fallback ctx).
        ctx = analytics._server_ctx
        assert ctx is not None
        assert ctx.client is not None
        assert ctx.client.name == "claude"
        assert ctx.client.version == "3.0"

    init = analytics.get_events("[MCP] Session Initialized")
    assert len(init) == 1
    assert init[0]["event_properties"]["[MCP] Client Name"] == "claude"
    assert init[0]["event_properties"]["[MCP] Client Version"] == "3.0"


async def test_returns_the_same_server_and_is_idempotent() -> None:
    analytics = make_analytics()
    mcp = FastMCP("test-mcp")
    low = mcp._mcp_server
    original_run = low.run

    returned = analytics.instrument_server(mcp, user_id="user-12345")
    assert returned is mcp

    wrapped_once = low.run
    assert wrapped_once is not original_run  # run actually wrapped

    analytics.instrument_server(mcp, user_id="user-12345")  # second call is a no-op
    assert low.run is wrapped_once  # run not wrapped twice

    # Behavioral proof: one connection yields exactly one lifecycle pair — a
    # double wrap would double the events.
    async with create_connected_server_and_client_session(
        mcp._mcp_server, client_info=CLIENT
    ):
        pass
    assert len(analytics.get_events("[MCP] Session Initialized")) == 1
    assert len(analytics.get_events("[MCP] Session Ended")) == 1


async def test_creates_a_fresh_scope_per_run() -> None:
    analytics = make_analytics()
    mcp = FastMCP("test-mcp")
    analytics.instrument_server(mcp, user_id="user-12345")

    async with create_connected_server_and_client_session(
        mcp._mcp_server, client_info=CLIENT
    ):
        pass
    first_ctx = analytics._server_ctx
    assert first_ctx is not None

    async with create_connected_server_and_client_session(
        mcp._mcp_server, client_info=Implementation(name="cursor", version="0.40")
    ):
        pass
    second_ctx = analytics._server_ctx
    assert second_ctx is not None

    # Each run gets its own scope ctx — the second connection did not mutate
    # the first run's (and captured its own handshake clientInfo).
    assert second_ctx is not first_ctx
    assert first_ctx.client is not None and first_ctx.client.name == "claude"
    assert second_ctx.client is not None and second_ctx.client.name == "cursor"

    # And each run emitted its own lifecycle pair.
    assert len(analytics.get_events("[MCP] Session Initialized")) == 2
    assert len(analytics.get_events("[MCP] Session Ended")) == 2


async def test_last_connected_fallback_is_set_after_the_handshake() -> None:
    # The client-level `_server_ctx` mirror exists ONLY for direct handler
    # invocation outside a dispatch frame (Node parity: the singleton
    # `_serverCtx`); the authoritative state lives on each run's scope.
    analytics = make_analytics()
    mcp = FastMCP("test-mcp")
    analytics.instrument_server(mcp, user_id="user-12345", auth_type="oauth")

    async with create_connected_server_and_client_session(
        mcp._mcp_server, client_info=CLIENT
    ) as client:
        # notifications/initialized is fire-and-forget; a request round-trip
        # guarantees the server's read loop has observed it (in-order stream).
        await client.send_ping()
        ctx = analytics._server_ctx
        assert ctx is not None
        # The handshake resolved the ctx into its connection form.
        assert ctx.identity.user_id == "user-12345"
        assert ctx.auth_type == "oauth"
        assert ctx.anchor.type == "process"  # stdio → process anchor


async def test_works_on_a_low_level_server() -> None:
    # No FastMCP layer at all — instrument_server accepts the low-level shape
    # (Node parity: "works on a low-level Server (no .server property)").
    analytics = make_analytics()
    server = Server("low-level")

    @server.list_tools()
    async def _list_tools():  # pyright: ignore[reportUnusedFunction]
        return []

    returned = analytics.instrument_server(server, user_id="user-12345")
    assert returned is server

    async with create_connected_server_and_client_session(
        server, client_info=Implementation(name="cli", version="0.1")
    ):
        pass

    ctx = analytics._server_ctx
    assert ctx is not None
    assert ctx.transport == "stdio"
    assert ctx.client is not None and ctx.client.name == "cli"
    assert len(analytics.get_events("[MCP] Session Initialized")) == 1
