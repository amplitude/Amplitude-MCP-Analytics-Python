"""Port of the Node repo's test/errors.test.ts.

Python-idiomatic classification divergences (intentional — see src errors.py):
- 'timeout' is keyed off TimeoutError / asyncio.CancelledError (Node keys off
  DOMException 'AbortError').
- 'transport_error' is keyed off the ConnectionError family / socket.gaierror,
  each mapped to the Node-style lowercase code (Node reads `err.code`).
- The HTTP status is sniffed from `err.status` / `err.status_code` /
  `err.response.status_code` (Node: `status` / `statusCode`).
- `hash_stack` takes an exception and hashes its innermost 3 traceback frames,
  so it needs a *raised* exception (Node hashes a stack string).
"""

from __future__ import annotations

import re
from types import SimpleNamespace
from typing import Any

import pytest

from amplitude_mcp_analytics import AmplitudeMCPAnalytics
from amplitude_mcp_analytics.context.factory import create_server_context
from amplitude_mcp_analytics.context.types import McpServerInfo, McpToolContext
from amplitude_mcp_analytics.context.vars import get_current_context
from amplitude_mcp_analytics.errors import (
    McpToolError,
    build_tool_error,
    classify_error,
    compute_fingerprint,
    hash_stack,
    normalize_message,
    tool_error_result,
)
from conftest import ListLogger, make_amplitude

HEX12 = re.compile(r"^[0-9a-f]{12}$")


def make_analytics(amplitude: Any | None = None) -> tuple[AmplitudeMCPAnalytics, Any]:
    client = amplitude if amplitude is not None else make_amplitude()
    analytics = AmplitudeMCPAnalytics(
        amplitude=client, server_name="test-mcp", server_version="9.9.9"
    )
    return analytics, client


def bind(analytics: AmplitudeMCPAnalytics) -> None:
    """Node's bind() writes the private `_serverCtx` fallback directly; the
    Python client keeps the same last-connected fallback field."""
    analytics._server_ctx = create_server_context(
        server=McpServerInfo(name="test-mcp", version="9.9.9"),
        transport="streamable-http",
    )


def raised(err: BaseException) -> BaseException:
    """Raise-and-catch so the exception carries a real ``__traceback__``
    (``hash_stack`` returns None without one)."""
    try:
        raise err
    except BaseException as caught:
        return caught


class TestBuildToolError:
    def test_always_builds_a_returned_error_hosts_do_not_pick_a_type(self) -> None:
        err = build_tool_error(code="missing_id", message="No ID provided.")

        assert err.type == "returned_error"
        assert err.code == "missing_id"
        assert err.message == "No ID provided."

    def test_preserves_all_user_supplied_fields(self) -> None:
        err = build_tool_error(
            code="upstream_timeout",
            message="API timed out.",
            correction_message="Try again in a moment.",
            recoverable=True,
            retry_suggested=True,
        )

        assert err.code == "upstream_timeout"
        assert err.message == "API timed out."
        assert err.type == "returned_error"
        assert err.correction_message == "Try again in a moment."
        assert err.recoverable is True
        assert err.retry_suggested is True
        assert err.fingerprint is not None and HEX12.match(err.fingerprint)

    def test_uses_caller_supplied_fingerprint_when_provided(self) -> None:
        err = build_tool_error(
            code="custom",
            message="Something broke.",
            fingerprint="my-custom-group",
        )

        assert err.fingerprint == "my-custom-group"

    def test_carries_an_explicit_http_status_through(self) -> None:
        err = build_tool_error(
            code="upstream_denied",
            message="Access denied by upstream.",
            http_status=403,
        )

        assert err.http_status == 403

    def test_drops_an_out_of_range_explicit_http_status(self) -> None:
        err = build_tool_error(code="weird", message="Bad status.", http_status=12345)

        assert err.http_status is None


class TestToolErrorResult:
    def test_returns_a_valid_mcp_call_tool_result_with_is_error_true(self) -> None:
        err = McpToolError(
            code="missing_chart_id",
            message="No chart ID was provided.",
            type="returned_error",
            correction_message="Search for a chart first, then retry with the chart ID.",
        )

        result = tool_error_result(err)

        assert result["isError"] is True
        assert len(result["content"]) == 1
        assert result["content"][0] == {
            "type": "text",
            "text": "No chart ID was provided. Search for a chart first, then retry with the chart ID.",
        }

    def test_omits_correction_when_not_provided(self) -> None:
        err = McpToolError(
            code="fail",
            message="Something went wrong.",
            type="thrown_exception",
        )

        result = tool_error_result(err)

        assert result["content"][0] == {"type": "text", "text": "Something went wrong."}


