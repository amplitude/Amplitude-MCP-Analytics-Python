"""PII redaction for free-form event content.

Ported from Amplitude-MCP-Analytics-Node ``src/core/privacy.ts`` at commit
``3b481c9`` (PR #58), which vendors Amplitude-AI-Node ``src/core/privacy.ts``
at ``c8ab2be``. The patterns, base64-image replacement, and content hash match
that module. Agent-only pieces are not ported: ``$llm_message`` chunking,
content modes, and system-prompt / reasoning / tool-definition shaping.

``PrivacyConfig`` is taxonomy-free (``redact_text`` / ``redact_value``). The
MCP emit seam decides which fields are redacted. Base64-image replacement
runs inside ``redact_value`` and is not gated on ``redact_pii``.

@internal Not part of the public package surface.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import urlparse

from ..utils.logger import get_logger

REDACTED_IMAGE_PLACEHOLDER = "[base64 image redacted]"

# Patterns match the Node module, including the phone false-edge that leaves
# a leading "(" (`Call (555) 123-4567` → `Call ([phone]`). `\b` / `\d` / `\w`
# stay on Python's default Unicode classes; the cases these patterns exist
# for are ASCII, and a caller's custom pattern is compiled the same way.
_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_PHONE_RE = re.compile(r"\b\(?([0-9]{3})\)?[-. ]?([0-9]{3})[-. ]?([0-9]{4})\b")
_CREDIT_CARD_RE = re.compile(r"\b(?:\d{4}[-\s]?){3}\d{4}\b")
_SSN_RE = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
_SSN_SPACE_RE = re.compile(r"\b\d{3} \d{2} \d{4}\b")
_IPV4_RE = re.compile(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b")
# Bare "::" is omitted; free-standing "::" abbreviations require whitespace
# or start-of-string to avoid false positives on scope-resolution operators
# (C++ std::vector, Ruby ::Module, Python a[::2]). Bracket-enclosed forms
# preceded by "//" are URL-context IPv6 (RFC 2732, e.g. http://[::1]:8080).
_IPV6_RE = re.compile(
    r"(?:(?<=//)\[::(?:[0-9a-fA-F]{1,4}:){0,5}[0-9a-fA-F]{1,4}\]"
    r"|(?<=//)\[::1\]"
    r"|\b(?:[0-9a-fA-F]{1,4}:){7}[0-9a-fA-F]{1,4}\b"
    r"|\b(?:[0-9a-fA-F]{1,4}:){1,6}:[0-9a-fA-F]{1,4}\b"
    r"|(?<![^\s])::(?:[0-9a-fA-F]{1,4}:){0,5}[0-9a-fA-F]{1,4}\b"
    r"|(?<![^\s])::1\b)"
)
_INTL_PHONE_RE = re.compile(r"(?<!\w)\+[1-9]\d{6,14}\b")
_BASE64_DATA_URL_RE = re.compile(r"^data:([^;]+);base64,")
_RAW_BASE64_RE = re.compile(r"^[A-Za-z0-9+/]+=*$")
_BASE64_ALPHABET_MARK = re.compile(r"[+/=]")


def is_base64_data_url(text: str) -> bool:
    """Whether ``text`` begins with a ``data:*;base64,`` prefix. @internal"""
    return _BASE64_DATA_URL_RE.match(text) is not None


def is_valid_url(text: str) -> bool:
    """Whether ``text`` is an absolute URL or a relative path.

    Stand-in for the Node ``URL`` constructor: an absolute URL needs a scheme
    and a hostname; anything else counts only when it is a relative path
    (``/``, ``./``, ``../``). Used so URLs are not mistaken for raw base64.
    @internal
    """
    parsed = urlparse(text)
    if parsed.scheme and parsed.hostname:
        return True
    return text.startswith("/") or text.startswith("./") or text.startswith("../")


def is_raw_base64(text: str) -> bool:
    """Whether ``text`` is an entire raw base64 payload large enough to be an image.

    Standard base64 is padded to a multiple of 4, and any image-sized payload
    contains ``+``, ``/``, or ``=``. Requiring one of those keeps
    identifier-style strings that happen to use a subset of the base64
    alphabet — ULIDs, hex tokens, UUIDs without dashes — from being
    mis-redacted as base64 images. @internal
    """
    if is_valid_url(text):
        return False
    if len(text) <= 20 or len(text) % 4 != 0:
        return False
    if _BASE64_ALPHABET_MARK.search(text) is None:
        return False
    return _RAW_BASE64_RE.fullmatch(text) is not None


def create_content_hash(content: Any) -> str:
    """SHA-256 hex digest of ``content``, or ``""`` for ``None``. @internal"""
    if content is None:
        return ""
    content_str = content if isinstance(content, str) else str(content)
    return hashlib.sha256(content_str.encode("utf-8")).hexdigest()


def redact_base64_content(value: Any) -> Any:
    """Replace a string that is entirely a base64 image. Other values pass through.

    @internal
    """
    if not isinstance(value, str):
        return value
    if is_base64_data_url(value) or is_raw_base64(value):
        return REDACTED_IMAGE_PLACEHOLDER
    return value


def redact_pii_patterns(text: Any) -> Any:
    """Apply the built-in PII patterns. Non-strings are returned unchanged.

    No-op for non-string inputs so redaction is safe without caller-side type
    gating. @internal
    """
    if not isinstance(text, str):
        return text
    result = text
    result = _EMAIL_RE.sub("[email]", result)
    result = _PHONE_RE.sub("[phone]", result)
    result = _CREDIT_CARD_RE.sub("[credit_card]", result)
    result = _SSN_RE.sub("[ssn]", result)
    result = _SSN_SPACE_RE.sub("[ssn]", result)
    result = _IPV4_RE.sub("[ip_address]", result)
    result = _IPV6_RE.sub("[ip_address]", result)
    result = _INTL_PHONE_RE.sub("[phone]", result)
    return result


class PrivacyConfig:
    """Redaction policy (PII toggle, custom patterns, custom function).

    Applies it to a string or an arbitrary structured value. Unlike the
    upstream class this carries no agent/LLM event-property shaping — it
    returns redacted values, and the MCP taxonomy decides how they are emitted.

    @internal Not part of the public package surface.
    """

    def __init__(
        self,
        *,
        redact_pii: bool = True,
        custom_redaction_patterns: list[Any] | tuple[Any, ...] | None = None,
        custom_redaction_fn: Callable[[str], str] | None = None,
    ) -> None:
        self.redact_pii = redact_pii
        self.custom_patterns: tuple[Any, ...] = (
            tuple(custom_redaction_patterns) if custom_redaction_patterns else ()
        )
        self._compiled: list[tuple[re.Pattern[str], str]] = []
        self._custom_redaction_fn = custom_redaction_fn

        for pattern in self.custom_patterns:
            source, replacement = _pattern_source(pattern)
            if source is None or replacement is None:
                get_logger().warning(
                    "AmplitudeMCPAnalytics: skipping custom redaction pattern that is "
                    "not a string or {pattern, replacement}."
                )
                continue
            try:
                self._compiled.append((re.compile(source), replacement))
            except re.error as exc:
                get_logger().warning(
                    'AmplitudeMCPAnalytics: invalid custom redaction regex "%s": %s',
                    source,
                    exc,
                )

    def redact_text(self, text: str) -> str:
        """Redact one string: built-in PII patterns (when enabled), then custom
        patterns, then the custom function. Base64-image replacement is applied
        by :meth:`redact_value`.
        """
        result = redact_pii_patterns(text) if self.redact_pii else text
        result = self._apply_custom_patterns(result)
        return self._apply_custom_fn(result)

    def redact_value(self, value: Any) -> Any:
        """Recursively redact a structured value.

        Every string leaf is run through :meth:`redact_text` and base64-image
        redaction. Dicts, lists, and tuples are rebuilt. Other values pass
        through unchanged. Object keys are not rewritten.
        """
        if isinstance(value, str):
            return redact_base64_content(self.redact_text(value))

        if isinstance(value, dict):
            return {key: self.redact_value(item) for key, item in value.items()}

        if isinstance(value, list):
            return [self.redact_value(item) for item in value]

        # Tuples show up in Python where Node would have seen an array. Rebuild
        # them so a tuple of free-form strings is not a hole in redaction.
        if isinstance(value, tuple):
            return tuple(self.redact_value(item) for item in value)

        return value

    def _apply_custom_patterns(self, text: str) -> str:
        if not self._compiled:
            return text
        result = text
        for regex, replacement in self._compiled:
            try:
                result = regex.sub(replacement, result)
            except re.error as exc:
                get_logger().warning(
                    'AmplitudeMCPAnalytics: custom redaction regex "%s" failed: %s',
                    regex.pattern,
                    exc,
                )
        return result

    def _apply_custom_fn(self, text: str) -> str:
        if self._custom_redaction_fn is None:
            return text
        try:
            # The callback is typed to return str; the check below is the
            # fail-open guard for a function that doesn't.
            result: Any = self._custom_redaction_fn(text)
        except Exception as exc:  # noqa: BLE001 — fail-open: keep the current text
            get_logger().error(
                "AmplitudeMCPAnalytics: custom_redaction_fn raised an exception: %s "
                "— PII may not be fully redacted for this value",
                exc,
            )
            return text
        if isinstance(result, str):
            return result
        get_logger().error(
            "AmplitudeMCPAnalytics: custom_redaction_fn returned %s instead of str; "
            "skipping — PII may not be fully redacted for this value",
            type(result).__name__,
        )
        return text


def _pattern_source(pattern: Any) -> tuple[str | None, str | None]:
    """Split a custom pattern into ``(source, replacement)``, or ``(None, None)``."""
    if isinstance(pattern, str):
        return pattern, "[REDACTED]"
    if isinstance(pattern, Mapping):
        source = pattern.get("pattern")
        replacement = pattern.get("replacement")
        if isinstance(source, str) and isinstance(replacement, str):
            return source, replacement
    return None, None
