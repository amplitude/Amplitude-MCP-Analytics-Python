"""Main Amplitude MCP Analytics client for tracking MCP server activity.

Construct with either an API key (which initializes a fresh
``amplitude-analytics`` client under the hood) or a pre-built Amplitude client
owned by the host application. The server identity (``server_name``,
``server_version``) is required and is attached to every emitted event.
"""

from __future__ import annotations

import importlib
from typing import Any

from .config import MCPAnalyticsConfig
from .context.types import (
    McpServerContext,
    McpToolContext,
    SetIdentityInput,
)
from .context.vars import set_identity as _set_identity_on_ctx
from .context.vars import set_rationale as _set_rationale_on_ctx
from .core.delivery import (
    DeliveryClient,
    register_exit_hook,
    settle_unflushed_count,
)
from .errors import build_tool_error, tool_error_result
from .exceptions import ConfigurationError
from .tracking.track import track_server_event as _track_server_event
from .tracking.track import track_tool_event as _track_tool_event
from .tracking.types import TrackEventOptions
from .types import AmplitudeClientLike, AmplitudeEvent
from .utils.logger import get_logger

__all__ = ["AmplitudeMCPAnalytics", "create_mcp_analytics"]


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
                    "as a dependency: uv add amplitude-analytics (or pass a "
                    "pre-initialized client via the 'amplitude' option)."
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
        self._server_identity: dict[str, Any] | None = None

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
