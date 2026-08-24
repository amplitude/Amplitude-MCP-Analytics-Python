"""Ports the Node repo's test/identity.test.ts — the pure identity fallback
chain (``resolve_identity_from_chain``): resolve_identity callback ->
server identity -> anchor -> anonymous floor."""

from __future__ import annotations

import uuid
from typing import Any

from amplitude_mcp_analytics import McpAnchor, McpTenant, SetIdentityInput
from amplitude_mcp_analytics.core.identity import (
    AMP_MCP_NAMESPACE,
    ServerIdentity,
    resolve_identity_from_chain,
)
from conftest import ListLogger

PROCESS_ANCHOR = McpAnchor(type="process", value="12345")
SESSION_ANCHOR = McpAnchor(type="session-id", value="sess-abc")
TRACE_ANCHOR = McpAnchor(type="trace", value="4bf92f3577b34da6a3ce929d0e0e4736")
ANON_ANCHOR = McpAnchor(type="anonymous", value="aaa-bbb-ccc")

# The four namespace UUIDs RFC 9562 Appendix A reserves. None of them is a
# private namespace, so none may be used to derive device ids.
_RFC_RESERVED_NAMESPACES = {
    uuid.NAMESPACE_DNS,
    uuid.NAMESPACE_URL,
    uuid.NAMESPACE_OID,
    uuid.NAMESPACE_X500,
}


class TestNamespace:
    def test_namespace_is_not_an_rfc_reserved_constant(self) -> None:
        # Regression: through 0.1.x both SDKs derived device ids under
        # NAMESPACE_OID (6ba7b812-...), a globally published constant. Hashing
        # there makes every device_id reproducible by any third party and
        # collides with anyone else who reaches for the same reserved value.
        assert AMP_MCP_NAMESPACE not in _RFC_RESERVED_NAMESPACES
        assert AMP_MCP_NAMESPACE != uuid.UUID("6ba7b812-9dad-11d1-80b4-00c04fd430c8")

    def test_namespace_is_a_random_v4_uuid(self) -> None:
        # A privately minted namespace, not derived from anything guessable.
        assert AMP_MCP_NAMESPACE.version == 4

    def test_namespace_matches_the_node_sdk(self) -> None:
        # Cross-SDK wire contract. Changing this re-derives every anchor-derived
        # device_id; it must be changed in both SDKs together and called out as
        # a data-continuity break.
        assert str(AMP_MCP_NAMESPACE) == "f08626eb-3a5c-4f3a-bec2-227ab3178022"


class TestResolveIdentityCallback:
    def test_uses_user_id_from_resolve_identity_when_present(self) -> None:
        def resolver(auth_info: dict[str, Any] | None) -> SetIdentityInput:
            return SetIdentityInput(user_id=auth_info["email"] if auth_info else None)

        result = resolve_identity_from_chain(
            resolve_identity=resolver,
            auth_info={"email": "alice@example.com"},
            anchor=SESSION_ANCHOR,
        )

        assert result.identity.user_id == "alice@example.com"
        assert result.identity.resolved_from == "authInfo"

    def test_derives_device_id_from_anchor_when_resolver_returns_only_user_id(self) -> None:
        result = resolve_identity_from_chain(
            resolve_identity=lambda _auth: SetIdentityInput(user_id="alice"),
            auth_info={},
            anchor=SESSION_ANCHOR,
        )

        assert result.identity.user_id == "alice"
        assert result.identity.device_id is not None
        assert len(result.identity.device_id) >= 5

    def test_uses_explicit_device_id_from_resolver_skipping_anchor_derivation(self) -> None:
        result = resolve_identity_from_chain(
            resolve_identity=lambda _auth: SetIdentityInput(
                user_id="alice", device_id="my-device-123"
            ),
            auth_info={},
            anchor=SESSION_ANCHOR,
        )

        assert result.identity.device_id == "my-device-123"

    def test_passes_tenant_through_from_resolver(self) -> None:
        tenant = McpTenant(group_type="org id", group_value="42")

        result = resolve_identity_from_chain(
            resolve_identity=lambda _auth: SetIdentityInput(user_id="alice", tenant=tenant),
            auth_info={},
            anchor=SESSION_ANCHOR,
        )

        assert result.tenant == tenant

    def test_falls_through_when_resolver_returns_empty(self) -> None:
        result = resolve_identity_from_chain(
            resolve_identity=lambda _auth: SetIdentityInput(),
            auth_info={},
            anchor=SESSION_ANCHOR,
        )

        assert result.identity.resolved_from == "anchor"


