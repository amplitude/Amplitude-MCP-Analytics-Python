# Ported from amplitude/Amplitude-MCP-Analytics-Node src/utils/logger.ts
# (itself vendored from amplitude/Amplitude-AI-Node @ 97ea346).
# Python adaptation: the stdlib `logging.Logger` replaces the console-backed
# Logger interface; an injected Amplitude client's `configuration.logger` is
# preferred when present (the amplitude-analytics Config exposes `logger`).

"""Internal logger resolution. Not part of the public package surface."""

from __future__ import annotations

import logging
from typing import Any

__all__ = ["get_logger"]

_default_logger = logging.getLogger("amplitude_mcp_analytics")


def get_logger(amplitude: Any = None) -> logging.Logger:
    """Resolve a logger: prefer a ``logger`` exposed on the underlying
    Amplitude client's configuration, otherwise the SDK's module logger
    (warnings/errors surface via logging's last-resort stderr handler when the
    host hasn't configured logging — matching the Node default of printing
    only warn/error).

    @internal
    """
    if amplitude is not None:
        config = getattr(amplitude, "configuration", None)
        candidate = getattr(config, "logger", None) if config is not None else None
        if candidate is None and isinstance(config, dict):
            candidate = config.get("logger")
        if candidate is not None and hasattr(candidate, "warning"):
            return candidate
    return _default_logger
