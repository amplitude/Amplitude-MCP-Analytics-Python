"""`config.sanitize_rationale` — the redaction hook for `[MCP] Rationale`.

Python-only (the Node SDK has no equivalent yet). Same fail-closed contract as
`sanitize_error_message`, and the same test shape: the shared helper's contract
first, then the wiring — the hook is stamped onto the ctx by `instrument_server`
and applied in the tool-scope lowering, so it must hold on the default
`[MCP] Tool Call Response` event AND on tool-scope custom events, which are the
two things that lower a rationale.
"""

from __future__ import annotations

from typing import Any

import pytest

from amplitude_mcp_analytics import MCPAnalyticsConfig
from amplitude_mcp_analytics.context.types import McpToolContext
from amplitude_mcp_analytics.context.vars import get_current_context
from amplitude_mcp_analytics.testing import MockAmplitudeMCPAnalytics
from amplitude_mcp_analytics.tracking.sanitize_error_message import apply_sanitizer
from amplitude_mcp_analytics.types import AmplitudeEvent

pytestmark = pytest.mark.anyio

OK = {"content": [{"type": "text", "text": "ok"}]}
# A rationale that quotes the end user — exactly what the hook exists to scrub.
PII = 'user asked to look up jane@example.com'

RESPONSE = "[MCP] Tool Call Response"
CUSTOM = "[MCP] Query Tool Call"


class FakeLowLevelServer:
    """Minimal duck-typed low-level server (a `request_handlers` dict is what
    `unwrap_server` narrows on) whose `run()` is the instrumented scope frame —
    the same double `test_sanitize_error_message.py` uses."""

    def __init__(self) -> None:
        self.request_handlers: dict[Any, Any] = {}

    async def run(
        self, read_stream: Any = None, write_stream: Any = None, init_options: Any = None
    ) -> None:
        return None


async def bound_mock(config: MCPAnalyticsConfig | None = None) -> MockAmplitudeMCPAnalytics:
    """A mock client bound through the real `instrument_server` + one `run()` —
    the scope (and with it the stamped `sanitize_rationale`) is created per run,
    not at bind time."""
    mock = MockAmplitudeMCPAnalytics(
        server_name="test-mcp", server_version="9.9.9", config=config
    )
    server = FakeLowLevelServer()
    mock.instrument_server(server, user_id="user-12345")
    await server.run(None, None, None)
    return mock


async def call_tool_with_rationale(
    config: MCPAnalyticsConfig | None = None,
    *,
    rationale: str = PII,
    emit_custom: bool = False,
) -> list[AmplitudeEvent]:
    """Run one instrumented tool that sets a rationale, through the real
    `instrument_server` → ctx-stamping → lowering path."""
    mock = await bound_mock(config)

    async def handler(**_kwargs: Any) -> dict[str, Any]:
        mock.set_rationale(rationale)
        if emit_custom:
            ctx = get_current_context()
            assert isinstance(ctx, McpToolContext)
            mock.track_tool_event(ctx, CUSTOM, {"status": "pass"})
        return OK

    wrapped = mock.instrument_tool(handler, name="search")
    await wrapped(a=1)
    return mock.events


def props_of(events: list[AmplitudeEvent], event_type: str) -> dict[str, Any]:
    match = [e for e in events if e.get("event_type") == event_type]
    assert match, f"no {event_type} event was emitted"
    return match[0]["event_properties"]


class TestApplySanitizerHelper:
    """The shared fail-closed helper both sanitizers bind to."""

    def test_passes_the_value_through_when_no_sanitizer_is_configured(self) -> None:
        assert apply_sanitizer("why I called it", None) == "why I called it"

    def test_returns_the_sanitizers_replacement(self) -> None:
        assert apply_sanitizer("why", lambda v: f"<{v}>") == "<why>"

    def test_omits_the_value_when_the_sanitizer_returns_none(self) -> None:
        assert apply_sanitizer("why", lambda v: None) is None

    def test_omits_the_value_when_the_sanitizer_returns_a_non_string(self) -> None:
        assert apply_sanitizer("why", lambda v: 42) is None  # type: ignore[arg-type,return-value]

    def test_keeps_an_empty_string_replacement(self) -> None:
        # `""` is a string the sanitizer chose to return — the contract omits
        # the property only on None / non-string / raise.
        assert apply_sanitizer("why", lambda v: "") == ""

    def test_fails_closed_when_the_sanitizer_raises_never_falls_back_to_raw(self) -> None:
        def thrower(v: str) -> str:
            raise RuntimeError("sanitizer bug")

        assert apply_sanitizer(PII, thrower) is None


