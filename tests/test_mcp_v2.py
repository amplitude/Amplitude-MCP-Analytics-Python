"""Behavior coverage for the context-tier MCP server API."""

from __future__ import annotations

import inspect
from importlib.metadata import version
from types import SimpleNamespace
from typing import Any

import anyio
import pytest

from amplitude_mcp_analytics import MCPAnalyticsConfig
from amplitude_mcp_analytics.core.mcp import current_request_context, unwrap_server
from amplitude_mcp_analytics.core.server_scope import current_server_scope
from amplitude_mcp_analytics.testing import MockAmplitudeMCPAnalytics

if version("mcp").split(".")[0] != "2":
    pytest.skip("Requires mcp 2.x", allow_module_level=True)

from mcp import Client  # noqa: E402
from mcp.client._memory import InMemoryTransport  # noqa: E402
from mcp.server import Server  # noqa: E402
from mcp.server.mcpserver import Context, MCPServer  # noqa: E402
from mcp.shared.message import SessionMessage  # noqa: E402
from mcp.types import (  # noqa: E402
    CallToolRequestParams,
    CallToolResult,
    Implementation,
    JSONRPCRequest,
    ListToolsResult,
    PaginatedRequestParams,
    Tool,
)

pytestmark = pytest.mark.anyio
RESPONSE = "[MCP] Tool Call Response"
REJECTED = "[MCP] Tool Call Rejected"
LISTED = "[MCP] Tools Listed"
INITIALIZED = "[MCP] Session Initialized"
ENDED = "[MCP] Session Ended"


async def test_initialize_uses_negotiated_protocol_version() -> None:
    a = analytics()
    server = MCPServer("server")
    a.instrument_server(server, user_id="user-1")
    async with InMemoryTransport(server) as (read, write):
        await write.send(
            SessionMessage(
                JSONRPCRequest(
                    jsonrpc="2.0",
                    id=1,
                    method="initialize",
                    params={
                        "protocolVersion": "2024-11-05",
                        "capabilities": {},
                        "clientInfo": {"name": "older-client", "version": "1"},
                    },
                )
            )
        )
        reply = await read.receive()
        negotiated = reply.message.result["protocolVersion"]
        assert negotiated == "2024-11-05"
        assert (
            a.get_events(INITIALIZED)[0]["event_properties"]["[MCP] Protocol Version"] == negotiated
        )
    assert a.get_events(ENDED)[0]["event_properties"]["[MCP] Protocol Version"] == negotiated


async def test_initialize_preserves_configured_user_agent() -> None:
    a = analytics()
    server = MCPServer("server")
    a.instrument_server(server, user_id="user-1", client={"user_agent": "configured-agent"})
    async with Client(server, mode="legacy", client_info=Implementation(name="peer", version="7")):
        props = a.get_events(INITIALIZED)[0]["event_properties"]
        assert props["[MCP] Client Name"] == "peer"
        assert props["[MCP] Client Version"] == "7"
        assert props["[MCP] User Agent"] == "configured-agent"


def analytics(config: MCPAnalyticsConfig | None = None) -> MockAmplitudeMCPAnalytics:
    return MockAmplitudeMCPAnalytics(server_name="server", server_version="1", config=config)


@pytest.mark.parametrize("mode", ["legacy", "2026-07-28"])
async def test_tools_and_context(mode: str) -> None:
    a = analytics()
    server = MCPServer("server")
    seen = []

    def decorated(x: int) -> str:
        ctx = current_request_context()
        assert ctx is not None
        assert ctx.method == "tools/call"
        seen.append(ctx.request_id)
        return str(x)

    wrapped = a.instrument_tool(decorated, name="decorated")
    assert inspect.signature(wrapped) == inspect.signature(decorated)
    assert server.tool()(wrapped) is wrapped

    @server.tool()
    def plain(x: int) -> str:
        return str(x)

    a.instrument_server(server, user_id="user-1")
    async with Client(
        server, mode=mode, client_info=Implementation(name="research", version="7")
    ) as c:
        result = await c.list_tools()
        assert len(result.tools) == 2
        await c.call_tool("decorated", {"x": 1})
        await c.call_tool("plain", {"x": 2})
        await c.call_tool("missing", {})
        await c.call_tool("decorated", {"x": "bad"})

    assert seen and current_request_context() is None
    assert current_server_scope() is None
    responses = a.get_events(RESPONSE)
    assert len(responses) == 1
    assert responses[0]["event_properties"]["[MCP] Tool Name"] == "decorated"
    assert {e["event_properties"]["[MCP] Rejection Reason"] for e in a.get_events(REJECTED)} == {
        "unknown_tool",
        "schema_validation",
    }
    props = a.get_events(LISTED)[0]["event_properties"]
    assert props["[MCP] Tool Count"] == 2
    assert set(props["[MCP] Tool Names"]) == {"plain", "decorated"}
    if mode == "legacy":
        assert len(a.get_events(INITIALIZED)) == 1
        assert len(a.get_events(ENDED)) == 1
    else:
        assert not a.get_events(INITIALIZED)
        assert not a.get_events(ENDED)
        for event in responses:
            assert event["event_properties"]["[MCP] Client Name"] == "research"
            assert event["event_properties"]["[MCP] Protocol Version"] == mode