class TestClassifyError:
    def test_classifies_a_regular_raised_error_as_thrown_exception(self) -> None:
        classified = classify_error(raised(RuntimeError("kaboom")))

        assert classified.type == "thrown_exception"
        assert classified.message == "kaboom"
        # No `code`: a bare raise with no `err.code` carries only the coarse type.
        assert classified.code is None
        assert classified.stack_hash is not None

    def test_classifies_timeout_cancellation_as_timeout(self) -> None:
        # Node classifies DOMException 'AbortError'; the Python analogue is
        # TimeoutError / asyncio.CancelledError (intentional divergence).
        classified = classify_error(raised(TimeoutError("signal aborted")))

        assert classified.type == "timeout"
        assert classified.code is None

    def test_classifies_network_errors_as_transport_error(self) -> None:
        # Node reads err.code === 'ECONNREFUSED'; Python maps the exception
        # class to the Node-style lowercase code (intentional divergence).
        classified = classify_error(raised(ConnectionRefusedError("connect ECONNREFUSED")))

        assert classified.type == "transport_error"
        assert classified.code == "econnrefused"

    def test_classifies_econnreset_as_transport_error(self) -> None:
        classified = classify_error(raised(ConnectionResetError("socket hang up")))

        assert classified.type == "transport_error"

    def test_classifies_a_non_exception_value_as_unknown(self) -> None:
        classified = classify_error("just a string")

        assert classified.type == "unknown"
        assert classified.message == "just a string"
        assert classified.code is None
        assert classified.stack_hash is None

    def test_handles_a_thrown_none_gracefully(self) -> None:
        # Node covers both null and undefined; Python has only None.
        assert classify_error(None).type == "unknown"
        assert classify_error(None).message == "Unknown error"

    def test_preserves_err_code_when_present_on_a_regular_error(self) -> None:
        err = RuntimeError("validation failed")
        err.code = "ERR_VALIDATION"  # type: ignore[attr-defined]

        classified = classify_error(raised(err))

        assert classified.code == "ERR_VALIDATION"
        assert classified.type == "thrown_exception"

    def test_sniffs_http_status_from_err_status(self) -> None:
        err = RuntimeError("Forbidden")
        err.status = 403  # type: ignore[attr-defined]

        classified = classify_error(raised(err))

        assert classified.http_status == 403
        assert classified.type == "thrown_exception"

    def test_classifies_http_429_as_rate_limited(self) -> None:
        err = RuntimeError("Too Many Requests")
        err.status = 429  # type: ignore[attr-defined]

        classified = classify_error(raised(err))

        assert classified.type == "rate_limited"
        assert classified.http_status == 429
        assert classified.retry_suggested is True

    def test_sniffs_http_status_from_err_status_code_when_err_status_is_absent(self) -> None:
        # Node sniffs `statusCode`; Python's second convention is `status_code`.
        err = RuntimeError("Server error")
        err.status_code = 502  # type: ignore[attr-defined]

        assert classify_error(raised(err)).http_status == 502

    def test_prefers_err_status_over_err_status_code(self) -> None:
        err = RuntimeError("Conflicting shapes")
        err.status = 404  # type: ignore[attr-defined]
        err.status_code = 500  # type: ignore[attr-defined]

        assert classify_error(raised(err)).http_status == 404

    def test_ignores_non_http_shaped_status_values(self) -> None:
        string_status = RuntimeError("stringy")
        string_status.status = "oops"  # type: ignore[attr-defined]
        out_of_range = RuntimeError("rangey")
        out_of_range.status = 42  # type: ignore[attr-defined]

        assert classify_error(raised(string_status)).http_status is None
        assert classify_error(raised(out_of_range)).http_status is None

    def test_omits_http_status_when_the_error_carries_none(self) -> None:
        assert classify_error(raised(RuntimeError("plain"))).http_status is None

    def test_attaches_http_status_on_the_transport_error_branch_too(self) -> None:
        err = ConnectionResetError("socket closed mid-response")
        err.status_code = 502  # type: ignore[attr-defined]

        classified = classify_error(raised(err))

        assert classified.type == "transport_error"
        assert classified.http_status == 502


def _boom() -> BaseException:
    """Single raise site so repeated calls share an identical traceback shape."""
    try:
        raise RuntimeError("boom")
    except RuntimeError as err:
        return err


def _boom_from_a() -> BaseException:
    try:
        raise ValueError("boom")
    except ValueError as err:
        return err


def _boom_from_b() -> BaseException:
    try:
        raise ValueError("boom")
    except ValueError as err:
        return err


