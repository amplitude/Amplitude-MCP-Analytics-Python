"""Decide whether a ``tools/call`` request that never reached a tool callback
was rejected before dispatch — an unknown/disabled tool name, or schema
validation — and recover the JSON-RPC code when the SDK still carries it.

## Why this needs evidence rather than a shape check

In the Python SDK (like ``@modelcontextprotocol/sdk`` >= 1.21), the low-level
``tools/call`` handler funnels every failure through an in-band ``isError``
result: input-schema validation becomes ``"Input validation error: ..."``,
FastMCP's unknown tool becomes ``ToolError("Unknown tool: ...")`` → ``str(e)``,
and a tool raising becomes ``str(e)`` too. A pre-dispatch rejection and a tool
reporting its own failure are the *same shape* at the handler boundary.

The dispatch marker (``was_tool_call_dispatched``) separates them whenever the
tool is instrumented, and callers must apply it first. It cannot speak for a
tool that was registered but never wrapped with ``instrument_tool``, though —
that callback runs unseen, so an ``isError`` from it also arrives unmarked.
Claiming every unmarked ``isError`` as a rejection would mis-file those as
protocol failures, so this module requires positive evidence instead:

  1. the attempted name is absent from (or disabled in) the server's tool
     registry — authoritative, and independent of message wording; or
  2. the message carries the SDK's own pre-dispatch wording (input-validation
     prefix, unknown-tool prose, or an ``MCP error <code>:`` prefix).

Anything else is treated as a tool's own error and left alone.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal

from ..errors import classify_error, error_message_from_result, is_error_result
from .mcp import RegisteredToolState

__all__ = ["PreDispatchRejection", "RejectionReason", "classify_pre_dispatch_rejection"]

# The McpError message format (`MCP error -32602: Tool x not found`). Matching
# it is the only way to recover a code the SDK no longer reports structurally.
_MCP_ERROR_PREFIX = re.compile(r"^MCP error (-?\d+):")

# JSON-RPC internal-error code, the fallback when no code is recoverable.
_JSON_RPC_INTERNAL_ERROR = -32603

# Wording a schema rejection carries. Deliberately an alternation — the SDK
# has more than one validation path and upstream has reworded before:
#   - low-level server, validate_input=True: "Input validation error: ..."
#   - FastMCP (validate_input=False): pydantic's wording surfaced through
#     ToolError — "Error executing tool x: 1 validation error for xArguments"
#   - Node SDK 1.14-era "Invalid arguments for tool x", kept for cross-SDK safety.
# This is the sole route to `schema_validation` — a deliberate call: if this
# wording drifts, the value degrades to `unrecognized` rather than becoming
# wrong. (For an *instrumented* tool the dispatch marker already excludes the
# tool's own pydantic errors; an uninstrumented tool raising a pydantic
# ValidationError from its own body is misattributed here — the same accepted
# prose-matching hazard as Node.)
_VALIDATION_WORDING = re.compile(
    r"input validation|invalid arguments|validation errors? for", re.IGNORECASE
)

# Wording that places a failure **after** the tool callback already ran
# (`"Output validation error: ..."`), so it is not a pre-dispatch rejection no
# matter how it surfaced. Covers the case the dispatch marker cannot see: a
# tool registered without instrument_tool whose callback runs unobserved.
_POST_DISPATCH_WORDING = re.compile(
    r"output validation|invalid structured content", re.IGNORECASE
)

# FastMCP's unknown-tool prose (`ToolError(f"Unknown tool: {name}")`).
_UNKNOWN_TOOL_WORDING = re.compile(r"^unknown tool:|\bnot found\b", re.IGNORECASE)

RejectionReason = Literal["unknown_tool", "disabled_tool", "schema_validation", "unrecognized"]
"""Why a ``tools/call`` was rejected, as a closed set the SDK assigns.

Pre-dispatch failures all surface with the same error shape, so ``[MCP] Error
Code`` cannot tell them apart and the distinction survives only in the message
text — which ``sanitize_error_message`` may rewrite or drop. This carries it
structurally instead, free of caller data either way.

- ``unknown_tool`` — the requested name is not registered (hallucinated,
  mistyped, or since removed)
- ``disabled_tool`` — registered but turned off (unreachable with the official
  Python SDK, which has no tool disable; kept for cross-SDK schema parity)
- ``schema_validation`` — the name resolved but the payload failed the tool's
  input schema
- ``unrecognized`` — a pre-dispatch failure we cannot attribute further, e.g.
  on a low-level ``Server`` with no tool registry to consult
