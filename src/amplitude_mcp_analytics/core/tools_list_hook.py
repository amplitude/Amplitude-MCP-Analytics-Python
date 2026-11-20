"""Intercept the MCP server's ``tools/list`` request handler so the SDK can
emit ``[MCP] Tools Listed`` without the consumer changing anything.

FastMCP registers a single ``tools/list`` handler on the low-level ``Server``
at construction; that closure enumerates the *live* tool set at call time, so
wrapping it once keeps counting correctly even as tools are added or removed
later.

Python-SDK trap: the low-level server invokes this handler **internally with
``req=None``** to refresh its tool cache during ``tools/call`` dispatch
(``_get_cached_tool_definition``). Those internal calls must not emit phantom
``[MCP] Tools Listed`` events — the wrapper delegates them untouched.
"""

from __future__ import annotations

import functools
import time
from collections.abc import Awaitable, Callable
from typing import Any

__all__ = ["install_tools_list_hook"]

_WRAPPED_ATTR = "_amplitude_mcp_tools_list_wrapped"


async def observe_tools_list(
    call: Callable[[], Awaitable[Any]],
    on_listed: Callable[[dict[str, Any]], None],
) -> Any:
    """Observe a tools/list outcome without protocol request types. @internal"""
    start = time.perf_counter()

    def report(result: Any = None, error: Any = None) -> None:
        try:
            on_listed({"result": result, "error": error,
                       "duration_ms": (time.perf_counter() - start) * 1000})
        except Exception:
            pass

    try:
        result = await call()
    except BaseException as error:
        report(error=error)
        raise
    report(result=result)
    return result


def install_tools_list_hook(
    server: Any,
    on_listed: Callable[[dict[str, Any]], None],
) -> None:
    """Wrap the server's registered ``tools/list`` handler so each request
    reports its outcome (success or failure) to ``on_listed``. The handler's
    behavior is unchanged — its result/raise pass through untouched.
    Best-effort and idempotent: a no-op if the SDK shape is unrecognized or no
    ``tools/list`` handler is registered yet.

    ``on_listed`` receives a dict with ``result`` / ``error`` and
    ``duration_ms``.

    @internal
    """
    from .mcp import mcp_types

    handlers = getattr(server, "request_handlers", None)
    if not isinstance(handlers, dict):
        return
    request_type = mcp_types().ListToolsRequest

    original = handlers.get(request_type)
    if original is None or getattr(original, _WRAPPED_ATTR, False):
        return

    @functools.wraps(original)
    async def wrapped(req: Any, *args: Any, **kwargs: Any) -> Any:
        # Internal tool-cache refresh (req is None) — not a client request.
        if req is None:
            return await original(req, *args, **kwargs)

        async def call() -> Any:
            return await original(req, *args, **kwargs)

        return await observe_tools_list(call, on_listed)

    setattr(wrapped, _WRAPPED_ATTR, True)
    handlers[request_type] = wrapped
