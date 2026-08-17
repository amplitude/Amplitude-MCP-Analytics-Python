"""The five default events. All emitters are internal — they are called by
``instrument_server`` / ``instrument_tool``, never by consumers directly."""

from __future__ import annotations

from typing import Any

from ...config import ErrorMessageSanitizer
from ...context.types import McpServerContext, McpToolContext
from ...types import AmplitudeClientLike
from ..constants import (
    ATTEMPTED_TOOL_NAME_MAX,
    SESSION_ENDED,
    SESSION_INITIALIZED,
    TOOL_CALL_REJECTED,
    TOOL_CALL_RESPONSE,
    TOOL_NAMES_MAX,
    TOOLS_LISTED,
)
from ..constants import (
    EVENT_PROPERTY_KEYS as K,
)
from ..sanitize_error_message import sanitize_error_message
from ..track import track_server_event, track_tool_event

__all__ = [
    "emit_session_ended",
    "emit_session_initialized",
    "emit_tool_call_rejected",
    "emit_tool_call_response",
    "emit_tools_listed",
]


def emit_session_initialized(amplitude: AmplitudeClientLike, ctx: McpServerContext) -> None:
    """Emit ``[MCP] Session Initialized`` at the ``initialize`` handshake.
    Carries only the ctx-derived reserved props; the server ``ctx.extra`` bag
    rides along downstream. Session events apply only where a protocol session
    exists — never fabricated on stateless HTTP. @internal"""
    track_server_event(amplitude, ctx, SESSION_INITIALIZED)


def emit_session_ended(
    amplitude: AmplitudeClientLike,
    ctx: McpServerContext,
    *,
    duration_ms: float | None = None,
) -> None:
    """Emit ``[MCP] Session Ended`` when the transport closes — only for
    sessions that emitted ``[MCP] Session Initialized`` first. Adds
    ``[MCP] Session Duration`` when known. @internal"""
    properties: dict[str, Any] = {}
    if duration_ms is not None:
        properties[K["session_duration"]] = round(duration_ms)
    track_server_event(amplitude, ctx, SESSION_ENDED, properties)


def emit_tools_listed(
    amplitude: AmplitudeClientLike,
    ctx: McpServerContext,
    *,
    is_error: bool,
    tool_count: int,
    tool_names: list[str] | None = None,
    duration_ms: float | None = None,
    response_size_bytes: int | None = None,
    error_message: str | None = None,
    error_code: str | None = None,
    error_type: str | None = None,
    sanitize: ErrorMessageSanitizer | None = None,
) -> None:
    """Emit ``[MCP] Tools Listed``. Called by ``instrument_server`` when a
    ``tools/list`` request is served. @internal"""
    properties: dict[str, Any] = {
        K["is_error"]: is_error,
        K["tool_count"]: tool_count,
    }
    if tool_names is not None:
        if len(tool_names) > TOOL_NAMES_MAX:
            properties[K["tool_names"]] = tool_names[:TOOL_NAMES_MAX]
            properties[K["tool_names_truncated"]] = True
        else:
            properties[K["tool_names"]] = tool_names
    if duration_ms is not None:
        properties[K["response_duration"]] = round(duration_ms)
    if response_size_bytes is not None:
        properties[K["response_size"]] = response_size_bytes
    if error_message is not None:
        message = sanitize_error_message(error_message, sanitize)
        if message is not None:
            properties[K["error_message"]] = message
    if error_code is not None:
        properties[K["error_code"]] = error_code
    if error_type is not None:
        properties[K["error_type"]] = error_type

    track_server_event(amplitude, ctx, TOOLS_LISTED, properties)


def emit_tool_call_response(
    amplitude: AmplitudeClientLike,
    ctx: McpToolContext,
    *,
    is_tool_error: bool,
    duration_ms: float,
    request_size_bytes: int | None = None,
    response_size_bytes: int | None = None,
    sanitize: ErrorMessageSanitizer | None = None,
) -> None:
    """Emit ``[MCP] Tool Call Response``. Called by ``instrument_tool``; the
    outcome props ride as ``track_tool_event``'s ``properties`` (ctx-derived
    reserved props and tool ``extra`` are added downstream). @internal"""
    properties: dict[str, Any] = {
        K["is_error"]: is_tool_error,
        K["response_duration"]: round(duration_ms),
    }
    if request_size_bytes is not None:
        properties[K["request_size"]] = request_size_bytes
    if response_size_bytes is not None:
        properties[K["response_size"]] = response_size_bytes

    if ctx.error is not None:
        message = sanitize_error_message(ctx.error.message, sanitize)
        if message is not None:
            properties[K["error_message"]] = message
        if ctx.error.code is not None:
            properties[K["error_code"]] = ctx.error.code
        properties[K["error_type"]] = ctx.error.type
        # HTTP status attached to the tool's failure (upstream response /
        # HTTP-shaped raised error) — NOT the MCP transport status.
        if ctx.error.http_status is not None:
            properties[K["error_http_status"]] = ctx.error.http_status

    track_tool_event(amplitude, ctx, TOOL_CALL_RESPONSE, properties)


def emit_tool_call_rejected(
    amplitude: AmplitudeClientLike,
    ctx: McpServerContext,
    *,
    attempted_tool_name: str | None = None,
    rejection_reason: str | None = None,
    error_message: str | None = None,
    error_code: str | None = None,
    error_type: str | None = None,
    duration_ms: float | None = None,
    response_size_bytes: int | None = None,
    response_http_status: int | None = None,
    sanitize: ErrorMessageSanitizer | None = None,
) -> None:
    """Emit ``[MCP] Tool Call Rejected``. Called by ``instrument_server`` when a
    ``tools/call`` request fails before any tool callback runs. A
    **server-scope** event on purpose: the attempted name is unvalidated caller
    input (hallucinated, mistyped, or since-removed tools), so it rides on
    ``[MCP] Attempted Tool Name`` and never pollutes the ``[MCP] Tool Name``
    reserved key that per-tool dashboards slice on. @internal"""
    properties: dict[str, Any] = {K["is_error"]: True}
    if attempted_tool_name is not None:
        properties[K["attempted_tool_name"]] = attempted_tool_name[:ATTEMPTED_TOOL_NAME_MAX]
    if rejection_reason is not None:
        properties[K["rejection_reason"]] = rejection_reason
    if error_message is not None:
        message = sanitize_error_message(error_message, sanitize)
        if message is not None:
            properties[K["error_message"]] = message
    if error_code is not None:
        properties[K["error_code"]] = error_code
    if error_type is not None:
        properties[K["error_type"]] = error_type
    if duration_ms is not None:
        properties[K["response_duration"]] = round(duration_ms)
    if response_size_bytes is not None:
        properties[K["response_size"]] = response_size_bytes
    if response_http_status is not None:
        properties[K["response_http_status"]] = response_http_status

    track_server_event(amplitude, ctx, TOOL_CALL_REJECTED, properties)
