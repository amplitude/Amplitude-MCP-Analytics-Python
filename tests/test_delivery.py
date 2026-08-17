"""Ports the Node repo's test/delivery.test.ts.

Node's TrackingProxy + installTrackCounter + installTrackHook are one class in
the Python port: ``DeliveryClient(raw, config, on_tracked=...)``. The hook
ordering contract is identical — the dry-run gate sits outermost, so dry-run
events are never counted as unflushed.

Adaptations for the Python design:
- console.warn spying becomes ``caplog`` on the SDK's own
  ``amplitude_mcp_analytics`` logger; the debug line is a DEBUG record on that
  logger, while the dry-run line still prints to stderr (read via capsys) so a
  dry run is visible with no logging configured.
- The transport-level >=400 delivery warning rides a per-event callback on
  each BaseEvent (built only for a real ``amplitude.Amplitude`` client), not a
  composed client-level ``configuration.callback`` — so there is no "preserves
  an existing callback" case to port. It logs through the injected
  ``logging.Logger``, so those cases still use a ListLogger double.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest
from amplitude import Amplitude

from amplitude_mcp_analytics import AmplitudeMCPAnalytics, MCPAnalyticsConfig
from amplitude_mcp_analytics.core import delivery
from amplitude_mcp_analytics.core.delivery import (
    DeliveryClient,
    get_global_unflushed_count,
    increment_unflushed_count,
    is_serverless,
    register_exit_hook,
    settle_unflushed_count,
)
from amplitude_mcp_analytics.types import AmplitudeEvent
from conftest import ListLogger

EVENT: AmplitudeEvent = {"event_type": "[MCP] Test", "user_id": "user-123"}


class RawClient:
    """Minimal fake client. The SDK logs to its own ``amplitude_mcp_analytics``
    logger regardless of the injected client, so warnings are read via
    ``caplog``, not off this double."""

    def __init__(self) -> None:
        self.tracked: list[Any] = []
        self.flush_calls = 0
        self.shutdown_calls = 0

    def track(self, event: Any) -> None:
        self.tracked.append(event)

    def flush(self) -> list[Any]:
        self.flush_calls += 1
        return []

    def shutdown(self) -> None:
        self.shutdown_calls += 1


def build_client(raw: Any, config: MCPAnalyticsConfig) -> DeliveryClient:
    """Wire a delivery client the same way the analytics constructor does."""
    return DeliveryClient(raw, config)


class TestDryRun:
    def test_skips_the_underlying_track(self, capsys: pytest.CaptureFixture[str]) -> None:
        raw = RawClient()
        client = build_client(raw, MCPAnalyticsConfig(dry_run=True))

        client.track(EVENT)

        assert raw.tracked == []
        # The event JSON still lands on stderr so a dry run is observable.
        assert "[MCP] Test" in capsys.readouterr().err

    def test_does_not_count_dry_run_events_as_unflushed(self) -> None:
        raw = RawClient()
        client = build_client(raw, MCPAnalyticsConfig(dry_run=True))

        client.track(EVENT)

        # The dry-run gate sits outermost, so it short-circuits before the counter runs.
        assert get_global_unflushed_count() == 0


class TestDebug:
    def test_emits_a_log_line_and_still_delivers(
        self, caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture[str]
    ) -> None:
        caplog.set_level(logging.DEBUG, logger="amplitude_mcp_analytics")
        raw = RawClient()
        client = build_client(raw, MCPAnalyticsConfig(debug=True))

        client.track(EVENT)

        assert len(raw.tracked) == 1
        # Debug rides the SDK's logger (host-routable), not a stderr print.
        logged = caplog.text
        # Placeholder debug line emits only the event type for now (pending the
        # MCP event taxonomy — see format_debug_line).
        assert "[amplitude-mcp-analytics]" in logged
        assert "[MCP] Test" in logged
        assert [r.levelno for r in caplog.records] == [logging.DEBUG]
        assert capsys.readouterr().err == ""

    def test_emits_nothing_when_debug_is_off(self, caplog: pytest.LogCaptureFixture) -> None:
        caplog.set_level(logging.DEBUG, logger="amplitude_mcp_analytics")
        client = build_client(RawClient(), MCPAnalyticsConfig())

        client.track(EVENT)

        assert caplog.records == []


class TestShortIdWarning:
    def test_fires_once_per_field_value(self, caplog: pytest.LogCaptureFixture) -> None:
        caplog.set_level(logging.WARNING, logger="amplitude_mcp_analytics")
        raw = RawClient()
        client = build_client(raw, MCPAnalyticsConfig())

        def warned() -> int:
            return sum("shorter than 5 characters" in r.getMessage() for r in caplog.records)

        client.track({"event_type": "e", "user_id": "ab"})
        client.track({"event_type": "e", "user_id": "ab"})
        assert warned() == 1

        # A different value warns again.
        client.track({"event_type": "e", "user_id": "cd"})
        assert warned() == 2

        # A different field warns again.
        client.track({"event_type": "e", "device_id": "xy"})
        assert warned() == 3


class TestDeliveryFailureCallback:
    """The >=400 warning rides a per-BaseEvent callback, built only when the
    raw client is a real ``amplitude.Amplitude`` instance."""

    @staticmethod
    def _real_client() -> DeliveryClient:
        raw = Amplitude("test-api-key-000")
        # Keep the real client from ever enqueueing/delivering over the network.
        raw.configuration.opt_out = True
        return DeliveryClient(raw, MCPAnalyticsConfig())

    def test_surfaces_4xx_5xx_via_the_per_event_callback(self) -> None:
        client = self._real_client()
        # Exercises the real-client conversion path end-to-end (is_real_amplitude).
        client.track(EVENT)

        logger = ListLogger()
        event_obj = client._convert(EVENT, logger)  # type: ignore[arg-type]
        # BaseEvent.callback(status, message) invokes the stored per-event
        # delivery callback with the event object itself.
        event_obj.callback(500, "boom")

        logged = "\n".join(logger.warnings)
        assert "delivery failed" in logged
        assert "HTTP 500" in logged

    def test_does_not_warn_on_a_2xx_delivery(self) -> None:
        client = self._real_client()

        logger = ListLogger()
        event_obj = client._convert(EVENT, logger)  # type: ignore[arg-type]
        event_obj.callback(200, "ok")

        assert not any("delivery failed" in w for w in logger.warnings)

    def test_conversion_maps_the_event_fields_onto_the_base_event(self) -> None:
        client = self._real_client()
        logger = ListLogger()

        event_obj = client._convert(  # type: ignore[arg-type]
            {
                "event_type": "[MCP] Test",
                "user_id": "user-123",
                "device_id": "device-456",
                "event_properties": {"foo": "bar"},
                "groups": {"org id": "42"},
            },
            logger,
        )

        assert event_obj.event_type == "[MCP] Test"
        assert event_obj.user_id == "user-123"
        assert event_obj.device_id == "device-456"
        assert event_obj.event_properties == {"foo": "bar"}
        assert event_obj.groups == {"org id": "42"}


class TestUnflushedCounter:
    def test_increments_per_track_at_the_delivery_level(self) -> None:
        raw = RawClient()
        client = build_client(raw, MCPAnalyticsConfig())

        client.track(EVENT)
        client.track(EVENT)
        client.track(EVENT)

        assert get_global_unflushed_count() == 3
        assert len(raw.tracked) == 3

    # Settling lives on the client (not the delivery wrapper) because it must
    # run whether or not we own the underlying client — mirroring AI-Node.
    @staticmethod
    def make_client(raw: RawClient) -> AmplitudeMCPAnalytics:
        return AmplitudeMCPAnalytics(
            amplitude=raw,
            server_name="test-server",
            server_version="0.0.0",
        )

    def test_client_flush_settles_the_global_counter_and_resets(self) -> None:
        raw = RawClient()
        analytics = self.make_client(raw)

        analytics.track(EVENT)
        analytics.track(EVENT)
        analytics.track(EVENT)
        assert get_global_unflushed_count() == 3

        analytics.flush()

        assert get_global_unflushed_count() == 0
        assert raw.flush_calls == 1
        # A second flush stays at zero (clamped — never goes negative).
        analytics.flush()
        assert get_global_unflushed_count() == 0

    def test_client_shutdown_settles_but_does_not_tear_down_a_borrowed_client(self) -> None:
        raw = RawClient()
        analytics = self.make_client(raw)

        analytics.track(EVENT)
        analytics.track(EVENT)
        assert get_global_unflushed_count() == 2

        analytics.shutdown()

        assert get_global_unflushed_count() == 0
        # _owns_client is False for a caller-supplied client.
        assert raw.shutdown_calls == 0

    def test_client_shutdown_tears_down_a_client_it_owns(self) -> None:
        raw = RawClient()
        analytics = self.make_client(raw)
        # Simulate the api_key path, which sets _owns_client = True.
        analytics._owns_client = True

        analytics.track(EVENT)
        analytics.shutdown()

        assert get_global_unflushed_count() == 0
        assert raw.shutdown_calls == 1

    def test_settle_is_clamped_at_zero(self) -> None:
        increment_unflushed_count()
        increment_unflushed_count()

        settle_unflushed_count(5)

        # Over-settling never pushes the counter negative.
        assert get_global_unflushed_count() == 0


class TestServerless:
    """Python module surface for the serverless exit accounting (the Node SDK
    keeps this in src/core/delivery/serverless.ts)."""

    _ENV_VARS = (
        "AWS_LAMBDA_FUNCTION_NAME",
        "VERCEL",
        "NETLIFY",
        "FUNCTION_TARGET",
        "WEBSITE_INSTANCE_ID",
        "CF_PAGES",
    )

    @staticmethod
    def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
        for var in TestServerless._ENV_VARS:
            monkeypatch.delenv(var, raising=False)

    def test_is_serverless_false_outside_known_platforms(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._clear_env(monkeypatch)
        delivery._reset_serverless_cache()

        assert is_serverless() is False

    @pytest.mark.parametrize("var", _ENV_VARS)
    def test_is_serverless_detects_each_platform_env_var(
        self, monkeypatch: pytest.MonkeyPatch, var: str
    ) -> None:
        self._clear_env(monkeypatch)
        monkeypatch.setenv(var, "some-value")
        delivery._reset_serverless_cache()

        assert is_serverless() is True

    def test_is_serverless_caches_its_first_answer(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._clear_env(monkeypatch)
        monkeypatch.setenv("VERCEL", "1")
        delivery._reset_serverless_cache()
        assert is_serverless() is True

        monkeypatch.delenv("VERCEL")
        # Still True: the env is only consulted once per cache lifetime.
        assert is_serverless() is True

        delivery._reset_serverless_cache()
        assert is_serverless() is False

    def test_register_exit_hook_is_idempotent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        registrations: list[Any] = []
        monkeypatch.setattr(delivery, "_exit_hook_registered", False)
        monkeypatch.setattr(delivery.atexit, "register", registrations.append)

        register_exit_hook()
        register_exit_hook()
        register_exit_hook()

        assert len(registrations) == 1
