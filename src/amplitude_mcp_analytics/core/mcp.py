"""THE MCP SDK adapter — the single module allowed to reach into the ``mcp``
package. Everything here is lazy, duck-typed, and hasattr-guarded so the SDK
keeps zero hard dependencies and degrades honestly when shapes drift.

Supports mcp 1.16 and up, including 2.x: legacy handler registry or middleware.
"""

from __future__ import annotations

import functools
import importlib
import inspect
import time
from collections.abc import Awaitable, Callable, Mapping
from contextvars import Context, ContextVar, copy_context
from dataclasses import replace
from typing import Any, Literal

from ..context.types import McpClientInfo
from ..exceptions import ConfigurationError
from .server_scope import ServerScope, current_server_scope, scope_var
from .tool_call_hook import mark_tool_callback_entered, observe_tool_call
from .tools_list_hook import observe_tools_list

__all__ = [
    "RegisteredToolState",
    "current_request_context",
    "lookup_registered_tool",
    "mcp_types",
    "read_request_header",
    "request_headers",
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
_request_context: ContextVar[Any] = ContextVar("amplitude_mcp_request_context", default=None)


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
                "Install it: uv add 'mcp>=1.16'."
            ) from err
    return _types_module


def unwrap_server(server: Any) -> tuple[Any, Any | None]:
    """Narrow either server era to its low-level server and registry. @internal"""
    tool_manager = getattr(server, "_tool_manager", None)
    low_level = (
        getattr(server, "_mcp_server", None)
        or getattr(server, "_lowlevel_server", None)
        or server
    )

    handlers = getattr(low_level, "request_handlers", None)
    if isinstance(handlers, dict):
        return low_level, tool_manager

    if is_v2_server(low_level):
        return low_level, tool_manager
    raise ConfigurationError(
        "instrument_server() received an object that is not a recognized MCP "
        "server (expected FastMCP, MCPServer, or a low-level Server from mcp>=1.16)."
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


def is_v2_server(server: Any) -> bool:
    """Whether the server exposes the context-tier middleware API. @internal"""
    return isinstance(getattr(server, "middleware", None), list) and callable(
        getattr(server, "add_request_handler", None)
    )


def request_client_info(ctx: Any) -> Any:
    """Handshake or envelope client implementation, when supplied. @internal"""
    params = getattr(getattr(ctx, "session", None), "client_params", None)
    return getattr(params, "client_info", None) or getattr(params, "clientInfo", None)


def request_protocol_version(ctx: Any) -> str | None:
    """Protocol version from either era's request/session context. @internal"""
    session = getattr(ctx, "session", None)
    params = getattr(session, "client_params", None)
    value = (
        getattr(ctx, "protocol_version", None)
        or getattr(session, "protocol_version", None)
        or getattr(params, "protocol_version", None)
        or getattr(params, "protocolVersion", None)
    )
    return value if isinstance(value, str) else None


def feedback_annotations(values: Mapping[str, Any]) -> Any:
    """Build annotations lazily without leaking protocol types. @internal"""
    return mcp_types().ToolAnnotations(**values)


class _ScopedReadStream:
    """Preserve the run scope when streams restore a message's sender context."""

    def __init__(self, stream: Any, scope: ServerScope) -> None:
        self._stream = stream
        self._scope = scope

    @property
    def last_context(self) -> Context:
        source = getattr(self._stream, "last_context", None)
        context = source.copy() if source is not None else copy_context()
        context.run(scope_var().set, self._scope)
        return context

    def __aiter__(self) -> _ScopedReadStream:
        return self

    async def __anext__(self) -> Any:
        return await self._stream.__anext__()

    async def receive(self) -> Any:
        return await self._stream.receive()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._stream, name)


def _wrap_v2_tool_callback(tool_manager: Any, name: Any) -> None:
    try:
        get_tool = getattr(tool_manager, "get_tool", None)
        if not callable(get_tool) or not isinstance(name, str):
            return
        tool: Any = get_tool(name)
        original = getattr(tool, "fn", None)
        marker = "_amplitude_mcp_callback_wrapped"
        if not callable(original) or getattr(original, marker, False):
            return
        wrapped: Callable[..., Any]
        if inspect.iscoroutinefunction(original):
            @functools.wraps(original)
            async def wrapped_async(*args: Any, **kwargs: Any) -> Any:
                mark_tool_callback_entered()
                return await original(*args, **kwargs)
            wrapped = wrapped_async
        else:
            @functools.wraps(original)
            def wrapped_sync(*args: Any, **kwargs: Any) -> Any:
                mark_tool_callback_entered()
                return original(*args, **kwargs)
            wrapped = wrapped_sync
        setattr(wrapped, marker, True)
        tool.fn = wrapped
    except Exception:
        pass


