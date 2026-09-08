"""D3: `apply` — graph → GitHub issues + Project items, idempotently.

Run against a recorded API fixture (`tests/fixtures/dispatch/`) and an
in-memory double, never the network. The central claim is convergence: apply
the plan, re-read the state, plan again, and the second plan must be empty.
`id` is the idempotency key — a second run updates the existing issue rather
than creating a twin.

`Status` and `Attempts` are deliberately *not* written here. The Project is a
derived view that the scheduler reconciles on every pass, so writing a
readiness guess at apply time would just be a value to go stale.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from dispatchkit.apply import build_body, desired_labels, execute_plan, plan_apply
from dispatchkit.block import parse_block
from dispatchkit.github import (
    AddProjectItem,
    CreateIssue,
    IssueState,
    RepoState,
    SetProjectField,
    UpdateIssue,
)
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
    def test_every_task_becomes_an_issue_a_project_item_and_its_fields(self) -> None:
        plan = plan_apply(two_task_graph(), RepoState(()))

        assert [type(op) for op in plan.operations] == [
            CreateIssue,
            AddProjectItem,
            SetProjectField,
            SetProjectField,
            SetProjectField,
            CreateIssue,
            AddProjectItem,
            SetProjectField,
            SetProjectField,
            SetProjectField,
        ]
        assert plan.notices == ()

    def test_project_fields_are_task_id_lane_and_verify_only(self) -> None:
        plan = plan_apply(two_task_graph(), RepoState(()))
        fields = {
            op.field_name: op.value
            for op in plan.operations
            if isinstance(op, SetProjectField)
        }
        assert fields == {"Task ID": "adapter", "Lane": "local", "Verify": "human"}
        assert "Status" not in fields
        assert "Attempts" not in fields

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
        body = build_body(
            task("ports"), plan=PLAN, spec="Long-form spec.\n\nSecond paragraph."
        )
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
            isinstance(op, UpdateIssue) and op.task_id == TaskId("ports")
            for op in plan.operations
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

    def test_a_missing_project_item_is_added_without_touching_the_issue(self) -> None:
        state = RepoState((_issue(number=7, task_id="ports", project_item_id=None),))
        plan = plan_apply(graph(task("ports"), plan=PLAN), state)
        assert [type(op) for op in plan.operations] == [
            AddProjectItem,
            SetProjectField,
            SetProjectField,
            SetProjectField,
        ]

    def test_only_mismatched_project_fields_are_written(self) -> None:
        state = RepoState(
            (
                _issue(
                    number=7,
                    task_id="ports",
                    project_item_id="PVTI_1",
                    fields={"Task ID": "ports", "Lane": "local", "Verify": "human"},
                ),
            )
        )
        plan = plan_apply(graph(task("ports"), plan=PLAN), state)
        assert [op.field_name for op in plan.operations if isinstance(op, SetProjectField)] == [
            "Lane"
        ]


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
                    project_item_id=None,
                    fields={},
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
    project_item_id: str | None = "PVTI_1",
    fields: dict[str, str] | None = None,
) -> IssueState:
    the_task = task(task_id)
    return IssueState(
        number=number,
        title=title if title is not None else the_task.title,
        body=body if body is not None else build_body(the_task, plan=plan, spec=None),
        labels=labels if labels is not None else tuple(desired_labels(the_task, plan=plan)),
        closed=closed,
        project_item_id=project_item_id,
        fields=fields
        if fields is not None
        else {"Task ID": task_id, "Lane": "cloud", "Verify": "human"},
    )


class TestFieldsOnAnAlreadyBoardedIssue:
    """Changing a field on an issue the board already holds.

    Latent since D3 and only reachable here: `apply` learns board item ids
    from the `AddProjectItem` operations it performs in the same run, so a
    `SetProjectField` for an item added by an *earlier* run had no id to write
    to. Every prior live run either created the issue (id in hand) or changed
    only the body (no field op), so the path was never taken until a task's
    `verify` was edited in place — which then died with `KeyError`.
    """

    def _boarded(self) -> tuple[FakeGitHub, TaskGraph]:
        api = FakeGitHub()
        first = graph(task("ports", verify=Verify.HUMAN), plan=PLAN)
        execute_plan(plan_apply(first, api.fetch_state(plan=PLAN)), api)
        return api, graph(task("ports", verify=Verify.AUTO), plan=PLAN)

    def test_the_field_change_is_planned(self) -> None:
        api, changed = self._boarded()
        plan = plan_apply(changed, api.fetch_state(plan=PLAN))
        assert any(
            isinstance(op, SetProjectField) and op.field_name == "Verify"
            for op in plan.operations
        )

    def test_the_field_change_can_actually_be_executed(self) -> None:
        api, changed = self._boarded()
        plan = plan_apply(changed, api.fetch_state(plan=PLAN))
        result = execute_plan(plan, api)
        assert result.fields_set == 1

    def test_it_writes_to_the_item_the_board_already_had(self) -> None:
        # Not a new item: `apply` must never add a second board entry for an
        # issue that is already on it.
        api, changed = self._boarded()
        before = len(api.state.issues)
        execute_plan(plan_apply(changed, api.fetch_state(plan=PLAN)), api)
        assert len(api.state.issues) == before
        assert api.state.issues[0].fields["Verify"] == "auto"
        assert api.calls.count("add_project_item(100)") == 1

    def test_it_converges(self) -> None:
        api, changed = self._boarded()
        execute_plan(plan_apply(changed, api.fetch_state(plan=PLAN)), api)
        assert plan_apply(changed, api.fetch_state(plan=PLAN)).operations == ()