class TestHashStack:
    def test_returns_none_for_an_exception_that_was_never_raised(self) -> None:
        # Node: undefined input. Python analogue: no __traceback__ attached.
        assert hash_stack(RuntimeError("boom")) is None

    def test_returns_none_for_an_exception_with_a_cleared_traceback(self) -> None:
        # Node: a stack string with no frames.
        err = raised(RuntimeError("boom")).with_traceback(None)
        assert hash_stack(err) is None

    def test_produces_a_12_char_hex_string(self) -> None:
        digest = hash_stack(_boom())

        assert digest is not None
        assert len(digest) == 12
        assert HEX12.match(digest)

    def test_is_deterministic_for_the_same_stack(self) -> None:
        # Both raises share raise site and call site, so the frames match.
        digests = [hash_stack(_boom()) for _ in range(2)]

        assert digests[0] == digests[1]

    def test_differs_for_different_stacks(self) -> None:
        assert hash_stack(_boom_from_a()) != hash_stack(_boom_from_b())

    def test_never_includes_raw_stack_frames_in_output(self) -> None:
        def secret_project_handler() -> None:
            raise RuntimeError("boom")

        digest = None
        try:
            secret_project_handler()
        except RuntimeError as err:
            digest = hash_stack(err)

        assert digest is not None
        assert "secret" not in digest
        assert "handler" not in digest
        assert "/Users" not in digest


class TestNormalizeMessage:
    def test_replaces_uuids_with_uuid_token(self) -> None:
        assert (
            normalize_message("Chart 550e8400-e29b-41d4-a716-446655440000 not found")
            == "Chart <uuid> not found"
        )

    def test_replaces_numbers_with_n_token(self) -> None:
        assert normalize_message("Row 42 in table 7 failed") == "Row <n> in table <n> failed"

    def test_replaces_double_quoted_strings_with_str_token(self) -> None:
        assert normalize_message('Unknown column "user_name"') == 'Unknown column "<str>"'

    def test_replaces_single_quoted_strings_with_str_token(self) -> None:
        assert normalize_message("Key 'abc' not found") == "Key '<str>' not found"

    def test_normalizes_a_complex_message_consistently(self) -> None:
        a = normalize_message('User 123 chart "sales" error 550e8400-e29b-41d4-a716-446655440000')
        b = normalize_message('User 456 chart "revenue" error a1b2c3d4-e5f6-7890-abcd-ef1234567890')

        assert a == b


class TestComputeFingerprint:
    def test_produces_a_12_char_hex_string(self) -> None:
        fp = compute_fingerprint("thrown_exception", "boom")

        assert HEX12.match(fp)

    def test_is_deterministic(self) -> None:
        a = compute_fingerprint("timeout", "timed out")
        b = compute_fingerprint("timeout", "timed out")

        assert a == b

    def test_differs_by_error_type(self) -> None:
        a = compute_fingerprint("thrown_exception", "boom")
        b = compute_fingerprint("timeout", "boom")

        assert a != b

    def test_groups_same_template_messages_with_different_dynamic_values(self) -> None:
        a = compute_fingerprint("thrown_exception", "Chart 123 not found")
        b = compute_fingerprint("thrown_exception", "Chart 456 not found")

        assert a == b


class TestClassifyErrorFingerprints:
    def test_includes_a_fingerprint_on_classified_errors(self) -> None:
        fingerprint = classify_error(raised(RuntimeError("boom"))).fingerprint
        assert fingerprint is not None and HEX12.match(fingerprint)

    def test_groups_same_type_errors_with_template_equivalent_messages(self) -> None:
        a = classify_error(raised(RuntimeError("Chart 123 not found")))
        b = classify_error(raised(RuntimeError("Chart 456 not found")))

        assert a.fingerprint == b.fingerprint

    def test_includes_a_fingerprint_on_non_exception_values(self) -> None:
        fingerprint = classify_error("oops").fingerprint
        assert fingerprint is not None and HEX12.match(fingerprint)