def install_v2_observer(
    server: Any,
    *,
    tool_manager: Any | None,
    make_scope: Callable[[], ServerScope],
    on_initialized: Callable[[ServerScope, str | None], None],
    on_ended: Callable[[ServerScope, float], None],
    on_listed: Callable[[dict[str, Any]], None] | None,
    on_called: Callable[[dict[str, Any]], None] | None,
) -> None:
    """Install the provisional v2 seam behind an SDK-free boundary. @internal"""
    from .build_context import resolve_transport_evidence

    original_run = server.run
    binding = object()

    def new_scope() -> ServerScope:
        scope = make_scope()
        scope.extra_state["binding"] = binding
        return scope

    @functools.wraps(original_run)
    async def run(read_stream: Any, *args: Any, **kwargs: Any) -> Any:
        scope = new_scope()
        token = scope_var().set(scope)
        try:
            return await original_run(_ScopedReadStream(read_stream, scope), *args, **kwargs)
        finally:
            scope_var().reset(token)
            if scope.session_start is not None:
                try:
                    on_ended(scope, (time.perf_counter() - scope.session_start) * 1000)
                except Exception:
                    pass

    server.run = run

    async def observer(ctx: Any, call_next: Callable[[Any], Awaitable[Any]]) -> Any:
        scope = current_server_scope()
        token = None
        if scope is None or scope.extra_state.get("binding") is not binding:
            scope = new_scope()
            scope.transport_persists = False
            token = scope_var().set(scope)
        request_token = _request_context.set(ctx)
        try:
            evidence = resolve_transport_evidence(getattr(ctx, "request", None))
            if evidence in ("streamable-http", "sse") and not scope.transport_resolved:
                if scope.ctx is not None:
                    scope.ctx.transport = evidence
                scope.transport_resolved = True
            params = ctx.params if isinstance(ctx.params, Mapping) else {}
            if ctx.method == "tools/call" and on_called is not None:
                _wrap_v2_tool_callback(tool_manager, params.get("name"))
                return await observe_tool_call(
                    params.get("name"), params.get("arguments"),
                    lambda: call_next(ctx), on_called,
                )
            if ctx.method == "tools/list" and on_listed is not None:
                return await observe_tools_list(lambda: call_next(ctx), on_listed)
            result = await call_next(ctx)
            if ctx.method == "initialize":
                if scope.ctx is not None:
                    info = params.get("clientInfo")
                    if isinstance(info, Mapping):
                        existing = scope.ctx.client or McpClientInfo()
                        scope.ctx.client = replace(
                            existing,
                            name=info.get("name") or existing.name,
                            version=info.get("version") or existing.version,
                        )
                    scope.ctx.protocol_version = (
                        result.get("protocolVersion") if isinstance(result, dict) else None
                    )
                try:
                    on_initialized(
                        scope, scope.ctx.protocol_version if scope.ctx is not None else None,
                    )
                except Exception:
                    pass
            return result
        finally:
            _request_context.reset(request_token)
            if token is not None:
                scope_var().reset(token)

    server.middleware.append(observer)


def current_request_context() -> Any | None:
    """The middleware's v2 context or the SDK's ambient v1 request context.
    Returns ``None`` outside a request frame. @internal"""
    current = _request_context.get()
    if current is not None:
        return current
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


def request_headers(request_context: Any | None) -> dict[str, str] | None:
    """HTTP request headers as a plain, SDK-free mapping. @internal"""
    if request_context is None:
        return None
    request = getattr(request_context, "request", None)
    headers = getattr(request, "headers", None)
    if headers is None:
        return None
    try:
        return {str(key): str(value) for key, value in headers.items()}
    except Exception:
        return None


def request_meta_value(request_context: Any | None, key: str) -> Any | None:
    """A pass-through ``_meta`` value for the current request. The SDK models
    ``_meta`` as ``RequestParams.Meta`` with ``extra="allow"``, so unknown keys
    (``traceparent``, ``clientInfo``, ``protocolVersion``) land in
    ``model_extra``."""
    if request_context is None:
        return None
    meta = getattr(request_context, "meta", None)
    if isinstance(meta, Mapping):
        return meta.get(key)
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
