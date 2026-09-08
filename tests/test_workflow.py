"""D5: the dispatch workflow file is part of the design, so it is tested.

Three things here are security or correctness properties rather than
formatting, and each one has already been a real incident somewhere:

- the token's permissions (never `contents: write` — only PRs mutate the tree),
- no `${{ }}` interpolation inside a `run:` script (expression injection),
- the cron offset (the alert platform throttles on the hour and half hour).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from dispatchkit.init import WORKFLOW_TEMPLATE

WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "dispatchkit.yml"

#: Both workflows that exist: the one this repository runs, and the one `init`
#: writes into somebody else's. They cannot be byte-identical — this repository
#: already has `src/dispatchkit` in its tree and an adopter does not — so the
#: guarantee is that every property below holds of both. Pinning them to each
#: other, as this file used to, asserted a sameness that was not true and hid a
#: template that could not run anywhere but here.
SOURCES = {
    "this repository": WORKFLOW.read_text(encoding="utf-8"),
    "the init template": WORKFLOW_TEMPLATE,
}


@pytest.fixture
def template() -> dict[Any, Any]:
    loaded: dict[Any, Any] = yaml.safe_load(WORKFLOW_TEMPLATE)
    return loaded


@pytest.fixture(params=sorted(SOURCES), ids=sorted(SOURCES))
def workflow(request: pytest.FixtureRequest) -> dict[Any, Any]:
    # YAML 1.1 reads a bare `on:` as the boolean True, which is why the `on`
    # key is looked up defensively rather than by name.
    loaded: dict[Any, Any] = yaml.safe_load(SOURCES[request.param])
    return loaded


def triggers(workflow: dict[Any, Any]) -> dict[str, Any]:
    on: dict[str, Any] = workflow.get("on") or workflow[True]
    return on


def steps(workflow: dict[Any, Any]) -> list[dict[str, Any]]:
    return [step for job in workflow["jobs"].values() for step in job["steps"]]


class TestPermissions:
    def test_the_token_can_never_write_to_the_tree(self, workflow: dict[Any, Any]) -> None:
        # Least privilege, and the rule that only PRs mutate the repository.
        assert workflow["permissions"]["contents"] == "read"

    def test_it_holds_exactly_the_permissions_the_pass_needs(
        self, workflow: dict[Any, Any]
    ) -> None:
        assert workflow["permissions"] == {
            "issues": "write",
            "repository-projects": "write",
            "contents": "read",
        }


class TestTriggers:
    def test_it_reacts_to_the_events_that_unblock_work(self, workflow: dict[Any, Any]) -> None:
        on = triggers(workflow)
        assert set(on["issues"]["types"]) == {"closed", "reopened", "labeled"}
        assert on["pull_request"]["types"] == ["closed"]

    def test_cron_is_the_self_healing_safety_net(self, workflow: dict[Any, Any]) -> None:
        assert triggers(workflow)["schedule"][0]["cron"] == "7,37 * * * *"

    def test_cron_avoids_the_busy_hour_and_half_hour(self, workflow: dict[Any, Any]) -> None:
        minutes = triggers(workflow)["schedule"][0]["cron"].split()[0]
        assert "0" not in minutes.split(",")
        assert "30" not in minutes.split(",")

    def test_it_can_be_run_by_hand(self, workflow: dict[Any, Any]) -> None:
        assert "workflow_dispatch" in triggers(workflow)


class TestConcurrency:
    def test_passes_are_serialised(self, workflow: dict[Any, Any]) -> None:
        assert workflow["concurrency"]["group"]

    def test_a_running_pass_is_never_cancelled(self, workflow: dict[Any, Any]) -> None:
        # Cancelling mid-pass could interrupt between assigning an issue and
        # recording it. Harmless (the next pass reconciles) but pointless.
        assert workflow["concurrency"]["cancel-in-progress"] is False


class TestScriptSafety:
    def test_no_run_step_interpolates_a_github_expression(self, workflow: dict[Any, Any]) -> None:
        # `${{ }}` inside `run:` is substituted before the shell sees it, so a
        # value carrying a quote becomes a command. Values arrive via `env:`.
        for step in steps(workflow):
            assert "${{" not in step.get("run", "")

    def test_the_pass_is_told_which_plan_to_run(self, workflow: dict[Any, Any]) -> None:
        assert any("--plan" in step.get("run", "") for step in steps(workflow))

    def test_the_pass_actually_pushes(self, workflow: dict[Any, Any]) -> None:
        assert any("--push" in step.get("run", "") for step in steps(workflow))

    def test_actions_are_pinned_to_a_major_version(self, workflow: dict[Any, Any]) -> None:
        for step in steps(workflow):
            if "uses" in step:
                assert "@" in step["uses"]


class TestDependencies:
    def test_the_pass_installs_nothing(self, workflow: dict[Any, Any]) -> None:
        """`src/dispatchkit` is pure standard library, and stays that way.

        A job holding a token that can assign work is the last place to run a
        freshly resolved dependency tree, and there is nothing here to resolve.
        """
        scripts = " ".join(step.get("run", "") for step in steps(workflow))
        assert "uv sync" not in scripts
        assert "pip install" not in scripts

    def test_the_dispatch_package_imports_no_third_party_module(self) -> None:
        import ast
        import sys

        package = Path(__file__).resolve().parents[1] / "src" / "dispatchkit"
        roots = set()
        for source in package.glob("*.py"):
            for node in ast.walk(ast.parse(source.read_text(encoding="utf-8"))):
                if isinstance(node, ast.Import):
                    roots |= {alias.name.split(".")[0] for alias in node.names}
                elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    roots.add(node.module.split(".")[0])

        assert roots - {"dispatchkit", "__future__"} <= sys.stdlib_module_names


class TestTheTemplateCanRunWhereItIsWritten:
    """`init` writes this into a repository that does not contain dispatchkit.

    The bug: the template set `PYTHONPATH: src` and explained that there was
    nothing to install. True here, false everywhere else — the scheduler died
    on `No module named dispatchkit` in every repository `init` had ever
    touched, and nothing caught it because the only test read this repository's
    own copy, where `src/dispatchkit` happens to exist.
    """

    def test_it_fetches_the_dispatchkit_source(self, template: dict[Any, Any]) -> None:
        checkouts = [step for step in steps(template) if "checkout" in step.get("uses", "")]
        assert any("repository" in step.get("with", {}) for step in checkouts)

    def test_the_source_lands_where_pythonpath_points(self, template: dict[Any, Any]) -> None:
        # The two halves are written in different steps, so nothing but a test
        # keeps them agreeing.
        paths = {
            step["with"]["path"]
            for step in steps(template)
            if "checkout" in step.get("uses", "") and "path" in step.get("with", {})
        }
        env = next(step["env"] for step in steps(template) if "run" in step)
        assert any(path in env["PYTHONPATH"] for path in paths)

    def test_the_source_checkout_is_pinned(self, template: dict[Any, Any]) -> None:
        # An unpinned default branch is a third party deciding what runs in a
        # job that holds a token which can assign work.
        for step in steps(template):
            with_ = step.get("with", {})
            if "repository" in with_:
                assert with_.get("ref")

    def test_the_default_ref_is_a_version_tag(self, template: dict[Any, Any]) -> None:
        # Asserting a ref merely exists would accept `ref: main`, which is the
        # exact thing the pin is for: a moving branch means every adopter runs
        # whatever was pushed here last, unreviewed, in a job holding a token
        # that can assign work. The default must name an immutable release.
        for step in steps(template):
            with_ = step.get("with", {})
            if "repository" not in with_:
                continue
            default = with_["ref"].split("||")[-1].strip(" }'\"")
            assert re.fullmatch(r"v\d+\.\d+\.\d+", default), default

    def test_it_still_installs_nothing(self, template: dict[Any, Any]) -> None:
        # Fetching source is not installing: no resolver, no build, no
        # third-party code. The rule survives the fix.
        scripts = " ".join(step.get("run", "") for step in steps(template))
        assert "pip install" not in scripts
        assert "uv sync" not in scripts

    def test_this_repository_runs_its_own_source(self) -> None:
        # The mirror image: here there is nothing to fetch, and fetching would
        # test master rather than the branch under review.
        here: dict[Any, Any] = yaml.safe_load(SOURCES["this repository"])
        env = next(step["env"] for step in steps(here) if "run" in step)
        assert env["PYTHONPATH"] == "src"


def test_the_pass_is_skipped_until_the_repository_is_configured(
    workflow: dict[Any, Any],
) -> None:
    """An unconfigured repository must not fail every half hour.

    Found on dispatchkit's own repository, which carries the workflow but has
    never been given a plan or a project: the cron fired on schedule and died
    on `--project: invalid int value: ''`, twice an hour, for as long as it had
    been installed. A repository that has not been set up yet is not a failure
    to alert on, and a scheduler that cries wolf every thirty minutes is one
    nobody reads.
    """
    job = workflow["jobs"]["tick"]
    condition = job.get("if", "")
    assert "DISPATCHKIT_PLAN" in condition
    assert "DISPATCHKIT_PROJECT" in condition
