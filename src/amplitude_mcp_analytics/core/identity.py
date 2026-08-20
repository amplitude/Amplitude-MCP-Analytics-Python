"""Identity resolution: the fallback chain that always yields a valid
Amplitude identity (user_id and/or device_id, each >= 5 chars).

Fallback order (first match wins):
  1. ``set_identity()`` called during this request       -> 'explicit'
  2. ``resolve_identity(auth_info)`` returns non-empty   -> 'authInfo'
  3. Static identity from ``instrument_server`` opts     -> 'explicit'
  4. Correlation anchor available                        -> 'anchor'
  5. No anchor (anonymous per-request floor)             -> 'anonymous'

``set_identity`` is applied lazily (the handler mutates ctx via
``set_identity`` during execution) — this module handles the fallback chain.

Internal.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Any

from ..context.types import IdentityResolver, McpAnchor, McpIdentity, McpTenant

__all__ = [
    "AMP_MCP_NAMESPACE",
    "ResolvedIdentity",
    "ServerIdentity",
    "anchor_device_id",
    "resolve_identity_from_chain",
]

_MIN_ID_LENGTH = 5

# Cross-SDK namespace for anchor-derived device ids. MUST match the Node SDK's
# AMP_MCP_NAMESPACE so the same anchor yields the same device_id from either
# SDK (uuid5 is the RFC 9562 §5.5 SHA-1 name-based UUID).
AMP_MCP_NAMESPACE = uuid.UUID("6ba7b812-9dad-11d1-80b4-00c04fd430c8")


def anchor_device_id(anchor_key: str) -> str:
    """Deterministic device id for an anchor key (``"{type}:{value}"``)."""
    return str(uuid.uuid5(AMP_MCP_NAMESPACE, anchor_key))


@dataclass
class ServerIdentity:
    """Static identity passed to ``instrument_server()`` — server identity of
    the chain."""

    user_id: str | None = None
    device_id: str | None = None
    tenant: McpTenant | None = None


@dataclass
class ResolvedIdentity:
    """The result of identity resolution: identity + optional tenant override."""

    identity: McpIdentity
    tenant: McpTenant | None = None


def resolve_identity_from_chain(
    *,
    anchor: McpAnchor,
    resolve_identity: IdentityResolver | None = None,
    auth_info: dict[str, Any] | None = None,
    server_identity: ServerIdentity | None = None,
    logger: logging.Logger | None = None,
) -> ResolvedIdentity:
    """Run the fallback chain. ``set_identity`` is handled lazily by the
    context module during handler execution."""
    # resolve_identity callback (Streamable HTTP / OAuth path)
    if resolve_identity is not None:
        try:
            resolved = resolve_identity(auth_info)
            if resolved.user_id is not None or resolved.device_id is not None:
                identity = _apply_anchor_fallback(
                    McpIdentity(
                        resolved_from="authInfo",
                        user_id=resolved.user_id,
                        device_id=resolved.device_id,
                    ),
                    anchor,
                )
                _warn_short_ids(logger, identity, "resolve_identity")
                return ResolvedIdentity(identity=identity, tenant=resolved.tenant)
            if logger is not None:
                logger.warning(
                    "resolve_identity callback returned empty — "
                    "falling through to next identity level."
                )
        except Exception as err:  # noqa: BLE001 — best-effort chain, never raises
            if logger is not None:
                logger.warning(
                    "resolve_identity callback raised: %s — "
                    "falling back to next identity level.",
                    err,
                )

    # server identity from instrument_server opts (stdio / single-user)
    if server_identity is not None and (
        server_identity.user_id is not None or server_identity.device_id is not None
    ):
        identity = _apply_anchor_fallback(
            McpIdentity(
                resolved_from="explicit",
                user_id=server_identity.user_id,
                device_id=server_identity.device_id,
            ),
            anchor,
        )
        _warn_short_ids(logger, identity, "instrument_server")
        return ResolvedIdentity(identity=identity, tenant=server_identity.tenant)

    # anchor-based identity
    if anchor.type != "anonymous":
        anchor_key = f"{anchor.type}:{anchor.value}"
        return ResolvedIdentity(
            identity=McpIdentity(
                resolved_from="anchor",
                user_id=anchor_key,
                device_id=anchor_device_id(anchor_key),
            )
        )

    # anonymous per-request floor
    device_id = str(uuid.uuid4())
    return ResolvedIdentity(
        identity=McpIdentity(
            resolved_from="anonymous",
            device_id=device_id,
            user_id=f"anonymous:{device_id}",
        )
    )


def _warn_short_ids(
    logger: logging.Logger | None, identity: McpIdentity, source: str
) -> None:
    if logger is None:
        return
    description = f"Amplitude silently drops IDs shorter than {_MIN_ID_LENGTH} characters."
    if identity.user_id is not None and len(identity.user_id) < _MIN_ID_LENGTH:
        logger.warning(
            '%s returned user_id "%s" (%d chars) — %s',
            source,
            identity.user_id,
            len(identity.user_id),
            description,
        )
    if identity.device_id is not None and len(identity.device_id) < _MIN_ID_LENGTH:
        logger.warning(
            '%s returned device_id "%s" (%d chars) — %s',
            source,
            identity.device_id,
            len(identity.device_id),
            description,
        )


def _apply_anchor_fallback(identity: McpIdentity, anchor: McpAnchor) -> McpIdentity:
    """If the resolved identity has a user_id but no device_id, derive
    device_id from the anchor for cross-call correlation. Explicit device_id is
    used as-is."""
    if identity.device_id is not None:
        return identity
    if anchor.type == "anonymous":
        return identity
    anchor_key = f"{anchor.type}:{anchor.value}"
    identity.device_id = anchor_device_id(anchor_key)
    return identity
