"""Real stdio and loopback Streamable HTTP coverage across both MCP eras."""

from __future__ import annotations

import json
import socket
import sys
from contextlib import AsyncExitStack, asynccontextmanager
from importlib.metadata import version
from pathlib import Path
from typing import Any

import anyio
import httpx
import pytest
import uvicorn
from mcp.types import Implementation

from amplitude_mcp_analytics import set_rationale
from amplitude_mcp_analytics.core.mcp import current_request_context
from amplitude_mcp_analytics.core.server_scope import current_server_scope
from amplitude_mcp_analytics.testing import MockAmplitudeMCPAnalytics

V2 = version("mcp").split(".")[0] == "2"
pytestmark = pytest.mark.anyio
RESPONSE = "[MCP] Tool Call Response"
INITIALIZED = "[MCP] Session Initialized"
ENDED = "[MCP] Session Ended"


@asynccontextmanager
async def client_connection(target: Any, name: str, mode: str = "legacy"):
    info = Implementation(name=name, version="7")
    async with AsyncExitStack() as stack:
        if V2:
            from mcp import Client

            client = await stack.enter_async_context(
                Client(target, mode=mode, client_info=info, read_timeout_seconds=10)
            )
        else:
            from mcp import ClientSession
            from mcp.client.stdio import stdio_client
            from mcp.client.streamable_http import streamablehttp_client

            if isinstance(target, str):
                read, write, _ = await stack.enter_async_context(streamablehttp_client(target))
            else:
                read, write = await stack.enter_async_context(stdio_client(target))
            client = await stack.enter_async_context(ClientSession(read, write, client_info=info))
            await client.initialize()
        yield client


async def test_stdio_transport_and_lifecycle(tmp_path: Path) -> None:
    from mcp.client.stdio import StdioServerParameters

    output = tmp_path / "events.json"
    script = Path(__file__).with_name("stdio_server.py")
    params = StdioServerParameters(command=sys.executable, args=[str(script), str(output)])
    with anyio.fail_after(25):
        async with client_connection(params, "stdio-client") as client:
            listed = await client.list_tools()
            assert [tool.name for tool in listed.tools] == ["echo"]
            result = await client.call_tool("echo", {"label": "hello"})
            assert result.content[0].text == "hello"
    events = json.loads(output.read_text(encoding="utf-8"))
    assert sum(e["event_type"] == INITIALIZED for e in events) == 1
    assert sum(e["event_type"] == ENDED for e in events) == 1
    assert sum(e["event_type"] == RESPONSE for e in events) == 1
    assert any(e["event_type"] == "[MCP] Tools Listed" for e in events)
    negotiated = next(
        e["event_properties"]["[MCP] Protocol Version"]
        for e in events if e["event_type"] == INITIALIZED
    )
    assert negotiated.startswith("20")
    for event in events:
        props = event["event_properties"]
        assert props["[MCP] Transport"] == "stdio"
        assert props["[MCP] Anchor Type"] == "process"
        assert event["user_id"].startswith("process:")
        assert props["[MCP] Protocol Version"] == negotiated
        assert props["[MCP] Client Name Source"] == "handshake"
        assert "[MCP] User Agent" not in props


@asynccontextmanager
async def http_server(app: Any):
    # Pre-bind port zero to avoid a free-port lookup race.
    with socket.socket() as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="on"))
        stopped = anyio.Event()

        async def serve() -> None:
            try:
                await server.serve(sockets=[listener])
            finally:
                stopped.set()

        async with anyio.create_task_group() as tasks:
            tasks.start_soon(serve)
            try:
                with anyio.fail_after(10):
                    while not server.started:
                        await anyio.sleep(0.01)
                yield f"http://127.0.0.1:{port}/mcp"
            finally:
                server.should_exit = True
                with anyio.fail_after(10):
                    await stopped.wait()
    # The task group has awaited shutdown and the listening socket is closed.
    with socket.socket() as probe:
        assert probe.connect_ex(("127.0.0.1", port)) != 0


@pytest.mark.parametrize("mode", ["legacy", "2026-07-28"])
async def test_streamable_http_transport_and_client_isolation(mode: str) -> None:
    if mode != "legacy" and not V2:
        pytest.skip("The modern protocol requires mcp 2.x")
    if V2:
        from mcp.server.mcpserver import MCPServer

        server = MCPServer("http-compat")
        app = server.streamable_http_app(json_response=True)
    else:
        from mcp.server.fastmcp import FastMCP

        server = FastMCP("http-compat", json_response=True)
        app = server.streamable_http_app()
    analytics = MockAmplitudeMCPAnalytics(server_name="http-compat", server_version="1")
    analytics.instrument_server(server, user_id="http-user")
    scopes = []
    sessions = []

    @server.tool()
    @analytics.instrument_tool(name="echo")
    async def echo(label: str) -> str:
        scope = current_server_scope()
        ctx = current_request_context()
        assert ctx.request is not None
        if V2:
            assert scope is not None and not scope.transport_persists
            assert scope.ctx.client is None  # No handshake cached on this fresh request scope.
            scopes.append(scope)
            sessions.append(
                (ctx.session.client_params.client_info.name, ctx.session.protocol_version)
            )
        await anyio.sleep(0)
        set_rationale(label)
        return label

    if V2:

        async def unexpected_run(*args: Any, **kwargs: Any) -> None:
            pytest.fail("v2 HTTP must bypass Server.run")

        server._lowlevel_server.run = unexpected_run

    async def exercise(url: str, name: str) -> None:
        async with client_connection(url, name, mode) as client:
            listed = await client.list_tools()
            assert [tool.name for tool in listed.tools] == ["echo"]
            for _ in range(2):
                result = await client.call_tool("echo", {"label": name})
                assert result.content[0].text == name

    with anyio.fail_after(30):
        async with http_server(app) as url:
            async with anyio.create_task_group() as clients:
                clients.start_soon(exercise, url, "client-one")
                clients.start_soon(exercise, url, "client-two")
    responses = analytics.get_events(RESPONSE)
    assert len(responses) == 4
    for event in analytics.events:
        props = event["event_properties"]
        assert props["[MCP] Transport"] == "streamable-http"
        assert props["[MCP] Anchor Type"] != "process"
    assert {e["event_properties"]["[MCP] Client Name"] for e in responses} == {
        "client-one",
        "client-two",
    }
    for event in responses:
        props = event["event_properties"]
        assert props["[MCP] Client Name"] == props["[MCP] Rationale"]
        assert props["[MCP] Client Name Source"] == (
            "handshake" if mode == "legacy" else "request-metadata"
        )
        if mode != "legacy":
            assert props["[MCP] Protocol Version"] == "2026-07-28"
        elif V2:
            assert props["[MCP] Protocol Version"] == sessions[0][1]
        else:
            assert props["[MCP] Protocol Version"].startswith("2025-")
    if V2:
        assert len({id(scope) for scope in scopes}) == 4
        assert {name for name, _ in sessions} == {"client-one", "client-two"}
        assert not analytics.get_events(ENDED)
    if mode == "legacy":
        assert len(analytics.get_events(INITIALIZED)) == 2
    else:
        assert not analytics.get_events(INITIALIZED)


