"""Intercept the MCP server's ``tools/call`` request handler so the SDK can
emit ``[MCP] Tool Call Rejected`` for requests that fail before any tool
callback runs — an unknown tool name or input-schema validation — where the
client receives an error instead of a tool result.

Mirrors ``tools_list_hook.py``: FastMCP registers a single ``tools/call``
handler on the low-level ``Server`` at construction, so wrapping it once (at
the first instrumented ``run``) observes every call.

Dispatched calls are excluded via :func:`mark_tool_call_dispatched` /
:func:`was_tool_call_dispatched`: ``instrument_tool`` marks every dispatch it
sees, and the emit site skips marked requests, so a request never lands on
both ``[MCP] Tool Call Response`` and ``[MCP] Tool Call Rejected``.

De-dup mechanism: Python's ``RequestContext`` is an eq-comparing dataclass
(unhashable), so it cannot key a set or dict directly. Instead the hook plants
a fresh **mutable marker object** in a ContextVar around each delegated call.
``instrument_tool`` retrieves the marker from the (copied) context and mutates
it — mutation, not ``ContextVar.set``, so the flag survives ``anyio.to_thread``
and nested task groups, whose context copies would strand a ``set()``.
Per-request isolation holds because the hook's frame is ancestral to
everything the handler spawns.
"""

from __future__ import annotations

import functools
import time
from collections.abc import Callable
from contextvars import ContextVar
from typing import Any

__all__ = [
    "DispatchMarker",
    "install_tool_call_hook",
    "mark_tool_call_dispatched",
    "was_tool_call_dispatched",
]

_WRAPPED_ATTR = "_amplitude_mcp_tool_call_wrapped"


class DispatchMarker:
    """Mutable per-``tools/call`` flag: did the request reach an
    ``instrument_tool``-wrapped callback? @internal"""

    __slots__ = ("dispatched",)

    def __init__(self) -> None:
        self.dispatched = False


_dispatch_marker: ContextVar[DispatchMarker | None] = ContextVar(
    "amplitude_mcp_analytics_dispatch_marker", default=None
)


def mark_tool_call_dispatched() -> None:
    """Record that the current ``tools/call`` request reached a tool callback.
    No-op outside an instrumented ``tools/call`` frame. @internal"""
    marker = _dispatch_marker.get()
    if marker is not None:
        marker.dispatched = True


def was_tool_call_dispatched(marker: DispatchMarker | None) -> bool:
    """Whether the request owning ``marker`` reached a tool callback. @internal"""
    return marker is not None and marker.dispatched


def install_tool_call_hook(
    server: Any,
    on_settled: Callable[[dict[str, Any]], None],
) -> None:
    """Wrap the server's registered ``tools/call`` handler so each request
    reports its outcome (success or failure) to ``on_settled``. The handler's
    behavior is unchanged — its result/raise pass through untouched.
    Best-effort and idempotent: a no-op if the SDK shape is unrecognized or no
    ``tools/call`` handler is registered yet.

    ``on_settled`` receives a dict with ``tool_name``, ``result`` / ``error``,
    ``duration_ms``, and the request's ``marker`` (for the dispatch check).

    @internal
    """
    from .mcp import mcp_types

    handlers = getattr(server, "request_handlers", None)
    if not isinstance(handlers, dict):
        return
    request_type = mcp_types().CallToolRequest

    original = handlers.get(request_type)
    if original is None or getattr(original, _WRAPPED_ATTR, False):
        return

    @functools.wraps(original)
    async def wrapped(req: Any, *args: Any, **kwargs: Any) -> Any:
        start = time.perf_counter()
        name = getattr(getattr(req, "params", None), "name", None)
        tool_name = name if isinstance(name, str) else None
        marker = DispatchMarker()
        token = _dispatch_marker.set(marker)

        def report(*, result: Any = None, error: Any = None) -> None:
            try:
                on_settled(
                    {
                        "tool_name": tool_name,
                        "result": result,
                        "error": error,
                        "duration_ms": (time.perf_counter() - start) * 1000,
                        "marker": marker,
                    }
                )
            except Exception:
                # Telemetry is best-effort — never break the tools/call response.
                pass

        try:
            result = await original(req, *args, **kwargs)
        except BaseException as error:
            report(error=error)
            raise
        finally:
            _dispatch_marker.reset(token)
        report(result=result)
        return result

    setattr(wrapped, _WRAPPED_ATTR, True)
    handlers[request_type] = wrapped
