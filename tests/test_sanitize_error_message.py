"""Port of the Node repo's test/sanitize-error-message.test.ts.

`config.sanitize_error_message` — the opt-out for `[MCP] Error Message`.

Covers the helper's contract directly, then the wiring: every default event
that carries the property must route through it, and no other error property
may be disturbed.

Port notes: Node's fake `McpServer` double becomes a duck-typed low-level
server (a `request_handlers` dict is what `unwrap_server` narrows on), and
Node's `server.connect(transport)` becomes one instrumented `run()` — the
per-connection seam in the Python SDK. The tools/list drive happens inside the
run frame (the scope ContextVar is only set there); instrumented tools called
after the run fall back to the last-connected scope, mirroring Node's flow of
connect-then-call.
"""

from __future__ import annotations

import re
from types import SimpleNamespace
from typing import Any

import pytest
from mcp.types import ListToolsRequest

from amplitude_mcp_analytics import AmplitudeMCPAnalytics, MCPAnalyticsConfig
from amplitude_mcp_analytics.tracking.sanitize_error_message import sanitize_error_message
from conftest import CapturingAmplitude, make_amplitude


class TestSanitizeErrorMessageHelper:
    def test_passes_the_message_through_when_no_sanitizer_is_configured(self) -> None:
        assert sanitize_error_message("boom", None) == "boom"

    def test_returns_the_sanitizers_replacement(self) -> None:
        assert (
            sanitize_error_message(
                "user@example.com failed",
                lambda m: re.sub(r"\S+@\S+", "<email>", m, count=1),
            )
            == "<email> failed"
        )

    def test_omits_the_property_when_the_sanitizer_returns_none(self) -> None:
        assert sanitize_error_message("boom", lambda m: None) is None

    def test_omits_the_property_when_the_sanitizer_returns_a_non_string(self) -> None:
        assert sanitize_error_message("boom", lambda m: 42) is None  # type: ignore[arg-type,return-value]
        assert sanitize_error_message("boom", lambda m: object()) is None  # type: ignore[arg-type,return-value]

    def test_fails_closed_when_the_sanitizer_raises_never_falls_back_to_raw(self) -> None:
        def thrower(m: str) -> str:
            raise RuntimeError("sanitizer bug")

        assert sanitize_error_message("user@example.com", thrower) is None

    def test_passes_the_raw_message_to_the_sanitizer_exactly_once(self) -> None:
        calls: list[str] = []

        def spy(m: str) -> str:
            calls.append(m)
            return "clean"

        sanitize_error_message("raw", spy)

        assert calls == ["raw"]


PII = 'No subscriber found for "jane@example.com"'


def make_analytics(
    config: MCPAnalyticsConfig | None = None,
) -> tuple[AmplitudeMCPAnalytics, CapturingAmplitude]:
    amplitude = make_amplitude()
    analytics = AmplitudeMCPAnalytics(
        amplitude=amplitude,
        server_name="test-mcp",
        server_version="9.9.9",
        config=config,
    )
    return analytics, amplitude


class FakeLowLevelServer:
    """Minimal server double — a `request_handlers` dict (what `unwrap_server`
    narrows on) plus a `tools/list` handler so the Tools Listed path can be
    driven for real rather than by hand-building a context. `run()` executes a
    test-supplied body inside the instrumented scope frame — the Python
    analogue of Node's fake `connect()`."""

    def __init__(self, tools_list_throws: BaseException | None = None) -> None:
        self._throws = tools_list_throws
        self._body: Any = None

        async def tools_list(req: Any) -> Any:
            if self._throws is not None:
                raise self._throws
            return SimpleNamespace(root=SimpleNamespace(tools=[SimpleNamespace(name="search")]))

        self.request_handlers: dict[Any, Any] = {ListToolsRequest: tools_list}

    def set_body(self, body: Any) -> None:
        self._body = body

    async def run(
        self, read_stream: Any = None, write_stream: Any = None, init_options: Any = None
    ) -> Any:
        if self._body is not None:
            return await self._body()

    async def list_tools(self) -> Any:
        # Drive the (wrapped) handler like a client request — req is non-None;
        # None marks the SDK's internal tool-cache refresh, which must not emit.
        handler = self.request_handlers[ListToolsRequest]
        return await handler(ListToolsRequest(method="tools/list"))


