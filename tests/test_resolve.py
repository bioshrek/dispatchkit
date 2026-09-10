"""D4: readiness resolution and admission as pure functions.

Everything here is a function of the issue snapshot alone — no network, no
local state, no graph file. That is the design claim being tested: `Status` is
derived rather than stored, so there is nothing anywhere that could disagree
with the issues, and a status is only ever computed and printed.

The rule under test, verbatim from the design: *a task is ready iff it is open,
unassigned, and every id in `depends` maps to a closed issue.*
"""

from __future__ import annotations

from dispatchkit.config import SchedulerConfig
from dispatchkit.github import MarkReady, MergePr
from dispatchkit.model import Checks, Lane, PullRequest, Verify
from dispatchkit.resolve import (
    Status,
    TaskItem,
    admit,
    ci_notices,
    merge_ops,
    ready_ops,
    resolve,
)
from tests.items import item, items_of, ref


class TestReadiness:
    def test_a_task_with_no_dependencies_is_ready(self) -> None:
        assert resolve(items_of(item("a")))[ref("a")] is Status.READY

    def test_a_task_waiting_on_an_open_issue_is_blocked(self) -> None:
        statuses = resolve(items_of(item("a"), item("b", depends=("a",))))
        assert statuses[ref("b")] is Status.BLOCKED

    def test_a_task_becomes_ready_when_its_dependency_closes(self) -> None:
        statuses = resolve(items_of(item("a", closed=True), item("b", depends=("a",))))
        assert statuses[ref("a")] is Status.DONE
        assert statuses[ref("b")] is Status.READY

    def test_an_assigned_task_is_dispatched_not_ready(self) -> None:
        # Assignment *is* the dispatch lock, so an assigned task is never in
        # the ready set — two concurrent passes therefore converge.
        assert resolve(items_of(item("a", assignees=("copilot",))))[ref("a")] is (
            Status.DISPATCHED
        )

    def test_a_local_lane_claim_label_also_counts_as_dispatched(self) -> None:
        claimed = item("a", lane=Lane.LOCAL, labels=("dispatchkit", "dispatch:local"))
        assert resolve(items_of(claimed))[ref("a")] is Status.DISPATCHED

    def test_a_closed_task_is_done_even_if_it_was_never_assigned(self) -> None:
        assert resolve(items_of(item("a", closed=True)))[ref("a")] is Status.DONE

    def test_a_dependency_on_a_deleted_issue_leaves_the_task_blocked(self) -> None:
        # "Dependency issue deleted -> dependents stay Blocked."
        statuses = resolve(items_of(item("b", depends=("ghost",))))
        assert statuses[ref("b")] is Status.BLOCKED

    def test_every_dependency_must_be_closed_not_just_one(self) -> None:
        statuses = resolve(
            items_of(
                item("a", closed=True),
                item("b"),
                item("c", depends=("a", "b")),
            )
        )
        assert statuses[ref("c")] is Status.BLOCKED


class TestWorkInFlight:
    def test_an_open_pr_on_a_human_verify_task_is_in_review(self) -> None:
        working = item("a", assignees=("copilot",), open_prs=(7,), verify=Verify.HUMAN)
        assert resolve(items_of(working))[ref("a")] is Status.IN_REVIEW

    def test_an_open_pr_on_an_auto_verify_task_is_auto_merging(self) -> None:
        working = item("a", assignees=("copilot",), open_prs=(7,), verify=Verify.AUTO)
        assert resolve(items_of(working))[ref("a")] is Status.AUTO_MERGING

    def test_an_unassigned_task_with_a_pr_still_counts_as_work_in_flight(self) -> None:
        # The local daemon works from a label rather than an assignee, so a PR
        # is the more reliable signal that something is already happening.
        assert resolve(items_of(item("a", open_prs=(7,))))[ref("a")] is Status.IN_REVIEW


