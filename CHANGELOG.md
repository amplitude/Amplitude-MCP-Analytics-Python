# Changelog

## 0.1.0 (unreleased)

Initial port of [`@amplitude/mcp-analytics`](https://github.com/amplitude/Amplitude-MCP-Analytics-Node)
v0.4.1 to Python. Feature parity with the Node SDK:

- `instrument_server` / `instrument_tool` for FastMCP and low-level
  `mcp.server.lowlevel.Server` (supported range `mcp>=1.16,<2`)
- The five default events — `[MCP] Session Initialized`, `[MCP] Session
  Ended`, `[MCP] Tools Listed`, `[MCP] Tool Call Response`, `[MCP] Tool Call
  Rejected` — with the Node SDK's exact wire contract
- Identity fallback chain (explicit → authInfo → server identity → anchor →
  anonymous floor) with cross-SDK-compatible anchor-derived device ids
- `set_identity`, `set_rationale`, `tool_error`, custom
  `track_server_event`/`track_tool_event`, autocapture config,
  `sanitize_error_message` (fail-closed), `emit_anonymous_event`,
  dry-run/debug modes, serverless unflushed-events warning
- `MockAmplitudeMCPAnalytics` in-memory test double

See `PORTING.md` for intentional divergences from the Node SDK.
