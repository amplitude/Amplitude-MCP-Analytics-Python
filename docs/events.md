# Event reference

Every event `amplitude-mcp-analytics` sends to Amplitude, with the exact
conditions under which it fires and every property it can carry. For setup and
API usage, see the [README](../README.md).

Events are delivered through the standard Amplitude Python client (`track()`),
so they land in your project like any other event. All default event names and
all SDK-derived properties carry the `[MCP] ` prefix (including the trailing
space) so they never collide with same-named events or properties sent by other
Amplitude SDKs on the same project.

## Events at a glance

| Event | Fires when | Transports | Toggle |
| -- | -- | -- | -- |
| [`[MCP] Session Initialized`](#mcp-session-initialized) | The `initialize` handshake completes | every transport that handshakes | `autocapture.session_lifecycle` |
| [`[MCP] Session Ended`](#mcp-session-ended) | The run settles (transport close), after an initialized session | stdio, stateful Streamable HTTP, SSE | `autocapture.session_lifecycle` |
| [`[MCP] Tools Listed`](#mcp-tools-listed) | A `tools/list` request is served | all | `autocapture.tools_listed` |
| [`[MCP] Tool Call Response`](#mcp-tool-call-response) | An instrumented tool call settles | all | `autocapture.tool_calls` |
| [`[MCP] Tool Call Rejected`](#mcp-tool-call-rejected) | A `tools/call` request fails before any tool callback runs | all | `autocapture.tool_calls` |

(`session_lifecycle` and `tools_listed` default to the umbrella
`autocapture.server_events` value — see the README's
[Choosing what's captured](../README.md#choosing-whats-captured).)

A sessionless Streamable HTTP transport still performs the `initialize`
handshake, so initialization is reported with no session id. It does not emit
an end event because its run lives for only one request and has no meaningful
session duration.
`[MCP] Tools Listed`, `[MCP] Tool Call Response`, and
`[MCP] Tool Call Rejected` fire on every transport.

All five events require `instrument_server(server)` to have been called before
the server runs. `instrument_tool` without a bound server is a no-op
passthrough: the handler runs untouched and nothing is emitted (a one-time
warning is logged).

### Emission guarantees

- **Best-effort, never raises.** A failure inside the SDK or the Amplitude
  client is swallowed and logged; it never breaks the handler, the `tools/list`
  response, or the connection. Handler exceptions are re-raised to the MCP SDK
  *after* the failure event is emitted.
- **Aggregate-only floor, with one skip rule.** When no real identity is
  available the SDK emits under a synthetic, anchor-derived identity (see
  [Identity](#identity-on-every-event)). If an event would carry *neither* a
  resolvable identity *nor* a tenant — a fully anonymous request on stateless
  HTTP with no trace context — it is **dropped**, not emitted as noise.
- **`dry_run`** (`MCPAnalyticsConfig`) builds events normally but does not
  deliver them; `debug` logs each emission.

## Identity on every event

Alongside `event_properties`, every event carries the standard Amplitude
identity fields, resolved per request through a fixed fallback chain (first
match wins):

| Order | Source | Identity `resolvedFrom` |
| -- | -- | -- |
| 1 | `analytics.set_identity(user_id=..., ...)` called during the request | `explicit` |
| 2 | `resolve_identity(auth_info)` callback (per-tool opt-in) returning a non-empty result | `authInfo` |
| 3 | Static identity from `instrument_server(server, user_id=..., device_id=..., tenant=...)` | `explicit` |
| 4 | Correlation anchor available (process / session id / trace) | `anchor` |
| 5 | Anonymous per-request floor | `anonymous` |

- **`user_id`** — the resolved user id. At level 4 it is the synthetic key
  `<anchor type>:<anchor value>` (e.g. `process:12345-1f0c…`,
  `session-id:8f4c…`); at level 5 it is `anonymous:<device id>`.
- **`device_id`** — the resolved device id. When you supply a `user_id` without
  a `device_id`, the SDK derives a stable device id from the anchor (a UUIDv5 of
  the anchor key, under this SDK's private namespace) so calls in the same
  session/process/trace correlate. At level 5 it is a random UUID per request.

  > The `process` anchor value is `<pid>-<random hex>`, not a bare pid. Pids are
  > small integers recycled per machine, so a bare pid made two unrelated
  > servers on different hosts resolve to the same `user_id` and `device_id`.
  > The random component is minted once per process and is stable for that
  > process's lifetime, which is the correlation scope this anchor promises.
- **`groups`** — set to `{tenant.group_type: tenant.group_value}` when a
  tenant was provided (via `set_identity`, `resolve_identity`, or
  `instrument_server` options). The tenant is carried on Amplitude `groups`,
  **not** as an event property. Via `resolve_identity` or the
  `instrument_server` options a tenant only takes effect alongside a
  `user_id`/`device_id` from the same source; `set_identity` and manually built
  contexts can set it on its own.

Amplitude silently drops ids shorter than 5 characters; the SDK warns when a
resolved id is that short.

**Skip rule.** An event whose identity resolved to the anonymous floor (level
5) *and* that has no tenant is dropped entirely. In practice this only affects
stateless Streamable HTTP requests with no identity configured and no
`traceparent` propagated.

### Correlation anchor

The anchor is the per-request correlation key the SDK derives from the
transport. It feeds `[MCP] Anchor Type`, `[MCP] Session ID`, and the synthetic
identity floor:

| Transport | Anchor (`[MCP] Anchor Type`) | Anchor value | `[MCP] Session ID` |
| -- | -- | -- | -- |
| stdio | `process` | Server process id | `no-session` |
| Streamable HTTP, session id present (stateful) | `session-id` | The transport session id (`mcp-session-id` header) | The session id |
| SSE | `session-id` | The `session_id` query param | The session id |
| Streamable HTTP, stateless, W3C `traceparent` propagated | `trace` | The 32-hex trace id | `no-session` |
| Streamable HTTP, stateless, no trace context | `anonymous` | Random UUID per request | `no-session` |

The trace anchor reads the standard `traceparent` HTTP header first and
`_meta.traceparent` second, so a normally propagated W3C trace context
correlates without the client doing anything MCP-specific. Only a
fully-valid `traceparent` counts (`version-traceid-parentid-flags`, all hex,
no all-zero trace or parent id); a malformed one is treated as absent rather
than anchored on.

A session id is never assumed or fabricated — its absence is what selects the
stateless branch. One host escape hatch: a session id bound via
`instrument_server(session_id=...)` fills the session-id slot when the
transport itself carries none (for per-request servers whose session ids live
in the host's own store rather than on the transport).

### Client identity

Client fields resolve, by field, from `instrument_server(resolve_client_info=)`
first, then namespaced per-request `_meta`
(`io.modelcontextprotocol/clientInfo`), the unnamespaced spelling, the
`initialize` handshake, and finally the HTTP `User-Agent` for the user-agent
field only. The resolver receives a `ResolveClientInfoInput` containing the
request's verified `auth_info` and HTTP headers. Empty fields and exceptions
fall through to the next source.

`[MCP] OAuth Client ID` is separate. It comes from `auth_info["client_id"]` or
the resolver and is never inherited from the connection, so it always
describes the request carrying the event. It identifies a registration, not a
product, and is deliberately not used as `[MCP] Client Name`.

## Shared properties

Every event — the five default events *and* custom events emitted through
`track_server_event` / `track_tool_event` — carries these SDK-derived
properties:

| Property | Type | Present | Value |
| -- | -- | -- | -- |
| `[MCP] Session ID` | string | always | The protocol session id when the anchor is a session id; the literal `no-session` otherwise |
| `[MCP] Client Name` | string | always | MCP client name from the per-request resolver, namespaced/legacy `_meta`, or handshake; `unknown` when unavailable |
| `[MCP] Client Version` | string | when known | MCP client version, same sources as the name |
| `[MCP] OAuth Client ID` | string | when authenticated or resolved | OAuth client registration id for this request; never folded into the client name |
| `[MCP] User Agent` | string | always | Raw HTTP `User-Agent` header (Streamable HTTP / SSE); `unknown` otherwise (always `unknown` on stdio) |
| `[MCP] Server Name` | string | always | `server_name` from the client options |
| `[MCP] Server Version` | string | when set | `server_version` from the client options (always set when instrumented through `instrument_server`) |
| `[MCP] Server Type` | string | when set | Server classification; only present when set on a manually built context |
| `[MCP] Transport` | string | always | `stdio`, `streamable-http`, or `sse`, auto-detected from the message stream (or the `transport=` override on `instrument_server`). `sse` — the deprecated HTTP+SSE transport — is a **Python-SDK-only addition**; the Node SDK emits only `stdio`/`streamable-http` |
| `[MCP] Anchor Type` | string | always | `session-id`, `trace`, `process`, or `anonymous` — see [Correlation anchor](#correlation-anchor) |
| `[MCP] Protocol Version` | string | when known | Negotiated MCP protocol revision: the `MCP-Protocol-Version` HTTP header, namespaced/legacy `_meta` per request, or the value negotiated at the `initialize` handshake |
| `[MCP] Auth Type` | string | when configured | The `auth_type` passed to `instrument_server` (e.g. `oauth`); values are server-specific |

On top of these, any **`extra`** enrichment bags in scope ride along as
event properties: the server-scope bag (`extra=` in `instrument_server`)
on every event, plus the tool-scope bag (`extra=` in `instrument_tool`) on
tool-scope events. See [Property precedence](#property-precedence).

## `[MCP] Session Initialized`

Marks the start of a protocol session.

- **Fires when:** the MCP `initialize` request succeeds. The SDK wraps that
  request handler, where it captures `clientInfo` and protocol version.
- **Transports:** every transport that performs the handshake, including
  sessionless Streamable HTTP.
- **Toggle:** `autocapture.session_lifecycle` (defaults to
  `autocapture.server_events`).
- **Identity:** resolved at the handshake from the static `instrument_server`
  identity, else the anchor (the transport's session id, or the process on
  stdio). Per-request sources (`set_identity`, `resolve_identity`) don't apply
  — no tool call is in flight.

**Properties:** the [shared properties](#shared-properties) and the
server-scope `extra` bag only; this event has no event-specific properties.

## `[MCP] Session Ended`

Marks the end of a protocol session.

- **Fires when:** the run settles — `run()` returning or raising is the
  transport closing — but only when `[MCP] Session Initialized` was emitted for
  that connection first and the transport outlives one request. Sessionless
  runs and never-initialized connections never emit it.
- **Transports / toggle / identity:** persistent transports only; the
  event reuses the session context resolved at the handshake.

**Event-specific properties:**

| Property | Type | Present | Value |
| -- | -- | -- | -- |
| `[MCP] Session Duration` | number (ms, integer) | when known | Wall-clock time from the `initialize` handshake to the run's teardown, rounded to the nearest millisecond |

## `[MCP] Tools Listed`

Reports each `tools/list` request the server serves, with the live tool set at
call time (tools added or removed after startup are reflected).

- **Fires when:** the server's `tools/list` handler returns **or throws** for a
  client request. The handler's behavior is unchanged — its result or raise
  passes through untouched. Only client-initiated requests count: the Python
  MCP SDK also invokes the handler internally to refresh its tool cache during
  `tools/call` dispatch, and those internal calls never emit.
- **Transports:** all. On stateless HTTP the [skip rule](#emission-guarantees)
  applies per request.
- **Toggle:** `autocapture.tools_listed` (defaults to
  `autocapture.server_events`).
- **Identity:** resolved per request through the fallback chain, minus the
  per-tool sources (`set_identity` needs a tool handler in flight;
  `resolve_identity` is bound per tool).

**Event-specific properties:**

| Property | Type | Present | Value |
| -- | -- | -- | -- |
| `[MCP] Is Error` | boolean | always | `true` when the `tools/list` handler raised |
| `[MCP] Tool Count` | number | always | Number of tools returned. Always the **true total**, even when the names list is truncated; `0` on failure |
| `[MCP] Tool Names` | string[] | when ≥ 1 tool | The returned tool names, capped at **100** entries |
| `[MCP] Tool Names Truncated` | boolean | only when capped | `true` when more than 100 names were returned and the list was truncated; absent otherwise |
| `[MCP] Response Duration` | number (ms, integer) | always | Wall-clock duration of the `tools/list` handler |
| `[MCP] Response Size` | number (bytes) | on success, when serializable | Serialized byte size of the full `tools/list` result |
| `[MCP] Error Message` | string | on failure | Message of the classified error |
| `[MCP] Error Code` | string | on failure, when a specific code is known | Machine-readable error identifier — see [Error classification](#error-classification) |
| `[MCP] Error Type` | string | on failure | Error category — see [Error classification](#error-classification) |

## `[MCP] Tool Call Response`

The default tool-execution event — one per call of a handler wrapped with
`instrument_tool`.

- **Fires when:** the wrapped handler settles: returns (sync or async), or
  raises. On failure the event is emitted **before** the error is re-raised
  to the MCP SDK.
- **A call counts as a failure when** the handler raises, **or** it
  returns an in-band error result (`CallToolResult` with `isError: true`).
- **Transports:** all. The stateless-HTTP skip rule applies per request.
- **Toggle:** `autocapture.tool_calls`. When off, the wrapper still builds and
  provides `ctx` (so `set_identity` and custom events keep working) but emits no
  default event.
- **Identity:** resolved per request through the full fallback chain,
  including `set_identity` calls made inside the handler and the
  `resolve_identity` callback passed to `instrument_tool`.

**Event-specific properties** (on top of the shared set):

| Property | Type | Present | Value |
| -- | -- | -- | -- |
| `[MCP] Tool Name` | string | always | `name` from the tool metadata |
| `[MCP] Tool Owner` | string | when set | `owner` from the tool metadata |
| `[MCP] Tool Tags` | string[] | when set, non-empty | `tags` from the tool metadata (`meta`) |
| `[MCP] Tool Category` | string | when set, non-empty | `category` from the tool metadata (`meta`) |
| `[MCP] Rationale` | string | when the host called `set_rationale` during the call, unless dropped by [`sanitize_rationale`](#redacting-mcp-rationale) | Why the agent called this tool, as supplied by the host — never sniffed out of tool inputs by the SDK. Truncated to 1000 characters |
| `[MCP] Is Error` | boolean | always | `true` on a raised exception or an in-band `isError` result |
| `[MCP] Response Duration` | number (ms, integer) | always | Wall-clock handler duration, rounded |
| `[MCP] Request Size` | number (bytes) | when the call carried arguments, when serializable | Serialized byte size of the tool's arguments — the handler's keyword arguments (how FastMCP calls tools) or its first positional argument (low-level handlers). Absent for calls with no arguments |
| `[MCP] Response Size` | number (bytes) | when the handler returned, when serializable | Serialized byte size of the returned result. Absent when the handler raised |
| `[MCP] Error Message` | string | on failure | Message of the classified error |
| `[MCP] Error Code` | string | on failure, when a specific code is known | Machine-readable error identifier — the host's `code` from `analytics.tool_error()`, a raised error's own `code` attribute, or a network code assigned by the classifier. Absent for a bare raised exception with no code |
| `[MCP] Error Type` | string | on failure | Error category — see [Error classification](#error-classification) |
| `[MCP] Error HTTP Status` | number | on failure, when the failure carried one | HTTP status attached to the tool's failure (the tool's, not the transport's) — see [Error classification](#error-classification) |

The tool-scope `extra` bag (from the tool metadata) and the server-scope bag
both ride along as additional properties. The bag is read at emit time, so a
handler may enrich `ctx.tool.extra` mid-call and the values land on this event.

**Example** (success, stateful Streamable HTTP, OAuth-resolved identity):

```json
{
  "event_type": "[MCP] Tool Call Response",
  "user_id": "user-123",
  "device_id": "1c6afb4c-6ba7-5f5d-9c2e-6a1f8d1f2ab3",
  "groups": { "org id": "456" },
  "event_properties": {
    "[MCP] Session ID": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
    "[MCP] Client Name": "cursor",
    "[MCP] Client Version": "0.40",
    "[MCP] User Agent": "node",
    "[MCP] Server Name": "my-mcp-server",
    "[MCP] Server Version": "1.0.0",
    "[MCP] Transport": "streamable-http",
    "[MCP] Anchor Type": "session-id",
    "[MCP] Protocol Version": "2025-11-25",
    "[MCP] Auth Type": "oauth",
    "[MCP] Tool Name": "search_docs",
    "[MCP] Tool Owner": "docs-team",
    "[MCP] Is Error": false,
    "[MCP] Response Duration": 184,
    "[MCP] Request Size": 64,
    "[MCP] Response Size": 2048,
    "feature flag": "new-ranker"
  }
}
```

## `[MCP] Tool Call Rejected`

Reports each `tools/call` request that fails **before any tool callback
runs**: the requested tool doesn't exist, or the arguments fail input-schema
validation. No handler executes, so these would otherwise be invisible —
`[MCP] Tool Call Response` only fires for dispatched calls.

In the Python MCP SDK, the `tools/call` handler funnels every failure through
an in-band `isError` result: input-schema validation, FastMCP's unknown-tool
error, and a failing tool all reach the protocol layer with the **same shape**,
and the client receives an `isError` result rather than a JSON-RPC error
envelope. The SDK therefore requires positive evidence before calling
something a rejection (see "Not emitted for" below), and the event means the
same thing here as in the Node SDK: a `tools/call` that failed before any tool
callback ran.

This is a separate event on purpose. `[MCP] Tool Call Response`'s dimensions
are execution-scoped (tool metadata, handler duration, request size); a
rejected request has none of them. And the attempted name is unvalidated
caller input — hallucinated, mistyped, or since-removed tool names — so it
rides on `[MCP] Attempted Tool Name` and never lands on the `[MCP] Tool Name`
reserved key that per-tool dashboards slice on.

- **Fires when:** the server's `tools/call` request handler settles with a
  failure the SDK can positively attribute to a pre-dispatch cause — an in-band
  `isError` result (the usual Python path) or, rarely, a raise — and no
  `instrument_tool`-wrapped callback ran for that request. The handler's own
  behavior passes through untouched. A call that reached a tool callback never
  emits this event — even when the protocol layer fails after dispatch (e.g.
  output-schema validation) — it is reported on `[MCP] Tool Call Response`
  instead, so one request never lands on both.
- **Not emitted for** a tool that is registered but not wrapped with
  `instrument_tool` and returns its own `isError` result. That is shaped
  identically to a rejection, so the SDK requires positive evidence — the name
  being absent from the server's tool registry, the SDK's own pre-dispatch
  wording, or an `MCP error <code>:` message prefix — before reporting one. An
  uninstrumented tool's failure is left unreported rather than misfiled as
  `protocol_error`.
- **Not emitted for output-schema failures**, which happen *after* the callback
  returns. An instrumented tool is covered by the dispatch marker, but an
  uninstrumented one's callback runs unobserved, so output-validation wording
  (`Output validation error`, invalid structured content) is what keeps it out
  of this event — it already executed, and reporting it here would contradict
  the event's meaning.
- **Not covered:** failures the MCP server itself never sees — e.g. a
  transport- or host-level `4xx` for an invalid session id, written before the
  request reaches the protocol layer. Emit those from your host if you need
  them.
- **Transports:** all. On stateless HTTP the [skip rule](#emission-guarantees)
  applies per request.
- **Toggle:** `autocapture.tool_calls`.
- **Identity:** resolved per request through the fallback chain, minus the
  per-tool sources (`set_identity` needs a tool handler in flight;
  `resolve_identity` is bound per tool).

**Event-specific properties:**

| Property | Type | Present | Value |
| -- | -- | -- | -- |
| `[MCP] Attempted Tool Name` | string | always | `params.name` as sent by the client — unvalidated input, capped at **200** characters |
| `[MCP] Rejection Reason` | string | always | Why the call was rejected: `unknown_tool`, `disabled_tool`, `schema_validation`, or `unrecognized` — see [Telling rejections apart](#telling-rejections-apart) |
| `[MCP] Is Error` | boolean | always | Always `true` — every rejection is a failure |
| `[MCP] Error Message` | string | unless dropped by [`sanitize_error_message`](#redacting-mcp-error-message) | Message of the classified error, as the client saw it (e.g. `Unknown tool: foo`, `Input validation error: …`) |
| `[MCP] Error Code` | string | when recoverable | The JSON-RPC error code for the rejection (e.g. `-32602` invalid params). The Python SDK reports pre-dispatch failures as in-band results with no structural code, so it is parsed back out of an `MCP error <code>:` message prefix when one is present; an attributed in-band rejection with no recoverable code falls back to `-32603` (internal error). Absent only when a raised rejection carried no code at all |
| `[MCP] Error Type` | string | always | Always `protocol_error` — see [Error classification](#error-classification) |
| `[MCP] Response Duration` | number (ms, integer) | always | Wall-clock duration of the `tools/call` handler |
| `[MCP] Response Size` | number (bytes) | when serializable | Serialized byte size of the response the client received — the in-band `isError` result the SDK produced, or the JSON-RPC error envelope reconstructed from a raise |
| `[MCP] Response HTTP Status` | number | HTTP transports only | `200` — protocol-level rejections answer HTTP 200 with the error in the JSON-RPC body. Absent over stdio, which has no HTTP status |

### Telling rejections apart

In the Python SDK every pre-dispatch failure arrives with the **same** shape —
an in-band `isError` result, the same `[MCP] Error Type` (`protocol_error`),
and usually no distinguishing code — so neither the code nor the type
separates the causes, and the difference otherwise survives only in the
message text — which
[`sanitize_error_message`](#redacting-mcp-error-message) may rewrite or drop.

`[MCP] Rejection Reason` carries the cause structurally instead. It contains no
caller data, so it is unaffected by sanitization:

| Value | Meaning |
| -- | -- |
| `unknown_tool` | The requested name is not registered — hallucinated, mistyped, or since removed. Pair with `[MCP] Attempted Tool Name` to see which names clients expect |
| `disabled_tool` | Registered but turned off. **Unreachable with the official Python MCP SDK**, which has no tool disable — kept for cross-SDK schema parity, so dashboards spanning Node and Python servers share one closed set |
| `schema_validation` | The name resolved, but the payload failed the tool's schema |
| `unrecognized` | A pre-dispatch failure that could not be attributed further |

The values are not all equally robust, and it is worth knowing which is which
before building on them:

- `unknown_tool` is read from the server's own tool registry (FastMCP's tool
  manager). It does not depend on message wording at all (wording is consulted
  only for a low-level `Server`, which has no registry). The same applies to
  `disabled_tool`, should a future SDK make it reachable.
- **`schema_validation` is matched from the SDK's error text.** The registry
  can prove the name resolved but not why the call failed, so this one value is
  derived from prose. It matches the SDK's validation wording — the low-level
  server's `Input validation error: …` (with `validate_input=True`) and
  pydantic's `N validation error(s) for …` phrasing surfaced through FastMCP —
  and it is the least durable of the four: upstream has reworded validation
  messages before, and a future reword would surface as `unrecognized` rather
  than `schema_validation`. Treat a sudden shift between those two buckets
  after an SDK upgrade as a wording change, not a behavior change.

A registered, live tool that fails pre-dispatch for some other reason is reported
as `unrecognized` rather than being labelled a schema problem it may not be.

`unrecognized` is the deliberate catch-all whenever the cause cannot be
established: a low-level `Server` (which has no registry) whose message wording
is also unfamiliar, or a pre-dispatch failure mode a future SDK introduces. The
rejection is still recorded with its code and duration — only the attribution is
withheld. If you see `unrecognized` climbing after an SDK upgrade, that is the
signal to look at what upstream added.

## Error classification

`[MCP] Error Type` is a **closed, SDK-owned** set — the SDK assigns it; hosts
never pick it. It appears on `[MCP] Tool Call Response`, `[MCP] Tools Listed`,
and `[MCP] Tool Call Rejected`, and carries one of:

| Value | Meaning |
| -- | -- |
| `returned_error` | The tool returned an in-band error result (`isError: true`), including everything built with `analytics.tool_error()` |
| `thrown_exception` | The handler raised an exception not matching a more specific rule |
| `timeout` | The raised error was a timeout or cancellation — `TimeoutError` (which covers `socket.timeout` and `asyncio.TimeoutError`) or `asyncio.CancelledError`, the closest analogue of Node's `AbortError` |
| `transport_error` | The raised error was a network/DNS failure — the `ConnectionError` family or `socket.gaierror`. Emitted with the matching lowercase Node-style code (`econnrefused`, `econnreset`, `epipe`, `econnaborted`, `enotfound`) as `[MCP] Error Code`, so values line up across SDKs |
| `rate_limited` | The raised error carried HTTP status **429** (sniffed as below). The structured error on `ctx.error` also sets `retry_suggested` |
| `protocol_error` | A `tools/call` failed before dispatch (unknown tool, input-schema validation) — always the type on `[MCP] Tool Call Rejected` |
| `unknown` | A non-exception value was classified |

An HTTP status carried on a raised error is sniffed via the dominant Python
conventions — `err.status` (aiohttp), `err.status_code`, then
`err.response.status_code` (httpx, requests) — and emitted as
`[MCP] Error HTTP Status`; a sniffed `429` is what selects `rate_limited`. For
error shapes the SDK can't sniff, set the status explicitly via
`analytics.tool_error(ctx, ..., http_status=...)`.

Raised values are classified automatically. For in-band error results, the
default is `returned_error` with `[MCP] Error Message` taken from the result's
text content; to attach a machine-readable code and control the message, build
the result with `analytics.tool_error(ctx, code=..., message=..., ...)` — the
structured error is stored on `ctx.error` and the event reports your values.
Host-built errors are always `returned_error`; describe the specific failure
through the free-form **`code`** (emitted as `[MCP] Error Code`), not the type.

`[MCP] Error Message` and `[MCP] Error Type` are emitted on a failure (the
message unless [`sanitize_error_message`](#redacting-mcp-error-message) drops
it). `[MCP] Error Code` is the finer-grained companion to the type and is
emitted **only when a specific identifier is known** — a host `code`, a raised
error's own `code` attribute (an errno/OS string), a classifier-assigned
network code, or a JSON-RPC code.
It is omitted for a bare raised exception rather than echoing the type, so its
presence means "there is a specific known reason." The rest of the structured
error (`recoverable`, `fingerprint`, privacy-safe `stack_hash`, …) stays on
`ctx.error`, where a custom event can pick it up.

### Redacting `[MCP] Error Message`

`[MCP] Error Message` is the one error property carrying free text the SDK did
not compose. For an in-band `isError` result it is the result's own message; for
a rejection it is the MCP SDK's validation text, which quotes the offending
argument value. Either can contain end-user data that no server code
deliberately sent — `No subscriber found for "jane@example.com"`.

`sanitize_error_message` rewrites or drops the property before it is emitted.
Return a replacement string, or `None` to omit it entirely:

```python
import re

from amplitude_mcp_analytics import MCPAnalyticsConfig

# Redact, keeping the shape of the message for debugging:
MCPAnalyticsConfig(
    sanitize_error_message=lambda message: re.sub(
        r"[\w.+-]+@[\w-]+\.[\w.]+", "<email>", message
    ),
)

# Or drop the property outright:
MCPAnalyticsConfig(sanitize_error_message=lambda message: None)
```

It applies to every event that carries the property — `[MCP] Tools Listed`,
`[MCP] Tool Call Response`, and `[MCP] Tool Call Rejected` — so no code path can
bypass it. `[MCP] Error Code` and `[MCP] Error Type` are untouched, which keeps
failures segmentable with no message text in the event stream.

Two deliberate behaviors: the sanitizer never sees a successful call, and it
**fails closed**. A sanitizer that raises (or returns anything other than a
string) omits the property rather than falling back to the raw message — the
value it exists to suppress is never emitted because the function was buggy. The
rest of the event is unaffected, and the tool response never breaks.

The message reaching the client is never modified; this affects telemetry only.
To control the client-facing text as well, build the result with
`analytics.tool_error(ctx, code=..., message=...)`.

### Redacting `[MCP] Rationale`

`[MCP] Rationale` is the other free-text property, and the only one the *model*
writes. A rationale is prose about why a tool was called, so it can quote the
end user's prompt verbatim — `"the user wants the invoice for
jane@example.com"` — even when every argument the tool received is clean. Your
server chose to call `set_rationale`, but it did not choose the words.

`sanitize_rationale` is the counterpart to `sanitize_error_message`: same
signature, same fail-closed contract.

```python
from amplitude_mcp_analytics import MCPAnalyticsConfig

# Truncate to a length that keeps the category but not the quoted prompt:
MCPAnalyticsConfig(sanitize_rationale=lambda rationale: rationale[:120])

# Or drop the property outright, keeping the rest of the tool event:
MCPAnalyticsConfig(sanitize_rationale=lambda rationale: None)
```

It applies wherever the property is lowered — the default
`[MCP] Tool Call Response` event and every tool-scope custom event of the same
invocation — so no emit path bypasses it. A sanitizer that raises, or returns
anything other than a string, omits the property rather than falling back to the
raw text. Left unset, the rationale is emitted exactly as supplied (the v0
default: configuring nothing changes nothing on the wire).

The rationale is host-supplied and never travels back to the client, so this
affects telemetry only.

## Custom events

`track_server_event(ctx, name, properties=None, options=None)` and
`track_tool_event(ctx, name, properties=None, options=None)` emit your own
events with the same treatment as the defaults:

- They inherit the full [shared property set](#shared-properties) (plus, for
  `track_tool_event`, the tool-scope reserved properties) and the `extra` bags.
- The [skip rule](#emission-guarantees) and best-effort guarantee apply.
- Event names are yours verbatim — the SDK does not prefix them. Avoid the
  `[MCP] ` prefix, which is reserved for SDK-emitted names and properties.
- Pass `TrackEventOptions(drop_extra_props=True)` to omit the `extra` bags from
  one event.

### Property precedence

Properties merge in a fixed order — **later sources overwrite earlier ones**:

```
reserved (SDK-derived)  <  extra (context bag)  <  properties (per call)
```

- A per-call `properties` value wins over everything, including a same-named
  reserved property.
- An `extra` value wins over a reserved property but loses to `properties`.
- On the **default events**, the SDK's own outcome values (`[MCP] Is Error`,
  `[MCP] Response Duration`, sizes, error fields, `[MCP] Session Duration`,
  `[MCP] Tool Count`, …) are passed as per-call properties, so a colliding
  `extra` key cannot overwrite them.

All reserved names carry the `[MCP] ` prefix — keep it out of your own keys
and collisions never arise.

Values are sent exactly as provided; the SDK does not escape, truncate, or
redact `extra` or `properties` values. Apply output encoding where the data is
rendered, and keep sensitive values out.

## Measurement conventions

- **Durations** (`[MCP] Response Duration`, `[MCP] Session Duration`) are
  wall-clock milliseconds, rounded to the nearest integer.
- **Sizes** (`[MCP] Request Size`, `[MCP] Response Size`) are the UTF-8 byte
  length of the value's compact JSON serialization (`json.dumps` with compact
  separators; pydantic values — a `CallToolResult` a low-level handler
  returned, a validated model argument, or either nested inside a payload —
  via their own JSON dump) — the payload semantics, not the bytes on the wire.
  Omitted when the value isn't JSON-serializable.
  Sizes are approximations and may differ slightly across SDKs (the Node SDK
  measures `JSON.stringify` output) — treat them as comparable magnitudes, not
  exact wire measurements.

## Property index

Every property the SDK can emit, and where it appears. *All* = the five
default events plus custom events emitted through `track_server_event` /
`track_tool_event`; *tool-scope* = `[MCP] Tool Call Response` and
`track_tool_event` events.

| Property | Type | Appears on |
| -- | -- | -- |
| `[MCP] Anchor Type` | string | All |
| `[MCP] Attempted Tool Name` | string | `Tool Call Rejected` |
| `[MCP] Auth Type` | string | All (when configured) |
| `[MCP] Client Name` | string | All |
| `[MCP] Client Version` | string | All (when known) |
| `[MCP] Error Code` | string | `Tools Listed`, `Tool Call Response` (failures), `Tool Call Rejected` — only when a specific code is known |
| `[MCP] Error HTTP Status` | number | `Tool Call Response` (when the failure carried an HTTP status — the tool's, not the transport's) |
| `[MCP] Error Message` | string | `Tools Listed`, `Tool Call Response` (failures), `Tool Call Rejected` |
| `[MCP] Error Type` | string | `Tools Listed`, `Tool Call Response` (failures), `Tool Call Rejected` |
| `[MCP] Is Error` | boolean | `Tools Listed`, `Tool Call Response`, `Tool Call Rejected` |
| `[MCP] Protocol Version` | string | All (when known) |
| `[MCP] Rationale` | string | Tool-scope (opt-in, via `set_rationale`; redactable via [`sanitize_rationale`](#redacting-mcp-rationale)) |
| `[MCP] Rejection Reason` | string | `Tool Call Rejected` |
| `[MCP] Request Size` | number | `Tool Call Response` |
| `[MCP] Response Duration` | number | `Tools Listed`, `Tool Call Response`, `Tool Call Rejected` |
| `[MCP] Response HTTP Status` | number | `Tool Call Rejected` (HTTP transports); tool-scope when host-supplied via `ctx.request.response_http_status` |
| `[MCP] Response Size` | number | `Tools Listed`, `Tool Call Response`, `Tool Call Rejected` |
| `[MCP] Server Name` | string | All |
| `[MCP] Server Type` | string | All (when set on the context) |
| `[MCP] Server Version` | string | All |
| `[MCP] Session Duration` | number | `Session Ended` |
| `[MCP] Session ID` | string | All |
| `[MCP] Tool Category` | string | Tool-scope (when set) |
| `[MCP] Tool Count` | number | `Tools Listed` |
| `[MCP] Tool Name` | string | Tool-scope |
| `[MCP] Tool Names` | string[] | `Tools Listed` |
| `[MCP] Tool Names Truncated` | boolean | `Tools Listed` (only when truncated) |
| `[MCP] Tool Owner` | string | Tool-scope (when set) |
| `[MCP] Tool Tags` | string[] | Tool-scope (when set) |
| `[MCP] Transport` | string | All |
| `[MCP] User Agent` | string | All |
