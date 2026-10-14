"""Opt-in ``submit_feedback`` tool.

``instrument_server`` does not register it. ``register_feedback_tool`` does,
and only on a high-level FastMCP server. The handler builds a tool-scope
context and emits ``[MCP] Feedback Submitted``. It is not wrapped in
``instrument_tool``, so the call does not also emit ``[MCP] Tool Call Response``.

Ported from Amplitude-MCP-Analytics-Node ``src/core/feedback-tool.ts`` at
``4898c974a437b598f9a3cbe1ad0f148df702c064``.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal, TypedDict

from ..config import ErrorMessageSanitizer
from ..context.types import ClientInfoResolver, McpServerContext, McpToolMeta
from ..context.vars import run_with_context
from ..core.privacy import PrivacyConfig
from ..tracking.constants import FEEDBACK_COMMENT_MAX, FEEDBACK_TOOLS_MAX
from ..tracking.events.feedback_submitted import emit_feedback_submitted
from ..tracking.sanitize_error_message import sanitize_error_message
from ..types import AmplitudeClientLike
from ..utils.logger import get_logger
from .build_context import build_tool_context
from .identity import ServerIdentity
from .mcp import lookup_registered_tool
from .tool_call_hook import mark_tool_call_dispatched

__all__ = [
    "FEEDBACK_REASONS",
    "FEEDBACK_RECORDED_TEXT",
    "FEEDBACK_TOOL_ANNOTATIONS",
    "FEEDBACK_TOOL_DEFINITION",
    "FEEDBACK_TOOL_INSTRUCTIONS",
    "FEEDBACK_TOOL_NAME",
    "FEEDBACK_TOOL_TITLE",
    "CreateFeedbackToolHandlerOptions",
    "FeedbackInputJsonSchema",
    "FeedbackJsonSchemaProperty",
    "FeedbackReason",
    "FeedbackToolAnnotations",
    "FeedbackToolDefinition",
    "FeedbackToolDependencies",
    "RegisterFeedbackToolOptions",
    "create_feedback_handler",
    "feedback_tool_definition",
    "feedback_tool_instructions",
    "register_feedback_tool_on_server",
    "resolve_feedback_tool",
]

#: Default tool name. Plain on purpose: agents match it more reliably than a prefixed name.
FEEDBACK_TOOL_NAME = "submit_feedback"

FEEDBACK_TOOL_TITLE = "Submit feedback on this server"

FeedbackReason = Literal[
    "wrong_result",
    "incomplete",
    "too_slow",
    "wrong_tool",
    "missing_capability",
    "other",
]
"""Why the result did not help, when the user indicated a reason.

Closed set — a value outside it is dropped rather than emitted.
"""

FEEDBACK_REASONS: tuple[FeedbackReason, ...] = (
    "wrong_result",
    "incomplete",
    "too_slow",
    "wrong_tool",
    "missing_capability",
    "other",
)

#: Static tool result. Never echoes the arguments back to the model.
FEEDBACK_RECORDED_TEXT = (
    "Feedback recorded. Thank the user briefly and continue. Do not ask for "
    "feedback again in this conversation unless the user raises it."
)

_TOOL_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_REASON_SET = frozenset(FEEDBACK_REASONS)

_HELPFUL_DESCRIPTION = (
    "Whether the result actually helped the user. True only when the user "
    "indicated it helped; false when they indicated it did not."
)
_REASON_DESCRIPTION = (
    "Why it did not help, when the user indicated a reason. Omit when they did not say."
)
_COMMENT_DESCRIPTION = (
    "The user's own words, only when they offered a comment and agreed to share it. "
    "At most 500 characters."
)
_TOOLS_DESCRIPTION = "Names of this server's tools the feedback is about, if the user named any."
_SOLICITED_DESCRIPTION = (
    "True when you asked the user whether it helped; false when they volunteered the reaction."
)


class FeedbackToolAnnotations(TypedDict):
    """Hints for hosts that show an approval dialog on non-read-only tools."""

    readOnlyHint: Literal[False]
    destructiveHint: Literal[False]
    idempotentHint: Literal[False]
    openWorldHint: Literal[True]


FEEDBACK_TOOL_ANNOTATIONS: FeedbackToolAnnotations = {
    "readOnlyHint": False,
    "destructiveHint": False,
    "idempotentHint": False,
    "openWorldHint": True,
}


class FeedbackJsonSchemaProperty(TypedDict, total=False):
    """One field of the feedback tool's JSON Schema."""

    type: Literal["boolean", "string", "array"]
    description: str
    enum: list[FeedbackReason]
    maxLength: int
    maxItems: int
    items: dict[str, str]


