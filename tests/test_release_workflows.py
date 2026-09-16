"""The release-please pipeline, as a contract.

Releases run entirely from `.github/workflows/release-please.yml`: a push to
`main` grooms the Release PR, and merging that PR builds and uploads to PyPI
over OIDC Trusted Publishing. Three properties of that file are load-bearing
and easy to weaken by accident, so they are pinned here:

* **Pins.** Both third-party actions are SHA-pinned. A floating tag would let
  an upstream (or a compromised tag) run with `contents: write` and a token
  that can publish to PyPI as us.
* **The gate.** Publishing happens only when release-please itself reports
  `releases_created == 'true'`. Unlike the PR title/body markers this replaced,
  that output is produced by the action, not by a PR author.
* **No credentials.** Trusted Publishing needs `id-token: write` and *no*
  secret. Reintroducing a `PYPI_TOKEN` would work, which is exactly why the
  absence has to be asserted rather than assumed.

`release-please-config.json` / `.release-please-manifest.json` and the
assumptions the `python` release type makes about this repo's layout are
covered here too, since a mismatch there surfaces as a bad version bump inside
an already-merged Release PR.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import yaml

REPO = Path(__file__).resolve().parent.parent
WORKFLOWS = REPO / ".github" / "workflows"
WORKFLOW_FILENAME = "release-please.yml"
WORKFLOW_PATH = WORKFLOWS / WORKFLOW_FILENAME
CONFIG_PATH = REPO / "release-please-config.json"
MANIFEST_PATH = REPO / ".release-please-manifest.json"
PYPROJECT_PATH = REPO / "pyproject.toml"
UV_LOCK_PATH = REPO / "uv.lock"
PACKAGE_INIT = REPO / "src" / "amplitude_mcp_analytics" / "__init__.py"

ENVIRONMENT = "pypi-release"
PACKAGE_NAME = "amplitude-mcp-analytics"

# The GitHub App whose installation token release-please authors PRs with, so
# that those PRs trigger test.yml / semantic-pr.yml. Nothing else is secret.
EXPECTED_SECRETS = {"AMPLITUDE_DEV_EXP_APP_ID", "AMPLITUDE_DEV_EXP_PRIVATE_KEY"}

EXPECTED_PINS = {
    "actions/create-github-app-token": "df432ceedc7162793a195dd1713ff69aefc7379e",
    "googleapis/release-please-action": "16a9c90856f42705d54a6fda1823352bdc62cf38",
}

SHA_PINNED = re.compile(r"^[^@\s]+@[0-9a-f]{40}$")
SECRET_REF = re.compile(r"secrets\.([A-Za-z0-9_]+)")

# release-please's PythonFileWithVersion updater, verbatim (Python flavour):
# it rewrites the first match it finds in `src/<package>/__init__.py`.
RELEASE_PLEASE_VERSION_LITERAL = re.compile(
    r"""(__version__ ?= ?["'])[0-9]+\.[0-9]+\.[0-9]+(?:-\w+)?(["'])"""
)


def workflow() -> dict[str, Any]:
    return yaml.safe_load(WORKFLOW_PATH.read_text())


def triggers() -> dict[str, Any]:
    # PyYAML reads the bare key `on` as the boolean True (YAML 1.1).
    parsed = workflow()
    return parsed.get("on", parsed.get(True, {}))


def jobs() -> dict[str, Any]:
    return workflow()["jobs"]


def steps(job: str) -> list[dict[str, Any]]:
    return jobs()[job]["steps"]


def run_scripts(job: str) -> list[str]:
    return [str(step.get("run", "")) for step in steps(job)]


def uses_lines() -> list[str]:
    return [str(step["uses"]) for job in jobs().values() for step in job["steps"] if "uses" in step]


def uncommented_lines() -> list[str]:
    lines = WORKFLOW_PATH.read_text().splitlines()
    return [line for line in lines if not line.strip().startswith("#")]


def config() -> dict[str, Any]:
    return json.loads(CONFIG_PATH.read_text())


def manifest() -> dict[str, Any]:
    return json.loads(MANIFEST_PATH.read_text())


def pyproject_version() -> str:
    project_table = PYPROJECT_PATH.read_text().split("[project]", 1)[1]
    match = re.search(r'^version = "([^"]+)"', project_table, re.MULTILINE)
    assert match is not None, "pyproject.toml has no [project] version"
    return match.group(1)


def root_lock_version() -> str:
    package_block = UV_LOCK_PATH.read_text().split(
        'name = "amplitude-mcp-analytics"', 1
    )[1]
    match = re.search(r'^version = "([^"]+)"', package_block, re.MULTILINE)
    assert match is not None, "uv.lock has no root project version"
    return match.group(1)


class TestTheOldFlowIsGone:
    def test_the_hand_rolled_release_workflows_are_deleted(self) -> None:
        # Both are superseded by release-please.yml; leaving either behind
        # would give the repo two ways to cut a release.
        assert not (WORKFLOWS / "release.yml").exists()
        assert not (WORKFLOWS / "publish.yml").exists()

    def test_release_please_is_the_only_release_workflow(self) -> None:
        names = sorted(path.name for path in WORKFLOWS.glob("*.yml"))
        assert names == [WORKFLOW_FILENAME, "semantic-pr.yml", "test.yml"]


class TestWorkflowShape:
    def test_it_runs_on_pushes_to_main_only(self) -> None:
        assert triggers() == {"push": {"branches": ["main"]}}

    def test_runners_are_version_pinned(self) -> None:
        # `ubuntu-latest` silently rolls the publish environment under us.
        runners = [job["runs-on"] for job in jobs().values()]
        assert runners == ["ubuntu-24.04", "ubuntu-24.04"]

    def test_both_jobs_run_in_the_release_environment(self) -> None:
        # The App secrets and the PyPI Trusted Publisher are both scoped to it.
        assert [job.get("environment") for job in jobs().values()] == [ENVIRONMENT] * 2

    def test_release_pr_updates_are_serialized(self) -> None:
        assert workflow()["concurrency"] == {
            "group": "release-please-main",
            "cancel-in-progress": False,
        }


class TestActionsArePinned:
    def test_every_action_is_sha_pinned(self) -> None:
        unpinned = [
            line for line in uses_lines() if not SHA_PINNED.match(line.split(" #")[0].strip())
        ]
        assert unpinned == [], f"floating action refs: {unpinned}"

    def test_the_expected_pins_are_in_place(self) -> None:
        pinned = {
            line.split("@")[0]: line.split("@")[1].split(" #")[0].strip()
            for line in uses_lines()
        }
        for action, sha in EXPECTED_PINS.items():
            assert pinned.get(action) == sha


class TestPublishGate:
    def test_release_please_exports_releases_created(self) -> None:
        outputs = jobs()["release-please"]["outputs"]
        assert outputs["releases_created"] == "${{ steps.release.outputs.releases_created }}"

    def test_publish_depends_on_release_please(self) -> None:
        assert jobs()["publish"]["needs"] == "release-please"

    def test_publish_is_gated_on_releases_created(self) -> None:
        condition = str(jobs()["publish"]["if"]).strip()
        assert condition == "needs.release-please.outputs.releases_created == 'true'"

    def test_the_gate_has_no_alternative_branch(self) -> None:
        # A single `||` would let some other condition carry the publish.
        assert "||" not in str(jobs()["publish"]["if"])

    def test_only_two_jobs_exist(self) -> None:
        # A third job would need its own gate; today there is nothing to miss.
        assert list(jobs()) == ["release-please", "publish"]


class TestPublishGatesAndTooling:
    def test_the_checks_run_before_the_upload(self) -> None:
        scripts = run_scripts("publish")
        index = {
            name: next(i for i, script in enumerate(scripts) if name in script)
            for name in ("pyright src", "pytest", "uv build", "uv publish")
        }
        assert index["pyright src"] < index["uv publish"]
        assert index["pytest"] < index["uv publish"]
        assert index["uv build"] < index["uv publish"]

    def test_dependencies_come_from_a_current_lockfile(self) -> None:
        scripts = run_scripts("publish")
        assert any("uv sync --locked" in script for script in scripts)
        assert all("--frozen" not in script for script in scripts)


class TestReleasePRLockfile:
    def test_release_pr_is_checked_out_with_the_app_token(self) -> None:
        checkout = next(
            step for step in steps("release-please") if step.get("name") == "Checkout the Release PR"
        )
        assert checkout["if"] == "steps.release.outputs.prs_created == 'true'"
        assert checkout["with"]["ref"] == (
            "${{ fromJSON(steps.release.outputs.pr).headBranchName }}"
        )
        assert checkout["with"]["token"] == "${{ steps.app-token.outputs.token }}"

    def test_uv_regenerates_and_commits_the_lockfile(self) -> None:
        update = next(
            step
            for step in steps("release-please")
            if step.get("name") == "Update the Release PR lockfile"
        )
        script = update["run"]
        for command in ("uv lock", "git add uv.lock", "git commit", "git push"):
            assert command in script

    def test_the_committed_lockfile_matches_the_project(self) -> None:
        assert root_lock_version() == pyproject_version()


class TestTrustedPublishing:
    def test_id_token_write_is_granted(self) -> None:
        # Without it uv has no OIDC token to exchange and the upload fails.
        assert workflow()["permissions"]["id-token"] == "write"

    def test_publish_forces_trusted_publishing(self) -> None:
        # `always`, not the default `automatic`: no silent fallback to an
        # unauthenticated or credential-scavenging upload.
        assert any(
            "uv publish --trusted-publishing always" in script for script in run_scripts("publish")
        )

    def test_the_only_secrets_used_are_the_github_app_credentials(self) -> None:
        referenced = set(SECRET_REF.findall(WORKFLOW_PATH.read_text()))
        assert referenced == EXPECTED_SECRETS

    def test_no_upload_credential_is_configured(self) -> None:
        # Comments deliberately mention PYPI_TOKEN to say it must not exist, so
        # only live YAML is scanned.
        forbidden = (
            "PYPI_TOKEN",
            "UV_PUBLISH_TOKEN",
            "UV_PUBLISH_PASSWORD",
            "--token",
            "--password",
        )
        offenders = [
            line for line in uncommented_lines() if any(word in line for word in forbidden)
        ]
        assert offenders == []


class TestReleasePleaseConfiguration:
    def test_the_config_declares_one_python_package_at_the_root(self) -> None:
        packages = config()["packages"]
        assert list(packages) == ["."]
        assert packages["."]["release-type"] == "python"

    def test_tags_are_not_component_prefixed(self) -> None:
        # Single-package repo: releases are `v1.2.3`, not
        # `amplitude-mcp-analytics-v1.2.3`.
        assert config()["packages"]["."]["include-component-in-tag"] is False

    def test_breaking_changes_stay_below_1_0_0(self) -> None:
        # Without this option release-please's default sends a breaking change
        # in a pre-1.0 package straight to 1.0.0 — which it did: the first
        # Release PR proposed 1.0.0 off the `fix!` that changed the device-id
        # namespace. Staying pre-1.0 is deliberate while the wire contract is
        # still settling, so a breaking change bumps the minor instead.
        assert config()["packages"]["."]["bump-minor-pre-major"] is True

    def test_the_manifest_covers_exactly_the_configured_packages(self) -> None:
        # release-please refuses to run when the two disagree.
        assert set(manifest()) == set(config()["packages"])

    def test_the_manifest_tracks_the_pyproject_version(self) -> None:
        # The manifest is the release-please source of truth; if it drifts from
        # pyproject.toml the next bump is computed from the wrong base.
        assert manifest()["."] == pyproject_version()

    def test_the_workflow_points_at_these_files(self) -> None:
        release_step = next(
            step for step in steps("release-please") if "release-please-action" in step["uses"]
        )
        assert release_step["with"]["config-file"] == CONFIG_PATH.name
        assert release_step["with"]["manifest-file"] == MANIFEST_PATH.name


class TestPythonReleaseTypeAssumptions:
    """What the `python` release type will touch in this repo."""

    def test_pyproject_is_the_only_declared_version(self) -> None:
        # The python strategy also updates setup.py / setup.cfg / any version.py
        # when they exist. None do, so pyproject.toml stays the single source.
        assert not (REPO / "setup.py").exists()
        assert not (REPO / "setup.cfg").exists()
        assert list((REPO / "src").rglob("version.py")) == []

    def test_the_uninstalled_version_fallback_is_not_rewritable(self) -> None:
        # release-please always queues an update for
        # `src/<package>/__init__.py` and rewrites the first
        # `__version__ = "x.y.z"` literal in it. Ours is bound through a name so
        # the sentinel is not silently bumped to the released version.
        assert RELEASE_PLEASE_VERSION_LITERAL.search(PACKAGE_INIT.read_text()) is None


class TestInfraHandoff:
    """The header comment is the only record of what infra must provision."""

    def test_the_header_names_what_must_exist(self) -> None:
        header = WORKFLOW_PATH.read_text().split("on:", 1)[0]
        for needle in (
            ENVIRONMENT,
            PACKAGE_NAME,
            "AMPLITUDE_DEV_EXP_APP_ID",
            "AMPLITUDE_DEV_EXP_PRIVATE_KEY",
            "Amplitude-MCP-Analytics-Python",
        ):
            assert needle in header, f"infra handoff comment no longer names {needle}"

    def test_the_header_names_this_files_own_name(self) -> None:
        # PyPI matches the Trusted Publisher on the workflow *filename*, so a
        # rename breaks publishing and must be mirrored in the handoff note.
        header = WORKFLOW_PATH.read_text().split("on:", 1)[0]
        assert WORKFLOW_FILENAME in header
