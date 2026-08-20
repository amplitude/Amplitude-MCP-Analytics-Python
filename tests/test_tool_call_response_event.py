"""Port of the Node repo's test/tracking/events/tool-call-response.test.ts.

Node's `buildToolContext(server, meta, {} as McpExtra)` becomes the core
`build_tool_context(server, meta)` — Python reads the per-request facts from
the SDK's ambient request ContextVar, unset in these tests, which matches
Node's empty `extra`.
"""

from __future__ import annotations

from amplitude_mcp_analytics.context.factory import create_server_context
from amplitude_mcp_analytics.context.types import (
    McpServerInfo,
    McpTenant,
    McpToolContext,
    McpToolMeta,
)
from amplitude_mcp_analytics.core.build_context import build_tool_context
from amplitude_mcp_analytics.errors import McpToolError, build_tool_error
from amplitude_mcp_analytics.tracking.events import emit_tool_call_response
from conftest import make_amplitude


def tool_ctx(meta: McpToolMeta | None = None) -> McpToolContext:
    server = create_server_context(
        server=McpServerInfo(name="svc", version="1.0.0"),
        transport="streamable-http",
        tenant=McpTenant(group_type="org id", group_value="36958"),
    )
    return build_tool_context(server, meta if meta is not None else McpToolMeta(name="search_docs"))


class TestEmitToolCallResponse:
    def test_emits_the_canonical_outcome_props_plus_the_ctx_derived_properties(self) -> None:
        client = make_amplitude()
        emit_tool_call_response(client, tool_ctx(), is_tool_error=False, duration_ms=12.7)

        assert len(client.tracked) == 1
        assert client.tracked[0]["event_type"] == "[MCP] Tool Call Response"
        props = client.tracked[0]["event_properties"]
        assert props["[MCP] Is Error"] is False
        assert props["[MCP] Response Duration"] == 13  # rounded
        assert props["[MCP] Tool Name"] == "search_docs"  # ctx-derived
        # No error keys on success, no size keys when absent.
        assert "[MCP] Error Message" not in props
        assert "[MCP] Request Size" not in props

    def test_includes_request_response_sizes_when_present(self) -> None:
        client = make_amplitude()
        emit_tool_call_response(
            client,
            tool_ctx(),
            is_tool_error=False,
            duration_ms=5,
            request_size_bytes=40,
            response_size_bytes=128,
        )

        props = client.tracked[0]["event_properties"]
        assert props["[MCP] Request Size"] == 40
        assert props["[MCP] Response Size"] == 128

    def test_emits_error_message_plus_code_plus_type_from_ctx_error_on_failure(self) -> None:
        client = make_amplitude()
        ctx = tool_ctx()
        ctx.error = McpToolError(code="thrown_exception", message="boom", type="thrown_exception")
        emit_tool_call_response(client, ctx, is_tool_error=True, duration_ms=1)

        props = client.tracked[0]["event_properties"]
        assert props["[MCP] Is Error"] is True
        assert props["[MCP] Error Message"] == "boom"
        assert props["[MCP] Error Code"] == "thrown_exception"
        assert props["[MCP] Error Type"] == "thrown_exception"
        assert "[MCP] Error HTTP Status" not in props

    def test_omits_error_code_when_the_classified_error_carries_no_code(self) -> None:
        client = make_amplitude()
        ctx = tool_ctx()
        ctx.error = McpToolError(message="boom", type="thrown_exception")
        emit_tool_call_response(client, ctx, is_tool_error=True, duration_ms=1)

        props = client.tracked[0]["event_properties"]
        assert props["[MCP] Error Message"] == "boom"
        assert props["[MCP] Error Type"] == "thrown_exception"
        assert "[MCP] Error Code" not in props

    def test_emits_the_host_supplied_code_from_tool_error_as_error_code(self) -> None:
        client = make_amplitude()
        ctx = tool_ctx()
        ctx.error = build_tool_error(code="missing_chart_id", message="No chart ID.")
        emit_tool_call_response(client, ctx, is_tool_error=True, duration_ms=1)

        props = client.tracked[0]["event_properties"]
        assert props["[MCP] Error Code"] == "missing_chart_id"
        # host-built errors are always in-band returned errors
        assert props["[MCP] Error Type"] == "returned_error"

    def test_emits_the_error_http_status_when_the_error_carries_one(self) -> None:
        client = make_amplitude()
        ctx = tool_ctx()
        ctx.error = build_tool_error(
            code="upstream_denied",
            message="Access denied by upstream.",
            http_status=403,
        )
        emit_tool_call_response(client, ctx, is_tool_error=True, duration_ms=1)

        props = client.tracked[0]["event_properties"]
        assert props["[MCP] Is Error"] is True
        assert props["[MCP] Error HTTP Status"] == 403

    def test_emits_rationale_when_the_host_set_a_rationale_on_the_ctx(self) -> None:
        client = make_amplitude()
        ctx = tool_ctx()
        assert ctx.request is not None  # build_tool_context always attaches one
        ctx.request.rationale = "checking config before mutation"
        emit_tool_call_response(client, ctx, is_tool_error=False, duration_ms=12.7)

        props = client.tracked[0]["event_properties"]
        assert props["[MCP] Rationale"] == "checking config before mutation"

    def test_emits_a_tools_extra_enrichment_as_event_properties(self) -> None:
        client = make_amplitude()
        emit_tool_call_response(
            client,
            tool_ctx(McpToolMeta(name="search_docs", extra={"org url": "amp"})),
            is_tool_error=False,
            duration_ms=12.7,
        )

        assert client.tracked[0]["event_properties"]["org url"] == "amp"

    def test_lets_the_outcome_props_override_a_colliding_extra_value(self) -> None:
        # SDK value wins
        client = make_amplitude()
        ctx = tool_ctx(McpToolMeta(name="search_docs", extra={"[MCP] Is Error": "bogus"}))
        emit_tool_call_response(client, ctx, is_tool_error=False, duration_ms=12.7)

        assert client.tracked[0]["event_properties"]["[MCP] Is Error"] is False
