"""Port of the Node repo's test/context.test.ts.

Skipped case: Node's "requires transport at the type level" is a
`// @ts-expect-error` compile-time assertion with no Python equivalent
(Python's keyword-only factory signature already raises TypeError at runtime
when `transport` is omitted).
"""

from __future__ import annotations

from amplitude_mcp_analytics.context.factory import create_server_context, create_tool_context
from amplitude_mcp_analytics.context.types import (
    McpAnchor,
    McpClientInfo,
    McpIdentity,
    McpRequestInfo,
    McpServerContext,
    McpServerInfo,
    McpTenant,
    McpToolMeta,
)
from amplitude_mcp_analytics.errors import McpToolError


class TestCreateServerContext:
    def test_fills_the_identity_and_anchor_floor_when_unset(self) -> None:
        ctx = create_server_context(
            server=McpServerInfo(name="my-server"),
            transport="stdio",
        )

        assert ctx.identity == McpIdentity(resolved_from="anonymous")
        assert ctx.anchor == McpAnchor(type="anonymous", value="")
        assert ctx.server == McpServerInfo(name="my-server")

    def test_keeps_caller_supplied_values_caller_over_derived(self) -> None:
        ctx = create_server_context(
            server=McpServerInfo(name="my-server", version="1.0.0", type="remote"),
            identity=McpIdentity(user_id="u1", resolved_from="explicit"),
            anchor=McpAnchor(type="session-id", value="sess-1"),
            transport="streamable-http",
            protocol_version="2026-07-28",
            tenant=McpTenant(group_type="org id", group_value="36958"),
            client=McpClientInfo(name="cursor", version="0.42", user_agent="cursor/0.42"),
        )

        assert ctx.identity == McpIdentity(user_id="u1", resolved_from="explicit")
        assert ctx.anchor == McpAnchor(type="session-id", value="sess-1")
        assert ctx.transport == "streamable-http"
        assert ctx.protocol_version == "2026-07-28"
        assert ctx.tenant == McpTenant(group_type="org id", group_value="36958")
        assert ctx.client is not None and ctx.client.name == "cursor"

    def test_passes_through_the_cross_cutting_fields(self) -> None:
        ctx = create_server_context(
            server=McpServerInfo(name="my-server"),
            transport="streamable-http",
            protocol_version="2026-07-28",
            auth_type="OAuth",
            # server-domain values (e.g. Amplitude's `org url`) ride in `extra`
            extra={"org url": "amplitude"},
        )

        assert ctx.protocol_version == "2026-07-28"
        assert ctx.auth_type == "OAuth"
        assert ctx.extra == {"org url": "amplitude"}

    def test_fills_only_the_fields_the_caller_left_unset(self) -> None:
        # anchor supplied, identity defaulted to the floor
        ctx = create_server_context(
            server=McpServerInfo(name="my-server"),
            transport="stdio",
            anchor=McpAnchor(type="trace", value="trace-abc"),
        )

        assert ctx.anchor == McpAnchor(type="trace", value="trace-abc")
        assert ctx.identity == McpIdentity(resolved_from="anonymous")


class TestCreateToolContext:
    def test_extends_an_existing_resolved_server_context_with_tool_metadata(self) -> None:
        server = create_server_context(
            server=McpServerInfo(name="my-server"),
            identity=McpIdentity(user_id="u1", resolved_from="explicit"),
            transport="streamable-http",
        )
        ctx = create_tool_context(
            server,
            McpToolMeta(
                name="search_docs",
                owner="docs-team",
                # server-specific fields (e.g. Amplitude project id) ride the
                # free-form `meta` dict (Node's index signature)
                meta={"projectId": "67890"},
            ),
        )

        assert ctx.tool.name == "search_docs"
        assert ctx.tool.owner == "docs-team"
        assert ctx.tool.meta["projectId"] == "67890"
        # server-scope fields carry through unchanged
        assert ctx.server.name == "my-server"
        assert ctx.identity == McpIdentity(user_id="u1", resolved_from="explicit")
        assert ctx.transport == "streamable-http"

    def test_builds_both_scopes_at_once_from_inline_server_input(self) -> None:
        # Node passes an inline CreateServerContextInput; the Python analogue is
        # a hand-built partial base whose identity/anchor are still unset.
        base = McpServerContext(
            identity=None,  # type: ignore[arg-type]
            anchor=None,  # type: ignore[arg-type]
            transport="streamable-http",
            server=McpServerInfo(name="my-server"),
        )
        ctx = create_tool_context(
            base,
            McpToolMeta(name="search_docs"),
            request=McpRequestInfo(method="tools/call", size_bytes=128),
        )

        # inline input was resolved through the floor (identity/anchor defaulted)
        assert ctx.identity == McpIdentity(resolved_from="anonymous")
        assert ctx.anchor == McpAnchor(type="anonymous", value="")
        assert ctx.transport == "streamable-http"
        assert ctx.tool.name == "search_docs"
        assert ctx.request == McpRequestInfo(method="tools/call", size_bytes=128)

    def test_always_applies_the_floor_to_a_partial_inline_base_no_structural_skip(self) -> None:
        # base has identity but not anchor; normalization must still floor it
        base = McpServerContext(
            identity=McpIdentity(user_id="u1", resolved_from="explicit"),
            anchor=None,  # type: ignore[arg-type]
            transport="stdio",
            server=McpServerInfo(name="my-server"),
        )
        ctx = create_tool_context(base, McpToolMeta(name="search_docs"))

        assert ctx.identity == McpIdentity(user_id="u1", resolved_from="explicit")
        assert ctx.anchor == McpAnchor(type="anonymous", value="")

    def test_does_not_re_default_a_fully_resolved_base_context(self) -> None:
        server = create_server_context(
            server=McpServerInfo(name="my-server"),
            anchor=McpAnchor(type="process", value="4242"),
            transport="stdio",
        )
        ctx = create_tool_context(server, McpToolMeta(name="t"))

        # the resolved anchor is preserved, not overwritten with the floor
        assert ctx.anchor == McpAnchor(type="process", value="4242")

    def test_leaves_request_and_error_unset_when_no_opts_are_provided(self) -> None:
        ctx = create_tool_context(
            create_server_context(server=McpServerInfo(name="my-server"), transport="stdio"),
            McpToolMeta(name="search_docs"),
        )

        assert ctx.request is None
        assert ctx.error is None

    def test_attaches_a_structured_error_when_provided(self) -> None:
        error = McpToolError(code="internal", message="boom", type="thrown_exception")

        ctx = create_tool_context(
            create_server_context(server=McpServerInfo(name="my-server"), transport="stdio"),
            McpToolMeta(name="search_docs"),
            error=error,
        )

        assert ctx.error == error

    def test_produces_a_tool_context_assignable_to_its_server_scope_base(self) -> None:
        # McpToolContext extends McpServerContext — a tool ctx is usable
        # anywhere the shared server context is expected.
        ctx = create_tool_context(
            create_server_context(server=McpServerInfo(name="my-server"), transport="stdio"),
            McpToolMeta(name="search_docs"),
        )

        assert isinstance(ctx, McpServerContext)
        assert ctx.server.name == "my-server"
