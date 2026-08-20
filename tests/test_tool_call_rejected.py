"""Ports test/tool-call-rejected.test.ts (Node) — ``[MCP] Tool Call Rejected``
dispatch semantics.

Adapted to the Python seam: Node drove a hand-rolled server whose fake
``tools/call`` handler encoded the pre-1.21 SDK contract (pre-dispatch failures
threw). The Python SDK funnels every ``tools/call`` failure through an in-band
``isError`` result (the >=1.21 shape), and the rejection hook only emits inside
a run frame — so these tests drive a REAL FastMCP server over the in-memory
transport instead of a fake handler map."""

from __future__ import annotations

import pytest
from mcp.server.fastmcp import FastMCP
from mcp.shared.memory import create_connected_server_and_client_session
from mcp.types import Implementation

from amplitude_mcp_analytics import MCPAnalyticsConfig
from amplitude_mcp_analytics.testing import MockAmplitudeMCPAnalytics

pytestmark = pytest.mark.anyio

CLIENT = Implementation(name="cursor", version="0.40")

REJECTED = "[MCP] Tool Call Rejected"
RESPONSE = "[MCP] Tool Call Response"


def make_harness(
    config: MCPAnalyticsConfig | None = None,
    *,
    transport: str | None = None,
    search_raises: bool = False,
) -> tuple[MockAmplitudeMCPAnalytics, FastMCP]:
    analytics = MockAmplitudeMCPAnalytics(
        server_name="test-mcp", server_version="9.9.9", config=config
    )
    mcp = FastMCP("test-mcp")

    if search_raises:

        async def search(q: str) -> str:
            raise ValueError("upstream exploded")

    else:

        async def search(q: str) -> str:
            return "ok"

    mcp.add_tool(analytics.instrument_tool(search, name="search"), name="search")
    kwargs = {} if transport is None else {"transport": transport}
    analytics.instrument_server(mcp, user_id="user-1", **kwargs)
    return analytics, mcp


async def test_emits_for_an_unknown_tool_rejected_before_any_callback() -> None:
    analytics, mcp = make_harness()
    async with create_connected_server_and_client_session(
        mcp._mcp_server, client_info=CLIENT
    ) as client:
        # The Python SDK reports the pre-dispatch failure as an in-band
        # isError result (the post-1.21 contract), not a raise.
        result = await client.call_tool("made_up_tool", {})
        assert result.isError is True

    rejected = analytics.get_events(REJECTED)
    assert len(rejected) == 1
    event = rejected[0]
    assert event["user_id"] == "user-1"
    props = event["event_properties"]
    assert props["[MCP] Attempted Tool Name"] == "made_up_tool"
    assert props["[MCP] Rejection Reason"] == "unknown_tool"
    assert props["[MCP] Error Type"] == "protocol_error"
    assert props["[MCP] Is Error"] is True
    assert isinstance(props["[MCP] Error Code"], str)
    assert isinstance(props["[MCP] Response Duration"], int)
    assert isinstance(props["[MCP] Response Size"], int)
    # The unvalidated name never lands on the reserved per-tool key.
    assert "[MCP] Tool Name" not in props
    assert analytics.get_events(RESPONSE) == []


async def test_does_not_emit_for_a_dispatched_tool_that_succeeds() -> None:
    analytics, mcp = make_harness()
    async with create_connected_server_and_client_session(
        mcp._mcp_server, client_info=CLIENT
    ) as client:
        result = await client.call_tool("search", {"q": "hi"})
        assert result.isError is False

    assert analytics.get_events(REJECTED) == []
    assert len(analytics.get_events(RESPONSE)) == 1


async def test_dispatched_tool_that_throws_is_owned_by_the_wrapper() -> None:
    # The de-dup marker: the wrapper marks every dispatch it sees, so a tool
    # failure — which the SDK converts to the SAME in-band isError shape as a
    # pre-dispatch rejection — lands ONLY on `[MCP] Tool Call Response`.
    analytics, mcp = make_harness(search_raises=True)
    async with create_connected_server_and_client_session(
        mcp._mcp_server, client_info=CLIENT
    ) as client:
        result = await client.call_tool("search", {"q": "hi"})
        assert result.isError is True  # the SDK's in-band conversion

    assert analytics.get_events(REJECTED) == []
    responses = analytics.get_events(RESPONSE)
    assert len(responses) == 1
    assert responses[0]["event_properties"]["[MCP] Is Error"] is True


async def test_truncates_an_oversized_attempted_tool_name() -> None:
    # Rejected requests carry unvalidated names (typos, hallucinations,
    # scanner junk) — the value is capped to keep property payloads bounded.
    analytics, mcp = make_harness()
    async with create_connected_server_and_client_session(
        mcp._mcp_server, client_info=CLIENT
    ) as client:
        await client.call_tool("x" * 500, {})

    rejected = analytics.get_events(REJECTED)
    assert len(rejected) == 1
    assert rejected[0]["event_properties"]["[MCP] Attempted Tool Name"] == "x" * 200


async def test_emits_nothing_when_autocapture_tool_calls_is_false() -> None:
    analytics, mcp = make_harness(
        MCPAnalyticsConfig(autocapture={"tool_calls": False})
    )
    async with create_connected_server_and_client_session(
        mcp._mcp_server, client_info=CLIENT
    ) as client:
        await client.call_tool("made_up_tool", {})
        await client.call_tool("search", {"q": "hi"})  # dispatched call is silent too

    assert analytics.get_events(REJECTED) == []
    assert analytics.get_events(RESPONSE) == []


async def test_response_http_status_200_on_streamable_http_and_absent_on_stdio() -> None:
    # Protocol-level rejections still answer HTTP 200 over the HTTP transports
    # (the error travels in the response body); stdio has no HTTP status, so
    # none is fabricated there.
    for transport, expected in (("streamable-http", 200), (None, None)):
        analytics, mcp = make_harness(transport=transport)
        async with create_connected_server_and_client_session(
            mcp._mcp_server, client_info=CLIENT
        ) as client:
            await client.call_tool("nope", {})

        rejected = analytics.get_events(REJECTED)
        assert len(rejected) == 1, transport
        props = rejected[0]["event_properties"]
        if expected is None:
            assert "[MCP] Response HTTP Status" not in props
        else:
            assert props["[MCP] Response HTTP Status"] == expected
