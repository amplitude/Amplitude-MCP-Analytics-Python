"""Type definitions for the shared per-invocation MCP context (``ctx``) — the
seam every event derives its shared properties from. Load-bearing but not
frozen: the shape (server-scope base + tool-scope extension) is stable;
individual fields may still move.

Fields documented as *public* are part of the stable, semver-governed ctx
contract (``transport``, ``extra``, ``tool``). Changing or removing one is a
breaking change; other fields may still evolve before they are promoted.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from ..errors import McpToolError

__all__ = [
    "AnchorType",
    "IdentityResolvedFrom",
    "IdentityResolver",
    "McpAnchor",
    "McpClientInfo",
    "McpIdentity",
    "McpRequestInfo",
    "McpRequestMethod",
    "McpServerContext",
    "McpServerInfo",
    "McpTenant",
    "McpToolContext",
    "McpToolMeta",
    "McpTransport",
    "SetIdentityInput",
]

IdentityResolvedFrom = Literal["explicit", "authInfo", "anchor", "anonymous"]
"""Which fallback level produced the resolved subject, for debuggability."""

AnchorType = Literal["session-id", "trace", "process", "anonymous"]
"""Correlation-anchor source: stdio = process; stateful HTTP = session id;
stateless HTTP = trace -> anonymous floor. No session id is ever assumed."""

McpTransport = Literal["stdio", "streamable-http", "sse"]
"""MCP transport. ``sse`` (the deprecated HTTP+SSE transport) is a Python-SDK
addition to the cross-SDK taxonomy — the Node SDK emits only the first two."""

McpRequestMethod = Literal[
    "tools/call",
    "tools/list",
    "resources/read",
    "resources/list",
    "prompts/get",
    "prompts/list",
]
"""MCP protocol JSON-RPC method names (server-agnostic). Curated to the
execution requests this tool-scope context describes; widening is non-breaking."""


@dataclass
class McpTenant:
    """Operator/tenant — who owns the data. Maps to an Amplitude group."""

    group_type: str
    group_value: str


@dataclass
class McpIdentity:
    """Resolved subject identity; ``anonymous`` is the per-request floor (gated
    at emit time by ``config.emit_anonymous_event`` — see ``should_emit``)."""

    resolved_from: IdentityResolvedFrom
    user_id: str | None = None
    device_id: str | None = None


@dataclass
class SetIdentityInput:
    """Consumer-facing input for ``set_identity`` and the ``resolve_identity``
    callback. All fields are optional — the SDK fills in the rest via the
    fallback chain."""

    user_id: str | None = None
    device_id: str | None = None
    tenant: McpTenant | None = None


@dataclass
class McpAnchor:
    """Correlation anchor."""

    type: AnchorType
    value: str


IdentityResolver = Callable[[dict[str, Any] | None], SetIdentityInput]
"""Callback that resolves identity from the request's auth info. For MCP
servers using the standard OAuth flow where auth info carries the user's
claims. The SDK never guesses — the consumer specifies which claim maps to
which field."""


@dataclass
class McpClientInfo:
    """MCP client info — a dimension, NOT identity."""

    name: str | None = None
    """Protocol ``clientInfo.name`` from the handshake / ``_meta`` (e.g. ``"cursor"``)."""
    version: str | None = None
    user_agent: str | None = None
    """Raw HTTP ``User-Agent`` (HTTP transports only)."""


@dataclass
class McpServerInfo:
    """MCP server identity — attached to every event."""

    name: str
    version: str | None = None
    type: str | None = None
    """Server classification; allowed values may expand in future releases."""


@dataclass
class McpServerContext:
    """Server/connection-scope context — the base every event shares."""

    identity: McpIdentity
    anchor: McpAnchor
    transport: McpTransport
    """Server-scope MCP transport. Public."""
    server: McpServerInfo
    tenant: McpTenant | None = None
    protocol_version: str | None = None
    """Negotiated MCP protocol version."""
    client: McpClientInfo | None = None
    auth_type: str | None = None
    """How the subject authenticated; values are server-specific (e.g. ``"OAuth"``)."""
    extra: dict[str, Any] | None = None
    """Mutable enrichment bag for domain values without a top-level field. Public."""
    emit_anonymous_event: bool | None = None
    """Resolved from ``config.emit_anonymous_event``, stamped at context
    creation so the emit gate (``should_emit``) can honor it without threading
    config through every emit path. When truthy, the fully anonymous,
    tenant-less floor still emits. Internal."""
    sanitize_rationale: Callable[[str], str | None] | None = None
    """Resolved from ``config.sanitize_rationale``, stamped at context creation
    for the same reason as ``emit_anonymous_event``: ``[MCP] Rationale`` is
    lowered from the ctx by both the default tool event and every tool-scope
    custom event, and the ctx is the one seam all of them share. ``None`` means
    pass-through. Internal."""


@dataclass
class McpToolMeta:
    """Tool metadata the caller attaches when instrumenting a tool."""

    name: str
    owner: str | None = None
    extra: dict[str, Any] | None = None
    """Custom enrichment for this tool — its key/value pairs are carried on the
    ctx and emitted as event properties on the default ``[MCP] Tool Call
    Response`` event (the event's SDK-computed outcome values win on collision;
    avoid ``[MCP] ``-prefixed keys, which are reserved for SDK-derived
    properties)."""
    meta: dict[str, Any] = field(default_factory=dict)
    """Free-form metadata; forward-compatible and the home for server-specific
    fields (the Node SDK's index signature). ``tags`` (list of str) and
    ``category`` (str) are read from here for the reserved
    ``[MCP] Tool Tags`` / ``[MCP] Tool Category`` properties."""


@dataclass
class McpRequestInfo:
    """Per-request shape/size info — feeds duration/size properties on events."""

    method: McpRequestMethod | None = None
    size_bytes: int | None = None
    rationale: str | None = None
    """Host-supplied rationale for this invocation ("why the agent called this
    tool") — set via ``set_rationale``, never sniffed from tool inputs by the
    SDK. Emitted as the reserved ``[MCP] Rationale`` property on every
    tool-scope event lowered from this ctx."""
    response_http_status: int | None = None
    """Transport-level HTTP status of the response to this call, emitted as the
    reserved ``[MCP] Response HTTP Status`` property. Host-supplied — intended
    for events a host emits itself for calls that failed before dispatch (where
    the transport status actually varies). The instrumented-tool wrapper never
    sets it: it emits when the handler settles, before the response is written,
    and dispatched tool calls answer 200 anyway (tool failures are in-band
    ``isError`` results; their HTTP class belongs on ``[MCP] Error HTTP
    Status`` via ``McpToolError.http_status``)."""


@dataclass
class McpToolContext(McpServerContext):
    """Tool-invocation-scope context — extends the server context with the
    tool, request info, and an error slot. Handed to instrumented tool
    handlers."""

    # Note: dataclass inheritance requires a default; ``tool`` is always set by
    # ``create_tool_context``. Public.
    tool: McpToolMeta = field(default_factory=lambda: McpToolMeta(name=""))
    request: McpRequestInfo | None = None
    error: McpToolError | None = None
    """Populated on failure — classified by ``classify_error`` or ``build_tool_error``."""
