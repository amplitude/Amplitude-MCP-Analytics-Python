# Ported from amplitude/Amplitude-MCP-Analytics-Node src/utils/debug.ts
# (itself vendored from amplitude/Amplitude-AI-Node @ 97ea346).

"""Debug/dry-run line formatting. Internal."""

from __future__ import annotations

import json
from typing import Any

__all__ = ["format_debug_line", "format_dry_run_line"]


def format_debug_line(event: Any) -> str:
    """Placeholder one-liner for the ``debug`` setting. @internal"""
    event_type = (
        event.get("event_type")
        if isinstance(event, dict)
        else getattr(event, "event_type", None)
    )
    return f"[amplitude-mcp-analytics] {event_type or 'unknown'}"


def format_dry_run_line(event: Any) -> str:
    """@internal"""
    try:
        return json.dumps(event, separators=(",", ":"), ensure_ascii=False, default=repr)
    except (TypeError, ValueError):
        return str(event)
