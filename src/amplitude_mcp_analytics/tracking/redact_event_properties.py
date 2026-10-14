"""Apply a :class:`PrivacyConfig` to a merged event-property bag.

Dimension keys (everything in :data:`EVENT_PROPERTY_KEYS` except rationale
and error message) pass through unchanged so attribution stays intact.
Every other value — ``extra``, caller properties, ``[MCP] Rationale``,
``[MCP] Error Message``, ``[MCP] Feedback Comment``, and ``[MCP] Param:``
derived strings — is redacted.

@internal
"""

from __future__ import annotations

from typing import Any

from ..core.privacy import PrivacyConfig
from .constants import EVENT_PROPERTY_KEYS, FEEDBACK_COMMENT_MAX
from .types import TrackEventOptions

#: Built-in PII patterns, used when a caller does not pass a policy.
_DEFAULT_PRIVACY = PrivacyConfig()


def resolve_event_privacy(options: TrackEventOptions | None) -> PrivacyConfig:
    """Policy for one emit.

    A client-supplied :class:`PrivacyConfig` wins. Otherwise the standalone
    redaction fields on ``options`` are compiled, defaulting to built-in
    patterns on. Invalid standalone fields raise ``ConfigurationError``.

    @internal
    """
    if options is not None and options.privacy is not None:
        return options.privacy
    if options is None or (
        options.redact_pii is None
        and options.custom_redaction_patterns is None
        and options.custom_redaction_fn is None
    ):
        return _DEFAULT_PRIVACY
    return PrivacyConfig(
        redact_pii=True if options.redact_pii is None else options.redact_pii,
        custom_redaction_patterns=options.custom_redaction_patterns,
        custom_redaction_fn=options.custom_redaction_fn,
    )

_FEEDBACK_COMMENT_KEY = EVENT_PROPERTY_KEYS["feedback_comment"]

_FREE_TEXT_RESERVED = frozenset(
    {
        EVENT_PROPERTY_KEYS["rationale"],
        EVENT_PROPERTY_KEYS["error_message"],
        _FEEDBACK_COMMENT_KEY,
    }
)

_DIMENSION_KEYS = frozenset(
    key for key in EVENT_PROPERTY_KEYS.values() if key not in _FREE_TEXT_RESERVED
)


def redact_freeform_properties(
    properties: dict[str, Any],
    privacy: PrivacyConfig,
) -> dict[str, Any]:
    """Redact free-form values in a merged property bag. Dimension keys pass through.

    @internal
    """
    redacted: dict[str, Any] = {}
    for key, value in properties.items():
        if key in _DIMENSION_KEYS:
            redacted[key] = value
            continue
        cleaned = privacy.redact_value(value)
        # A custom replacement can grow the comment past the input cap. Slice
        # once, after redaction, so the emitted value stays within 500 characters.
        if (
            key == _FEEDBACK_COMMENT_KEY
            and isinstance(cleaned, str)
            and len(cleaned) > FEEDBACK_COMMENT_MAX
        ):
            cleaned = cleaned[:FEEDBACK_COMMENT_MAX]
        redacted[key] = cleaned
    return redacted
