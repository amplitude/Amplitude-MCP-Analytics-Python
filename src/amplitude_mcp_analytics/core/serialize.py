"""Serialization helpers shared by the event emitters. Internal."""

from __future__ import annotations

import json
from typing import Any

__all__ = ["byte_size", "result_byte_size"]


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
        encoded = json.dumps(value, separators=(",", ":"), ensure_ascii=False)
    except (TypeError, ValueError):
        return None
    return len(encoded.encode("utf-8"))


def result_byte_size(result: Any) -> int | None:
    """Byte size of an MCP result — unwraps a ``ServerResult`` root and
    serializes pydantic models via ``model_dump_json`` (compact, aliased),
    falling back to :func:`byte_size` for plain values. Best-effort. @internal"""
    if result is None:
        return None
    root = getattr(result, "root", result)
    dump = getattr(root, "model_dump_json", None)
    if callable(dump):
        try:
            text = dump(by_alias=True, exclude_none=True)
        except Exception:
            return None
        return len(text.encode("utf-8")) if isinstance(text, str) else None
    return byte_size(root)
