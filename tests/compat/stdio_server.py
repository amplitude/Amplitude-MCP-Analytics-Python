"""Subprocess fixture; stdout is reserved for the MCP wire protocol."""

from __future__ import annotations

import json
import sys
from importlib.metadata import version
from pathlib import Path

from amplitude_mcp_analytics.testing import MockAmplitudeMCPAnalytics

if version("mcp").split(".")[0] == "2":
    from mcp.server.mcpserver import MCPServer as Server
else:
    from mcp.server.fastmcp import FastMCP as Server

analytics = MockAmplitudeMCPAnalytics(server_name="stdio-compat", server_version="1")
server = Server("stdio-compat")
analytics.instrument_server(server)


@server.tool()
@analytics.instrument_tool(name="echo")
def echo(label: str) -> str:
    return label


if __name__ == "__main__":
    try:
        server.run()
    finally:
        Path(sys.argv[1]).write_text(json.dumps(analytics.events), encoding="utf-8")
