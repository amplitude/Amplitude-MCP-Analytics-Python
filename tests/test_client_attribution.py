"""Run the same sanitized client signal fixtures as the Node SDK."""

import json
from pathlib import Path

import pytest

from amplitude_mcp_analytics import (
    McpClientInfo,
    McpIdentity,
    McpServerInfo,
    McpToolMeta,
    create_server_context,
    create_tool_context,
)
from amplitude_mcp_analytics.testing import MockAmplitudeMCPAnalytics

_FIXTURES = json.loads(
    (Path(__file__).parent / "fixtures" / "client-attribution.json").read_text(encoding="utf-8")
)
_KEYS = {
    "[MCP] Client Name", "[MCP] Client Version", "[MCP] Client Name Source",
    "[MCP] Client Family", "[MCP] Client Surface", "[MCP] Client Family Source", "[MCP] User Agent",
}
_FIELD_NAMES = {"nameSource": "name_source", "userAgent": "user_agent"}


@pytest.mark.parametrize("fixture", _FIXTURES, ids=[row["case"] for row in _FIXTURES])
def test_client_attribution_on_custom_events(fixture: dict) -> None:
    sdk = MockAmplitudeMCPAnalytics(server_name="sdk-test", server_version="1")
    raw = {_FIELD_NAMES.get(key, key): value for key, value in fixture["client"].items()}
    client = McpClientInfo(**raw)
    ctx = create_server_context(
        server=McpServerInfo(name="sdk-test"),
        transport="streamable-http",
        identity=McpIdentity(user_id="test-user", resolved_from="explicit"),
        client=client,
    )

    sdk.track_server_event(ctx, "server-custom")
    sdk.track_tool_event(create_tool_context(ctx, McpToolMeta(name="echo")), "tool-custom")

    assert len(sdk.events) == 2
    for event in sdk.events:
        props = event["event_properties"]
        assert {key: props[key] for key in _KEYS & props.keys()} == fixture["expected"]
    assert vars(client) == vars(McpClientInfo(**raw))
