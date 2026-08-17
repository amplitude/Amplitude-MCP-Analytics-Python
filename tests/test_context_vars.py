"""Port of the Node repo's test/als.test.ts — the ambient context accessor.
Node's AsyncLocalStorage becomes a ContextVar (see src context/vars.py)."""

from __future__ import annotations

from typing import Any

import anyio
import pytest

from amplitude_mcp_analytics.context.factory import create_server_context
from amplitude_mcp_analytics.context.types import McpServerInfo
from amplitude_mcp_analytics.context.vars import get_current_context, run_with_context

ctx = create_server_context(server=McpServerInfo(name="my-server"), transport="stdio")


class TestAmbientContext:
    def test_is_none_outside_any_scope(self) -> None:
        assert get_current_context() is None

    def test_exposes_the_context_inside_the_scope_and_returns_the_fn_result(self) -> None:
        seen: dict[str, Any] = {}

        def fn() -> int:
            seen["ctx"] = get_current_context()
            return 42

        result = run_with_context(ctx, fn)

        assert seen["ctx"] is ctx
        assert result == 42

    @pytest.mark.anyio
    async def test_propagates_across_awaited_async_work_within_the_scope(self) -> None:
        seen: dict[str, Any] = {}

        async def fn() -> None:
            await anyio.sleep(0)  # Node's `await Promise.resolve()`
            seen["ctx"] = get_current_context()

        await run_with_context(ctx, fn)

        assert seen["ctx"] is ctx

    def test_restores_the_outer_context_after_a_nested_scope(self) -> None:
        inner = create_server_context(server=McpServerInfo(name="inner"), transport="stdio")
        seen: dict[str, Any] = {}

        def outer_fn() -> None:
            def inner_fn() -> None:
                seen["inner"] = get_current_context()

            run_with_context(inner, inner_fn)
            seen["after_inner"] = get_current_context()

        run_with_context(ctx, outer_fn)

        assert seen["inner"] is inner
        assert seen["after_inner"] is ctx

    def test_is_none_again_after_the_scope_exits(self) -> None:
        run_with_context(ctx, lambda: None)
        assert get_current_context() is None
