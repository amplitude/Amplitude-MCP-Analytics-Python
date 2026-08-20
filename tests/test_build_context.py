"""Ports test/build-context.test.ts (Node) — the per-request context resolvers.

Adapted to the Python seam: Node fed the resolvers a hand-built handler
``extra`` object; Python reads the SDK's ambient request frame instead, so the
private helpers are unit-tested by name with fake request objects (the
integration path is covered by the run-based suites). Node's structural
``resolveTransport(transport)`` becomes ``resolve_transport_evidence(request)``
— the Python SDK has no transport object, only the HTTP request evidence each
message may carry."""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any

import pytest

from amplitude_mcp_analytics import McpAnchor, McpClientInfo
from amplitude_mcp_analytics.core.build_context import (
    _parse_trace_id,
    _request_session_id,
    _request_traceparent,
    _resolve_anchor,
    build_server_context,
    resolve_transport_evidence,
)
from amplitude_mcp_analytics.core.identity import ServerIdentity
from conftest import server_ctx

TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"
TRACEPARENT = f"00-{TRACE_ID}-00f067aa0ba902b7-01"

META_TRACE_ID = "0af7651916cd43dd8448eb211c80319c"
META_TRACEPARENT = f"00-{META_TRACE_ID}-b7ad6b7169203331-01"


def http_request(
    headers: dict[str, str] | None = None,
    query_params: dict[str, str] | None = None,
) -> Any:
    return SimpleNamespace(headers=headers, query_params=query_params)


def request_context(request: Any = None, meta: Any = None) -> Any:
    """The duck-typed slice of the SDK's RequestContext these helpers read."""
    return SimpleNamespace(request=request, meta=meta)


def request_meta(**values: Any) -> Any:
    """A stand-in for ``RequestParams.Meta`` (extra='allow'), whose unknown keys
    the SDK exposes as plain attributes."""
    return SimpleNamespace(**values)


# --- resolve_transport_evidence ---------------------------------------------


def test_no_request_is_no_evidence() -> None:
    # stdio stays the default when no message ever carries HTTP evidence.
    assert resolve_transport_evidence(None) is None


def test_request_without_headers_is_no_evidence() -> None:
    assert resolve_transport_evidence(SimpleNamespace(headers=None)) is None


def test_request_with_headers_classifies_streamable_http() -> None:
    assert resolve_transport_evidence(http_request(headers={})) == "streamable-http"
    # With or without an mcp-session-id header — stateless streamable HTTP
    # carries none, and is still streamable HTTP.
    assert (
        resolve_transport_evidence(http_request(headers={"mcp-session-id": "sess-1"}))
        == "streamable-http"
    )


def test_session_id_query_param_classifies_sse() -> None:
    # The deprecated HTTP+SSE transport is the only one carrying the session id
    # as a query parameter.
    request = http_request(headers={}, query_params={"session_id": "sess-1"})
    assert resolve_transport_evidence(request) == "sse"
    # An empty value is not evidence of sse.
    empty = http_request(headers={}, query_params={"session_id": ""})
    assert resolve_transport_evidence(empty) == "streamable-http"


# --- _parse_trace_id ----------------------------------------------------------


def test_parses_a_valid_traceparent() -> None:
    assert _parse_trace_id(TRACEPARENT) == TRACE_ID


def test_lowercases_uppercase_hex() -> None:
    assert _parse_trace_id(f"00-{TRACE_ID.upper()}-00f067aa0ba902b7-01") == TRACE_ID


def test_rejects_malformed_traceparents() -> None:
    assert _parse_trace_id("not-a-traceparent") is None
    assert _parse_trace_id(f"00-{TRACE_ID}") is None  # too few fields
    assert _parse_trace_id(f"00-{TRACE_ID[:10]}-00f067aa0ba902b7-01") is None  # short id
    assert _parse_trace_id(None) is None
    assert _parse_trace_id(1234) is None  # non-string, guarded