class TestServerIdentityFromInstrumentServer:
    def test_uses_static_user_id_from_server_identity(self) -> None:
        result = resolve_identity_from_chain(
            server_identity=ServerIdentity(user_id="operator@example.com"),
            anchor=PROCESS_ANCHOR,
        )

        assert result.identity.user_id == "operator@example.com"
        assert result.identity.resolved_from == "explicit"

    def test_derives_device_id_from_anchor_when_server_identity_has_only_user_id(self) -> None:
        result = resolve_identity_from_chain(
            server_identity=ServerIdentity(user_id="operator@example.com"),
            anchor=PROCESS_ANCHOR,
        )

        assert result.identity.device_id is not None

    def test_uses_explicit_device_id_from_server_identity(self) -> None:
        result = resolve_identity_from_chain(
            server_identity=ServerIdentity(user_id="op", device_id="dev-static"),
            anchor=PROCESS_ANCHOR,
        )

        assert result.identity.device_id == "dev-static"

    def test_passes_tenant_from_server_identity(self) -> None:
        tenant = McpTenant(group_type="org id", group_value="99")

        result = resolve_identity_from_chain(
            server_identity=ServerIdentity(user_id="op", tenant=tenant),
            anchor=PROCESS_ANCHOR,
        )

        assert result.tenant == tenant


class TestAnchorBasedIdentity:
    def test_derives_user_id_and_device_id_from_a_process_anchor(self) -> None:
        result = resolve_identity_from_chain(anchor=PROCESS_ANCHOR)

        assert result.identity.resolved_from == "anchor"
        assert result.identity.user_id == "process:12345"
        assert result.identity.device_id is not None
        assert len(result.identity.device_id) >= 5

    def test_derives_user_id_and_device_id_from_a_session_anchor(self) -> None:
        result = resolve_identity_from_chain(anchor=SESSION_ANCHOR)

        assert result.identity.resolved_from == "anchor"
        assert result.identity.user_id == "session-id:sess-abc"

    def test_derives_user_id_and_device_id_from_a_trace_anchor(self) -> None:
        result = resolve_identity_from_chain(anchor=TRACE_ANCHOR)

        assert result.identity.resolved_from == "anchor"
        assert result.identity.user_id == f"trace:{TRACE_ANCHOR.value}"

    def test_produces_stable_device_id_for_the_same_anchor(self) -> None:
        a = resolve_identity_from_chain(anchor=SESSION_ANCHOR)
        b = resolve_identity_from_chain(anchor=SESSION_ANCHOR)

        assert a.identity.device_id == b.identity.device_id
        # RFC 9562 uuid5 in this SDK's private namespace — the same anchor must
        # yield the same device_id here and in the Node SDK.
        assert a.identity.device_id == str(uuid.uuid5(AMP_MCP_NAMESPACE, "session-id:sess-abc"))

    def test_produces_different_device_ids_for_different_anchors(self) -> None:
        a = resolve_identity_from_chain(anchor=PROCESS_ANCHOR)
        b = resolve_identity_from_chain(anchor=SESSION_ANCHOR)

        assert a.identity.device_id != b.identity.device_id


class TestAnonymousFloor:
    def test_falls_to_anonymous_when_anchor_is_anonymous(self) -> None:
        result = resolve_identity_from_chain(anchor=ANON_ANCHOR)

        assert result.identity.resolved_from == "anonymous"

    def test_synthesizes_user_id_with_anonymous_prefix_and_device_id(self) -> None:
        result = resolve_identity_from_chain(anchor=ANON_ANCHOR)

        assert result.identity.device_id is not None
        assert result.identity.user_id == f"anonymous:{result.identity.device_id}"

    def test_produces_a_fresh_device_id_per_call(self) -> None:
        a = resolve_identity_from_chain(anchor=ANON_ANCHOR)
        b = resolve_identity_from_chain(anchor=ANON_ANCHOR)

        # No stitching across anonymous requests.
        assert a.identity.device_id != b.identity.device_id