class FeedbackInputJsonSchema(TypedDict):
    """JSON Schema for the tool input. Plain data — registration builds a matching function."""

    type: Literal["object"]
    properties: dict[str, FeedbackJsonSchemaProperty]
    required: list[Literal["helpful"]]
    additionalProperties: Literal[False]


class FeedbackToolDefinition(TypedDict):
    """Name, title, description, schema, and annotations for hand-wiring the tool."""

    name: str
    title: str
    description: str
    inputSchema: FeedbackInputJsonSchema
    annotations: FeedbackToolAnnotations


class RegisterFeedbackToolOptions(TypedDict, total=False):
    """Options for ``register_feedback_tool``.

    ``name`` is the tool name advertised to the agent. Default
    ``submit_feedback``. It must be 1–64 characters of letters, numbers,
    underscores, or hyphens. An invalid name is not registered.

    ``capture_comment`` accepts an optional ``comment`` argument and emits
    ``[MCP] Feedback Comment``. Default false: the field is omitted from the
    schema, and a comment that arrives anyway is dropped.

    ``description`` replaces the default tool description entirely. The
    description is the instruction the agent follows, so a replacement should
    still say when to call the tool and that the rating must be the user's,
    not the agent's.
    """

    name: str
    capture_comment: bool
    description: str


class CreateFeedbackToolHandlerOptions(RegisterFeedbackToolOptions, total=False):
    """Options for ``create_feedback_tool_handler``.

    ``server`` is the server whose registered tool names are checked against
    the ``tools`` argument. Names that are not registered are dropped. When
    omitted, or when the registry cannot be read, ``tools`` is left off the
    event.
    """

    server: Any


@dataclass
class FeedbackToolDependencies:
    """Dependencies the handler needs from the analytics client. @internal"""

    amplitude: AmplitudeClientLike
    get_server_ctx: Callable[[], McpServerContext | None]
    get_server_identity: Callable[[], ServerIdentity | None]
    get_client_info_resolver: Callable[[], ClientInfoResolver | None]
    get_scope_session_id: Callable[[], str | None] | None = None
    sanitize_error_message: ErrorMessageSanitizer | None = None
    privacy: PrivacyConfig | None = None
    logger: logging.Logger | None = None


@dataclass
class ResolvedFeedbackTool:
    """Registration options resolved into one tool definition. @internal"""

    name: str
    capture_comment: bool
    description: str
    definition: FeedbackToolDefinition


def _default_description(capture_comment: bool) -> str:
    fields = (
        "Set helpful to whether it helped, set reason when it did not, and set comment "
        "only when the user offered a short comment in their own words and agreed to "
        "share it. Submitting sends only those fields."
        if capture_comment
        else (
            "Set helpful to whether it helped, and set reason when it did not. "
            "Submitting sends only those fields."
        )
    )
    return " ".join(
        [
            "Record whether the help provided through this server's tools actually "
            "worked for the user.",
            'Call this when the user reacts to a result from this server, positively '
            '("thanks, that\'s it", "perfect") or negatively ("that\'s wrong", '
            '"not what I asked", "this didn\'t work"), or when the user explicitly '
            "asks to give feedback.",
            "After you finish a multi-step task that relied on this server's tools, "
            "you may ask the user once, briefly, whether the result was helpful, and "
            "call this with their answer.",
            "Do not ask if they already indicated it, and never more than once per task.",
            "Report only what the user actually expressed; do not infer a rating from "
            "your own judgment of the result.",
            fields,
            "No conversation content is sent.",
            "This is never required for the task to succeed.",
        ]
    )