def test_rejects_the_all_zero_trace_id() -> None:
    # All-zero is the W3C "invalid" sentinel — anchoring on it would stitch
    # unrelated requests together.
    assert _parse_trace_id(f"00-{'0' * 32}-00f067aa0ba902b7-01") is None


@pytest.mark.parametrize(
    ("case", "traceparent"),
    [
        # A 32-hex second field is NOT enough: the whole header has to be well
        # formed before its trace id becomes a correlation key, or any string
        # that merely contains one stitches unrelated requests together.
        ("trailing junk field", f"00-{TRACE_ID}-00f067aa0ba902b7-01-junk"),
        ("all-zero parent id", f"00-{TRACE_ID}-{'0' * 16}-01"),
        ("short parent id", f"00-{TRACE_ID}-00f067-01"),
        ("long parent id", f"00-{TRACE_ID}-00f067aa0ba902b7ff-01"),
        ("non-hex parent id", f"00-{TRACE_ID}-00f067aa0ba902bz-01"),
        ("forbidden ff version", f"ff-{TRACE_ID}-00f067aa0ba902b7-01"),
        ("short version", f"0-{TRACE_ID}-00f067aa0ba902b7-01"),
        ("non-hex version", f"zz-{TRACE_ID}-00f067aa0ba902b7-01"),
        ("non-hex flags", f"00-{TRACE_ID}-00f067aa0ba902b7-zz"),
        ("short flags", f"00-{TRACE_ID}-00f067aa0ba902b7-1"),
        ("empty flags field", f"00-{TRACE_ID}-00f067aa0ba902b7-"),
        ("missing flags field", f"00-{TRACE_ID}-00f067aa0ba902b7"),
    ],
)
def test_rejects_a_traceparent_whose_full_w3c_shape_is_invalid(
    case: str, traceparent: str
) -> None:
    assert _parse_trace_id(traceparent) is None, case


def test_a_valid_traceparent_still_yields_the_same_anchor_value() -> None:
    # Cross-SDK parity guard: tightening the parser must not move the anchor
    # value for a well-formed header, or Python and Node would derive different
    # device ids from the same trace.
    assert _parse_trace_id(TRACEPARENT) == TRACE_ID
    assert _parse_trace_id(f"  {TRACEPARENT}  ") == TRACE_ID  # surrounding space
    assert _parse_trace_id(f"01-{TRACE_ID}-00f067aa0ba902b7-00") == TRACE_ID  # future version
    assert _resolve_anchor("streamable-http", None, None, TRACEPARENT) == McpAnchor(
        type="trace", value=TRACE_ID
    )


# --- _request_traceparent: HTTP header first, `_meta` as the fallback ---------


def test_the_traceparent_header_is_read() -> None:
    # A normally propagated W3C header is the transport-level truth for this
    # hop; ignoring it (Node reads `_meta` only) drops stateless HTTP callers
    # onto a fresh anonymous anchor, which the default skip rule then drops.
    rc = request_context(http_request(headers={"traceparent": TRACEPARENT}))
    assert _request_traceparent(rc) == TRACEPARENT
    assert _resolve_anchor("streamable-http", None, None, _request_traceparent(rc)) == McpAnchor(
        type="trace", value=TRACE_ID
    )


def test_the_traceparent_header_wins_over_meta() -> None:
    rc = request_context(
        http_request(headers={"traceparent": TRACEPARENT}),
        meta=request_meta(traceparent=META_TRACEPARENT),
    )
    assert _request_traceparent(rc) == TRACEPARENT


def test_meta_is_the_fallback_when_no_header_is_present() -> None:
    # stdio and any HTTP hop without the header keep the `_meta` convention.
    meta = request_meta(traceparent=META_TRACEPARENT)
    assert _request_traceparent(request_context(http_request(headers={}), meta)) == META_TRACEPARENT
    # No HTTP request at all (stdio) — `_meta` is the only source there.
    assert _request_traceparent(request_context(None, meta)) == META_TRACEPARENT


