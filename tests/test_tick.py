"""D5: `tick` — the full scheduler pass, including the one mutating step.

Load → resolve → reconcile → admit → dispatch. Only dispatch mutates work
assignment, and it is guarded by assignment itself: assigning the issue *is*
the lock, so the claim being tested here is the same one D3 made about `apply`
— run the pass twice and the second run must do nothing.

Everything runs against an in-memory double. No network, no `gh`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from dispatchkit.cli import main
from dispatchkit.config import SchedulerConfig
from dispatchkit.github import (
    AssignAgent,
    LabelIssue,
    MarkReady,
    RepoState,
    SetProjectField,
)
from dispatchkit.model import Checks, Lane, PullRequest, TaskId, Verify
from dispatchkit.resolve import LABEL_LOCAL_CLAIM, Status
from dispatchkit.tick import TickPlan, execute_tick, plan_tick, summarise
from tests.fake_github import FakeGitHub
from tests.items import PLAN, issue, state_of

pytestmark = pytest.mark.replay

CONFIG = SchedulerConfig()


def dispatches(plan: TickPlan) -> list[AssignAgent | LabelIssue]:
    return [op for op in plan.operations if isinstance(op, AssignAgent | LabelIssue)]


class TestDispatchByLane:
    def test_a_ready_cloud_task_is_assigned_to_the_agent(self) -> None:
        plan = plan_tick(state_of(issue("a", 1)), plan=PLAN, config=CONFIG)
        assert dispatches(plan) == [AssignAgent(TaskId("a"), 1, "I_1")]

    def test_a_ready_local_task_is_only_labelled(self) -> None:
        # The scheduler cannot reach the workstation, so the label *is* the
        # dispatch; a daemon picks it up on its own schedule.
        plan = plan_tick(state_of(issue("a", 1, lane=Lane.LOCAL)), plan=PLAN, config=CONFIG)
        assert dispatches(plan) == [LabelIssue(TaskId("a"), 1, add=(LABEL_LOCAL_CLAIM,))]

    def test_a_blocked_task_is_never_dispatched(self) -> None:
        state = state_of(issue("a", 1), issue("b", 2, depends=("a",)))
        assert [op.task_id for op in dispatches(plan_tick(state, plan=PLAN, config=CONFIG))] == [
            TaskId("a")
        ]

    def test_a_closed_task_is_never_touched(self) -> None:
        state = state_of(issue("a", 1, closed=True, fields={"Status": "Done"}))
        assert plan_tick(state, plan=PLAN, config=CONFIG).operations == ()

    def test_an_already_assigned_task_is_not_reassigned(self) -> None:
        state = state_of(issue("a", 1, assignees=("copilot-swe-agent",)))
        assert dispatches(plan_tick(state, plan=PLAN, config=CONFIG)) == []


class TestBoardReconciliation:
    def test_status_is_written_for_every_item_not_just_dispatched_ones(self) -> None:
        # A board that only tracked in-flight work would be silent about the
        # thing a human actually wants to know: why nothing is moving.
        state = state_of(
            issue("a", 1, closed=True, fields={"Status": "Ready"}),
            issue("b", 2, depends=("a",), fields={"Status": "Done"}),
            issue("c", 3, depends=("b",), fields={"Status": "Ready"}),
        )
        writes = [
            op
            for op in plan_tick(state, plan=PLAN, config=CONFIG).operations
            if isinstance(op, SetProjectField)
        ]
        assert [(op.task_id, op.value) for op in writes] == [
            (TaskId("a"), "Done"),
            (TaskId("b"), "Dispatched"),
            (TaskId("c"), "Blocked"),
        ]

    def test_a_task_dispatched_this_pass_is_recorded_as_dispatched_not_ready(self) -> None:
        # Writing `Ready` for an issue we just assigned would leave the board
        # contradicting the issue for half an hour, until the next pass.
        plan = plan_tick(state_of(issue("a", 1)), plan=PLAN, config=CONFIG)
        writes = [op for op in plan.operations if isinstance(op, SetProjectField)]
        assert writes == [SetProjectField(TaskId("a"), "Status", "Dispatched")]

    def test_a_deferred_task_stays_ready_on_the_board(self) -> None:
        # Deferred means "unblocked, not started" — the board should say so,
        # otherwise a capped lane looks like a blocked plan.
        state = state_of(*(issue(f"t{i}", i) for i in range(1, 6)))
        plan = plan_tick(state, plan=PLAN, config=CONFIG)
        values = {op.task_id: op.value for op in plan.operations if isinstance(op, SetProjectField)}
        assert values[TaskId("t4")] == "Ready"
        assert values[TaskId("t5")] == "Ready"

    def test_the_lock_is_taken_before_the_board_is_updated(self) -> None:
        # If the pass dies between the two, an assigned issue with a stale
        # board entry self-heals next pass; the reverse would double-dispatch.
        ops = plan_tick(state_of(issue("a", 1)), plan=PLAN, config=CONFIG).operations
        assert isinstance(ops[0], AssignAgent)


class TestGates:
    def test_paid_work_is_not_dispatched_without_approval(self) -> None:
        state = state_of(issue("a", 1, spend=True))
        plan = plan_tick(state, plan=PLAN, config=CONFIG)
        assert dispatches(plan) == []
        assert plan.deferred[0].reason == "awaiting-spend-approval"

    def test_approved_paid_work_is_dispatched(self) -> None:
        state = state_of(issue("a", 1, spend=True, labels=("dispatchkit", "spend:approved")))
        assert len(dispatches(plan_tick(state, plan=PLAN, config=CONFIG))) == 1

    def test_the_lane_cap_bounds_how_much_is_dispatched_per_pass(self) -> None:
        state = state_of(*(issue(f"t{i}", i) for i in range(1, 6)))
        assert len(dispatches(plan_tick(state, plan=PLAN, config=CONFIG))) == 3

    def test_a_stuck_task_is_never_dispatched(self) -> None:
        state = state_of(issue("a", 1, labels=("dispatchkit", "dispatch:stuck")))
        assert dispatches(plan_tick(state, plan=PLAN, config=CONFIG)) == []


class TestIdempotency:
    def test_a_second_pass_over_the_same_repo_does_nothing(self) -> None:
        api = FakeGitHub(state=state_of(issue("a", 1), issue("b", 2, depends=("a",))))
        first = plan_tick(api.fetch_state(plan=PLAN), plan=PLAN, config=CONFIG)
        execute_tick(first, api)

        second = plan_tick(api.fetch_state(plan=PLAN), plan=PLAN, config=CONFIG)
        assert second.operations == ()

    def test_dispatching_removes_the_task_from_the_ready_set(self) -> None:
        api = FakeGitHub(state=state_of(issue("a", 1)))
        execute_tick(plan_tick(api.fetch_state(plan=PLAN), plan=PLAN, config=CONFIG), api)
        second = plan_tick(api.fetch_state(plan=PLAN), plan=PLAN, config=CONFIG)
        assert second.statuses[TaskId("a")] is Status.DISPATCHED
        assert second.admitted == ()

    def test_two_concurrent_passes_do_not_double_dispatch(self) -> None:
        # Both passes plan off the same snapshot; the second re-reads before
        # executing, which is what the real scheduler does every 30 minutes.
        api = FakeGitHub(state=state_of(issue("a", 1)))
        snapshot = api.fetch_state(plan=PLAN)
        execute_tick(plan_tick(snapshot, plan=PLAN, config=CONFIG), api)
        execute_tick(plan_tick(api.fetch_state(plan=PLAN), plan=PLAN, config=CONFIG), api)
        assert api.calls.count("assign_agent(1)") == 1

    def test_a_local_claim_is_not_reapplied(self) -> None:
        api = FakeGitHub(state=state_of(issue("a", 1, lane=Lane.LOCAL)))
        execute_tick(plan_tick(api.fetch_state(plan=PLAN), plan=PLAN, config=CONFIG), api)
        second = plan_tick(api.fetch_state(plan=PLAN), plan=PLAN, config=CONFIG)
        assert dispatches(second) == []


class TestExecution:
    def test_execution_reports_what_it_did(self) -> None:
        api = FakeGitHub(state=state_of(issue("a", 1), issue("b", 2, lane=Lane.LOCAL)))
        result = execute_tick(plan_tick(api.fetch_state(plan=PLAN), plan=PLAN, config=CONFIG), api)
        assert (result.dispatched, result.reconciled) == (2, 2)

    def test_an_empty_plan_touches_nothing(self) -> None:
        api = FakeGitHub(state=RepoState(()))
        result = execute_tick(plan_tick(api.fetch_state(plan=PLAN), plan=PLAN, config=CONFIG), api)
        assert (result.dispatched, result.reconciled) == (0, 0)
        assert api.calls == ["fetch_state(demo)"]


class TestDegradedInput:
    def test_an_issue_with_no_node_id_is_reported_not_half_dispatched(self) -> None:
        # Only reachable from a stale recording, but assigning needs the node
        # id, so the honest move is to say so rather than guess a lookup.
        state = state_of(issue("a", 1, node_id=""))
        plan = plan_tick(state, plan=PLAN, config=CONFIG)
        assert dispatches(plan) == []
        assert plan.notices[0].code == "missing-node-id"

    def test_issues_from_another_plan_are_ignored(self) -> None:
        state = state_of(issue("a", 1))
        assert plan_tick(state, plan="other", config=CONFIG).operations == ()


class TestStalledCi:
    """A pass must surface a `verify: auto` task whose CI cannot start.

    This is the failure the sandbox actually hit: the coding agent opened its
    PRs, GitHub parked the workflow runs pending approval, and nothing said so.
    """

    @staticmethod
    def _state(checks: Checks) -> RepoState:
        return state_of(
            issue(
                "a",
                1,
                verify=Verify.AUTO,
                assignees=("copilot-swe-agent",),
                open_prs=(PullRequest(6, checks),),
            )
        )

    def test_the_pass_reports_the_held_runs(self) -> None:
        plan = plan_tick(self._state(Checks.BLOCKED), plan=PLAN, config=CONFIG)
        assert [notice.code for notice in plan.notices] == ["ci-approval-required"]

    def test_the_board_is_told_in_review_rather_than_auto_merging(self) -> None:
        plan = plan_tick(self._state(Checks.BLOCKED), plan=PLAN, config=CONFIG)
        assert plan.statuses[TaskId("a")] is Status.IN_REVIEW

    def test_a_green_pr_is_reported_as_auto_merging_and_silently(self) -> None:
        plan = plan_tick(self._state(Checks.PASSING), plan=PLAN, config=CONFIG)
        assert plan.statuses[TaskId("a")] is Status.AUTO_MERGING
        assert plan.notices == ()

    def test_the_summary_shows_the_reason_to_a_human(self) -> None:
        plan = plan_tick(self._state(Checks.BLOCKED), plan=PLAN, config=CONFIG)
        assert any("ci-approval-required" in line for line in summarise(plan))

    def test_a_stalled_task_still_occupies_its_lane(self) -> None:
        # It is genuinely in flight — an agent did the work and a PR is open.
        # Freeing the slot would let the scheduler pile more work on a repo
        # that already has a PR nobody can merge.
        state = state_of(
            issue("a", 1, verify=Verify.AUTO, open_prs=(PullRequest(6, Checks.BLOCKED),)),
            issue("b", 2),
        )
        config = SchedulerConfig(caps={Lane.CLOUD: 1})
        plan = plan_tick(state, plan=PLAN, config=config)
        assert dispatches(plan) == []
        assert [deferral.reason for deferral in plan.deferred] == ["lane-cap"]


class TestTickCommand:
    def test_a_dry_run_reports_without_a_client(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        fixture = Path(__file__).resolve().parent / "fixtures"
        code = main(
            [
                "tick",
                "--plan",
                PLAN,
                "--state",
                str(fixture / "search_issues.json"),
                "--config",
                str(tmp_path / "absent.toml"),
            ]
        )
        out = capsys.readouterr().out
        assert code == 0
        assert "dry run" in out
        assert "dispatch: ports" in out

    def test_pushing_without_a_target_is_refused(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Constructing a client by accident is exactly what must not happen.
        assert main(["tick", "--plan", PLAN, "--push"]) == 2
        assert "--repo" in capsys.readouterr().err

    def test_a_dry_run_without_a_snapshot_is_refused(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main(["tick", "--plan", PLAN]) == 2
        assert "--state" in capsys.readouterr().err


class TestMarkingAutoPrsReady:
    """A pass clears the draft gate for `verify: auto`, and only then.

    The ordering matters for the same reason dispatch-before-record does: the
    status the board is told reflects the operations this pass is about to
    perform, and a draft cleared now is `Auto-merging` on the next pass, not
    this one. Claiming it early would be the board asserting a merge over a
    pull request that was still a draft when we looked.
    """

    @staticmethod
    def _state(verify: Verify, checks: Checks, *, draft: bool = True) -> RepoState:
        return state_of(
            issue(
                "a",
                number=3,
                verify=verify,
                assignees=("copilot",),
                open_prs=(PullRequest(7, checks, draft=draft),),
            )
        )

    def _plan(self, verify: Verify, checks: Checks, *, draft: bool = True) -> TickPlan:
        state = self._state(verify, checks, draft=draft)
        return plan_tick(state, plan=PLAN, config=SchedulerConfig())

    def test_a_green_auto_draft_is_marked_ready(self) -> None:
        plan = self._plan(Verify.AUTO, Checks.PASSING)
        assert MarkReady(TaskId("a"), 7) in plan.operations

    def test_a_human_draft_is_left_for_its_reviewer(self) -> None:
        plan = self._plan(Verify.HUMAN, Checks.PASSING)
        assert not [op for op in plan.operations if isinstance(op, MarkReady)]

    def test_the_board_is_not_told_auto_merging_in_the_same_pass(self) -> None:
        plan = self._plan(Verify.AUTO, Checks.PASSING)
        assert plan.statuses[TaskId("a")] is Status.IN_REVIEW

    def test_executing_it_calls_the_port(self) -> None:
        api = FakeGitHub(state=self._state(Verify.AUTO, Checks.PASSING))
        execute_tick(self._plan(Verify.AUTO, Checks.PASSING), api)
        assert "mark_ready(7)" in api.calls

    def test_a_pass_over_a_ready_pr_plans_nothing(self) -> None:
        # Convergence: once the draft is cleared the operation must not be
        # re-emitted, or every pass would write to the same pull request.
        plan = self._plan(Verify.AUTO, Checks.PASSING, draft=False)
        assert not [op for op in plan.operations if isinstance(op, MarkReady)]
        assert plan.statuses[TaskId("a")] is Status.AUTO_MERGING


class TestMarkReadyConverges:
    """The standard for any mutating behaviour: plan, apply, re-read, re-plan.

    A second pass must plan nothing. `gh pr ready` on a PR that is already out
    of draft is harmless, but a pass that keeps emitting it would be writing to
    GitHub on every cron tick forever, which is how rate limits and confusing
    audit trails are made.
    """

    def _state(self) -> RepoState:
        return state_of(
            issue(
                "a",
                number=3,
                verify=Verify.AUTO,
                assignees=("copilot",),
                open_prs=(PullRequest(7, Checks.PASSING, draft=True),),
            )
        )

    def test_the_second_pass_plans_no_ready_op(self) -> None:
        api = FakeGitHub(state=self._state())
        first = plan_tick(api.state, plan=PLAN, config=SchedulerConfig())
        assert [op for op in first.operations if isinstance(op, MarkReady)]

        execute_tick(first, api)
        second = plan_tick(api.state, plan=PLAN, config=SchedulerConfig())
        assert not [op for op in second.operations if isinstance(op, MarkReady)]

    def test_the_second_pass_reaches_auto_merging(self) -> None:
        # The point of clearing the draft: the task can now actually merge,
        # and the board says so on the very next pass without further help.
        api = FakeGitHub(state=self._state())
        execute_tick(plan_tick(api.state, plan=PLAN, config=SchedulerConfig()), api)
        second = plan_tick(api.state, plan=PLAN, config=SchedulerConfig())
        assert second.statuses[TaskId("a")] is Status.AUTO_MERGING

    def test_the_third_pass_is_empty(self) -> None:
        api = FakeGitHub(state=self._state())
        for _ in range(2):
            execute_tick(plan_tick(api.state, plan=PLAN, config=SchedulerConfig()), api)
        assert not plan_tick(api.state, plan=PLAN, config=SchedulerConfig())


class TestReadyIsNotCountedAsDispatch:
    """`dispatched` means work handed to an agent, and this hands out nothing.

    Reporting it there would tell the log a task had been started when in fact
    an existing pull request was merely un-drafted, and the count is the one
    number a human skims in the workflow output.
    """

    def test_marking_ready_is_reported_separately(self) -> None:
        state = state_of(
            issue(
                "a",
                number=3,
                verify=Verify.AUTO,
                assignees=("copilot",),
                open_prs=(PullRequest(7, Checks.PASSING, draft=True),),
            )
        )
        api = FakeGitHub(state=state)
        result = execute_tick(plan_tick(state, plan=PLAN, config=SchedulerConfig()), api)
        assert result.readied == 1
        assert result.dispatched == 0