def feedback_tool_instructions(tool_name: str = FEEDBACK_TOOL_NAME) -> str:
    """Paragraph to append to the server's instructions at construction time.

    Uses ``tool_name`` so a renamed tool is still named in the instructions.
    Server instructions cannot be changed after the server is constructed.
    """
    return (
        f"This server provides a {tool_name} tool. When the user reacts to a result "
        f"from this server, or explicitly asks to give feedback, call {tool_name} with "
        "what they expressed. You may ask once, briefly, after finishing a multi-step "
        "task that used this server's tools. Do not ask more than once per task, and "
        "do not infer a rating the user did not express."
    )


#: Default instructions, for a server that keeps the default tool name.
FEEDBACK_TOOL_INSTRUCTIONS = feedback_tool_instructions()


def _input_json_schema(capture_comment: bool) -> FeedbackInputJsonSchema:
    properties: dict[str, FeedbackJsonSchemaProperty] = {
        "helpful": {"type": "boolean", "description": _HELPFUL_DESCRIPTION},
        "reason": {
            "type": "string",
            "description": _REASON_DESCRIPTION,
            "enum": list(FEEDBACK_REASONS),
        },
        "tools": {
            "type": "array",
            "description": _TOOLS_DESCRIPTION,
            "items": {"type": "string"},
            "maxItems": FEEDBACK_TOOLS_MAX,
        },
        "solicited": {"type": "boolean", "description": _SOLICITED_DESCRIPTION},
    }
    if capture_comment:
        properties["comment"] = {
            "type": "string",
            "description": _COMMENT_DESCRIPTION,
            "maxLength": FEEDBACK_COMMENT_MAX,
        }
    return {
        "type": "object",
        "properties": properties,
        "required": ["helpful"],
        "additionalProperties": False,
    }


def resolve_feedback_tool(
    options: Mapping[str, Any] | None = None,
) -> ResolvedFeedbackTool | None:
    """Resolve registration options into the tool definition.

    Returns ``None`` when ``name`` was provided and is not a valid tool name.
    @internal
    """
    requested = None if options is None else options.get("name")
    if requested is None:
        name: str | None = FEEDBACK_TOOL_NAME
    elif isinstance(requested, str) and _TOOL_NAME_PATTERN.fullmatch(requested):
        name = requested
    else:
        name = None
    if name is None:
        return None

    capture_comment = options is not None and options.get("capture_comment") is True
    custom = None if options is None else options.get("description")
    description = (
        custom
        if isinstance(custom, str) and custom.strip() != ""
        else _default_description(capture_comment)
    )
    return ResolvedFeedbackTool(
        name=name,
        capture_comment=capture_comment,
        description=description,
        definition={
            "name": name,
            "title": FEEDBACK_TOOL_TITLE,
            "description": description,
            "inputSchema": _input_json_schema(capture_comment),
            "annotations": FEEDBACK_TOOL_ANNOTATIONS,
        },
    )


#: Definition for the default tool (``submit_feedback``, comment capture off).
FEEDBACK_TOOL_DEFINITION: FeedbackToolDefinition = {
    "name": FEEDBACK_TOOL_NAME,
    "title": FEEDBACK_TOOL_TITLE,
    "description": _default_description(False),
    "inputSchema": _input_json_schema(False),
    "annotations": FEEDBACK_TOOL_ANNOTATIONS,
}


def feedback_tool_definition(
    *,
    name: str | None = None,
    capture_comment: bool = False,
    description: str | None = None,
) -> FeedbackToolDefinition | None:
    """Definition for a non-default name, description, or comment capture."""
    options: dict[str, Any] = {}
    if name is not None:
        options["name"] = name
    if capture_comment:
        options["capture_comment"] = True
    if description is not None:
        options["description"] = description
    resolved = resolve_feedback_tool(options or None)
    return None if resolved is None else resolved.definition