class TestAutoMergeRequiresLiveCi:
    """`Auto-merging` is a claim about CI, so CI has to be in a position to act.

    The bug this class exists to prevent: the resolver used to return
    `Auto-merging` for any `verify: auto` task with an open PR, without ever
    looking at a check. A coding agent's PR has its workflow runs held for
    human approval, so the board would sit on `Auto-merging` forever for a
    pipeline that had not started and never would. Saying nothing is better
    than saying something false; `In Review` is true in every one of these
    cases, because a human is indeed the next mover.
    """

    @staticmethod
    def _auto(checks: Checks) -> Status:
        working = item(
            "a",
            assignees=("copilot",),
            open_prs=(PullRequest(7, checks, mergeable=True),),
            verify=Verify.AUTO,
        )
        return resolve(items_of(working))[ref("a")]

    def test_a_run_held_for_approval_is_not_auto_merging(self) -> None:
        assert self._auto(Checks.BLOCKED) is Status.IN_REVIEW

    def test_a_failing_pr_is_not_auto_merging(self) -> None:
        assert self._auto(Checks.FAILING) is Status.IN_REVIEW

    def test_a_green_pr_is_auto_merging(self) -> None:
        assert self._auto(Checks.PASSING) is Status.AUTO_MERGING

    def test_a_pr_whose_ci_is_still_running_is_auto_merging(self) -> None:
        assert self._auto(Checks.PENDING) is Status.AUTO_MERGING

    def test_a_pr_with_no_checks_yet_is_still_auto_merging(self) -> None:
        # Absence is ambiguous and usually transient; only GitHub saying
        # `ACTION_REQUIRED` is treated as a stall. See Checks.stalled.
        assert self._auto(Checks.NONE) is Status.AUTO_MERGING

    def test_one_blocked_pr_among_several_stalls_the_task(self) -> None:
        working = item(
            "a",
            open_prs=(PullRequest(7, Checks.PASSING), PullRequest(8, Checks.BLOCKED)),
            verify=Verify.AUTO,
        )
        assert resolve(items_of(working))[ref("a")] is Status.IN_REVIEW

    def test_human_verify_is_unaffected_by_checks(self) -> None:
        # `verify: human` never promised CI would decide, so a blocked run
        # changes nothing about what the board should say.
        working = item("a", open_prs=(PullRequest(7, Checks.BLOCKED),), verify=Verify.HUMAN)
        assert resolve(items_of(working))[ref("a")] is Status.IN_REVIEW


class TestCiNotices:
    """A stalled pipeline must be *explained*, not merely absorbed.

    `In Review` is true when CI is blocked, but on its own it tells a reader to
    go and review a PR that nobody can merge. The notice carries the reason and
    the remedy, which is the difference between a board that is quiet and a
    board that is honest.
    """

    def test_a_blocked_run_produces_a_notice_naming_the_pr(self) -> None:
        held = (PullRequest(7, Checks.BLOCKED),)
        working = item("a", number=3, open_prs=held, verify=Verify.AUTO)
        notices = ci_notices(items_of(working))
        assert [notice.code for notice in notices] == ["ci-approval-required"]
        assert notices[0].where == "#3"
        assert "7" in notices[0].message

    def test_the_remedy_is_stated(self) -> None:
        working = item("a", open_prs=(PullRequest(7, Checks.BLOCKED),), verify=Verify.AUTO)
        message = ci_notices(items_of(working))[0].message
        assert "approve" in message.lower()

    def test_the_remedy_is_one_that_actually_works(self) -> None:
        # The obvious REST call — POST .../actions/runs/{id}/approve — answers
        # 403 "not from a fork pull request or queued by the Actions bot" for
        # a Copilot-authored run. `gh run rerun` re-queues it under the
        # maintainer's own identity and does start CI. Verified live.
        message = ci_notices(
            items_of(item("a", open_prs=(PullRequest(7, Checks.BLOCKED),), verify=Verify.AUTO))
        )[0].message
        assert "gh run rerun" in message
        assert "/approve" not in message

    def test_the_remedy_does_not_pretend_the_gate_is_a_formality(self) -> None:
        # Clearing it is a decision to run agent-authored code on your
        # runners, which is the reason the gate exists.
        message = ci_notices(
            items_of(item("a", open_prs=(PullRequest(7, Checks.BLOCKED),), verify=Verify.AUTO))
        )[0].message
        assert "agent-authored" in message

    def test_a_failing_run_is_not_an_approval_problem(self) -> None:
        # A red PR is the agent's problem or a reviewer's; it is not a gate
        # someone can click away, so it must not suggest that it is.
        working = item("a", open_prs=(PullRequest(7, Checks.FAILING),), verify=Verify.AUTO)
        assert ci_notices(items_of(working)) == ()

    def test_a_healthy_pr_produces_nothing(self) -> None:
        working = item("a", open_prs=(PullRequest(7, Checks.PASSING),), verify=Verify.AUTO)
        assert ci_notices(items_of(working)) == ()

    def test_human_verify_tasks_are_not_reported(self) -> None:
        # Nobody was waiting on CI, so a held run is not blocking the task.
        working = item("a", open_prs=(PullRequest(7, Checks.BLOCKED),), verify=Verify.HUMAN)
        assert ci_notices(items_of(working)) == ()

    def test_a_closed_task_is_not_reported(self) -> None:
        working = item(
            "a", closed=True, open_prs=(PullRequest(7, Checks.BLOCKED),), verify=Verify.AUTO
        )
        assert ci_notices(items_of(working)) == ()


