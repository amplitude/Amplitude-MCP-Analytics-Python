"""Main Amplitude MCP Analytics client for tracking MCP server activity.

Construct with either an API key (which initializes a fresh
``amplitude-analytics`` client under the hood) or a pre-built Amplitude client
owned by the host application. The server identity (``server_name``,
``server_version``) is required and is attached to every emitted event.
"""

from __future__ import annotations

import importlib
import time
from collections.abc import Callable, Mapping
from typing import Any

from .config import MCPAnalyticsConfig
from .context.factory import create_server_context
from .context.types import (
    IdentityResolver,
    McpAnchor,
    McpClientInfo,
    McpServerContext,
    McpServerInfo,
    McpTenant,
    McpToolContext,
    McpToolMeta,
    McpTransport,
    SetIdentityInput,
)
from .context.vars import set_identity as _set_identity_on_ctx
from .context.vars import set_rationale as _set_rationale_on_ctx
from .core.build_context import build_server_context
from .core.delivery import (
    DeliveryClient,
    register_exit_hook,
    settle_unflushed_count,
)
from .core.identity import ServerIdentity
from .core.mcp import current_request_context, lookup_registered_tool, unwrap_server
from .core.run_wrapper import install_run_wrapper
from .core.serialize import byte_size, result_byte_size
from .core.server_scope import ServerScope, current_server_scope
from .core.tool_call_hook import install_tool_call_hook, was_tool_call_dispatched
from .core.tool_call_rejection import classify_pre_dispatch_rejection
from .core.tools_list_hook import install_tools_list_hook
from .errors import build_tool_error, classify_error, tool_error_result
from .exceptions import ConfigurationError
from .tracking.events import (
    emit_session_ended,
    emit_session_initialized,
    emit_tool_call_rejected,
    emit_tools_listed,
)
from .tracking.instrument_tool import (
    InstrumentToolDependencies,
)
from .tracking.instrument_tool import (
    instrument_tool as _instrument_tool_factory,
)
from .tracking.track import track_server_event as _track_server_event
from .tracking.track import track_tool_event as _track_tool_event
from .tracking.types import TrackEventOptions
from .types import AmplitudeClientLike, AmplitudeEvent
from .utils.logger import get_logger

__all__ = ["AmplitudeMCPAnalytics", "create_mcp_analytics"]

# Marks a server whose `run` we've already wrapped, to stay idempotent.
_INSTRUMENTED_ATTR = "_amplitude_mcp_instrumented"