async def bind_server(
    analytics: AmplitudeMCPAnalytics, server: FakeLowLevelServer
) -> None:
    """`instrumentServer(server, { userId: 'user-1' })` + `connect()` in Node."""
    analytics.instrument_server(server, user_id="user-1")
    await server.run(None, None, None)


async def call_failing_tool(config: MCPAnalyticsConfig | None = None) -> dict[str, Any] | None:
    """Run one instrumented tool that fails in-band, returning the emitted event."""
    analytics, amplitude = make_analytics(config)
    await bind_server(analytics, FakeLowLevelServer())

    async def lookup() -> dict[str, Any]:
        return {"content": [{"type": "text", "text": PII}], "isError": True}

    tool = analytics.instrument_tool(lookup, name="lookup")
    await tool()

    events = amplitude.events_of("[MCP] Tool Call Response")
    return events[0] if events else None


class TestToolCallResponseSanitization:
    @pytest.mark.anyio
    async def test_emits_the_raw_result_text_by_default_documented_v0_behavior(self) -> None:
        event = await call_failing_tool()
        assert event is not None
        assert event["event_properties"]["[MCP] Error Message"] == PII

    @pytest.mark.anyio
    async def test_emits_the_sanitizers_rewrite_instead_of_the_result_text(self) -> None:
        event = await call_failing_tool(
            MCPAnalyticsConfig(
                sanitize_error_message=lambda m: re.sub(
                    r"[\w.+-]+@[\w-]+\.[\w.]+", "<email>", m
                ),
            )
        )
        assert event is not None
        assert event["event_properties"]["[MCP] Error Message"] == (
            'No subscriber found for "<email>"'
        )

    @pytest.mark.anyio
    async def test_omits_the_property_on_none_keeping_error_code_and_error_type(self) -> None:
        event = await call_failing_tool(MCPAnalyticsConfig(sanitize_error_message=lambda m: None))

        assert event is not None
        assert "[MCP] Error Message" not in event["event_properties"]
        # The segmentable classification is exactly what must survive.
        assert event["event_properties"]["[MCP] Error Type"] == "returned_error"
        assert event["event_properties"]["[MCP] Is Error"] is True

    @pytest.mark.anyio
    async def test_omits_the_property_when_the_sanitizer_raises_and_still_emits(self) -> None:
        def buggy(m: str) -> str:
            raise RuntimeError("sanitizer bug")

        event = await call_failing_tool(MCPAnalyticsConfig(sanitize_error_message=buggy))

        assert event is not None
        assert "[MCP] Error Message" not in event["event_properties"]
        assert event["event_properties"]["[MCP] Is Error"] is True

    @pytest.mark.anyio
    async def test_leaves_a_successful_call_untouched_the_sanitizer_never_runs(self) -> None:
        calls: list[str] = []

        def spy(m: str) -> str:
            calls.append(m)
            return m

        analytics, amplitude = make_analytics(MCPAnalyticsConfig(sanitize_error_message=spy))
        await bind_server(analytics, FakeLowLevelServer())

        async def ok() -> dict[str, Any]:
            return {"content": [{"type": "text", "text": "ok"}]}

        tool = analytics.instrument_tool(ok, name="ok")
        await tool()

        assert calls == []
        events = amplitude.events_of("[MCP] Tool Call Response")
        assert events and "[MCP] Error Message" not in events[0]["event_properties"]


