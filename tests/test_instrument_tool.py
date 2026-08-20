"""Ports test/tracking/instrument-tool.test.ts (Node) — the tool wrapper.

Adapted to the Python seam: Node resolved per-request facts from the handler's
``extra`` argument; Python resolves them from the SDK's ambient request frame,
so direct invocation here binds the scope via ``mock._server_ctx = <ctx>``
(mirroring Node's cast to ``_serverCtx``) and the request-frame-derived paths
(session id / traceparent / headers from ``extra``) live in
``test_build_context.py`` instead. A Python-only concern is added on top: the
wrapper must preserve the handler's sync/async nature, because FastMCP builds
its dispatch around ``inspect.iscoroutinefunction``."""

from __future__ import annotations

import inspect
import json
import logging
from typing import Any

import anyio
import pytest
from mcp.types import CallToolResult, TextContent

from amplitude_mcp_analytics import (
    AmplitudeMCPAnalytics,
    MCPAnalyticsConfig,
    McpToolMeta,
    SetIdentityInput,
    get_current_context,
    set_identity,
)
from amplitude_mcp_analytics.testing import MockAmplitudeMCPAnalytics
from conftest import server_ctx, tenant

RESPONSE = "[MCP] Tool Call Response"

OK = {"content": [{"type": "text", "text": "ok"}]}


def make_mock(config: MCPAnalyticsConfig | None = None) -> MockAmplitudeMCPAnalytics:
    return MockAmplitudeMCPAnalytics(
        server_name="my-server", server_version="1.0.0", config=config
    )


def bind(mock: MockAmplitudeMCPAnalytics) -> None:
    """Bind the last-connected fallback scope the way Node's tests cast onto
    `_serverCtx`. Identity floors to anonymous inside `build_tool_context`; the
    tenant keeps the events above the anonymous-floor skip rule (an event with
    neither identity nor tenant is silently dropped — a vacuous pass)."""
    mock._server_ctx = server_ctx(
        transport="streamable-http", identity=None, tenant=tenant()
    )


@pytest.mark.anyio
async def test_wraps_the_native_handler_unchanged_and_exposes_ctx() -> None:
    mock = make_mock()
    received: Any = None
    seen_ctx: Any = None

    async def handler(q: str) -> dict[str, Any]:
        nonlocal received, seen_ctx
        received = q  # native args pass through untouched
        seen_ctx = get_current_context()
        return OK

    wrapped = mock.instrument_tool(
        handler, McpToolMeta(name="search_docs", owner="docs-team")
    )
    bind(mock)

    result = await wrapped(q="hi")

    assert result == OK
    assert received == "hi"
    assert seen_ctx is not None
    assert seen_ctx.tool.name == "search_docs"
    assert seen_ctx.tool.owner == "docs-team"
    assert seen_ctx.tenant == tenant()
    assert seen_ctx.transport == "streamable-http"  # inherited from server scope


@pytest.mark.anyio
async def test_emits_the_response_event_with_outcome_and_duration_on_success() -> None:
    mock = make_mock()

    async def handler(q: str) -> dict[str, Any]:
        return OK

    wrapped = mock.instrument_tool(handler, name="search_docs")
    bind(mock)

    await wrapped(q="hi")

    events = mock.get_events(RESPONSE)
    assert len(events) == 1
    props = events[0]["event_properties"]
    assert props["[MCP] Is Error"] is False
    assert props["[MCP] Tool Name"] == "search_docs"
    duration = props["[MCP] Response Duration"]
    assert isinstance(duration, int) and duration >= 0
    # No error keys on a successful call.
    assert "[MCP] Error Message" not in props
    assert "[MCP] Error Type" not in props


def test_sync_handler_stays_sync_and_async_stays_coroutine_function() -> None:
    # FastMCP dispatches through iscoroutinefunction, so the wrapper flipping
    # a handler's nature would silently change execution semantics.
    mock = make_mock()

    def sync_handler(q: str) -> dict[str, Any]:
        return OK

    async def async_handler(q: str) -> dict[str, Any]:
        return OK

    wrapped_sync = mock.instrument_tool(sync_handler, name="sync_tool")
    wrapped_async = mock.instrument_tool(async_handler, name="async_tool")

    assert not inspect.iscoroutinefunction(wrapped_sync)
    assert inspect.iscoroutinefunction(wrapped_async)

    bind(mock)
    # A sync wrapper returns the result directly, never a coroutine/promise.
    result = wrapped_sync(q="hi")
    assert result == OK
    assert len(mock.get_events(RESPONSE)) == 1


