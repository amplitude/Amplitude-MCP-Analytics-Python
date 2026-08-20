"""Ports the Node repo's test/identity-integration.test.ts — identity options
flowing through instrument_server/instrument_tool into the per-request ctx.

Adaptations for the Python design:
- Node connects a fake McpServer; here the server scope + server identity are
  bound by hand (``mock._server_ctx`` / ``mock._server_identity`` — the
  last-connected fallback instrument_tool uses outside a dispatch frame),
  mirroring the Node tests casting to reach ``_serverCtx``.
- ``resolve_identity`` is passed to ``instrument_tool`` at wrap time. In a
  direct invocation there is no live MCP request context, so ``auth_info`` is
  always ``None`` — the callback must work from its own sources (Node's tests
  read the extra's authInfo instead).
"""

from __future__ import annotations

from typing import Any

import pytest

from amplitude_mcp_analytics import (
    McpTenant,
    McpToolMeta,
    SetIdentityInput,
    get_current_context,
)
from amplitude_mcp_analytics.context.types import McpToolContext
from amplitude_mcp_analytics.core.identity import ServerIdentity
from amplitude_mcp_analytics.testing import MockAmplitudeMCPAnalytics
from conftest import server_ctx

pytestmark = pytest.mark.anyio

OK = {"content": [{"type": "text", "text": "ok"}]}


def make_mock() -> MockAmplitudeMCPAnalytics:
    return MockAmplitudeMCPAnalytics(server_name="test-mcp", server_version="9.9.9")


def capture_ctx(seen: dict[str, Any]):
    """An async handler that snapshots the ambient tool ctx."""

    async def handler(**_kwargs: Any) -> dict[str, Any]:
        seen["ctx"] = get_current_context()
        return OK

    return handler


class TestInstrumentServerWithIdentityOptions:
    async def test_sets_server_identity_that_flows_into_instrument_tool_ctx(self) -> None:
        mock = make_mock()
        mock._server_ctx = server_ctx()
        mock._server_identity = ServerIdentity(
            user_id="operator@example.com",
            tenant=McpTenant(group_type="org id", group_value="123"),
        )

        seen: dict[str, Any] = {}
        wrapped = mock.instrument_tool(capture_ctx(seen), McpToolMeta(name="search"))

        await wrapped(a=1)

        ctx: McpToolContext = seen["ctx"]
        assert ctx.identity.user_id == "operator@example.com"
        assert ctx.identity.resolved_from == "explicit"
        assert ctx.tenant == McpTenant(group_type="org id", group_value="123")

    async def test_sets_device_id_from_server_options(self) -> None:
        mock = make_mock()
        mock._server_ctx = server_ctx()
        mock._server_identity = ServerIdentity(
            user_id="operator", device_id="my-static-device"
        )

        seen: dict[str, Any] = {}
        wrapped = mock.instrument_tool(capture_ctx(seen), McpToolMeta(name="search"))

        await wrapped(a=1)

        ctx: McpToolContext = seen["ctx"]
        assert ctx.identity.device_id == "my-static-device"


class TestInstrumentToolWithResolveIdentityCallback:
    async def test_resolves_identity_from_the_callback(self) -> None:
        mock = make_mock()
        mock._server_ctx = server_ctx()

        received_auth_info: list[Any] = []

        def resolver(auth_info: dict[str, Any] | None) -> SetIdentityInput:
            received_auth_info.append(auth_info)
            return SetIdentityInput(
                user_id="bob@example.com",
                tenant=McpTenant(group_type="org id", group_value="99"),
            )

        seen: dict[str, Any] = {}
        wrapped = mock.instrument_tool(
            capture_ctx(seen), McpToolMeta(name="search"), resolve_identity=resolver
        )

        await wrapped(a=1)

        ctx: McpToolContext = seen["ctx"]
        assert ctx.identity.user_id == "bob@example.com"
        assert ctx.identity.resolved_from == "authInfo"
        assert ctx.tenant == McpTenant(group_type="org id", group_value="99")
        # Direct invocation runs outside a live MCP request frame, so the
        # callback receives None rather than the request's auth info.
        assert received_auth_info == [None]

    async def test_resolve_identity_wins_over_server_identity(self) -> None:
        mock = make_mock()
        mock._server_ctx = server_ctx()
        mock._server_identity = ServerIdentity(user_id="server-level")

        seen: dict[str, Any] = {}
        wrapped = mock.instrument_tool(
            capture_ctx(seen),
            McpToolMeta(name="search"),
            resolve_identity=lambda _auth: SetIdentityInput(user_id="from-auth"),
        )

        await wrapped(a=1)

        ctx: McpToolContext = seen["ctx"]
        assert ctx.identity.user_id == "from-auth"
        assert ctx.identity.resolved_from == "authInfo"

    async def test_falls_through_to_server_identity_when_callback_returns_empty(self) -> None:
        mock = make_mock()
        mock._server_ctx = server_ctx()
        mock._server_identity = ServerIdentity(user_id="server-level")

        seen: dict[str, Any] = {}
        wrapped = mock.instrument_tool(
            capture_ctx(seen),
            McpToolMeta(name="search"),
            resolve_identity=lambda _auth: SetIdentityInput(),
        )

        await wrapped(a=1)

        ctx: McpToolContext = seen["ctx"]
        assert ctx.identity.user_id == "server-level"
        assert ctx.identity.resolved_from == "explicit"

    async def test_set_identity_during_handler_overrides_resolve_identity(self) -> None:
        mock = make_mock()
        mock._server_ctx = server_ctx()

        seen: dict[str, Any] = {}

        async def handler(**_kwargs: Any) -> dict[str, Any]:
            mock.set_identity(SetIdentityInput(user_id="explicit-override"))
            seen["ctx"] = get_current_context()
            return OK

        wrapped = mock.instrument_tool(
            handler,
            McpToolMeta(name="search"),
            resolve_identity=lambda _auth: SetIdentityInput(user_id="from-auth"),
        )

        await wrapped(a=1)

        ctx: McpToolContext = seen["ctx"]
        # set_identity is the top of the fallback chain — it outranks the callback.
        assert ctx.identity.user_id == "explicit-override"
        assert ctx.identity.resolved_from == "explicit"
