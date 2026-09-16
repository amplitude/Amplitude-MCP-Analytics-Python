"""Event names, wire property-name map, and value caps. The wire strings are
byte-identical to the Node SDK's — this is the cross-SDK schema contract."""

from __future__ import annotations

__all__ = [
    "ATTEMPTED_TOOL_NAME_MAX",
    "EVENT_PROPERTY_KEYS",
    "NO_SESSION",
    "SESSION_ENDED",
    "SESSION_INITIALIZED",
    "TOOLS_LISTED",
    "TOOL_CALL_REJECTED",
    "TOOL_CALL_RESPONSE",
    "TOOL_NAMES_MAX",
    "UNKNOWN",
]

NO_SESSION = "no-session"
UNKNOWN = "unknown"

#: Default tool-execution event.
TOOL_CALL_RESPONSE = "[MCP] Tool Call Response"

#: Default rejected-tool-call event — a ``tools/call`` request that failed
#: before any tool callback ran (unknown/disabled tool, input-schema
#: validation). Server-scope on purpose: the attempted name is unvalidated
#: caller input, so it never lands on the ``[MCP] Tool Name`` reserved key.
TOOL_CALL_REJECTED = "[MCP] Tool Call Rejected"

#: Upper bound on ``[MCP] Attempted Tool Name`` — rejected requests carry
#: unvalidated names (typos, hallucinations, scanner junk), so the value is
#: capped to keep property payloads bounded.
ATTEMPTED_TOOL_NAME_MAX = 200

#: Default server connection / capability events. Sessionless Streamable HTTP
#: still emits initialization when its real handshake succeeds, but no ended
#: event or duration is fabricated for its one-request transport.
SESSION_INITIALIZED = "[MCP] Session Initialized"
SESSION_ENDED = "[MCP] Session Ended"
TOOLS_LISTED = "[MCP] Tools Listed"

#: Upper bound on ``[MCP] Tool Names`` emitted on ``[MCP] Tools Listed``;
#: larger lists are truncated to this many names and flagged ``[MCP] Tool
#: Names Truncated`` (the ``[MCP] Tool Count`` always reflects the true total).
TOOL_NAMES_MAX = 100

#: snake_case -> wire property-name map for the default events: the reserved
#: ctx-derived fields plus the tool-call outcome keys. Every property carries
#: the ``[MCP] `` prefix so MCP analytics never collide with same-named
#: properties on other Amplitude events (e.g. a native ``session id``).
EVENT_PROPERTY_KEYS: dict[str, str] = {
    # server-scope reserved fields
    "session_id": "[MCP] Session ID",
    "client_name": "[MCP] Client Name",
    "client_version": "[MCP] Client Version",
    "oauth_client_id": "[MCP] OAuth Client ID",
    "user_agent": "[MCP] User Agent",
    "server_name": "[MCP] Server Name",
    "server_version": "[MCP] Server Version",
    "transport": "[MCP] Transport",
    "anchor_type": "[MCP] Anchor Type",
    "server_type": "[MCP] Server Type",
    "protocol_version": "[MCP] Protocol Version",
    "auth_type": "[MCP] Auth Type",
    # tool-scope reserved fields
    "tool_name": "[MCP] Tool Name",
    "tool_owner": "[MCP] Tool Owner",
    "tool_tags": "[MCP] Tool Tags",
    "tool_category": "[MCP] Tool Category",
    "rationale": "[MCP] Rationale",
    # tool-call outcome
    # Rejected-call tool name — unvalidated input, kept off `[MCP] Tool Name`.
    "attempted_tool_name": "[MCP] Attempted Tool Name",
    # Why a `tools/call` was rejected pre-dispatch — the structured, PII-free
    # companion to `[MCP] Error Message`, which the SDK cannot separate by code.
    "rejection_reason": "[MCP] Rejection Reason",
    "is_error": "[MCP] Is Error",
    "error_message": "[MCP] Error Message",
    "error_code": "[MCP] Error Code",
    "error_type": "[MCP] Error Type",
    "error_http_status": "[MCP] Error HTTP Status",
    "response_http_status": "[MCP] Response HTTP Status",
    "response_duration": "[MCP] Response Duration",
    "response_size": "[MCP] Response Size",
    "request_size": "[MCP] Request Size",
    # server connection / capability outcome
    "tool_count": "[MCP] Tool Count",
    "tool_names": "[MCP] Tool Names",
    "tool_names_truncated": "[MCP] Tool Names Truncated",
    "session_duration": "[MCP] Session Duration",
}
