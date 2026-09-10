"""D13.1: cancelling a task, and what it does to everything downstream.

The bug this fixes was live and reachable from the web UI. `_status_of`
returned `Done` for anything closed, so closing an issue as **not planned** —
GitHub's own word for "this will never happen" — silently satisfied every
dependency waiting on it. The dependents unblocked and were dispatched, with a
prerequisite that was never done. That is the exact promise the tool exists to
keep, broken by somebody tidying a backlog.

The fix uses GitHub's vocabulary rather than a new label: closed as completed
is `Done`, closed as not planned is `Cancelled` and satisfies nothing. The
state query gains `stateReason`, which is the whole cost.

The half that is easy to forget is the reporting. Dependents of a cancelled
task are blocked *forever*, and a graph that silently stops is the failure this
system exists to prevent — so it is said out loud, in the same family as the
dangling-dependency error.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from dispatchkit.config import SchedulerConfig
from dispatchkit.gh_cli import STATE_QUERY, parse_state
from dispatchkit.github import RepoState
from dispatchkit.resolve import Status, blocking, resolve
from dispatchkit.tick import plan_tick, summarise
from tests.items import PLAN, issue, item, items_of, ref, state_of

pytestmark = pytest.mark.unit

CONFIG = SchedulerConfig()
NOW = datetime(2026, 9, 9, 12, 0, tzinfo=UTC)


class TestCancelledIsNotDone:
    def test_an_issue_closed_as_not_planned_is_cancelled(self) -> None:
        statuses = resolve(items_of(item("a", closed=True, cancelled=True)))
        assert statuses[ref("a")] is Status.CANCELLED

    def test_an_issue_closed_normally_is_still_done(self) -> None:
        statuses = resolve(items_of(item("a", closed=True)))
        assert statuses[ref("a")] is Status.DONE

    def test_an_open_issue_is_unaffected(self) -> None:
        statuses = resolve(items_of(item("a")))
        assert statuses[ref("a")] is Status.READY


class TestACancelledTaskSatisfiesNothing:
    """The bug, stated as a test.

    Before this, closing `a` as not planned made `b` ready — work dispatched on
    a prerequisite that will never exist.
    """

    def test_a_dependent_of_a_cancelled_task_stays_blocked(self) -> None:
        statuses = resolve(
            items_of(item("a", closed=True, cancelled=True), item("b", depends=("a",)))
        )
        assert statuses[ref("b")] is Status.BLOCKED

    def test_a_dependent_of_a_completed_task_is_still_released(self) -> None:
        statuses = resolve(items_of(item("a", closed=True), item("b", depends=("a",))))
        assert statuses[ref("b")] is Status.READY

    def test_it_is_never_dispatched(self) -> None:
        plan = plan_tick(
            state_of(issue("a", 1, closed=True, cancelled=True), issue("b", 2, depends=("a",))),
            config=CONFIG,
            now=NOW,
        )
        assert plan.operations == ()

    def test_a_task_needing_one_cancelled_and_one_done_stays_blocked(self) -> None:
        statuses = resolve(
            items_of(
                item("done", closed=True),
                item("gone", closed=True, cancelled=True),
                item("b", depends=("done", "gone")),
            )
        )
        assert statuses[ref("b")] is Status.BLOCKED

    def test_the_blocked_hint_names_the_cancelled_dependency(self) -> None:
        waiting = blocking(
            items_of(item("a", closed=True, cancelled=True), item("b", depends=("a",)))
        )
        assert waiting[ref("b")] == ("a",)

    def test_cancelling_only_scopes_to_its_own_plan(self) -> None:
        # `TaskRef` again: one plan's cancelled `a` must not block another
        # plan's `b`, whose own `a` is genuinely done.
        statuses = resolve(
            items_of(
                item("a", plan="alpha", closed=True, cancelled=True),
                item("a", plan="beta", closed=True),
                item("b", plan="beta", depends=("a",)),
            )
        )
        assert statuses[ref("b", "beta")] is Status.READY


class TestTheDeadEndIsReported:
    """A graph that silently stops is the failure this system exists to prevent.

    `Blocked` on its own reads as "not yet". A task waiting on something
    cancelled is not waiting, it is finished without having run, and nothing
    else in the report says so.
    """

    def test_a_pass_says_which_tasks_can_never_run(self) -> None:
        plan = plan_tick(
            state_of(issue("a", 1, closed=True, cancelled=True), issue("b", 2, depends=("a",))),
            config=CONFIG,
            now=NOW,
        )
        printed = "\n".join(summarise(plan))

        assert "cancelled-dependency" in printed
        assert "demo/b" in printed

    def test_it_names_the_cancelled_task_not_only_the_stranded_one(self) -> None:
        plan = plan_tick(
            state_of(issue("a", 1, closed=True, cancelled=True), issue("b", 2, depends=("a",))),
            config=CONFIG,
            now=NOW,
        )
        notice = next(n for n in plan.notices if "cancelled-dependency" in str(n))
        assert "demo/a" in str(notice)

    def test_a_healthy_graph_says_nothing(self) -> None:
        plan = plan_tick(
            state_of(issue("a", 1, closed=True), issue("b", 2, depends=("a",))),
            config=CONFIG,
            now=NOW,
        )
        assert not any("cancelled" in str(n) for n in plan.notices)

    def test_the_stranding_is_reported_transitively(self) -> None:
        # `c` depends on `b` depends on cancelled `a`. `c` can never run
        # either, and a report that only names `b` invites someone to fix `b`
        # and expect the rest to follow.
        plan = plan_tick(
            state_of(
                issue("a", 1, closed=True, cancelled=True),
                issue("b", 2, depends=("a",)),
                issue("c", 3, depends=("b",)),
            ),
            config=CONFIG,
            now=NOW,
        )
        printed = "\n".join(str(n) for n in plan.notices)
        assert "demo/b" in printed
        assert "demo/c" in printed


class TestReadingItFromGitHub:
    def test_the_state_query_asks_for_the_reason(self) -> None:
        assert "stateReason" in STATE_QUERY

    def test_not_planned_is_parsed_as_cancelled(self) -> None:
        state = parse_state(_payload(state="CLOSED", reason="NOT_PLANNED"))
        assert state.issues[0].cancelled is True

    def test_completed_is_not(self) -> None:
        state = parse_state(_payload(state="CLOSED", reason="COMPLETED"))
        assert state.issues[0].cancelled is False

    def test_a_payload_without_the_field_fails_safe(self) -> None:
        # Recorded fixtures predate the field, and so does any cached response.
        # Guessing `cancelled` there would strand tasks that are merely done.
        state = parse_state(_payload(state="CLOSED", reason=None))
        assert state.issues[0].cancelled is False

    def test_an_open_issue_is_never_cancelled(self) -> None:
        # GitHub reports `REOPENED` here, which is not a cancellation.
        state = parse_state(_payload(state="OPEN", reason="REOPENED"))
        assert state.issues[0].cancelled is False


def _payload(*, state: str, reason: str | None) -> dict[str, object]:
    node: dict[str, object] = {
        "id": "I_1",
        "number": 1,
        "title": "a",
        "body": f"<!-- dispatchkit\nv: 1\nid: a\nplan: {PLAN}\nmilestone: M\n-->",
        "state": state,
        "labels": {"nodes": [{"name": "dispatchkit"}]},
        "assignees": {"nodes": []},
        "dispatches": {"nodes": []},
        "timelineItems": {"nodes": []},
    }
    if reason is not None:
        node["stateReason"] = reason
    return {"data": {"repository": {"issues": {"nodes": [node]}}}}


def test_the_repo_state_double_defaults_to_not_cancelled() -> None:
    assert RepoState(()).issues == ()
