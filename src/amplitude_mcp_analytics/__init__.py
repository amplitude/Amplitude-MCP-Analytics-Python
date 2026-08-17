"""Amplitude MCP Analytics SDK — MCP server usage tracking for Amplitude.

Public surface (semver-governed; guarded by ``tests/test_smoke.py``). Mirrors
the Node SDK's root exports 1:1 in snake_case.
"""

from .client import AmplitudeMCPAnalytics, create_mcp_analytics
from .config import (
    AutocaptureConfig,
    ErrorMessageSanitizer,
    MCPAnalyticsConfig,
    ResolvedAutocapture,
)
from .context import (
    AnchorType,
    IdentityResolvedFrom,
    IdentityResolver,
    McpAnchor,
    McpClientInfo,
    McpIdentity,
    McpRequestInfo,
    McpRequestMethod,
    McpServerContext,
    McpServerInfo,
    McpTenant,
    McpToolContext,
    McpToolMeta,
    McpTransport,
    SetIdentityInput,
    create_server_context,
    create_tool_context,
    get_current_context,
    run_with_context,
    set_identity,
    set_rationale,
)
from .errors import (
    McpToolError,
    McpToolErrorType,
    build_tool_error,
    classify_error,
    tool_error_result,
)
from .testing import MockAmplitudeMCPAnalytics
from .tracking import (
    AmplitudeFields,
    DefaultServerFields,
    DefaultToolFields,
    TrackEventOptions,
    ctx_to_amplitude_fields,
    ctx_to_amplitude_fields_for_tool,
    should_emit,
)
from .tracking.track import track_server_event, track_tool_event
from .types import AmplitudeClientLike, AmplitudeEvent

__all__ = [
    "AmplitudeClientLike",
    "AmplitudeEvent",
    "AmplitudeFields",
    "AmplitudeMCPAnalytics",
    "AnchorType",
    "AutocaptureConfig",
    "DefaultServerFields",
    "DefaultToolFields",
    "ErrorMessageSanitizer",
    "IdentityResolvedFrom",
    "IdentityResolver",
    "MCPAnalyticsConfig",
    "McpAnchor",
    "McpClientInfo",
    "McpIdentity",
    "McpRequestInfo",
    "McpRequestMethod",
    "McpServerContext",
    "McpServerInfo",
    "McpTenant",
    "McpToolContext",
    "McpToolError",
    "McpToolErrorType",
    "McpToolMeta",
    "McpTransport",
    "MockAmplitudeMCPAnalytics",
    "ResolvedAutocapture",
    "SetIdentityInput",
    "TrackEventOptions",
    "build_tool_error",
    "classify_error",
    "create_mcp_analytics",
    "create_server_context",
    "create_tool_context",
    "ctx_to_amplitude_fields",
    "ctx_to_amplitude_fields_for_tool",
    "get_current_context",
    "run_with_context",
    "set_identity",
    "set_rationale",
    "should_emit",
    "tool_error_result",
    "track_server_event",
    "track_tool_event",
]
