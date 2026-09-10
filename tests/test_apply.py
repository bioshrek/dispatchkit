"""D3: `apply` — graph → GitHub issues, idempotently.

Run against a recorded API fixture (`tests/fixtures/dispatch/`) and an
in-memory double, never the network. The central claim is convergence: apply
the plan, re-read the state, plan again, and the second plan must be empty.
`id` is the idempotency key — a second run updates the existing issue rather
than creating a twin.

Since D14 an issue is the *only* thing `apply` writes. Status is derived on
every pass and printed, so there is nothing here that could record a readiness
guess and then go stale.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from dispatchkit.apply import build_body, desired_labels, execute_plan, plan_apply
from dispatchkit.block import parse_block
from dispatchkit.github import CreateIssue, IssueState, RepoState, UpdateIssue
from dispatchkit.model import Lane, TaskGraph, TaskId, Verify
from tests.fake_github import FakeGitHub
from tests.graphs import graph, task

pytestmark = pytest.mark.replay

FIXTURES = Path(__file__).resolve().parent / "fixtures"

PLAN = "demo"


def two_task_graph() -> TaskGraph:
    return graph(
        task("ports", verify=Verify.AUTO),
        task("adapter", depends=("ports",), lane=Lane.LOCAL, requires=("gpu",)),
        plan=PLAN,
    )


def recorded_state() -> RepoState:
    from dispatchkit.gh_cli import parse_state

    payload = json.loads((FIXTURES / "search_issues.json").read_text(encoding="utf-8"))
    return parse_state(payload)


class TestPlanningFromEmpty:
    def test_every_task_becomes_one_issue_and_nothing_else(self) -> None:
        plan = plan_apply(two_task_graph(), RepoState(()))

        assert [type(op) for op in plan.operations] == [CreateIssue, CreateIssue]
        assert plan.notices == ()

    def test_a_tasks_routing_travels_as_labels_not_as_stored_state(self) -> None:
        # Lane and verify are in the machine block already; the labels mirror
        # them so an issue-list URL can filter on them. Nothing else is
        # recorded, and `Status` in particular is derived every pass.
        plan = plan_apply(two_task_graph(), RepoState(()))
        adapter = plan.operations[1]
        assert isinstance(adapter, CreateIssue)
        assert set(adapter.labels) == {"dispatchkit", "plan:demo", "lane:local", "verify:human"}
        assert not any(label.startswith("status:") for label in adapter.labels)

    def test_issue_carries_the_documented_labels(self) -> None:
        plan = plan_apply(two_task_graph(), RepoState(()))
        create = next(op for op in plan.operations if isinstance(op, CreateIssue))
        assert set(create.labels) == {"dispatchkit", "plan:demo", "lane:cloud", "verify:auto"}

    def test_issue_body_carries_acceptance_and_a_round_trippable_block(self) -> None:
        plan = plan_apply(two_task_graph(), RepoState(()))
        create = next(op for op in plan.operations if isinstance(op, CreateIssue))

        assert "true" in create.body  # the acceptance command
        block = parse_block(create.body)
        assert block.id == TaskId("ports")
        assert block.plan == PLAN
        assert block.verify is Verify.AUTO

    def test_body_file_spec_is_included_when_supplied(self) -> None:
        body = build_body(task("ports"), plan=PLAN, spec="Long-form spec.\n\nSecond paragraph.")
        assert "Long-form spec." in body
        assert body.rstrip().endswith("-->")


class TestIdempotency:
    def test_applying_twice_is_a_no_op(self) -> None:
        api = FakeGitHub()
        first = plan_apply(two_task_graph(), api.fetch_state(plan=PLAN))
        execute_plan(first, api)

        second = plan_apply(two_task_graph(), api.fetch_state(plan=PLAN))
        assert second.operations == ()
        assert second.notices == ()

    def test_a_second_run_updates_rather_than_duplicating(self) -> None:
        api = FakeGitHub()
        execute_plan(plan_apply(two_task_graph(), api.fetch_state(plan=PLAN)), api)

        changed = graph(
            task("ports", verify=Verify.AUTO),
            task("adapter", depends=("ports",), lane=Lane.LOCAL, requires=("gpu", "long-run")),
            plan=PLAN,
        )
        second = plan_apply(changed, api.fetch_state(plan=PLAN))
        execute_plan(second, api)

        assert [type(op) for op in second.operations] == [UpdateIssue]
        assert len(api.state.issues) == 2  # not four

    def test_execute_returns_the_numbers_it_created(self) -> None:
        api = FakeGitHub()
        result = execute_plan(plan_apply(two_task_graph(), RepoState(())), api)
        assert sorted(result.issue_numbers) == [100, 101]
        assert result.created == 2
        assert result.updated == 0

    def test_labels_are_created_before_they_are_used(self) -> None:
        api = FakeGitHub()
        execute_plan(plan_apply(two_task_graph(), RepoState(())), api)
        assert "lane:local" in api.ensured_labels
        assert api.calls[0].startswith("ensure_labels")


class TestDriftAgainstTheRecordedFixture:
    def test_an_unchanged_task_produces_no_operations(self) -> None:
        # `ports` in the fixture was recorded from a previous apply of this graph.
        plan = plan_apply(two_task_graph(), recorded_state())
        assert not any(
            isinstance(op, UpdateIssue) and op.task_id == TaskId("ports") for op in plan.operations
        )

    def test_a_changed_task_is_updated_in_place(self) -> None:
        changed = graph(
            task("ports", verify=Verify.AUTO),
            task("adapter", depends=("ports",), lane=Lane.LOCAL, requires=("gpu",)),
            plan=PLAN,
        )
        state = recorded_state()
        plan = plan_apply(changed, state)
        updates = [op for op in plan.operations if isinstance(op, UpdateIssue)]
        assert [op.task_id for op in updates] == [TaskId("adapter")]

    def test_update_preserves_labels_it_does_not_own(self) -> None:
        state = RepoState(
            (
                _issue(
                    number=7,
                    task_id="ports",
                    title="Stale title",
                    labels=(
                        "dispatchkit",
                        "plan:demo",
                        "lane:cloud",
                        "verify:auto",
                        "spend:approved",
                    ),
                ),
            )
        )
        plan = plan_apply(graph(task("ports", verify=Verify.AUTO), plan=PLAN), state)
        update = next(op for op in plan.operations if isinstance(op, UpdateIssue))
        assert "spend:approved" in update.labels
        assert "dispatchkit" in update.labels

    def test_stale_managed_labels_are_replaced(self) -> None:
        state = RepoState(
            (
                _issue(
                    number=7,
                    task_id="ports",
                    labels=("dispatchkit", "plan:demo", "lane:local", "verify:human"),
                ),
            )
        )
        plan = plan_apply(graph(task("ports", verify=Verify.AUTO), plan=PLAN), state)
        update = next(op for op in plan.operations if isinstance(op, UpdateIssue))
        assert "lane:cloud" in update.labels
        assert "lane:local" not in update.labels
        assert "verify:auto" in update.labels


class TestNotices:
    def test_a_closed_issue_is_never_mutated(self) -> None:
        state = RepoState((_issue(number=7, task_id="ports", title="Old", closed=True),))
        plan = plan_apply(graph(task("ports"), plan=PLAN), state)

        assert plan.operations == ()
        assert [n.code for n in plan.notices] == ["closed-drift"]

    def test_a_closed_issue_with_no_drift_is_silent(self) -> None:
        graph_ = graph(task("ports"), plan=PLAN)
        state = RepoState(
            (
                _issue(
                    number=7,
                    task_id="ports",
                    title="ports",
                    body=build_body(graph_.tasks[0], plan=PLAN, spec=None),
                    closed=True,
                ),
            )
        )
        plan = plan_apply(graph_, state)
        assert plan.operations == ()
        assert plan.notices == ()

    def test_an_issue_for_a_deleted_task_is_reported_not_closed(self) -> None:
        state = RepoState((_issue(number=7, task_id="ghost"),))
        plan = plan_apply(graph(task("ports"), plan=PLAN), state)

        assert [n.code for n in plan.notices] == ["orphan-issue"]
        assert "ghost" in plan.notices[0].message
        assert not any(isinstance(op, UpdateIssue) for op in plan.operations)

    def test_an_unreadable_block_is_reported_rather_than_crashing(self) -> None:
        state = RepoState(
            (
                IssueState(
                    number=7,
                    title="Hand-edited",
                    body="someone deleted the block",
                    labels=("dispatchkit", "plan:demo"),
                    closed=False,
                ),
            )
        )
        plan = plan_apply(graph(task("ports"), plan=PLAN), state)
        assert "unreadable-issue" in [n.code for n in plan.notices]

    def test_an_issue_from_another_plan_is_ignored(self) -> None:
        state = RepoState((_issue(number=7, task_id="ports", plan="other"),))
        plan = plan_apply(graph(task("ports"), plan=PLAN), state)
        assert plan.notices == ()
        assert any(isinstance(op, CreateIssue) for op in plan.operations)


def _issue(
    *,
    number: int,
    task_id: str,
    title: str | None = None,
    body: str | None = None,
    labels: tuple[str, ...] | None = None,
    closed: bool = False,
    plan: str = PLAN,
) -> IssueState:
    the_task = task(task_id)
    return IssueState(
        number=number,
        title=title if title is not None else the_task.title,
        body=body if body is not None else build_body(the_task, plan=plan, spec=None),
        labels=labels if labels is not None else tuple(desired_labels(the_task, plan=plan)),
        closed=closed,
    )


class TestEditingATaskInPlace:
    """Changing a task's routing on an issue an earlier run created.

    This was the D5.6 latent bug: `apply` learned board item ids only from the
    `AddProjectItem` operations it performed in the same run, so a field write
    for an item added by an *earlier* run had no id and died with `KeyError`.
    D14 deleted the board, and with it the whole failure mode — but the
    behaviour it was hiding still has to work, so the case is kept and asked
    the question that outlives the board: does an edited task converge?
    """

    def _applied(self) -> tuple[FakeGitHub, TaskGraph]:
        api = FakeGitHub()
        first = graph(task("ports", verify=Verify.HUMAN), plan=PLAN)
        execute_plan(plan_apply(first, api.fetch_state(plan=PLAN)), api)
        return api, graph(task("ports", verify=Verify.AUTO), plan=PLAN)

    def test_the_change_is_planned_as_a_single_issue_update(self) -> None:
        api, changed = self._applied()
        plan = plan_apply(changed, api.fetch_state(plan=PLAN))
        assert [type(op) for op in plan.operations] == [UpdateIssue]

    def test_the_verify_label_is_swapped_rather_than_accumulated(self) -> None:
        api, changed = self._applied()
        execute_plan(plan_apply(changed, api.fetch_state(plan=PLAN)), api)
        labels = api.state.issues[0].labels
        assert "verify:auto" in labels
        assert "verify:human" not in labels

    def test_the_machine_block_is_rewritten_too(self) -> None:
        # The labels are a mirror; the block is what the scheduler reads, so
        # the two must never be allowed to disagree.
        api, changed = self._applied()
        execute_plan(plan_apply(changed, api.fetch_state(plan=PLAN)), api)
        assert parse_block(api.state.issues[0].body).verify is Verify.AUTO

    def test_it_converges(self) -> None:
        api, changed = self._applied()
        execute_plan(plan_apply(changed, api.fetch_state(plan=PLAN)), api)
        assert plan_apply(changed, api.fetch_state(plan=PLAN)).operations == ()
