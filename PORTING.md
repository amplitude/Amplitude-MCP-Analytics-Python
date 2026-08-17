# Porting notes

This SDK is a port of
[`@amplitude/mcp-analytics`](https://github.com/amplitude/Amplitude-MCP-Analytics-Node)
(Amplitude-MCP-Analytics-Node) to Python, taken at upstream commit `c1cbb40`
(v0.4.1). The **wire contract is shared**: event names, `[MCP] `-prefixed
property names, property precedence, the anonymous-drop rule, the error-type
taxonomy, and the anchor-derived identity math (`uuid5` over the
`6ba7b812-9dad-11d1-80b4-00c04fd430c8` namespace) are identical, so the same
caller produces the same `device_id` from either SDK and dashboards can slice
both without special-casing.

## This is a fork point, not a sync target

Like the Node repo's own vendoring policy: provenance here is for attribution
and archaeology, not an obligation to track upstream. When a Node change *is*
worth pulling over, do it as a normal PR with subject
`chore: port <thing> from Amplitude-MCP-Analytics-Node @ <sha>`.

## Intentional divergences

| Area | Node | Python | Why |
|---|---|---|---|
| Integration seam | wraps `server.connect(transport)`; patches the private `Server._requestHandlers` Map; chains `oninitialized`/`onclose` | wraps `Server.run(...)` (one run = one connection); replaces entries in the **public** `request_handlers` dict; a read-stream proxy observes `initialize`/`notifications/initialized`; run teardown = session end | the Python SDK has no `connect(transport)`; transports exist only as message streams |
| Per-binding scope | wraps every request handler in an AsyncLocalStorage frame | one `ContextVar` set around the delegated `run` — anyio task groups copy context at `start_soon`, so handler tasks inherit it | no per-handler machinery needed; also fixes stateless-HTTP concurrency (one shared `Server`, many concurrent runs) |
| Dispatch de-dup | `WeakSet` keyed on the per-request `extra` object | mutable `DispatchMarker` in a `ContextVar`; `instrument_tool` mutates the marker retrieved from the copied context | `RequestContext` is an eq-comparing dataclass (unhashable); mutation survives `anyio.to_thread` context copies |
| Ambient context | `AsyncLocalStorage` | `contextvars.ContextVar` | direct equivalent |
| Transport detection | structural probe on the transport object (`handleRequest`) | evidence from message metadata (HTTP request → streamable-http; `session_id` query param → sse; none → stdio); `transport=` override on `instrument_server` | no transport object crosses the seam |
| `[MCP] Transport` values | `stdio` \| `streamable-http` | adds `sse` (deprecated HTTP+SSE transport) | truthful reporting; documented as a Python-only value |
| Error classification inputs | `AbortError`, Node syscall codes, `err.status`/`err.statusCode` | `TimeoutError`/`CancelledError`; `ConnectionError` family + `gaierror` (emitting `econnrefused`-style codes for cross-SDK parity); `err.status`/`err.status_code`/`err.response.status_code` | platform idioms; the emitted taxonomy is identical |
| `TrackingProxy` | exists (frozen ES module namespaces can't be patched) | deleted — a plain `DeliveryClient` wrapper composes the same hooks in the same order | Python objects are mutable |
| `utils/resolve-module` (bundler detection) | exists | deleted — a three-line lazy `importlib` import | no bundler-rewrite problem in Python |
| Exit warning | `process.on('beforeExit')` | `atexit` | closest equivalent; fires once at interpreter shutdown, not on `os._exit`/SIGKILL |
| Delivery-failure warning (HTTP ≥ 400) | composes the client-level `configuration.callback` | per-event `BaseEvent(callback=...)` | avoids mutating a caller-owned client's config |
| `disabled_tool` rejection reason | reachable (`tool.disable()`) | unreachable — the official Python SDK has no tool disable; kept in the taxonomy for cross-SDK schema parity | documented in `docs/events.md` |
| Byte sizes | `Buffer.byteLength(JSON.stringify(v))` | UTF-8 length of compact `json.dumps` / `model_dump_json` | approximations across SDKs; documented |
| Peer dependencies | `@amplitude/analytics-node`, `@modelcontextprotocol/sdk` as peers | zero hard deps; `amplitude-analytics` behind the `[amplitude]` extra (lazy import on the `api_key` path); `mcp` duck-typed, dev/test dep only | Python has no peer-dependency concept |
| License file | none in the repo | none (mirrored) | revisit before publishing to PyPI |

## MCP SDK support

Supported: `mcp>=1.16,<2` (seams verified against 1.16.0 and 1.29.0; CI runs a
compat matrix of the floor and the latest 1.x). The 2.x line restructures the
server (removes FastMCP, privatizes the handler registry, introduces a
middleware seam that is still marked provisional upstream) —
`instrument_server` detects it and raises a `ConfigurationError` naming the
supported range rather than silently emitting nothing. A 2.x adapter is
planned as a follow-up inside `core/mcp.py`, the single module allowed to
touch the `mcp` package.
