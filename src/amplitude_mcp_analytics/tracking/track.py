"""Custom event emitters — ``analytics.track_server_event(ctx, name, props)``
and ``analytics.track_tool_event(ctx, name, props)``.

Inherit every cross-cutting property from ``ctx`` (identity, tenant, client,
server, auth, transport, etc.) so the caller only specifies the event-specific
delta. Caller-supplied ``properties`` win on collision; emit failures are
swallowed (best-effort) and logged via the configured logger. Pass
``TrackEventOptions(drop_extra_props=True)`` to omit the ``ctx.extra`` bag.
"""

from __future__ import annotations

from typing import Any

from ..context.types import McpServerContext, McpToolContext
from ..types import AmplitudeClientLike, AmplitudeEvent
from ..utils.logger import get_logger
from .ctx_to_properties import (
    ctx_to_amplitude_fields,
    ctx_to_amplitude_fields_for_tool,
    reserved_fields_to_properties,
    should_emit,
)
from .types import AmplitudeFields, TrackEventOptions

__all__ = ["track_server_event", "track_tool_event"]


def _emit(
    amplitude: AmplitudeClientLike,
    fields: AmplitudeFields,
    event_name: str,
    properties: dict[str, Any] | None,
    options: TrackEventOptions | None,
) -> None:
    event: AmplitudeEvent = {
        "event_type": event_name,
        "event_properties": {
            **reserved_fields_to_properties(fields.event_properties),
            **({} if options is not None and options.drop_extra_props else fields.extra_properties),
            **(properties or {}),
        },
    }
    if fields.user_id is not None:
        event["user_id"] = fields.user_id
    if fields.device_id is not None:
        event["device_id"] = fields.device_id
    if fields.groups is not None:
        event["groups"] = fields.groups
    amplitude.track(event)


def track_server_event(
    amplitude: AmplitudeClientLike,
    ctx: McpServerContext,
    event_name: str,
    properties: dict[str, Any] | None = None,
    options: TrackEventOptions | None = None,
) -> None:
    """Emit a server-scope custom event, inheriting the ctx's reserved
    properties. Property precedence (last wins): reserved < ``ctx.extra`` <
    caller ``properties``. Drops silently under the identity/tenant skip rule
    and on client error (best-effort)."""
    if not should_emit(ctx):
        return
    try:
        _emit(amplitude, ctx_to_amplitude_fields(ctx), event_name, properties, options)
    except Exception as err:  # noqa: BLE001 — best-effort telemetry, never raises
        get_logger(amplitude).warning(
            "track_server_event('%s') failed: %s", event_name, err
        )


def track_tool_event(
    amplitude: AmplitudeClientLike,
    ctx: McpToolContext,
    event_name: str,
    properties: dict[str, Any] | None = None,
    options: TrackEventOptions | None = None,
) -> None:
    """Emit a tool-scope custom event — same contract as
    :func:`track_server_event` plus the tool metadata and ``extra``."""
    if not should_emit(ctx):
        return
    try:
        _emit(amplitude, ctx_to_amplitude_fields_for_tool(ctx), event_name, properties, options)
    except Exception as err:  # noqa: BLE001 — best-effort telemetry, never raises
        get_logger(amplitude).warning(
            "track_tool_event('%s') failed: %s", event_name, err
        )