def test_a_malformed_header_yields_to_a_valid_meta_value() -> None:
    # A broken header is not evidence that the in-band value is stale.
    rc = request_context(
        http_request(headers={"traceparent": "not-a-traceparent"}),
        meta=request_meta(traceparent=META_TRACEPARENT),
    )
    assert _request_traceparent(rc) == META_TRACEPARENT


def test_no_traceparent_anywhere_is_none() -> None:
    assert _request_traceparent(None) is None
    assert _request_traceparent(request_context(http_request(headers={}))) is None


# --- _resolve_anchor ladder ---------------------------------------------------


def test_stdio_anchors_on_the_process() -> None:
    # Process lifetime, stable across every call of this server process.
    anchor = _resolve_anchor("stdio", None, None, None)
    assert anchor == McpAnchor(type="process", value=str(os.getpid()))


def test_http_with_a_session_id_anchors_on_it() -> None:
    anchor = _resolve_anchor("streamable-http", "sess-abc123", None, None)
    assert anchor == McpAnchor(type="session-id", value="sess-abc123")


def test_http_falls_back_to_the_bound_session_id_anchor() -> None:
    # Host-managed sessions: instrument_server(session_id=...) binds an anchor
    # for transports that carry none of their own.
    bound = McpAnchor(type="session-id", value="sess-host-managed")
    anchor = _resolve_anchor("streamable-http", None, bound, None)
    assert anchor is bound


def test_a_non_session_bound_anchor_is_not_used() -> None:
    # Only a bound session-id participates in the ladder — a leftover anchor of
    # another type must not shadow the trace/anonymous branches.
    bound = McpAnchor(type="anonymous", value="stale")
    anchor = _resolve_anchor("streamable-http", None, bound, TRACEPARENT)
    assert anchor == McpAnchor(type="trace", value=TRACE_ID)


def test_stateless_http_anchors_on_the_propagated_trace() -> None:
    anchor = _resolve_anchor("streamable-http", None, None, TRACEPARENT)
    assert anchor == McpAnchor(type="trace", value=TRACE_ID)


def test_stateless_http_without_a_trace_gets_the_anonymous_floor() -> None:
    first = _resolve_anchor("streamable-http", None, None, None)
    second = _resolve_anchor("streamable-http", None, None, None)
    assert first.type == "anonymous"
    assert len(first.value) >= 5  # valid synthetic id
    assert first.value != second.value  # fresh per request, no stitching


def test_malformed_or_all_zero_traceparent_falls_to_the_anonymous_floor() -> None:
    malformed = _resolve_anchor("streamable-http", None, None, "not-a-traceparent")
    all_zero = _resolve_anchor(
        "streamable-http", None, None, f"00-{'0' * 32}-00f067aa0ba902b7-01"
    )
    assert malformed.type == "anonymous"
    assert all_zero.type == "anonymous"


def test_an_empty_session_id_falls_through_to_the_stateless_branch() -> None:
    # A session id is never assumed — an empty string selects statelessness.
    anchor = _resolve_anchor("streamable-http", "", None, None)
    assert anchor.type == "anonymous"


# --- _request_session_id precedence -------------------------------------------


def test_the_mcp_session_id_header_wins() -> None:
    rc = request_context(
        http_request(
            headers={"mcp-session-id": "from-header"},
            query_params={"session_id": "from-query"},
        )
    )
    assert _request_session_id(rc, "from-scope") == "from-header"


def test_the_sse_query_param_is_next() -> None:
    rc = request_context(http_request(headers={}, query_params={"session_id": "from-query"}))
    assert _request_session_id(rc, "from-scope") == "from-query"


def test_the_scope_captured_id_is_the_fallback() -> None:
    # The streamable-http initialize POST carries no mcp-session-id header (the
    # server mints the id in its response), so the run scope's captured id is
    # the only source for that first request.
    rc = request_context(http_request(headers={}))
    assert _request_session_id(rc, "from-scope") == "from-scope"
    assert _request_session_id(None, "from-scope") == "from-scope"


def test_no_session_id_anywhere_is_none() -> None:
    assert _request_session_id(None, None) is None
    assert _request_session_id(request_context(http_request(headers={})), None) is None


