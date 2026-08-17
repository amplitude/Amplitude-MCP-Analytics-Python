"""Port of the Node repo's test/tracking/track-server-event.test.ts."""

from __future__ import annotations

import logging
from typing import Any

import pytest

from amplitude_mcp_analytics.context.factory import create_server_context
from amplitude_mcp_analytics.context.types import (
    McpAnchor,
    McpIdentity,
    McpServerInfo,
    McpTenant,
)
from amplitude_mcp_analytics.tracking.track import track_server_event
from amplitude_mcp_analytics.tracking.types import TrackEventOptions
from conftest import make_amplitude


def resolved_ctx(**overrides: Any) -> Any:
    defaults: dict[str, Any] = {
        "server": McpServerInfo(name="my-server", version="1.0.0"),
        "transport": "streamable-http",
        "identity": McpIdentity(user_id="u1", resolved_from="explicit"),
        "tenant": McpTenant(group_type="org id", group_value="36958"),
        "anchor": McpAnchor(type="session-id", value="sess-1"),
    }
    defaults.update(overrides)
    return create_server_context(**defaults)


class ThrowingAmplitude:
    """Structural client whose track() always raises; the SDK's warning lands on
    its own `amplitude_mcp_analytics` logger (see utils/logger.get_logger)."""

    def track(self, event: Any) -> None:
        raise RuntimeError("amplitude broke")

    def flush(self) -> None:
        return None


class TestTrackServerEvent:
    def test_emits_an_event_with_the_configured_name_and_inherited_ctx_properties(self) -> None:
        client = make_amplitude()
        track_server_event(client, resolved_ctx(), "mcp: custom server event")

        assert len(client.tracked) == 1
        event = client.tracked[0]
        assert event["event_type"] == "mcp: custom server event"
        assert event["user_id"] == "u1"
        assert event["groups"] == {"org id": "36958"}
        props = event["event_properties"]
        assert props["[MCP] Session ID"] == "sess-1"
        assert props["[MCP] Server Name"] == "my-server"
        assert props["[MCP] Client Name"] == "unknown"
        # Tenant is carried on `groups`, not as an event property.
        assert event["groups"] == {"org id": "36958"}

    def test_merges_caller_properties_which_win_over_reserved_on_collision(self) -> None:
        client = make_amplitude()

        track_server_event(
            client,
            resolved_ctx(),
            "mcp: custom server event",
            {
                "query type": "cohort",
                # Collides with the reserved '[MCP] Client Name' → caller wins.
                "[MCP] Client Name": "custom-client-name",
            },
        )

        props = client.tracked[0]["event_properties"]
        assert props["query type"] == "cohort"
        assert props["[MCP] Client Name"] == "custom-client-name"  # caller wins
        assert props["[MCP] Session ID"] == "sess-1"

    def test_omits_the_ctx_extra_bag_when_drop_extra_props_is_set(self) -> None:
        client = make_amplitude()
        ctx = resolved_ctx(
            server=McpServerInfo(name="my-server"),
            transport="streamable-http",
            identity=McpIdentity(user_id="u1", resolved_from="explicit"),
            extra={"org url": "amplitude"},
        )
        track_server_event(
            client, ctx, "mcp: no extra", None, TrackEventOptions(drop_extra_props=True)
        )

        assert "org url" not in client.tracked[0]["event_properties"]

    def test_drops_emission_when_identity_is_anonymous_and_no_tenant_skip_rule(self) -> None:
        client = make_amplitude()
        ctx = create_server_context(server=McpServerInfo(name="my-server"), transport="stdio")
        track_server_event(client, ctx, "mcp: never emitted")

        assert len(client.tracked) == 0

    def test_emits_when_tenant_is_set_even_with_anonymous_identity(self) -> None:
        # e.g. the auth org mismatch path
        client = make_amplitude()
        ctx = create_server_context(
            server=McpServerInfo(name="my-server"),
            transport="streamable-http",
            tenant=McpTenant(group_type="org id", group_value="36958"),
        )
        track_server_event(client, ctx, "mcp: auth org mismatch")

        assert len(client.tracked) == 1

    def test_swallows_underlying_client_errors_and_never_raises(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        # best-effort isolation
        caplog.set_level(logging.WARNING, logger="amplitude_mcp_analytics")
        client = ThrowingAmplitude()

        track_server_event(client, resolved_ctx(), "mcp: failing event")  # must not raise

        assert len(caplog.records) == 1
        warning = caplog.records[0].getMessage()
        assert "mcp: failing event" in warning
        assert "amplitude broke" in warning

    def test_emits_ctx_extra_values_host_domain_enrichment_onto_event_properties(self) -> None:
        client = make_amplitude()
        ctx = resolved_ctx(
            server=McpServerInfo(name="my-server"),
            transport="streamable-http",
            identity=McpIdentity(user_id="u1", resolved_from="explicit"),
            extra={"org url": "amplitude", "user email": "a@b.com"},
        )
        track_server_event(client, ctx, "mcp: enriched event")

        props = client.tracked[0]["event_properties"]
        assert props["org url"] == "amplitude"
        assert props["user email"] == "a@b.com"

    def test_caller_properties_win_over_ctx_extra_values(self) -> None:
        # precedence chain: typed < extra < caller
        client = make_amplitude()
        ctx = resolved_ctx(
            server=McpServerInfo(name="my-server"),
            transport="streamable-http",
            identity=McpIdentity(user_id="u1", resolved_from="explicit"),
            extra={"user email": "from-extra@x.com"},
        )
        track_server_event(client, ctx, "mcp: collision", {"user email": "from-caller@x.com"})

        assert client.tracked[0]["event_properties"]["user email"] == "from-caller@x.com"
