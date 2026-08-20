"""The release/publish workflow pair, as a contract.

`publish.yml` runs with `contents: write` and the PyPI token, and its only gate
is the job-level `if:`. Two of the facts it checks (title, body) are free text
the PR author controls, so the authorization has to rest on facts the forge
sets — the PR's author and its head branch. These tests pin that gate and keep
it consistent with what `release.yml` actually produces, since a drift in
either file silently turns the guard into a rubber stamp.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

WORKFLOWS = Path(__file__).resolve().parent.parent / ".github" / "workflows"

# release.yml opens the PR with `gh pr create` authenticated as the default
# GITHUB_TOKEN, which attributes it to the Actions bot.
RELEASE_PR_AUTHOR = "github-actions[bot]"
RELEASE_BRANCH_PREFIX = "release/v"


def load(name: str) -> dict[str, Any]:
    return yaml.safe_load((WORKFLOWS / name).read_text())


def publish_condition() -> str:
    return load("publish.yml")["jobs"]["publish"]["if"]


def release_script() -> str:
    steps = load("release.yml")["jobs"]["open-release-pr"]["steps"]
    return "\n".join(str(step.get("run", "")) for step in steps)


class TestPublishAuthorization:
    def test_requires_the_pr_to_be_merged(self) -> None:
        assert "github.event.pull_request.merged == true" in publish_condition()

    def test_requires_the_actions_bot_as_the_pr_author(self) -> None:
        # Without this, any contributor whose PR is merged can trigger a
        # publish by copying the public title/body strings.
        assert (
            f"github.event.pull_request.user.login == '{RELEASE_PR_AUTHOR}'"
            in publish_condition()
        )

    def test_requires_a_release_head_branch(self) -> None:
        assert (
            f"startsWith(github.event.pull_request.head.ref, '{RELEASE_BRANCH_PREFIX}')"
            in publish_condition()
        )

    def test_keeps_the_title_and_body_markers_as_defense_in_depth(self) -> None:
        condition = publish_condition()
        assert "startsWith(github.event.pull_request.title, 'chore(release): v')" in condition
        assert "contains(github.event.pull_request.body, 'Automated release PR for')" in condition

    def test_every_clause_is_required_together(self) -> None:
        # `&&` only: one `||` anywhere would let a single clause carry the gate.
        condition = publish_condition()
        assert "||" not in condition
        assert condition.count("&&") == 4

    def test_the_publish_job_is_the_only_job(self) -> None:
        # A second job would need its own guard; today there is nothing to miss.
        assert list(load("publish.yml")["jobs"]) == ["publish"]


class TestReleasePrMatchesTheGuard:
    """The guard is only as good as its agreement with the producer."""

    def test_release_pushes_a_branch_the_guard_accepts(self) -> None:
        script = release_script()
        # branch=release/${NEW_VERSION} with NEW_VERSION="v$(uv version --short)"
        assert 'echo "branch=release/${NEW_VERSION}"' in script
        assert 'NEW_VERSION="v$(uv version --short)"' in script
        assert 'git push -u origin "$BRANCH"' in script
        assert '--head "$BRANCH"' in script

    def test_release_opens_the_pr_as_the_actions_bot(self) -> None:
        # `gh pr create` with the default GITHUB_TOKEN → author github-actions[bot].
        steps = load("release.yml")["jobs"]["open-release-pr"]["steps"]
        pr_step = next(s for s in steps if "gh pr create" in str(s.get("run", "")))
        assert pr_step["env"]["GH_TOKEN"] == "${{ secrets.GITHUB_TOKEN }}"

    def test_release_title_and_body_match_the_markers(self) -> None:
        script = release_script()
        assert '--title "chore(release): ${VERSION}"' in script
        assert '--body "Automated release PR for ${VERSION}' in script
