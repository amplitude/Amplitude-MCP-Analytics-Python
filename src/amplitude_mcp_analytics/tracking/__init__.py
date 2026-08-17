"""Public tracking surface: ctx lowering, the emit gate, and custom-event
tracking. (``instrument_tool`` is deliberately *not* exported here — it is
reached via ``AmplitudeMCPAnalytics.instrument_tool``.)"""

from .ctx_to_properties import (
    ctx_to_amplitude_fields,
    ctx_to_amplitude_fields_for_tool,
    should_emit,
)
from .types import AmplitudeFields, DefaultServerFields, DefaultToolFields, TrackEventOptions

__all__ = [
    "AmplitudeFields",
    "DefaultServerFields",
    "DefaultToolFields",
    "TrackEventOptions",
    "ctx_to_amplitude_fields",
    "ctx_to_amplitude_fields_for_tool",
    "should_emit",
]
