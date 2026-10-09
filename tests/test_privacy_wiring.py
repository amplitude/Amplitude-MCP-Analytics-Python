"""End-to-end wiring: a privacy config on the client redacts free-form event
content and leaves typed identity and dimension fields untouched.

Port of the Node repo's test/tracking/privacy-wiring.test.ts.
"""

from __future__ import annotations

from typing import Any

import pytest

from amplitude_mcp_analytics import MCPAnalyticsConfig
from amplitude_mcp_analytics.context.factory import create_server_context, create_tool_context
from amplitude_mcp_analytics.context.types import (
    McpAnchor,
    McpIdentity,
    McpRequestInfo,
    McpServerInfo,
    McpTenant,
    McpToolMeta,
)
from amplitude_mcp_analytics.core.privacy import REDACTED_IMAGE_PLACEHOLDER
from amplitude_mcp_analytics.testing import MockAmplitudeMCPAnalytics
from amplitude_mcp_analytics.types import AmplitudeEvent

pytestmark = pytest.mark.anyio

PNG_BASE64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8A"
    "AAAASUVORK5CYII="
)


def make_analytics(config: MCPAnalyticsConfig | None = None) -> MockAmplitudeMCPAnalytics:
    return MockAmplitudeMCPAnalytics(
        server_name="my-server",
        server_version="1.0.0",
        config=config,
    )


def resolved_ctx(extra: dict[str, Any] | None = None) -> Any:
    return create_server_context(
        server=McpServerInfo(name="my-server", version="1.0.0"),
        transport="streamable-http",
        identity=McpIdentity(user_id="admin@corp.com", resolved_from="explicit"),
        tenant=McpTenant(group_type="org id", group_value="36958"),
        anchor=McpAnchor(type="session-id", value="10.0.0.1"),
        extra=extra,
    )


class FakeLowLevelServer:
    def __init__(self) -> None:
        self.request_handlers: dict[Any, Any] = {}

    async def run(
        self, read_stream: Any = None, write_stream: Any = None, init_options: Any = None
    ) -> None:
        return None


class TestPrivacyWiring:
    def test_redacts_pii_in_caller_properties_and_ctx_extra_by_default(self) -> None:
        analytics = make_analytics()
        analytics.track_server_event(
            resolved_ctx({"user email": "e@x.com", "org url": "amplitude"}),
            "mcp: custom",
            {
                "note": "reach me at user@x.com",
                "count": 3,
                "profile": {"phone": "Call (555) 123-4567"},
            },
        )

        props = analytics.events[0]["event_properties"]
        assert props["note"] == "reach me at [email]"
        assert props["count"] == 3
        assert props["user email"] == "[email]"
        assert props["org url"] == "amplitude"
        assert props["profile"] == {"phone": "Call ([phone]"}

    def test_never_redacts_typed_identity_or_dimension_fields(self) -> None:
        analytics = make_analytics()
        ctx = create_tool_context(
            resolved_ctx(),
            McpToolMeta(name="user@example.com"),
            request=McpRequestInfo(rationale="email the user at user@x.com"),
        )
        analytics.track_tool_event(
            ctx,
            "mcp: identity",
            {
                "[MCP] Error Message": "failed for user@x.com",
                "[MCP] Error Type": "returned_error",
                "[MCP] Tool Name": "user@example.com",
                "[MCP] Param: note": "reach user@x.com",
            },
        )

        event = analytics.events[0]
        assert event["user_id"] == "admin@corp.com"
        assert event["groups"] == {"org id": "36958"}
        props = event["event_properties"]
        assert props["[MCP] Session ID"] == "10.0.0.1"
        assert props["[MCP] Server Name"] == "my-server"
        assert props["[MCP] Tool Name"] == "user@example.com"
        assert props["[MCP] Error Type"] == "returned_error"
        assert props["[MCP] Rationale"] == "email the user at [email]"
        assert props["[MCP] Error Message"] == "failed for [email]"
        assert props["[MCP] Param: note"] == "reach [email]"

    def test_honors_redact_pii_false_and_still_applies_custom_patterns_and_base64(self) -> None:
        analytics = make_analytics(
            MCPAnalyticsConfig(redact_pii=False, custom_redaction_patterns=[r"secret-\d+"])
        )
        analytics.track_server_event(
            resolved_ctx(),
            "mcp: raw",
            {"note": "reach me at user@x.com", "token": "secret-99", "image": PNG_BASE64},
        )

        props = analytics.events[0]["event_properties"]
        assert props["note"] == "reach me at user@x.com"
        assert props["token"] == "[REDACTED]"
        assert props["image"] == REDACTED_IMAGE_PLACEHOLDER

    def test_redacts_base64_image_content_when_builtin_pii_patterns_are_on(self) -> None:
        analytics = make_analytics()
        analytics.track_server_event(
            resolved_ctx(),
            "mcp: image",
            {"image": f"data:image/png;base64,{PNG_BASE64}"},
        )
        assert analytics.events[0]["event_properties"]["image"] == REDACTED_IMAGE_PLACEHOLDER


class TestSanitizeRunsBeforeRedaction:
    async def _call_failing_tool(self, config: MCPAnalyticsConfig) -> AmplitudeEvent:
        analytics = make_analytics(config)
        server = FakeLowLevelServer()
        analytics.instrument_server(server, user_id="user-1")
        await server.run(None, None, None)

        async def lookup() -> dict[str, Any]:
            return {
                "content": [{"type": "text", "text": 'No subscriber found for "jane@example.com"'}],
                "isError": True,
            }

        tool = analytics.instrument_tool(lookup, name="lookup")
        await tool()
        events = analytics.get_events("[MCP] Tool Call Response")
        assert events
        return events[0]

    async def test_gives_the_sanitizer_the_raw_message_then_redacts_whatever_it_returns(
        self,
    ) -> None:
        seen: list[str] = []

        def spy(message: str) -> str:
            seen.append(message)
            return message

        event = await self._call_failing_tool(MCPAnalyticsConfig(sanitize_error_message=spy))
        assert seen == ['No subscriber found for "jane@example.com"']
        assert event["event_properties"]["[MCP] Error Message"] == (
            'No subscriber found for "[email]"'
        )

    async def test_omits_the_property_when_the_sanitizer_returns_none_before_redaction(
        self,
    ) -> None:
        event = await self._call_failing_tool(
            MCPAnalyticsConfig(sanitize_error_message=lambda _message: None)
        )
        assert "[MCP] Error Message" not in event["event_properties"]
        assert event["event_properties"]["[MCP] Error Type"] == "returned_error"

    async def test_gives_sanitize_rationale_the_raw_text_then_redacts_whatever_it_returns(
        self,
    ) -> None:
        seen: list[str] = []

        def spy(rationale: str) -> str:
            seen.append(rationale)
            return rationale

        analytics = make_analytics(MCPAnalyticsConfig(sanitize_rationale=spy))
        server = FakeLowLevelServer()
        analytics.instrument_server(server, user_id="user-1")
        await server.run(None, None, None)

        async def handler() -> dict[str, Any]:
            analytics.set_rationale("email the user at user@x.com")
            return {"content": [{"type": "text", "text": "ok"}]}

        await analytics.instrument_tool(handler, name="search")()
        events = analytics.get_events("[MCP] Tool Call Response")
        assert seen == ["email the user at user@x.com"]
        assert events[0]["event_properties"]["[MCP] Rationale"] == "email the user at [email]"
