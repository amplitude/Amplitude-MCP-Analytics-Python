"""Ports test/feedback-tool.test.ts — the opt-in submit_feedback tool.

The live case drives a real FastMCP server over the in-memory transport.
Schema failures arrive as in-band isError results (the Python SDK's post-1.21
shape) and never reach the feedback callback.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest
from mcp.server.fastmcp import FastMCP
from mcp.shared.memory import create_connected_server_and_client_session
from mcp.types import Implementation

from amplitude_mcp_analytics import (
    FEEDBACK_TOOL_DEFINITION as PUBLIC_DEFINITION,
)
from amplitude_mcp_analytics import (
    create_server_context,
    feedback_tool_definition,
)
from amplitude_mcp_analytics.context.types import McpServerInfo
from amplitude_mcp_analytics.core.feedback_tool import (
    FEEDBACK_RECORDED_TEXT,
    FEEDBACK_TOOL_DEFINITION,
    create_feedback_handler,
)
from amplitude_mcp_analytics.core.identity import ServerIdentity
from amplitude_mcp_analytics.testing import MockAmplitudeMCPAnalytics
from amplitude_mcp_analytics.types import AmplitudeEvent

SUBMITTED = "[MCP] Feedback Submitted"
RESPONSE = "[MCP] Tool Call Response"
REJECTED = "[MCP] Tool Call Rejected"
CLIENT = Implementation(name="cursor", version="0.40")


def _registry(*names: str) -> SimpleNamespace:
    return SimpleNamespace(
        _tool_manager=SimpleNamespace(_tools={name: object() for name in names})
    )


def _handler(
    tracked: list[AmplitudeEvent],
    *,
    transport: str = "streamable-http",
    identity: ServerIdentity | None = None,
    identity_resolver=None,
    emit_anonymous_event: bool | None = None,
    sanitize=None,
    privacy=None,
    logger=None,
    server=None,
    capture_comment: bool = False,
):
    from amplitude_mcp_analytics.core.feedback_tool import FeedbackToolDependencies

    ctx = create_server_context(
        server=McpServerInfo(name="svc", version="1.0.0"),
        transport=transport,  # type: ignore[arg-type]
        emit_anonymous_event=emit_anonymous_event,
    )
    options: dict = {}
    if capture_comment:
        options["capture_comment"] = True
    if server is not None:
        options["server"] = server
    return create_feedback_handler(
        FeedbackToolDependencies(
            amplitude=SimpleNamespace(track=tracked.append, flush=lambda: None),
            get_server_ctx=lambda: ctx,
            get_server_identity=lambda: identity,
            get_client_info_resolver=lambda: None,
            get_identity_resolver=None if identity_resolver is None else lambda: identity_resolver,
            sanitize_error_message=sanitize,
            privacy=privacy,
            logger=logger,
        ),
        options or None,
    )


def _feedback(tracked: list[AmplitudeEvent]) -> list[AmplitudeEvent]:
    return [event for event in tracked if event.get("event_type") == SUBMITTED]


def test_default_definition_omits_comment() -> None:
    assert PUBLIC_DEFINITION is FEEDBACK_TOOL_DEFINITION
    assert FEEDBACK_TOOL_DEFINITION["name"] == "submit_feedback"
    assert FEEDBACK_TOOL_DEFINITION["title"] == "Submit feedback on this server"
    assert FEEDBACK_TOOL_DEFINITION["annotations"] == {
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    }
    assert "thanks, that's it" in FEEDBACK_TOOL_DEFINITION["description"]
    assert "that's wrong" in FEEDBACK_TOOL_DEFINITION["description"]
    assert FEEDBACK_TOOL_DEFINITION["inputSchema"]["required"] == ["helpful"]
    assert "wrong_result" in FEEDBACK_TOOL_DEFINITION["inputSchema"]["properties"]["reason"]["enum"]
    assert "comment" not in FEEDBACK_TOOL_DEFINITION["inputSchema"]["properties"]


def test_comment_field_and_invalid_name() -> None:
    with_comment = feedback_tool_definition(capture_comment=True, name="rate_help")
    assert with_comment is not None
    assert with_comment["name"] == "rate_help"
    assert with_comment["inputSchema"]["properties"]["comment"]["maxLength"] == 500
    assert "agreed to share" in with_comment["description"]
    assert feedback_tool_definition(name="has a space") is None


def test_emits_feedback_and_not_a_tool_call_response() -> None:
    tracked: list[AmplitudeEvent] = []
    handler = _handler(
        tracked,
        identity=ServerIdentity(user_id="user-1"),
        server=_registry("ping", "submit_feedback"),
    )
    result = handler(
        {
            "helpful": False,
            "reason": "wrong_result",
            "solicited": False,
            "tools": ["ping", "not-a-tool", "ping", "submit_feedback"],
            "comment": "should be dropped",
        }
    )
    assert result["content"] == [{"type": "text", "text": FEEDBACK_RECORDED_TEXT}]
    assert "isError" not in result
    assert [event["event_type"] for event in tracked] == [SUBMITTED]
    assert tracked[0]["user_id"] == "user-1"
    props = tracked[0]["event_properties"]
    assert props["[MCP] Tool Name"] == "submit_feedback"
    assert props["[MCP] Feedback Helpful"] is False
    assert props["[MCP] Feedback Reason"] == "wrong_result"
    assert props["[MCP] Feedback Solicited"] is False
    assert props["[MCP] Feedback Tool Names"] == ["ping", "submit_feedback"]
    assert props["[MCP] Feedback Has Comment"] is False
    assert "[MCP] Feedback Comment" not in props


def test_redacts_comment_only_when_capture_is_on() -> None:
    tracked: list[AmplitudeEvent] = []
    handler = _handler(
        tracked,
        transport="stdio",
        identity=ServerIdentity(user_id="user-1"),
        sanitize=lambda message: message.replace("secret", "<redacted>"),
        capture_comment=True,
    )
    handler({"helpful": True, "comment": f"hello secret {'x' * 600}"})
    props = tracked[0]["event_properties"]
    assert props["[MCP] Feedback Has Comment"] is True
    comment = props["[MCP] Feedback Comment"]
    assert isinstance(comment, str)
    assert "<redacted>" in comment
    assert "secret" not in comment
    assert len(comment) <= 500


def test_keeps_has_comment_when_the_sanitizer_drops_the_text() -> None:
    tracked: list[AmplitudeEvent] = []
    handler = _handler(
        tracked,
        transport="stdio",
        identity=ServerIdentity(user_id="user-1"),
        sanitize=lambda _message: None,
        capture_comment=True,
    )
    handler({"helpful": True, "comment": "jane@example.com"})
    props = tracked[0]["event_properties"]
    assert props["[MCP] Feedback Has Comment"] is True
    assert "[MCP] Feedback Comment" not in props


def test_emits_with_the_identity_resolver_when_stateless_http_has_no_anchor() -> None:
    from amplitude_mcp_analytics import SetIdentityInput

    tracked: list[AmplitudeEvent] = []
    handler = _handler(
        tracked,
        identity_resolver=lambda _auth: SetIdentityInput(user_id="user-1"),
    )
    handler({"helpful": True})
    assert len(tracked) == 1
    assert tracked[0]["event_type"] == SUBMITTED
    assert tracked[0]["user_id"] == "user-1"


def test_caps_a_comment_that_redaction_lengthens() -> None:
    from amplitude_mcp_analytics.core.privacy import PrivacyConfig

    tracked: list[AmplitudeEvent] = []
    handler = _handler(
        tracked,
        transport="stdio",
        identity=ServerIdentity(user_id="user-1"),
        capture_comment=True,
        privacy=PrivacyConfig(
            redact_pii=False,
            custom_redaction_patterns=[{"pattern": "X", "replacement": "Y" * 600}],
        ),
    )
    handler({"helpful": True, "comment": "see X"})
    comment = tracked[0]["event_properties"]["[MCP] Feedback Comment"]
    assert comment == "see " + ("Y" * 496)
    assert len(comment) == 500
    assert "X" not in comment


def test_standalone_callback_uses_the_resolved_name_and_description() -> None:
    from amplitude_mcp_analytics import create_feedback_tool_handler

    analytics = MockAmplitudeMCPAnalytics(server_name="test-mcp", server_version="9.9.9")
    callback = create_feedback_tool_handler(
        analytics, name="rate_help", description="Custom instructions"
    )
    assert callback.__name__ == "rate_help"
    assert callback.__doc__ == "Custom instructions"

    default = create_feedback_tool_handler(analytics, description="   ")
    assert default.__name__ == "submit_feedback"
    assert default.__doc__ is not None
    assert "thanks, that's it" in default.__doc__

    mcp = FastMCP("s")
    analytics.register_feedback_tool(
        mcp, name="rate-help", description="Custom instructions", capture_comment=True
    )
    registered = mcp._tool_manager.get_tool("rate-help")
    assert registered is not None
    assert registered.description == "Custom instructions"
    properties = registered.parameters["properties"]
    assert set(properties) == {"helpful", "reason", "comment", "tools", "solicited"}
    assert properties["comment"]["anyOf"][0]["maxLength"] == 500
    assert properties["tools"]["anyOf"][0]["maxItems"] == 8
    assert registered.parameters["required"] == ["helpful"]


def test_drops_the_anonymous_floor_unless_emit_anonymous_event_is_set() -> None:
    tracked: list[AmplitudeEvent] = []
    anonymous = _handler(tracked)
    anonymous({"helpful": True})
    assert tracked == []

    opted_in = _handler(tracked, emit_anonymous_event=True)
    opted_in({"helpful": True})
    assert len(_feedback(tracked)) == 1


def test_answers_without_emitting_when_the_server_was_never_instrumented(caplog) -> None:
    tracked: list[AmplitudeEvent] = []
    logger = logging.getLogger("test.feedback")
    from amplitude_mcp_analytics.core.feedback_tool import FeedbackToolDependencies

    handler = create_feedback_handler(
        FeedbackToolDependencies(
            amplitude=SimpleNamespace(track=tracked.append, flush=lambda: None),
            get_server_ctx=lambda: None,
            get_server_identity=lambda: None,
            get_client_info_resolver=lambda: None,
            logger=logger,
        )
    )
    with caplog.at_level(logging.WARNING, logger="test.feedback"):
        result = handler({"helpful": True})
        assert "isError" not in result
        assert tracked == []
        assert len(caplog.records) == 1
        handler({"helpful": False})
        assert len(caplog.records) == 1


def test_returns_an_error_and_emits_nothing_when_helpful_is_missing() -> None:
    tracked: list[AmplitudeEvent] = []
    handler = _handler(tracked, transport="stdio", identity=ServerIdentity(user_id="user-1"))
    result = handler({"reason": "other"})
    assert result["isError"] is True
    assert tracked == []


def test_warns_and_does_nothing_for_a_low_level_server(caplog) -> None:
    analytics = MockAmplitudeMCPAnalytics(server_name="test-mcp", server_version="9.9.9")
    server = object()
    with caplog.at_level(logging.WARNING, logger="amplitude_mcp_analytics"):
        assert analytics.register_feedback_tool(server) is server
    assert any("FastMCP" in record.message for record in caplog.records)


def test_skips_a_name_that_is_already_registered_or_invalid(caplog) -> None:
    analytics = MockAmplitudeMCPAnalytics(server_name="test-mcp", server_version="9.9.9")
    mcp = FastMCP("s")

    def existing() -> str:
        return "existing"

    mcp.add_tool(existing, name="submit_feedback", description="already here")
    with caplog.at_level(logging.WARNING, logger="amplitude_mcp_analytics"):
        analytics.register_feedback_tool(mcp)
        analytics.register_feedback_tool(mcp, name="not a name")
    messages = [record.message for record in caplog.records]
    assert any("already on the server" in message for message in messages)
    assert any("1-64 characters" in message for message in messages)
    registered = mcp._tool_manager.get_tool("submit_feedback")
    assert registered is not None
    assert registered.description == "already here"
    assert mcp._tool_manager.get_tool("not a name") is None


@pytest.mark.anyio
async def test_lists_the_tool_and_emits_feedback_without_a_tool_call_response() -> None:
    analytics = MockAmplitudeMCPAnalytics(server_name="test-mcp", server_version="9.9.9")
    mcp = FastMCP("s")

    async def ping() -> str:
        return "pong"

    mcp.add_tool(ping, name="ping", description="ping")
    analytics.instrument_server(mcp, user_id="user-7")
    analytics.register_feedback_tool(mcp, name="submit_feedback")

    async with create_connected_server_and_client_session(
        mcp._mcp_server, client_info=CLIENT
    ) as client:
        listed = await client.list_tools()
        feedback = next(tool for tool in listed.tools if tool.name == "submit_feedback")
        assert "this didn't work" in (feedback.description or "")
        assert "helpful" in (feedback.inputSchema.get("required") or [])
        assert "comment" not in (feedback.inputSchema.get("properties") or {})
        assert "ping" in [tool.name for tool in listed.tools]

        result = await client.call_tool(
            "submit_feedback",
            {"helpful": True, "solicited": True, "tools": ["ping", "made-up"]},
        )
        assert result.isError is False
        assert result.content[0].text == FEEDBACK_RECORDED_TEXT

    submitted = analytics.get_events(SUBMITTED)
    assert len(submitted) == 1
    assert submitted[0]["user_id"] == "user-7"
    props = submitted[0]["event_properties"]
    assert props["[MCP] Feedback Helpful"] is True
    assert props["[MCP] Feedback Solicited"] is True
    assert props["[MCP] Feedback Tool Names"] == ["ping"]
    assert props["[MCP] Feedback Has Comment"] is False
    assert analytics.get_events(RESPONSE) == []

    analytics.reset()
    async with create_connected_server_and_client_session(
        mcp._mcp_server, client_info=CLIENT
    ) as client:
        # A value pydantic will not coerce to bool. The callback never runs.
        invalid = await client.call_tool("submit_feedback", {"helpful": ["no"]})
        assert invalid.isError is True
    invalid_types = [event["event_type"] for event in analytics.events]
    assert REJECTED in invalid_types
    assert SUBMITTED not in invalid_types
    assert RESPONSE not in invalid_types


@pytest.mark.anyio
async def test_authenticated_feedback_uses_the_server_identity_resolver() -> None:
    from amplitude_mcp_analytics import SetIdentityInput

    analytics = MockAmplitudeMCPAnalytics(server_name="test-mcp", server_version="9.9.9")
    mcp = FastMCP("s")
    analytics.instrument_server(
        mcp, resolve_identity=lambda _auth: SetIdentityInput(user_id="user-9")
    )
    analytics.register_feedback_tool(mcp)

    async with create_connected_server_and_client_session(
        mcp._mcp_server, client_info=CLIENT
    ) as client:
        result = await client.call_tool(
            "submit_feedback", {"helpful": False, "reason": "other"}
        )
        assert result.isError is False

    submitted = analytics.get_events(SUBMITTED)
    assert len(submitted) == 1
    assert submitted[0]["user_id"] == "user-9"
    assert analytics.get_events(RESPONSE) == []
