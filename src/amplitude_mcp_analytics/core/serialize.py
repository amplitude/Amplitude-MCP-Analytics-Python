"""Serialization helpers shared by the event emitters. Internal."""

from __future__ import annotations

import json
from typing import Any

__all__ = ["byte_size", "payload_byte_size"]


def _json_default(value: Any) -> Any:
    """``json.dumps`` fallback for values it refuses.

    Pydantic models are the one shape we teach it, because they are the shape
    the size contract already promises to measure (via their own JSON dump) and
    they reach us *nested*: FastMCP validates a tool's arguments before calling
    it, so a declared model parameter arrives as a model inside the kwargs
    dict, where the top-level ``model_dump_json`` path can't see it.

    Anything else re-raises ``TypeError``, so a genuinely unserializable
    payload (a FastMCP ``Context``, a file handle) still measures as ``None``
    rather than as a fabricated size. @internal
    """
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        try:
            return dump(mode="json", by_alias=True, exclude_none=True)
        except Exception as err:
            raise TypeError(f"unserializable model: {type(value).__name__}") from err
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def byte_size(value: Any) -> int | None:
    """Serialized byte size of a value, or ``None`` when absent or not
    JSON-serializable. Best-effort — never raises into the emit path.

    Uses compact separators and ``ensure_ascii=False`` so sizes line up with
    the Node SDK's ``Buffer.byteLength(JSON.stringify(v))`` for plain data
    (sizes remain approximations across SDKs for non-plain values). ``None``
    input means "absent" and returns ``None``. @internal
    """
    if value is None:
        return None
    try:
        encoded = json.dumps(
            value, separators=(",", ":"), ensure_ascii=False, default=_json_default
        )
    except (TypeError, ValueError, RecursionError):
        return None
    return len(encoded.encode("utf-8"))


def payload_byte_size(payload: Any) -> int | None:
    """Byte size of an MCP payload — a result OR a handler's arguments.
    Unwraps a ``ServerResult`` root and serializes pydantic models via
    ``model_dump_json`` (compact, aliased), falling back to :func:`byte_size`
    for plain values. Best-effort.

    This is the measurement every ``[MCP] Request Size`` / ``[MCP] Response
    Size`` goes through, because neither side is guaranteed to be plain data:
    a low-level handler can return a pydantic ``CallToolResult`` and a FastMCP
    tool can be handed a validated pydantic model argument, both of which
    ``json.dumps`` refuses. @internal
    """
    if payload is None:
        return None
    root = getattr(payload, "root", payload)
    dump = getattr(root, "model_dump_json", None)
    if callable(dump):
        try:
            text = dump(by_alias=True, exclude_none=True)
        except Exception:
            return None
        return len(text.encode("utf-8")) if isinstance(text, str) else None
    return byte_size(root)