class TestEveryEmittingPath:
    # Regression guard (from Node): the tool-call recording seam has multiple
    # call shapes (sync throw, sync return, async resolve, async reject) and
    # the sync-return one originally omitted the sanitizer, so a sync handler
    # leaked the raw message.
    @pytest.mark.anyio
    async def test_applies_on_a_synchronous_handler_returning_is_error(self) -> None:
        analytics, amplitude = make_analytics(
            MCPAnalyticsConfig(sanitize_error_message=lambda m: "<redacted>")
        )
        await bind_server(analytics, FakeLowLevelServer())

        # Not async — returns the result directly rather than a coroutine.
        def sync_lookup() -> dict[str, Any]:
            return {"content": [{"type": "text", "text": PII}], "isError": True}

        tool = analytics.instrument_tool(sync_lookup, name="sync-lookup")
        tool()

        events = amplitude.events_of("[MCP] Tool Call Response")
        assert events and events[0]["event_properties"]["[MCP] Error Message"] == "<redacted>"

    @pytest.mark.anyio
    async def test_applies_on_a_synchronous_handler_that_raises(self) -> None:
        analytics, amplitude = make_analytics(
            MCPAnalyticsConfig(sanitize_error_message=lambda m: "<redacted>")
        )
        await bind_server(analytics, FakeLowLevelServer())

        def sync_throw() -> dict[str, Any]:
            raise RuntimeError(PII)

        tool = analytics.instrument_tool(sync_throw, name="sync-throw")
        with pytest.raises(RuntimeError) as exc_info:
            tool()
        assert str(exc_info.value) == PII

        events = amplitude.events_of("[MCP] Tool Call Response")
        assert events and events[0]["event_properties"]["[MCP] Error Message"] == "<redacted>"

    # Wired in `emit_tools_listed` and documented as covered, but previously
    # asserted nowhere — only Response and Rejected were tested. Driven through
    # the real hook so the config → emitter plumbing is exercised too.
    @pytest.mark.anyio
    async def test_applies_to_tools_listed(self) -> None:
        analytics, amplitude = make_analytics(
            MCPAnalyticsConfig(sanitize_error_message=lambda m: "<redacted>")
        )
        server = FakeLowLevelServer(tools_list_throws=RuntimeError(PII))
        analytics.instrument_server(server, user_id="user-1")

        async def body() -> None:
            with pytest.raises(RuntimeError) as exc_info:
                await server.list_tools()
            assert str(exc_info.value) == PII

        server.set_body(body)
        await server.run(None, None, None)

        events = amplitude.events_of("[MCP] Tools Listed")
        assert events
        assert events[0]["event_properties"]["[MCP] Error Message"] == "<redacted>"
        # Classification is untouched by sanitization.
        assert events[0]["event_properties"]["[MCP] Error Type"] == "thrown_exception"

    @pytest.mark.anyio
    async def test_omits_the_message_on_tools_listed_when_the_sanitizer_returns_none(
        self,
    ) -> None:
        analytics, amplitude = make_analytics(
            MCPAnalyticsConfig(sanitize_error_message=lambda m: None)
        )
        server = FakeLowLevelServer(tools_list_throws=RuntimeError(PII))
        analytics.instrument_server(server, user_id="user-1")

        async def body() -> None:
            with pytest.raises(RuntimeError) as exc_info:
                await server.list_tools()
            assert str(exc_info.value) == PII

        server.set_body(body)
        await server.run(None, None, None)

        events = amplitude.events_of("[MCP] Tools Listed")
        assert events
        assert "[MCP] Error Message" not in events[0]["event_properties"]
        assert events[0]["event_properties"]["[MCP] Error Type"] == "thrown_exception"


class TestConfigSanitizeErrorMessage:
    def test_is_none_by_default(self) -> None:
        assert MCPAnalyticsConfig().sanitize_error_message is None

    def test_retains_a_supplied_function(self) -> None:
        def fn(m: str) -> str:
            return m

        assert MCPAnalyticsConfig(sanitize_error_message=fn).sanitize_error_message is fn

    def test_ignores_a_non_function_value_rather_than_crashing_at_emit_time(self) -> None:
        config = MCPAnalyticsConfig(sanitize_error_message="nope")  # type: ignore[arg-type]
        assert config.sanitize_error_message is None
