"""Build a request-resolved ``ctx`` from a live MCP request (internal). Reads
the SDK's per-request ``RequestContext`` and composes the pure context
factories. In ``core/`` (not the SDK-free public ``context/``) because it is
SDK-aware and not public.

The Node SDK resolves per-request facts from the handler ``extra`` argument;
Python resolves them from the public ``request_ctx`` ContextVar (request id,
``_meta``, session, and — on HTTP transports — the ASGI request with headers).
"""

from __future__ import annotations

import logging
import os
import re
import uuid
from typing import Any

from ..context.factory import create_server_context, create_tool_context
from ..context.types import (
    IdentityResolver,
    McpAnchor,
    McpClientInfo,
    McpRequestInfo,
    McpServerContext,
    McpToolContext,
    McpToolMeta,
)
from .identity import ServerIdentity, resolve_identity_from_chain
from .mcp import (
    current_request_context,
    read_request_header,
    request_auth_info,
    request_meta_value,
)

__all__ = ["build_server_context", "build_tool_context", "resolve_transport_evidence"]

# The complete W3C `traceparent` shape: `version-traceid-parentid-flags`, all
# hex, exactly four fields. Anything looser (extra trailing fields, a truncated
# parent id) is a malformed header, not a correlation key — see _parse_trace_id.
# Case-insensitive by tolerance: the spec mandates lowercase, but a
# case-correct-otherwise header is far more likely a sloppy producer than an
# unrelated value, and the parsed id is lowercased for cross-SDK parity.
_TRACEPARENT_RE = re.compile(
    r"^(?P<version>[0-9a-f]{2})"
    r"-(?P<trace_id>[0-9a-f]{32})"
    r"-(?P<parent_id>[0-9a-f]{16})"
    r"-(?P<flags>[0-9a-f]{2})$",
    re.IGNORECASE,
)


def resolve_transport_evidence(request: Any) -> str | None:
    """Classify the transport from a message's HTTP request object (when the
    transport attached one via ``ServerMessageMetadata.request_context``).

    - No request / no headers → no evidence (stdio stays the default).
    - ``session_id`` query param → ``sse`` (the deprecated HTTP+SSE transport).
    - Otherwise → ``streamable-http`` (with or without an ``mcp-session-id``
      header — stateless streamable HTTP carries none).

    @internal
    """
    if request is None:
        return None
    headers = getattr(request, "headers", None)
    if headers is None:
        return None
    query_params = getattr(request, "query_params", None)
    if query_params is not None:
        try:
            if query_params.get("session_id"):
                return "sse"
        except Exception:
            pass
    return "streamable-http"


def _parse_trace_id(traceparent: Any) -> str | None:
    """Parse the trace-id (2nd field, 32 hex chars) from a W3C ``traceparent``:
    ``version-traceid-parentid-flags``. ``None`` if absent or malformed.

    The **whole** header must be well formed before its trace id becomes a
    correlation anchor — validating only the trace-id field would let
    ``00-<trace-id>-0000000000000000-01-junk`` (or any other malformed value
    that happens to carry 32 hex in the second position) stitch unrelated
    requests together under one identity. Rejected: anything but exactly four
    hyphen-separated fields (a future spec version that appends fields would
    have to be allowed for here deliberately), a non-hex or wrong-length
    field, the ``ff`` version (forbidden by the spec), and the all-zero
    trace-id or parent-id sentinels ("invalid" per W3C).

    A **valid** header still yields the identical lowercase 32-hex trace id the
    Node SDK produces, so cross-SDK correlation is unchanged. @internal
    """
    if not isinstance(traceparent, str):
        return None
    match = _TRACEPARENT_RE.match(traceparent.strip())
    if match is None:
        return None
    # `ff` is explicitly forbidden; any other version number is accepted so a
    # future revision still correlates (only its four-field form — a version
    # that appends fields would need a deliberate revisit here).
    if match.group("version").lower() == "ff":
        return None
    trace_id = match.group("trace_id").lower()
    if trace_id == "0" * 32 or match.group("parent_id") == "0" * 16:
        return None
    return trace_id