@pytest.mark.skipif(V2, reason="Exercises the mcp 1.x legacy HTTP adapter")
@pytest.mark.parametrize("stateless", [False, True], ids=["stateful", "fresh-per-request"])
async def test_legacy_http_uses_negotiated_version_when_request_omits_it(stateless: bool) -> None:
    from mcp.server.fastmcp import FastMCP

    server = FastMCP("http-compat", json_response=True, stateless_http=stateless)
    analytics = MockAmplitudeMCPAnalytics(server_name="http-compat", server_version="1")
    analytics.instrument_server(server, user_id="http-user")

    @server.tool()
    @analytics.instrument_tool(name="echo")
    async def echo(label: str) -> str:
        return label

    async with http_server(server.streamable_http_app()) as url, httpx.AsyncClient(
        headers={
            "accept": "application/json, text/event-stream",
            "user-agent": "claude-code/2.1.311 (sdk-cli)",
        }
    ) as client:
        async def post(**message: Any) -> httpx.Response:
            return await client.post(url, json={"jsonrpc": "2.0", **message})

        if stateless:
            rejected = await post(
                id=0, method="initialize", params={
                    "protocolVersion": "2025-11-25", "capabilities": {},
                    "clientInfo": {"name": "rejected-client"},
                },
            )
            assert "error" in rejected.json()
            assert not analytics.get_events(INITIALIZED)

        initialized = await post(
            id=1, method="initialize", params={
                "protocolVersion": "1900-01-01", "capabilities": {},
                "clientInfo": {"name": "cursor-vscode", "version": "1.0.0"},
            },
        )
        initialized.raise_for_status()
        negotiated = initialized.json()["result"]["protocolVersion"]
        assert negotiated != "1900-01-01"
        session_id = initialized.headers.get("mcp-session-id")
        if session_id:
            client.headers["mcp-session-id"] = session_id
        assert (await post(method="notifications/initialized")).status_code == 202
        call = await post(
            id=2, method="tools/call", params={"name": "echo", "arguments": {"label": "hello"}},
        )
        assert call.json()["result"]["content"][0]["text"] == "hello"

        if not stateless:
            await post(
                id=3, method="tools/call", params={
                    "name": "echo", "arguments": {"label": "override"},
                    "_meta": {
                        "io.modelcontextprotocol/clientInfo": {"name": "openai-mcp"},
                        "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                    },
                },
            )
            await post(
                id=4, method="tools/call",
                params={"name": "echo", "arguments": {"label": "after-override"}},
            )

    init = analytics.get_events(INITIALIZED)[0]["event_properties"]
    assert init["[MCP] Protocol Version"] == negotiated
    assert init["[MCP] Client Family"] == "cursor"
    assert init["[MCP] Client Surface"] == "vscode"
    assert init["[MCP] Client Name Source"] == "handshake"
    assert init["[MCP] Client Family Source"] == "client-name"
    props = analytics.get_events(RESPONSE)[0]["event_properties"]
    if stateless:
        assert "[MCP] Protocol Version" not in props
        assert props["[MCP] Client Name"] == "unknown"
        assert props["[MCP] Client Name Source"] == "none"
        assert props["[MCP] Client Family"] == "claude-code"
        assert props["[MCP] Client Surface"] == "cli"
        assert props["[MCP] Client Family Source"] == "user-agent"
        assert not analytics.get_events(ENDED)
    else:
        assert props["[MCP] Protocol Version"] == negotiated
        assert props["[MCP] Client Name Source"] == "handshake"
        assert props["[MCP] Client Family"] == "cursor"
        ended = analytics.get_events(ENDED)[0]["event_properties"]
        assert ended["[MCP] Protocol Version"] == negotiated
        assert "[MCP] User Agent" not in ended

        _, overridden, later = [
            event["event_properties"] for event in analytics.get_events(RESPONSE)
        ]
        assert overridden["[MCP] Client Name Source"] == "request-metadata"
        assert overridden["[MCP] Client Family"] == "chatgpt"
        assert "[MCP] Client Version" not in overridden
        assert overridden["[MCP] Protocol Version"] == "2026-07-28"
        assert later["[MCP] Client Name Source"] == "handshake"
        assert later["[MCP] Client Family"] == "cursor"
        assert later["[MCP] Protocol Version"] == negotiated
