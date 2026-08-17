"""THE MCP SDK adapter — the single module allowed to reach into the ``mcp``
package. Everything here is lazy, duck-typed, and hasattr-guarded so the SDK
keeps zero hard dependencies and degrades honestly when shapes drift.

Supported MCP SDK range: ``mcp>=1.16,<2``. The 2.x line restructures the
server (removes FastMCP, privatizes the handler registry); a 2.x server is
detected and rejected with a clear error rather than silently not emitting.
"""

from __future__ import annotations

import importlib
from typing import Any, Literal

from ..exceptions import ConfigurationError

__all__ = [
    "RegisteredToolState",
    "current_request_context",
    "lookup_registered_tool",
    "mcp_types",
    "read_request_header",
    "request_auth_info",
    "request_meta_value",
    "unwrap_server",
]

RegisteredToolState = Literal["missing", "disabled", "enabled"] | None
"""The attempted name's state in the server's tool registry, when readable.
``None`` means no registry to consult (low-level server) — honest degradation.
The official Python SDK's Tool model has no enabled/disabled flag, so
``disabled`` is currently unreachable here; it stays in the taxonomy for
cross-SDK schema parity."""

_types_module: Any = None


def mcp_types() -> Any:
    """The ``mcp.types`` module, imported lazily. Raises ConfigurationError
    when the mcp package is not installed."""
    global _types_module
    if _types_module is None:
        try:
            _types_module = importlib.import_module("mcp.types")
        except ImportError as err:
            raise ConfigurationError(
                "The 'mcp' package is required to instrument an MCP server. "
                "Install it: uv add 'mcp>=1.16,<2'."
            ) from err
    return _types_module


def unwrap_server(server: Any) -> tuple[Any, Any | None]:
    """Narrow a FastMCP or low-level server to ``(low_level_server,
    tool_manager_or_None)``. Raises :class:`ConfigurationError` for the mcp 2.x
    server shape or anything unrecognized."""
    tool_manager = getattr(server, "_tool_manager", None)
    low_level = getattr(server, "_mcp_server", None) or server

    handlers = getattr(low_level, "request_handlers", None)
    if isinstance(handlers, dict):
        return low_level, tool_manager

    # mcp 2.x privatized the registry (`_request_handlers` keyed by method
    # string) and replaced FastMCP with MCPServer. Fail loudly rather than
    # silently emitting nothing.
    if hasattr(low_level, "_request_handlers") or type(server).__name__ == "MCPServer":
        raise ConfigurationError(
            "This mcp SDK version is not supported yet: amplitude-mcp-analytics "
            "currently supports mcp>=1.16,<2. The installed SDK looks like the 2.x "
            "line — pin 'mcp<2' or wait for a release of this SDK with 2.x support."
        )
    raise ConfigurationError(
        "instrument_server() received an object that is not a recognized MCP "
        "server (expected mcp.server.fastmcp.FastMCP or mcp.server.lowlevel.Server "
        "from mcp>=1.16,<2)."
    )


def lookup_registered_tool(tool_manager: Any | None, name: str | None) -> RegisteredToolState:
    """The attempted tool name's registry state, when a FastMCP tool manager is
    available. ``None`` (not ``'missing'``) when there is no registry or the
    shape is unreadable, so a low-level ``Server`` degrades honestly."""
    if tool_manager is None or name is None:
        return None
    get_tool = getattr(tool_manager, "get_tool", None)
    if not callable(get_tool):
        return None
    try:
        tool = get_tool(name)
    except Exception:
        return None
    if tool is None:
        return "missing"
    if getattr(tool, "enabled", True) is False:
        return "disabled"
    return "enabled"


def current_request_context() -> Any | None:
    """The SDK's per-request ``RequestContext`` (from the public
    ``mcp.server.lowlevel.server.request_ctx`` ContextVar), or ``None`` outside
    a request frame or when mcp is not importable."""
    try:
        module = importlib.import_module("mcp.server.lowlevel.server")
    except ImportError:
        return None
    request_ctx = getattr(module, "request_ctx", None)
    if request_ctx is None:
        return None
    try:
        return request_ctx.get()
    except LookupError:
        return None


def read_request_header(request_context: Any | None, name: str) -> str | None:
    """Case-insensitive header read off the RequestContext's HTTP request
    (streamable-http / sse), or ``None`` when there is no HTTP request. The
    ``request`` field is typed ``object`` upstream, so everything is guarded."""
    if request_context is None:
        return None
    request = getattr(request_context, "request", None)
    headers = getattr(request, "headers", None)
    if headers is None:
        return None
    try:
        value = headers.get(name)
    except Exception:
        return None
    return value if isinstance(value, str) and value != "" else None


def request_meta_value(request_context: Any | None, key: str) -> Any | None:
    """A pass-through ``_meta`` value for the current request. The SDK models
    ``_meta`` as ``RequestParams.Meta`` with ``extra="allow"``, so unknown keys
    (``traceparent``, ``clientInfo``, ``protocolVersion``) land in
    ``model_extra``."""
    if request_context is None:
        return None
    meta = getattr(request_context, "meta", None)
    if meta is None:
        return None
    direct = getattr(meta, key, None)
    if direct is not None:
        return direct
    model_extra = getattr(meta, "model_extra", None)
    if isinstance(model_extra, dict):
        return model_extra.get(key)
    return None


def request_auth_info(request_context: Any | None) -> dict[str, Any] | None:
    """Auth info for the ``resolve_identity`` callback, as a plain dict.

    Primary source: the authenticated user on the HTTP request's ASGI scope
    (set by the SDK's ``AuthContextMiddleware``/bearer middleware) — its
    ``access_token`` carries token/client_id/scopes/expires_at. Fallback: the
    SDK's ``get_access_token()`` contextvar, which is only visible in the
    handler task under stateless HTTP (in stateful mode the session runs in a
    different task than the ASGI request).
    """
    request = getattr(request_context, "request", None) if request_context is not None else None
    scope = getattr(request, "scope", None)
    user = None
    if isinstance(scope, dict):
        user = scope.get("user")
    access_token = getattr(user, "access_token", None)

    if access_token is None:
        try:
            auth_module = importlib.import_module("mcp.server.auth.middleware.auth_context")
            access_token = auth_module.get_access_token()
        except Exception:
            access_token = None

    if access_token is None:
        return None
    model_dump = getattr(access_token, "model_dump", None)
    if callable(model_dump):
        try:
            dumped = model_dump()
            if isinstance(dumped, dict):
                return dumped
        except Exception:
            pass
    return {
        "token": getattr(access_token, "token", None),
        "client_id": getattr(access_token, "client_id", None),
        "scopes": getattr(access_token, "scopes", None),
        "expires_at": getattr(access_token, "expires_at", None),
    }
