"""Amplitude MCP Analytics SDK — MCP server usage tracking for Amplitude.

Public surface (semver-governed; guarded by ``tests/test_smoke.py``). Mirrors
the Node SDK's root exports in snake_case, minus the ones Python does not need
(see ``PORTING.md``).
"""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _version

from .client import AmplitudeMCPAnalytics
from .config import (
    AutocaptureConfig,
    ErrorMessageSanitizer,
    MCPAnalyticsConfig,
    RationaleSanitizer,
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

try:
    __version__ = _version("amplitude-mcp-analytics")
except PackageNotFoundError:  # pragma: no cover — running from an uninstalled source tree
    __version__ = "0.0.0"

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
    "RationaleSanitizer",
    "ResolvedAutocapture",
    "SetIdentityInput",
    "TrackEventOptions",
    "__version__",
    "build_tool_error",
    "classify_error",
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
