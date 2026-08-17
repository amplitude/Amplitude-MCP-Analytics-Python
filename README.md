# amplitude-mcp-analytics

Amplitude MCP Analytics SDK — Model Context Protocol server usage tracking for Amplitude Analytics.

> **Status:** Preview. Server and tool instrumentation, the default event set,
> identity resolution, and custom events are available now. Transport and
> correlation handling (stdio, Streamable HTTP — stateful and stateless — and
> SSE) is handled for you under the hood.

## Install

```bash
uv add amplitude-mcp-analytics 'mcp>=1.16,<2'
```

The SDK itself has no hard runtime dependencies — your MCP server already
depends on `mcp`. The supported MCP SDK range is **`mcp>=1.16,<2`**. The `mcp`
2.x line restructures the server internals; a 2.x server is detected and
rejected with a clear error rather than silently emitting nothing (2.x support
is planned).

To construct the client from an API key, the SDK needs the
`amplitude-analytics` package — install it via the optional extra:

```bash
uv add 'amplitude-mcp-analytics[amplitude]'
```

If you already own an Amplitude client, skip the extra and pass the client in
(see below).

> **Agent-assisted setup:** the
> [`instrument-mcp-server` skill](https://github.com/amplitude/builder-skills/tree/main/engineering-skills/skills/instrument-mcp-server)
> (part of the [builder-skills](https://github.com/amplitude/builder-skills)
> `engineering-skills` plugin) exists for agent-assisted setup, but it was
> written for the Node SDK. A coding agent can still follow this README
> manually to instrument a Python server.

## Quick start

```python
import os

from mcp.server.fastmcp import FastMCP

from amplitude_mcp_analytics import AmplitudeMCPAnalytics

mcp = FastMCP("my-mcp-server")

analytics = AmplitudeMCPAnalytics(
    api_key=os.environ["AMPLITUDE_API_KEY"],
    server_name="my-mcp-server",
    server_version="1.0.0",
)

# Bind the server (enables analytics + emits connection events), then wrap your
# tool handlers. Order matters: instrument_server() must run before mcp.run().
analytics.instrument_server(mcp, auth_type="oauth")

@mcp.tool()  # @mcp.tool() stays outermost — see below
@analytics.instrument_tool(name="search", owner="docs-team")
async def search(query: str) -> str:
    return await do_search(query)  # your handler, unchanged

mcp.run()
```

To reuse an Amplitude client you already own, pass it instead of `api_key`:

```python
from amplitude import Amplitude

AmplitudeMCPAnalytics(amplitude=Amplitude("KEY"), server_name="...", server_version="...")
```

## Instrumenting your server

Two steps, both wrap things you already have — no handler signatures change.

**`instrument_server(server, ...)`** binds the SDK to your MCP server — a
`FastMCP` or a low-level `mcp.server.lowlevel.Server`. The Python MCP SDK has
no `connect(transport)`; each `run()` call is one connection, so the SDK wraps
`run()` instead. It auto-detects the transport from the message stream
(`stdio` / `streamable-http` / `sse`; override with `transport=`), captures the
client/server handshake, and emits the default connection events. Call it
**before** the server runs. It's idempotent and returns the same server.

**`instrument_tool(...)`** wraps a tool handler. The returned function has the
exact same signature and sync/async nature as the one you pass in
(`functools.wraps` preserves the original signature), so FastMCP still builds
the tool's input schema from your parameters. On each call it emits
`[MCP] Tool Call Response` with timing, error, and size details.

```python
analytics.instrument_server(mcp)

@mcp.tool()
@analytics.instrument_tool(
    name="search", owner="docs-team", extra={"feature flag": "new-ranker"}
)
async def search(query: str) -> str:
    return await do_search(query)
```

> **Decorator order is load-bearing.** `@mcp.tool()` must be **outermost**,
> with `@analytics.instrument_tool(...)` directly under it. FastMCP builds the
> tool's input schema from the signature of the function it decorates —
> `functools.wraps` preserves that signature through the analytics wrapper, but
> only if the wrapper sits *inside* `@mcp.tool()`. Swapping the order would
> register the raw handler and instrument nothing.

Not using decorators? The same method wraps a handler directly:
`mcp.add_tool(analytics.instrument_tool(search, McpToolMeta(name="search")))`.

> **`instrument_tool` requires `instrument_server`.** If the server was never
> bound, the wrapper is a **no-op passthrough**: your handler runs untouched,
> nothing is emitted, and a one-time warning is logged. Instrumenting a tool can
> never change its behavior.

## Default events

Once a server is bound and its tools wrapped, the SDK emits these automatically:

| Event | When | Notable properties |
| -- | -- | -- |
| `[MCP] Session Initialized` | Connection handshake (stdio, stateful Streamable HTTP, SSE) | client/server identity, `[MCP] Transport`, `[MCP] Auth Type` |
| `[MCP] Session Ended` | Transport close (same transports) | `[MCP] Session Duration` |
| `[MCP] Tools Listed` | A `tools/list` request | `[MCP] Tool Count`, `[MCP] Tool Names` (capped), `[MCP] Response Duration`, `[MCP] Response Size` |
| `[MCP] Tool Call Response` | Every instrumented tool call | `[MCP] Is Error`, `[MCP] Error Message`/`[MCP] Error Code`/`[MCP] Error Type`/`[MCP] Error HTTP Status`, `[MCP] Response Duration`, `[MCP] Request Size`, `[MCP] Response Size`, `[MCP] Rationale` (opt-in, see below) |
| `[MCP] Tool Call Rejected` | A `tools/call` request that fails before any tool callback runs (unknown tool, input-schema validation) | `[MCP] Attempted Tool Name` (unvalidated input — kept off `[MCP] Tool Name`), `[MCP] Rejection Reason` (`unknown_tool`/`disabled_tool`/`schema_validation`/`unrecognized`), `[MCP] Error Message`, `[MCP] Response Duration`, `[MCP] Response Size`, `[MCP] Response HTTP Status` |

All event names and properties are prefixed `[MCP] ` so they never collide with
same-named events/properties from other Amplitude SDKs on the same project.

This table is a summary. The full reference — every property and when it's
present, identity resolution, transport nuances, and the error taxonomy —
lives in [`docs/events.md`](./docs/events.md).

Session events model a real protocol session, which only exists where an
`initialize` handshake happens: stdio, stateful Streamable HTTP, and SSE. On
stateless Streamable HTTP the session manager spins up one stateless run per
request — there is no handshake, so `[MCP] Session Initialized` /
`[MCP] Session Ended` are **not** emitted rather than fabricated. Every event
also carries the shared context properties (identity, client/server, transport,
trace correlation).

## Identity

`user_id` must match whatever you already send to Amplitude for the same user.
The SDK never guesses it from auth — you provide it, via whichever path fits:

```python
from amplitude_mcp_analytics import McpTenant, SetIdentityInput

# 1. Bound with the server. Scoped to that binding — hosts that build one
#    server per request can pass per-request values safely; concurrent
#    bindings never overwrite each other.
analytics.instrument_server(
    mcp,
    user_id="user-123",
    tenant=McpTenant(group_type="org id", group_value="456"),
)

# 2. Per request, inside a handler (wins over everything else).
@mcp.tool()
@analytics.instrument_tool(name="search")
async def search(query: str) -> str:
    analytics.set_identity(SetIdentityInput(user_id=my_auth.get_login_id()))
    return await do_work(query)

# 3. Opt-in, derived from the request's auth info (you map the claims).
@mcp.tool()
@analytics.instrument_tool(
    name="lookup",
    resolve_identity=lambda auth_info: SetIdentityInput(
        user_id=(auth_info or {}).get("client_id"),
    ),
)
async def lookup(doc_id: str) -> str: ...
```

The `resolve_identity` callback receives the request's auth info as a plain
dict — the access token the MCP SDK's auth middleware validated (`client_id`,
`scopes`, plus whatever claims your token model carries). One transport nuance
the SDK absorbs for you: under **stateful** Streamable HTTP the session runs in
a different task than the HTTP request, so the SDK reads the token from the
request's ASGI scope — the SDK's `get_access_token()` contextvar alone is not
visible there.

Resolution order (first match wins): `set_identity()` → `resolve_identity()` →
`instrument_server` options → correlation anchor → an anonymous floor. When no
explicit identity is supplied but a correlation anchor exists (a stdio process,
a transport session id, or a propagated W3C trace context), the SDK emits
accurate aggregate-only data under a synthetic `device_id` derived from that
anchor — never a polluting placeholder, never a fabricated user.

If there is no anchor either — the fully stateless case with no identity and no
tenant — each request would mint a brand-new random `device_id` with no
cross-call stitching, so those events are **dropped by default** rather than
inflating your user counts (see [`docs/events.md`](./docs/events.md)). Opt in to
emit them as anonymous, aggregate-only data with `emit_anonymous_event=True`:

```python
from amplitude_mcp_analytics import MCPAnalyticsConfig

MCPAnalyticsConfig(emit_anonymous_event=True)
```

## Rationale

Agent clients often supply a free-text rationale for a tool call ("why I'm
calling this tool"). If your server receives one — as a tool argument, in
`_meta`, a header, or however your convention works — pass it to the SDK and
it is emitted as the reserved `[MCP] Rationale` property on the tool-call
event and on every tool-scope custom event of the same invocation:

```python
@mcp.tool()
@analytics.instrument_tool(name="search")
async def search(query: str, rationale: str | None = None) -> str:
    if isinstance(rationale, str):
        analytics.set_rationale(rationale)
    return await do_work(query)
```

The SDK never reads rationale out of tool inputs itself: it is content-bearing
free text, so emitting it is an explicit opt-in, and where it lives is your
convention. Callable at any depth inside an instrumented handler (like
`set_identity`); truncated to 1000 characters; last write wins. Omitted
entirely when never set.

## Error HTTP status

When a tool call fails on a raised error that carries an HTTP status
(`err.status`, `err.status_code`, or `err.response.status_code` — the common
Python conventions, covering aiohttp, httpx, and requests), the tool-call
event includes `[MCP] Error HTTP Status`. This is the status of the failure the
tool hit (an upstream API response, an HTTP-shaped error), NOT the MCP
transport status — per the MCP spec, tool failures are returned in-band, so the
transport typically answers 200 even when this property is a 4xx/5xx.

For error shapes the SDK can't sniff, set it explicitly when building the
error: `analytics.tool_error(ctx, code=..., message=..., http_status=502)`.

Related but distinct: `[MCP] Response HTTP Status` is the transport-level
status of the HTTP response itself. The instrumented-tool wrapper never emits
it (dispatched tool calls answer 200; the wrapper emits before the response is
written). The default `[MCP] Tool Call Rejected` event carries it on the HTTP
transports (protocol-level rejections answer 200 with the error in the
JSON-RPC body). For events you emit yourself, set
`ctx.request.response_http_status` before calling `track_tool_event`.

## Choosing what's captured

All default events are on by default. Toggle them with `autocapture` — a boolean
for everything, or a mapping to control families independently:

```python
import os

from amplitude_mcp_analytics import AmplitudeMCPAnalytics, MCPAnalyticsConfig

AmplitudeMCPAnalytics(
    api_key=os.environ["AMPLITUDE_API_KEY"],
    server_name="my-mcp-server",
    server_version="1.0.0",
    config=MCPAnalyticsConfig(
        # keep tool-call events, drop connection events
        autocapture={"server_events": False},
    ),
)
```

`autocapture=False` disables all default events; `{"server_events": ...,
"tool_calls": ...}` toggles each family. `tool_calls` covers both
`[MCP] Tool Call Response` and `[MCP] Tool Call Rejected`. `server_events` can
be split further with `session_lifecycle` (`[MCP] Session Initialized`/`Ended`)
and `tools_listed` (`[MCP] Tools Listed`) — e.g. servers built per HTTP request
typically want `{"session_lifecycle": False, "tools_listed": True}`, since
their transports close at the end of every request rather than at session end.
Custom events (below) are unaffected.

### Redacting error messages

`[MCP] Error Message` carries free text the SDK didn't compose — a failing tool's
own message, or the MCP SDK's input-validation text, which quotes the rejected
argument value. Either may contain end-user data. `sanitize_error_message` rewrites
or drops it before emission, on every event that carries it:

```python
import os
import re

from amplitude_mcp_analytics import AmplitudeMCPAnalytics, MCPAnalyticsConfig

AmplitudeMCPAnalytics(
    api_key=os.environ["AMPLITUDE_API_KEY"],
    server_name="my-mcp-server",
    server_version="1.0.0",
    config=MCPAnalyticsConfig(
        sanitize_error_message=lambda message: re.sub(
            r"[\w.+-]+@[\w-]+\.[\w.]+", "<email>", message
        ),
    ),
)
```

Return `None` to omit the property entirely. A sanitizer that raises fails
**closed** — the property is omitted, never the raw message. `[MCP] Error Code`
and `[MCP] Error Type` are unaffected, so failures stay segmentable. The text
sent to the client never changes. See
[Redacting `[MCP] Error Message`](docs/events.md#redacting-mcp-error-message).

## Context (`ctx`)

Every tracked event carries a per-invocation context object. You can construct
one and pass it explicitly to the tracking APIs, or expose it via
`run_with_context` so deeper call stacks can read it through
`get_current_context()`.

```python
from amplitude_mcp_analytics import (
    McpServerInfo,
    McpToolMeta,
    create_server_context,
    create_tool_context,
    run_with_context,
)

server_ctx = create_server_context(
    server=McpServerInfo(name="my-mcp-server", version="1.0.0"),
    transport="stdio",
)

tool_ctx = create_tool_context(server_ctx, McpToolMeta(name="search_docs"))

def emit() -> None:
    ...  # get_current_context() is available here if needed

run_with_context(tool_ctx, emit)
```

Types and helpers live in the `amplitude_mcp_analytics.context` subpackage and
are also re-exported from the main package (`amplitude_mcp_analytics`).

You usually don't build `ctx` by hand — `instrument_server` / `instrument_tool`
construct and inject it for you (inside an instrumented handler,
`get_current_context()` returns the current invocation's context). Reach for
these factories when emitting events outside an instrumented handler.

## Custom event properties

Every event carries a set of **reserved** properties the SDK derives from the
context — identity, session/trace correlation, client/server identity, and (for
tool events) the tool metadata. You can attach your own properties on top of
these from two places:

- **`extra`** — an enrichment bag carried on the context. Put domain values at
  the server scope (`extra=` in `instrument_server`) or on a tool (`extra=` in
  `instrument_tool`) and they ride along on every event derived from that
  scope — including the default events.
- **`properties`** — the per-call argument to `track_server_event` /
  `track_tool_event`, for values specific to that one event.

### Precedence

When the same key appears in more than one place, the merge order is fixed —
later sources overwrite earlier ones:

```
reserved (SDK-derived)  <  extra (context bag)  <  properties (per call)
```

- A **`properties`** value wins over anything with the same key — including a
  reserved property (the explicit, per-call value is the most intentional one).
- An **`extra`** value overrides a reserved property but loses to `properties`.
- On the **default events**, the SDK's outcome values (`[MCP] Is Error`,
  `[MCP] Response Duration`, …) ride as per-call `properties`, so a colliding
  `extra` key can't overwrite them.

Reserved names all carry the `[MCP] ` prefix — avoid it in your own keys and
collisions never arise.

### Dropping the `extra` bag

`extra` properties are included by default. To omit them for a single event,
pass `TrackEventOptions(drop_extra_props=True)`:

```python
from amplitude_mcp_analytics import TrackEventOptions

analytics.track_tool_event(
    ctx, "my event", {"foo": "bar"}, TrackEventOptions(drop_extra_props=True)
)
```

Values are sent as provided — the SDK does not escape or redact them. Apply any
output encoding where the data is rendered.

## Architecture decisions

### Separate repo from the agent-analytics SDKs

MCP server analytics is a distinct product from agent analytics. Different
audience (MCP server operators vs. agent developers), different domain model
(server / session / tool invocation vs. agent / turn / message), and a
different release cadence. Keeping the repos separate lets each evolve on
its own timeline without coupling unrelated breaking changes.

### The sibling the `-node` suffix left room for

The Node SDK shipped first as Amplitude-MCP-Analytics-Node, its suffix
deliberately leaving room for a Python SDK without forcing a future rename.
This repo is that sibling: same product, same wire contract, one repo per
runtime.

### Mimic the Node SDK for DX, not for the domain model

Repo layout, constructor shape, mock test client, and docs mirror
Amplitude-MCP-Analytics-Node (which in turn mirrors `@amplitude/ai`), so
contributors moving between the repos see familiar patterns — with
Python-native build tooling (uv, pytest, pyright, ruff) in place of the Node
stack. The domain model — events, properties, identity, context — is
MCP-native and intentionally does not reuse agent vocabulary.

### Zero hard dependencies

The Node SDK keeps the MCP SDK and the Amplitude client as peer dependencies;
Python has no peer-dependency concept, so this package ships with **zero hard
runtime dependencies** instead. Coupling to the `mcp` package is structural
(duck-typed, confined to one adapter module); `amplitude-analytics` is lazily
imported only on the `api_key` path and installable via the `[amplitude]`
extra. The low-level delivery utilities (delivery hooks, serverless flush
accounting) are ported rather than depended on — no shared package, no version
coupling at runtime.

### Ported from the Node SDK

This SDK is a port of `@amplitude/mcp-analytics`
(Amplitude-MCP-Analytics-Node). The wire contract — event names, `[MCP] `
property names, precedence rules, identity math — is shared, so dashboards can
slice both SDKs without special-casing; the integration seams are
Python-native. The intentional divergences, and the policy for pulling
upstream changes over, are recorded in [`PORTING.md`](./PORTING.md).

## Development

```bash
uv sync
uv run pytest
uv run pyright src
uv run ruff check src tests
```
