"""D5: the dispatch workflow file is part of the design, so it is tested.

Three things here are security or correctness properties rather than
formatting, and each one has already been a real incident somewhere:

- the token's permissions (never `contents: write` — only PRs mutate the tree),
- no `${{ }}` interpolation inside a `run:` script (expression injection),
- the cron offset (the alert platform throttles on the hour and half hour).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "dispatchkit.yml"


@pytest.fixture(scope="module")
def workflow() -> dict[Any, Any]:
    loaded: dict[Any, Any] = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    # YAML 1.1 reads a bare `on:` as the boolean True, which is why this key
    # is looked up defensively rather than by name.
    return loaded


def triggers(workflow: dict[Any, Any]) -> dict[str, Any]:
    on: dict[str, Any] = workflow.get("on") or workflow[True]
    return on


def steps(workflow: dict[Any, Any]) -> list[dict[str, Any]]:
    return [step for job in workflow["jobs"].values() for step in job["steps"]]


class TestPermissions:
    def test_the_token_can_never_write_to_the_tree(self) -> None:
        # Least privilege, and the rule that only PRs mutate the repository.
        assert yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["permissions"][
            "contents"
        ] == "read"

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
    def test_no_run_step_interpolates_a_github_expression(
        self, workflow: dict[Any, Any]
    ) -> None:
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
