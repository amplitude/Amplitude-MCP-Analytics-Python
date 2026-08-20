# Delivery is composed as a single plain DeliveryClient wrapper around the
# injected client — no separate proxy layer, so the hook order and guarantees
# documented on DeliveryClient below live in one place.
#
# The transport-level >=400 delivery warning rides a per-event callback on
# each BaseEvent (the amplitude-analytics client invokes
# callback(event, status_code, message)) rather than mutating the caller's
# client-level Config.callback, so instrumenting a caller-supplied client never
# overwrites a callback the host already set.
#
# Unflushed-event exit accounting is settled via atexit, which fires once at
# interpreter shutdown — not on os._exit or SIGKILL, an accepted limit given
# neither offers a reliable shutdown hook to settle accounting from.

"""Event delivery: dry-run, debug, short-id warnings, unflushed-event exit
accounting, and conversion to the amplitude-analytics client. Internal."""

from __future__ import annotations

import atexit
import logging
import os
import sys
from collections.abc import Callable
from typing import Any

from ..config import MCPAnalyticsConfig
from ..types import AmplitudeClientLike, AmplitudeEvent
from ..utils.debug import format_debug_line, format_dry_run_line
from ..utils.logger import get_logger

__all__ = [
    "DeliveryClient",
    "get_global_unflushed_count",
    "increment_unflushed_count",
    "is_serverless",
    "register_exit_hook",
    "settle_unflushed_count",
]

_MIN_ID_LENGTH = 5

_short_id_warned: set[str] = set()


def _warn_short_id(event: AmplitudeEvent, logger: logging.Logger) -> None:
    """Warn once per (field, value) when a user_id/device_id is shorter than
    Amplitude's 5-character minimum — the server rejects such events with
    HTTP 400 ("Invalid id length"), which is otherwise easy to miss."""
    for field in ("user_id", "device_id"):
        val = event.get(field)
        if isinstance(val, str) and 0 < len(val) < _MIN_ID_LENGTH:
            key = f"{field}:{val}"
            if key not in _short_id_warned:
                _short_id_warned.add(key)
                logger.warning(
                    'AmplitudeMCPAnalytics: %s="%s" is shorter than %d characters. '
                    "Amplitude's server will reject this event with HTTP 400 "
                    '("Invalid id length"). Use a longer identifier.',
                    field,
                    val,
                    _MIN_ID_LENGTH,
                )


def _reset_short_id_warned() -> None:  # pyright: ignore[reportUnusedFunction] — test hook
    """@internal Exposed for testing only."""
    _short_id_warned.clear()


# --- Serverless detection + unflushed-event exit accounting -----------------
#
# Module-level (rather than per-instance) so it never holds a strong reference
# to a client instance — that would prevent GC if the consumer forgets to call
# shutdown().

_SERVERLESS_ENV_VARS = (
    "AWS_LAMBDA_FUNCTION_NAME",  # AWS Lambda
    "VERCEL",  # Vercel Functions
    "NETLIFY",  # Netlify Functions
    "FUNCTION_TARGET",  # Google Cloud Functions
    "WEBSITE_INSTANCE_ID",  # Azure Functions
    "CF_PAGES",  # Cloudflare Pages Functions
)

_serverless_cached: bool | None = None
_global_unflushed_count = 0
_exit_hook_registered = False


def is_serverless() -> bool:
    """Detect whether the current process is running in a serverless
    environment. Checks well-known environment variables set by major
    serverless platforms. Result is cached after the first call. @internal"""
    global _serverless_cached
    if _serverless_cached is not None:
        return _serverless_cached
    _serverless_cached = any(os.environ.get(v) not in (None, "") for v in _SERVERLESS_ENV_VARS)
    return _serverless_cached


def _reset_serverless_cache() -> None:  # pyright: ignore[reportUnusedFunction] — test hook
    """@internal Reset the cached serverless result (for testing)."""
    global _serverless_cached
    _serverless_cached = None


def increment_unflushed_count() -> None:
    """Record that one more event has been handed to the underlying transport.
    @internal"""
    global _global_unflushed_count
    _global_unflushed_count += 1


def settle_unflushed_count(count: int) -> None:
    """Settle ``count`` previously-tracked events against the global counter —
    called on flush()/shutdown() once the underlying client has taken ownership
    of them. Clamped at zero so double-settling can never push the counter
    negative. @internal"""
    global _global_unflushed_count
    _global_unflushed_count = max(0, _global_unflushed_count - count)


def get_global_unflushed_count() -> int:
    """@internal Exposed for testing only."""
    return _global_unflushed_count


def _reset_unflushed_state() -> None:  # pyright: ignore[reportUnusedFunction] — test hook
    """@internal Reset the global unflushed counter (for testing)."""
    global _global_unflushed_count
    _global_unflushed_count = 0