class TestSyntheticShapes:
    def test_diamond_opens_two_parallel_tasks_then_the_join(self) -> None:
        nodes = items_of(
            item("root", closed=True),
            item("left", depends=("root",)),
            item("right", depends=("root",)),
            item("join", depends=("left", "right")),
        )
        statuses = resolve(nodes)
        assert statuses[ref("left")] is Status.READY
        assert statuses[ref("right")] is Status.READY
        assert statuses[ref("join")] is Status.BLOCKED

    def test_diamond_join_opens_only_when_both_arms_close(self) -> None:
        nodes = items_of(
            item("root", closed=True),
            item("left", closed=True, depends=("root",)),
            item("right", closed=True, depends=("root",)),
            item("join", depends=("left", "right")),
        )
        assert resolve(nodes)[ref("join")] is Status.READY

    def test_star_opens_every_leaf_at_once(self) -> None:
        nodes = items_of(
            item("root", closed=True),
            *(item(f"leaf{i}", depends=("root",)) for i in range(4)),
        )
        statuses = resolve(nodes)
        assert sum(1 for status in statuses.values() if status is Status.READY) == 4

    def test_chain_opens_exactly_one_task(self) -> None:
        nodes = items_of(
            item("t0", closed=True),
            item("t1", depends=("t0",)),
            item("t2", depends=("t1",)),
        )
        statuses = resolve(nodes)
        assert statuses[ref("t1")] is Status.READY
        assert statuses[ref("t2")] is Status.BLOCKED

    def test_a_cycle_blocks_everything_without_hanging(self) -> None:
        # A cycle can only reach the scheduler through a hand-edited issue,
        # since `validate` rejects it. Readiness is a one-step check, so it
        # terminates and simply never opens anything.
        statuses = resolve(items_of(item("a", depends=("b",)), item("b", depends=("a",))))
        assert set(statuses.values()) == {Status.BLOCKED}

    def test_a_self_dependency_blocks_only_itself(self) -> None:
        statuses = resolve(items_of(item("a", depends=("a",)), item("b")))
        assert statuses[ref("a")] is Status.BLOCKED
        assert statuses[ref("b")] is Status.READY


class TestStatusIsDerivedAndNotStored:
    """D14: there is nowhere to record a status, so there is nothing to reconcile.

    What the board's reconciliation used to prove is now a property of
    `resolve` itself: it answers for every task on every pass, from the issues
    alone, having read nothing it wrote earlier.
    """

    def test_every_task_gets_a_status_not_just_the_dispatched_ones(self) -> None:
        # An idle pass has to explain itself, so a task nothing is happening to
        # is still answered for.
        nodes = items_of(item("a", closed=True), item("b", depends=("a",)))
        assert resolve(nodes) == {ref("a"): Status.DONE, ref("b"): Status.READY}

    def test_the_same_snapshot_always_derives_the_same_statuses(self) -> None:
        # Nothing accumulates between passes, so resolving twice is resolving
        # once. This is what makes killing a pass mid-loop harmless.
        nodes = items_of(item("a", assignees=("copilot-swe-agent",)), item("b", depends=("a",)))
        assert resolve(nodes) == resolve(nodes)

    def test_a_stale_label_cannot_contradict_the_derivation(self) -> None:
        # A `status:*` label is exactly the stored state D14 removed. Should
        # one ever appear -- hand-written, or from an older version -- it is
        # not consulted: the issue itself decides.
        nodes = items_of(item("a", labels=("dispatchkit", "status:Done")))
        assert resolve(nodes)[ref("a")] is Status.READY


