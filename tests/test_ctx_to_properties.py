"""Port of the Node repo's test/tracking/ctx-to-properties.test.ts.

Reserved fields use snake_case keys internally (Node: camelCase); Node's
free-form tool fields (`tags`, `category`) ride the `McpToolMeta.meta` dict.
"""

from __future__ import annotations

from typing import Any

from amplitude_mcp_analytics.context.factory import create_server_context, create_tool_context
from amplitude_mcp_analytics.context.types import (
    McpAnchor,
    McpClientInfo,
    McpIdentity,
    McpRequestInfo,
    McpServerInfo,
    McpTenant,
    McpToolMeta,
)
from amplitude_mcp_analytics.tracking.ctx_to_properties import (
    ctx_to_amplitude_fields,
    ctx_to_amplitude_fields_for_tool,
    reserved_fields_to_properties,
    should_emit,
)


def assert_subset(actual: dict[str, Any], expected: dict[str, Any]) -> None:
    """Node's `toMatchObject` — every expected pair must appear in `actual`."""
    for key, value in expected.items():
        assert key in actual, f"missing key: {key}"
        assert actual[key] == value, f"{key}: {actual[key]!r} != {value!r}"


class TestCtxToAmplitudeFields:
    def test_floors_the_cross_cutting_fields_when_ctx_is_built_from_the_anonymous_floor(
        self,
    ) -> None:
        ctx = create_server_context(server=McpServerInfo(name="my-server"), transport="stdio")
        mapped = ctx_to_amplitude_fields(ctx)

        assert_subset(
            mapped.event_properties,
            {
                "session_id": "no-session",
                "client_name": "unknown",
                "user_agent": "unknown",
                "server_name": "my-server",
                "transport": "stdio",
                "anchor_type": "anonymous",
            },
        )
        assert mapped.user_id is None
        assert mapped.device_id is None
        assert mapped.groups is None

    def test_promotes_user_device_identity_onto_top_level_fields(self) -> None:
        ctx = create_server_context(
            server=McpServerInfo(name="my-server"),
            transport="streamable-http",
            identity=McpIdentity(user_id="u1", device_id="d9", resolved_from="explicit"),
        )
        mapped = ctx_to_amplitude_fields(ctx)

        assert mapped.user_id == "u1"
        assert mapped.device_id == "d9"

    def test_maps_tenant_to_groups(self) -> None:
        ctx = create_server_context(
            server=McpServerInfo(name="my-server"),
            transport="streamable-http",
            tenant=McpTenant(group_type="org id", group_value="36958"),
            identity=McpIdentity(user_id="u1", resolved_from="explicit"),
        )
        mapped = ctx_to_amplitude_fields(ctx)

        assert mapped.groups == {"org id": "36958"}

    def test_sets_session_id_only_when_anchor_type_is_session_id(self) -> None:
        sessionful = ctx_to_amplitude_fields(
            create_server_context(
                server=McpServerInfo(name="my-server"),
                transport="streamable-http",
                anchor=McpAnchor(type="session-id", value="sess-abc"),
                identity=McpIdentity(user_id="u1", resolved_from="explicit"),
            )
        ).event_properties
        assert sessionful["session_id"] == "sess-abc"
        assert sessionful["anchor_type"] == "session-id"

        traced = ctx_to_amplitude_fields(
            create_server_context(
                server=McpServerInfo(name="my-server"),
                transport="streamable-http",
                anchor=McpAnchor(type="trace", value="trace-xyz"),
                identity=McpIdentity(user_id="u1", resolved_from="explicit"),
            )
        ).event_properties
        assert traced["session_id"] == "no-session"
        assert traced["anchor_type"] == "trace"

    def test_sets_client_and_server_identity_fields_when_present(self) -> None:
        ctx = create_server_context(
            server=McpServerInfo(name="my-server", version="1.2.3", type="remote"),
            transport="streamable-http",
            protocol_version="2026-07-28",
            auth_type="OAuth",
            client=McpClientInfo(name="cursor", version="0.42", user_agent="cursor/0.42 (mac)"),
        )
        fields = ctx_to_amplitude_fields(ctx).event_properties

        assert_subset(
            fields,
            {
                "client_name": "cursor",
                "client_version": "0.42",
                "user_agent": "cursor/0.42 (mac)",
                "server_name": "my-server",
                "server_version": "1.2.3",
                "server_type": "remote",
                "protocol_version": "2026-07-28",
                "auth_type": "OAuth",
            },
        )

    def test_puts_ctx_extra_in_the_separate_extra_properties_layer_not_event_properties(
        self,
    ) -> None:
        ctx = create_server_context(
            server=McpServerInfo(name="my-server"),
            transport="streamable-http",
            extra={"org url": "amplitude", "user email": "a@b.com"},
        )
        mapped = ctx_to_amplitude_fields(ctx)

        assert mapped.extra_properties == {"org url": "amplitude", "user email": "a@b.com"}
        assert "org url" not in mapped.event_properties

    def test_omits_client_and_server_versions_when_unset(self) -> None:
        fields = ctx_to_amplitude_fields(
            create_server_context(
                server=McpServerInfo(name="my-server"),
                transport="stdio",
                identity=McpIdentity(user_id="u1", resolved_from="explicit"),
            )
        ).event_properties

        assert "client_version" not in fields
        assert "server_version" not in fields
        assert "protocol_version" not in fields
        assert "auth_type" not in fields


