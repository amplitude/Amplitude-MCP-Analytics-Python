"""Ports the Node repo's test/set-rationale.test.ts — the module-level
``set_rationale`` on an ambient tool context, and ``analytics.set_rationale()``
inside an instrumented tool handler emitting ``[MCP] Rationale``.

The server scope is bound by hand (``mock._server_ctx`` — the last-connected
fallback used outside a dispatch frame), mirroring the Node tests casting to
reach ``_serverCtx``.
"""

from __future__ import annotations

from typing import Any

import pytest

from amplitude_mcp_analytics import (
    McpRequestInfo,
    McpServerInfo,
    McpToolMeta,
    SetIdentityInput,
    create_server_context,
    create_tool_context,
    get_current_context,
    run_with_context,
    set_rationale,
)
from amplitude_mcp_analytics.context.types import McpToolContext
from amplitude_mcp_analytics.testing import MockAmplitudeMCPAnalytics
from amplitude_mcp_analytics.types import AmplitudeEvent

OK = {"content": [{"type": "text", "text": "ok"}]}


def make_mock() -> MockAmplitudeMCPAnalytics:
    return MockAmplitudeMCPAnalytics(server_name="test-mcp", server_version="9.9.9")


def bind(mock: MockAmplitudeMCPAnalytics, transport: str = "streamable-http") -> None:
    mock._server_ctx = create_server_context(
        server=McpServerInfo(name="test-mcp", version="9.9.9"),
        transport=transport,  # type: ignore[arg-type]
    )


def mk_tool_ctx() -> McpToolContext:
    return create_tool_context(
        create_server_context(
            server=McpServerInfo(name="test", version="1"),
            transport="streamable-http",
        ),
        McpToolMeta(name="search"),
        request=McpRequestInfo(method="tools/call"),
    )


def find_event(events: list[AmplitudeEvent], event_type: str) -> AmplitudeEvent | None:
    return next((e for e in events if e.get("event_type") == event_type), None)


class TestSetRationale:
    def test_raises_when_called_outside_a_context_scope(self) -> None:
        with pytest.raises(RuntimeError, match="outside an active context scope"):
            set_rationale("why not")

    def test_sets_the_rationale_on_the_ambient_tool_context(self) -> None:
        ctx = mk_tool_ctx()

        run_with_context(ctx, lambda: set_rationale("need project ids before querying"))

        assert ctx.request is not None
        assert ctx.request.rationale == "need project ids before querying"

    def test_preserves_other_request_fields_when_setting(self) -> None:
        ctx = mk_tool_ctx()

        run_with_context(ctx, lambda: set_rationale("why"))

        assert ctx.request is not None
        assert ctx.request.method == "tools/call"

    def test_ignores_empty_and_non_string_values(self) -> None:
        ctx = mk_tool_ctx()

        def scope() -> None:
            set_rationale("")
            set_rationale(None)  # type: ignore[arg-type]
            set_rationale(42)  # type: ignore[arg-type]

        run_with_context(ctx, scope)

        assert ctx.request is not None
        assert ctx.request.rationale is None

    def test_truncates_to_1000_characters(self) -> None:
        ctx = mk_tool_ctx()

        run_with_context(ctx, lambda: set_rationale("x" * 5000))

        assert ctx.request is not None
        assert ctx.request.rationale is not None
        # Rationale is LLM-generated free text; the cap keeps a runaway agent
        # from bloating every event on the call.
        assert len(ctx.request.rationale) == 1000

    def test_last_write_wins_when_called_more_than_once(self) -> None:
        ctx = mk_tool_ctx()

        def scope() -> None:
            set_rationale("first")
            set_rationale("second")

        run_with_context(ctx, scope)

        assert ctx.request is not None
        assert ctx.request.rationale == "second"


@pytest.mark.anyio
class TestSetRationaleViaAnalyticsInsideInstrumentTool:
    async def test_emits_rationale_on_the_default_tool_call_event(self) -> None:
        mock = make_mock()
        bind(mock, "streamable-http")

        async def handler(**_kwargs: Any) -> dict[str, Any]:
            # Identity so `should_emit` passes — mirrors real host wiring.
            mock.set_identity(SetIdentityInput(user_id="alice@example.com"))
            mock.set_rationale("verify chart before mutation")
            return OK

        wrapped = mock.instrument_tool(handler, McpToolMeta(name="search"))
        await wrapped(a=1)

        event = find_event(mock.events, "[MCP] Tool Call Response")
        assert event is not None
        assert event["event_properties"]["[MCP] Rationale"] == "verify chart before mutation"

    async def test_is_inherited_by_tool_scope_custom_events_of_the_same_invocation(
        self,
    ) -> None:
        mock = make_mock()
        bind(mock, "streamable-http")

        async def handler(**_kwargs: Any) -> dict[str, Any]:
            mock.set_identity(SetIdentityInput(user_id="alice@example.com"))
            mock.set_rationale("inherited by customs")
            ctx = get_current_context()
            assert isinstance(ctx, McpToolContext)
            mock.track_tool_event(ctx, "[MCP] Query Tool Call", {"status": "pass"})
            return OK

        wrapped = mock.instrument_tool(handler, McpToolMeta(name="search"))
        await wrapped(a=1)

        custom = find_event(mock.events, "[MCP] Query Tool Call")
        assert custom is not None
        assert custom["event_properties"]["[MCP] Rationale"] == "inherited by customs"

    async def test_works_from_a_shared_helper_at_any_async_depth(self) -> None:
        mock = make_mock()
        bind(mock, "streamable-http")

        async def resolve_and_set_rationale() -> None:
            mock.set_rationale("deep-call rationale")

        async def handler(**_kwargs: Any) -> dict[str, Any]:
            mock.set_identity(SetIdentityInput(user_id="alice@example.com"))
            await resolve_and_set_rationale()
            return OK

        wrapped = mock.instrument_tool(handler, McpToolMeta(name="search"))
        await wrapped(a=1)

        event = find_event(mock.events, "[MCP] Tool Call Response")
        assert event is not None
        assert event["event_properties"]["[MCP] Rationale"] == "deep-call rationale"

    async def test_emits_no_rationale_when_never_set(self) -> None:
        mock = make_mock()
        bind(mock, "streamable-http")

        async def handler(**_kwargs: Any) -> dict[str, Any]:
            mock.set_identity(SetIdentityInput(user_id="alice@example.com"))
            return OK

        wrapped = mock.instrument_tool(handler, McpToolMeta(name="search"))
        await wrapped(a=1)

        event = find_event(mock.events, "[MCP] Tool Call Response")
        assert event is not None
        # Rationale is an optional opt-in — the property must be absent, not empty.
        assert "[MCP] Rationale" not in event["event_properties"]
