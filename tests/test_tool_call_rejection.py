"""Ports the Node repo's test/tool-call-rejection.test.ts.

``classify_pre_dispatch_rejection`` — deciding whether an undispatched
``tools/call`` outcome was a rejection before dispatch.

These are pure unit tests over the two failure shapes the MCP SDK uses, so
both contracts are covered regardless of which SDK version is installed:

  - ``error`` set        -> the handler raised (plus surviving throws)
  - ``result.isError``   -> in-band conversion (the Python SDK's usual shape)

Callers must already have excluded dispatched requests; every case here is
one the dispatch marker did not claim.
"""

from __future__ import annotations

from typing import Any

import pytest

from amplitude_mcp_analytics.core.tool_call_rejection import (
    PreDispatchRejection,
    classify_pre_dispatch_rejection,
)


def mcp_error(code: int, message: str) -> Exception:
    """An ``McpError``-shaped raise, as the SDK raises for pre-dispatch failures."""
    err = Exception(f"MCP error {code}: {message}")
    err.code = code  # type: ignore[attr-defined]
    return err


def error_result(text: str) -> dict[str, Any]:
    """An in-band error result, as the SDK returns for the same failures."""
    return {"content": [{"type": "text", "text": text}], "isError": True}


class TestThrownShape:
    def test_claims_any_undispatched_throw_and_keeps_the_numeric_code(self) -> None:
        out = classify_pre_dispatch_rejection(
            error=mcp_error(-32602, "Tool nope not found"),
            registry_state="missing",
        )

        assert out == PreDispatchRejection(
            message="MCP error -32602: Tool nope not found",
            json_rpc_code=-32602,
            reason="unknown_tool",
        )

    def test_claims_a_throw_even_when_the_registry_cannot_be_read(self) -> None:
        out = classify_pre_dispatch_rejection(error=mcp_error(-32602, "bad args"))
        assert out is not None
        assert out.json_rpc_code == -32602

    def test_recovers_the_code_from_the_message_when_the_error_carries_none(self) -> None:
        out = classify_pre_dispatch_rejection(
            error=Exception("MCP error -32601: Method not found"),
        )
        assert out is not None
        assert out.json_rpc_code == -32601

    def test_reports_no_code_rather_than_inventing_one_for_a_bare_throw(self) -> None:
        out = classify_pre_dispatch_rejection(error=Exception("something broke"))
        assert out == PreDispatchRejection(
            message="something broke",
            json_rpc_code=None,
            reason="unrecognized",
        )

    def test_ignores_a_non_integer_code_on_the_error(self) -> None:
        err = Exception("weird")
        err.code = 1.5  # type: ignore[attr-defined]
        out = classify_pre_dispatch_rejection(error=err)
        assert out is not None
        assert out.json_rpc_code is None


class TestInBandIsErrorShape:
    def test_claims_an_unknown_tool_on_registry_evidence(self) -> None:
        out = classify_pre_dispatch_rejection(
            result=error_result("MCP error -32602: Tool nope not found"),
            registry_state="missing",
        )

        assert out == PreDispatchRejection(
            message="MCP error -32602: Tool nope not found",
            json_rpc_code=-32602,
            reason="unknown_tool",
        )

    def test_claims_a_disabled_tool_on_registry_evidence(self) -> None:
        out = classify_pre_dispatch_rejection(
            result=error_result("MCP error -32602: Tool off disabled"),
            registry_state="disabled",
        )
        assert out is not None
        assert out.json_rpc_code == -32602

    def test_claims_a_schema_failure_on_a_live_tool_via_the_mcp_error_prefix(self) -> None:
        out = classify_pre_dispatch_rejection(
            result=error_result("MCP error -32602: Invalid arguments for tool echo: ..."),
            registry_state="enabled",
        )
        assert out is not None
        assert out.json_rpc_code == -32602

    def test_declines_a_live_tools_own_error_no_prefix_so_it_actually_ran(self) -> None:
        out = classify_pre_dispatch_rejection(
            result=error_result("upstream returned 500"),
            registry_state="enabled",
        )
        assert out is None

    def test_declines_a_live_tools_error_when_the_registry_is_unreadable(self) -> None:
        # Without registry evidence the prefix is the only signal, and absent it the
        # safe reading is "the tool ran" — misfiling this would inflate protocol_error.
        out = classify_pre_dispatch_rejection(result=error_result("upstream returned 500"))
        assert out is None

    def test_declines_a_successful_result(self) -> None:
        out = classify_pre_dispatch_rejection(
            result={"content": [{"type": "text", "text": "ok"}]},
            registry_state="enabled",
        )
        assert out is None

    def test_declines_when_nothing_settled_at_all(self) -> None:
        assert classify_pre_dispatch_rejection() is None

    def test_still_claims_a_missing_tool_when_the_message_has_no_usable_prefix(self) -> None:
        # Registry evidence stands on its own; the code falls back rather than the
        # whole rejection being dropped.
        out = classify_pre_dispatch_rejection(
            result=error_result("Tool nope not found"),
            registry_state="missing",
        )
        assert out is not None
        assert out.message == "Tool nope not found"
        assert out.json_rpc_code == -32603

    def test_attributes_a_disabled_tool_distinctly_from_an_unknown_one(self) -> None:
        disabled = classify_pre_dispatch_rejection(
            result=error_result("MCP error -32602: Tool off disabled"),
            registry_state="disabled",
        )
        unknown = classify_pre_dispatch_rejection(
            result=error_result("MCP error -32602: Tool nope not found"),
            registry_state="missing",
        )

        assert disabled is not None and unknown is not None
        # Both are -32602, so the reason is the only thing that separates them.
        assert disabled.json_rpc_code == unknown.json_rpc_code
        assert disabled.reason == "disabled_tool"
        assert unknown.reason == "unknown_tool"

    def test_substitutes_a_message_when_a_missing_tools_result_has_no_text(self) -> None:
        out = classify_pre_dispatch_rejection(
            result={"content": [], "isError": True},
            registry_state="missing",
        )
        assert out is not None
        assert out.message == "Tool call rejected before dispatch"


