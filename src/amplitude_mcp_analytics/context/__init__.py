"""Public context surface: the ctx types, factories, and ambient accessors."""

from .factory import create_server_context, create_tool_context
from .types import (
    AnchorType,
    ClientInfoResolver,
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
    ResolveClientInfoInput,
    SetIdentityInput,
)
from .vars import get_current_context, run_with_context, set_identity, set_rationale

__all__ = [
    "AnchorType",
    "ClientInfoResolver",
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
    "ResolveClientInfoInput",
    "SetIdentityInput",
    "create_server_context",
    "create_tool_context",
    "get_current_context",
    "run_with_context",
    "set_identity",
    "set_rationale",
]
