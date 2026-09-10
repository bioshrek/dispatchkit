"""D5: `tick` — the full scheduler pass, including the one mutating step.

Load → resolve → report → admit → dispatch. Only dispatch mutates work
assignment, and it is guarded by assignment itself: assigning the issue *is*
the lock, so the claim being tested here is the same one D3 made about `apply`
— run the pass twice and the second run must do nothing.

Everything runs against an in-memory double. No network, no `gh`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from dispatchkit.cli import main
from dispatchkit.config import SchedulerConfig
from dispatchkit.github import (
    AssignAgent,
    IssueState,
    LabelIssue,
    MarkReady,
    MergePr,
    RepoState,
)
from dispatchkit.model import Checks, Lane, PullRequest, Verify
from dispatchkit.resolve import LABEL_LOCAL_CLAIM, Status
from dispatchkit.tick import TickPlan, execute_tick, plan_tick, summarise
from tests.fake_github import FakeGitHub
from tests.items import PLAN, issue, ref, state_of

pytestmark = pytest.mark.replay

CONFIG = SchedulerConfig()

#: These fixtures carry no dispatch history, so no clock value can strand them;
#: a fixed one keeps the pass reproducible.
NOW = datetime(2026, 9, 9, 12, 0, tzinfo=UTC)


def dispatches(plan: TickPlan) -> list[AssignAgent | LabelIssue]:
    return [op for op in plan.operations if isinstance(op, AssignAgent | LabelIssue)]


class TestDispatchByLane:
    def test_a_ready_cloud_task_is_assigned_to_the_agent(self) -> None:
        plan = plan_tick(state_of(issue("a", 1)), config=CONFIG, now=NOW)
        assert dispatches(plan) == [AssignAgent(ref("a"), 1, "I_1")]

    def test_a_ready_local_task_is_only_labelled(self) -> None:
        # The scheduler cannot reach the workstation, so the label *is* the
        # dispatch; a daemon picks it up on its own schedule.
        plan = plan_tick(
            state_of(issue("a", 1, lane=Lane.LOCAL)), config=CONFIG, now=NOW
        )
        assert dispatches(plan) == [LabelIssue(ref("a"), 1, add=(LABEL_LOCAL_CLAIM,))]

    def test_a_blocked_task_is_never_dispatched(self) -> None:
        state = state_of(issue("a", 1), issue("b", 2, depends=("a",)))
        assert [
            op.ref for op in dispatches(plan_tick(state, config=CONFIG, now=NOW))
        ] == [ref("a")]

    def test_a_closed_task_is_never_touched(self) -> None:
        state = state_of(issue("a", 1, closed=True))
        assert plan_tick(state, config=CONFIG, now=NOW).operations == ()

    def test_an_already_assigned_task_is_not_reassigned(self) -> None:
        state = state_of(issue("a", 1, assignees=("copilot-swe-agent",)))
        assert dispatches(plan_tick(state, config=CONFIG, now=NOW)) == []


class TestTheReport:
    """What the pass prints, which since D14 is the whole of its output."""

    def test_every_task_is_reported_not_just_the_dispatched_ones(self) -> None:
        # A report covering only in-flight work would be silent about the thing
        # a human actually wants to know: why nothing is moving.
        state = state_of(
            issue("a", 1, closed=True),
            issue("b", 2, depends=("a",)),
            issue("c", 3, depends=("b",)),
        )
        plan = plan_tick(state, config=CONFIG, now=NOW)
        assert plan.statuses == {
            ref("a"): Status.DONE,
            ref("b"): Status.DISPATCHED,
            ref("c"): Status.BLOCKED,
        }

    def test_a_task_dispatched_this_pass_reads_as_dispatched_not_ready(self) -> None:
        # Printing `Ready` for an issue this pass just assigned would have the
        # report contradicting the repository at the moment it is read.
        plan = plan_tick(state_of(issue("a", 1)), config=CONFIG, now=NOW)
        assert plan.statuses[ref("a")] is Status.DISPATCHED

    def test_a_deferred_task_still_reads_as_ready(self) -> None:
        # Deferred means "unblocked, not started" — the report should say so,
        # otherwise a capped lane looks like a blocked plan.
        state = state_of(*(issue(f"t{i}", i) for i in range(1, 6)))
        plan = plan_tick(state, config=CONFIG, now=NOW)
        assert plan.statuses[ref("t4")] is Status.READY
        assert plan.statuses[ref("t5")] is Status.READY

    def test_the_report_is_the_only_place_a_status_goes(self) -> None:
        # D14: no operation a pass emits records a status anywhere. If one ever
        # did, it would be the stored state the whole design excludes.
        state = state_of(issue("a", 1), issue("b", 2, depends=("a",)))
        plan = plan_tick(state, config=CONFIG, now=NOW)
        assert all(isinstance(op, AssignAgent) for op in plan.operations)

    def test_the_lock_is_taken_before_anything_else_happens(self) -> None:
        # Dispatch first: a pass that dies later leaves an assigned issue, and
        # the next pass derives that unaided.
        ops = plan_tick(state_of(issue("a", 1)), config=CONFIG, now=NOW).operations
        assert isinstance(ops[0], AssignAgent)


class TestGates:
    def test_paid_work_is_not_dispatched_without_approval(self) -> None:
        state = state_of(issue("a", 1, spend=True))
        plan = plan_tick(state, config=CONFIG, now=NOW)
        assert dispatches(plan) == []
        assert plan.deferred[0].reason == "awaiting-spend-approval"

    def test_approved_paid_work_is_dispatched(self) -> None:
        state = state_of(issue("a", 1, spend=True, labels=("dispatchkit", "spend:approved")))
        assert len(dispatches(plan_tick(state, config=CONFIG, now=NOW))) == 1

    def test_the_lane_cap_bounds_how_much_is_dispatched_per_pass(self) -> None:
        state = state_of(*(issue(f"t{i}", i) for i in range(1, 6)))
        assert len(dispatches(plan_tick(state, config=CONFIG, now=NOW))) == 3

    def test_a_stuck_task_is_never_dispatched(self) -> None:
        state = state_of(issue("a", 1, labels=("dispatchkit", "dispatch:stuck")))
        assert dispatches(plan_tick(state, config=CONFIG, now=NOW)) == []


class TestIdempotency:
    def test_a_second_pass_over_the_same_repo_does_nothing(self) -> None:
        api = FakeGitHub(state=state_of(issue("a", 1), issue("b", 2, depends=("a",))))
        first = plan_tick(api.fetch_state(), config=CONFIG, now=NOW)
        execute_tick(first, api)

        second = plan_tick(api.fetch_state(), config=CONFIG, now=NOW)
        assert second.operations == ()

    def test_dispatching_removes_the_task_from_the_ready_set(self) -> None:
        api = FakeGitHub(state=state_of(issue("a", 1)))
        execute_tick(plan_tick(api.fetch_state(), config=CONFIG, now=NOW), api)
        second = plan_tick(api.fetch_state(), config=CONFIG, now=NOW)
        assert second.statuses[ref("a")] is Status.DISPATCHED
        assert second.admitted == ()

    def test_two_concurrent_passes_do_not_double_dispatch(self) -> None:
        # Both passes plan off the same snapshot; the second re-reads before
        # executing, which is what the real scheduler does every 30 minutes.
        api = FakeGitHub(state=state_of(issue("a", 1)))
        snapshot = api.fetch_state()
        execute_tick(plan_tick(snapshot, config=CONFIG, now=NOW), api)
        execute_tick(plan_tick(api.fetch_state(), config=CONFIG, now=NOW), api)
        assert api.calls.count("assign_agent(1)") == 1

    def test_a_local_claim_is_not_reapplied(self) -> None:
        api = FakeGitHub(state=state_of(issue("a", 1, lane=Lane.LOCAL)))
        execute_tick(plan_tick(api.fetch_state(), config=CONFIG, now=NOW), api)
        second = plan_tick(api.fetch_state(), config=CONFIG, now=NOW)
        assert dispatches(second) == []


class TestExecution:
    def test_execution_reports_what_it_did(self) -> None:
        api = FakeGitHub(state=state_of(issue("a", 1), issue("b", 2, lane=Lane.LOCAL)))
        result = execute_tick(
            plan_tick(api.fetch_state(), config=CONFIG, now=NOW), api
        )
        assert result.dispatched == 2

    def test_an_empty_plan_touches_nothing(self) -> None:
        api = FakeGitHub(state=RepoState(()))
        result = execute_tick(
            plan_tick(api.fetch_state(), config=CONFIG, now=NOW), api
        )
        assert result.dispatched == 0
        assert api.calls == ["fetch_state()"]


class TestDegradedInput:
    def test_an_issue_with_no_node_id_is_reported_not_half_dispatched(self) -> None:
        # Only reachable from a stale recording, but assigning needs the node
        # id, so the honest move is to say so rather than guess a lookup.
        state = state_of(issue("a", 1, node_id=""))
        plan = plan_tick(state, config=CONFIG, now=NOW)
        assert dispatches(plan) == []
        assert plan.notices[0].code == "missing-node-id"

    def test_an_issue_whose_block_is_unreadable_is_skipped_and_named(self) -> None:
        # Issue bodies are attacker-influencable and hand-editable, so one bad
        # block must cost that task and nothing else. A pass that raised here
        # would let any issue in the repository stop the scheduler.
        broken = issue("a", 1)
        state = state_of(
            IssueState(
                number=broken.number,
                title=broken.title,
                body="prose\n\n<!-- dispatchkit\nid: \n-->\n",
                labels=broken.labels,
                closed=False,
                assignees=(),
                open_prs=(),
                node_id=broken.node_id,
            ),
            issue("b", 2),
        )
        plan = plan_tick(state, config=CONFIG, now=NOW)
        assert [notice.code for notice in plan.notices] == ["unreadable-issue"]
        assert dispatches(plan) == [AssignAgent(ref("b"), 2, "I_2")]


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
                open_prs=(PullRequest(6, checks, mergeable=True),),
            )
        )

    def test_the_pass_reports_the_held_runs(self) -> None:
        plan = plan_tick(self._state(Checks.BLOCKED), config=CONFIG, now=NOW)
        assert [notice.code for notice in plan.notices] == ["ci-approval-required"]

    def test_the_report_says_in_review_rather_than_auto_merging(self) -> None:
        plan = plan_tick(self._state(Checks.BLOCKED), config=CONFIG, now=NOW)
        assert plan.statuses[ref("a")] is Status.IN_REVIEW

    def test_a_green_pr_is_reported_as_auto_merging_and_silently(self) -> None:
        plan = plan_tick(self._state(Checks.PASSING), config=CONFIG, now=NOW)
        assert plan.statuses[ref("a")] is Status.AUTO_MERGING
        assert plan.notices == ()

    def test_the_summary_shows_the_reason_to_a_human(self) -> None:
        plan = plan_tick(self._state(Checks.BLOCKED), config=CONFIG, now=NOW)
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
        plan = plan_tick(state, config=config, now=NOW)
        assert dispatches(plan) == []
        assert [deferral.reason for deferral in plan.deferred] == ["lane-cap"]


class TestWatchCommand:
    """D13: `tick` is gone as a verb and comes back as `watch --once`.

    The loop is *run once, wait, repeat*, so a terminating pass is a flag
    rather than a second command — and the convergence tests keep the
    terminating entry point they need.
    """

    def _once(self, tmp_path: Path, *extra: str) -> int:
        fixture = Path(__file__).resolve().parent / "fixtures"
        return main(
            [
                "watch",
                "--once",
                "--state",
                str(fixture / "search_issues.json"),
                "--config",
                str(tmp_path / "absent.toml"),
                *extra,
            ]
        )

    def test_a_dry_run_reports_without_a_client(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code = self._once(tmp_path)
        out = capsys.readouterr().out
        assert code == 0
        assert "dry run" in out
        assert "dispatch: demo/ports" in out

    def test_the_tick_verb_is_gone(self, capsys: pytest.CaptureFixture[str]) -> None:
        with pytest.raises(SystemExit):
            main(["tick", "--push", "--repo", "o/n"])

    def test_it_takes_no_plan_because_it_schedules_them_all(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with pytest.raises(SystemExit):
            self._once(tmp_path, "--plan", PLAN)

    def test_pushing_without_a_target_is_refused(self, capsys: pytest.CaptureFixture[str]) -> None:
        # Constructing a client by accident is exactly what must not happen.
        assert main(["watch", "--once", "--push"]) == 2
        assert "--repo" in capsys.readouterr().err

    def test_a_dry_run_without_a_snapshot_is_refused(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main(["watch", "--once"]) == 2
        assert "--state" in capsys.readouterr().err

    def test_a_recorded_snapshot_never_loops(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # A file cannot change under the loop, so looping over one would spin
        # forever printing the same pass. It runs once and says why, rather
        # than silently behaving differently from what was asked.
        fixture = Path(__file__).resolve().parent / "fixtures"
        code = main(
            [
                "watch",
                "--state",
                str(fixture / "search_issues.json"),
                "--config",
                str(tmp_path / "absent.toml"),
            ]
        )
        assert code == 0
        assert "a recorded snapshot cannot change" in capsys.readouterr().out

    def test_the_interval_is_rejected_when_it_is_not_a_wait(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # A zero interval is a busy loop against someone's API rate limit.
        assert main(["watch", "--push", "--repo", "o/n", "--interval", "0"]) == 2
        assert "--interval" in capsys.readouterr().err


class TestMarkingAutoPrsReady:
    """A pass clears the draft gate for `verify: auto`, and only then.

    The ordering matters for the same reason dispatching first does: the status
    reported reflects the operations this pass is about to perform, and a draft
    cleared now is `Auto-merging` on the next pass, not this one. Claiming it
    early would be the report asserting a merge over a pull request that was
    still a draft when we looked.
    """

    @staticmethod
    def _state(verify: Verify, checks: Checks, *, draft: bool = True) -> RepoState:
        return state_of(
            issue(
                "a",
                number=3,
                verify=verify,
                assignees=("copilot",),
                open_prs=(PullRequest(7, checks, draft=draft, mergeable=True),),
            )
        )

    def _plan(self, verify: Verify, checks: Checks, *, draft: bool = True) -> TickPlan:
        state = self._state(verify, checks, draft=draft)
        return plan_tick(state, config=SchedulerConfig(), now=NOW)

    def test_a_green_auto_draft_is_marked_ready(self) -> None:
        plan = self._plan(Verify.AUTO, Checks.PASSING)
        assert MarkReady(ref("a"), 7) in plan.operations

    def test_a_human_draft_is_left_for_its_reviewer(self) -> None:
        plan = self._plan(Verify.HUMAN, Checks.PASSING)
        assert not [op for op in plan.operations if isinstance(op, MarkReady)]

    def test_the_report_does_not_say_auto_merging_in_the_same_pass(self) -> None:
        plan = self._plan(Verify.AUTO, Checks.PASSING)
        assert plan.statuses[ref("a")] is Status.IN_REVIEW

    def test_executing_it_calls_the_port(self) -> None:
        api = FakeGitHub(state=self._state(Verify.AUTO, Checks.PASSING))
        execute_tick(self._plan(Verify.AUTO, Checks.PASSING), api)
        assert "mark_ready(7)" in api.calls

    def test_a_pass_over_a_ready_pr_plans_nothing(self) -> None:
        # Convergence: once the draft is cleared the operation must not be
        # re-emitted, or every pass would write to the same pull request.
        plan = self._plan(Verify.AUTO, Checks.PASSING, draft=False)
        assert not [op for op in plan.operations if isinstance(op, MarkReady)]
        assert plan.statuses[ref("a")] is Status.AUTO_MERGING


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
                open_prs=(PullRequest(7, Checks.PASSING, draft=True, mergeable=True),),
            )
        )

    def test_the_second_pass_plans_no_ready_op(self) -> None:
        api = FakeGitHub(state=self._state())
        first = plan_tick(api.state, config=SchedulerConfig(), now=NOW)
        assert [op for op in first.operations if isinstance(op, MarkReady)]

        execute_tick(first, api)
        second = plan_tick(api.state, config=SchedulerConfig(), now=NOW)
        assert not [op for op in second.operations if isinstance(op, MarkReady)]

    def test_the_second_pass_reaches_auto_merging(self) -> None:
        # The point of clearing the draft: the task can now actually merge,
        # and the board says so on the very next pass without further help.
        api = FakeGitHub(state=self._state())
        execute_tick(plan_tick(api.state, config=SchedulerConfig(), now=NOW), api)
        second = plan_tick(api.state, config=SchedulerConfig(), now=NOW)
        assert second.statuses[ref("a")] is Status.AUTO_MERGING

    def test_the_third_pass_is_empty(self) -> None:
        api = FakeGitHub(state=self._state())
        for _ in range(2):
            execute_tick(plan_tick(api.state, config=SchedulerConfig(), now=NOW), api)
        assert not plan_tick(api.state, config=SchedulerConfig(), now=NOW)


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
                open_prs=(PullRequest(7, Checks.PASSING, draft=True, mergeable=True),),
            )
        )
        api = FakeGitHub(state=state)
        result = execute_tick(plan_tick(state, config=SchedulerConfig(), now=NOW), api)
        assert result.readied == 1
        assert result.dispatched == 0


class TestMergingConverges:
    """D9: the pass merges a green `verify: auto` PR, and then stops.

    The convergence standard applied to the only operation that mutates the
    tree. A merge that kept being re-planned would be harmless at GitHub — the
    second call errors — but it would mean the pass never settles.
    """

    def _state(self) -> RepoState:
        return state_of(
            issue(
                "a",
                number=3,
                verify=Verify.AUTO,
                assignees=("copilot",),
                touches=("src/**",),
                open_prs=(
                    PullRequest(
                        7,
                        Checks.PASSING,
                        draft=False,
                        files=("src/app.py",),
                        mergeable=True,
                    ),
                ),
            )
        )

    def test_a_green_auto_pr_is_merged(self) -> None:
        plan = plan_tick(self._state(), config=SchedulerConfig(), now=NOW)
        assert MergePr(ref("a"), 7) in plan.operations

    def test_the_second_pass_plans_no_merge(self) -> None:
        api = FakeGitHub(state=self._state())
        first = plan_tick(api.state, config=SchedulerConfig(), now=NOW)
        result = execute_tick(first, api)
        assert result.merged == 1

        second = plan_tick(api.state, config=SchedulerConfig(), now=NOW)
        assert not [op for op in second.operations if isinstance(op, MergePr)]
        assert second.statuses[ref("a")] is Status.DONE

    def test_merging_is_not_counted_as_a_dispatch(self) -> None:
        # Nothing was handed to an agent; a pass reporting otherwise would
        # overstate what it did in the one place a human reads.
        api = FakeGitHub(state=self._state())
        result = execute_tick(
            plan_tick(api.state, config=SchedulerConfig(), now=NOW), api
        )
        assert result.dispatched == 0

    def test_a_merge_unblocks_the_dependent(self) -> None:
        state = state_of(
            issue(
                "a",
                number=3,
                verify=Verify.AUTO,
                assignees=("copilot",),
                touches=("src/**",),
                open_prs=(
                    PullRequest(
                        7,
                        Checks.PASSING,
                        draft=False,
                        files=("src/app.py",),
                        mergeable=True,
                    ),
                ),
            ),
            issue("b", number=4, depends=("a",)),
        )
        api = FakeGitHub(state=state)
        execute_tick(plan_tick(api.state, config=SchedulerConfig(), now=NOW), api)
        second = plan_tick(api.state, config=SchedulerConfig(), now=NOW)
        # Unblocked and handed out in the same pass: `b`'s dependency closed
        # because the merge closed it, so the projection reads `Dispatched`.
        assert second.admitted == (ref("b"),)


class TestARefusedMergeDoesNotEndThePass:
    """GitHub refusing a merge is a normal event, not a crash.

    Live, a conflicting pull request took the whole pass down with a traceback,
    and everything planned behind it was lost. A refusal has to be reported and
    stepped over, exactly like the CI notices are.

    What was lost then was the board writes queued after the merge. There are
    none since D14, so the invariant is stated against what remains and cannot
    go away: the *other* tasks in the same pass.
    """

    class _Refusing(FakeGitHub):
        def merge_pr(self, *, number: int) -> None:
            if number == 7:
                raise RuntimeError("gh pr failed: GraphQL: Pull Request has merge conflicts")
            super().merge_pr(number=number)

    @staticmethod
    def _mergeable(name: str, number: int, pr: int) -> IssueState:
        return issue(
            name,
            number=number,
            verify=Verify.AUTO,
            assignees=("copilot",),
            touches=("src/**",),
            open_prs=(
                PullRequest(pr, Checks.PASSING, draft=False, files=("src/app.py",), mergeable=True),
            ),
        )

    def _state(self) -> RepoState:
        return state_of(self._mergeable("a", 3, 7), self._mergeable("b", 4, 8))

    def test_the_pass_survives(self) -> None:
        api = self._Refusing(state=self._state())
        result = execute_tick(
            plan_tick(api.state, config=SchedulerConfig(), now=NOW), api
        )
        assert [notice.code for notice in result.refused] == ["merge-refused"]

    def test_the_refusal_is_reported(self) -> None:
        api = self._Refusing(state=self._state())
        result = execute_tick(
            plan_tick(api.state, config=SchedulerConfig(), now=NOW), api
        )
        assert any("conflict" in notice.message for notice in result.refused)

    def test_the_operations_behind_it_still_run(self) -> None:
        # The regression that cost a live pass: one raising operation silently
        # dropped every operation planned after it.
        api = self._Refusing(state=self._state())
        result = execute_tick(
            plan_tick(api.state, config=SchedulerConfig(), now=NOW), api
        )
        assert result.merged == 1
        assert "merge_pr(8)" in api.calls


class TestOneDispatcherEveryPlan:
    """D13: admission pools every plan in the repository.

    Admission ran over one plan's items, which quietly made both caps
    per-plan — so three active plans meant three times `caps.cloud` open pull
    requests, and a cap that has stopped doing its only job. It is worse for
    local, where the bounded resource is a machine that knows nothing about
    plans.

    Readiness stays per-plan, because a `depends` edge never crosses one.
    Only the admitted set is pooled.
    """

    def two_plans(self) -> RepoState:
        return state_of(
            issue("a", 1, plan="alpha"),
            issue("a", 2, plan="beta"),
        )

    def test_the_cloud_cap_is_counted_once_across_plans(self) -> None:
        plan = plan_tick(self.two_plans(), config=SchedulerConfig(caps={Lane.CLOUD: 1}), now=NOW)
        assert len(dispatches(plan)) == 1

    def test_the_deferral_names_the_plan_that_lost(self) -> None:
        plan = plan_tick(self.two_plans(), config=SchedulerConfig(caps={Lane.CLOUD: 1}), now=NOW)
        (deferred,) = plan.deferred
        assert deferred.ref == ref("a", "beta")
        assert deferred.reason == "lane-cap"

    def test_priority_is_first_in_first_out_by_issue_number(self) -> None:
        # Repo-global and monotonic, so it means oldest plan first, task order
        # within it, finish what you started -- and it needs no priority field
        # in the graph and no comparison rule.
        plan = plan_tick(self.two_plans(), config=SchedulerConfig(caps={Lane.CLOUD: 1}), now=NOW)
        assert plan.admitted == (ref("a", "alpha"),)

    def test_every_plans_tasks_appear_in_the_report(self) -> None:
        plan = plan_tick(self.two_plans(), config=CONFIG, now=NOW)
        assert set(plan.statuses) == {ref("a", "alpha"), ref("a", "beta")}

    def test_file_scope_exclusion_became_cross_plan_for_free(self) -> None:
        # Two plans editing the same file is exactly the collision the rule
        # exists for, and it was the one case it could not see.
        state = state_of(
            issue("a", 1, plan="alpha", touches=("src/core.py",)),
            issue("a", 2, plan="beta", touches=("src/core.py",)),
        )
        plan = plan_tick(state, config=CONFIG, now=NOW)
        assert len(dispatches(plan)) == 1
        assert plan.deferred[0].reason == "file-scope-conflict"

    def test_a_dependency_still_resolves_within_its_own_plan(self) -> None:
        # The pooling must not make one plan's closed `ports` unblock another's.
        state = state_of(
            issue("ports", 1, plan="alpha", closed=True),
            issue("ports", 2, plan="beta"),
            issue("api", 3, plan="beta", depends=("ports",)),
        )
        plan = plan_tick(state, config=CONFIG, now=NOW)
        assert plan.statuses[ref("api", "beta")] is Status.BLOCKED


class TestTheLoop:
    """D13: `watch` is *run a pass, wait, repeat*, in a terminal.

    The loop is the whole of what replaced the cron, so the two things it has
    to get right are that it comes back round, and that stopping it is not a
    failure. `time.sleep` is the seam — no test here waits for real time.
    """

    def _fake_clock(self, monkeypatch: pytest.MonkeyPatch, stop_after: int) -> list[int]:
        waits: list[int] = []

        def sleep(seconds: int) -> None:
            waits.append(seconds)
            if len(waits) >= stop_after:
                raise KeyboardInterrupt
        monkeypatch.setattr("dispatchkit.cli.time.sleep", sleep)
        return waits

    def _api(self, monkeypatch: pytest.MonkeyPatch) -> FakeGitHub:
        api = FakeGitHub(state=state_of(issue("a", 1)))
        monkeypatch.setattr("dispatchkit.cli.GhCli", lambda **_: api)
        return api

    def test_it_runs_a_pass_again_after_waiting(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        api = self._api(monkeypatch)
        self._fake_clock(monkeypatch, stop_after=3)

        main(["watch", "--push", "--repo", "o/n", "--config", str(tmp_path / "absent.toml")])

        # A pass, a wait, a pass, a wait, a pass — stopped in the third wait.
        assert api.calls.count("fetch_state()") == 3

    def test_it_waits_the_interval_it_was_given(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._api(monkeypatch)
        waits = self._fake_clock(monkeypatch, stop_after=2)

        main(
            [
                "watch",
                "--push",
                "--repo",
                "o/n",
                "--interval",
                "5",
                "--config",
                str(tmp_path / "absent.toml"),
            ]
        )
        assert waits == [5, 5]

    def test_stopping_it_is_not_a_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Stopping the scheduler is how you stop the scheduler. A traceback
        # would report the ordinary way of using it as a crash.
        self._api(monkeypatch)
        self._fake_clock(monkeypatch, stop_after=1)

        code = main(["watch", "--push", "--repo", "o/n", "--config", str(tmp_path / "absent.toml")])
        assert code == 0
        assert "stopped" in capsys.readouterr().out

    def test_an_interrupt_during_a_pass_is_also_clean(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Ctrl-C is far likelier to land in the seconds a pass is talking to
        # GitHub than in the seconds it is asleep.
        api = FakeGitHub(state=state_of(issue("a", 1)))

        def interrupt() -> RepoState:
            raise KeyboardInterrupt
        monkeypatch.setattr(api, "fetch_state", interrupt)
        monkeypatch.setattr("dispatchkit.cli.GhCli", lambda **_: api)

        code = main(["watch", "--push", "--repo", "o/n", "--config", str(tmp_path / "absent.toml")])
        assert code == 0
        assert "stopped" in capsys.readouterr().out

    def test_a_restart_mid_loop_changes_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The convergence standard's second case, which D13 adds.

        A long-running process wants to remember what it saw, and the moment it
        does, its memory and the issues can disagree. So: kill it mid-loop,
        start it again, and the pass that follows must be the one that would
        have followed anyway.
        """
        api = FakeGitHub(state=state_of(issue("a", 1), issue("b", 2)))
        monkeypatch.setattr("dispatchkit.cli.GhCli", lambda **_: api)
        config = str(tmp_path / "absent.toml")

        self._fake_clock(monkeypatch, stop_after=1)
        main(["watch", "--push", "--repo", "o/n", "--config", config])
        killed = plan_tick(api.fetch_state(), config=CONFIG, now=NOW)

        # A fresh process, holding nothing from the last one.
        self._fake_clock(monkeypatch, stop_after=1)
        main(["watch", "--push", "--repo", "o/n", "--config", config])
        restarted = plan_tick(api.fetch_state(), config=CONFIG, now=NOW)

        assert killed.operations == restarted.operations == ()
