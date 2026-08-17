# Contributing

Thanks for looking at `amplitude-mcp-analytics`. Before making changes, read
[`AGENTS.md`](./AGENTS.md) — it captures the tenets this repo optimizes for
(minimal public API surface, backwards compatibility, honest degradation,
small atomic PRs) and applies to human and AI contributors alike. If you're
touching anything shared with the Node SDK's wire contract (event names,
`[MCP] `-prefixed properties, identity math), read
[`PORTING.md`](./PORTING.md) first — it documents the upstream relationship
to `Amplitude-MCP-Analytics-Node` and the intentional divergences table.

## Development setup

This repo uses **`uv`, exclusively** — no `pip install` into the project, no
poetry. The committed `uv.lock` is the source of truth.

```bash
uv sync                              # install dependencies
uv run pytest                        # run the test suite
uv run pyright src                   # type check
uv run ruff check src tests          # lint
```

All four must pass before you open a PR. If you change a public export, keep
`tests/test_smoke.py` green and update `README.md` in the same PR — customer
docs must never lag the public API.

## Pull requests

- Keep PRs small and atomic — one concern per PR, scoped so a reviewer can
  hold it in their head.
- Link the ticket, reference related PRs, and call out anything intentionally
  out of scope or stubbed.
- When scope is ambiguous, ask rather than guessing.

### PR titles are Conventional Commits (required)

PR titles **must** follow [Conventional Commits](https://www.conventionalcommits.org/en/v1.0.0/):

```
<type>(<optional scope>)!: <description>
```

Allowed types:

- `feat` — new feature (minimum minor release)
- `fix` — bug fix (minimum patch release)
- `chore` — other changes that don't modify src or test files
- `docs` — documentation updates
- `test` — test updates
- `refactor` — code change that neither fixes a bug nor adds a feature
- `perf` — performance improvement
- `ci` — changes to CI configuration files and scripts
- `build` — changes to the build system or dependencies
- `revert` — revert a commit

A `!` before the colon (e.g. `feat!:`) marks a breaking change.

This isn't just a style preference — it's **load-bearing automation**. The
`semantic-pr.yml` workflow enforces this pattern on every PR, and the release
flow below keys off the exact title `chore(release): v<version>` to decide
whether a merged PR should trigger a publish. A malformed title blocks CI; a
title that doesn't match the release pattern means a release PR silently
won't publish.

## Release flow

Releases are driven by two workflows, not manual tags:

1. A maintainer runs **`release.yml`** via `workflow_dispatch`, choosing a
   version bump (`patch` / `minor` / `major`). It bumps the version, opens a
   branch, and creates a PR titled `chore(release): v<version>`.
2. Once that PR is reviewed and merged into `main`, **`publish.yml`** fires
   automatically (matching on the `chore(release): v` title and a marker in
   the PR body), re-runs the full check suite, tags the commit, creates the
   GitHub release, and publishes to PyPI.

Don't hand-edit the version or push tags directly — go through `release.yml`
so the title convention and publish gate stay intact.