def _recorded_result() -> dict[str, Any]:
    return {"content": [{"type": "text", "text": FEEDBACK_RECORDED_TEXT}]}


def _invalid_helpful_result() -> dict[str, Any]:
    return {
        "isError": True,
        "content": [{"type": "text", "text": "helpful is required and must be a boolean."}],
    }


def _registered_tool_names(server: Any) -> set[str] | None:
    tool_manager = getattr(server, "_tool_manager", None)
    if tool_manager is None:
        return None
    tools = getattr(tool_manager, "_tools", None)
    if not isinstance(tools, dict):
        return None
    return {key for key in tools if isinstance(key, str)}


def _filter_tool_names(value: Any, known: set[str] | None) -> list[str] | None:
    if known is None or not isinstance(value, list):
        return None
    out: list[str] = []
    for item in value:
        if not isinstance(item, str) or item not in known or item in out:
            continue
        out.append(item)
        if len(out) == FEEDBACK_TOOLS_MAX:
            break
    return out or None


def _read_reason(value: Any) -> FeedbackReason | None:
    if isinstance(value, str) and value in _REASON_SET:
        return value  # type: ignore[return-value]
    return None


def _read_comment(
    value: Any,
    capture_comment: bool,
    sanitize: ErrorMessageSanitizer | None,
) -> tuple[bool, str | None]:
    if not capture_comment or not isinstance(value, str):
        return False, None
    capped = value[:FEEDBACK_COMMENT_MAX]
    if capped.strip() == "":
        return False, None
    sanitized = sanitize_error_message(capped, sanitize)
    if sanitized is None:
        return True, None
    bounded = sanitized[:FEEDBACK_COMMENT_MAX]
    if bounded.strip() == "":
        return True, None
    return True, bounded