class TestAdmission:
    def test_ready_tasks_are_admitted_in_issue_order(self) -> None:
        nodes = items_of(item("b", number=20), item("a", number=10))
        plan = admit(nodes, resolve(nodes), SchedulerConfig())
        assert plan.admitted == (ref("a"), ref("b"))

    def test_lane_caps_bound_in_flight_work(self) -> None:
        nodes = items_of(*(item(f"t{i}", number=i) for i in range(5)))
        plan = admit(nodes, resolve(nodes), SchedulerConfig(caps={Lane.CLOUD: 3, Lane.LOCAL: 1}))
        assert len(plan.admitted) == 3
        assert [d.reason for d in plan.deferred] == ["lane-cap", "lane-cap"]

    def test_work_already_in_flight_counts_against_the_cap(self) -> None:
        nodes = items_of(
            item("running", number=1, assignees=("copilot",)),
            item("reviewing", number=2, open_prs=(9,)),
            item("waiting", number=3),
            item("also-waiting", number=4),
        )
        plan = admit(nodes, resolve(nodes), SchedulerConfig(caps={Lane.CLOUD: 3, Lane.LOCAL: 1}))
        assert plan.admitted == (ref("waiting"),)

    def test_lanes_have_independent_caps(self) -> None:
        nodes = items_of(
            item("c1", number=1),
            item("c2", number=2),
            item("l1", number=3, lane=Lane.LOCAL),
            item("l2", number=4, lane=Lane.LOCAL),
        )
        plan = admit(nodes, resolve(nodes), SchedulerConfig(caps={Lane.CLOUD: 3, Lane.LOCAL: 1}))
        assert plan.admitted == (ref("c1"), ref("c2"), ref("l1"))
        assert [d.ref for d in plan.deferred] == [ref("l2")]

    def test_only_ready_tasks_are_considered(self) -> None:
        nodes = items_of(item("a"), item("b", depends=("a",)))
        plan = admit(nodes, resolve(nodes), SchedulerConfig())
        assert plan.admitted == (ref("a"),)
        assert plan.deferred == ()  # blocked is not "deferred", it is just not ready


class TestSpendAndRetryGates:
    def test_a_paid_task_waits_for_human_approval(self) -> None:
        nodes = items_of(item("a", spend=True))
        plan = admit(nodes, resolve(nodes), SchedulerConfig())
        assert plan.admitted == ()
        assert plan.deferred[0].reason == "awaiting-spend-approval"

    def test_an_approved_paid_task_is_admitted(self) -> None:
        nodes = items_of(item("a", spend=True, labels=("dispatchkit", "spend:approved")))
        assert admit(nodes, resolve(nodes), SchedulerConfig()).admitted == (ref("a"),)

    def test_the_spend_gate_applies_to_the_local_lane_too(self) -> None:
        # Cost is orthogonal to lane: free work must not inherit the gate, and
        # paid work must not escape it by being routed elsewhere.
        nodes = items_of(item("a", spend=True, lane=Lane.LOCAL))
        assert admit(nodes, resolve(nodes), SchedulerConfig()).admitted == ()

    def test_a_stuck_task_is_never_dispatched_again(self) -> None:
        """Since D7 the label also makes the status `Stuck`, so admission never
        sees it as ready and there is no deferral to report -- the board column
        says it once, instead of every pass repeating itself."""
        nodes = items_of(item("a", labels=("dispatchkit", "dispatch:stuck")))
        plan = admit(nodes, resolve(nodes), SchedulerConfig())
        assert plan.admitted == ()
        assert plan.deferred == ()

    def test_a_task_at_the_retry_budget_is_stuck_even_without_the_label(self) -> None:
        nodes = items_of(item("a", attempts=3))
        plan = admit(nodes, resolve(nodes), SchedulerConfig(retry_budget=3))
        assert plan.deferred[0].reason == "stuck"

    def test_a_task_below_the_retry_budget_still_runs(self) -> None:
        nodes = items_of(item("a", attempts=2))
        assert admit(nodes, resolve(nodes), SchedulerConfig(retry_budget=3)).admitted == (
            ref("a"),
        )


