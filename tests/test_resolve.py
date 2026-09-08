"""D4: readiness resolution and admission as pure functions.

Everything here is a function of the issue snapshot alone — no network, no
local state, no graph file. That is the design claim being tested: if the
Project board were deleted, the scheduler could rebuild it from the issues,
because `Status` is derived rather than stored.

The rule under test, verbatim from the design: *a task is ready iff it is open,
unassigned, and every id in `depends` maps to a closed issue.*
"""

from __future__ import annotations

from dispatchkit.config import SchedulerConfig
from dispatchkit.github import SetProjectField
from dispatchkit.model import Lane, TaskId, Verify
from dispatchkit.resolve import Status, admit, reconcile_ops, resolve
from tests.items import item, items_of


class TestReadiness:
    def test_a_task_with_no_dependencies_is_ready(self) -> None:
        assert resolve(items_of(item("a")))[TaskId("a")] is Status.READY

    def test_a_task_waiting_on_an_open_issue_is_blocked(self) -> None:
        statuses = resolve(items_of(item("a"), item("b", depends=("a",))))
        assert statuses[TaskId("b")] is Status.BLOCKED

    def test_a_task_becomes_ready_when_its_dependency_closes(self) -> None:
        statuses = resolve(items_of(item("a", closed=True), item("b", depends=("a",))))
        assert statuses[TaskId("a")] is Status.DONE
        assert statuses[TaskId("b")] is Status.READY

    def test_an_assigned_task_is_dispatched_not_ready(self) -> None:
        # Assignment *is* the dispatch lock, so an assigned task is never in
        # the ready set — two concurrent passes therefore converge.
        assert resolve(items_of(item("a", assignees=("copilot",))))[TaskId("a")] is (
            Status.DISPATCHED
        )

    def test_a_local_lane_claim_label_also_counts_as_dispatched(self) -> None:
        claimed = item("a", lane=Lane.LOCAL, labels=("dispatchkit", "dispatch:local"))
        assert resolve(items_of(claimed))[TaskId("a")] is Status.DISPATCHED

    def test_a_closed_task_is_done_even_if_it_was_never_assigned(self) -> None:
        assert resolve(items_of(item("a", closed=True)))[TaskId("a")] is Status.DONE

    def test_a_dependency_on_a_deleted_issue_leaves_the_task_blocked(self) -> None:
        # "Dependency issue deleted -> dependents stay Blocked."
        statuses = resolve(items_of(item("b", depends=("ghost",))))
        assert statuses[TaskId("b")] is Status.BLOCKED

    def test_every_dependency_must_be_closed_not_just_one(self) -> None:
        statuses = resolve(
            items_of(
                item("a", closed=True),
                item("b"),
                item("c", depends=("a", "b")),
            )
        )
        assert statuses[TaskId("c")] is Status.BLOCKED


class TestWorkInFlight:
    def test_an_open_pr_on_a_human_verify_task_is_in_review(self) -> None:
        working = item("a", assignees=("copilot",), open_prs=(7,), verify=Verify.HUMAN)
        assert resolve(items_of(working))[TaskId("a")] is Status.IN_REVIEW

    def test_an_open_pr_on_an_auto_verify_task_is_auto_merging(self) -> None:
        working = item("a", assignees=("copilot",), open_prs=(7,), verify=Verify.AUTO)
        assert resolve(items_of(working))[TaskId("a")] is Status.AUTO_MERGING

    def test_an_unassigned_task_with_a_pr_still_counts_as_work_in_flight(self) -> None:
        # The local daemon works from a label rather than an assignee, so a PR
        # is the more reliable signal that something is already happening.
        assert resolve(items_of(item("a", open_prs=(7,))))[TaskId("a")] is Status.IN_REVIEW


class TestSyntheticShapes:
    def test_diamond_opens_two_parallel_tasks_then_the_join(self) -> None:
        nodes = items_of(
            item("root", closed=True),
            item("left", depends=("root",)),
            item("right", depends=("root",)),
            item("join", depends=("left", "right")),
        )
        statuses = resolve(nodes)
        assert statuses[TaskId("left")] is Status.READY
        assert statuses[TaskId("right")] is Status.READY
        assert statuses[TaskId("join")] is Status.BLOCKED

    def test_diamond_join_opens_only_when_both_arms_close(self) -> None:
        nodes = items_of(
            item("root", closed=True),
            item("left", closed=True, depends=("root",)),
            item("right", closed=True, depends=("root",)),
            item("join", depends=("left", "right")),
        )
        assert resolve(nodes)[TaskId("join")] is Status.READY

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
        assert statuses[TaskId("t1")] is Status.READY
        assert statuses[TaskId("t2")] is Status.BLOCKED

    def test_a_cycle_blocks_everything_without_hanging(self) -> None:
        # A cycle can only reach the scheduler through a hand-edited issue,
        # since `validate` rejects it. Readiness is a one-step check, so it
        # terminates and simply never opens anything.
        statuses = resolve(items_of(item("a", depends=("b",)), item("b", depends=("a",))))
        assert set(statuses.values()) == {Status.BLOCKED}

    def test_a_self_dependency_blocks_only_itself(self) -> None:
        statuses = resolve(items_of(item("a", depends=("a",)), item("b")))
        assert statuses[TaskId("a")] is Status.BLOCKED
        assert statuses[TaskId("b")] is Status.READY


