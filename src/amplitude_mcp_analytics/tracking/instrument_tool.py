"""The tool-instrumentation wrapper — ``analytics.instrument_tool(handler, meta)``.

The handler is your native MCP tool handler, unchanged — a FastMCP tool
function (sync or async, with its own named parameters and optional injected
``Context``), or a low-level ``call_tool`` callback. ``instrument_tool`` does
not alter its signature: ``functools.wraps`` preserves the original signature
(FastMCP builds the input schema and finds the ``Context`` parameter through
``__wrapped__``), and arguments pass through untouched.

On each call it:
  1. Builds the per-request context (transport-aware correlation anchor,
     client info, protocol version) from the scope bound by
     ``instrument_server``. The SDK builds the context; you never construct or
     pass it.
  2. Runs the handler with that context available via ``get_current_context()``
     (the ``track_*`` methods also accept it explicitly).
  3. Emits a tool-call event (success + duration, or failure) and records the
     error on the context. A failure is either a raised exception OR a tool
     result carrying ``isError: true``.
  4. Preserves the underlying handler's sync/async nature — a sync handler
     stays sync; an async handler stays a coroutine function.

If ``instrument_server`` was never called, the wrapper is a no-op passthrough:
the handler runs untouched and nothing is emitted (a once-per-tool warning is
logged).

Handler exceptions are RE-RAISED after the failure event is emitted, so the
MCP SDK still surfaces them to the client; the best-effort guarantee applies
only to event emission, not to the handler's return value.
"""

from __future__ import annotations

import functools
import inspect
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ..config import ErrorMessageSanitizer
from ..context.types import (
    ClientInfoResolver,
    IdentityResolver,
    McpServerContext,
    McpToolContext,
    McpToolMeta,
)
from ..context.vars import _context_var
from ..core.build_context import build_tool_context
from ..core.identity import ServerIdentity
from ..core.serialize import payload_byte_size
from ..core.tool_call_hook import mark_tool_call_dispatched
from ..errors import build_tool_error, classify_error, error_message_from_result, is_error_result
from ..types import AmplitudeClientLike
from ..utils.logger import get_logger
from .events import emit_tool_call_response

__all__ = ["InstrumentToolDependencies", "instrument_tool"]


@dataclass
class InstrumentToolDependencies:
    """Dependencies the standalone :func:`instrument_tool` factory needs from
    the client.

    @internal Not part of the public package surface — construct a client and
    use ``AmplitudeMCPAnalytics.instrument_tool(handler, meta)`` instead.
    """

    amplitude: AmplitudeClientLike

    get_server_ctx: Callable[[], McpServerContext | None]
    """The server scope's ctx bound by ``instrument_server``, or ``None`` when
    it was never called. Resolved on *every* invocation (not captured at wrap
    time) because the binding is late and mutable: the scope is created when
    the wrapped ``run()`` starts — which happens *after* tools are wrapped —
    and is mutated again at the handshake (``clientInfo``). When this returns
    ``None``, the wrapper is a no-op passthrough — it does NOT fabricate a
    floor and emit."""

    track_tool_calls: bool = True
    """Whether to emit the default ``[MCP] Tool Call Response`` event. When
    ``False``, the wrapper still builds and runs under ``ctx`` (so custom
    events and ``set_identity`` work), but emits no default event."""

    get_server_identity: Callable[[], ServerIdentity | None] | None = None
    """Resolved per invocation, like ``get_server_ctx`` — the identity comes
    from the dispatching server's binding (or the last-connected fallback),
    not from whatever happened to be bound at wrap time."""

    get_scope_session_id: Callable[[], str | None] | None = None
    """The dispatching run's captured session id (streamable HTTP), when any."""

    get_client_info_resolver: Callable[[], ClientInfoResolver | None] | None = None
    """The resolver owned by the dispatching server binding."""

    resolve_identity: IdentityResolver | None = None

    sanitize_error_message: ErrorMessageSanitizer | None = None
    """Rewrites/drops ``[MCP] Error Message``, from ``config.sanitize_error_message``."""

    logger: logging.Logger | None = None


def _is_async_callable(obj: Any) -> bool:
    """FastMCP-compatible coroutine-function check: unwrap ``functools.partial``
    and ``__wrapped__`` chains, and honor async ``__call__`` on instances."""
    while isinstance(obj, functools.partial):
        obj = obj.func
    unwrapped = inspect.unwrap(obj)
    if inspect.iscoroutinefunction(unwrapped):
        return True
    # Callable instance with an async __call__ (mirrors FastMCP's check).
    call = getattr(unwrapped, "__call__", None)  # noqa: B004
    return inspect.iscoroutinefunction(call)


