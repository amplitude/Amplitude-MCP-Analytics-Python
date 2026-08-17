"""Ports test/tool-call-rejected-integration.test.ts (Node) — ``[MCP] Tool Call
Rejected`` against a REAL server over a real (in-memory) transport pair.

The Python SDK reports every pre-dispatch failure as an in-band ``isError``
result (the same contract as ``@modelcontextprotocol/sdk`` >= 1.21), so every
assertion here reads the result rather than expecting a raise.

Node's harness also carried a ``.disable()``d tool; the official Python SDK has
no tool disable, so ``disabled_tool`` is unreachable here (kept in the taxonomy
for cross-SDK schema parity) and that arm is not portable."""

from __future__ import annotations

from typing import Any

import pytest
from mcp.server.fastmcp import FastMCP
from mcp.shared.memory import create_connected_server_and_client_session
from mcp.types import Implementation

from amplitude_mcp_analytics import MCPAnalyticsConfig
from amplitude_mcp_analytics.testing import MockAmplitudeMCPAnalytics

pytestmark = pytest.mark.anyio

CLIENT = Implementation(name="test-client", version="1.0.0")

REJECTED = "[MCP] Tool Call Rejected"
RESPONSE = "[MCP] Tool Call Response"


class Harness:
    """An instrumented FastMCP carrying one tool of each shape that matters:
    instrumented, instrumented-and-failing-in-band, and registered but never
    wrapped (its callback runs unseen, so its failure reaches the hook
    unmarked — the false positive the classifier must dodge)."""

    def __init__(self, config: MCPAnalyticsConfig | None = None) -> None:
        self.analytics = MockAmplitudeMCPAnalytics(
            server_name="test-mcp", server_version="9.9.9", config=config
        )
        self.mcp = FastMCP("test-mcp")
        self.echo_ran = False
        analytics = self.analytics

        async def echo(text: str) -> str:
            self.echo_ran = True
            return "ok"

        async def fails() -> dict[str, Any]:
            # In-band failure: the wrapper sees the raw isError return before
            # FastMCP serializes it.
            return {"content": [{"type": "text", "text": "upstream 500"}], "isError": True}

        async def uninstrumented() -> str:
            raise ValueError("upstream 500")

        self.mcp.add_tool(analytics.instrument_tool(echo, name="echo"), name="echo")
        self.mcp.add_tool(analytics.instrument_tool(fails, name="fails"), name="fails")
        self.mcp.add_tool(uninstrumented, name="uninstrumented")
        analytics.instrument_server(self.mcp, user_id="user-1")

    def events(self, event_type: str) -> list[dict[str, Any]]:
        return self.analytics.get_events(event_type)

    async def call(self, name: str, args: dict[str, Any] | None = None) -> Any:
        async with create_connected_server_and_client_session(
            self.mcp._mcp_server, client_info=CLIENT
        ) as client:
            return await client.call_tool(name, args or {})


async def test_emits_for_an_unknown_tool_name() -> None:
    h = Harness()
    await h.call("made_up_tool")

    rejected = h.events(REJECTED)
    assert len(rejected) == 1
    assert rejected[0]["user_id"] == "user-1"
    props = rejected[0]["event_properties"]
    assert props["[MCP] Attempted Tool Name"] == "made_up_tool"
    assert props["[MCP] Rejection Reason"] == "unknown_tool"
    assert props["[MCP] Error Type"] == "protocol_error"
    assert props["[MCP] Is Error"] is True
    # The attempted name is unvalidated input and must stay off the reserved key.
    assert "[MCP] Tool Name" not in props
    assert h.events(RESPONSE) == []


async def test_emits_for_an_input_schema_validation_failure() -> None:
    h = Harness()
    result = await h.call("echo", {"text": 123})
    assert result.isError is True

    rejected = h.events(REJECTED)
    assert len(rejected) == 1
    props = rejected[0]["event_properties"]
    assert props["[MCP] Attempted Tool Name"] == "echo"
    assert props["[MCP] Rejection Reason"] == "schema_validation"
    assert props["[MCP] Error Type"] == "protocol_error"
    # The rejection happened BEFORE dispatch: the instrumented wrapper (and the
    # tool body) never ran, so there is no Tool Call Response to pair with.
    assert h.echo_ran is False
    assert h.events(RESPONSE) == []


