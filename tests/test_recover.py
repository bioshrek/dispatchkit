"""D6.4: reconciling from the local disk outward.

There is no state file, so the question "what was this machine doing when it
died" is answered by the only durable thing a run produces: a worktree. This
walks them, preserves whatever they hold, and releases every claim — which is
also what makes a retry possible, since a retained failure would otherwise
collide with `git worktree add` on the next attempt.

Written as a planner over facts, so every case is a table row rather than a
sequence somebody has to reason about.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from dispatchkit.github import LABEL_LOCAL_CLAIM, IssueState
from dispatchkit.model import TaskId, TaskRef
from dispatchkit.recover import Recovery, execute_recovery, plan_recovery
from dispatchkit.resolve import TaskItem, build_items
from dispatchkit.workstation import Worktree
from tests.fake_github import FakeGitHub
from tests.fake_workstation import ROOT, FakeWorkstation
from tests.items import issue, ref, state_of

pytestmark = pytest.mark.unit

NOW = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)


def tree(name: str, commits: int = 0) -> Worktree:
    reference = TaskRef(plan="demo", id=TaskId(name))
    return Worktree(reference, ROOT / "demo" / name, f"dispatchkit/{name}/1", commits)


def items_of(*issues: IssueState) -> tuple[TaskItem, ...]:
    api = FakeGitHub(state_of(*issues))
    resolved, _ = build_items(api.fetch_state())
    return resolved


class TestWhatIsWorthKeeping:
    """Commits are the only thing in a worktree that cannot be recreated."""

    def test_a_worktree_holding_commits_has_its_branch_preserved(self) -> None:
        marked = issue("gpu", number=1, labels=("dispatchkit", LABEL_LOCAL_CLAIM))
        plan = plan_recovery((tree("gpu", commits=3),), items_of(marked), root=ROOT)
        assert plan == (
            Recovery(
                ref=ref("gpu"),
                number=1,
                path=ROOT / "demo" / "gpu",
                branch="dispatchkit/gpu/1",
                push=True,
                report=True,
                release=True,
            ),
        )

    def test_a_worktree_holding_nothing_is_simply_discarded(self) -> None:
        # Nothing is lost, so saying anything about it would be noise on an
        # issue a human reads.
        marked = issue("gpu", number=1, labels=("dispatchkit", LABEL_LOCAL_CLAIM))
        plan = plan_recovery((tree("gpu"),), items_of(marked), root=ROOT)
        assert plan[0].push is False
        assert plan[0].report is False
        assert plan[0].release is True

    def test_the_worktree_is_always_removed(self) -> None:
        # Including a retained failure. Retention is for the human who is
        # looking now, not for ever: the path is not attempt-scoped, so leaving
        # it would make `git worktree add` fail on the retry, and a task that
        # cannot start is worse than a branch that has to be fetched.
        for commits in (0, 5):
            plan = plan_recovery(
                (tree("gpu", commits=commits),),
                items_of(issue("gpu", number=1, labels=("dispatchkit", LABEL_LOCAL_CLAIM))),
                root=ROOT,
            )
            assert plan[0].path == ROOT / "demo" / "gpu"


class TestAClaimNobodyIsServing:
    def test_a_marked_issue_with_no_worktree_is_released(self) -> None:
        # One dispatcher means a mark this process cannot account for is by
        # definition abandoned. There is no adoption and no reclaim timeout.
        marked = issue("gpu", number=1, labels=("dispatchkit", LABEL_LOCAL_CLAIM))
        plan = plan_recovery((), items_of(marked), root=ROOT)
        assert plan == (
            Recovery(
                ref=ref("gpu"),
                number=1,
                path=None,
                branch="",
                push=False,
                report=False,
                release=True,
            ),
        )

    def test_an_unmarked_issue_with_no_worktree_is_not_mentioned(self) -> None:
        plan = plan_recovery((), items_of(issue("gpu", number=1)), root=ROOT)
        assert plan == ()

    def test_a_closed_task_is_cleaned_up_but_not_commented_on(self) -> None:
        # It closed because its pull request merged, so the branch is already
        # on the remote and the issue is finished. Reopening the conversation
        # would say nothing true.
        closed = issue("gpu", number=1, closed=True, labels=("dispatchkit",))
        plan = plan_recovery((tree("gpu", commits=2),), items_of(closed), root=ROOT)
        assert plan[0].report is False
        assert plan[0].release is False
        assert plan[0].push is True

    def test_a_worktree_for_a_task_this_repository_does_not_have_is_left_alone(self) -> None:
        # Another plan, another checkout, or a directory a human made. Deleting
        # somebody else's tree on the strength of a path is not ours to do.
        plan = plan_recovery((tree("elsewhere"),), items_of(issue("gpu", number=1)), root=ROOT)
        assert plan == ()


class TestRecoveryDoesIt:
    def setup_method(self) -> None:
        self.api = FakeGitHub(
            state_of(issue("gpu", number=1, labels=("dispatchkit", LABEL_LOCAL_CLAIM)))
        )
        self.machine = FakeWorkstation()

    def test_the_branch_is_pushed_before_the_worktree_is_removed(self) -> None:
        # The other order destroys the thing being preserved.
        self.machine.existing = (tree("gpu", commits=2),)
        self.machine.commit_counts[ROOT / "demo" / "gpu"] = 2
        execute_recovery(
            plan_recovery(self.machine.existing, items_of(*self.api.state.issues), root=ROOT),
            api=self.api,
            machine=self.machine,
        )
        assert self.machine.calls.index("push(dispatchkit/gpu/1)") < self.machine.calls.index(
            f"remove_worktree({ROOT / 'demo' / 'gpu'})"
        )

    def test_a_failed_push_does_not_take_the_worktree_with_it(self) -> None:
        # If the branch could not be saved, the tree is the only copy left.
        self.machine.existing = (tree("gpu", commits=2),)
        self.machine.push_fails = True
        execute_recovery(
            plan_recovery(self.machine.existing, items_of(*self.api.state.issues), root=ROOT),
            api=self.api,
            machine=self.machine,
        )
        assert self.machine.removed == []

    def test_the_mark_comes_off_even_when_the_push_fails(self) -> None:
        # Otherwise one bad push strands the task for ever, which is the whole
        # class of bug recovery exists to end.
        self.machine.existing = (tree("gpu", commits=2),)
        self.machine.push_fails = True
        execute_recovery(
            plan_recovery(self.machine.existing, items_of(*self.api.state.issues), root=ROOT),
            api=self.api,
            machine=self.machine,
        )
        assert any("-['dispatch:local']" in call for call in self.api.calls)

    def test_the_comment_names_the_branch_a_human_would_need(self) -> None:
        self.machine.existing = (tree("gpu", commits=2),)
        execute_recovery(
            plan_recovery(self.machine.existing, items_of(*self.api.state.issues), root=ROOT),
            api=self.api,
            machine=self.machine,
        )
        assert "dispatchkit/gpu/1" in self.api.comments[0]["body"]

    def test_recovering_twice_changes_nothing_the_second_time(self) -> None:
        # The convergence standard: recovery runs at startup and again before
        # every run, so it has to be a no-op the moment there is nothing left.
        self.machine.existing = (tree("gpu", commits=2),)
        execute_recovery(
            plan_recovery(self.machine.existing, items_of(*self.api.state.issues), root=ROOT),
            api=self.api,
            machine=self.machine,
        )
        self.machine.existing = ()
        before = len(self.api.calls)
        execute_recovery(
            plan_recovery(self.machine.existing, items_of(*self.api.state.issues), root=ROOT),
            api=self.api,
            machine=self.machine,
        )
        assert len(self.api.calls) == before


class TestNothingToDo:
    def test_a_clean_machine_plans_nothing(self) -> None:
        assert plan_recovery((), items_of(issue("gpu", number=1)), root=ROOT) == ()


class TestTheLockfile:
    """One dispatcher is a rule, and a rule needs something to enforce it."""

    def test_a_second_dispatcher_is_refused_by_name(self, tmp_path: Path) -> None:
        # The holder has to be a pid that is actually alive, because that is
        # the only kind this refuses -- see the stale-lockfile case below.
        import os

        from dispatchkit.recover import DispatcherBusy, hold_dispatcher

        alive = os.getpid()
        with hold_dispatcher(tmp_path / "lock", pid=alive):  # noqa: SIM117
            with pytest.raises(DispatcherBusy) as raised:
                with hold_dispatcher(tmp_path / "lock", pid=alive + 1):
                    pass
        assert str(alive) in str(raised.value)

    def test_the_lock_is_released_even_when_the_pass_raises(self, tmp_path: Path) -> None:
        # A crash inside a pass must not need a human to clear a file before
        # the next `watch` will start.
        import os

        from dispatchkit.recover import hold_dispatcher

        lock = tmp_path / "lock"
        with pytest.raises(ValueError, match="boom"):  # noqa: SIM117
            with hold_dispatcher(lock, pid=os.getpid()):
                raise ValueError("boom")
        assert not lock.exists()

    def test_a_lockfile_left_by_a_dead_process_is_taken_over(self, tmp_path: Path) -> None:
        # A machine that crashed leaves one behind. Refusing for ever on the
        # strength of a stale number would need a human to delete a file whose
        # existence they were never told about.
        from dispatchkit.recover import hold_dispatcher

        (tmp_path / "lock").write_text("999999999\n")
        with hold_dispatcher(tmp_path / "lock", pid=7):
            assert (tmp_path / "lock").read_text().strip() == "7"