"""


@dataclass
class PreDispatchRejection:
    """A ``tools/call`` request rejected before any tool callback ran."""

    message: str
    """Message as the client saw it, prefix included."""
    reason: RejectionReason
    """Structured cause — see :data:`RejectionReason`."""
    json_rpc_code: int | None = None
    """JSON-RPC code, when the SDK still carried it or the prefix yielded it."""


def _attribute(
    registry_state: RegisteredToolState, message: str | None
) -> RejectionReason:
    """Attribute a rejection, preferring the tool registry and using the SDK's
    own wording only where the registry cannot answer.

    ``enabled`` deliberately does **not** imply ``schema_validation``: the
    registry proves the name resolved, not *why* the call failed, and the
    handler has other pre-dispatch failure paths for a live tool. Assuming
    validation there would relabel any future cause as a schema problem — a
    confidently wrong value, worse than an honest ``unrecognized``. For the
    same reason wording is never allowed to overrule the registry: a tool
    whose own message says "not found" must not become ``unknown_tool`` when
    the registry says it is live.
    """
    if registry_state == "missing":
        return "unknown_tool"
    if registry_state == "disabled":
        return "disabled_tool"

    if message is not None:
        if _VALIDATION_WORDING.search(message):
            return "schema_validation"
        # Only with no registry to consult are these the best available evidence.
        if registry_state is None:
            if _UNKNOWN_TOOL_WORDING.search(message):
                return "unknown_tool"
            if re.search(r"\bdisabled\b", message, re.IGNORECASE):
                return "disabled_tool"
    return "unrecognized"


def _read_mcp_error_code(text: str | None) -> int | None:
    """The ``MCP error <code>:`` code, when the text carries one."""
    if text is None:
        return None
    match = _MCP_ERROR_PREFIX.match(text)
    if match is None:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def classify_pre_dispatch_rejection(
    *,
    error: Any = None,
    result: Any = None,
    registry_state: RegisteredToolState = None,
) -> PreDispatchRejection | None:
    """Classify one settled ``tools/call`` outcome, for a request already known
    not to have been dispatched. Returns ``None`` when the outcome is not a
    pre-dispatch rejection and no event should be emitted here.

    @internal
    """
    # The handler raised. Rare in the Python SDK (the low-level handler
    # converts failures to in-band results), but undispatched + raised is
    # unambiguous when it happens.
    if error is not None:
        classified = classify_error(error)
        # Post-dispatch failure surfacing as a raise (an uninstrumented tool).
        if _POST_DISPATCH_WORDING.search(classified.message):
            return None
        raw_code = getattr(error, "code", None)
        return PreDispatchRejection(
            message=classified.message,
            json_rpc_code=(
                raw_code
                if isinstance(raw_code, int) and not isinstance(raw_code, bool)
                else _read_mcp_error_code(classified.message)
            ),
            reason=_attribute(registry_state, classified.message),
        )

    # The SDK reports these as resolved `isError` results. Anything that is
    # not one settled successfully and is not our concern.
    if not is_error_result(result):
        return None

    text = error_message_from_result(result)

    # Same post-dispatch exclusion, for the in-band shape.
    if text is not None and _POST_DISPATCH_WORDING.search(text):
        return None

    # The tool is not there to have run — no message parsing needed.
    if registry_state in ("missing", "disabled"):
        code = _read_mcp_error_code(text)
        return PreDispatchRejection(
            message=text if text is not None else "Tool call rejected before dispatch",
            json_rpc_code=code if code is not None else _JSON_RPC_INTERNAL_ERROR,
            reason=_attribute(registry_state, text),
        )

    if text is None:
        return None

    # Registered and live (or no registry): a rejection here is identifiable
    # only by the SDK's own pre-dispatch wording. Without it, assume the tool
    # ran and failed on its own — `[MCP] Tool Call Response` owns that (or
    # nothing does, when the tool is uninstrumented).
    if _VALIDATION_WORDING.search(text):
        return PreDispatchRejection(
            message=text,
            json_rpc_code=_read_mcp_error_code(text) or _JSON_RPC_INTERNAL_ERROR,
            reason=_attribute(registry_state, text),
        )
    if registry_state is None and _UNKNOWN_TOOL_WORDING.search(text):
        return PreDispatchRejection(
            message=text,
            json_rpc_code=_read_mcp_error_code(text) or _JSON_RPC_INTERNAL_ERROR,
            reason="unknown_tool",
        )
    json_rpc_code = _read_mcp_error_code(text)
    if json_rpc_code is None:
        return None
    return PreDispatchRejection(
        message=text,
        json_rpc_code=json_rpc_code,
        reason=_attribute(registry_state, text),
    )