def instrument_tool(
    deps: InstrumentToolDependencies,
    handler: Callable[..., Any],
    meta: McpToolMeta,
) -> Callable[..., Any]:
    """Build the wrapped handler for an MCP tool. The returned function has the
    same signature and sync/async nature as the handler you pass in, so it
    drops into ``@mcp.tool()`` (as the inner decorator) or the low-level
    ``call_tool`` slot.

    Exposed as a standalone factory (separate from the client method) so it can
    be reused without constructing a full client.

    @internal Not part of the public package surface. Consumers instrument
    tools via ``AmplitudeMCPAnalytics.instrument_tool(handler, meta)``.
    """
    warned_unbound = False

    def _begin() -> tuple[McpToolContext | None, float]:
        nonlocal warned_unbound
        # This request reached a tool callback, so the `tools/call` rejection
        # hook must never emit for it — `[MCP] Tool Call Response` owns
        # dispatched calls. Marked unconditionally, before any early return.
        mark_tool_call_dispatched()

        server_ctx = deps.get_server_ctx()

        # No instrument_server() binding → analytics is off: run the original
        # handler untouched, emit nothing, and establish no ambient context.
        # Warn once per tool so a missing instrument_server() doesn't silently
        # drop every event.
        if server_ctx is None:
            if not warned_unbound:
                warned_unbound = True
                get_logger().warning(
                    "AmplitudeMCPAnalytics: instrument_tool('%s') ran without "
                    "instrument_server(); analytics is disabled for this tool. Call "
                    "instrument_server(server) before running it to enable tracking.",
                    meta.name,
                )
            return None, 0.0

        start = time.perf_counter()
        ctx = build_tool_context(
            server_ctx,
            meta,
            scope_session_id=(
                deps.get_scope_session_id() if deps.get_scope_session_id is not None else None
            ),
            resolve_identity=deps.resolve_identity,
            resolve_client_info=(
                deps.get_client_info_resolver()
                if deps.get_client_info_resolver is not None
                else None
            ),
            server_identity=(
                deps.get_server_identity() if deps.get_server_identity is not None else None
            ),
            logger=deps.logger,
        )
        return ctx, start

    def _record(
        ctx: McpToolContext,
        start: float,
        call_args: tuple[Any, ...],
        call_kwargs: dict[str, Any],
        *,
        thrown: Any = None,
        returned: Any = None,
        raised: bool,
    ) -> None:
        """The single point every tool call funnels through: resolve the call
        status, classify any error onto ``ctx.error``, then emit the default
        event unless tracking is off. A failure is a raised exception
        (classified via ``classify_error``) or a returned result carrying
        ``isError: true`` (classified as ``returned_error``)."""
        duration_ms = (time.perf_counter() - start) * 1000
        is_tool_error = False

        if raised:
            ctx.error = classify_error(thrown)
            is_tool_error = True
        elif is_error_result(returned):
            if ctx.error is None:
                # Preserve a pre-existing error context (e.g. constructed by
                # `analytics.tool_error(ctx, ...)`).
                message = error_message_from_result(returned)
                ctx.error = build_tool_error(
                    code="returned_error",
                    message=message if message is not None else "Tool returned an error result",
                )
            is_tool_error = True

        if not deps.track_tool_calls:
            return

        request_payload: Any = None
        if call_kwargs:
            request_payload = call_kwargs
        elif call_args:
            request_payload = call_args[0]

        # `payload_byte_size`, not plain `byte_size`: handler arguments and
        # returns are not always plain data. A low-level handler may return a
        # pydantic `CallToolResult` (the shape `is_error_result` already
        # supports) and a FastMCP tool may be handed a validated pydantic
        # model argument — `json.dumps` refuses both, silently dropping the
        # size property that the documented contract says is measured via
        # `model_dump_json`.
        emit_tool_call_response(
            deps.amplitude,
            ctx,
            is_tool_error=is_tool_error,
            duration_ms=duration_ms,
            request_size_bytes=payload_byte_size(request_payload),
            response_size_bytes=payload_byte_size(returned) if not raised else None,
            sanitize=deps.sanitize_error_message,
        )

    if _is_async_callable(handler):

        @functools.wraps(handler)
        async def async_wrapped(*call_args: Any, **call_kwargs: Any) -> Any:
            ctx, start = _begin()
            if ctx is None:
                return await handler(*call_args, **call_kwargs)
            token = _context_var.set(ctx)
            try:
                result = await handler(*call_args, **call_kwargs)
            except BaseException as err:
                _record(ctx, start, call_args, call_kwargs, thrown=err, raised=True)
                raise
            finally:
                _context_var.reset(token)
            _record(ctx, start, call_args, call_kwargs, returned=result, raised=False)
            return result

        return async_wrapped

    @functools.wraps(handler)
    def sync_wrapped(*call_args: Any, **call_kwargs: Any) -> Any:
        ctx, start = _begin()
        if ctx is None:
            return handler(*call_args, **call_kwargs)
        token = _context_var.set(ctx)
        try:
            result = handler(*call_args, **call_kwargs)
        except BaseException as err:
            _record(ctx, start, call_args, call_kwargs, thrown=err, raised=True)
            raise
        finally:
            _context_var.reset(token)
        _record(ctx, start, call_args, call_kwargs, returned=result, raised=False)
        return result

    return sync_wrapped