def _as_args(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    return {}


def create_feedback_handler(
    deps: FeedbackToolDependencies,
    options: Mapping[str, Any] | None = None,
) -> Callable[[Any], dict[str, Any]]:
    """Build the tool callback from a raw argument mapping.

    Returns the static recorded result even when analytics is off, so a missing
    ``instrument_server`` never breaks the tool. @internal The supported
    constructor is :func:`create_feedback_tool_handler` on the client module.
    """
    resolved = resolve_feedback_tool(options)
    tool = resolved if resolved is not None else resolve_feedback_tool()
    assert tool is not None
    logger = deps.logger if deps.logger is not None else get_logger()
    warned_unbound = False
    registry_server = None if options is None else options.get("server")

    def handler(args: Any) -> dict[str, Any]:
        nonlocal warned_unbound
        # Reached a callback, so the rejection hook must not also emit.
        mark_tool_call_dispatched()

        result = _recorded_result()
        incoming = _as_args(args)
        if not isinstance(incoming.get("helpful"), bool):
            return _invalid_helpful_result()

        server_ctx = deps.get_server_ctx()
        if server_ctx is None:
            if not warned_unbound:
                warned_unbound = True
                logger.warning(
                    "AmplitudeMCPAnalytics: register_feedback_tool('%s') ran without "
                    "instrument_server(); the tool still responds, but no [MCP] Feedback "
                    "Submitted event is emitted. Call instrument_server(server) before "
                    "the server runs to enable tracking.",
                    tool.name,
                )
            return result

        try:
            ctx = build_tool_context(
                server_ctx,
                McpToolMeta(name=tool.name),
                scope_session_id=(
                    deps.get_scope_session_id() if deps.get_scope_session_id is not None else None
                ),
                resolve_client_info=deps.get_client_info_resolver(),
                server_identity=deps.get_server_identity(),
                logger=logger,
            )
            has_comment, comment = _read_comment(
                incoming.get("comment"), tool.capture_comment, deps.sanitize_error_message
            )
            solicited = incoming.get("solicited")

            def _emit() -> None:
                emit_feedback_submitted(
                    deps.amplitude,
                    ctx,
                    helpful=bool(incoming["helpful"]),
                    reason=_read_reason(incoming.get("reason")),
                    solicited=solicited if isinstance(solicited, bool) else None,
                    tool_names=_filter_tool_names(
                        incoming.get("tools"), _registered_tool_names(registry_server)
                    ),
                    has_comment=has_comment,
                    comment=comment,
                    privacy=deps.privacy,
                )

            run_with_context(ctx, _emit)
        except Exception as err:  # noqa: BLE001 — the tool still answers
            logger.warning(
                "AmplitudeMCPAnalytics: submit_feedback ('%s') could not emit "
                "[MCP] Feedback Submitted: %s",
                tool.name,
                err,
            )

        return result

    return handler


def _text_or_raise(outcome: dict[str, Any]) -> str:
    if outcome.get("isError"):
        raise ValueError(outcome["content"][0]["text"])
    return FEEDBACK_RECORDED_TEXT


def _compile_callback(source: str, handler: Callable[[Any], dict[str, Any]]) -> Callable[..., Any]:
    """Build the tool function outside this module's postponed annotations.

    ``from __future__ import annotations`` would leave ``Annotated`` and
    ``Field`` as strings FastMCP cannot resolve. ``exec`` evaluates them.
    """
    from typing import Annotated

    from pydantic import Field

    namespace: dict[str, Any] = {
        "Annotated": Annotated,
        "Field": Field,
        "FeedbackReason": FeedbackReason,
        "FEEDBACK_COMMENT_MAX": FEEDBACK_COMMENT_MAX,
        "FEEDBACK_TOOLS_MAX": FEEDBACK_TOOLS_MAX,
        "_HELPFUL_DESCRIPTION": _HELPFUL_DESCRIPTION,
        "_REASON_DESCRIPTION": _REASON_DESCRIPTION,
        "_COMMENT_DESCRIPTION": _COMMENT_DESCRIPTION,
        "_TOOLS_DESCRIPTION": _TOOLS_DESCRIPTION,
        "_SOLICITED_DESCRIPTION": _SOLICITED_DESCRIPTION,
        "_text_or_raise": _text_or_raise,
        "handler": handler,
    }
    exec(source, namespace)  # noqa: S102 — the source is a literal in this module
    callback = namespace["submit_feedback"]
    callback.__module__ = __name__
    return callback


_CALLBACK_WITH_COMMENT = """
async def submit_feedback(
    helpful: Annotated[bool, Field(description=_HELPFUL_DESCRIPTION)],
    reason: Annotated[FeedbackReason | None, Field(description=_REASON_DESCRIPTION)] = None,
    comment: Annotated[
        str | None, Field(description=_COMMENT_DESCRIPTION, max_length=FEEDBACK_COMMENT_MAX)
    ] = None,
    tools: Annotated[
        list[str] | None, Field(description=_TOOLS_DESCRIPTION, max_length=FEEDBACK_TOOLS_MAX)
    ] = None,
    solicited: Annotated[bool | None, Field(description=_SOLICITED_DESCRIPTION)] = None,
) -> str:
    return _text_or_raise(handler({
        "helpful": helpful,
        "reason": reason,
        "comment": comment,
        "tools": tools,
        "solicited": solicited,
    }))
"""

_CALLBACK_WITHOUT_COMMENT = """
async def submit_feedback(
    helpful: Annotated[bool, Field(description=_HELPFUL_DESCRIPTION)],
    reason: Annotated[FeedbackReason | None, Field(description=_REASON_DESCRIPTION)] = None,
    tools: Annotated[
        list[str] | None, Field(description=_TOOLS_DESCRIPTION, max_length=FEEDBACK_TOOLS_MAX)
    ] = None,
    solicited: Annotated[bool | None, Field(description=_SOLICITED_DESCRIPTION)] = None,
) -> str:
    return _text_or_raise(handler({
        "helpful": helpful,
        "reason": reason,
        "tools": tools,
        "solicited": solicited,
    }))
"""


def _callback_with_comment(handler: Callable[[Any], dict[str, Any]]) -> Callable[..., Any]:
    return _compile_callback(_CALLBACK_WITH_COMMENT, handler)


def _callback_without_comment(handler: Callable[[Any], dict[str, Any]]) -> Callable[..., Any]:
    return _compile_callback(_CALLBACK_WITHOUT_COMMENT, handler)


def _fastmcp_callback(
    handler: Callable[[Any], dict[str, Any]],
    *,
    capture_comment: bool,
) -> Callable[..., Any]:
    """Typed function FastMCP turns into the tool schema. Comment is absent unless capture is on."""
    if capture_comment:
        return _callback_with_comment(handler)
    return _callback_without_comment(handler)


def feedback_tool_callback(
    deps: FeedbackToolDependencies,
    options: Mapping[str, Any] | None = None,
) -> Callable[..., Any]:
    """FastMCP-compatible callback for the resolved tool. @internal"""
    resolved = resolve_feedback_tool(options) or resolve_feedback_tool()
    assert resolved is not None
    return _fastmcp_callback(
        create_feedback_handler(deps, options),
        capture_comment=resolved.capture_comment,
    )


def _as_fastmcp(server: Any) -> Any | None:
    if callable(getattr(server, "add_tool", None)) and getattr(server, "_tool_manager", None) is not None:
        return server
    return None


def register_feedback_tool_on_server(
    server: Any,
    deps: FeedbackToolDependencies,
    options: Mapping[str, Any] | None = None,
) -> Any:
    """Register the feedback tool on a FastMCP server.

    Warns and returns the same server when the server is low-level, the name
    is invalid, or a tool with that name is already registered. Does not raise.
    @internal Called by ``AmplitudeMCPAnalytics.register_feedback_tool``.
    """
    logger = deps.logger if deps.logger is not None else get_logger()
    fastmcp = _as_fastmcp(server)
    if fastmcp is None:
        logger.warning(
            "AmplitudeMCPAnalytics: register_feedback_tool() requires a FastMCP server. "
            "A low-level Server has no tool registry to add to, so the feedback tool "
            "was not registered."
        )
        return server

    requested = None if options is None else options.get("name")
    if requested is not None and (
        not isinstance(requested, str) or _TOOL_NAME_PATTERN.fullmatch(requested) is None
    ):
        logger.warning(
            "AmplitudeMCPAnalytics: register_feedback_tool() did not register a tool. "
            "name must be 1-64 characters of letters, numbers, underscores, or hyphens."
        )
        return server

    resolved = resolve_feedback_tool(options)
    if resolved is None:
        return server

    existing = lookup_registered_tool(fastmcp._tool_manager, resolved.name)
    if existing in ("enabled", "disabled"):
        logger.warning(
            "AmplitudeMCPAnalytics: register_feedback_tool() skipped registering '%s' "
            "because that name is already on the server. Pass a different name to "
            "register one alongside it.",
            resolved.name,
        )
        return server

    description = None if options is None else options.get("description")
    if isinstance(description, str) and description.strip() == "":
        logger.warning(
            "AmplitudeMCPAnalytics: register_feedback_tool('%s') ignored an empty "
            "description and used the default.",
            resolved.name,
        )

    handler_options = dict(options or {})
    handler_options["name"] = resolved.name
    handler_options["server"] = server
    callback = feedback_tool_callback(deps, handler_options)
    try:
        from mcp.types import ToolAnnotations

        fastmcp.add_tool(
            callback,
            name=resolved.name,
            title=resolved.definition["title"],
            description=resolved.description,
            annotations=ToolAnnotations(**FEEDBACK_TOOL_ANNOTATIONS),
            structured_output=False,
        )
    except Exception as err:  # noqa: BLE001 — registration must not raise
        logger.warning(
            "AmplitudeMCPAnalytics: register_feedback_tool() could not register '%s': %s",
            resolved.name,
            err,
        )
    return server