def _exit_warning() -> None:
    # Deliberately `print`, not `logger.warning`: this runs from an atexit hook,
    # where logging may already have been torn down (`logging.shutdown` is itself
    # registered atexit), and a dropped-events warning is exactly the message
    # that must never be the one lost.
    if not is_serverless():
        return
    if _global_unflushed_count > 0:
        print(
            f"⚠️  AmplitudeMCPAnalytics: {_global_unflushed_count} event(s) were tracked "
            "but never flushed. In serverless environments, call `analytics.flush()` "
            "before your handler returns to avoid losing events.",
            file=sys.stderr,
        )


def register_exit_hook() -> None:
    """Register a one-time exit handler that warns when events were tracked
    but never flushed in a serverless environment, where the runtime may freeze
    before the periodic flush interval fires. Idempotent across calls.
    @internal"""
    global _exit_hook_registered
    if _exit_hook_registered:
        return
    _exit_hook_registered = True
    atexit.register(_exit_warning)


# --- The delivery client ------------------------------------------------------


def _resolve_amplitude_types() -> tuple[type, type] | None:
    """The (Amplitude, BaseEvent) classes when amplitude-analytics is
    importable, else None. `amplitude-analytics` is a hard dependency, so the
    None branch is defensive only (a broken install); the import stays lazy so
    it is paid once, on first client construction, rather than at SDK import."""
    try:
        from amplitude import Amplitude, BaseEvent  # pyright: ignore[reportMissingImports]
    except ImportError:
        return None
    return Amplitude, BaseEvent


class DeliveryClient:
    """Wraps the raw injected client with the delivery hooks in a fixed,
    load-bearing order: the dry-run gate sits outermost, so dry-run events
    are never counted as unflushed; the counter runs only on real delivery.

    When the raw client is an ``amplitude.Amplitude`` instance, events are
    converted to ``BaseEvent`` with a per-event delivery callback that surfaces
    HTTP >= 400 failures as warnings (the base SDK only logs those at INFO).
    Any other structural client receives the plain event dict.
    """

    def __init__(
        self,
        raw: AmplitudeClientLike,
        config: MCPAnalyticsConfig,
        on_tracked: Callable[[], None] | None = None,
    ) -> None:
        self._raw = raw
        self._config = config
        self._on_tracked = on_tracked
        amp_types = _resolve_amplitude_types()
        self._is_real_amplitude = amp_types is not None and isinstance(raw, amp_types[0])
        self._base_event_cls = amp_types[1] if amp_types is not None else None

    def track(self, event: AmplitudeEvent) -> None:
        logger = get_logger()

        # Short-ID warning — Amplitude server rejects user_id/device_id shorter
        # than 5 characters with HTTP 400.
        _warn_short_id(event, logger)

        # Debug goes through logging (the host's own filter/route surface);
        # dry-run keeps printing, since a dry run is an explicit "show me the
        # events" request that must be visible with no logging setup at all.
        if self._config.debug:
            logger.debug("%s", format_debug_line(event))
        if self._config.dry_run:
            print(format_dry_run_line(event), file=sys.stderr)
            return

        increment_unflushed_count()
        if self._on_tracked is not None:
            self._on_tracked()
        self._raw.track(self._convert(event, logger))

    def flush(self) -> Any:
        return self._raw.flush()

    def shutdown(self) -> None:
        shutdown = getattr(self._raw, "shutdown", None)
        if callable(shutdown):
            shutdown()

    def _convert(self, event: AmplitudeEvent, logger: logging.Logger) -> Any:
        """Convert to a ``BaseEvent`` for the real amplitude-analytics client;
        pass the dict through for structural clients."""
        if not self._is_real_amplitude or self._base_event_cls is None:
            return event

        def _delivery_callback(event_options: Any, status_code: Any, message: Any = None) -> None:
            # Default delivery callback — surface failures that the base SDK
            # only logs at INFO (invisible under most configurations).
            if isinstance(status_code, int) and status_code >= 400:
                event_type = getattr(event_options, "event_type", None) or "unknown"
                user_id = getattr(event_options, "user_id", None) or ""
                logger.warning(
                    "AmplitudeMCPAnalytics: event delivery failed — HTTP %s for "
                    "event=%s user_id=%s: %s",
                    status_code,
                    event_type,
                    user_id,
                    "" if message is None else message,
                )

        return self._base_event_cls(
            event_type=event["event_type"],
            user_id=event.get("user_id"),
            device_id=event.get("device_id"),
            event_properties=event.get("event_properties"),
            user_properties=event.get("user_properties"),
            groups=event.get("groups"),
            callback=_delivery_callback,
        )
