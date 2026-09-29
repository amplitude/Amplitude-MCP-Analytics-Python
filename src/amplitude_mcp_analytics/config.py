"""SDK configuration: autocapture toggles and the top-level config object."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, TypedDict

__all__ = [
    "AutocaptureConfig",
    "DEFAULT_PARAM_NEVER_KEYS",
    "ErrorMessageSanitizer",
    "MCPAnalyticsConfig",
    "ParamCaptureConfig",
    "RationaleSanitizer",
    "ResolvedAutocapture",
    "ResolvedParamCapture",
]


class AutocaptureConfig(TypedDict, total=False):
    """Per-family toggles for the SDK's auto-captured (default) events — the
    events emitted automatically by ``instrument_server`` / ``instrument_tool``
    without an explicit ``track_*`` call.

    - ``server_events``: umbrella default for the server connection/capability
      events (``[MCP] Session Initialized``, ``[MCP] Session Ended``,
      ``[MCP] Tools Listed``). Overridable per sub-family via
      ``session_lifecycle`` / ``tools_listed`` — e.g. hosts that build one MCP
      server per HTTP request typically want
      ``{"session_lifecycle": False, "tools_listed": True}``. Default ``True``.
    - ``session_lifecycle``: ``[MCP] Session Initialized`` + ``[MCP] Session
      Ended``. Defaults to the ``server_events`` value.
    - ``tools_listed``: ``[MCP] Tools Listed``. Defaults to the
      ``server_events`` value.
    - ``tool_calls``: tool-execution events — ``[MCP] Tool Call Response``
      (dispatched calls) and ``[MCP] Tool Call Rejected`` (``tools/call``
      requests that fail before any tool callback runs). Default ``True``.
    """

    server_events: bool
    session_lifecycle: bool
    tools_listed: bool
    tool_calls: bool


ErrorMessageSanitizer = Callable[[str], str | None]
"""Rewrites the ``[MCP] Error Message`` property before it is emitted.

Receives the message the SDK would otherwise send — for an in-band ``isError``
tool result that is the result's own text, which is caller- or end-user-derived
and may carry personal data the server author never chose to send. Return a
replacement string, or ``None`` to omit the property entirely
(``[MCP] Error Code`` and ``[MCP] Error Type`` are unaffected either way).

Applied to every event that carries the property: ``[MCP] Tools Listed``,
``[MCP] Tool Call Response``, and ``[MCP] Tool Call Rejected``.

A sanitizer that raises is treated as ``None``. It fails **closed** — the raw
message is never used as a fallback, since a sanitizer exists precisely to keep
that value out of the event stream.
"""

RationaleSanitizer = Callable[[str], str | None]
"""Rewrites the ``[MCP] Rationale`` property before it is emitted.

Receives the rationale the host passed to ``set_rationale`` — LLM-generated free
text about why a tool was called, which can quote the end user's prompt. Return
a replacement string, or ``None`` to omit the property entirely.

Applied wherever the property is lowered: the default ``[MCP] Tool Call
Response`` event and every tool-scope custom event of the same invocation.

