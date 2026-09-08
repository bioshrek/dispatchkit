"""D9: `verify = "auto"` may only mean "green CI" if CI runs the acceptance.

The hole this closes was shipped, not hypothetical. `merge_ops` merges when the
check suites are green, and nothing anywhere established that those suites run
the task's own `acceptance` command. On any repository whose CI happens not to
cover it, "green" would have meant "something unrelated passed" and dispatchkit
would have merged on it.

The rule is deliberately conservative. It can refuse a task whose acceptance CI
genuinely does cover, and that is the correct direction to be wrong in: the
remedy is to widen CI or mark the task `human`, neither of which merges
anything by mistake.
"""

from __future__ import annotations

from dispatchkit.model import Lane, Task, TaskGraph, TaskId, Verify
from dispatchkit.validate import (
    ci_commands,
    triggers_on_pull_request,
    validate_acceptance,
)

WORKFLOW = """
name: CI
on: [pull_request]
jobs:
  check:
    steps:
      - uses: actions/checkout@v4
      - run: uv sync --group dev
      - run: uv run ruff check .
      - run: |
          uv run pytest -q
          uv run mypy --strict src
"""


def task(name: str, acceptance: str, verify: Verify = Verify.AUTO) -> Task:
    return Task(
        id=TaskId(name),
        title=name,
        milestone="M",
        lane=Lane.CLOUD,
        acceptance=acceptance,
        verify=verify,
    )


def graph(*tasks: Task) -> TaskGraph:
    return TaskGraph(plan="demo", tasks=tasks)


class TestReadingTheWorkflow:
    """Extracted by scanning, not by parsing: `pyyaml` is a dev dependency.

    `src/dispatchkit` is pure standard library and stays that way, so this
    reads `run:` steps off the text the way `doctor` already reads
    `PYTHONPATH`.
    """

    def test_single_line_steps_are_found(self) -> None:
        assert "uv run ruff check ." in ci_commands(WORKFLOW)

    def test_block_steps_are_found(self) -> None:
        commands = ci_commands(WORKFLOW)
        assert "uv run pytest -q" in commands
        assert "uv run mypy --strict src" in commands

    def test_chained_commands_are_split(self) -> None:
        assert "b" in ci_commands("      - run: a && b")

    def test_uses_steps_are_not_commands(self) -> None:
        assert not any("actions/checkout" in c for c in ci_commands(WORKFLOW))


class TestTheSubsetRule:
    def test_an_exact_match_passes(self) -> None:
        graph_ = graph(task("a", "uv run ruff check ."))
        assert validate_acceptance(graph_, ci_commands(WORKFLOW)) == []

    def test_a_narrowed_command_passes(self) -> None:
        # CI runs `uv run pytest -q`; the task narrows it with a selector, so
        # CI runs a superset and a failure here is a failure there.
        graph_ = graph(task("a", "uv run pytest -q -k stopword"))
        assert validate_acceptance(graph_, ci_commands(WORKFLOW)) == []

    def test_every_clause_must_be_covered(self) -> None:
        graph_ = graph(task("a", "uv run ruff check . && ./scripts/custom.sh"))
        issues = validate_acceptance(graph_, ci_commands(WORKFLOW))
        assert [i.code for i in issues] == ["acceptance-not-in-ci"]
        assert "custom.sh" in issues[0].message

    def test_a_widened_command_is_refused(self) -> None:
        # `pytest` alone runs more than CI's `pytest -q`, so CI passing says
        # nothing about it. Conservative, and safe in the right direction.
        graph_ = graph(task("a", "uv run pytest"))
        assert [i.code for i in validate_acceptance(graph_, ci_commands(WORKFLOW))] == [
            "acceptance-not-in-ci"
        ]

    def test_human_tasks_are_not_checked(self) -> None:
        graph_ = graph(task("a", "listen to it", verify=Verify.HUMAN))
        assert validate_acceptance(graph_, ci_commands(WORKFLOW)) == []

    def test_a_repository_with_no_ci_cannot_have_auto_tasks(self) -> None:
        issues = validate_acceptance(graph(task("a", "uv run pytest -q")), ())
        assert [i.code for i in issues] == ["acceptance-not-in-ci"]
        assert "no CI" in issues[0].message


class TestOnlyPullRequestWorkflowsCount:
    """A workflow that never runs on a pull request produces no check on it.

    Without this the hole reappears in miniature: dispatchkit's own scheduler
    workflow runs `dispatchkit tick` on a cron, and counting its `run:` steps
    as CI coverage would let a task claim green from a workflow that never
    reports on the pull request at all.
    """

    def test_a_pull_request_workflow_counts(self) -> None:
        assert triggers_on_pull_request(WORKFLOW)

    def test_a_cron_only_workflow_does_not(self) -> None:
        scheduler = (
            "name: s\non:\n  schedule:\n    - cron: '7 * * * *'\n"
            "jobs:\n  t:\n    steps:\n      - run: x\n"
        )
        assert not triggers_on_pull_request(scheduler)

    def test_a_push_to_the_default_branch_is_not_enough(self) -> None:
        # It reports on `main` after the fact, which is too late to gate a merge.
        assert not triggers_on_pull_request("on:\n  push:\n    branches: [main]\njobs: {}\n")

    def test_the_word_must_be_a_trigger_not_a_mention(self) -> None:
        # `pull_request` appearing in a step's script is not a trigger.
        text = (
            "on:\n  schedule:\n    - cron: '0 * * * *'\n"
            "jobs:\n  t:\n    steps:\n      - run: gh pull_request list\n"
        )
        assert not triggers_on_pull_request(text)
