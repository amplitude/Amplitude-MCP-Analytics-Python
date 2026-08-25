"""Ports test/server-scope.test.ts (Node) — the per-server-instance scope.

Hosts that build one MCP server per HTTP request bind ``instrument_server`` per
request, possibly with per-request identity opts. These tests prove concurrent
bindings do not bleed into each other — the race that previously forced such
hosts to avoid ``instrument_server`` opts and server-event autocapture
entirely.

Adapted to the Python seam: Node dispatched through the private
``_requestHandlers`` map of fake servers; here each server is a REAL FastMCP
run over its own in-memory session, and the per-run scope rides a ContextVar
set by the wrapped ``run()`` — so a tool call resolves the scope of the server
that DISPATCHED it, not the last-connected one."""

from __future__ import annotations

import os
from contextlib import AsyncExitStack

import anyio
import pytest
from mcp.server.fastmcp import FastMCP
from mcp.shared.memory import (
    create_client_server_memory_streams,
    create_connected_server_and_client_session,
)
from mcp.types import Implementation

from amplitude_mcp_analytics import McpTenant
from amplitude_mcp_analytics.testing import MockAmplitudeMCPAnalytics
from amplitude_mcp_analytics.types import AmplitudeEvent

pytestmark = pytest.mark.anyio

CLIENT = Implementation(name="cursor", version="1.0.0")

RESPONSE = "[MCP] Tool Call Response"


def make_analytics() -> MockAmplitudeMCPAnalytics:
    return MockAmplitudeMCPAnalytics(server_name="test-mcp", server_version="9.9.9")


def by_tool(events: list[AmplitudeEvent]) -> dict[str, AmplitudeEvent]:
    return {e["event_properties"]["[MCP] Tool Name"]: e for e in events}


async def test_attributes_tool_calls_to_the_dispatching_server() -> None:
    analytics = make_analytics()

    # ONE wrapped handler registered on BOTH servers — attribution must come
    # from the dispatching run's scope, not from anything captured at wrap time.
    async def search(q: str) -> str:
        return "ok"

    wrapped = analytics.instrument_tool(search, name="search")

    mcp_a = FastMCP("test-mcp")
    mcp_a.add_tool(wrapped, name="search")
    mcp_b = FastMCP("test-mcp")
    mcp_b.add_tool(wrapped, name="search")

    analytics.instrument_server(
        mcp_a,
        user_id="alice@example.com",
        tenant=McpTenant(group_type="org id", group_value="org-a"),
    )
    analytics.instrument_server(
        mcp_b,
        user_id="bob@example.com",
        tenant=McpTenant(group_type="org id", group_value="org-b"),
    )

    async with AsyncExitStack() as stack:
        client_a = await stack.enter_async_context(
            create_connected_server_and_client_session(mcp_a._mcp_server, client_info=CLIENT)
        )
        client_b = await stack.enter_async_context(
            create_connected_server_and_client_session(mcp_b._mcp_server, client_info=CLIENT)
        )
        # B connected last → the last-connected fallback now points at bob.

        # Dispatch through A: must attribute to alice/org-a despite B
        # connecting later. Then through B.
        await client_a.call_tool("search", {"q": "hi"})
        await client_b.call_tool("search", {"q": "hi"})

    events = analytics.get_events(RESPONSE)
    assert len(events) == 2
    assert events[0]["user_id"] == "alice@example.com"
    assert events[0]["groups"] == {"org id": "org-a"}
    assert events[1]["user_id"] == "bob@example.com"
    assert events[1]["groups"] == {"org id": "org-b"}