Same fail-closed contract as :data:`ErrorMessageSanitizer` — a sanitizer that
raises, or returns a non-string, omits the property rather than falling back to
the raw text.
"""

#: Parameter keys omitted from capture for every tool. ``never_keys`` replaces
#: this list when provided — it is not unioned with it.
DEFAULT_PARAM_NEVER_KEYS: tuple[str, ...] = ("rationale", "context")


class ParamCaptureConfig(TypedDict, total=False):
    """Global controls for tool-parameter capture.

    - ``shape``: emit content-free parameter shape metadata on tool-call
      events. Default ``True``.
    - ``never_keys``: parameter keys excluded from capture for every tool.
      **Replaces** :data:`DEFAULT_PARAM_NEVER_KEYS` when provided — it is not
      unioned. Include ``rationale`` and ``context`` if you still want those
      omitted. Pass ``[]`` to exclude nothing globally. Tool-level ``never``
      is unioned with the resolved list.
    """

    shape: bool
    never_keys: Sequence[str]


@dataclass(frozen=True)
class ResolvedParamCapture:
    """Resolved global parameter-capture controls."""

    shape: bool
    never_keys: tuple[str, ...]


def _resolve_never_keys(value: Any) -> tuple[str, ...]:
    """``never_keys`` replaces the default only when it is a list or tuple.

    Non-strings inside that list are dropped. Anything else (including a bare
    string, which would otherwise iterate as characters) keeps the default.
    """
    if isinstance(value, (list, tuple)):
        return tuple(key for key in value if isinstance(key, str))
    return DEFAULT_PARAM_NEVER_KEYS


@dataclass(frozen=True)
class ResolvedAutocapture:
    """Resolved per-family autocapture flags, normalized from the option."""

    server_events: bool
    session_lifecycle: bool
    tools_listed: bool
    tool_calls: bool


_ALL_ON = ResolvedAutocapture(
    server_events=True, session_lifecycle=True, tools_listed=True, tool_calls=True
)
_ALL_OFF = ResolvedAutocapture(
    server_events=False, session_lifecycle=False, tools_listed=False, tool_calls=False
)


def _resolve_autocapture(
    option: bool | AutocaptureConfig | Mapping[str, bool] | None,
) -> ResolvedAutocapture:
    """Normalize the ``autocapture`` option into resolved per-family flags."""
    if option is None or option is True:
        return _ALL_ON
    if option is False:
        return _ALL_OFF
    server_events = option.get("server_events", True)
    return ResolvedAutocapture(
        server_events=server_events,
        session_lifecycle=option.get("session_lifecycle", server_events),
        tools_listed=option.get("tools_listed", server_events),
        tool_calls=option.get("tool_calls", True),
    )


class MCPAnalyticsConfig:
    """Configuration for the Amplitude MCP Analytics SDK.

    Intentionally minimal in v0 — additional knobs (privacy, content modes,
    event-validation, on-event hooks) will be added as the features that need
    them land.

    Example::

        from amplitude_mcp_analytics import AmplitudeMCPAnalytics, MCPAnalyticsConfig

        analytics = AmplitudeMCPAnalytics(
            api_key=os.environ["AMPLITUDE_API_KEY"],
            server_name="my-mcp-server",
            server_version="1.0.0",
            config=MCPAnalyticsConfig(autocapture={"server_events": False}),
        )
    """

    def __init__(
        self,
        *,
        debug: bool = False,
        dry_run: bool = False,
        autocapture: bool | AutocaptureConfig | None = None,
        emit_anonymous_event: bool = False,
        sanitize_error_message: ErrorMessageSanitizer | None = None,
        sanitize_rationale: RationaleSanitizer | None = None,
        param_capture: ParamCaptureConfig | None = None,
    ) -> None:
        #: Emit verbose internal logging. Default ``False``.
        self.debug = debug
        #: When true, the SDK builds events normally but does not deliver them.
        self.dry_run = dry_run
        #: Resolved per-family autocapture flags. Pass ``True``/``False`` to
        #: toggle every family at once, or a mapping to toggle families
        #: independently (e.g. ``{"server_events": False}``).
        self.autocapture = _resolve_autocapture(autocapture)
        #: Whether to emit events for a fully anonymous subject — one with no
        #: supplied or derivable identity AND no tenant, resolved to the
        #: per-request anonymous floor. Each such request mints a fresh
        #: synthetic ``device_id``, so emitting them inflates unique-user/device
        #: counts with values that never recur. Default ``False`` (dropped).
        self.emit_anonymous_event = emit_anonymous_event
        #: Rewrite or drop ``[MCP] Error Message`` before it is emitted. Left
        #: ``None``, the message is sent as-is (the v0 default).
        self.sanitize_error_message = (
            sanitize_error_message if callable(sanitize_error_message) else None
        )
        #: Rewrite or drop ``[MCP] Rationale`` before it is emitted. Left
        #: ``None``, the host-supplied rationale is sent as-is (the v0 default —
        #: configuring this changes nothing on the wire until you do).
        self.sanitize_rationale = (
            sanitize_rationale if callable(sanitize_rationale) else None
        )
        raw_capture = param_capture if isinstance(param_capture, Mapping) else {}
        raw_shape = raw_capture.get("shape", True)
        never_keys = (
            DEFAULT_PARAM_NEVER_KEYS
            if "never_keys" not in raw_capture
            else _resolve_never_keys(raw_capture.get("never_keys"))
        )
        #: Content-free parameter metadata on tool-call events. ``shape``
        #: defaults on. ``never_keys`` defaults to ``rationale`` and ``context``
        #: and replaces that list when set.
        self.param_capture = ResolvedParamCapture(
            shape=raw_shape if isinstance(raw_shape, bool) else True,
            never_keys=never_keys,
        )