class TestFileScopeExclusion:
    def test_two_ready_tasks_touching_the_same_tree_do_not_race(self) -> None:
        nodes = items_of(
            item("a", number=1, touches=("src/podkit/domain/**",)),
            item("b", number=2, touches=("src/podkit/domain/values.py",)),
        )
        plan = admit(nodes, resolve(nodes), SchedulerConfig())
        assert plan.admitted == (ref("a"),)
        assert plan.deferred[0].reason == "file-scope-conflict"

    def test_disjoint_scopes_run_concurrently(self) -> None:
        nodes = items_of(
            item("a", number=1, touches=("src/podkit/domain/**",)),
            item("b", number=2, touches=("src/podkit/infrastructure/**",)),
        )
        assert len(admit(nodes, resolve(nodes), SchedulerConfig()).admitted) == 2

    def test_a_ready_task_defers_to_work_already_dispatched(self) -> None:
        nodes = items_of(
            item("running", number=1, assignees=("copilot",), touches=("src/a/**",)),
            item("waiting", number=2, touches=("src/a/values.py",)),
        )
        assert admit(nodes, resolve(nodes), SchedulerConfig()).admitted == ()

    def test_undeclared_scope_excludes_nothing(self) -> None:
        # Omitting `touches` means "unknown scope", which must not be read as
        # "conflicts with everything" — that would serialise the whole board.
        nodes = items_of(
            item("a", number=1, touches=()),
            item("b", number=2, touches=()),
        )
        assert len(admit(nodes, resolve(nodes), SchedulerConfig()).admitted) == 2

    def test_exclusion_is_advisory_and_only_defers(self) -> None:
        nodes = items_of(
            item("a", number=1, touches=("src/a/**",)),
            item("b", number=2, touches=("src/a/**",)),
        )
        plan = admit(nodes, resolve(nodes), SchedulerConfig())
        deferred = plan.deferred[0]
        assert deferred.ref == ref("b")
        assert "a" in deferred.detail  # names what it is waiting behind


class TestDraftBlocksAutoMerge:
    """A draft pull request cannot be merged, so `Auto-merging` cannot be true.

    The same fault as `TestAutoMergeRequiresLiveCi`, reached by a different
    road. Copilot opens its pull requests as drafts and — by design, confirmed
    by a `copilot_work_finished` event with no `ready_for_review` after it —
    leaves them that way when it is done. Green CI on a draft still merges
    nothing.

    `mergeStateStatus` is no help and is actively misleading: GitHub documents
    a `DRAFT` value, but both live sandbox PRs reported `CLEAN` while
    `isDraft` was true. Only `isDraft` can be trusted, which is why it is a
    field on `PullRequest` rather than something inferred.
    """

    @staticmethod
    def _auto(*, draft: bool, checks: Checks = Checks.PASSING) -> Status:
        working = item(
            "a",
            assignees=("copilot",),
            open_prs=(PullRequest(7, checks, draft=draft, mergeable=True),),
            verify=Verify.AUTO,
        )
        return resolve(items_of(working))[ref("a")]

    def test_a_green_draft_is_not_auto_merging(self) -> None:
        assert self._auto(draft=True) is Status.IN_REVIEW

    def test_a_green_ready_pr_is_still_auto_merging(self) -> None:
        assert self._auto(draft=False) is Status.AUTO_MERGING

    def test_draft_does_not_change_a_human_task(self) -> None:
        # `verify: human` was always `In Review`; draft is the normal state
        # there and marking it ready is the reviewer's own act.
        working = item(
            "a",
            assignees=("copilot",),
            open_prs=(PullRequest(7, Checks.PASSING, draft=True),),
            verify=Verify.HUMAN,
        )
        assert resolve(items_of(working))[ref("a")] is Status.IN_REVIEW


class TestReadyOps:
    """`verify: auto` promises no human judgment, so draft is ours to clear.

    Only when CI has actually passed. `Checks.NONE` is an absence of evidence,
    not evidence, so a draft with no runs stays a draft — that is the one case
    where the old behaviour was accidentally right for the wrong reason.
    """

    @staticmethod
    def _ops(
        *, verify: Verify = Verify.AUTO, draft: bool = True, checks: Checks = Checks.PASSING
    ) -> tuple[MarkReady, ...]:
        working = item(
            "a",
            number=3,
            assignees=("copilot",),
            open_prs=(PullRequest(7, checks, draft=draft, mergeable=True),),
            verify=verify,
        )
        return ready_ops(items_of(working))

    def test_a_green_auto_draft_is_marked_ready(self) -> None:
        assert self._ops() == (MarkReady(ref("a"), 7),)

    def test_a_pr_already_out_of_draft_is_left_alone(self) -> None:
        assert self._ops(draft=False) == ()

    def test_a_human_task_is_never_marked_ready(self) -> None:
        assert self._ops(verify=Verify.HUMAN) == ()

    def test_a_draft_without_a_passing_run_is_left_alone(self) -> None:
        for checks in (Checks.NONE, Checks.PENDING, Checks.FAILING, Checks.BLOCKED):
            assert self._ops(checks=checks) == ()

    def test_a_closed_task_is_left_alone(self) -> None:
        done = item(
            "a",
            closed=True,
            open_prs=(PullRequest(7, Checks.PASSING, draft=True),),
            verify=Verify.AUTO,
        )
        assert ready_ops(items_of(done)) == ()


