"""Construction of the MCP context. Caller-supplied values win; unset fields
fall back to the anonymous floor (anonymous identity/anchor)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ..errors import McpToolError
from .types import (
    McpAnchor,
    McpClientInfo,
    McpIdentity,
    McpRequestInfo,
    McpServerContext,
    McpServerInfo,
    McpTenant,
    McpToolContext,
    McpToolMeta,
    McpTransport,
)

__all__ = ["create_server_context", "create_tool_context"]


def create_server_context(
    *,
    server: McpServerInfo,
    transport: McpTransport,
    tenant: McpTenant | None = None,
    identity: McpIdentity | None = None,
    anchor: McpAnchor | None = None,
    protocol_version: str | None = None,
    client: McpClientInfo | None = None,
    auth_type: str | None = None,
    extra: dict[str, Any] | None = None,
    emit_anonymous_event: bool | None = None,
    sanitize_rationale: Callable[[str], str | None] | None = None,
) -> McpServerContext:
    """Build a server-scope context, flooring identity/anchor when unset.
    ``server`` and ``transport`` are required."""
    return McpServerContext(
        tenant=tenant,
        identity=identity if identity is not None else McpIdentity(resolved_from="anonymous"),
        anchor=anchor if anchor is not None else McpAnchor(type="anonymous", value=""),
        transport=transport,
        protocol_version=protocol_version,
        client=client,
        server=server,
        auth_type=auth_type,
        extra=extra,
        emit_anonymous_event=emit_anonymous_event,
        sanitize_rationale=sanitize_rationale,
    )


def create_tool_context(
    base: McpServerContext,
    tool: McpToolMeta,
    *,
    request: McpRequestInfo | None = None,
    error: McpToolError | None = None,
) -> McpToolContext:
    """Build a tool-scope context from a server context. The base is
    re-normalized through the anonymous floor (idempotent for an
    already-resolved context)."""
    floored = create_server_context(
        server=base.server,
        transport=base.transport,
        tenant=base.tenant,
        identity=base.identity,
        anchor=base.anchor,
        protocol_version=base.protocol_version,
        client=base.client,
        auth_type=base.auth_type,
        extra=base.extra,
        emit_anonymous_event=base.emit_anonymous_event,
        sanitize_rationale=base.sanitize_rationale,
    )
    return McpToolContext(
        tenant=floored.tenant,
        identity=floored.identity,
        anchor=floored.anchor,
        transport=floored.transport,
        protocol_version=floored.protocol_version,
        client=floored.client,
        server=floored.server,
        auth_type=floored.auth_type,
        extra=floored.extra,
        emit_anonymous_event=floored.emit_anonymous_event,
        sanitize_rationale=floored.sanitize_rationale,
        tool=tool,
        request=request,
        error=error,
    )