@pytest.mark.anyio
async def test_emits_request_and_response_sizes_when_computable() -> None:
    mock = make_mock()

    async def with_args(q: str) -> dict[str, Any]:
        return OK

    async def no_args() -> dict[str, Any]:
        return OK

    wrapped_with = mock.instrument_tool(with_args, name="with_args")
    wrapped_without = mock.instrument_tool(no_args, name="no_args")
    bind(mock)

    await wrapped_with(q="hello")
    await wrapped_without()

    with_args_event, no_args_event = mock.get_events(RESPONSE)
    props = with_args_event["event_properties"]
    assert isinstance(props["[MCP] Request Size"], int) and props["[MCP] Request Size"] > 0
    assert isinstance(props["[MCP] Response Size"], int) and props["[MCP] Response Size"] > 0
    # No arguments → there is no request body to measure; nothing is fabricated.
    assert "[MCP] Request Size" not in no_args_event["event_properties"]
    assert isinstance(no_args_event["event_properties"]["[MCP] Response Size"], int)


@pytest.mark.anyio
async def test_measures_sizes_for_pydantic_results_and_arguments() -> None:
    # Neither side is guaranteed to be plain data: a low-level handler may
    # return a pydantic `CallToolResult` (the shape `is_error_result` already
    # understands) and a validated pydantic model may arrive as an argument.
    # `json.dumps` refuses both, so measuring with it silently drops the size
    # properties the documented contract promises via `model_dump_json`.
    mock = make_mock()
    result = CallToolResult(content=[TextContent(type="text", text="hello world")])
    request_model = TextContent(type="text", text="a query from the model")

    async def handler(payload: TextContent) -> CallToolResult:
        return result

    wrapped = mock.instrument_tool(handler, name="pydantic_tool")
    bind(mock)

    await wrapped(payload=request_model)

    props = mock.get_events(RESPONSE)[0]["event_properties"]
    assert props["[MCP] Response Size"] == len(
        result.model_dump_json(by_alias=True, exclude_none=True).encode("utf-8")
    )
    assert isinstance(props["[MCP] Request Size"], int)
    assert props["[MCP] Request Size"] > 0


@pytest.mark.anyio
async def test_an_unserializable_argument_still_omits_the_size() -> None:
    # Teaching the serializer about pydantic must not turn "not measurable"
    # into a fabricated number: a FastMCP `Context`-shaped argument has no
    # JSON form, so the property stays absent and the event still emits.
    mock = make_mock()

    class NotSerializable:
        pass

    async def handler(payload: Any) -> dict[str, Any]:
        return OK

    wrapped = mock.instrument_tool(handler, name="opaque_tool")
    bind(mock)

    await wrapped(payload=NotSerializable())

    props = mock.get_events(RESPONSE)[0]["event_properties"]
    assert "[MCP] Request Size" not in props
    assert props["[MCP] Is Error"] is False


@pytest.mark.anyio
async def test_plain_dict_sizes_are_unchanged() -> None:
    # Guard the common path against the pydantic-aware measurement: a plain
    # dict result must still measure as its compact JSON, byte for byte.
    mock = make_mock()

    async def handler(q: str) -> dict[str, Any]:
        return OK

    wrapped = mock.instrument_tool(handler, name="plain_tool")
    bind(mock)

    await wrapped(q="hello")

    props = mock.get_events(RESPONSE)[0]["event_properties"]
    assert props["[MCP] Response Size"] == len(
        json.dumps(OK, separators=(",", ":")).encode("utf-8")
    )
    assert props["[MCP] Request Size"] == len(
        json.dumps({"q": "hello"}, separators=(",", ":")).encode("utf-8")
    )


