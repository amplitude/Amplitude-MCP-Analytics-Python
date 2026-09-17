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

This isn't just a style preference — it's **load-bearing automation**, and
since the switch to release-please it now decides version numbers.

We squash-merge, so **a PR's title becomes the commit message on `main`**, and
that commit message is the only thing release-please parses to compute the next
version. `semantic-pr.yml` is therefore the guard on versioning, not a style
linter:

- `feat!:` / `fix!:` / a `BREAKING CHANGE:` footer → major bump, with a
  breaking-changes section
- `feat:` → minor bump, listed under **Features** in `CHANGELOG.md`
- `fix:` → patch bump, under **Bug Fixes**
- `perf:`, `docs:`, `revert:` → patch bump, under **Performance Improvements**
  / **Documentation** / **Reverts**. The `python` release type shows `docs:` in
  the changelog, so a docs-only merge does cut a patch release
- `chore:`, `test:`, `refactor:`, `build:`, `ci:` → hidden from the changelog;
  a batch containing only these opens no Release PR at all

A mistyped type isn't a cosmetic problem: `chore: add retry support` ships no
release at all, and `feat: fix a typo` burns a minor version. Pick the type
that matches the user-visible impact, and write the title for the changelog —
it is the line customers will read.

## Release flow

Normal releases are automated end to end by
[release-please](https://github.com/googleapis/release-please); there is no
manual bump or tag. Manual workflow dispatch exists only to recover an
already-created release whose PyPI upload did not complete.

1. **You merge a normal PR to `main`.** Nothing else to do.
2. **`release-please.yml` opens (or updates) a Release PR** titled
   `chore(main): release <version>`. It bumps `[project] version` in
   `pyproject.toml`, prepends a `CHANGELOG.md` section built from the
   conventional commits since the last release, and updates
   `.release-please-manifest.json`. Further merges to `main` keep amending the
   same PR, so it always describes the next release.
3. **Merging the Release PR publishes.** release-please tags `v<version>` and
   creates the GitHub release; the `publish` job then re-runs `pyright` and the
   test suite, `uv build`s the wheel and sdist, and uploads them to PyPI.

Practical notes:

- **Don't hand-edit `[project] version`, `CHANGELOG.md` entries, or
  `.release-please-manifest.json`.** release-please owns all three; a manual
  edit either gets overwritten or makes the next bump compute from the wrong
  base. Review the Release PR instead — editing the version there is the
  supported way to override a bump.
- **Release PRs run full CI.** The workflow mints a GitHub App token for
  release-please precisely so its PRs trigger `test.yml` and `semantic-pr.yml`
  — GitHub does not start workflows for events authored by the default
  `GITHUB_TOKEN`.
- **PyPI upload uses OIDC Trusted Publishing** (`uv publish
  --trusted-publishing always`) from the `pypi-release` environment. There is
  no PyPI token in the repo; don't add one. The header comment in
  `.github/workflows/release-please.yml` lists everything infra provisions —
  including the fact that PyPI matches the Trusted Publisher on the workflow's
  *filename*, so renaming that file breaks publishing.
- **Recover an interrupted PyPI upload with a manual workflow run.** If
  release-please created the tag and GitHub release but the publish job did not
  complete, run the `release-please` workflow from `main` and provide the
  existing `vX.Y.Z` tag. The workflow checks out that exact tag, verifies it
  matches the package version, and runs the normal checks, build, and trusted
  upload. Do not rerun the original failed workflow: GitHub reruns use its
  original workflow revision, and release-please will not create the same
  release twice. From the command line, run
  `gh workflow run release-please.yml --ref main -f release_tag=v0.3.0`,
  replacing `v0.3.0` with the release that needs recovery.
- The package is pre-1.0, so release-please's defaults apply: `feat:` bumps the
  minor version and a breaking change bumps to `1.0.0`. If we'd rather stay
  below 1.0 through a breaking change, that's the `bump-minor-pre-major` option
  in `release-please-config.json` — a deliberate decision, not a default to
  drift into.