async def test_attribution_correct_across_interleaved_concurrent_dispatches() -> None:
    analytics = make_analytics()

    entered = anyio.Event()
    release = anyio.Event()

    async def slow(q: str) -> str:
        entered.set()
        await release.wait()  # hold A's call open while B connects and dispatches
        return "slow"

    async def fast(q: str) -> str:
        return "fast"

    mcp_a = FastMCP("test-mcp")
    mcp_a.add_tool(analytics.instrument_tool(slow, name="slow"), name="slow")
    mcp_b = FastMCP("test-mcp")
    mcp_b.add_tool(analytics.instrument_tool(fast, name="fast"), name="fast")

    analytics.instrument_server(mcp_a, user_id="alice@example.com")
    analytics.instrument_server(mcp_b, user_id="bob@example.com")

    async with create_connected_server_and_client_session(
        mcp_a._mcp_server, client_info=CLIENT
    ) as client_a:
        async with anyio.create_task_group() as tg:
            tg.start_soon(lambda: client_a.call_tool("slow", {"q": "x"}))
            await entered.wait()

            # While A's call is in flight, a second binding connects and runs.
            async with create_connected_server_and_client_session(
                mcp_b._mcp_server, client_info=CLIENT
            ) as client_b:
                await client_b.call_tool("fast", {"q": "y"})
                release.set()

    attribution = {
        name: e["user_id"] for name, e in by_tool(analytics.get_events(RESPONSE)).items()
    }
    assert attribution == {
        "fast": "bob@example.com",
        "slow": "alice@example.com",  # NOT bob, despite B connecting mid-flight
    }


async def test_no_identity_bleed_into_an_identity_less_dispatching_server() -> None:
    analytics = make_analytics()

    async def search_a(q: str) -> str:
        return "ok"

    async def search_b(q: str) -> str:
        return "ok"

    mcp_a = FastMCP("test-mcp")
    mcp_a.add_tool(analytics.instrument_tool(search_a, name="search-a"), name="search_a")
    mcp_b = FastMCP("test-mcp")
    mcp_b.add_tool(analytics.instrument_tool(search_b, name="search-b"), name="search_b")

    # A binds with NO identity opts at all; B later binds with an explicit
    # identity, moving the singleton identity mirror to bob.
    analytics.instrument_server(mcp_a)
    analytics.instrument_server(mcp_b, user_id="bob@example.com")

    async with AsyncExitStack() as stack:
        client_a = await stack.enter_async_context(
            create_connected_server_and_client_session(mcp_a._mcp_server, client_info=CLIENT)
        )
        client_b = await stack.enter_async_context(
            create_connected_server_and_client_session(mcp_b._mcp_server, client_info=CLIENT)
        )
        await client_a.call_tool("search_a", {"q": "hi"})
        await client_b.call_tool("search_b", {"q": "hi"})

    events = by_tool(analytics.get_events(RESPONSE))
    # Node adaptation: over its HTTP fake, A's identity-less dispatch degraded
    # to the anonymous floor and emitted NOTHING. The in-memory transport is
    # stdio-shaped, so Python's ladder floors A to the process anchor instead —
    # the event emits, but MUST carry the anchor identity, never bob's. Before
    # the per-server-scope fix, A picked bob up from the singleton mirror.
    assert events["search-a"]["user_id"].startswith(f"process:{os.getpid()}-")
    assert events["search-a"]["user_id"] != "bob@example.com"
    assert events["search-b"]["user_id"] == "bob@example.com"


async def test_direct_invocation_falls_back_to_the_last_connected_scope() -> None:
    analytics = make_analytics()

    async def search(q: str) -> str:
        return "ok"

    wrapped = analytics.instrument_tool(search, name="search")
    mcp_a = FastMCP("test-mcp")
    mcp_a.add_tool(wrapped, name="search")
    analytics.instrument_server(mcp_a, user_id="alice@example.com")

    async with create_connected_server_and_client_session(
        mcp_a._mcp_server, client_info=CLIENT
    ):
        pass  # the run resolved and mirrored the scope onto client._server_ctx

    # No dispatch frame (no run's ContextVar in this task) → the wrapper falls
    # back to the last-connected client._server_ctx, so single-server hosts and
    # direct handler invocation keep working.
    result = await wrapped(q="hi")
    assert result == "ok"

    events = analytics.get_events(RESPONSE)
    assert len(events) == 1
    assert events[0]["user_id"] == "alice@example.com"


