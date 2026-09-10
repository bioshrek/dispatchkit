"""D16: the local lane branches from and targets the plan's base.

The symmetric half of the cloud change, and the easier one: this side owns git
outright, so the base is not a request to somebody else's agent but an argument
to `git fetch` and `git worktree add`.

Three places have to agree, and they are easy to get *partly* right:

- the worktree is cut from `origin/<base>`, or the agent starts without its
  dependencies' work;
- the branch is pushed to the fork of that base, which is already true;
- the pull request is opened against `<base>`, or the work lands in `main`
  after all — silently, because opening a pull request against the default
  branch is exactly what succeeds today.

The third is the one worth a test of its own. Everything upstream can be right
and the change still fails at the last step, in the direction that looks like
success.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from dispatchkit.config import SchedulerConfig
from dispatchkit.local import run_local
from dispatchkit.model import DEFAULT_BASE, Base, Lane, TaskId
from dispatchkit.resolve import TaskItem, build_items
from dispatchkit.workstation_cli import CliWorkstation, add_command, fetch_command
from tests.fake_github import FakeGitHub
from tests.fake_workstation import FakeWorkstation
from tests.items import issue, state_of

pytestmark = pytest.mark.unit

NOW = datetime(2026, 9, 11, 12, 0, tzinfo=UTC)
ROOT = Path("/tmp/dispatchkit-test")
WORKTREE = ROOT / "demo" / "one"
PLAN_BASE = Base("plan/wordfreq")


def run(base: Base) -> tuple[FakeGitHub, FakeWorkstation]:
    api = FakeGitHub(state=state_of(issue("one", number=1, lane=Lane.LOCAL, base=base)))
    machine = FakeWorkstation(commit_counts={WORKTREE: 1})
    resolved, _ = build_items(api.fetch_state())
    task: TaskItem = next(item for item in resolved if item.ref.id == TaskId("one"))
    run_local(
        task,
        api=api,
        machine=machine,
        config=SchedulerConfig(),
        root=ROOT,
        now=NOW,
    )
    return api, machine


class TestThePullRequestTargetsTheBase:
    """The failure that would look like success."""

    def test_it_is_opened_against_the_plan_branch(self) -> None:
        api, _ = run(PLAN_BASE)
        assert api.opened and api.opened[-1]["base"] == str(PLAN_BASE)

    def test_a_plan_on_main_is_unchanged(self) -> None:
        api, _ = run(DEFAULT_BASE)
        assert api.opened and api.opened[-1]["base"] == "main"


class TestTheWorktreeIsCutFromTheBase:
    def test_the_workstation_is_told_the_base(self) -> None:
        _, machine = run(PLAN_BASE)
        assert machine.bases == [str(PLAN_BASE)]


class TestTheCommandsThemselves:
    def test_the_fetch_names_the_base(self) -> None:
        assert fetch_command("origin", "plan/wordfreq") == [
            "git",
            "fetch",
            "--quiet",
            "origin",
            "plan/wordfreq",
        ]

    def test_the_worktree_starts_at_the_remote_base(self) -> None:
        command = add_command(Path("/w"), "dispatchkit/demo/one/1", "origin/plan/wordfreq")
        assert "origin/plan/wordfreq" in command

    def test_the_machine_holds_no_base_of_its_own(self) -> None:
        # One `watch` serves every plan in the repository, and two of them may
        # integrate on different branches. A base on the machine could only
        # ever be right for one of them, so there is not one.
        assert not hasattr(CliWorkstation(root=Path("/w"), repo=Path("/r")), "base")


class TestCountingWorkIsNotAskedOfTheBase:
    """The gate that stopped an empty branch (D6.6) would have re-opened.

    `commits` answered `origin/main..HEAD`. On a plan branch that is not the
    branch the worktree was cut from, so every commit the *plan* already
    carried counted as this task's work — the "the agent changed nothing" gate
    passes, an empty branch is pushed, and a pull request opens with no diff.
    The same defect D6.6 spent a live run finding, re-introduced by a base the
    counting was never told about.

    Rather than thread the base through a third place, the question is asked
    the way it was always meant: work here that `origin` has never seen. That
    is what "would be lost" means, and it needs no base at all.
    """

    def test_it_counts_against_the_remote_rather_than_a_named_branch(self) -> None:
        from dispatchkit.workstation_cli import commits_command

        assert commits_command() == [
            "git",
            "rev-list",
            "--count",
            "HEAD",
            "--not",
            "--remotes=origin",
        ]