class TestMergeOps:
    """D9: the pass that finally makes `Auto-merging` mean something.

    Every gate here is one dispatchkit checks itself, because the probe that
    preceded this code showed the repository cannot be relied on to check any
    of them. An unprotected branch merges whatever it is handed.
    """

    @staticmethod
    def _ops(
        *,
        verify: Verify = Verify.AUTO,
        draft: bool = False,
        checks: Checks = Checks.PASSING,
        files: tuple[str, ...] = ("src/app.py",),
        closed: bool = False,
        config: SchedulerConfig | None = None,
    ) -> tuple[MergePr, ...]:
        working = item(
            "a",
            number=3,
            assignees=("copilot",),
            open_prs=(PullRequest(7, checks, draft=draft, files=files, mergeable=True),),
            verify=verify,
            closed=closed,
            touches=("src/**",),
        )
        return merge_ops(items_of(working), config or SchedulerConfig())

    def test_a_green_undrafted_auto_pr_is_merged(self) -> None:
        assert self._ops() == (MergePr(ref("a"), 7),)

    def test_a_human_task_is_never_merged(self) -> None:
        assert self._ops(verify=Verify.HUMAN) == ()

    def test_a_draft_is_never_merged(self) -> None:
        assert self._ops(draft=True) == ()

    def test_only_a_passing_run_merges(self) -> None:
        for checks in (Checks.NONE, Checks.PENDING, Checks.FAILING, Checks.BLOCKED):
            assert self._ops(checks=checks) == ()

    def test_a_closed_task_is_left_alone(self) -> None:
        assert self._ops(closed=True) == ()

    def test_a_pr_touching_the_fence_is_never_merged(self) -> None:
        # The pipeline may not rewrite its own rules unattended, however green.
        for path in (
            ".github/workflows/ci.yml",
            ".github/dispatchkit.toml",
            "docs/plans/demo.tasks.toml",
        ):
            assert self._ops(files=("src/app.py", path)) == (), path

    def test_a_pr_whose_files_are_unknown_is_never_merged(self) -> None:
        # An empty file list is not an empty pull request; it is a question we
        # failed to get an answer to, and the fence cannot be applied to it.
        assert self._ops(files=()) == ()


class TestMergeNeedsAMergeablePr:
    """Green is not the same question as mergeable, and D9 shipped confusing them.

    Found live: PR #7 was `Checks.PASSING`, out of draft, outside the fence —
    and `CONFLICTING`. The pass asked GitHub to merge it and got a hard error.
    The board had again asserted something it never checked, which is the third
    time the same shape of bug has surfaced in this system.
    """

    @staticmethod
    def _ops(mergeable: bool) -> tuple[MergePr, ...]:
        working = item(
            "a",
            number=3,
            verify=Verify.AUTO,
            touches=("src/**",),
            open_prs=(
                PullRequest(
                    7,
                    Checks.PASSING,
                    draft=False,
                    files=("src/app.py",),
                    mergeable=mergeable,
                ),
            ),
        )
        return merge_ops(items_of(working), SchedulerConfig())

    def test_a_mergeable_pr_merges(self) -> None:
        assert self._ops(True) == (MergePr(ref("a"), 7),)

    def test_a_conflicting_pr_does_not(self) -> None:
        assert self._ops(False) == ()