class TestAmplitudeMCPAnalyticsToolError:
    @pytest.mark.anyio
    async def test_returns_a_valid_mcp_error_response_and_stores_error_on_ctx(self) -> None:
        analytics, _ = make_analytics()
        bind(analytics)

        captured: dict[str, Any] = {}

        async def handler() -> dict[str, Any]:
            ctx = get_current_context()
            captured["ctx"] = ctx
            result = analytics.tool_error(
                ctx,  # type: ignore[arg-type]
                code="missing_chart_id",
                message="No chart ID was provided.",
                correction_message="Search for a chart first.",
                recoverable=True,
            )
            captured["result"] = result
            return result

        wrapped = analytics.instrument_tool(handler, name="search_docs")
        await wrapped()

        result = captured["result"]
        ctx: McpToolContext = captured["ctx"]
        assert result["isError"] is True
        assert result["content"][0] == {
            "type": "text",
            "text": "No chart ID was provided. Search for a chart first.",
        }

        assert ctx.error is not None
        assert ctx.error.code == "missing_chart_id"
        assert ctx.error.type == "returned_error"
        assert ctx.error.recoverable is True

    def test_returns_the_mcp_error_result_and_warns_when_ctx_is_missing_never_raises(
        self,
    ) -> None:
        # Node captures console.warn; Python resolves the logger off the raw
        # client's `configuration.logger` (see utils/logger.get_logger).
        logger = ListLogger()
        client = make_amplitude()
        client.configuration = SimpleNamespace(logger=logger)  # type: ignore[attr-defined]
        analytics, _ = make_analytics(amplitude=client)

        result = analytics.tool_error(
            None, code="missing_chart_id", message="No chart ID was provided."
        )

        assert result["isError"] is True
        assert result["content"][0] == {"type": "text", "text": "No chart ID was provided."}
        assert any("tool_error('missing_chart_id')" in w for w in logger.warnings)


class TestInstrumentToolErrorClassification:
    def test_classifies_thrown_sync_errors_and_reraises(self) -> None:
        analytics, _ = make_analytics()
        bind(analytics)

        captured: dict[str, Any] = {}

        def handler() -> dict[str, Any]:
            captured["ctx"] = get_current_context()
            raise RuntimeError("kaboom")

        wrapped = analytics.instrument_tool(handler, name="boom")

        with pytest.raises(RuntimeError, match="kaboom"):
            wrapped()

        ctx: McpToolContext = captured["ctx"]
        assert ctx.error is not None
        assert ctx.error.type == "thrown_exception"
        assert ctx.error.message == "kaboom"
        assert ctx.error.stack_hash is not None

    @pytest.mark.anyio
    async def test_classifies_thrown_async_errors_and_reraises(self) -> None:
        analytics, _ = make_analytics()
        bind(analytics)

        captured: dict[str, Any] = {}

        async def handler() -> dict[str, Any]:
            captured["ctx"] = get_current_context()
            raise RuntimeError("async kaboom")

        wrapped = analytics.instrument_tool(handler, name="boom")

        with pytest.raises(RuntimeError, match="async kaboom"):
            await wrapped()

        ctx: McpToolContext = captured["ctx"]
        assert ctx.error is not None
        assert ctx.error.type == "thrown_exception"
        assert ctx.error.message == "async kaboom"

    @pytest.mark.anyio
    async def test_does_not_set_ctx_error_on_successful_calls(self) -> None:
        analytics, _ = make_analytics()
        bind(analytics)

        captured: dict[str, Any] = {}

        async def handler() -> dict[str, Any]:
            captured["ctx"] = get_current_context()
            return {"content": [{"type": "text", "text": "ok"}]}

        wrapped = analytics.instrument_tool(handler, name="ok-tool")
        await wrapped()

        ctx: McpToolContext = captured["ctx"]
        assert ctx.error is None

    @pytest.mark.anyio
    async def test_classifies_timeout_from_cancelled_call(self) -> None:
        # Node throws DOMException 'AbortError'; Python's analogue is
        # TimeoutError (intentional divergence).
        analytics, _ = make_analytics()
        bind(analytics)

        captured: dict[str, Any] = {}

        async def handler() -> dict[str, Any]:
            captured["ctx"] = get_current_context()
            raise TimeoutError("signal aborted")

        wrapped = analytics.instrument_tool(handler, name="timeout-tool")

        with pytest.raises(TimeoutError, match="signal aborted"):
            await wrapped()

        ctx: McpToolContext = captured["ctx"]
        assert ctx.error is not None
        assert ctx.error.type == "timeout"

    @pytest.mark.anyio
    async def test_carries_the_sniffed_http_status_from_a_thrown_error_onto_ctx_error(
        self,
    ) -> None:
        analytics, _ = make_analytics()
        bind(analytics)

        captured: dict[str, Any] = {}

        async def handler() -> dict[str, Any]:
            captured["ctx"] = get_current_context()
            err = RuntimeError("upstream said no")
            err.status = 403  # type: ignore[attr-defined]
            raise err

        wrapped = analytics.instrument_tool(handler, name="denied-tool")

        with pytest.raises(RuntimeError, match="upstream said no"):
            await wrapped()

        ctx: McpToolContext = captured["ctx"]
        assert ctx.error is not None
        assert ctx.error.type == "thrown_exception"
        assert ctx.error.http_status == 403
