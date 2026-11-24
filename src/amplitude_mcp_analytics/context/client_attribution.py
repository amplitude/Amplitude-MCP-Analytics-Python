"""Client attribution validation shared by request resolution and serialization."""

import re
from collections.abc import Mapping
from dataclasses import replace
from typing import Any, cast

from .types import McpClientInfo, McpServerContext, _ClientFamilySource, _ClientNameSource

_NAME_FAMILIES: dict[str, tuple[str, str | None]] = {
    "claude-code": ("claude-code", None),
    "anthropic/claudeai": ("claude", None),
    "anthropic/toolbox": ("claude", None),
    "codex-mcp-client": ("codex", None),
    "cursor": ("cursor", None),
    "cursor-vscode": ("cursor", "vscode"),
    "openai-mcp": ("chatgpt", None),
}
_UA_FAMILIES = {
    "claude-code": "claude-code",
    "codex-mcp-client": "codex",
    "cursor": "cursor",
    "openai-mcp": "chatgpt",
}
_BRIDGE_SUFFIX = re.compile(r" \(via mcp-remote [0-9][a-z0-9.+-]*\)$")
_PRODUCT_UA = re.compile(r"([a-z-]+)/[0-9][a-z0-9.+-]*(?:\s+\(([^()]*)\))?")


def client_string(value: object) -> str | None:
    """Validate a signal while preserving the original value. @internal"""
    return value if isinstance(value, str) and value.strip() else None


def client_field(value: Any, field: str) -> Any:
    """Read from a mapping or a typed client-info object. @internal"""
    return value.get(field) if isinstance(value, Mapping) else getattr(value, field, None)


def explicit_name_source(value: object) -> _ClientNameSource | None:
    """Validate an explicitly supplied raw-name source. @internal"""
    if isinstance(value, str) and value in ("host", "oauth", "request-metadata", "handshake"):
        return cast(_ClientNameSource, value)
    return None


def client_name_source(client: McpClientInfo | None) -> _ClientNameSource:
    """Default an unlabelled host-supplied name to host attribution. @internal"""
    if client is None or client_string(client.name) is None:
        return "none"
    return explicit_name_source(client.name_source) or "host"


def read_client_identity(value: Any, source: _ClientNameSource) -> McpClientInfo:
    """Read the raw identity pair from a single source. @internal"""
    return McpClientInfo(
        name=client_string(client_field(value, "name")),
        version=client_string(client_field(value, "version")),
        name_source=source,
    )


def select_client_identity(*candidates: McpClientInfo) -> McpClientInfo:
    """A named identity wins as a pair. Keep a standalone version only without a name."""
    for candidate in candidates:
        if candidate.name is not None:
            return candidate
    return next(
        (candidate for candidate in candidates if candidate.version is not None), McpClientInfo()
    )


def record_handshake(
    ctx: McpServerContext, client_info: Any, protocol_version: str | None,
) -> None:
    """Update the connection with the successful handshake. @internal"""
    existing = ctx.client
    identity = select_client_identity(
        read_client_identity(client_info, "handshake"),
        read_client_identity(existing, client_name_source(existing)),
    )
    ctx.client = replace(
        existing if existing is not None else McpClientInfo(),
        name=identity.name,
        version=identity.version,
        name_source=identity.name_source,
    )
    if protocol_version is not None:
        ctx.protocol_version = protocol_version


def _match_name(value: str | None) -> tuple[str | None, str | None]:
    name = _BRIDGE_SUFFIX.sub("", (value or "").strip().lower())
    if re.fullmatch(r"local-agent-mode-\S+", name):
        return "claude", "desktop"
    return _NAME_FAMILIES.get(name, (None, None))


def _match_user_agent(value: str | None) -> tuple[str | None, str | None]:
    ua = (value or "").strip().lower()
    if ua == "claude-user":
        return "claude", None
    match = _PRODUCT_UA.fullmatch(ua)
    if match is None:
        return None, None
    family = _UA_FAMILIES.get(match[1])
    return family, "cli" if family == "claude-code" and match[2] == "sdk-cli" else None


def resolve_client_family(
    client: McpClientInfo,
) -> tuple[str | None, str | None, _ClientFamilySource]:
    """Lower family and surface without changing any raw client fields. @internal"""
    name_family, name_surface = _match_name(client_string(client.name))
    ua_family, ua_surface = _match_user_agent(client_string(client.user_agent))
    if name_family is not None:
        surface = name_surface or (ua_surface if name_family == ua_family else None)
        return name_family, surface, "client-name"
    if ua_family is not None:
        return ua_family, ua_surface, "user-agent"
    return None, None, "none"
