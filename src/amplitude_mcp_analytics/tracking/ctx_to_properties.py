"""Pure mapper from the typed MCP ``ctx`` to Amplitude event payload fields.
This is the seam every emit site — ``track_server_event``, ``track_tool_event``,
and the default-event emitters — lowers through.

Per-tenant ``extra`` enrichment values are spread onto ``event_properties``
after the typed fields, so a host can surface domain values without a custom
emit path. Caller-supplied properties on the ``track_*`` methods always win
over both.
"""

from __future__ import annotations

from typing import Any

from ..context.types import McpServerContext, McpToolContext
from .constants import EVENT_PROPERTY_KEYS, NO_SESSION, UNKNOWN
from .types import AmplitudeFields, DefaultServerFields, DefaultToolFields

__all__ = ["ctx_to_amplitude_fields", "ctx_to_amplitude_fields_for_tool", "should_emit"]


def ctx_to_amplitude_fields(ctx: McpServerContext) -> AmplitudeFields:
    """Lower an :class:`McpServerContext` to the Amplitude payload fields
    shared by every event the SDK emits. Server-scope events consume this
    directly; tool-scope events go through
    :func:`ctx_to_amplitude_fields_for_tool` which extends the result."""
    event_fields: DefaultServerFields = {
        "session_id": ctx.anchor.value if ctx.anchor.type == "session-id" else NO_SESSION,
        "client_name": (ctx.client.name if ctx.client and ctx.client.name else UNKNOWN),
        "user_agent": (ctx.client.user_agent if ctx.client and ctx.client.user_agent else UNKNOWN),
        "server_name": ctx.server.name,
        "transport": ctx.transport,
        "anchor_type": ctx.anchor.type,
    }

    if ctx.client is not None and ctx.client.version is not None:
        event_fields["client_version"] = ctx.client.version
    if ctx.server.version is not None:
        event_fields["server_version"] = ctx.server.version
    if ctx.server.type is not None:
        event_fields["server_type"] = ctx.server.type
    if ctx.protocol_version is not None:
        event_fields["protocol_version"] = ctx.protocol_version
    if ctx.auth_type is not None:
        event_fields["auth_type"] = ctx.auth_type

    fields = AmplitudeFields(
        event_properties=event_fields,
        extra_properties=dict(ctx.extra) if ctx.extra else {},
    )
    if ctx.identity.user_id is not None:
        fields.user_id = ctx.identity.user_id
    if ctx.identity.device_id is not None:
        fields.device_id = ctx.identity.device_id
    if ctx.tenant is not None:
        fields.groups = {ctx.tenant.group_type: ctx.tenant.group_value}
    return fields


def ctx_to_amplitude_fields_for_tool(ctx: McpToolContext) -> AmplitudeFields:
    """Lower an :class:`McpToolContext` — adds the tool-scope properties on top
    of :func:`ctx_to_amplitude_fields`. Forward-compatible tool metadata
    (``tags``, ``category``) is read from ``ctx.tool.meta``."""
    base = ctx_to_amplitude_fields(ctx)
    fields: DefaultToolFields = {**base.event_properties, "tool_name": ctx.tool.name}

    if ctx.tool.owner is not None:
        fields["tool_owner"] = ctx.tool.owner
    tags = ctx.tool.meta.get("tags")
    if isinstance(tags, (list, tuple)) and len(tags) > 0:
        fields["tool_tags"] = list(tags)
    category = ctx.tool.meta.get("category")
    if isinstance(category, str) and len(category) > 0:
        fields["tool_category"] = category
    rationale = ctx.request.rationale if ctx.request is not None else None
    if isinstance(rationale, str) and len(rationale) > 0:
        fields["rationale"] = rationale
    response_http_status = ctx.request.response_http_status if ctx.request is not None else None
    if isinstance(response_http_status, int):
        fields["response_http_status"] = response_http_status

    return AmplitudeFields(
        event_properties=fields,
        extra_properties={**base.extra_properties, **(ctx.tool.extra or {})},
        user_id=base.user_id,
        device_id=base.device_id,
        groups=base.groups,
    )


def reserved_fields_to_properties(fields: dict[str, Any]) -> dict[str, Any]:
    """Convert reserved fields to their wire property names via
    :data:`EVENT_PROPERTY_KEYS`. ``None`` fields are skipped. @internal"""
    out: dict[str, Any] = {}
    for key, value in fields.items():
        if value is None:
            continue
        wire_key = EVENT_PROPERTY_KEYS.get(key)
        if wire_key is not None:
            out[wire_key] = value
    return out


def should_emit(ctx: McpServerContext) -> bool:
    """Emit gate for the fully anonymous floor. When the subject resolved to
    the per-request anonymous floor (``resolved_from == 'anonymous'``) and
    there is no tenant, the event carries only a synthetic ``device_id`` that
    never recurs (a fresh id per request, no cross-call stitching). Emitting
    these by default inflates unique-user/device counts with noise, so they are
    dropped unless the consumer opts in via ``config.emit_anonymous_event``
    (stamped onto the ctx). Any non-anonymous subject or any tenant always
    emits."""
    if ctx.emit_anonymous_event:
        return True
    if ctx.identity.resolved_from == "anonymous" and ctx.tenant is None:
        return False
    return True
