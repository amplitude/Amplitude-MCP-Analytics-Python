"""Ambient access to the current MCP context via :mod:`contextvars`.

Threading model: explicit ``ctx`` is the public contract — the SDK's
tool-instrumentation API injects it and the custom-event / error APIs take it
as an argument. This ambient accessor is an optional *convenience* for emit
sites deep in a call stack that can't easily thread ``ctx``, or for hosts that
already use context variables. It is NOT the contract — prefer explicit ``ctx``.

Ambient is the fallback, not the default: the store is lost across boundaries
that escape the :func:`run_with_context` scope, is harder to test, and has
serverless pitfalls. When in doubt, pass ``ctx``.

(Ports the Node SDK's ``src/context/als.ts``; ``AsyncLocalStorage`` becomes a
``ContextVar``.)
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import replace
from typing import Any, TypeVar

from .types import McpRequestInfo, McpServerContext, McpToolContext, SetIdentityInput

__all__ = ["get_current_context", "run_with_context", "set_identity", "set_rationale"]

T = TypeVar("T")

_current_context: ContextVar[McpServerContext | None] = ContextVar(
    "amplitude_mcp_analytics_context", default=None
)

# Upper bound on the stored rationale. Rationale is LLM-generated free text;
# the cap keeps a runaway agent from bloating every event on the call.
_RATIONALE_MAX_LENGTH = 1000


def run_with_context(ctx: McpServerContext, fn: Callable[[], T]) -> T:
    """Run ``fn`` with ``ctx`` as the ambient context, including any async work
    awaited within an ``async def`` ``fn``. Returns whatever ``fn`` returns
    (for an ``async def`` ``fn``, a coroutine that manages the scope itself).
    A tool-scope context is accepted (it extends the server scope)."""
    if inspect.iscoroutinefunction(fn):

        async def _run_async() -> Any:
            token = _current_context.set(ctx)
            try:
                return await fn()  # type: ignore[misc]
            finally:
                _current_context.reset(token)

        return _run_async()  # type: ignore[return-value]

    token = _current_context.set(ctx)
    try:
        return fn()
    finally:
        _current_context.reset(token)


def get_current_context() -> McpServerContext | None:
    """The ambient context for the current scope, or ``None`` outside any
    :func:`run_with_context` scope. Surfaces the server scope; for tool-scope
    fields, prefer the explicitly-passed ``ctx``."""
    return _current_context.get()


def set_identity(input: SetIdentityInput) -> None:
    """Set or override the identity on the current request's ambient context.

    Must be called inside a :func:`run_with_context` scope (e.g. inside an
    instrumented tool handler). Raises :class:`RuntimeError` if called outside
    a context scope.

    This is the primary integration point for consumers who resolve identity in
    custom auth middleware or inside the handler itself.
    """
    ctx = _current_context.get()
    if ctx is None:
        raise RuntimeError(
            "set_identity() called outside an active context scope. "
            "Call it inside an instrumented tool handler or a run_with_context() block."
        )

    if input.user_id is not None or input.device_id is not None:
        updates: dict[str, Any] = {"resolved_from": "explicit"}
        if input.user_id is not None:
            updates["user_id"] = input.user_id
        if input.device_id is not None:
            updates["device_id"] = input.device_id
        ctx.identity = replace(ctx.identity, **updates)

    if input.tenant is not None:
        ctx.tenant = input.tenant


def set_rationale(rationale: str) -> None:
    """Set the rationale ("why the agent called this tool") for the current
    tool invocation. Must be called inside a :func:`run_with_context` scope —
    in practice, anywhere inside an instrumented tool handler, at any call
    depth. Raises :class:`RuntimeError` outside a context scope (same contract
    as :func:`set_identity`).

    The value is emitted as the reserved ``[MCP] Rationale`` property on the
    default ``[MCP] Tool Call Response`` event and on every tool-scope custom
    event of the same invocation. The SDK never reads rationale out of tool
    inputs itself — where it lives (a tool argument, ``_meta``, a header, a
    derived value) is the host's convention, and rationale is content-bearing
    free text, so emitting it is an explicit host opt-in.

    Truncated to 1000 characters. Last write wins if called more than once.
    Non-string or empty values are ignored.

    Example::

        @mcp.tool()
        @analytics.instrument_tool(name="search")
        async def search(query: str, rationale: str | None = None) -> str:
            if isinstance(rationale, str):
                set_rationale(rationale)
            return do_search(query)
    """
    ctx = _current_context.get()
    if ctx is None:
        raise RuntimeError(
            "set_rationale() called outside an active context scope. "
            "Call it inside an instrumented tool handler or a run_with_context() block."
        )

    # Runtime guard kept for untyped callers (Node parity: non-string or empty
    # values are ignored).
    if not isinstance(rationale, str) or len(rationale) == 0:  # pyright: ignore[reportUnnecessaryIsInstance]
        return

    existing: McpRequestInfo | None = getattr(ctx, "request", None)
    truncated = rationale[:_RATIONALE_MAX_LENGTH]
    if existing is not None:
        existing.rationale = truncated
    elif isinstance(ctx, McpToolContext):
        ctx.request = McpRequestInfo(rationale=truncated)
    else:
        # A server-scope ctx has no `request` field; attach one dynamically to
        # preserve the Node semantics (the ambient object simply gains the
        # value — it is only ever read from tool-scope lowering).
        setattr(ctx, "request", McpRequestInfo(rationale=truncated))  # noqa: B010


# Alias for internal wrappers that need direct token control (the
# instrumented-tool wrapper sets/resets around the handler inside its own
# task, where a callback-scoped runner cannot preserve async shape).
_context_var: ContextVar[McpServerContext | None] = _current_context
