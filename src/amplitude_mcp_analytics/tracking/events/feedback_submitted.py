"""The opt-in feedback event — ``[MCP] Feedback Submitted``. @internal"""

from __future__ import annotations

from ...context.types import McpToolContext
from ...core.privacy import PrivacyConfig
from ...types import AmplitudeClientLike
from ..constants import EVENT_PROPERTY_KEYS as K
from ..constants import FEEDBACK_SUBMITTED
from ..track import track_tool_event
from ..types import TrackEventOptions


def emit_feedback_submitted(
    amplitude: AmplitudeClientLike,
    ctx: McpToolContext,
    *,
    helpful: bool,
    has_comment: bool,
    reason: str | None = None,
    solicited: bool | None = None,
    tool_names: list[str] | None = None,
    comment: str | None = None,
    privacy: PrivacyConfig | None = None,
) -> None:
    """Emit ``[MCP] Feedback Submitted``.

    Outcome props ride as ``track_tool_event``'s ``properties``, so they win
    over a colliding ``extra`` key. The anonymous-floor skip rule applies
    inside ``track_tool_event``. @internal
    """
    properties: dict[str, object] = {
        K["feedback_helpful"]: helpful,
        K["feedback_has_comment"]: has_comment,
    }
    if reason is not None:
        properties[K["feedback_reason"]] = reason
    if solicited is not None:
        properties[K["feedback_solicited"]] = solicited
    if tool_names:
        properties[K["feedback_tool_names"]] = list(tool_names)
    if comment:
        properties[K["feedback_comment"]] = comment

    options = None if privacy is None else TrackEventOptions(privacy=privacy)
    track_tool_event(amplitude, ctx, FEEDBACK_SUBMITTED, properties, options)
