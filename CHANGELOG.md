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

### Pre-release API review changes

Applied before the first release, so none of these are breaking changes against
a published version:

- **MIT `LICENSE`** added, with `license`/classifier/`[project.urls]` metadata
  in `pyproject.toml`
- **`amplitude-analytics` is now a hard runtime dependency**; the
  `[amplitude]` extra is gone (`mcp` stays undeclared — duck-typed, and every
  consumer already depends on it)
- **`create_mcp_analytics` removed** — Node's `createMcpAnalytics` alias is
  redundant with the `AmplitudeMCPAnalytics(...)` constructor
- **`set_identity` takes keyword arguments** — `set_identity(user_id=...)` on
  both the module function and the client method; a positional
  `SetIdentityInput` is still accepted (passing both raises `ValueError`)
- **`__version__`** exported from the package root, read from installed
  package metadata
- **`sanitize_rationale` config option** — rewrites or drops `[MCP] Rationale`
  before emission, same fail-closed contract as `sanitize_error_message`; no
  wire change unless configured
- **SDK warnings now log to `amplitude_mcp_analytics`**, always — the injected
  Amplitude client's `configuration.logger` is no longer preferred, so hosts
  silencing the `amplitude` logger no longer lose the SDK's warnings
- **`debug` logs at DEBUG level** instead of printing to stderr (`dry_run` and
  the serverless exit warning still print, deliberately)
- **Re-instrumenting a bound server from a *different* client now warns** —
  the original binding stays active, as before

See `PORTING.md` for intentional divergences from the Node SDK.