@pytest.mark.anyio
async def test_async_is_error_result_is_a_failure() -> None:
    # The MCP in-band error channel: the handler RETURNS (does not throw) but
    # signals failure with isError: true.
    mock = make_mock()
    seen_ctx: Any = None

    async def handler() -> dict[str, Any]:
        nonlocal seen_ctx
        seen_ctx = get_current_context()
        return {"content": [{"type": "text", "text": "chart not found"}], "isError": True}

    wrapped = mock.instrument_tool(handler, name="get_chart")
    bind(mock)

    result = await wrapped()

    # The result still flows back to the SDK untouched.
    assert result["isError"] is True
    events = mock.get_events(RESPONSE)
    assert len(events) == 1
    props = events[0]["event_properties"]
    assert props["[MCP] Is Error"] is True
    assert props["[MCP] Tool Name"] == "get_chart"
    assert props["[MCP] Error Message"] == "chart not found"
    assert props["[MCP] Error Type"] == "returned_error"
    assert seen_ctx.error is not None and seen_ctx.error.type == "returned_error"


def test_sync_is_error_result_is_a_failure_and_sanitizer_applies() -> None:
    # The SYNC arm guards the sanitizer-bypass regression: both wrapper shapes
    # must funnel through the same record path, so the sanitizer applies to a
    # synchronously returned in-band error exactly as it does to an async one.
    mock = make_mock(MCPAnalyticsConfig(sanitize_error_message=lambda _m: "<redacted>"))

    def handler() -> dict[str, Any]:
        return {"content": [{"type": "text", "text": "bad input"}], "isError": True}

    wrapped = mock.instrument_tool(handler, name="sync_returns_error")
    bind(mock)

    result = wrapped()
    assert result["isError"] is True

    events = mock.get_events(RESPONSE)
    assert len(events) == 1
    props = events[0]["event_properties"]
    assert props["[MCP] Is Error"] is True
    assert props["[MCP] Error Message"] == "<redacted>"  # raw 'bad input' never emitted
    assert props["[MCP] Error Type"] == "returned_error"


@pytest.mark.anyio
async def test_async_throw_is_classified_emitted_and_reraised() -> None:
    mock = make_mock()
    seen_ctx: Any = None

    async def handler() -> dict[str, Any]:
        nonlocal seen_ctx
        seen_ctx = get_current_context()
        raise ValueError("kaboom")

    wrapped = mock.instrument_tool(handler, name="boom")
    bind(mock)

    with pytest.raises(ValueError, match="kaboom"):
        await wrapped()

    events = mock.get_events(RESPONSE)
    assert len(events) == 1
    props = events[0]["event_properties"]
    assert props["[MCP] Is Error"] is True
    assert props["[MCP] Tool Name"] == "boom"
    assert props["[MCP] Error Message"] == "kaboom"
    # The event carries the *classified* type, same shape as a returned error.
    assert props["[MCP] Error Type"] == "thrown_exception"
    assert seen_ctx.error is not None and seen_ctx.error.type == "thrown_exception"


def test_sync_throw_is_classified_emitted_and_reraised() -> None:
    mock = make_mock()
    seen_ctx: Any = None

    def handler() -> dict[str, Any]:
        nonlocal seen_ctx
        seen_ctx = get_current_context()
        raise ValueError("sync boom")

    wrapped = mock.instrument_tool(handler, name="sync_throw")
    bind(mock)

    with pytest.raises(ValueError, match="sync boom"):
        wrapped()

    events = mock.get_events(RESPONSE)
    assert len(events) == 1
    assert events[0]["event_properties"]["[MCP] Is Error"] is True
    assert seen_ctx.error is not None and seen_ctx.error.type == "thrown_exception"


