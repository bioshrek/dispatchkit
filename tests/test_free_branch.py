"""D6.6: a kept branch must not block the retry for ever.

Found live, one failure after the last. A local run that fails keeps its
branch — deliberately, since that branch is evidence a human may want to read
and nothing in the system reads it back. The retry then asked for a worktree on
a branch of the same name and git refused:

    fatal: a branch named 'dispatchkit/wordfreq/version-flag/2' already exists

So the task fails at `worktree` before the agent is ever started, every time,
until the retry budget marks it `dispatch:stuck`. Two good decisions — keep the
evidence, and put the attempt in the name so a retry cannot force-push over it
— combine into a task that can never make progress again.

The name repeats because `attempts` is derived from the issue's timeline, read
at the *start* of a pass and therefore before that pass writes its own mark.
Two consecutive passes can legitimately compute the same number, and the design
explicitly permits concurrent passes, so uniqueness was never something the
attempt count could promise.

The docstring on `branch_name` says the attempt is in the name so a retry
cannot overwrite the previous branch. That purpose is *uniqueness*, not
accounting — so the fix is to keep asking for the next number until the name is
free. The old branches stay exactly where they are.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from dispatchkit.config import SchedulerConfig
from dispatchkit.local import LocalRun, run_local
from dispatchkit.model import Lane, TaskId, TaskRef
from dispatchkit.resolve import build_items
from dispatchkit.workstation import branch_name, free_branch
from tests.fake_github import FakeGitHub
from tests.fake_workstation import FakeWorkstation
from tests.items import issue, state_of

pytestmark = pytest.mark.unit

REF = TaskRef(plan="wordfreq", id=TaskId("version-flag"))
NOW = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)


def _items(api: FakeGitHub):  # type: ignore[no-untyped-def]
    resolved, _ = build_items(api.fetch_state())
    return resolved


class TestPickingAFreeName:
    def test_the_attempt_is_used_when_nothing_is_in_the_way(self) -> None:
        assert free_branch(REF, 2, taken=()) == branch_name(REF, 2)

    def test_it_steps_over_a_kept_branch(self) -> None:
        # The exact live failure: attempt 2 computed twice, the first one's
        # branch still on disk.
        taken = (branch_name(REF, 2),)
        assert free_branch(REF, 2, taken=taken) == branch_name(REF, 3)

    def test_it_steps_over_a_run_of_them(self) -> None:
        taken = tuple(branch_name(REF, n) for n in (1, 2, 3, 4))
        assert free_branch(REF, 2, taken=taken) == branch_name(REF, 5)

    def test_another_tasks_branches_do_not_count(self) -> None:
        other = TaskRef(plan="wordfreq", id=TaskId("top-n"))
        assert free_branch(REF, 1, taken=(branch_name(other, 1),)) == branch_name(REF, 1)

    def test_another_plans_branches_do_not_count(self) -> None:
        other = TaskRef(plan="other", id=TaskId("version-flag"))
        assert free_branch(REF, 1, taken=(branch_name(other, 1),)) == branch_name(REF, 1)

    def test_the_evidence_is_never_reused(self) -> None:
        # The whole point of keeping the branch: the name it picks is not one
        # of the ones already there.
        taken = tuple(branch_name(REF, n) for n in range(1, 6))
        assert free_branch(REF, 1, taken=taken) not in taken

    def test_it_is_pure(self) -> None:
        # A planner decision, not an adapter one: given the same inputs it is
        # the same name, and it touches nothing.
        taken = (branch_name(REF, 2),)
        assert free_branch(REF, 2, taken=taken) == free_branch(REF, 2, taken=taken)


class TestTheExecutorUsesIt:
    """The live deadlock, end to end."""

    def _run(self, branches: tuple[str, ...]) -> LocalRun:
        api = FakeGitHub(state=state_of(issue("version-flag", number=1, lane=Lane.LOCAL)))
        machine = FakeWorkstation(branches_=branches)
        task = next(t for t in _items(api) if t.ref.id == TaskId("version-flag"))
        return run_local(
            task,
            api=api,
            machine=machine,
            config=SchedulerConfig(),
            root=Path("/tmp/dispatchkit-test"),
            now=NOW,
        )

    def test_it_does_not_die_on_a_kept_branch(self) -> None:
        ref = TaskRef(plan="demo", id=TaskId("version-flag"))
        run = self._run((branch_name(ref, 1),))

        # The bug: this failed at `worktree` before the agent ever started.
        assert run.stage != "worktree", run.detail

    def test_it_names_a_branch_that_was_free(self) -> None:
        ref = TaskRef(plan="demo", id=TaskId("version-flag"))
        run = self._run((branch_name(ref, 1),))

        assert run.branch != branch_name(ref, 1)