class AmplitudeMCPAnalytics:
    """Amplitude analytics client for MCP servers.

    Example (API key)::

        analytics = AmplitudeMCPAnalytics(
            api_key=os.environ["AMPLITUDE_API_KEY"],
            server_name="my-mcp-server",
            server_version="1.0.0",
        )

    Example (reusing an existing Amplitude client)::

        from amplitude import Amplitude

        client = Amplitude("YOUR_KEY")
        analytics = AmplitudeMCPAnalytics(
            amplitude=client,
            server_name="my-mcp-server",
            server_version="1.0.0",
        )
    """

    def __init__(
        self,
        *,
        server_name: str,
        server_version: str,
        amplitude: AmplitudeClientLike | None = None,
        api_key: str | None = None,
        config: MCPAnalyticsConfig | None = None,
    ) -> None:
        if not server_name:
            raise ConfigurationError("AmplitudeMCPAnalytics: server_name is required")
        if not server_version:
            raise ConfigurationError("AmplitudeMCPAnalytics: server_version is required")
        if amplitude is not None and api_key is not None:
            raise ConfigurationError(
                "Provide either 'amplitude' or 'api_key'. If you are already using an "
                "Amplitude client, pass it via the 'amplitude' option. Only pass the "
                "api key if you want to initialize a new Amplitude client with a "
                "different api key."
            )

        raw_amplitude: AmplitudeClientLike
        if amplitude is not None:
            raw_amplitude = amplitude
            self._owns_client = False
        elif api_key is not None:
            try:
                amplitude_module = importlib.import_module("amplitude")
            except ImportError as err:
                raise ConfigurationError(
                    "amplitude-analytics is required for the api_key path. Install it "
                    "via the extra: uv add 'amplitude-mcp-analytics[amplitude]' (or "
                    "pass a pre-initialized client via the 'amplitude' option)."
                ) from err
            raw_amplitude = amplitude_module.Amplitude(api_key)
            self._owns_client = True
        else:
            raise ConfigurationError(
                "AmplitudeMCPAnalytics: provide either an existing Amplitude instance "
                "via 'amplitude' or an API key via 'api_key'."
            )

        #: Logical name of the MCP server being instrumented.
        self.server_name = server_name
        #: Version string of the MCP server being instrumented.
        self.server_version = server_version
        self.config = config if config is not None else MCPAnalyticsConfig()

        # Wrap the raw client with the delivery hooks. Ordering is load-bearing
        # (see core/delivery.py): the dry-run gate sits outermost so dry-run
        # events are never counted as unflushed.
        self._amplitude: AmplitudeClientLike = DeliveryClient(
            raw_amplitude, self.config, on_tracked=self._on_tracked
        )
        register_exit_hook()

        # Events tracked since the last flush()/shutdown(); drives the
        # serverless exit warning.
        self._track_count_since_flush = 0

        # LAST-connected server scope — the fallback when a call runs outside a
        # dispatch frame (see core/server_scope.py). The authoritative
        # per-server state lives on each binding's ServerScope; these fields
        # only mirror the most recent one so single-server hosts and direct
        # handler invocation keep working. When unset, instrument_tool is a
        # no-op passthrough (warns once) and the handler runs untouched.
        self._server_ctx: McpServerContext | None = None
        # Identity from the most recent instrument_server(server, ...) — same
        # fallback role as _server_ctx.
        self._server_identity: ServerIdentity | None = None

    def _on_tracked(self) -> None:
        self._track_count_since_flush += 1

    def track(self, event: AmplitudeEvent) -> None:
        """Low-level passthrough to the underlying Amplitude client. @internal"""
        self._amplitude.track(event)

    def set_identity(self, input: SetIdentityInput) -> None:
        """Set or override the subject identity on the current request's
        context. Must be called inside an instrumented tool handler (or a
        ``run_with_context`` block). This is the first step of the fallback
        chain and wins over all other identity sources.

        Example (inside a tool handler)::

            analytics.set_identity(SetIdentityInput(
                user_id=my_auth.get_login_id(),
                tenant=McpTenant(group_type="org id", group_value=my_auth.get_org_id()),
            ))
        """
        _set_identity_on_ctx(input)

    def set_rationale(self, rationale: str) -> None:
        """Set the rationale ("why the agent called this tool") for the current
        tool invocation. Must be called inside an instrumented tool handler (or
        a ``run_with_context`` block), at any call depth. Emitted as the
        reserved ``[MCP] Rationale`` property on the default ``[MCP] Tool Call
        Response`` event and on every tool-scope custom event of the same
        invocation.

        The SDK never reads rationale out of tool inputs itself — where it
        lives (a tool argument, ``_meta``, a header, a derived value) is your
        convention, and rationale is content-bearing free text, so emitting it
        is an explicit opt-in. Truncated to 1000 characters; last write wins.
        """
        _set_rationale_on_ctx(rationale)

    def tool_error(
        self,
        ctx: McpToolContext | None,
        *,
        code: str,
        message: str,
        correction_message: str | None = None,
        recoverable: bool | None = None,
        retry_suggested: bool | None = None,
        http_status: int | None = None,
        fingerprint: str | None = None,
    ) -> dict[str, Any]:
        """Build a structured MCP error response and store the error on ``ctx``
        for telemetry. Returns a valid ``CallToolResult``-shaped dict with
        ``isError: true`` and a client-facing message that includes the
        correction guidance.

        When ``ctx`` is missing (e.g. called outside an instrumented handler,
        or ``get_current_context()`` returned ``None``), still returns the MCP
        error result for the client, but skips telemetry attachment and warns —
        never raises.

        Example::

            return analytics.tool_error(
                ctx,
                code="missing_chart_id",
                message="No chart ID was provided.",
                correction_message="Search for a chart first, then retry with the chart ID.",
                recoverable=True,
            )
        """
        error = build_tool_error(
            code=code,
            message=message,
            correction_message=correction_message,
            recoverable=recoverable,
            retry_suggested=retry_suggested,
            http_status=http_status,
            fingerprint=fingerprint,
        )
        if ctx is None:
            get_logger(self._amplitude).warning(
                "tool_error('%s') called without a tool context; returning the MCP "
                "error result but skipping telemetry. Call it inside an instrumented "
                "tool handler (or pass the wrapper's ctx).",
                code,
            )
            return tool_error_result(error)
        ctx.error = error
        return tool_error_result(error)

    def track_server_event(
        self,
        ctx: McpServerContext,
        event_name: str,
        properties: dict[str, Any] | None = None,
        options: TrackEventOptions | None = None,
    ) -> None:
        """Emit a server-scope custom event. Inherits identity/tenant/session/
        client/server/auth/transport from ``ctx``; caller-supplied properties
        win on collision. Drops silently when the event has neither an identity
        nor a tenant, and on underlying client failure — never raises."""
        _track_server_event(self._amplitude, ctx, event_name, properties, options)

    def track_tool_event(
        self,
        ctx: McpToolContext,
        event_name: str,
        properties: dict[str, Any] | None = None,
        options: TrackEventOptions | None = None,
    ) -> None:
        """Emit a tool-scope custom event. Same contract as
        :meth:`track_server_event` plus the tool metadata inherited from the
        tool-scope ``ctx``."""
        _track_tool_event(self._amplitude, ctx, event_name, properties, options)

    def instrument_tool(
        self,
        handler: Callable[..., Any] | None = None,
        meta: McpToolMeta | Mapping[str, Any] | None = None,
        *,
        name: str | None = None,
        owner: str | None = None,
        extra: dict[str, Any] | None = None,
        resolve_identity: IdentityResolver | None = None,
        **meta_fields: Any,
    ) -> Callable[..., Any]:
        """Instrument an MCP tool handler — the single tool-instrumentation
        entry point. Two spellings:

        Node-parity call::

            mcp.add_tool(analytics.instrument_tool(search, McpToolMeta(name="search")))

        Decorator factory (``@mcp.tool()`` must stay outermost — FastMCP builds
        the input schema from the wrapped function's signature, which
        ``functools.wraps`` preserves)::

            @mcp.tool()
            @analytics.instrument_tool(name="search", owner="docs-team")
            async def search(query: str) -> str: ...

        On each call it builds the per-request tool-scope ``ctx``
        (transport-aware anchor, ``_meta`` client info, protocol version) from
        the server scope bound by :meth:`instrument_server`, runs your
        **unchanged** handler with the ctx ambient (reachable via
        ``get_current_context()``; the ``track_*`` methods take it explicitly),
        emits a ``[MCP] Tool Call Response`` event (success + duration, or
        failure), and classifies raised errors onto ``ctx.error``. Errors are
        re-raised so the MCP SDK surfaces them; the best-effort guarantee
        applies only to emission.

        **Requires** :meth:`instrument_server`. If the server was never bound,
        this is a no-op passthrough: the original handler runs untouched and NO
        event is emitted (it also warns once). Analytics is opt-in via
        ``instrument_server``.
        """

        def wrap(fn: Callable[..., Any]) -> Callable[..., Any]:
            resolved_meta = self._resolve_tool_meta(fn, meta, name, owner, extra, meta_fields)
            deps = InstrumentToolDependencies(
                amplitude=self._amplitude,
                # Resolve the dispatching server's scope first (set per run by
                # instrument_server — see core/server_scope.py); fall back to
                # the last-connected scope ONLY for direct invocation outside a
                # dispatch frame. Inside a frame we trust the frame's own
                # values even when they are empty.
                get_server_ctx=lambda: (
                    scope.ctx
                    if (scope := current_server_scope()) is not None
                    else self._server_ctx
                ),
                get_server_identity=lambda: (
                    scope.identity
                    if (scope := current_server_scope()) is not None
                    else self._server_identity
                ),
                get_scope_session_id=lambda: (
                    scope.captured_session_id
                    if (scope := current_server_scope()) is not None
                    else None
                ),
                resolve_identity=resolve_identity,
                track_tool_calls=self.config.autocapture.tool_calls,
                sanitize_error_message=self.config.sanitize_error_message,
                logger=get_logger(self._amplitude),
            )
            return _instrument_tool_factory(deps, fn, resolved_meta)

        if handler is not None:
            return wrap(handler)
        return wrap

    @staticmethod
    def _resolve_tool_meta(
        fn: Callable[..., Any],
        meta: McpToolMeta | Mapping[str, Any] | None,
        name: str | None,
        owner: str | None,
        extra: dict[str, Any] | None,
        meta_fields: dict[str, Any],
    ) -> McpToolMeta:
        if isinstance(meta, McpToolMeta):
            return meta
        if meta is not None:
            fields = {k: v for k, v in meta.items() if k not in ("name", "owner", "extra")}
            return McpToolMeta(
                name=str(meta.get("name") or getattr(fn, "__name__", "unknown")),
                owner=meta.get("owner"),
                extra=meta.get("extra"),
                meta=fields,
            )
        return McpToolMeta(
            name=name if name is not None else getattr(fn, "__name__", "unknown"),
            owner=owner,
            extra=extra,
            meta=dict(meta_fields),
        )

    def instrument_server(
        self,
        server: Any,
        *,
        user_id: str | None = None,
        device_id: str | None = None,
        tenant: McpTenant | None = None,
        auth_type: str | None = None,
        client: McpClientInfo | Mapping[str, str] | None = None,
        session_id: str | None = None,
        protocol_version: str | None = None,
        extra: dict[str, Any] | None = None,
        transport: McpTransport | None = None,
    ) -> Any:
        """Bind a server so its instrumented tools inherit a server-scope
        context and the SDK emits the default connection events
        (``[MCP] Session Initialized`` / ``[MCP] Session Ended`` /
        ``[MCP] Tools Listed``) plus ``[MCP] Tool Call Rejected`` for
        ``tools/call`` requests that fail before any tool callback runs.

        Accepts a ``FastMCP`` or low-level ``mcp.server.lowlevel.Server``.
        Wraps ``run()`` (each run is one connection) to auto-detect the
        transport from the message stream, captures the handshake
        ``clientInfo``, and emits the lifecycle events at the points it
        controls. Call **before** the server runs. Returns the same server for
        chaining. Idempotent.

        The identity fields sit at the server-identity step of the fallback
        chain, scoped to THIS server binding. ``client`` supplies MCP client
        info resolved out-of-band (handshake / per-request values still win);
        ``session_id`` binds a host-managed correlation session id (a transport
        session id still wins); ``protocol_version`` is the out-of-band
        fallback; ``extra`` is enrichment attached to every event from this
        server; ``transport`` overrides transport auto-detection
        (``'stdio'`` / ``'streamable-http'`` / ``'sse'``).

        Example::

            mcp = FastMCP("my-mcp")
            analytics.instrument_server(mcp, auth_type="oauth")  # before run
            mcp.run()  # transport auto-detected
        """
        if getattr(server, _INSTRUMENTED_ATTR, False):
            return server
        low_level, tool_manager = unwrap_server(server)
        setattr(server, _INSTRUMENTED_ATTR, True)

        logger = get_logger(self._amplitude)
        sanitize = self.config.sanitize_error_message

        # Analytics state owned by THIS binding — never shared across servers,
        # so per-request-server hosts can pass per-request values safely.
        identity: ServerIdentity | None = None
        if user_id is not None or device_id is not None or tenant is not None:
            identity = ServerIdentity(user_id=user_id, device_id=device_id, tenant=tenant)
            self._server_identity = identity

        bound_anchor = (
            McpAnchor(type="session-id", value=session_id) if session_id else None
        )
        if isinstance(client, Mapping):
            bound_client: McpClientInfo | None = McpClientInfo(
                name=client.get("name"),
                version=client.get("version"),
                user_agent=client.get("user_agent"),
            )
        else:
            bound_client = client

        def make_scope() -> ServerScope:
            ctx = create_server_context(
                server=McpServerInfo(name=self.server_name, version=self.server_version),
                transport=transport if transport is not None else "stdio",
                auth_type=auth_type,
                client=(
                    McpClientInfo(
                        name=bound_client.name,
                        version=bound_client.version,
                        user_agent=bound_client.user_agent,
                    )
                    if bound_client is not None
                    else None
                ),
                protocol_version=protocol_version,
                anchor=bound_anchor,
                extra=extra,
                emit_anonymous_event=self.config.emit_anonymous_event,
            )
            scope = ServerScope(
                ctx=ctx,
                identity=identity,
                transport_resolved=transport is not None,
            )
            # Mirror onto the last-connected fallback (see the field doc).
            self._server_ctx = ctx
            return scope

        def resolved_request_ctx(scope: ServerScope) -> McpServerContext | None:
            if scope.ctx is None:
                return None
            return build_server_context(
                scope.ctx,
                scope_session_id=scope.captured_session_id,
                server_identity=scope.identity,
                logger=logger,
            )

        def on_tools_listed(outcome: dict[str, Any]) -> None:
            # `tools/list` → `[MCP] Tools Listed`. The wrapped handler
            # enumerates the live tool set per call, so counts stay correct as
            # tools are added or removed.
            scope = current_server_scope()
            if scope is None:
                return
            ctx = resolved_request_ctx(scope)
            if ctx is None:
                return
            result, error = outcome["result"], outcome["error"]
            root = getattr(result, "root", result) if result is not None else None
            tools = getattr(root, "tools", None) or []
            names = [
                t_name
                for t in tools
                if isinstance((t_name := getattr(t, "name", None)), str)
            ]
            tool_error = classify_error(error) if error is not None else None
            emit_tools_listed(
                self._amplitude,
                ctx,
                is_error=error is not None,
                tool_count=len(tools),
                tool_names=names if names else None,
                duration_ms=outcome["duration_ms"],
                response_size_bytes=result_byte_size(result) if result is not None else None,
                error_message=tool_error.message if tool_error is not None else None,
                error_code=tool_error.code if tool_error is not None else None,
                error_type=tool_error.type if tool_error is not None else None,
                sanitize=sanitize,
            )

        def on_tool_call_settled(outcome: dict[str, Any]) -> None:
            # `tools/call` → `[MCP] Tool Call Rejected`, for requests that fail
            # before any tool callback runs (unknown tool, input-schema
            # validation): no instrument_tool wrapper ever sees the call.
            # Dispatched calls never double-emit — the wrapper marks each
            # dispatch and reports those on `[MCP] Tool Call Response` instead.
            scope = current_server_scope()
            if scope is None:
                return
            # Checked first: a real tool failure and a pre-dispatch rejection
            # reach us as the same shape, and this is what parts them.
            if was_tool_call_dispatched(outcome["marker"]):
                return

            result = outcome["result"]
            root = getattr(result, "root", result) if result is not None else None
            rejection = classify_pre_dispatch_rejection(
                error=outcome["error"],
                result=root,
                registry_state=lookup_registered_tool(tool_manager, outcome["tool_name"]),
            )
            if rejection is None:
                return

            ctx = resolved_request_ctx(scope)
            if ctx is None:
                return
            # Response size as the client actually received it: an in-band
            # `isError` result when the SDK produced one, else the JSON-RPC
            # error envelope reconstructed from the raise.
            if result is not None:
                response_size = result_byte_size(result)
            else:
                request_context = current_request_context()
                response_size = byte_size(
                    {
                        "jsonrpc": "2.0",
                        "id": getattr(request_context, "request_id", None),
                        "error": {
                            "code": (
                                rejection.json_rpc_code
                                if rejection.json_rpc_code is not None
                                else -32603
                            ),
                            "message": rejection.message,
                        },
                    }
                )

            emit_tool_call_rejected(
                self._amplitude,
                ctx,
                attempted_tool_name=outcome["tool_name"],
                # Structured cause: pre-dispatch failures share one error
                # shape, so this is the only thing that separates them once
                # sanitize_error_message has had a say over the message.
                rejection_reason=rejection.reason,
                error_message=rejection.message,
                # Pre-dispatch `tools/call` failures are JSON-RPC protocol
                # errors, so the JSON-RPC code rides `[MCP] Error Code` —
                # omitted rather than guessed when no code is recoverable,
                # leaving `[MCP] Error Type` as the segmentation key.
                error_code=(
                    str(rejection.json_rpc_code) if rejection.json_rpc_code is not None else None
                ),
                error_type="protocol_error",
                duration_ms=outcome["duration_ms"],
                response_size_bytes=response_size,
                # Protocol-level rejections still answer HTTP 200 over the HTTP
                # transports — the error travels in the response body. stdio
                # has no HTTP status, so none is fabricated there.
                response_http_status=(
                    200 if ctx.transport in ("streamable-http", "sse") else None
                ),
                sanitize=sanitize,
            )

        def install_hooks() -> None:
            # Default server connection / capability events (opt-out via
            # config). Re-asserted at every run so handlers registered after
            # instrument_server are caught.
            if self.config.autocapture.tools_listed:
                install_tools_list_hook(low_level, on_tools_listed)
            if self.config.autocapture.tool_calls:
                install_tool_call_hook(low_level, on_tool_call_settled)

        def on_initialized(scope: ServerScope) -> None:
            # `[MCP] Session Initialized` — the handshake only fires on the
            # session-bearing paths (stdio, stateful streamable HTTP, sse), so
            # this is never emitted on stateless HTTP. Resolve the floored
            # server ctx into its connection form (real anchor/identity) once,
            # in place; per-request builds still override these per call. The
            # handshake clientInfo was already captured by the stream observer.
            if not self.config.autocapture.session_lifecycle:
                return
            ctx = resolved_request_ctx(scope)
            if ctx is None:
                return
            scope.ctx = ctx
            self._server_ctx = ctx
            scope.session_start = time.perf_counter()
            emit_session_initialized(self._amplitude, ctx)

        def on_ended(scope: ServerScope, duration_ms: float) -> None:
            # `[MCP] Session Ended` on run teardown — only when a session was
            # initialized (gates out stateless HTTP, which never handshakes).
            if scope.ctx is None:
                return
            emit_session_ended(self._amplitude, scope.ctx, duration_ms=duration_ms)

        install_run_wrapper(
            low_level,
            make_scope=make_scope,
            install_hooks=install_hooks,
            on_initialized=on_initialized,
            on_ended=on_ended,
        )
        return server

    def flush(self) -> Any:
        """Flush the underlying client; returns its (client-specific) result."""
        settle_unflushed_count(self._track_count_since_flush)
        self._track_count_since_flush = 0
        return self._amplitude.flush()

    def shutdown(self) -> None:
        """Settle unflushed accounting; tear down the underlying client only if
        this SDK created it (api_key path) — never a caller-supplied client."""
        settle_unflushed_count(self._track_count_since_flush)
        self._track_count_since_flush = 0
        if self._owns_client:
            shutdown = getattr(self._amplitude, "shutdown", None)
            if callable(shutdown):
                shutdown()


def create_mcp_analytics(
    *,
    server_name: str,
    server_version: str,
    amplitude: AmplitudeClientLike | None = None,
    api_key: str | None = None,
    config: MCPAnalyticsConfig | None = None,
) -> AmplitudeMCPAnalytics:
    """Construct an :class:`AmplitudeMCPAnalytics` client — a factory
    equivalent to ``AmplitudeMCPAnalytics(...)``, for callers who prefer a
    function over the class."""
    return AmplitudeMCPAnalytics(
        server_name=server_name,
        server_version=server_version,
        amplitude=amplitude,
        api_key=api_key,
        config=config,
    )