async def test_carries_the_json_rpc_code() -> None:
    h = Harness()
    await h.call("made_up_tool")

    # Cross-SDK note: Node recovers -32602 from the McpError message prefix;
    # the Python SDK funnels rejections through FastMCP's ToolError with no
    # `MCP error <code>:` prefix, so the JSON-RPC internal-error fallback is
    # reported instead. Either way the code rides `[MCP] Error Code` as a
    # string.
    assert h.events(REJECTED)[0]["event_properties"]["[MCP] Error Code"] == "-32603"


async def test_rejection_reason_separates_causes_that_error_code_cannot() -> None:
    h = Harness()
    await h.call("made_up_tool")
    unknown = h.events(REJECTED)[0]["event_properties"]
    h.analytics.reset()
    await h.call("echo", {"text": 123})
    invalid = h.events(REJECTED)[0]["event_properties"]

    # Both causes share one error shape: same code, same type...
    assert unknown["[MCP] Error Code"] == invalid["[MCP] Error Code"]
    assert unknown["[MCP] Error Type"] == "protocol_error"
    assert invalid["[MCP] Error Type"] == "protocol_error"
    # ...so Rejection Reason is what actually separates them.
    assert unknown["[MCP] Rejection Reason"] == "unknown_tool"
    assert invalid["[MCP] Rejection Reason"] == "schema_validation"


async def test_keeps_rejection_reason_when_sanitizer_drops_the_message() -> None:
    # The whole point: dropping the message must not cost the distinction.
    h = Harness(MCPAnalyticsConfig(sanitize_error_message=lambda _m: None))
    await h.call("made_up_tool")

    props = h.events(REJECTED)[0]["event_properties"]
    assert "[MCP] Error Message" not in props
    assert props["[MCP] Rejection Reason"] == "unknown_tool"


async def test_routes_the_rejection_message_through_sanitize_error_message() -> None:
    # Validation errors quote the offending argument value, so this path can
    # carry caller-supplied data.
    h = Harness(MCPAnalyticsConfig(sanitize_error_message=lambda _m: "<redacted>"))
    await h.call("echo", {"text": 123})

    rejected = h.events(REJECTED)
    assert len(rejected) == 1
    props = rejected[0]["event_properties"]
    assert props["[MCP] Error Message"] == "<redacted>"
    # Classification survives sanitization — it is what remains segmentable.
    assert props["[MCP] Error Type"] == "protocol_error"
    assert props["[MCP] Rejection Reason"] == "schema_validation"


async def test_carries_duration_and_response_size() -> None:
    h = Harness()
    await h.call("made_up_tool")

    props = h.events(REJECTED)[0]["event_properties"]
    assert isinstance(props["[MCP] Response Duration"], int)
    assert isinstance(props["[MCP] Response Size"], int)
    assert props["[MCP] Response Size"] > 0


async def test_does_not_emit_for_a_successful_dispatched_call() -> None:
    h = Harness()
    result = await h.call("echo", {"text": "hi"})
    assert result.isError is False
    assert h.echo_ran is True

    assert h.events(REJECTED) == []
    assert len(h.events(RESPONSE)) == 1


async def test_instrumented_in_band_failure_is_owned_by_response() -> None:
    h = Harness()
    await h.call("fails")

    assert h.events(REJECTED) == []
    responses = h.events(RESPONSE)
    assert len(responses) == 1
    assert responses[0]["event_properties"]["[MCP] Is Error"] is True
    assert responses[0]["event_properties"]["[MCP] Error Type"] == "returned_error"


async def test_uninstrumented_tool_failure_is_not_misreported_as_a_rejection() -> None:
    # The uninstrumented callback runs unseen and its raise surfaces as the
    # SAME in-band isError shape as a real pre-dispatch rejection — unmarked.
    # Nothing is emitted: the wrapper never saw the call, and it is not a
    # protocol rejection either. Claiming it would inflate protocol_error.
    h = Harness()
    result = await h.call("uninstrumented")
    assert result.isError is True  # the tool really did fail, in-band

    assert h.events(REJECTED) == []
    assert h.events(RESPONSE) == []


async def test_respects_autocapture_tool_calls_false() -> None:
    h = Harness(MCPAnalyticsConfig(autocapture={"tool_calls": False}))
    await h.call("made_up_tool")

    assert h.events(REJECTED) == []
