"""Apply a :class:`PrivacyConfig` to a merged event-property bag.

Dimension keys (everything in :data:`EVENT_PROPERTY_KEYS` except rationale
and error message) pass through unchanged so attribution stays intact.
Every other value — ``extra``, caller properties, ``[MCP] Rationale``,
``[MCP] Error Message``, and ``[MCP] Param:`` derived strings — is redacted.

@internal
"""

from __future__ import annotations

from typing import Any

from ..core.privacy import PrivacyConfig
from .constants import EVENT_PROPERTY_KEYS

_FREE_TEXT_RESERVED = frozenset(
    {
        EVENT_PROPERTY_KEYS["rationale"],
        EVENT_PROPERTY_KEYS["error_message"],
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
