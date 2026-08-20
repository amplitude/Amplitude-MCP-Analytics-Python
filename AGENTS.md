# AGENTS.md

Guidance for AI coding agents (and humans) working in this repo. Read this
before making changes. It captures the tenets and conventions the team aligns
on so that parallel sessions produce consistent, shippable work.

This file is **internal** to the development team. It is not customer-facing —
that is what `README.md` is for.

## What this project is

`amplitude-mcp-analytics` is a **public, customer-facing SDK** for tracking
Model Context Protocol (MCP) server usage in Amplitude — the Python port of
`@amplitude/mcp-analytics` (Amplitude-MCP-Analytics-Node). It mirrors that
repo's developer experience (repo layout, constructor shape, mock test client)
with Python-native tooling, but the **domain model is MCP-native** — servers,
sessions, tool invocations — and intentionally does not reuse agent vocabulary.

## Tenets

These are the principles we use to break ties when a specific rule doesn't cover
a situation.

1. **The public API is a promise.** Anything exported becomes a semver contract.
   Keep the public surface minimal; prefer adding API later over removing it.
2. **Optimize for the consumer's ease of use.** The common case must work with
   minimal config and sensible zero-config defaults. Wrap, don't rewrite: an
   integration should not force consumers to change their existing handler
   signatures, implement our interfaces, or hand-build values the SDK can
   derive itself. Advanced hooks (custom resolvers, overrides) are opt-in
   escape hatches for the long tail — never the required path for the basics.
3. **Customer-facing artifacts assume zero internal knowledge.** `README.md`,
   error messages, and published types must make sense to someone outside
   Amplitude. No internal ticket numbers, no Slack/Linear links, no team jargon.
4. **Nothing internal or sensitive gets committed — ever.** The repo and its
   full git history are public. No secrets, API keys, tokens, internal URLs, or
   customer data. A secret committed once lives in history forever.
5. **Backwards compatibility is the default.** Don't break consumers or force
   dependency conflicts. Keep dependency constraints wide (the `mcp>=1.16,<2`
   range is deliberate); don't re-export a dependency's types into our public
   API unless we intend to track them.
6. **Porting is a fork point, not a sync target.** Ported code belongs to
   this repo once copied; there is no obligation to track upstream.
7. **Degrade honestly.** When we can't derive something accurately (e.g. a user
   or session on stateless transport), emit accurate aggregate-only data rather
   than fabricating it.
8. **Small, atomic PRs over large ones.** One concern per PR; scoped so a
   reviewer can hold it in their head.
9. **When scope is ambiguous, stop and ask** rather than guessing.

## Public API surface

The published surface is **curated, not automatic.**

- The public surface is the explicit `__all__` list in
  `src/amplitude_mcp_analytics/__init__.py`. Add a name there only when you
  intend to support importing it forever.
- The smoke test (`tests/test_smoke.py`) is the tripwire for forgotten public
  exports — it asserts every expected name exists and is in `__all__`. Keep it
  green, and update its expected list in the **same PR** as any export change.
- Internal modules (`src/amplitude_mcp_analytics/core/**`,
  `src/amplitude_mcp_analytics/utils/**`) exist because public modules import
  them, but they are **never** exported from the package root. Do not re-export
  them.
- Mark internal/test-only symbols with `@internal` in their docstring (the
  Python stand-in for the Node repo's `@internal` + `stripInternal`; reviewers
  and agents enforce it by convention). Test-only helpers should also be
  prefixed `_`.
- Before changing exports, ask: "Am I willing to support this import for the life
  of the package?" If not, keep it internal.

## Customer-facing vs internal references

- **No ticket numbers** in `README.md`, shipped source comments, or error messages.
  Internal references shouldn't be in anything that ships or that a customer reads.
- Keep maintainer/process docs (porting policy, architecture rationale) out of
  the customer README; put them here or in `PORTING.md`.
- **Keep `README.md` in sync with the API.** When you introduce or change public
  functionality (a new export, a changed signature, a new config option, a new
  install/usage step), update `README.md` in the **same PR**. Customer-facing
  docs must never lag the public surface.

## Porting policy

This repo **ports** from Amplitude-MCP-Analytics-Node (rather than vendoring
from Amplitude-AI-Node, as the Node repo does). See `PORTING.md` for the
upstream commit, the shared wire contract, and the full table of intentional
divergences. Rules:

- The **wire contract is shared** with the Node SDK — event names, `[MCP] `
  property names, property precedence, the anonymous-drop rule, the error
  taxonomy, and the anchor-derived identity math. Never change it unilaterally;
  a wire change is a cross-SDK decision.
- Adapt freely for Python needs; record intentional divergences in the
  `PORTING.md` table in the same PR.
- When a Node change is worth pulling over, do it as a normal PR with subject
  `chore: port <thing> from Amplitude-MCP-Analytics-Node @ <sha>`.
- Files that carry a provenance header (e.g. `core/delivery.py`) name their
  upstream source; keep the header accurate when you refresh them.

## Conventions

- **Package manager: `uv`, exclusively.** `uv sync` to install, `uv add` to
  change dependencies, `uv run` to execute tools. The committed `uv.lock` is
  the source of truth. Never `pip install` into the project or introduce
  poetry.
- **PRs:** link the ticket, reference related PRs, and explicitly call out what is
  intentionally out of scope or stubbed.
- **PR titles set the version.** We squash-merge, so the title becomes the
  commit message release-please parses: `!` or a `BREAKING CHANGE:` footer →
  major, `feat:` → minor, `fix:`/`perf:`/`docs:`/`revert:` → patch, and
  `chore:`/`test:`/`refactor:`/`build:`/`ci:` → no release. Choose the type by
  user-visible impact, and write the title as the changelog line it will
  become. `semantic-pr.yml` enforces the shape, not the accuracy.
- **Never hand-edit the version.** `[project] version` in `pyproject.toml`,
  `CHANGELOG.md` entries, and `.release-please-manifest.json` are owned by
  release-please (`.github/workflows/release-please.yml`); merging its Release
  PR is what tags the release and publishes to PyPI over OIDC Trusted
  Publishing. No PyPI token exists in this repo — don't add one. Release
  mechanics and the infra prerequisites are in `CONTRIBUTING.md` and that
  workflow's header comment.

## Verify before you finish

All of these must pass:

```bash
uv sync
uv run pyright src
uv run pytest
uv run ruff check src tests
```

Add tests next to the behavior you change. The smoke test in `tests/` is the
tripwire for forgotten public exports — keep it green.