async def test_tools_listed_carries_the_binding_identity() -> None:
    analytics = make_analytics()
    mcp = FastMCP("test-mcp")
    mcp.add_tool(lambda q: "ok", name="a")
    mcp.add_tool(lambda q: "ok", name="b")
    analytics.instrument_server(
        mcp,
        user_id="alice@example.com",
        tenant=McpTenant(group_type="org id", group_value="org-a"),
    )

    async with create_connected_server_and_client_session(
        mcp._mcp_server, client_info=CLIENT
    ) as client:
        await client.list_tools()

    events = analytics.get_events("[MCP] Tools Listed")
    assert len(events) == 1
    assert events[0]["user_id"] == "alice@example.com"
    assert events[0]["groups"] == {"org id": "org-a"}
    assert events[0]["event_properties"]["[MCP] Tool Count"] == 2


async def test_session_lifecycle_scoped_per_server_no_cross_instance_bleed() -> None:
    analytics = make_analytics()
    mcp_a = FastMCP("test-mcp")
    mcp_b = FastMCP("test-mcp")
    analytics.instrument_server(mcp_a, user_id="alice@example.com")
    analytics.instrument_server(mcp_b, user_id="bob@example.com")

    async with create_connected_server_and_client_session(
        mcp_a._mcp_server, client_info=CLIENT
    ) as client_a:
        await client_a.send_ping()  # A's handshake fully processed

        # B's run starts and settles WITHOUT ever handshaking, while A's
        # session is still open. B closing must NOT emit — it has no session.
        low_b = mcp_b._mcp_server
        async with create_client_server_memory_streams() as (b_client, b_server):
            server_read, server_write = b_server
            _b_read, b_write = b_client
            async with anyio.create_task_group() as tg:
                tg.start_soon(
                    lambda: low_b.run(
                        server_read, server_write, low_b.create_initialization_options()
                    )
                )
                await b_write.aclose()

    initialized = analytics.get_events("[MCP] Session Initialized")
    ended = analytics.get_events("[MCP] Session Ended")
    assert len(initialized) == 1
    assert initialized[0]["user_id"] == "alice@example.com"
    assert len(ended) == 1
    assert ended[0]["user_id"] == "alice@example.com"


async def test_handshake_values_win_over_bound_client_opts() -> None:
    # instrument_server's `client`/`protocol_version` opts are out-of-band
    # FALLBACKS; the real handshake's values must still win per request.
    analytics = make_analytics()

    async def search(q: str) -> str:
        return "ok"

    mcp = FastMCP("test-mcp")
    mcp.add_tool(analytics.instrument_tool(search, name="search"), name="search")
    analytics.instrument_server(
        mcp,
        user_id="alice@example.com",
        client={"name": "Stale", "version": "0.0.0"},
        protocol_version="2020-01-01",
    )

    async with create_connected_server_and_client_session(
        mcp._mcp_server, client_info=Implementation(name="claude", version="9.9.0")
    ) as client:
        await client.call_tool("search", {"q": "hi"})

    props = analytics.get_events(RESPONSE)[0]["event_properties"]
    assert props["[MCP] Client Name"] == "claude"
    assert props["[MCP] Client Version"] == "9.9.0"
    assert props["[MCP] Protocol Version"] != "2020-01-01"  # negotiated value won


async def test_bound_session_id_anchors_http_tool_events() -> None:
    # Host-managed sessions: an HTTP transport that carries no session id of
    # its own still anchors on the id bound via instrument_server(session_id=).
    analytics = make_analytics()

    async def search(q: str) -> str:
        return "ok"

    mcp = FastMCP("test-mcp")
    mcp.add_tool(analytics.instrument_tool(search, name="search"), name="search")
    analytics.instrument_server(
        mcp,
        user_id="alice@example.com",
        transport="streamable-http",
        session_id="sess-host-managed",
    )

    async with create_connected_server_and_client_session(
        mcp._mcp_server, client_info=CLIENT
    ) as client:
        await client.call_tool("search", {"q": "hi"})

    props = analytics.get_events(RESPONSE)[0]["event_properties"]
    assert props["[MCP] Transport"] == "streamable-http"
    assert props["[MCP] Session ID"] == "sess-host-managed"
    assert props["[MCP] Anchor Type"] == "session-id"
