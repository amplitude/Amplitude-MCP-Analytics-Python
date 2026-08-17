"""Port of the Node repo's test/tracking/track-tool-event.test.ts. Node's
free-form tool fields (`tags`, `category`) ride the `McpToolMeta.meta` dict."""

from __future__ import annotations

from typing import Any

from amplitude_mcp_analytics.context.factory import create_server_context, create_tool_context
from amplitude_mcp_analytics.context.types import (
    McpAnchor,
    McpIdentity,
    McpServerInfo,
    McpTenant,
    McpToolMeta,
)
from amplitude_mcp_analytics.tracking.track import track_tool_event
from conftest import make_amplitude


class ThrowingAmplitude:
    def track(self, event: Any) -> None:
        raise RuntimeError("amplitude broke")

    def flush(self) -> None:
        return None


class TestTrackToolEvent:
    def test_emits_a_tool_scope_event_with_inherited_server_plus_tool_properties(self) -> None:
        client = make_amplitude()
        ctx = create_tool_context(
            create_server_context(
                server=McpServerInfo(name="my-server"),
                transport="streamable-http",
                identity=McpIdentity(user_id="u1", resolved_from="explicit"),
                tenant=McpTenant(group_type="org id", group_value="36958"),
                anchor=McpAnchor(type="session-id", value="sess-1"),
            ),
            McpToolMeta(
                name="search_docs",
                owner="docs-team",
                meta={"tags": ["search"], "category": "retrieval"},
            ),
        )

        track_tool_event(client, ctx, "mcp: tool result rendered")

        assert len(client.tracked) == 1
        event = client.tracked[0]
        assert event["event_type"] == "mcp: tool result rendered"
        assert event["user_id"] == "u1"
        assert event["groups"] == {"org id": "36958"}
        props = event["event_properties"]
        # server-scope inherited
        assert props["[MCP] Session ID"] == "sess-1"
        assert props["[MCP] Server Name"] == "my-server"
        # tool-scope added
        assert props["[MCP] Tool Name"] == "search_docs"
        assert props["[MCP] Tool Owner"] == "docs-team"
        assert props["[MCP] Tool Tags"] == ["search"]
        assert props["[MCP] Tool Category"] == "retrieval"

    def test_merges_caller_properties_which_win_over_reserved_on_collision(self) -> None:
        client = make_amplitude()
        ctx = create_tool_context(
            create_server_context(
                server=McpServerInfo(name="my-server"),
                transport="streamable-http",
                identity=McpIdentity(user_id="u1", resolved_from="explicit"),
            ),
            McpToolMeta(name="search_docs"),
        )

        track_tool_event(
            client,
            ctx,
            "mcp: tool query",
            {
                "query text": "how to do X",
                # Collides with the reserved '[MCP] Tool Name' → caller wins.
                "[MCP] Tool Name": "overridden_name",
            },
        )

        props = client.tracked[0]["event_properties"]
        assert props["query text"] == "how to do X"
        assert props["[MCP] Tool Name"] == "overridden_name"  # caller wins

    def test_emits_the_tools_extra_enrichment_and_lets_caller_properties_override_it(
        self,
    ) -> None:
        client = make_amplitude()
        ctx = create_tool_context(
            create_server_context(
                server=McpServerInfo(name="my-server"),
                transport="streamable-http",
                identity=McpIdentity(user_id="u1", resolved_from="explicit"),
            ),
            McpToolMeta(name="search_docs", extra={"org url": "from-extra", "region": "us"}),
        )

        track_tool_event(client, ctx, "mcp: tool query", {"org url": "from-caller"})

        props = client.tracked[0]["event_properties"]
        assert props["region"] == "us"  # extra, no collision
        assert props["org url"] == "from-caller"  # caller overrides extra

    def test_drops_emission_under_the_identity_tenant_skip_rule(self) -> None:
        # anonymous + no tenant
        client = make_amplitude()
        ctx = create_tool_context(
            create_server_context(server=McpServerInfo(name="my-server"), transport="stdio"),
            McpToolMeta(name="search_docs"),
        )
        track_tool_event(client, ctx, "mcp: never emitted")

        assert len(client.tracked) == 0

    def test_swallows_underlying_client_errors_best_effort(self) -> None:
        client = ThrowingAmplitude()
        ctx = create_tool_context(
            create_server_context(
                server=McpServerInfo(name="my-server"),
                transport="streamable-http",
                identity=McpIdentity(user_id="u1", resolved_from="explicit"),
            ),
            McpToolMeta(name="search_docs"),
        )

        track_tool_event(client, ctx, "mcp: failing event")  # must not raise