class TestConflictBlocksAutoMerging:
    """A conflicting pull request needs a human, so the board must say so.

    The fourth time this exact shape of bug has been found here: `Auto-merging`
    claimed over a pull request that would never merge. D5.6 was CI it never
    checked, D5.7 was draft, D9 was mergeability at the merge site — and the
    *status* still had to be taught the same lesson separately.
    """

    @staticmethod
    def _status(mergeable: bool) -> Status:
        working = item(
            "a",
            verify=Verify.AUTO,
            open_prs=(
                PullRequest(7, Checks.PASSING, draft=False, files=("s.py",), mergeable=mergeable),
            ),
        )
        return resolve(items_of(working))[ref("a")]

    def test_a_mergeable_pr_is_auto_merging(self) -> None:
        assert self._status(True) is Status.AUTO_MERGING

    def test_a_conflicting_pr_falls_back_to_in_review(self) -> None:
        # Only a human can rebase it, which is what `In Review` means here.
        assert self._status(False) is Status.IN_REVIEW


class TestScopeDrift:
    """A pull request that wandered outside its declared `touches` is not merged.

    The live failure behind this: `stopwords` declared
    `touches = ["src/wordfreq/count.py", "tests/test_count.py"]` and its pull
    request edited `src/wordfreq/cli.py` and `tests/test_cli.py`, which `top-n`
    was editing at the same time. The file-scope exclusion reasons about
    *declared* scope, so it never fired; the two ran concurrently and collided,
    and a human had to resolve the conflict by hand.

    An agent outside its blast radius is exactly the case the design says not
    to merge unattended, and the declaration is the only thing the exclusion
    had to work with.
    """

    @staticmethod
    def _ops(*, touches: tuple[str, ...], files: tuple[str, ...]) -> tuple[MergePr, ...]:
        working = item(
            "a",
            verify=Verify.AUTO,
            touches=touches,
            open_prs=(PullRequest(7, Checks.PASSING, draft=False, files=files, mergeable=True),),
        )
        return merge_ops(items_of(working), SchedulerConfig())

    def test_a_pr_inside_its_declared_scope_merges(self) -> None:
        assert self._ops(touches=("src/a.py", "tests/test_a.py"), files=("src/a.py",)) == (
            MergePr(ref("a"), 7),
        )

    def test_a_pr_that_drifted_is_not_merged(self) -> None:
        assert self._ops(touches=("src/a.py",), files=("src/a.py", "src/b.py")) == ()

    def test_touches_may_be_a_glob(self) -> None:
        assert self._ops(touches=("src/**",), files=("src/deep/a.py",)) == (
            MergePr(ref("a"), 7),
        )

    def test_an_undeclared_scope_cannot_be_checked_and_so_does_not_merge(self) -> None:
        # Empty `touches` excludes nothing for the concurrency fence, where the
        # permissive reading is right. Here it is an unanswerable question, and
        # the answer to those is no.
        assert self._ops(touches=(), files=("src/a.py",)) == ()


class TestTwoPlansInOneRepository:
    """D13: `watch` covers every plan, so a `TaskId` is no longer unique.

    `id` is a slug scoped to its own graph file, and nothing has ever stopped
    two plans both containing `ports`. While a pass took `--plan` that was
    harmless — one plan's items were the only ones in the room. Pooling them
    makes the bare id ambiguous, and the resolver is where that first bites:
    a closed `ports` in one plan must not unblock a `depends = ["ports"]` in
    another. So the key is `TaskRef(plan, id)`, and `depends` is resolved
    within the depending task's own plan.
    """

    def two_plans(self) -> tuple[TaskItem, ...]:
        return items_of(
            item("ports", plan="alpha", number=1, closed=True),
            item("ports", plan="beta", number=2),
            item("api", plan="beta", number=3, depends=("ports",)),
        )

    def test_a_closed_task_does_not_unblock_the_same_id_in_another_plan(self) -> None:
        statuses = resolve(self.two_plans())
        assert statuses[ref("api", "beta")] is Status.BLOCKED

    def test_each_plans_task_gets_its_own_verdict(self) -> None:
        statuses = resolve(self.two_plans())
        assert statuses[ref("ports", "alpha")] is Status.DONE
        assert statuses[ref("ports", "beta")] is Status.READY

    def test_no_task_is_lost_to_a_collision(self) -> None:
        # The bug this replaces was silent: a dict keyed on the bare id simply
        # held the last writer, so one of the two `ports` vanished from the
        # report entirely.
        assert len(resolve(self.two_plans())) == 3

    def test_a_ref_reads_as_plan_slash_id(self) -> None:
        # It goes in the report, in a worktree path and in a branch name, so
        # it needs one spelling rather than three.
        assert str(ref("api", "beta")) == "beta/api"