# The SDK's output-schema check runs on the callback's RETURN value, so the
# tool already executed. Instrumented tools are excluded by the dispatch
# marker; an uninstrumented tool's callback runs unseen, so the wording is the
# only thing keeping it out of `[MCP] Tool Call Rejected`.
OUTPUT_MSGS = [
    "MCP error -32602: Output validation error: Invalid structured content for tool x: ...",
    "MCP error -32602: Invalid structured content for tool x: expected number",
]


class TestPostDispatchFailuresAreNotRejections:
    @pytest.mark.parametrize("message", OUTPUT_MSGS)
    def test_declines_the_in_band_shape(self, message: str) -> None:
        assert (
            classify_pre_dispatch_rejection(
                result=error_result(message), registry_state="enabled"
            )
            is None
        )

    @pytest.mark.parametrize("message", OUTPUT_MSGS)
    def test_declines_the_thrown_shape(self, message: str) -> None:
        err = Exception(message)
        err.code = -32602  # type: ignore[attr-defined]
        assert classify_pre_dispatch_rejection(error=err, registry_state="enabled") is None

    def test_still_claims_input_validation_which_is_genuinely_pre_dispatch(self) -> None:
        out = classify_pre_dispatch_rejection(
            result=error_result(
                "MCP error -32602: Input validation error: Invalid arguments for tool x"
            ),
            registry_state="enabled",
        )
        assert out is not None
        assert out.reason == "schema_validation"

    def test_declines_output_validation_even_when_the_registry_is_unreadable(self) -> None:
        assert classify_pre_dispatch_rejection(result=error_result(OUTPUT_MSGS[0])) is None


class TestAttributionWithoutARegistry:
    # A low-level `Server` has no tool registry, so wording is the only fallback.
    @pytest.mark.parametrize(
        ("message", "reason"),
        [
            ("MCP error -32602: Tool nope not found", "unknown_tool"),
            ("MCP error -32602: Tool off disabled", "disabled_tool"),
            ("MCP error -32602: Invalid arguments for tool echo: ...", "schema_validation"),
            ("MCP error -32602: Input validation error: ...", "schema_validation"),
        ],
    )
    def test_reads_wording_as_the_expected_reason(self, message: str, reason: str) -> None:
        out = classify_pre_dispatch_rejection(result=error_result(message))
        assert out is not None
        assert out.reason == reason

    def test_claims_fastmcp_pydantic_validation_wording(self) -> None:
        # Python addition: FastMCP (validate_input=False) surfaces pydantic's own
        # wording through ToolError — no `MCP error` prefix, so the code falls back.
        out = classify_pre_dispatch_rejection(
            result=error_result("Error executing tool echo: 1 validation error for echoArguments"),
            registry_state="enabled",
        )
        assert out is not None
        assert out.reason == "schema_validation"
        assert out.json_rpc_code == -32603

    def test_falls_back_to_unrecognized_rather_than_guessing_on_unfamiliar_wording(
        self,
    ) -> None:
        # Degrades safely if the SDK rewords: better an honest `unrecognized` bucket
        # than a confidently wrong attribution.
        out = classify_pre_dispatch_rejection(
            error=mcp_error(-32602, "something the SDK has never said before"),
        )
        assert out is not None
        assert out.reason == "unrecognized"

    def test_does_not_assume_schema_validation_just_because_the_tool_is_live(self) -> None:
        # The server has other pre-dispatch throw paths for a registered, enabled
        # tool, and more may be added. An unfamiliar one must land in `unrecognized`
        # rather than being relabelled a schema problem.
        out = classify_pre_dispatch_rejection(
            result=error_result(
                "MCP error -32603: Tool tasky has taskSupport but was not registered"
            ),
            registry_state="enabled",
        )
        assert out is not None
        assert out.reason == "unrecognized"
        # Still reported as a rejection — only the attribution is withheld.
        assert out.json_rpc_code == -32603

    def test_prefers_registry_evidence_over_wording_when_both_are_available(self) -> None:
        # A tool whose own message happens to say "not found" must not be reported
        # as unknown_tool when the registry says it is live.
        out = classify_pre_dispatch_rejection(
            result=error_result("MCP error -32602: Invalid arguments: record not found"),
            registry_state="enabled",
        )
        assert out is not None
        assert out.reason == "schema_validation"