@pytest.mark.parametrize("mode", ["legacy", "2026-07-28"])
async def test_feedback_and_double_instrument(mode: str) -> None:
    a = analytics()
    server = MCPServer("server")
    a.instrument_server(server, user_id="user-1")
    low, _ = unwrap_server(server)
    count = len(server.middleware)
    run = low.run
    assert a.instrument_server(server) is server
    assert a.instrument_server(low) is low
    assert len(server.middleware) == count
    assert low.run is run
    a.register_feedback_tool(server)
    async with Client(server, mode=mode) as c:
        await c.call_tool("submit_feedback", {"helpful": True})
    assert len(a.get_events("[MCP] Feedback Submitted")) == 1
    assert not a.get_events(REJECTED)


@pytest.mark.parametrize("mode", ["legacy", "2026-07-28"])
async def test_lowlevel_server(mode: str, caplog: pytest.LogCaptureFixture) -> None:
    a = analytics()
    server = Server("low")

    async def listed(ctx: Any, params: PaginatedRequestParams) -> ListToolsResult:
        return ListToolsResult(tools=[Tool(name="hello", input_schema={"type": "object"})])

    async def called(ctx: Any, params: CallToolRequestParams) -> CallToolResult:
        assert current_request_context() is ctx
        return CallToolResult(content=[])

    server.add_request_handler("tools/list", PaginatedRequestParams, listed)
    server.add_request_handler("tools/call", CallToolRequestParams, called)
    a.instrument_server(server, user_id="user-1")
    a.register_feedback_tool(server)
    assert "was not registered" in caplog.text
    async with Client(server, mode=mode) as c:
        await c.list_tools()
        await c.call_tool("hello", {})
    assert not a.get_events(RESPONSE)
    assert len(a.get_events(LISTED)) == 1


async def test_injected_context_is_not_captured() -> None:
    a = analytics()
    server = MCPServer("server")

    @server.tool()
    @a.instrument_tool(name="hello")
    def hello(ctx: Context) -> str:
        assert current_request_context() is ctx.request_context
        return "hello"

    a.instrument_server(server, user_id="user-1")
    async with Client(server, mode="2026-07-28") as c:
        await c.call_tool("hello", {})
    props = a.get_events(RESPONSE)[0]["event_properties"]
    assert not any("ctx" in str(value) for key, value in props.items() if "Param" in key)


@pytest.mark.parametrize("method", ["tools/list", "tools/call", "initialize"])
async def test_empty_list_and_error_passthrough(method: str) -> None:
    a = analytics()
    server = MCPServer("server")
    a.instrument_server(server, user_id="user-1")
    async with Client(server, mode="2026-07-28") as c:
        await c.list_tools()
    assert a.get_events(LISTED)[0]["event_properties"]["[MCP] Tool Count"] == 0
    observer = server.middleware[-1]
    ctx = SimpleNamespace(method=method, params={}, request=None)
    failure = RuntimeError("failure")

    async def fail(ctx: Any) -> Any:
        raise failure

    with pytest.raises(RuntimeError) as caught:
        await observer(ctx, fail)
    assert caught.value is failure
    assert current_request_context() is None
    assert current_server_scope() is None
    assert not a.get_events(INITIALIZED)
    assert not a.get_events(ENDED)


async def test_http_evidence_and_concurrent_request_isolation() -> None:
    a = analytics()
    server = MCPServer("server")
    a.instrument_server(server, user_id="user-1")
    observer = server.middleware[-1]
    result = {"tools": []}

    async def request(name: str) -> None:
        ctx = SimpleNamespace(
            method="tools/list",
            params={},
            request_id=name,
            meta={"io.modelcontextprotocol/clientInfo": {"name": name}},
            protocol_version="2026-07-28",
            request=SimpleNamespace(headers={}, query_params={}),
        )

        async def next_(ctx: Any) -> Any:
            await anyio.sleep(0)
            assert current_request_context() is ctx
            return result

        assert await observer(ctx, next_) is result

    async with anyio.create_task_group() as tg:
        tg.start_soon(request, "one")
        tg.start_soon(request, "two")
    events = a.get_events(LISTED)
    assert {e["event_properties"]["[MCP] Client Name"] for e in events} == {"one", "two"}
    assert all(e["event_properties"]["[MCP] Transport"] == "streamable-http" for e in events)
    assert not a.get_events(INITIALIZED)
    assert not a.get_events(ENDED)


async def test_autocapture_disabled_and_lowlevel_first_binding() -> None:
    a = analytics(
        MCPAnalyticsConfig(
            autocapture={
                "tool_calls": False,
                "tools_listed": False,
                "session_lifecycle": False,
            }
        )
    )
    server = MCPServer("server")
    low, _ = unwrap_server(server)
    a.instrument_server(low, user_id="user-1")
    count = len(server.middleware)
    a.instrument_server(server)
    assert len(server.middleware) == count

    @server.tool()
    @a.instrument_tool(name="hello")
    def hello() -> str:
        assert current_request_context() is not None
        return "hello"

    async with Client(server, mode="legacy") as c:
        await c.list_tools()
        await c.call_tool("hello", {})
        await c.call_tool("missing", {})
    assert not a.events


@pytest.mark.parametrize("decorated", [True, False])
async def test_tool_failures_are_responses_not_rejections(decorated: bool) -> None:
    a = analytics()
    server = MCPServer("server")

    def fail() -> str:
        raise ValueError("tool failed")

    server.add_tool(a.instrument_tool(fail, name="fail") if decorated else fail)
    a.instrument_server(server, user_id="user-1")
    async with Client(server, mode="2026-07-28") as c:
        result = await c.call_tool("fail", {})
        assert result.is_error
    assert not a.get_events(REJECTED)
    responses = a.get_events(RESPONSE)
    assert len(responses) == int(decorated)
    if decorated:
        assert responses[0]["event_properties"]["[MCP] Is Error"] is True