class TestPrecedence:
    def test_resolve_identity_wins_over_server_identity(self) -> None:
        result = resolve_identity_from_chain(
            resolve_identity=lambda _auth: SetIdentityInput(user_id="from-auth"),
            auth_info={},
            server_identity=ServerIdentity(user_id="from-server"),
            anchor=PROCESS_ANCHOR,
        )

        assert result.identity.user_id == "from-auth"
        assert result.identity.resolved_from == "authInfo"

    def test_server_identity_wins_over_anchor(self) -> None:
        result = resolve_identity_from_chain(
            server_identity=ServerIdentity(user_id="from-server"),
            anchor=PROCESS_ANCHOR,
        )

        assert result.identity.user_id == "from-server"
        assert result.identity.resolved_from == "explicit"


class TestErrorResilience:
    def test_falls_through_to_next_level_when_resolver_raises(
        self, list_logger: ListLogger
    ) -> None:
        def resolver(_auth: dict[str, Any] | None) -> SetIdentityInput:
            raise RuntimeError("auth service down")

        result = resolve_identity_from_chain(
            resolve_identity=resolver,
            auth_info={},
            server_identity=ServerIdentity(user_id="fallback-server"),
            anchor=SESSION_ANCHOR,
            logger=list_logger,  # type: ignore[arg-type]
        )

        assert result.identity.user_id == "fallback-server"
        assert result.identity.resolved_from == "explicit"
        assert len(list_logger.warnings) == 1
        assert "auth service down" in list_logger.warnings[0]

    def test_falls_through_to_anchor_when_resolver_raises_and_no_server_identity(
        self, list_logger: ListLogger
    ) -> None:
        def resolver(_auth: dict[str, Any] | None) -> SetIdentityInput:
            raise TypeError("cannot read property")

        result = resolve_identity_from_chain(
            resolve_identity=resolver,
            auth_info={},
            anchor=SESSION_ANCHOR,
            logger=list_logger,  # type: ignore[arg-type]
        )

        assert result.identity.resolved_from == "anchor"
        assert len(list_logger.warnings) == 1

    def test_warns_when_resolver_returns_empty(self, list_logger: ListLogger) -> None:
        resolve_identity_from_chain(
            resolve_identity=lambda _auth: SetIdentityInput(),
            auth_info={},
            anchor=SESSION_ANCHOR,
            logger=list_logger,  # type: ignore[arg-type]
        )

        assert len(list_logger.warnings) == 1
        assert "returned empty" in list_logger.warnings[0]

    def test_warns_when_resolver_returns_a_too_short_user_id(
        self, list_logger: ListLogger
    ) -> None:
        result = resolve_identity_from_chain(
            resolve_identity=lambda _auth: SetIdentityInput(user_id="ab"),
            auth_info={},
            anchor=SESSION_ANCHOR,
            logger=list_logger,  # type: ignore[arg-type]
        )

        # The short id is kept (the server rejects, the SDK only warns).
        assert result.identity.user_id == "ab"
        assert result.identity.resolved_from == "authInfo"
        assert len(list_logger.warnings) == 1
        assert "shorter than 5" in list_logger.warnings[0]

    def test_warns_when_server_identity_has_a_too_short_device_id(
        self, list_logger: ListLogger
    ) -> None:
        resolve_identity_from_chain(
            server_identity=ServerIdentity(user_id="valid-user", device_id="xy"),
            anchor=PROCESS_ANCHOR,
            logger=list_logger,  # type: ignore[arg-type]
        )

        assert len(list_logger.warnings) == 1
        assert "device_id" in list_logger.warnings[0]
        assert "shorter than 5" in list_logger.warnings[0]

    def test_does_not_warn_when_ids_are_long_enough(self, list_logger: ListLogger) -> None:
        resolve_identity_from_chain(
            resolve_identity=lambda _auth: SetIdentityInput(user_id="alice@example.com"),
            auth_info={},
            anchor=SESSION_ANCHOR,
            logger=list_logger,  # type: ignore[arg-type]
        )

        assert list_logger.warnings == []