@pytest.mark.anyio
async def test_anonymous_floor_and_no_session_sentinel() -> None:
    # Stateless HTTP shape: no session id, no trace context → the per-request
    # anonymous floor. The event still emits (the bound tenant keeps it above
    # the skip rule) and the session-id property falls back to its sentinel.
    mock = make_mock()
    seen_ctx: Any = None

    async def handler() -> dict[str, Any]:
        nonlocal seen_ctx
        seen_ctx = get_current_context()
        return OK

    wrapped = mock.instrument_tool(handler, name="search_docs")
    bind(mock)

    await wrapped()

    assert seen_ctx.anchor.type == "anonymous"
    events = mock.get_events(RESPONSE)
    assert len(events) == 1
    props = events[0]["event_properties"]
    assert props["[MCP] Session ID"] == "no-session"
    assert props["[MCP] Is Error"] is False
    assert events[0]["groups"] == {"org id": "org-123"}


@pytest.mark.anyio
async def test_fresh_ctx_per_invocation_under_concurrent_calls() -> None:
    # Both calls held in flight together; each must see ITS OWN ctx, and an
    # identity set inside one call must never leak into the other's event.
    mock = make_mock()
    seen: list[Any] = []
    started = 0
    gate = anyio.Event()

    async def handler(uid: str) -> dict[str, Any]:
        nonlocal started
        seen.append(get_current_context())
        set_identity(SetIdentityInput(user_id=uid))
        started += 1
        if started == 2:
            gate.set()
        await gate.wait()  # force ordering — both in flight before either resolves
        return OK

    wrapped = mock.instrument_tool(handler, name="search_docs")
    bind(mock)

    async with anyio.create_task_group() as tg:
        tg.start_soon(lambda: wrapped(uid="user-alpha-1"))
        tg.start_soon(lambda: wrapped(uid="user-beta-22"))

    assert len(seen) == 2
    assert seen[0] is not seen[1]  # no state bleed across concurrent calls
    assert seen[0].anchor.value != seen[1].anchor.value  # fresh floor per request

    events = mock.get_events(RESPONSE)
    assert {e["user_id"] for e in events} == {"user-alpha-1", "user-beta-22"}


@pytest.mark.anyio
async def test_meta_extra_merges_into_the_default_event_at_emit_time() -> None:
    mock = make_mock()

    async def handler() -> dict[str, Any]:
        return OK

    meta = McpToolMeta(name="search_docs", extra={"feature flag": "new-ranker"})
    wrapped = mock.instrument_tool(handler, meta)
    bind(mock)

    await wrapped()
    first = mock.get_events(RESPONSE)[0]["event_properties"]
    assert first["feature flag"] == "new-ranker"
    assert first["[MCP] Is Error"] is False

    # extra is read at EMIT time, not captured at wrap time: a mutation between
    # calls shows up on later events.
    assert meta.extra is not None
    meta.extra["cohort"] = "b"
    await wrapped()
    second = mock.get_events(RESPONSE)[1]["event_properties"]
    assert second["feature flag"] == "new-ranker"
    assert second["cohort"] == "b"
    assert "cohort" not in first  # the earlier event was not rewritten


@pytest.mark.anyio
async def test_handler_can_enrich_ctx_tool_extra_mid_call() -> None:
    mock = make_mock()

    async def handler(q: str) -> dict[str, Any]:
        # Dynamic enrichment: mutate the tool-scope extra bag mid-call. Values
        # pass through verbatim — output encoding is the renderer's job and
        # value sanitization belongs at the serialize-and-send boundary.
        ctx = get_current_context()
        assert ctx is not None
        ctx.tool.extra = {**(ctx.tool.extra or {}), "echoed query": q}
        return OK

    wrapped = mock.instrument_tool(handler, name="search_docs")
    bind(mock)

    await wrapped(q="<script>alert(1)</script>")

    props = mock.get_events(RESPONSE)[0]["event_properties"]
    assert props["echoed query"] == "<script>alert(1)</script>"


@pytest.mark.anyio
async def test_reserved_contract_keys_win_over_colliding_meta_extra() -> None:
    # Parity guard: the canonical outcome value wins; the collision is discarded.
    mock = make_mock()

    async def handler() -> dict[str, Any]:
        return OK

    wrapped = mock.instrument_tool(
        handler,
        McpToolMeta(name="search_docs", extra={"[MCP] Is Error": "definitely not a boolean"}),
    )
    bind(mock)

    await wrapped()

    props = mock.get_events(RESPONSE)[0]["event_properties"]
    assert props["[MCP] Is Error"] is False


