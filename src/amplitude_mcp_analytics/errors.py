"""Tool-error taxonomy, classification, and privacy-preserving hashing.

The wire taxonomy (:data:`McpToolErrorType`) is a closed, cross-SDK set so
analytics grouping stays consistent regardless of which SDK emitted an event;
the *classification heuristics* that assign it are Python-native, built around
this language's own exception conventions (see :func:`classify_error`).
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import traceback
from dataclasses import dataclass
from typing import Any, Literal

__all__ = [
    "McpToolError",
    "McpToolErrorType",
    "build_tool_error",
    "classify_error",
    "tool_error_result",
]

# High-level error category the SDK classifies a failure into. This is a
# **closed, SDK-owned** set: hosts describe their own failures through `code`
# (free-form), not by picking a type. Every value here is one the SDK itself
# assigns:
#
# - `returned_error` — a tool returned an in-band error result (`isError: true`).
# - `thrown_exception` — a handler raised an exception with no more specific rule.
# - `timeout` — the raised error was a timeout/cancellation.
# - `transport_error` — the raised error was a network/connection failure.
# - `protocol_error` — a `tools/call` failed before dispatch (unknown tool,
#   input-schema validation) — see `[MCP] Tool Call Rejected`.
# - `rate_limited` — the raised error carried HTTP status 429.
# - `unknown` — a non-exception value was classified.
McpToolErrorType = Literal[
    "returned_error",
    "thrown_exception",
    "transport_error",
    "timeout",
    "protocol_error",
    "rate_limited",
    "unknown",
]


@dataclass
class McpToolError:
    """Structured error descriptor attached to ``ctx.error`` on failure.

    Powers both the MCP error response returned to the client and the
    telemetry event emitted by the SDK.
    """

    message: str
    """Human-readable error description."""

    type: McpToolErrorType
    """High-level, SDK-assigned error category for analytics grouping."""

    code: str | None = None
    """Machine-readable error identifier, finer-grained than ``type``. Set when
    a specific reason is known — a host ``code`` from ``analytics.tool_error()``,
    an OS/errno code, or a JSON-RPC code — and left ``None`` when the only thing
    known is the coarse ``type``, so it never just echoes ``type``. Emitted as
    ``[MCP] Error Code`` when present."""

    correction_message: str | None = None
    """Guidance for the LLM client to self-correct and retry."""

    recoverable: bool | None = None
    """Whether the caller can reasonably retry the same request."""

    retry_suggested: bool | None = None
    """SDK hint that a retry is worth attempting."""

    http_status: int | None = None
    """HTTP status attached to the tool's failure (e.g. an upstream API
    response, or a raised HTTP-shaped error). NOT the MCP transport status.
    Sniffed from ``err.status`` / ``err.status_code`` / ``err.response.status_code``
    by :func:`classify_error`; set explicitly via ``build_tool_error`` otherwise."""

    stack_hash: str | None = None
    """Privacy-safe hash of the top stack frames; never contains raw paths."""

    fingerprint: str | None = None
    """Grouping key derived from ``type + normalized message``. Override via
    ``build_tool_error(fingerprint=...)``."""


def build_tool_error(
    *,
    code: str,
    message: str,
    correction_message: str | None = None,
    recoverable: bool | None = None,
    retry_suggested: bool | None = None,
    http_status: int | None = None,
    fingerprint: str | None = None,
) -> McpToolError:
    """Build a host-described in-band tool error (always ``returned_error``).

    ``code`` is required — describe the specific failure (e.g.
    ``'missing_chart_id'``); it is emitted as ``[MCP] Error Code``.
    """
    error_type: McpToolErrorType = "returned_error"
    return McpToolError(
        code=code,
        message=message,
        type=error_type,
        correction_message=correction_message,
        recoverable=recoverable,
        retry_suggested=retry_suggested,
        http_status=http_status if _is_http_status(http_status) else None,
        fingerprint=fingerprint if fingerprint is not None else compute_fingerprint(error_type, message),
    )


def tool_error_result(error: McpToolError) -> dict[str, Any]:
    """Lower an :class:`McpToolError` to an in-band MCP ``CallToolResult`` dict
    (``isError: true``) suitable for returning from a low-level tool handler."""
    text = (
        f"{error.message} {error.correction_message}"
        if error.correction_message
        else error.message
    )
    return {"content": [{"type": "text", "text": text}], "isError": True}


def is_error_result(value: Any) -> bool:
    """Whether a tool result signals an in-band protocol error — a
    ``CallToolResult`` with ``isError: true``. Accepts a handler's raw return
    (dict or object) and narrows. @internal"""
    if value is None:
        return False
    if isinstance(value, dict):
        return value.get("isError") is True
    return getattr(value, "isError", None) is True or getattr(value, "is_error", None) is True


def error_message_from_result(result: Any) -> str | None:
    """Best-effort human-readable message from an ``isError`` result's text
    content parts. ``None`` when the content carries no text. @internal"""
    content = result.get("content") if isinstance(result, dict) else getattr(result, "content", None)
    if not isinstance(content, (list, tuple)):
        return None
    parts: list[str] = []
    for part in content:
        if isinstance(part, dict):
            if part.get("type") == "text" and isinstance(part.get("text"), str):
                parts.append(part["text"])
        elif getattr(part, "type", None) == "text" and isinstance(getattr(part, "text", None), str):
            parts.append(part.text)
    text = " ".join(p for p in parts if p).strip()
    return text or None


# Python analogues of Node's network error codes (ECONNREFUSED, ECONNRESET,
# ENOTFOUND, ETIMEDOUT, EPIPE, EAI_AGAIN): the ConnectionError family plus
# socket.gaierror. Each maps to the lowercase Node-style code so the emitted
# `[MCP] Error Code` values line up across SDKs.
_NETWORK_ERROR_CODES: dict[type[BaseException], str] = {
    ConnectionRefusedError: "econnrefused",
    ConnectionResetError: "econnreset",
    BrokenPipeError: "epipe",
    ConnectionAbortedError: "econnaborted",
}


def _is_http_status(value: Any) -> bool:
    """Valid HTTP status range guard for the sniffed/explicit values. @internal"""
    return isinstance(value, int) and not isinstance(value, bool) and 100 <= value <= 599


def _http_status_from(err: BaseException) -> int | None:
    """Sniff an HTTP status off a raised error via the dominant Python
    conventions: ``err.status`` (aiohttp), ``err.status_code``, then
    ``err.response.status_code`` (httpx, requests). Errors with other shapes go
    through ``build_tool_error(http_status=...)`` instead. @internal"""
    for attr in ("status", "status_code"):
        candidate = getattr(err, attr, None)
        if _is_http_status(candidate):
            return candidate
    response = getattr(err, "response", None)
    if response is not None:
        candidate = getattr(response, "status_code", None)
        if _is_http_status(candidate):
            return candidate
    return None


def _is_timeout(err: BaseException) -> bool:
    # TimeoutError covers socket.timeout (3.10+) and asyncio.TimeoutError
    # (3.11+); asyncio.TimeoutError is checked separately for 3.10.
    # CancelledError is treated the same way: an abort signal interrupting the
    # call is, from the caller's perspective, indistinguishable from a timeout.
    return isinstance(err, (TimeoutError, asyncio.TimeoutError, asyncio.CancelledError))


def _network_code(err: BaseException) -> str | None:
    for exc_type, code in _NETWORK_ERROR_CODES.items():
        if isinstance(err, exc_type):
            return code
    # socket.gaierror (DNS failure) — the ENOTFOUND/EAI_AGAIN analogue.
    if type(err).__name__ == "gaierror":
        return "enotfound"
    return None


def classify_error(err: Any) -> McpToolError:
    """Classify a raised value into the closed :data:`McpToolErrorType` set.

    A fixed priority ladder built on Python idioms: timeout/cancellation →
    ``timeout``; connection/DNS failures → ``transport_error``; HTTP 429 →
    ``rate_limited`` (with ``retry_suggested``); any other exception →
    ``thrown_exception``; a non-exception value → ``unknown``.
    """
    if not isinstance(err, BaseException):
        message = err if isinstance(err, str) else "Unknown error"
        return McpToolError(
            message=message,
            type="unknown",
            fingerprint=compute_fingerprint("unknown", message),
        )

    message = str(err) or type(err).__name__
    stack = hash_stack(err)
    http_status = _http_status_from(err)
    raw_code = getattr(err, "code", None)
    carried_code = str(raw_code) if isinstance(raw_code, (str, int)) else None

    if _is_timeout(err):
        return McpToolError(
            message=message,
            type="timeout",
            stack_hash=stack,
            http_status=http_status,
            fingerprint=compute_fingerprint("timeout", message),
        )

    network_code = _network_code(err)
    if network_code is not None:
        return McpToolError(
            code=network_code,
            message=message,
            type="transport_error",
            stack_hash=stack,
            http_status=http_status,
            fingerprint=compute_fingerprint("transport_error", message),
        )

    if http_status == 429:
        return McpToolError(
            message=message,
            type="rate_limited",
            code=carried_code,
            stack_hash=stack,
            http_status=http_status,
            fingerprint=compute_fingerprint("rate_limited", message),
            retry_suggested=True,
        )

    # Carry the error's own `code` (errno/OS string, or a host-raised error's
    # code attribute) when present; otherwise omit it.
    return McpToolError(
        message=message,
        type="thrown_exception",
        code=carried_code,
        stack_hash=stack,
        http_status=http_status,
        fingerprint=compute_fingerprint("thrown_exception", message),
    )


_UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE
)
_DIGITS_RE = re.compile(r"\b\d+\b")
_DQUOTE_RE = re.compile(r'"[^"]*"')
_SQUOTE_RE = re.compile(r"'[^']*'")


def normalize_message(message: str) -> str:
    """Replace volatile fragments (UUIDs, numbers, quoted strings) so messages
    that differ only in embedded values share a fingerprint. @internal"""
    message = _UUID_RE.sub("<uuid>", message)
    message = _DIGITS_RE.sub("<n>", message)
    message = _DQUOTE_RE.sub('"<str>"', message)
    message = _SQUOTE_RE.sub("'<str>'", message)
    return message


def compute_fingerprint(type_: str, message: str) -> str:
    """sha256 of ``f"{type}:{normalized message}"``, first 12 hex chars. @internal"""
    normalized = normalize_message(message)
    return hashlib.sha256(f"{type_}:{normalized}".encode()).hexdigest()[:12]


def hash_stack(err: BaseException) -> str | None:
    """Privacy-safe hash of the innermost 3 traceback frames — file basename,
    line, and function only, never raw paths. ``None`` without a traceback.
    @internal"""
    tb = getattr(err, "__traceback__", None)
    if tb is None:
        return None
    frames = traceback.extract_tb(tb)
    if not frames:
        return None
    innermost = frames[-3:]
    parts: list[str] = []
    for frame in innermost:
        basename = frame.filename.replace("\\", "/").rsplit("/", 1)[-1]
        parts.append(f"{basename}:{frame.lineno}:{frame.name}")
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()[:12]
