"""D7: a dispatch that never produced a pull request.

The retry budget existed before any of this did, and it was enforced against a
counter nothing ever wrote, so `attempts` was always zero and the budget never
fired. These tests fix the counter first and the timeout second.

`attempts` is derived from the issue's own assignment history rather than
stored on the board, because the board is a derived view and storing it there
would make it the only record of how many times work had been handed out.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from dispatchkit.config import SchedulerConfig
from dispatchkit.github import LabelIssue, UnassignAgent
from dispatchkit.model import Checks, PullRequest, TaskId
from dispatchkit.resolve import LABEL_STUCK, Status, resolve, stall_ops
from dispatchkit.tick import execute_tick, plan_tick
from tests.fake_github import FakeGitHub
from tests.items import PLAN, issue, item, items_of, state_of

pytestmark = pytest.mark.unit

NOW = datetime(2026, 9, 9, 12, 0, tzinfo=UTC)
LONG_AGO = NOW - timedelta(hours=30)
RECENT = NOW - timedelta(hours=2)

CONFIG = SchedulerConfig()
AGENT = ("copilot-swe-agent",)


class TestAttemptsAreDerived:
    def test_one_dispatch_is_one_attempt(self) -> None:
        task = item("a", assignees=AGENT, dispatches=(LONG_AGO,))
        assert task.attempts == 1

    def test_a_human_co_assignee_does_not_inflate_the_count(self) -> None:
        """GitHub records two AssignedEvents per dispatch: the bot and the user.

        Confirmed live on the sandbox. Only the agent's own assignment counts,
        or every attempt would be worth two against the budget.
        """
        task = item("a", assignees=("copilot-swe-agent", "bioshrek"), dispatches=(LONG_AGO,))
        assert task.attempts == 1

    def test_a_task_never_dispatched_has_no_attempts(self) -> None:
        assert item("a").attempts == 0


class TestTheStallItself:
    def test_a_dispatch_with_no_pull_request_is_reclaimed(self) -> None:
        task = item("a", number=7, assignees=AGENT, dispatches=(LONG_AGO,))
        assert stall_ops(items_of(task), CONFIG, NOW) == (UnassignAgent(TaskId("a"), 7, AGENT),)

    def test_a_recent_dispatch_is_given_more_time(self) -> None:
        task = item("a", assignees=AGENT, dispatches=(RECENT,))
        assert stall_ops(items_of(task), CONFIG, NOW) == ()

    def test_an_open_pull_request_means_the_agent_is_working(self) -> None:
        """The timeout asks whether anything was produced, not whether it is done.

        A long review is not a stall, and reclaiming here would throw away work.
        """
        task = item(
            "a",
            assignees=AGENT,
            dispatches=(LONG_AGO,),
            open_prs=(PullRequest(number=3, checks=Checks.PENDING),),
        )
        assert stall_ops(items_of(task), CONFIG, NOW) == ()

    def test_an_unassigned_task_cannot_stall(self) -> None:
        assert stall_ops(items_of(item("a", dispatches=(LONG_AGO,))), CONFIG, NOW) == ()

    def test_a_closed_task_is_left_alone(self) -> None:
        task = item("a", closed=True, assignees=AGENT, dispatches=(LONG_AGO,))
        assert stall_ops(items_of(task), CONFIG, NOW) == ()


class TestTheBudget:
    def test_the_last_permitted_attempt_is_marked_stuck(self) -> None:
        """Reclaiming the third attempt would hand out a fourth, so it stops here."""
        task = item("a", number=7, assignees=AGENT, dispatches=(LONG_AGO,) * 3)
        assert stall_ops(items_of(task), CONFIG, NOW) == (
            UnassignAgent(TaskId("a"), 7, AGENT),
            LabelIssue(TaskId("a"), 7, add=(LABEL_STUCK,)),
        )

    def test_a_stuck_task_is_not_reclaimed_again(self) -> None:
        task = item(
            "a",
            assignees=AGENT,
            dispatches=(LONG_AGO,) * 3,
            labels=("dispatchkit", LABEL_STUCK),
        )
        assert stall_ops(items_of(task), CONFIG, NOW) == ()

    def test_the_budget_is_configurable(self) -> None:
        task = item("a", number=7, assignees=AGENT, dispatches=(LONG_AGO,))
        ops = stall_ops(items_of(task), SchedulerConfig(retry_budget=1), NOW)
        assert LabelIssue(TaskId("a"), 7, add=(LABEL_STUCK,)) in ops


class TestTheBoardTellsTheTruthAboutIt:
    def test_a_stuck_task_does_not_read_as_dispatched(self) -> None:
        task = item(
            "a",
            assignees=AGENT,
            dispatches=(LONG_AGO,) * 3,
            labels=("dispatchkit", LABEL_STUCK),
        )
        rows = resolve(items_of(task))
        assert rows[TaskId("a")] is Status.STUCK


class TestReclaimConverges:
    """The standard for anything that mutates: apply, re-read, re-plan.

    The reclaim is unusual in that the second plan is *not* empty — releasing
    the lock is supposed to hand the task out again. What must converge is the
    budget: each cycle has to cost exactly one attempt, or a task could be
    retried forever.
    """

    def _api(self) -> FakeGitHub:
        api = FakeGitHub(state=state_of(issue("a", 1)))
        api.clock = NOW - timedelta(hours=30)
        return api

    def _pass(self, api: FakeGitHub) -> None:
        execute_tick(plan_tick(api.state, plan=PLAN, config=CONFIG, now=NOW), api)

    def test_a_dispatch_that_stalls_is_handed_out_again(self) -> None:
        api = self._api()
        self._pass(api)
        assert api.state.issues[0].assignees == ("copilot-swe-agent",)
        self._pass(api)  # the stall is noticed and the lock released
        assert api.state.issues[0].assignees == ()  # type: ignore[comparison-overlap]
        self._pass(api)
        assert api.state.issues[0].assignees == ("copilot-swe-agent",)

    def test_each_cycle_costs_exactly_one_attempt(self) -> None:
        api = self._api()
        for _ in range(4):
            self._pass(api)
        assert len(api.state.issues[0].dispatches) == 2

    def test_the_budget_eventually_stops_it(self) -> None:
        api = self._api()
        for _ in range(12):
            self._pass(api)
        issue_state = api.state.issues[0]
        assert LABEL_STUCK in issue_state.labels
        assert len(issue_state.dispatches) <= CONFIG.retry_budget

    def test_a_stuck_task_settles(self) -> None:
        api = self._api()
        for _ in range(12):
            self._pass(api)
        before = api.state
        self._pass(api)
        assert api.state == before