@pytest.mark.anyio
async def test_track_tool_calls_false_still_runs_under_ctx() -> None:
    # No default event, but the ctx machinery stays live: set_identity works
    # and custom tool events still emit with the full inherited context.
    mock = make_mock(MCPAnalyticsConfig(autocapture={"tool_calls": False}))

    async def handler() -> dict[str, Any]:
        set_identity(SetIdentityInput(user_id="user-999xx"))
        ctx = get_current_context()
        assert ctx is not None
        mock.track_tool_event(ctx, "Custom Tool Event", {"foo": "bar"})
        return OK

    wrapped = mock.instrument_tool(handler, name="search_docs")
    bind(mock)

    await wrapped()

    assert mock.get_events(RESPONSE) == []  # no default event
    custom = mock.get_events("Custom Tool Event")
    assert len(custom) == 1
    assert custom[0]["user_id"] == "user-999xx"  # set_identity took effect
    assert custom[0]["event_properties"]["foo"] == "bar"
    assert custom[0]["event_properties"]["[MCP] Tool Name"] == "search_docs"


@pytest.mark.anyio
async def test_unbound_wrapper_is_a_true_noop_passthrough() -> None:
    # instrument_server never called and no fallback ctx bound → analytics is
    # off: the handler runs untouched, no ambient ctx, nothing emitted.
    mock = make_mock()
    received: Any = None
    ambient: Any = "sentinel"

    async def handler(q: str) -> dict[str, Any]:
        nonlocal received, ambient
        received = q
        ambient = get_current_context()
        return OK

    wrapped = mock.instrument_tool(handler, name="search_docs")
    # NOTE: no bind(mock) here — mock._server_ctx stays None.

    result = await wrapped(q="hi")

    assert result == OK  # tool ran and returned normally
    assert received == "hi"  # native args pass through
    assert ambient is None  # no ambient context established
    assert mock.events == []  # nothing emitted


@pytest.mark.anyio
async def test_unbound_wrapper_warns_once_per_tool_not_per_call(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger="amplitude_mcp_analytics")

    class RawClient:
        def track(self, event: dict[str, Any]) -> None:
            pass

        def flush(self) -> list[Any]:
            return []

    analytics = AmplitudeMCPAnalytics(
        amplitude=RawClient(), server_name="my-server", server_version="1.0.0"
    )

    async def handler() -> dict[str, Any]:
        return OK

    first = analytics.instrument_tool(handler, name="search_docs")
    second = analytics.instrument_tool(handler, name="other_tool")

    await first()
    await first()
    assert len(caplog.records) == 1  # once, not per call
    warning = caplog.records[0].getMessage()
    assert "instrument_tool('search_docs') ran without" in warning
    assert "instrument_server" in warning

    await second()
    await second()
    assert len(caplog.records) == 2  # per tool, not global


@pytest.mark.anyio
async def test_tool_error_inside_the_handler_preserves_the_ctx_error_code() -> None:
    # analytics.tool_error() builds the in-band error AND stores the structured
    # error on ctx; the wrapper must keep that richer descriptor (host code)
    # rather than rebuilding a generic returned_error from the result text.
    mock = make_mock()

    async def handler() -> dict[str, Any]:
        ctx = get_current_context()
        return mock.tool_error(
            ctx,  # type: ignore[arg-type]
            code="missing_chart_id",
            message="No chart ID was provided.",
            correction_message="Search for a chart first, then retry with the chart ID.",
            recoverable=True,
        )

    wrapped = mock.instrument_tool(handler, name="get_chart")
    bind(mock)

    result = await wrapped()

    assert result["isError"] is True
    # The client-facing text includes the correction guidance.
    assert "Search for a chart first" in result["content"][0]["text"]

    events = mock.get_events(RESPONSE)
    assert len(events) == 1
    props = events[0]["event_properties"]
    assert props["[MCP] Is Error"] is True
    assert props["[MCP] Error Code"] == "missing_chart_id"  # host code preserved
    assert props["[MCP] Error Type"] == "returned_error"
    assert props["[MCP] Error Message"] == "No chart ID was provided."
