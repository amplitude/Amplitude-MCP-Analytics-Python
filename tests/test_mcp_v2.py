"""Behavior coverage for the context-tier MCP server API."""

from __future__ import annotations

import inspect
import threading
from importlib.metadata import version
from types import SimpleNamespace
from typing import Annotated, Any

import anyio
import pytest

from amplitude_mcp_analytics import MCPAnalyticsConfig
from amplitude_mcp_analytics.core.mcp import (
    _wrap_v2_tool_callback,
    current_request_context,
    unwrap_server,
)
from amplitude_mcp_analytics.core.server_scope import current_server_scope
from amplitude_mcp_analytics.core.tool_call_hook import _dispatch_marker
from amplitude_mcp_analytics.testing import MockAmplitudeMCPAnalytics

if version("mcp").split(".")[0] != "2":
    pytest.skip("Requires mcp 2.x", allow_module_level=True)

from mcp import Client  # noqa: E402
from mcp.client._memory import InMemoryTransport  # noqa: E402
from mcp.server import Server  # noqa: E402
from mcp.server.mcpserver import Context, MCPServer, Resolve  # noqa: E402
from mcp.shared.exceptions import MCPError  # noqa: E402
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


@pytest.mark.parametrize(
    "shape",
    ["no_manager", "no_getter", "getter_raises", "no_tool", "no_fn", "not_callable", "readonly_fn"],
)
def test_callback_wrapping_degrades_silently(shape: str, caplog: pytest.LogCaptureFixture) -> None:
    def original() -> str:
        return "unchanged"

    class ReadonlyTool:
        @property
        def fn(self) -> Any:
            return original

    def fail(name: str) -> Any:
        raise RuntimeError("unavailable registry")

    tool = ReadonlyTool() if shape == "readonly_fn" else SimpleNamespace(fn=original)
    manager = SimpleNamespace(get_tool=lambda name: tool)
    if shape == "no_manager":
        manager = None
    elif shape == "no_getter":
        manager = SimpleNamespace()
    elif shape == "getter_raises":
        manager.get_tool = fail
    elif shape == "no_tool":
        manager.get_tool = lambda name: None
    elif shape == "no_fn":
        del tool.fn
    elif shape == "not_callable":
        tool.fn = None
    _wrap_v2_tool_callback(manager, "example")
    assert not caplog.records
    assert original() == "unchanged"
    if shape not in ("no_fn", "not_callable"):
        assert tool.fn is original


@pytest.mark.parametrize("mode", ["legacy", "2026-07-28"])
@pytest.mark.parametrize("is_async", [True, False])
@pytest.mark.parametrize("decorated", [True, False])
@pytest.mark.parametrize("late_registration", [True, False])
async def test_mcp_error_after_callback_is_not_rejected(
    mode: str,
    is_async: bool,
    decorated: bool,
    late_registration: bool,
) -> None:
    a = analytics()
    server = MCPServer("server")
    main_thread = threading.get_ident()
    calls = []

    def fail(query: str, limit: int = 5) -> str:
        marker = _dispatch_marker.get()
        assert marker is not None and marker.dispatched
        assert marker.supplied_claimed is decorated
        assert marker.supplied_keys == frozenset({"query", "limit"})
        assert current_request_context() is not None
        calls.append(threading.get_ident())
        raise MCPError(-32602, "Invalid request parameters")

    async def async_fail(query: str, limit: int = 5) -> str:
        return fail(query, limit)

    original = async_fail if is_async else fail
    if decorated:
        original = a.instrument_tool(original, name="fail")
    if late_registration:
        a.instrument_server(server, user_id="user-1")
    server.add_tool(original, name="fail")
    if not late_registration:
        a.instrument_server(server, user_id="user-1")
    tool = server._tool_manager.get_tool("fail")
    async with Client(server, mode=mode) as client:
        with pytest.raises(MCPError) as caught:
            await client.call_tool("fail", {"query": "x", "limit": 5})
        assert caught.value.code == -32602
        wrapped = tool.fn
        assert wrapped.__wrapped__ is original
        assert inspect.signature(wrapped) == inspect.signature(original)
        assert inspect.iscoroutinefunction(wrapped) is is_async
        a.instrument_server(server)
        a.instrument_server(server._lowlevel_server)
        with pytest.raises(MCPError):
            await client.call_tool("fail", {"query": "x", "limit": 5})
        assert tool.fn is wrapped
    assert len(calls) == 2
    assert all((thread == main_thread) is is_async for thread in calls)
    assert not a.get_events(REJECTED)
    responses = a.get_events(RESPONSE)
    assert len(responses) == (2 if decorated else 0)
    for event in responses:
        props = event["event_properties"]
        assert props["[MCP] Is Error"] is True
        assert props["[MCP] Param Keys"] == ["limit", "query"]


def _failing_resolver() -> str:
    raise MCPError(-32602, "Invalid request parameters")


async def test_callback_marker_preserves_pre_dispatch_rejections() -> None:
    a = analytics()
    server = MCPServer("server")
    calls = []

    @server.tool()
    def typed(count: int) -> str:
        calls.append("typed")
        return str(count)

    @server.tool()
    def resolved(value: Annotated[str, Resolve(_failing_resolver)]) -> str:
        calls.append("resolved")
        return value

    a.instrument_server(server, user_id="user-1")
    async with Client(server, mode="legacy") as client:
        assert (await client.call_tool("missing", {})).is_error
        assert (await client.call_tool("typed", {"count": "bad"})).is_error
        with pytest.raises(MCPError):
            await client.call_tool("resolved", {})
    assert not calls
    assert not a.get_events(RESPONSE)
    assert [e["event_properties"]["[MCP] Rejection Reason"] for e in a.get_events(REJECTED)] == [
        "unknown_tool",
        "schema_validation",
        "unrecognized",
    ]


async def test_malformed_call_is_a_pre_dispatch_rejection() -> None:
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
                        "protocolVersion": "2025-11-25",
                        "capabilities": {},
                        "clientInfo": {"name": "probe", "version": "1"},
                    },
                )
            )
        )
        await read.receive()
        await write.send(
            SessionMessage(
                JSONRPCRequest(
                    jsonrpc="2.0",
                    id=2,
                    method="tools/call",
                    params={},
                )
            )
        )
        result = await read.receive()
        assert result.message.error.code == -32602
        assert result.message.error.message == "Invalid request parameters"
    assert not a.get_events(RESPONSE)
    assert len(a.get_events(REJECTED)) == 1


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
