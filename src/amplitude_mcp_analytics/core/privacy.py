"""PII redaction for free-form event content.

Ported from Amplitude-AI-Node ``src/core/privacy.ts`` at ``792806e`` for the
domain-agnostic redaction behavior (bounded Unicode email matching, base64
data URLs with MIME parameters, config-time pattern validation, and
fail-closed custom redaction). Phone patterns follow Amplitude-MCP-Analytics-Node
``src/core/privacy.ts`` at ``0492eed``, which formats numbers AA has not
taken. Agent-only pieces are not ported: ``$llm_message`` chunking, content
modes, the 100k text cap, and ``toTextSafe``.

Stdlib ``re`` has no ``\\p{Script=...}``. The email letter class approximates
Latin, Greek, and Cyrillic with Unicode ranges. Python ``\\w`` and ``\\b`` are
Unicode, so the phone boundaries use an ASCII word class to match Node's
non-unicode ``\\w`` / ``\\b``.

``PrivacyConfig`` is taxonomy-free (``redact_text`` / ``redact_value``). The
MCP emit seam decides which fields are redacted. Base64-image replacement
runs inside ``redact_value`` and is not gated on ``redact_pii``.

@internal Not part of the public package surface.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Mapping, Sequence
from typing import Any
from urllib.parse import urlparse

from ..exceptions import ConfigurationError
from ..utils.logger import get_logger

REDACTED_IMAGE_PLACEHOLDER = "[base64 image redacted]"
REDACTED_CONTENT_PLACEHOLDER = "[content redacted]"

# Latin (including Latin-1, so "josé" matches), Greek, and Cyrillic. Han, Kana,
# and Hangul are absent so CJK text written against an address is not swallowed
# into the local part ("联系alice@example.com" → "联系[email]").
_EMAIL_LETTER = (
    "A-Za-z"
    "\u00c0-\u00d6\u00d8-\u00f6\u00f8-\u00ff"
    "\u0100-\u024f"
    "\u0250-\u02af"
    "\u1e00-\u1eff"
    "\u2c60-\u2c7f"
    "\ua720-\ua7ff"
    "\uab30-\uab6f"
    "\u0370-\u03ff"
    "\u1f00-\u1fff"
    "\u0400-\u04ff"
    "\u0500-\u052f"
    "\u1c80-\u1c8f"
    "\u2de0-\u2dff"
    "\ua640-\ua69f"
)
_EMAIL_MARK = "\u0300-\u036f\u1ab0-\u1aff\u1dc0-\u1dff\u20d0-\u20ff\ufe20-\ufe2f"
_EMAIL_NUMBER = "0-9"
_EMAIL_LOCAL = f"{_EMAIL_LETTER}{_EMAIL_MARK}{_EMAIL_NUMBER}._%+\\-"
_EMAIL_LABEL = f"{_EMAIL_LETTER}{_EMAIL_MARK}{_EMAIL_NUMBER}\\-"
# Every quantifier is bounded, and a match may only start where the preceding
# character cannot belong to a local part, so matching stays linear.
_EMAIL_RE = re.compile(
    rf"(?<![{_EMAIL_LOCAL}])"
    rf"[{_EMAIL_LOCAL}]{{1,64}}"
    rf"@(?:[{_EMAIL_LABEL}]{{1,63}}\.){{1,8}}"
    rf"[{_EMAIL_LETTER}]{{2,63}}"
    rf"(?![{_EMAIL_LETTER}{_EMAIL_MARK}{_EMAIL_NUMBER}_])"
)
# `(?<![A-Za-z0-9_])` rather than `\b`, so an opening `(` is part of the match.
# An ASCII class matches Node's non-unicode `\w` / `\b`.
_PHONE_RE = re.compile(
    r"(?<![A-Za-z0-9_])\(?\d{3}\)?[-. ]?\d{3}[-. ]?\d{4}(?![A-Za-z0-9_])"
)
_CREDIT_CARD_RE = re.compile(r"\b(?:\d{4}[-\s]?){3}\d{4}\b")
_SSN_RE = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
_SSN_SPACE_RE = re.compile(r"\b\d{3} \d{2} \d{4}\b")
_IPV4_RE = re.compile(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b")
# URL-bracket form first so `http://[::1]:8080` keeps a single pair of
# brackets. A bare candidate is everything else made of hex and colons.
# `::` compression is validated separately so `2001:db8::` and
# `2001:db8::1:2` are replaced in full, while `std::vector` and `a[::2]` stay.
_IPV6_CANDIDATE_RE = re.compile(r"//\[[0-9a-fA-F:]+\]|[0-9a-fA-F:]{2,}")
_HEX_GROUP_RE = re.compile(r"^[0-9a-fA-F]{1,4}$")
_ASCII_WORD_RE = re.compile(r"[A-Za-z0-9_]")
# 7–15 digits total (`[1-9]` plus 6–14 more), with spaces, dots, dashes, or
# parentheses between digits. An uninterrupted `+14155552671` still matches.
_INTL_PHONE_RE = re.compile(
    r"(?<![A-Za-z0-9_])\+[1-9](?:[ \t.\-()]*\d){6,14}(?![A-Za-z0-9_])"
)
# MIME parameters (`data:image/png;charset=utf-8;base64,`) and an omitted
# media type (`data:;base64,`) are both data-URL images.
_BASE64_DATA_URL_RE = re.compile(r"^data:[^;,]*(?:;[^;,]+)*?;base64,")
_RAW_BASE64_RE = re.compile(r"^[A-Za-z0-9+/]+=*$")
_BASE64_ALPHABET_MARK = re.compile(r"[+/=]")
# JPEG base64 starts with `/9j/`. `is_valid_url` treats every leading `/` as a
# relative path, which would hide that payload from image redaction. PNG, GIF,
# and WebP prefixes are included so the same exemption cannot hide them.
_IMAGE_BASE64_PREFIXES = ("/9j/", "iVBORw0KGgo", "R0lGOD", "UklGR")


def is_base64_data_url(text: str) -> bool:
    """Whether ``text`` begins with a ``data:*;base64,`` prefix. @internal"""
    return _BASE64_DATA_URL_RE.match(text) is not None


def is_valid_url(text: str) -> bool:
    """Whether ``text`` is an absolute URL or a relative path.

    Stand-in for the Node ``URL`` constructor: an absolute URL needs a scheme
    and a hostname; a parse failure (malformed brackets such as ``http://[``)
    counts as not a URL. Anything else counts only when it is a relative path
    (``/``, ``./``, ``../``). Used so URLs are not mistaken for raw base64.
    @internal
    """
    # urlparse raises ValueError for malformed bracketed URLs such as
    # "http://[". Node's URL constructor throws on those too. A parse failure
    # is "not a URL", not an error that should abort the event.
    try:
        parsed = urlparse(text)
        if parsed.scheme and parsed.hostname:
            return True
    except ValueError:
        pass
    return text.startswith("/") or text.startswith("./") or text.startswith("../")


def is_raw_base64(text: str) -> bool:
    """Whether ``text`` is an entire raw base64 payload large enough to be an image.

    Standard base64 is padded to a multiple of 4, and any image-sized payload
    contains ``+``, ``/``, or ``=``. Requiring one of those keeps
    identifier-style strings that happen to use a subset of the base64
    alphabet — ULIDs, hex tokens, UUIDs without dashes — from being
    mis-redacted as base64 images. A known image prefix is checked before the
    relative-path exemption, so a JPEG (`/9j/...`) is not mistaken for a path.
    @internal
    """
    image_payload = text.startswith(_IMAGE_BASE64_PREFIXES)
    if not image_payload and is_valid_url(text):
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
    # International before the domestic pattern, so `+1 (415) 555-2671` is one
    # token instead of a leftover `+1` plus a domestic match.
    result = _INTL_PHONE_RE.sub("[phone]", result)
    result = _PHONE_RE.sub("[phone]", result)
    result = _CREDIT_CARD_RE.sub("[credit_card]", result)
    result = _SSN_RE.sub("[ssn]", result)
    result = _SSN_SPACE_RE.sub("[ssn]", result)
    result = _IPV4_RE.sub("[ip_address]", result)
    result = _redact_ipv6(result)
    return result


def _hex_groups(side: str) -> list[str] | None:
    if side == "":
        return []
    groups = side.split(":")
    if all(_HEX_GROUP_RE.fullmatch(group) for group in groups):
        return groups
    return None


def _is_ipv6_address(token: str) -> bool:
    """A full 8-group address, or one ``::`` compression standing in for the rest."""
    if ":" not in token or token == "::" or ":::" in token:
        return False
    halves = token.split("::")
    if len(halves) > 2:
        return False
    if len(halves) == 1:
        groups = _hex_groups(token)
        return groups is not None and len(groups) == 8
    left = _hex_groups(halves[0])
    right = _hex_groups(halves[1])
    # `::` replaces at least one group, so the explicit groups are at most 7.
    return left is not None and right is not None and len(left) + len(right) <= 7


def _is_free_standing_ipv6(text: str, offset: int) -> bool:
    """Colon-leading forms (`::1`) must be free-standing.

    A letter or ``[`` before them is a scope operator (`std::vector`,
    ``a[::2]``), not an address. Hex-leading forms must not continue an
    identifier. The word check is ASCII, matching Node's non-unicode ``\\w``.
    """
    before = text[offset - 1] if offset > 0 else ""
    if text[offset] == ":":
        return before == "" or before.isspace()
    return before == "" or _ASCII_WORD_RE.fullmatch(before) is None


def _redact_ipv6(text: str) -> str:
    def replace(match: re.Match[str]) -> str:
        token = match.group(0)
        if token.startswith("//["):
            return "//[ip_address]" if _is_ipv6_address(token[3:-1]) else token
        if not _is_ipv6_address(token) or not _is_free_standing_ipv6(text, match.start()):
            return token
        return "[ip_address]"

    return _IPV6_CANDIDATE_RE.sub(replace, text)


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
        custom_redaction_patterns: Sequence[Any] | None = None,
        custom_redaction_fn: Callable[[str], str] | None = None,
    ) -> None:
        self.redact_pii = redact_pii
        if custom_redaction_fn is not None and not callable(custom_redaction_fn):
            raise ConfigurationError(
                "AmplitudeMCPAnalytics: custom_redaction_fn must be a function."
            )
        self._custom_redaction_fn = custom_redaction_fn
        self.custom_patterns, self._compiled = _compile_custom_patterns(
            custom_redaction_patterns
        )
        self._warned_custom_redaction_failure = False

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
            # Replace an image before text rules can punch holes in the payload
            # (`AAA` inside a PNG would otherwise survive as mixed text). Check
            # again afterwards in case a custom rule rewrote a value into one.
            if is_base64_data_url(value) or is_raw_base64(value):
                return REDACTED_IMAGE_PLACEHOLDER
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
            except (re.error, IndexError) as exc:
                # The pattern source can be the literal the rule exists to hide,
                # and a bad replacement (`\g<missing>`) raises IndexError rather
                # than re.error. Log the type only.
                self._warn_custom_redaction_failure(
                    f"custom_redaction_patterns failed ({type(exc).__name__})"
                )
                return REDACTED_CONTENT_PLACEHOLDER
        return result

    def _apply_custom_fn(self, text: str) -> str:
        if self._custom_redaction_fn is None:
            return text
        try:
            result: Any = self._custom_redaction_fn(text)
        except Exception as exc:  # noqa: BLE001 — replace the value; do not log the message
            # The exception message may echo the input, so only its type is logged.
            self._warn_custom_redaction_failure(
                f"custom_redaction_fn threw {type(exc).__name__}"
            )
            return REDACTED_CONTENT_PLACEHOLDER
        if isinstance(result, str):
            return result
        self._warn_custom_redaction_failure(
            f"custom_redaction_fn returned {type(result).__name__} instead of str"
        )
        return REDACTED_CONTENT_PLACEHOLDER

    def _warn_custom_redaction_failure(self, detail: str) -> None:
        if self._warned_custom_redaction_failure:
            return
        self._warned_custom_redaction_failure = True
        get_logger().error(
            'AmplitudeMCPAnalytics: %s; content replaced with "%s". '
            "Further failures from this config are not logged.",
            detail,
            REDACTED_CONTENT_PLACEHOLDER,
        )


def _compile_custom_patterns(
    patterns: Sequence[Any] | None,
) -> tuple[tuple[Any, ...], tuple[tuple[re.Pattern[str], str], ...]]:
    """Compile caller patterns. Invalid entries raise rather than being skipped.

    A typo has to fail at configuration time. Skipping the rule would leave
    that text unredacted with only a log line to show for it.
    """
    if patterns is None:
        return (), ()
    if not isinstance(patterns, (list, tuple)):
        raise ConfigurationError(
            "AmplitudeMCPAnalytics: custom_redaction_patterns must be a list of "
            "strings or {pattern, replacement} mappings."
        )
    compiled: list[tuple[re.Pattern[str], str]] = []
    for index, pattern in enumerate(patterns):
        source, replacement = _pattern_source(pattern)
        if source is None or replacement is None:
            raise ConfigurationError(
                "AmplitudeMCPAnalytics: "
                f"custom_redaction_patterns[{index}] must be a string or "
                "{pattern, replacement}."
            )
        try:
            compiled.append((re.compile(source), replacement))
        except re.error as exc:
            raise ConfigurationError(
                "AmplitudeMCPAnalytics: "
                f"custom_redaction_patterns[{index}] is not a valid regular "
                f"expression: {exc}"
            ) from exc
    return tuple(patterns), tuple(compiled)


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