def _request_session_id(request_context: Any | None, scope_session_id: str | None) -> str | None:
    """The session id for this request: the ``mcp-session-id`` header
    (stateful streamable HTTP), the ``session_id`` query param (sse), else the
    scope-captured id (the streamable-http initialize POST carries no header —
    the server mints the id in its response)."""
    header = read_request_header(request_context, "mcp-session-id")
    if header is not None:
        return header
    request = getattr(request_context, "request", None) if request_context is not None else None
    query_params = getattr(request, "query_params", None)
    if query_params is not None:
        try:
            value = query_params.get("session_id")
            if isinstance(value, str) and value != "":
                return value
        except Exception:
            pass
    return scope_session_id


def _request_traceparent(request_context: Any | None) -> Any:
    """The W3C ``traceparent`` candidate for this request: the standard HTTP
    header first, MCP ``_meta`` as the fallback.

    The header is the transport-level truth for this hop — it is what every
    W3C-compliant client, proxy, and tracing SDK propagates automatically,
    while ``_meta.traceparent`` is an in-band convention an MCP client has to
    opt into. Reading only ``_meta`` (what the Node SDK does) loses correlation
    for normally-instrumented stateless HTTP callers, and a lost anchor means a
    fresh anonymous floor — i.e. a dropped event by default.

    A header that fails :func:`_parse_trace_id` yields to ``_meta`` rather than
    poisoning the request: a malformed header is not evidence that the ``_meta``
    value is stale. stdio has no HTTP request, so this reduces to ``_meta``
    there. @internal
    """
    header = read_request_header(request_context, "traceparent")
    if _parse_trace_id(header) is not None:
        return header
    return request_meta_value(request_context, "traceparent")


def _resolve_anchor(
    transport: str,
    session_id: str | None,
    bound_anchor: McpAnchor | None,
    traceparent: Any,
) -> McpAnchor:
    """Per-request correlation anchor, by transport:

    - **stdio** → process lifetime.
    - **HTTP with a session id** (stateful streamable HTTP / sse) → the session id.
    - **HTTP, host-managed sessions** — a session-id anchor bound via
      ``instrument_server(session_id=...)`` when the transport carries none.
    - **HTTP, stateless** (no session id anywhere) → W3C trace context if
      propagated (``traceparent`` header, else ``_meta`` — see
      :func:`_request_traceparent`), else an anonymous per-request floor
      (aggregate-only).

    A session id is never assumed — its absence selects the stateless branch.
    @internal
    """
    if transport == "stdio":
        return McpAnchor(type="process", value=str(os.getpid()))

    if session_id:
        return McpAnchor(type="session-id", value=session_id)

    # Host-managed session bound on the server scope (per-request servers whose
    # session ids live in the host's own store, not on the transport).
    if bound_anchor is not None and bound_anchor.type == "session-id" and bound_anchor.value:
        return bound_anchor

    trace_id = _parse_trace_id(traceparent)
    if trace_id is not None:
        return McpAnchor(type="trace", value=trace_id)

    # Anonymous per-request floor.
    return McpAnchor(type="anonymous", value=str(uuid.uuid4()))


