"""Per-instrumented-server, per-run analytics scope.

Python needs no per-handler machinery: the wrapped ``Server.run`` sets a single
ContextVar before delegating, and because anyio's task group copies the
caller's context at ``start_soon``, every handler task the SDK spawns inherits
the scope — one frame per connection/run. Concurrent ``run()`` calls on one
shared ``Server`` object (stateless HTTP) each see their own scope, so
per-request servers sharing one object stay correctly isolated too.
"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass, field

from ..context.types import IdentityResolver, McpServerContext
from .identity import ServerIdentity

__all__ = ["ServerScope", "current_server_scope", "scope_var"]


@dataclass
class ServerScope:
    """Analytics state owned by one ``instrument_server`` binding's run. @internal"""

    ctx: McpServerContext | None = None
    """The server-scope ctx, created when the wrapped ``run`` starts and
    resolved into its connection form at the handshake."""

    identity: ServerIdentity | None = None
    """Identity from ``instrument_server`` opts — per-binding safe."""

    identity_resolver: IdentityResolver | None = None
    """Per-request identity resolver from ``instrument_server``."""

    session_start: float | None = None
    """Handshake timestamp (``time.perf_counter()`` seconds) — doubles as the
    "a session is active" flag for THIS run's transport."""

    captured_session_id: str | None = None
    """Session id observed on the wire (``mcp-session-id`` header) — the
    streamable-http initialize POST carries none (the server mints the id in
    its response), so it is captured from the ``notifications/initialized``
    POST instead."""

    transport_resolved: bool = False
    """True once transport evidence (or an explicit override) fixed
    ``ctx.transport`` — later messages no longer flip it."""

    extra_state: dict[str, object] = field(default_factory=dict)
    """Free slot for adapter bookkeeping."""


_current_scope: ContextVar[ServerScope | None] = ContextVar(
    "amplitude_mcp_analytics_server_scope", default=None
)


def current_server_scope() -> ServerScope | None:
    """The ambient server scope, or ``None`` outside a run frame. @internal"""
    return _current_scope.get()


def scope_var() -> ContextVar[ServerScope | None]:
    """The scope ContextVar, for the run wrapper's set/reset. @internal"""
    return _current_scope
