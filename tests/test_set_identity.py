"""Ports the Node repo's test/set-identity.test.ts — the module-level
``set_identity`` on an ambient context, and ``analytics.set_identity()`` inside
an instrumented tool handler.

The server scope is bound by hand (``mock._server_ctx`` — the last-connected
fallback used outside a dispatch frame), mirroring the Node tests casting to
reach ``_serverCtx``.
"""

from __future__ import annotations

from typing import Any

import pytest

from amplitude_mcp_analytics import (
    McpIdentity,
    McpServerInfo,
    McpTenant,
    McpToolMeta,
    SetIdentityInput,
    create_server_context,
    get_current_context,
    run_with_context,
    set_identity,
)
from amplitude_mcp_analytics.context.types import McpServerContext, McpToolContext
from amplitude_mcp_analytics.testing import MockAmplitudeMCPAnalytics

OK = {"content": [{"type": "text", "text": "ok"}]}


def make_mock() -> MockAmplitudeMCPAnalytics:
    return MockAmplitudeMCPAnalytics(server_name="test-mcp", server_version="9.9.9")


def bind(mock: MockAmplitudeMCPAnalytics, transport: str = "streamable-http") -> None:
    mock._server_ctx = create_server_context(
        server=McpServerInfo(name="test-mcp", version="9.9.9"),
        transport=transport,  # type: ignore[arg-type]
    )


class TestSetIdentity:
    def test_raises_when_called_outside_a_context_scope(self) -> None:
        with pytest.raises(RuntimeError, match="outside an active context scope"):
            set_identity(SetIdentityInput(user_id="alice"))

    def test_overrides_identity_on_the_ambient_context_user_id(self) -> None:
        ctx = create_server_context(
            server=McpServerInfo(name="test", version="1"),
            transport="stdio",
        )
        snapshot: dict[str, McpServerContext | None] = {}

        def scope() -> None:
            set_identity(SetIdentityInput(user_id="alice@example.com"))
            snapshot["ctx"] = get_current_context()

        run_with_context(ctx, scope)

        assert snapshot["ctx"] is not None
        assert snapshot["ctx"].identity.user_id == "alice@example.com"
        assert snapshot["ctx"].identity.resolved_from == "explicit"

    def test_overrides_identity_on_the_ambient_context_device_id_only(self) -> None:
        ctx = create_server_context(
            server=McpServerInfo(name="test", version="1"),
            transport="stdio",
        )
        snapshot: dict[str, McpServerContext | None] = {}

        def scope() -> None:
            set_identity(SetIdentityInput(device_id="my-device-id"))
            snapshot["ctx"] = get_current_context()

        run_with_context(ctx, scope)

        assert snapshot["ctx"] is not None
        assert snapshot["ctx"].identity.device_id == "my-device-id"
        assert snapshot["ctx"].identity.resolved_from == "explicit"

    def test_sets_tenant_on_the_ambient_context(self) -> None:
        ctx = create_server_context(
            server=McpServerInfo(name="test", version="1"),
            transport="stdio",
        )
        snapshot: dict[str, McpServerContext | None] = {}

        def scope() -> None:
            set_identity(
                SetIdentityInput(tenant=McpTenant(group_type="org id", group_value="42"))
            )
            snapshot["ctx"] = get_current_context()

        run_with_context(ctx, scope)

        assert snapshot["ctx"] is not None
        assert snapshot["ctx"].tenant == McpTenant(group_type="org id", group_value="42")

    def test_does_not_change_resolved_from_when_only_tenant_is_set(self) -> None:
        ctx = create_server_context(
            server=McpServerInfo(name="test", version="1"),
            transport="stdio",
            identity=McpIdentity(resolved_from="anchor", user_id="process:1234"),
        )

        run_with_context(
            ctx,
            lambda: set_identity(
                SetIdentityInput(tenant=McpTenant(group_type="org id", group_value="42"))
            ),
        )

        # Tenant-only input must not reclassify how the subject was resolved.
        assert ctx.identity.resolved_from == "anchor"
        assert ctx.identity.user_id == "process:1234"


@pytest.mark.anyio
class TestSetIdentityViaAnalyticsInsideInstrumentTool:
    async def test_overrides_the_fallback_chain_identity_during_handler_execution(
        self,
    ) -> None:
        mock = make_mock()
        bind(mock, "streamable-http")

        seen: dict[str, Any] = {}

        async def handler(**_kwargs: Any) -> dict[str, Any]:
            mock.set_identity(
                SetIdentityInput(
                    user_id="alice@example.com",
                    tenant=McpTenant(group_type="org id", group_value="42"),
                )
            )
            seen["ctx"] = get_current_context()
            return OK

        wrapped = mock.instrument_tool(handler, McpToolMeta(name="search"))
        await wrapped(a=1)

        ctx: McpToolContext = seen["ctx"]
        assert ctx.identity.user_id == "alice@example.com"
        assert ctx.identity.resolved_from == "explicit"
        assert ctx.tenant == McpTenant(group_type="org id", group_value="42")

    async def test_set_identity_from_a_shared_helper_at_any_async_depth(self) -> None:
        mock = make_mock()
        bind(mock, "streamable-http")

        async def resolve_and_set_identity() -> None:
            mock.set_identity(SetIdentityInput(user_id="deep-call@example.com"))

        seen: dict[str, Any] = {}

        async def handler(**_kwargs: Any) -> dict[str, Any]:
            await resolve_and_set_identity()
            seen["ctx"] = get_current_context()
            return OK

        wrapped = mock.instrument_tool(handler, McpToolMeta(name="search"))
        await wrapped(a=1)

        ctx: McpToolContext = seen["ctx"]
        # ContextVars flow into awaited helpers, so the deep call mutates the
        # same per-request ctx.
        assert ctx.identity.user_id == "deep-call@example.com"
        assert ctx.identity.resolved_from == "explicit"
