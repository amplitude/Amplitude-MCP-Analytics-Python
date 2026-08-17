# Ported from amplitude/Amplitude-MCP-Analytics-Node src/utils/logger.ts
# (itself vendored from amplitude/Amplitude-AI-Node @ 97ea346).
# Python adaptation: the stdlib `logging.Logger` replaces the console-backed
# Logger interface, and the SDK owns a single namespace of its own rather than
# borrowing the injected Amplitude client's logger (see get_logger).

"""Internal logger resolution. Not part of the public package surface."""

from __future__ import annotations

import logging

__all__ = ["get_logger"]

LOGGER_NAME = "amplitude_mcp_analytics"

_logger = logging.getLogger(LOGGER_NAME)


def get_logger() -> logging.Logger:
    """The SDK's logger — always ``logging.getLogger("amplitude_mcp_analytics")``.

    This deliberately ignores the injected Amplitude client's
    ``configuration.logger``. That preference looked like "respect the host's
    logging choice", but the amplitude-analytics client's logger is never
    ``None``, so it applied to *every* installation: all of this SDK's warnings
    were emitted into the ``amplitude`` namespace, and a host that silences that
    (deservedly chatty) logger silently lost them. Python already has a
    host-facing configuration surface for this — the logger hierarchy — so the
    SDK names itself and lets the host route, filter, or silence
    ``amplitude_mcp_analytics`` on its own terms. Warnings and errors still
    surface through logging's last-resort stderr handler when the host has
    configured nothing, matching the Node default of printing only warn/error.

    @internal
    """
    return _logger