# --- build_server_context outside a request frame ------------------------------


def test_outside_a_request_frame_it_degrades_to_the_scope_values() -> None:
    # No ambient request_ctx in this test frame — the resolvers must fall back
    # to the values already on the server scope (the direct-invocation path).
    base = server_ctx(
        transport="stdio",
        protocol_version="2025-11-25",
        client=McpClientInfo(name="claude", version="3.0"),
        auth_type="oauth",
        extra={"region": "us"},
    )

    resolved = build_server_context(base)

    assert resolved.transport == "stdio"  # inherited from the scope
    assert resolved.anchor == McpAnchor(type="process", value=str(os.getpid()))
    assert resolved.protocol_version == "2025-11-25"  # scope fallback used
    assert resolved.client is not None
    assert resolved.client.name == "claude"
    assert resolved.client.version == "3.0"
    assert resolved.auth_type == "oauth"
    assert resolved.extra == {"region": "us"}


def test_outside_a_request_frame_the_server_identity_still_resolves() -> None:
    base = server_ctx(transport="stdio", identity=None)
    resolved = build_server_context(
        base, server_identity=ServerIdentity(user_id="alice@example.com")
    )
    assert resolved.identity.user_id == "alice@example.com"
    assert resolved.identity.resolved_from == "explicit"


def test_outside_a_request_frame_the_scope_session_id_feeds_the_anchor() -> None:
    base = server_ctx(transport="streamable-http", identity=None)
    resolved = build_server_context(base, scope_session_id="sess-captured")
    assert resolved.anchor == McpAnchor(type="session-id", value="sess-captured")


# --- build_server_context inside a request frame -------------------------------


@contextmanager
def request_frame(rc: Any) -> Iterator[None]:
    """Set the SDK's ambient ``request_ctx`` around a block, the way a live
    handler dispatch does."""
    from mcp.server.lowlevel.server import request_ctx

    token = request_ctx.set(rc)
    try:
        yield
    finally:
        request_ctx.reset(token)


def test_a_stateless_request_anchors_on_the_propagated_traceparent_header() -> None:
    # End-to-end for the header source: a stateless HTTP request carrying only
    # the standard W3C header must correlate, not fall to the anonymous floor
    # (which `should_emit` then drops by default).
    base = server_ctx(transport="streamable-http", identity=None)

    with request_frame(request_context(http_request(headers={"traceparent": TRACEPARENT}))):
        resolved = build_server_context(base)

    assert resolved.anchor == McpAnchor(type="trace", value=TRACE_ID)
    assert resolved.identity.resolved_from == "anchor"
    assert resolved.identity.user_id == f"trace:{TRACE_ID}"


def test_a_stateless_request_still_anchors_on_the_meta_traceparent() -> None:
    # The Node-compatible in-band convention keeps working unchanged.
    base = server_ctx(transport="streamable-http", identity=None)

    with request_frame(
        request_context(http_request(headers={}), meta=request_meta(traceparent=META_TRACEPARENT))
    ):
        resolved = build_server_context(base)

    assert resolved.anchor == McpAnchor(type="trace", value=META_TRACE_ID)


def test_a_transport_session_id_still_outranks_a_traceparent_header() -> None:
    # Ladder order is unchanged: a real session id is a stronger anchor than
    # trace context, header or not.
    base = server_ctx(transport="streamable-http", identity=None)

    with request_frame(
        request_context(
            http_request(headers={"mcp-session-id": "sess-1", "traceparent": TRACEPARENT})
        )
    ):
        resolved = build_server_context(base)

    assert resolved.anchor == McpAnchor(type="session-id", value="sess-1")


def test_a_malformed_traceparent_header_falls_to_the_anonymous_floor() -> None:
    base = server_ctx(transport="streamable-http", identity=None)
    header = {"traceparent": f"00-{TRACE_ID}-0000000000000000-01-junk"}

    with request_frame(request_context(http_request(headers=header))):
        resolved = build_server_context(base)

    assert resolved.anchor.type == "anonymous"
    assert resolved.identity.resolved_from == "anonymous"
