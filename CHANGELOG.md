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

### Code-review follow-ups

Also pre-release. Where the Node SDK has the same behavior, fixing it here is a
deliberate divergence with a row in `PORTING.md`:

- **Release publishing is authorized by the forge, not by PR text** —
  `publish.yml` now additionally requires the merged PR to be authored by
  `github-actions[bot]` and to come from a `release/v*` head branch (the two
  facts `release.yml` actually produces), instead of resting on a title prefix
  and a body marker any PR author can copy into a PR that would then run with
  `contents: write` and the PyPI token. The title/body checks stay as defense
  in depth
- **W3C `traceparent` is read from the HTTP header**, with `_meta.traceparent`
  as the fallback — a normally propagated header no longer falls through to a
  fresh anonymous anchor (and, by default, a dropped event) on stateless HTTP.
  The anchor value is unchanged, so correlation still lines up with the Node
  SDK; stdio is unaffected
- **`traceparent` is validated against the full W3C shape** before its trace id
  becomes a correlation anchor — malformed values such as
  `00-<trace-id>-0000000000000000-01-junk`, an all-zero parent id, or the `ff`
  version no longer stitch unrelated requests together. A valid header parses
  exactly as before
- **`[MCP] Response Size` / `[MCP] Request Size` now cover pydantic payloads** —
  a low-level handler returning a `CallToolResult`, or a tool handed a
  validated model argument, is measured via `model_dump_json` as the size
  contract documents, instead of silently losing the property. Genuinely
  unserializable values still omit it
- **`flush()` / `shutdown()` settle the unflushed counters only after the
  underlying call succeeds** — a failed flush no longer suppresses the
  serverless "events were never flushed" warning for events that are still
  queued
- **The instrumentation marker is stamped on both views of a server** (a
  `FastMCP` and its `_mcp_server`), so instrumenting one after the other is a
  no-op with the usual different-client warning instead of a second client that
  looks configured while emitting nothing
- **An empty-string `sanitize_rationale` replacement is emitted** as
  `[MCP] Rationale: ""` rather than dropped — the sanitizer contract omits the
  property only on `None`, a non-string, or a raise, which is what
  `sanitize_error_message` already did

See `PORTING.md` for intentional divergences from the Node SDK.
