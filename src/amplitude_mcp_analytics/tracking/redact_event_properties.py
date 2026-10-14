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
from .constants import EVENT_PROPERTY_KEYS
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

_FREE_TEXT_RESERVED = frozenset(
    {
        EVENT_PROPERTY_KEYS["rationale"],
        EVENT_PROPERTY_KEYS["error_message"],
        EVENT_PROPERTY_KEYS["feedback_comment"],
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
    return {
        key: value if key in _DIMENSION_KEYS else privacy.redact_value(value)
        for key, value in properties.items()
    }