def _resolve_client_info(
    request_context: Any | None, server_ctx: McpServerContext
) -> McpClientInfo:
    """Per-request client info. ``_meta.clientInfo`` wins (stateless clients
    carry it per request); the handshake value cached on the server scope (or
    readable off ``session.client_params``) is the fallback."""
    meta_info = request_meta_value(request_context, "clientInfo")
    meta_name = meta_version = None
    if isinstance(meta_info, dict):
        meta_name = meta_info.get("name")
        meta_version = meta_info.get("version")
    elif meta_info is not None:
        meta_name = getattr(meta_info, "name", None)
        meta_version = getattr(meta_info, "version", None)

    session = getattr(request_context, "session", None) if request_context is not None else None
    client_params = getattr(session, "client_params", None)
    handshake_info = getattr(client_params, "clientInfo", None)

    cached = server_ctx.client
    name = meta_name or getattr(handshake_info, "name", None) or (cached.name if cached else None)
    version = (
        meta_version
        or getattr(handshake_info, "version", None)
        or (cached.version if cached else None)
    )
    user_agent = read_request_header(request_context, "user-agent") or (
        cached.user_agent if cached else None
    )
    return McpClientInfo(name=name, version=version, user_agent=user_agent)


def _resolve_protocol_version(request_context: Any | None) -> str | None:
    """Negotiated protocol version for one request: the
    ``MCP-Protocol-Version`` header (HTTP), else ``_meta.protocolVersion``
    (stateless), else the session's negotiated value. ``None`` over stdio
    (carried at the handshake). @internal"""
    from_header = read_request_header(request_context, "mcp-protocol-version")
    if from_header is not None:
        return from_header
    from_meta = request_meta_value(request_context, "protocolVersion")
    if isinstance(from_meta, str):
        return from_meta
    session = getattr(request_context, "session", None) if request_context is not None else None
    client_params = getattr(session, "client_params", None)
    negotiated = getattr(client_params, "protocolVersion", None)
    return negotiated if isinstance(negotiated, str) else None


def build_server_context(
    server_ctx: McpServerContext,
    *,
    scope_session_id: str | None = None,
    resolve_identity: IdentityResolver | None = None,
    server_identity: ServerIdentity | None = None,
    logger: logging.Logger | None = None,
) -> McpServerContext:
    """Extend the server-scope ``server_ctx`` with one request's resolved
    fields (anchor, identity, protocol version, client info), without adding
    any tool scope. Reads the SDK's ambient ``request_ctx``; degrades to the
    scope's own values outside a request frame. ``transport`` is inherited from
    the server scope. @internal"""
    request_context = current_request_context()

    resolved_anchor = _resolve_anchor(
        server_ctx.transport,
        _request_session_id(request_context, scope_session_id),
        server_ctx.anchor,
        _request_traceparent(request_context),
    )

    resolved = resolve_identity_from_chain(
        anchor=resolved_anchor,
        resolve_identity=resolve_identity,
        auth_info=request_auth_info(request_context),
        server_identity=server_identity,
        logger=logger,
    )

    return create_server_context(
        server=server_ctx.server,
        transport=server_ctx.transport,
        anchor=resolved_anchor,
        protocol_version=_resolve_protocol_version(request_context) or server_ctx.protocol_version,
        identity=resolved.identity,
        tenant=resolved.tenant if resolved.tenant is not None else server_ctx.tenant,
        client=_resolve_client_info(request_context, server_ctx),
        auth_type=server_ctx.auth_type,
        extra=server_ctx.extra,
        emit_anonymous_event=server_ctx.emit_anonymous_event,
        sanitize_rationale=server_ctx.sanitize_rationale,
    )


def build_tool_context(
    server_ctx: McpServerContext,
    meta: McpToolMeta,
    *,
    scope_session_id: str | None = None,
    resolve_identity: IdentityResolver | None = None,
    server_identity: ServerIdentity | None = None,
    logger: logging.Logger | None = None,
) -> McpToolContext:
    """Extend the server-scope ``server_ctx`` with this request's fields plus
    the tool scope. Identity is resolved via the fallback chain. @internal"""
    return create_tool_context(
        build_server_context(
            server_ctx,
            scope_session_id=scope_session_id,
            resolve_identity=resolve_identity,
            server_identity=server_identity,
            logger=logger,
        ),
        meta,
        request=McpRequestInfo(method="tools/call"),
    )
