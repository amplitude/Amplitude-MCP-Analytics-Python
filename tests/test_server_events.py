"""Port of the Node repo's test/tracking/events/server-events.test.ts."""

from __future__ import annotations

from typing import Any

from amplitude_mcp_analytics.context.factory import create_server_context
from amplitude_mcp_analytics.context.types import (
    McpClientInfo,
    McpIdentity,
    McpServerContext,
    McpServerInfo,
)
from amplitude_mcp_analytics.tracking.events import (
    emit_session_ended,
    emit_session_initialized,
    emit_tools_listed,
)
from conftest import make_amplitude


def make_server_ctx(**overrides: Any) -> McpServerContext:
    """A server ctx with a real identity so the audit skip rule doesn't drop it."""
    defaults: dict[str, Any] = {
        "server": McpServerInfo(name="svc", version="1.0.0"),
        "transport": "stdio",
        "identity": McpIdentity(resolved_from="explicit", user_id="user-123"),
        "client": McpClientInfo(name="cursor", version="0.40"),
        "protocol_version": "2025-11-25",
        "auth_type": "oauth",
    }
    defaults.update(overrides)
    return create_server_context(**defaults)


class TestEmitSessionInitialized:
    def test_emits_the_event_with_ctx_derived_reserved_props(self) -> None:
        client = make_amplitude()
        emit_session_initialized(client, make_server_ctx())

        assert len(client.tracked) == 1
        assert client.tracked[0]["event_type"] == "[MCP] Session Initialized"
        props = client.tracked[0]["event_properties"]
        assert props["[MCP] Server Name"] == "svc"
        assert props["[MCP] Client Name"] == "cursor"
        assert props["[MCP] Transport"] == "stdio"
        assert props["[MCP] Protocol Version"] == "2025-11-25"
        assert props["[MCP] Auth Type"] == "oauth"
        assert client.tracked[0]["user_id"] == "user-123"

    def test_is_dropped_when_the_ctx_has_neither_identity_nor_tenant_skip_rule(self) -> None:
        client = make_amplitude()
        emit_session_initialized(
            client,
            create_server_context(server=McpServerInfo(name="svc"), transport="stdio"),
        )
        assert len(client.tracked) == 0


class TestEmitSessionEnded:
    def test_rounds_and_emits_session_duration_when_provided(self) -> None:
        client = make_amplitude()
        emit_session_ended(client, make_server_ctx(), duration_ms=1234.7)

        assert client.tracked[0]["event_type"] == "[MCP] Session Ended"
        assert client.tracked[0]["event_properties"]["[MCP] Session Duration"] == 1235

    def test_omits_session_duration_when_unknown(self) -> None:
        client = make_amplitude()
        emit_session_ended(client, make_server_ctx())
        assert "[MCP] Session Duration" not in client.tracked[0]["event_properties"]


class TestEmitToolsListed:
    def test_always_emits_is_error_plus_tool_count_plus_optionals_when_present(self) -> None:
        client = make_amplitude()
        emit_tools_listed(
            client,
            make_server_ctx(),
            is_error=False,
            tool_count=2,
            tool_names=["search", "create"],
            duration_ms=3.2,
            response_size_bytes=256,
        )

        assert client.tracked[0]["event_type"] == "[MCP] Tools Listed"
        props = client.tracked[0]["event_properties"]
        assert props["[MCP] Is Error"] is False
        assert props["[MCP] Tool Count"] == 2
        assert props["[MCP] Tool Names"] == ["search", "create"]
        assert props["[MCP] Response Duration"] == 3
        assert props["[MCP] Response Size"] == 256

    def test_emits_is_error_plus_classified_error_on_failure(self) -> None:
        client = make_amplitude()
        emit_tools_listed(
            client,
            make_server_ctx(),
            is_error=True,
            tool_count=0,
            error_message="boom",
            error_type="thrown_exception",
        )

        props = client.tracked[0]["event_properties"]
        assert props["[MCP] Is Error"] is True
        assert props["[MCP] Tool Count"] == 0
        assert props["[MCP] Error Message"] == "boom"
        assert props["[MCP] Error Type"] == "thrown_exception"

    def test_truncates_tool_names_past_the_cap_and_flags_it_keeping_the_true_count(self) -> None:
        client = make_amplitude()
        names = [f"t{i}" for i in range(101)]
        emit_tools_listed(
            client, make_server_ctx(), is_error=False, tool_count=len(names), tool_names=names
        )

        props = client.tracked[0]["event_properties"]
        assert props["[MCP] Tool Count"] == 101  # true total preserved
        assert len(props["[MCP] Tool Names"]) == 100  # clipped to the cap
        assert props["[MCP] Tool Names Truncated"] is True

    def test_does_not_set_the_truncated_flag_when_the_list_fits(self) -> None:
        client = make_amplitude()
        emit_tools_listed(
            client, make_server_ctx(), is_error=False, tool_count=2, tool_names=["a", "b"]
        )
        assert "[MCP] Tool Names Truncated" not in client.tracked[0]["event_properties"]