class TestToolCallResponseRationaleSanitization:
    async def test_emits_the_raw_rationale_by_default_no_wire_change(self) -> None:
        # The default config must not alter a single byte of the v0 wire shape.
        props = props_of(await call_tool_with_rationale(), RESPONSE)
        assert props["[MCP] Rationale"] == PII

    async def test_emits_the_sanitizers_rewrite_instead_of_the_raw_rationale(self) -> None:
        props = props_of(
            await call_tool_with_rationale(
                MCPAnalyticsConfig(sanitize_rationale=lambda _v: "<redacted>")
            ),
            RESPONSE,
        )
        assert props["[MCP] Rationale"] == "<redacted>"

    async def test_omits_the_property_on_none_keeping_the_rest_of_the_event(self) -> None:
        props = props_of(
            await call_tool_with_rationale(
                MCPAnalyticsConfig(sanitize_rationale=lambda _v: None)
            ),
            RESPONSE,
        )
        assert "[MCP] Rationale" not in props
        # Everything else about the call still lands.
        assert props["[MCP] Tool Name"] == "search"
        assert props["[MCP] Is Error"] is False

    async def test_omits_the_property_when_the_sanitizer_raises_and_still_emits(self) -> None:
        def buggy(_v: str) -> str:
            raise RuntimeError("sanitizer bug")

        props = props_of(
            await call_tool_with_rationale(MCPAnalyticsConfig(sanitize_rationale=buggy)),
            RESPONSE,
        )
        assert "[MCP] Rationale" not in props
        assert props["[MCP] Is Error"] is False

    async def test_omits_the_property_when_the_sanitizer_returns_a_non_string(self) -> None:
        props = props_of(
            await call_tool_with_rationale(
                MCPAnalyticsConfig(sanitize_rationale=lambda _v: 42)  # type: ignore[arg-type,return-value]
            ),
            RESPONSE,
        )
        assert "[MCP] Rationale" not in props

    async def test_emits_an_empty_string_replacement_instead_of_dropping_it(self) -> None:
        # An emptied-out rationale is a *rewrite*, not a decline: the sanitizer
        # returned a string, so the property is emitted (as `""`) — the same
        # thing `[MCP] Error Message` does with an empty replacement. Dropping
        # it here would make "the sanitizer scrubbed everything" and "there was
        # no rationale" indistinguishable downstream.
        props = props_of(
            await call_tool_with_rationale(MCPAnalyticsConfig(sanitize_rationale=lambda _v: "")),
            RESPONSE,
        )
        assert props["[MCP] Rationale"] == ""

    async def test_never_runs_when_no_rationale_was_set(self) -> None:
        calls: list[str] = []

        def spy(v: str) -> str:
            calls.append(v)
            return v

        mock = await bound_mock(MCPAnalyticsConfig(sanitize_rationale=spy))

        async def handler(**_kwargs: Any) -> dict[str, Any]:
            return OK

        await mock.instrument_tool(handler, name="search")(a=1)

        assert calls == []
        assert "[MCP] Rationale" not in props_of(mock.events, RESPONSE)

    async def test_receives_the_raw_rationale_exactly_once(self) -> None:
        seen: list[str] = []

        def spy(v: str) -> str:
            seen.append(v)
            return "clean"

        await call_tool_with_rationale(MCPAnalyticsConfig(sanitize_rationale=spy))

        assert seen == [PII]


class TestCustomToolEventRationaleSanitization:
    """Custom tool-scope events inherit the rationale, so they must inherit the
    hook too — a sanitizer that only covered the default event would leak."""

    async def test_applies_to_a_custom_tool_event(self) -> None:
        events = await call_tool_with_rationale(
            MCPAnalyticsConfig(sanitize_rationale=lambda _v: "<redacted>"),
            emit_custom=True,
        )
        assert props_of(events, CUSTOM)["[MCP] Rationale"] == "<redacted>"
        assert props_of(events, RESPONSE)["[MCP] Rationale"] == "<redacted>"

    async def test_drops_it_from_a_custom_tool_event_on_none(self) -> None:
        events = await call_tool_with_rationale(
            MCPAnalyticsConfig(sanitize_rationale=lambda _v: None), emit_custom=True
        )
        custom = props_of(events, CUSTOM)
        assert "[MCP] Rationale" not in custom
        # The caller's own property is untouched.
        assert custom["status"] == "pass"

    async def test_passes_through_on_a_custom_tool_event_by_default(self) -> None:
        events = await call_tool_with_rationale(emit_custom=True)
        assert props_of(events, CUSTOM)["[MCP] Rationale"] == PII

    async def test_keeps_an_empty_string_replacement_on_a_custom_tool_event(self) -> None:
        events = await call_tool_with_rationale(
            MCPAnalyticsConfig(sanitize_rationale=lambda _v: ""), emit_custom=True
        )
        assert props_of(events, CUSTOM)["[MCP] Rationale"] == ""


class TestConfigSanitizeRationale:
    def test_is_none_by_default(self) -> None:
        assert MCPAnalyticsConfig().sanitize_rationale is None

    def test_retains_a_supplied_function(self) -> None:
        def fn(v: str) -> str:
            return v

        assert MCPAnalyticsConfig(sanitize_rationale=fn).sanitize_rationale is fn

    def test_ignores_a_non_function_value_rather_than_crashing_at_emit_time(self) -> None:
        config = MCPAnalyticsConfig(sanitize_rationale="nope")  # type: ignore[arg-type]
        assert config.sanitize_rationale is None

    def test_is_independent_of_sanitize_error_message(self) -> None:
        config = MCPAnalyticsConfig(sanitize_error_message=lambda m: "<e>")
        assert config.sanitize_rationale is None
