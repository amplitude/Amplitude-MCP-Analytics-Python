"""Applies the configured ``ErrorMessageSanitizer`` to ``[MCP] Error Message``.

Every event that carries the property routes its value through here, so a
consumer's sanitizer cannot be bypassed by whichever code path happens to be
emitting — see the ``sanitize_error_message`` config option.
"""

from __future__ import annotations

from ..config import ErrorMessageSanitizer

__all__ = ["sanitize_error_message"]


def sanitize_error_message(
    message: str, sanitize: ErrorMessageSanitizer | None
) -> str | None:
    """Resolve the value to emit for ``[MCP] Error Message``, or ``None`` to
    omit the property.

    With no sanitizer configured the message passes through unchanged (the v0
    default). Otherwise the property is omitted whenever the sanitizer declines
    to produce a string — an explicit ``None``, a non-string return, or a raise.

    Raising **fails closed** rather than falling back to ``message``: a
    sanitizer exists to keep that exact value out of the event stream, so a
    buggy one must not leak what it was installed to scrub. The exception is
    swallowed to preserve the SDK's best-effort telemetry contract — emitting
    an event must never break the tool response it describes.

    @internal
    """
    if sanitize is None:
        return message
    try:
        sanitized = sanitize(message)
    except Exception:
        return None
    return sanitized if isinstance(sanitized, str) else None
