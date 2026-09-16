"""Public-surface tripwire (ports the Node repo's smoke.test.ts): every
expected export exists, and forgotten exports fail here before a consumer
finds out."""

from __future__ import annotations

import importlib
import importlib.metadata

EXPECTED_EXPORTS = [
    # values
    "AmplitudeMCPAnalytics",
    "__version__",
    "MCPAnalyticsConfig",
    "create_server_context",
    "create_tool_context",
    "get_current_context",
    "run_with_context",
    "set_identity",
    "set_rationale",
    "build_tool_error",
    "classify_error",
    "tool_error_result",
    "MockAmplitudeMCPAnalytics",
    "ctx_to_amplitude_fields",
    "ctx_to_amplitude_fields_for_tool",
    "should_emit",
    "track_server_event",
    "track_tool_event",
    # types / dataclasses
    "AmplitudeClientLike",
    "AmplitudeEvent",
    "AmplitudeFields",
    "AnchorType",
    "AutocaptureConfig",
    "ClientInfoResolver",
    "DefaultServerFields",
    "DefaultToolFields",
    "ErrorMessageSanitizer",
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
    "McpToolError",
    "McpToolErrorType",
    "McpToolMeta",
    "McpTransport",
    "RationaleSanitizer",
    "ResolvedAutocapture",
    "ResolveClientInfoInput",
    "SetIdentityInput",
    "TrackEventOptions",
]


def test_root_exports_exist() -> None:
    module = importlib.import_module("amplitude_mcp_analytics")
    for name in EXPECTED_EXPORTS:
        assert hasattr(module, name), f"missing public export: {name}"
        assert name in module.__all__, f"{name} not in __all__"


def test_all_matches_exports() -> None:
    module = importlib.import_module("amplitude_mcp_analytics")
    for name in module.__all__:
        assert hasattr(module, name), f"__all__ names missing attribute: {name}"


def test_subpath_modules_importable() -> None:
    for subpath in (
        "amplitude_mcp_analytics.client",
        "amplitude_mcp_analytics.config",
        "amplitude_mcp_analytics.context",
        "amplitude_mcp_analytics.exceptions",
        "amplitude_mcp_analytics.testing",
        "amplitude_mcp_analytics.tracking",
        "amplitude_mcp_analytics.types",
    ):
        importlib.import_module(subpath)


def test_version_is_the_installed_distribution_version() -> None:
    module = importlib.import_module("amplitude_mcp_analytics")
    # Resolved from package metadata (the source tree is installed editable),
    # never hand-maintained alongside pyproject's `version`.
    assert module.__version__ == importlib.metadata.version("amplitude-mcp-analytics")
    assert module.__version__ != "0.0.0"


def test_create_mcp_analytics_is_not_exported() -> None:
    # Dropped before 0.1.0: redundant with the constructor (see PORTING.md).
    module = importlib.import_module("amplitude_mcp_analytics")
    assert not hasattr(module, "create_mcp_analytics")


def test_mock_defaults() -> None:
    from amplitude_mcp_analytics.testing import MockAmplitudeMCPAnalytics

    mock = MockAmplitudeMCPAnalytics(server_name="smoke", server_version="0.0.0")
    assert mock.config.debug is False
    assert mock.config.dry_run is False
    assert mock.events == []
    assert callable(mock.instrument_server)
    assert callable(mock.instrument_tool)
    assert callable(mock.set_identity)
