"""Port of the Node repo's test/config.test.ts."""

from __future__ import annotations

from amplitude_mcp_analytics.config import MCPAnalyticsConfig, ResolvedAutocapture

ALL_ON = ResolvedAutocapture(
    server_events=True, session_lifecycle=True, tools_listed=True, tool_calls=True
)


class TestAutocaptureNormalization:
    def test_defaults_every_family_on_when_unset(self) -> None:
        assert MCPAnalyticsConfig().autocapture == ALL_ON

    def test_treats_true_as_all_on_and_false_as_all_off(self) -> None:
        assert MCPAnalyticsConfig(autocapture=True).autocapture == ALL_ON
        assert MCPAnalyticsConfig(autocapture=False).autocapture == ResolvedAutocapture(
            server_events=False,
            session_lifecycle=False,
            tools_listed=False,
            tool_calls=False,
        )

    def test_toggles_families_independently_defaulting_the_omitted_ones_on(self) -> None:
        assert MCPAnalyticsConfig(
            autocapture={"server_events": False}
        ).autocapture == ResolvedAutocapture(
            server_events=False,
            session_lifecycle=False,
            tools_listed=False,
            tool_calls=True,
        )
        assert MCPAnalyticsConfig(
            autocapture={"tool_calls": False}
        ).autocapture == ResolvedAutocapture(
            server_events=True,
            session_lifecycle=True,
            tools_listed=True,
            tool_calls=False,
        )

    def test_sub_families_default_to_the_server_events_umbrella_and_override_it(self) -> None:
        # Umbrella off, one sub-family re-enabled — the per-request-server shape.
        assert MCPAnalyticsConfig(
            autocapture={"server_events": False, "tools_listed": True}
        ).autocapture == ResolvedAutocapture(
            server_events=False,
            session_lifecycle=False,
            tools_listed=True,
            tool_calls=True,
        )

        # Umbrella on (default), one sub-family opted out.
        assert MCPAnalyticsConfig(
            autocapture={"session_lifecycle": False}
        ).autocapture == ResolvedAutocapture(
            server_events=True,
            session_lifecycle=False,
            tools_listed=True,
            tool_calls=True,
        )


class TestEmitAnonymousEvent:
    def test_defaults_to_false(self) -> None:
        # Node checks both `new MCPAnalyticsConfig()` and `({})`; Python has a
        # single no-args spelling.
        assert MCPAnalyticsConfig().emit_anonymous_event is False
        assert MCPAnalyticsConfig().emit_anonymous_event is False

    def test_honors_an_explicit_true(self) -> None:
        assert MCPAnalyticsConfig(emit_anonymous_event=True).emit_anonymous_event is True