class TestReconciliation:
    def test_status_is_written_for_every_item_not_just_dispatched_ones(self) -> None:
        nodes = items_of(
            item("a", closed=True, fields={"Status": "Dispatched"}),
            item("b", depends=("a",), fields={"Status": "Blocked"}),
        )
        ops = reconcile_ops(nodes, resolve(nodes))
        assert [(op.task_id, op.value) for op in ops] == [
            (TaskId("a"), "Done"),
            (TaskId("b"), "Ready"),
        ]

    def test_an_already_correct_status_is_not_rewritten(self) -> None:
        nodes = items_of(item("a", fields={"Status": "Ready"}))
        assert reconcile_ops(nodes, resolve(nodes)) == ()

    def test_reconciliation_only_ever_writes_status(self) -> None:
        nodes = items_of(item("a", fields={}))
        ops = reconcile_ops(nodes, resolve(nodes))
        assert all(isinstance(op, SetProjectField) and op.field_name == "Status" for op in ops)

    def test_an_item_not_on_the_board_is_skipped(self) -> None:
        # Nothing to write a field on; `apply` adds the item on its next run.
        nodes = items_of(item("a", project_item_id=None))
        assert reconcile_ops(nodes, resolve(nodes)) == ()


class TestAdmission:
    def test_ready_tasks_are_admitted_in_issue_order(self) -> None:
        nodes = items_of(item("b", number=20), item("a", number=10))
        plan = admit(nodes, resolve(nodes), SchedulerConfig())
        assert plan.admitted == (TaskId("a"), TaskId("b"))

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
        assert plan.admitted == (TaskId("waiting"),)

    def test_lanes_have_independent_caps(self) -> None:
        nodes = items_of(
            item("c1", number=1),
            item("c2", number=2),
            item("l1", number=3, lane=Lane.LOCAL),
            item("l2", number=4, lane=Lane.LOCAL),
        )
        plan = admit(nodes, resolve(nodes), SchedulerConfig(caps={Lane.CLOUD: 3, Lane.LOCAL: 1}))
        assert plan.admitted == (TaskId("c1"), TaskId("c2"), TaskId("l1"))
        assert [d.task_id for d in plan.deferred] == [TaskId("l2")]

    def test_only_ready_tasks_are_considered(self) -> None:
        nodes = items_of(item("a"), item("b", depends=("a",)))
        plan = admit(nodes, resolve(nodes), SchedulerConfig())
        assert plan.admitted == (TaskId("a"),)
        assert plan.deferred == ()  # blocked is not "deferred", it is just not ready


class TestSpendAndRetryGates:
    def test_a_paid_task_waits_for_human_approval(self) -> None:
        nodes = items_of(item("a", spend=True))
        plan = admit(nodes, resolve(nodes), SchedulerConfig())
        assert plan.admitted == ()
        assert plan.deferred[0].reason == "awaiting-spend-approval"

    def test_an_approved_paid_task_is_admitted(self) -> None:
        nodes = items_of(item("a", spend=True, labels=("dispatchkit", "spend:approved")))
        assert admit(nodes, resolve(nodes), SchedulerConfig()).admitted == (TaskId("a"),)

    def test_the_spend_gate_applies_to_the_local_lane_too(self) -> None:
        # Cost is orthogonal to lane: free work must not inherit the gate, and
        # paid work must not escape it by being routed elsewhere.
        nodes = items_of(item("a", spend=True, lane=Lane.LOCAL))
        assert admit(nodes, resolve(nodes), SchedulerConfig()).admitted == ()

    def test_a_stuck_task_is_never_dispatched_again(self) -> None:
        nodes = items_of(item("a", labels=("dispatchkit", "dispatch:stuck")))
        plan = admit(nodes, resolve(nodes), SchedulerConfig())
        assert plan.admitted == ()
        assert plan.deferred[0].reason == "stuck"

    def test_a_task_at_the_retry_budget_is_stuck_even_without_the_label(self) -> None:
        nodes = items_of(item("a", attempts=3))
        plan = admit(nodes, resolve(nodes), SchedulerConfig(retry_budget=3))
        assert plan.deferred[0].reason == "stuck"

    def test_a_task_below_the_retry_budget_still_runs(self) -> None:
        nodes = items_of(item("a", attempts=2))
        assert admit(nodes, resolve(nodes), SchedulerConfig(retry_budget=3)).admitted == (
            TaskId("a"),
        )


class TestFileScopeExclusion:
    def test_two_ready_tasks_touching_the_same_tree_do_not_race(self) -> None:
        nodes = items_of(
            item("a", number=1, touches=("src/podkit/domain/**",)),
            item("b", number=2, touches=("src/podkit/domain/values.py",)),
        )
        plan = admit(nodes, resolve(nodes), SchedulerConfig())
        assert plan.admitted == (TaskId("a"),)
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
        assert deferred.task_id == TaskId("b")
        assert "a" in deferred.detail  # names what it is waiting behind
