"""Port of the Node repo's test/core/privacy.test.ts."""

from __future__ import annotations

import logging

import pytest

from amplitude_mcp_analytics.core.privacy import (
    REDACTED_IMAGE_PLACEHOLDER,
    PrivacyConfig,
    create_content_hash,
    is_base64_data_url,
    is_raw_base64,
    is_valid_url,
    redact_base64_content,
    redact_pii_patterns,
)

PNG_BASE64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8A"
    "AAAASUVORK5CYII="
)


class TestIsBase64DataUrl:
    def test_detects_base64_data_urls(self) -> None:
        assert is_base64_data_url("data:image/png;base64,iVBOR...") is True
        assert is_base64_data_url("https://example.com") is False


class TestIsValidUrl:
    def test_recognizes_absolute_and_relative_urls(self) -> None:
        assert is_valid_url("https://example.com/path") is True
        assert is_valid_url("./relative/path") is True
        assert is_valid_url("../up") is True
        assert is_valid_url("not a url") is False


class TestIsRawBase64:
    def test_detects_raw_base64_strings(self) -> None:
        assert is_raw_base64(PNG_BASE64) is True
        assert is_raw_base64("short") is False
        assert is_raw_base64("https://example.com") is False

    def test_does_not_flag_ulids_hex_tokens_or_uuids_without_dashes(self) -> None:
        assert is_raw_base64("01KRESR2V3E22E29C3JBB8FR8Z") is False
        assert is_raw_base64("01ARZ3NDEKTSV4RRFFQ69G5FAV") is False
        assert is_raw_base64("a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4") is False
        assert is_raw_base64("550e8400e29b41d4a716446655440000") is False


class TestCreateContentHash:
    def test_returns_a_stable_sha256_hash_and_an_empty_string_for_none(self) -> None:
        assert create_content_hash("hello") == create_content_hash("hello")
        assert len(create_content_hash("hello")) == 64
        assert create_content_hash("input-a") != create_content_hash("input-b")
        assert create_content_hash(None) == ""


class TestRedactBase64Content:
    def test_redacts_base64_data_urls_and_raw_base64_images(self) -> None:
        assert redact_base64_content("data:image/png;base64,iVBOR") == REDACTED_IMAGE_PLACEHOLDER
        assert redact_base64_content(PNG_BASE64) == REDACTED_IMAGE_PLACEHOLDER

    def test_leaves_non_strings_unchanged(self) -> None:
        assert redact_base64_content(42) == 42
        assert redact_base64_content(None) is None


class TestRedactPiiPatterns:
    def test_redacts_emails_phones_ssns_credit_cards_and_ip_addresses(self) -> None:
        assert redact_pii_patterns("Contact user@example.com for info") == "Contact [email] for info"
        assert redact_pii_patterns("Call (555) 123-4567") == "Call ([phone]"
        assert redact_pii_patterns("Call +14155552671 today") == "Call [phone] today"
        assert redact_pii_patterns("SSN: 123-45-6789") == "SSN: [ssn]"
        assert redact_pii_patterns("SSN: 123 45 6789") == "SSN: [ssn]"
        assert redact_pii_patterns("Card: 4111 1111 1111 1111") == "Card: [credit_card]"
        assert redact_pii_patterns("Server at 192.168.1.1 is down") == "Server at [ip_address] is down"
        assert redact_pii_patterns("IPv6: 2001:0db8:85a3:0000:0000:8a2e:0370:7334") == (
            "IPv6: [ip_address]"
        )
        assert redact_pii_patterns("loopback ::1") == "loopback [ip_address]"
        assert redact_pii_patterns("see http://[::1]:8080/health") == (
            "see http://[ip_address]:8080/health"
        )

    def test_does_not_treat_scope_resolution_operators_as_ipv6(self) -> None:
        assert redact_pii_patterns("std::vector and a[::2]") == "std::vector and a[::2]"

    def test_redacts_repeatedly(self) -> None:
        # Node's global RegExp lastIndex can skip a later call. Python `re.sub`
        # has no equivalent, and this locks that in.
        assert redact_pii_patterns("a@b.com") == "[email]"
        assert redact_pii_patterns("c@d.com") == "[email]"

    def test_returns_non_strings_unchanged(self) -> None:
        assert redact_pii_patterns(42) == 42


class TestPrivacyConfig:
    def test_walks_nested_objects_lists_and_tuples_and_leaves_keys_and_primitives(
        self,
    ) -> None:
        privacy = PrivacyConfig()
        assert privacy.redact_value(
            {
                "note": "reach user@x.com",
                "count": 3,
                "ok": True,
                "empty": None,
                "nested": {"email": "a@b.com"},
                "list": ["c@d.com", 1],
                "pair": ("e@f.com", 2),
            }
        ) == {
            "note": "reach [email]",
            "count": 3,
            "ok": True,
            "empty": None,
            "nested": {"email": "[email]"},
            "list": ["[email]", 1],
            "pair": ("[email]", 2),
        }

    def test_replaces_a_value_that_is_entirely_a_base64_image(self) -> None:
        privacy = PrivacyConfig(redact_pii=False)
        assert privacy.redact_value(PNG_BASE64) == REDACTED_IMAGE_PLACEHOLDER
        assert privacy.redact_value(f"data:image/png;base64,{PNG_BASE64}") == (
            REDACTED_IMAGE_PLACEHOLDER
        )

    def test_skips_an_invalid_custom_regex_and_still_applies_the_valid_ones(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.WARNING, logger="amplitude_mcp_analytics")
        privacy = PrivacyConfig(redact_pii=False, custom_redaction_patterns=["(", r"secret-\d+"])
        assert privacy.redact_text("token secret-99") == "token [REDACTED]"
        assert any("invalid custom redaction regex" in record.getMessage() for record in caplog.records)

    def test_applies_a_pattern_object_replacement_after_builtin_patterns(self) -> None:
        privacy = PrivacyConfig(
            custom_redaction_patterns=[{"pattern": r"\bACME-\d+\b", "replacement": "[ticket]"}]
        )
        assert privacy.redact_text("user@x.com filed ACME-12") == "[email] filed [ticket]"

    def test_keeps_the_current_text_when_custom_redaction_fn_throws_or_returns_a_non_string(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.ERROR, logger="amplitude_mcp_analytics")

        def throwing(_text: str) -> str:
            raise RuntimeError("redactor bug")

        throwing_privacy = PrivacyConfig(redact_pii=False, custom_redaction_fn=throwing)
        assert throwing_privacy.redact_text("keep me") == "keep me"

        def wrong_type(_text: str) -> str:
            return 42  # type: ignore[return-value]

        wrong = PrivacyConfig(redact_pii=False, custom_redaction_fn=wrong_type)
        assert wrong.redact_text("keep me") == "keep me"
        assert len(caplog.records) == 2