class TestCtxToAmplitudeFieldsForTool:
    def test_extends_server_fields_with_tool_name(self) -> None:
        ctx = create_tool_context(
            create_server_context(server=McpServerInfo(name="my-server"), transport="stdio"),
            McpToolMeta(name="search_docs"),
        )
        assert ctx_to_amplitude_fields_for_tool(ctx).event_properties["tool_name"] == "search_docs"

    def test_promotes_tool_owner_tags_category(self) -> None:
        ctx = create_tool_context(
            create_server_context(
                server=McpServerInfo(name="my-server"), transport="streamable-http"
            ),
            McpToolMeta(
                name="search_docs",
                owner="docs-team",
                meta={"tags": ["search", "rag"], "category": "retrieval"},
            ),
        )
        fields = ctx_to_amplitude_fields_for_tool(ctx).event_properties

        assert fields["tool_owner"] == "docs-team"
        assert fields["tool_tags"] == ["search", "rag"]
        assert fields["tool_category"] == "retrieval"

    def test_omits_tool_tags_when_empty(self) -> None:
        ctx = create_tool_context(
            create_server_context(server=McpServerInfo(name="my-server"), transport="stdio"),
            McpToolMeta(name="search_docs", meta={"tags": []}),
        )
        assert "tool_tags" not in ctx_to_amplitude_fields_for_tool(ctx).event_properties

    def test_resolves_the_tool_extra_bag_into_extra_properties_layered_over_server_extra(
        self,
    ) -> None:
        ctx = create_tool_context(
            create_server_context(
                server=McpServerInfo(name="my-server"),
                transport="streamable-http",
                extra={"org url": "server-level", "region": "us"},
            ),
            McpToolMeta(name="search_docs", extra={"org url": "tool-level", "team": "docs"}),
        )
        mapped = ctx_to_amplitude_fields_for_tool(ctx)

        assert mapped.extra_properties == {
            "org url": "tool-level",
            "region": "us",
            "team": "docs",
        }

    def test_inherits_server_scope_fields_unchanged(self) -> None:
        ctx = create_tool_context(
            create_server_context(
                server=McpServerInfo(name="my-server"),
                transport="streamable-http",
                tenant=McpTenant(group_type="org id", group_value="36958"),
                identity=McpIdentity(user_id="u1", resolved_from="explicit"),
                anchor=McpAnchor(type="session-id", value="sess-1"),
            ),
            McpToolMeta(name="search_docs"),
        )
        mapped = ctx_to_amplitude_fields_for_tool(ctx)

        assert mapped.user_id == "u1"
        assert mapped.groups == {"org id": "36958"}
        assert mapped.event_properties["session_id"] == "sess-1"

    def test_promotes_the_request_scope_rationale_and_response_http_status(self) -> None:
        ctx = create_tool_context(
            create_server_context(
                server=McpServerInfo(name="my-server"), transport="streamable-http"
            ),
            McpToolMeta(name="search_docs"),
            request=McpRequestInfo(
                method="tools/call",
                rationale="need ids first",
                response_http_status=400,
            ),
        )
        fields = ctx_to_amplitude_fields_for_tool(ctx).event_properties

        assert fields["rationale"] == "need ids first"
        assert fields["response_http_status"] == 400

        props = reserved_fields_to_properties(fields)
        assert props["[MCP] Rationale"] == "need ids first"
        assert props["[MCP] Response HTTP Status"] == 400

    def test_omits_rationale_and_response_http_status_when_unset(self) -> None:
        ctx = create_tool_context(
            create_server_context(server=McpServerInfo(name="my-server"), transport="stdio"),
            McpToolMeta(name="search_docs"),
        )
        fields = ctx_to_amplitude_fields_for_tool(ctx).event_properties

        assert "rationale" not in fields
        assert "response_http_status" not in fields


class TestReservedFieldsToProperties:
    def test_converts_snake_case_fields_to_wire_property_names(self) -> None:
        ctx = create_tool_context(
            create_server_context(
                server=McpServerInfo(name="my-server", version="1.0.0"),
                transport="streamable-http",
                tenant=McpTenant(group_type="org id", group_value="36958"),
                anchor=McpAnchor(type="session-id", value="sess-1"),
            ),
            McpToolMeta(name="search_docs", owner="docs-team"),
        )
        props = reserved_fields_to_properties(
            ctx_to_amplitude_fields_for_tool(ctx).event_properties
        )

        assert_subset(
            props,
            {
                "[MCP] Session ID": "sess-1",
                "[MCP] Server Name": "my-server",
                "[MCP] Server Version": "1.0.0",
                "[MCP] Transport": "streamable-http",
                "[MCP] Anchor Type": "session-id",
                "[MCP] Tool Name": "search_docs",
                "[MCP] Tool Owner": "docs-team",
            },
        )
        # No snake_case keys leak onto the wire payload.
        assert "session_id" not in props


class TestShouldEmit:
    def test_drops_events_when_identity_is_anonymous_and_no_tenant_is_set(self) -> None:
        ctx = create_server_context(server=McpServerInfo(name="my-server"), transport="stdio")
        assert should_emit(ctx) is False

    def test_emits_when_identity_is_resolved_even_without_tenant(self) -> None:
        ctx = create_server_context(
            server=McpServerInfo(name="my-server"),
            transport="stdio",
            identity=McpIdentity(user_id="u1", resolved_from="explicit"),
        )
        assert should_emit(ctx) is True

    def test_emits_when_tenant_is_set_even_with_anonymous_identity(self) -> None:
        ctx = create_server_context(
            server=McpServerInfo(name="my-server"),
            transport="stdio",
            tenant=McpTenant(group_type="org id", group_value="36958"),
        )
        assert should_emit(ctx) is True

    def test_emits_the_anonymous_tenant_less_floor_when_emit_anonymous_event_is_set(self) -> None:
        ctx = create_server_context(
            server=McpServerInfo(name="my-server"),
            transport="stdio",
            emit_anonymous_event=True,
        )
        assert should_emit(ctx) is True
