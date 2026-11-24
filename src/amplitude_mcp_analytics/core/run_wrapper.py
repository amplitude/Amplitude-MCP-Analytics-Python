"""The per-connection seam: wraps ``Server.run`` (there is no
``connect(transport)`` in the Python SDK — one ``run()`` call is one
connection/session, and the transport exists only as the message streams).

This wrapper does three jobs:

1. **Scope**: create a fresh :class:`ServerScope` per run and set it into the
   scope ContextVar around the delegated ``run`` — anyio task groups copy the
   caller's context at ``start_soon``, so every handler task inherits it.
2. **Handshake capture** via stream proxies: the read side captures the
   ``initialize`` request and the write side reports it only after the matching
   successful response is sent. Python's ``ServerSession`` consumes initialize
   internally, before the public server request-handler registry.
3. **Session end**: ``run()`` returning or raising is the transport closing —
   emit ``[MCP] Session Ended`` from the ``finally`` when a session was
   initialized and the transport outlives one request. Sessionless runs still
   report their real initialize handshake, but never fabricate an end duration
   for a one-request transport.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from types import SimpleNamespace
from typing import Any

from ..context.client_attribution import client_string, record_handshake
from .build_context import resolve_transport_evidence
from .server_scope import ServerScope, scope_var

__all__ = ["install_run_wrapper"]

_RUN_WRAPPED_ATTR = "_amplitude_mcp_run_wrapped"


class _ReadStreamProxy:
    """Delegating wrapper over the anyio receive stream the session consumes.
    Covers the duck surface ``ServerSession`` uses (``async with``,
    ``async for`` / ``receive``, ``aclose``); anything else falls through via
    ``__getattr__``."""

    def __init__(self, inner: Any, observer: Callable[[Any], None]) -> None:
        self._inner = inner
        self._observer = observer

    def _observe(self, item: Any) -> None:
        try:
            self._observer(item)
        except Exception:
            # Telemetry is best-effort — never break message delivery.
            pass

    async def __aenter__(self) -> _ReadStreamProxy:
        await self._inner.__aenter__()
        return self

    async def __aexit__(self, *exc_info: Any) -> Any:
        return await self._inner.__aexit__(*exc_info)

    def __aiter__(self) -> _ReadStreamProxy:
        return self

    async def __anext__(self) -> Any:
        item = await self._inner.__anext__()
        self._observe(item)
        return item

    async def receive(self) -> Any:
        item = await self._inner.receive()
        self._observe(item)
        return item

    async def aclose(self) -> None:
        await self._inner.aclose()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class _WriteStreamProxy:
    """Delegating send stream that observes messages after a successful send."""

    def __init__(self, inner: Any, observer: Callable[[Any], None]) -> None:
        self._inner = inner
        self._observer = observer

    async def __aenter__(self) -> _WriteStreamProxy:
        await self._inner.__aenter__()
        return self

    async def __aexit__(self, *exc_info: Any) -> Any:
        return await self._inner.__aexit__(*exc_info)

    async def send(self, item: Any) -> None:
        await self._inner.send(item)
        try:
            self._observer(item)
        except Exception:
            pass

    def send_nowait(self, item: Any) -> None:
        self._inner.send_nowait(item)
        try:
            self._observer(item)
        except Exception:
            pass

    async def aclose(self) -> None:
        await self._inner.aclose()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class _HandshakeObserver:
    """Inspects each inbound ``SessionMessage`` for the scope's handshake and
    transport facts. All reads are hasattr-guarded — message shapes are the
    wire contract, but the metadata request object is typed ``object``
    upstream."""

    def __init__(
        self,
        scope: ServerScope,
        on_initialized: Callable[[ServerScope, Any], None],
    ) -> None:
        self._scope = scope
        self._on_initialized = on_initialized
        self._pending: dict[Any, tuple[Any, SimpleNamespace]] = {}

    def observe(self, item: Any) -> None:
        message = getattr(item, "message", None)
        root = getattr(message, "root", None)
        if root is None:
            return
        scope = self._scope
        metadata = getattr(item, "metadata", None)
        request = getattr(metadata, "request_context", None)

        # Transport evidence: first HTTP request seen fixes the transport
        # (stdio remains the default when no message ever carries one).
        if not scope.transport_resolved:
            evidence = resolve_transport_evidence(request)
            if evidence is not None:
                if scope.ctx is not None:
                    scope.ctx.transport = evidence  # type: ignore[assignment]
                scope.transport_resolved = True

        # Session id: the streamable-http initialize POST carries no
        # mcp-session-id header (the server mints the id in its response);
        # every later POST — including notifications/initialized — does.
        headers = getattr(request, "headers", None)
        if headers is not None and scope.captured_session_id is None:
            try:
                session_id = headers.get("mcp-session-id")
            except Exception:
                session_id = None
            if isinstance(session_id, str) and session_id != "":
                scope.captured_session_id = session_id

        method = getattr(root, "method", None)
        if method == "initialize":
            self._capture_initialize(root, request)

    def observe_outbound(self, item: Any) -> None:
        """Emit after the matching initialize response was sent successfully."""
        message = getattr(item, "message", None)
        root = getattr(message, "root", None)
        response_id = getattr(root, "id", None)
        pending = self._pending.pop(response_id, None)
        result = getattr(root, "result", None)
        if pending is None or not isinstance(result, Mapping) or self._scope.ctx is None:
            return
        client_info, request_context = pending
        record_handshake(self._scope.ctx, client_info, client_string(result.get("protocolVersion")))
        self._on_initialized(self._scope, request_context)

    def _capture_initialize(self, root: Any, request: Any) -> None:
        """Stage request evidence until the matching initialize succeeds."""
        params = getattr(root, "params", None)
        if not isinstance(params, dict):
            return
        request_id = getattr(root, "id", None)
        if request_id is not None:
            self._pending[request_id] = (
                params.get("clientInfo"),
                SimpleNamespace(request=request, meta=params.get("_meta")),
            )


def install_run_wrapper(
    low_level_server: Any,
    *,
    make_scope: Callable[[], ServerScope],
    install_hooks: Callable[[], None],
    on_initialized: Callable[[ServerScope, Any], None],
    on_ended: Callable[[ServerScope, float], None],
) -> None:
    """Replace ``low_level_server.run`` with the instrumented wrapper.
    Idempotent — a second install is a no-op.

    - ``make_scope`` builds a fresh per-run scope (from the binding's opts).
    - ``install_hooks`` (re-)asserts the request-handler surgery; called on
      every run so handlers registered after ``instrument_server`` are caught.
    - ``on_initialized`` fires after the successful initialize response.
    - ``on_ended`` fires when the run settles with an initialized session,
      with the session duration in milliseconds.
    """
    if getattr(low_level_server.run, _RUN_WRAPPED_ATTR, False):
        return
    original_run = low_level_server.run

    async def wrapped_run(
        read_stream: Any,
        write_stream: Any,
        initialization_options: Any,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        scope = make_scope()
        install_hooks()

        # Each stateless run handles one request, so an initialize POST can
        # report its handshake, but the run teardown does not end a session.
        stateless = bool(kwargs.get("stateless", args[1] if len(args) >= 2 else False))
        scope.transport_persists = not stateless
        observer = _HandshakeObserver(scope, on_initialized)
        proxied_read = _ReadStreamProxy(read_stream, observer.observe)
        proxied_write = _WriteStreamProxy(write_stream, observer.observe_outbound)

        token = scope_var().set(scope)
        try:
            return await original_run(
                proxied_read, proxied_write, initialization_options, *args, **kwargs
            )
        finally:
            scope_var().reset(token)
            if (
                scope.transport_persists
                and scope.session_start is not None
                and scope.ctx is not None
            ):
                duration_ms = (time.perf_counter() - scope.session_start) * 1000
                scope.session_start = None
                try:
                    on_ended(scope, duration_ms)
                except Exception:
                    # Best-effort telemetry — never mask the run's own outcome.
                    pass

    setattr(wrapped_run, _RUN_WRAPPED_ATTR, True)
    low_level_server.run = wrapped_run
