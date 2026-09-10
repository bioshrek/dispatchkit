"""D13.1c: `dispatch:hold`, the human's "not now".

The scheduler's whole job is to keep work moving, which makes "stop, but keep
it" the one thing it cannot express. `dispatch:hold` is that: a label a human
adds, from the CLI or the web UI or a phone, saying *not this task, not yet*.

Two things fall out of the design that are easy to get wrong.

A hold is a **status**, not a deferral. A deferral says "the scheduler declined
this pass" — a cap, a scope conflict — and is expected to clear itself. A hold
is a standing decision that will never clear on its own, and reporting it in
the same breath as a lane cap would invite the reader to wait for something
that is waiting for them.

And **a hold does not spend an attempt**. The retry budget exists to stop a
task that keeps failing. Holding is the human interrupting; the agent did not
fail, and charging it would let three interruptions burn a task's entire budget
and mark it `Stuck` — the scheduler punishing someone for using the control it
gave them.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from dispatchkit.config import SchedulerConfig
from dispatchkit.gh_cli import parse_state
from dispatchkit.github import LABEL_HOLD
from dispatchkit.resolve import (
    Status,
    build_items,
    merge_ops,
    ready_ops,
    resolve,
    stall_ops,
)
from dispatchkit.tick import plan_tick, summarise
from tests.items import PLAN, issue, item, items_of, ref, state_of

pytestmark = pytest.mark.unit

CONFIG = SchedulerConfig()
NOW = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
EARLIER = NOW - timedelta(days=2)


class TestAHeldTaskIsReportedAsHeld:
    def test_the_label_is_the_whole_mechanism(self) -> None:
        assert LABEL_HOLD == "dispatch:hold"

    def test_a_ready_task_with_the_label_is_held(self) -> None:
        statuses = resolve(items_of(item("a", labels=("dispatchkit", LABEL_HOLD))))
        assert statuses[ref("a")] is Status.HELD

    def test_without_it_the_same_task_is_ready(self) -> None:
        statuses = resolve(items_of(item("a")))
        assert statuses[ref("a")] is Status.READY

    def test_a_finished_task_is_still_done(self) -> None:
        # Holding something already closed is a no-op, not a resurrection.
        statuses = resolve(items_of(item("a", closed=True, labels=("dispatchkit", LABEL_HOLD))))
        assert statuses[ref("a")] is Status.DONE

    def test_it_outranks_stuck(self) -> None:
        # Both are true, but only one is a decision somebody made. Reading
        # `Stuck` over a held task blames the budget for a human's choice.
        statuses = resolve(
            items_of(item("a", labels=("dispatchkit", "dispatch:stuck", LABEL_HOLD)))
        )
        assert statuses[ref("a")] is Status.HELD


class TestAHeldTaskIsNotActedOn:
    """"Not now" has to mean the scheduler's hands come off entirely.

    Dispatch is the obvious one. Merging matters more: it is the only thing
    dispatchkit does that changes `main` without a human, and a hold that let
    it proceed would be the control failing at the moment it counts most.
    """

    def test_it_is_never_dispatched(self) -> None:
        plan = plan_tick(
            state_of(issue("a", 1, labels=("dispatchkit", LABEL_HOLD))),
            config=CONFIG,
            now=NOW,
        )
        assert plan.operations == ()

    def test_it_is_not_deferred_either(self) -> None:
        # A deferral is the scheduler's own choice and clears itself. A hold
        # will not, so listing it there would be an invitation to wait.
        plan = plan_tick(
            state_of(issue("a", 1, labels=("dispatchkit", LABEL_HOLD))),
            config=CONFIG,
            now=NOW,
        )
        assert plan.deferred == ()

    def test_a_held_pull_request_is_not_merged(self) -> None:
        from dispatchkit.model import Checks, PullRequest, Verify

        pr = PullRequest(7, Checks.PASSING, mergeable=True)
        held = item("a", verify=Verify.AUTO, open_prs=(pr,), labels=("dispatchkit", LABEL_HOLD))
        assert merge_ops(items_of(held), CONFIG) == ()

    def test_a_held_draft_is_not_marked_ready(self) -> None:
        from dispatchkit.model import Checks, PullRequest, Verify

        pr = PullRequest(7, Checks.PASSING, draft=True, mergeable=True)
        held = item("a", verify=Verify.AUTO, open_prs=(pr,), labels=("dispatchkit", LABEL_HOLD))
        assert ready_ops(items_of(held)) == ()

    def test_a_held_task_is_not_marked_stuck_for_stalling(self) -> None:
        # The clock keeps running while a task is held. Reclaiming it as a
        # stall would be the scheduler timing out its own instructions.
        held = item(
            "a",
            assignees=("copilot-swe-agent",),
            dispatches=(EARLIER,),
            labels=("dispatchkit", LABEL_HOLD),
        )
        assert stall_ops(items_of(held), CONFIG, NOW) == ()

    def test_dependents_of_a_held_task_wait(self) -> None:
        statuses = resolve(
            items_of(item("a", labels=("dispatchkit", LABEL_HOLD)), item("b", depends=("a",)))
        )
        assert statuses[ref("b")] is Status.BLOCKED


class TestAHoldDoesNotSpendAnAttempt:
    """The budget counts failures, and an interruption is not one."""

    def test_a_dispatch_ended_by_a_hold_is_not_charged(self) -> None:
        task = item("a", dispatches=(EARLIER,), holds=(EARLIER + timedelta(hours=1),))
        assert task.attempts == 0

    def test_a_dispatch_with_no_hold_after_it_still_is(self) -> None:
        task = item("a", dispatches=(EARLIER,))
        assert task.attempts == 1

    def test_a_hold_before_the_dispatch_charges_nothing_back(self) -> None:
        # Held, then released, then dispatched, and that run failed. The hold
        # is spent; it does not discount an attempt that came after it.
        task = item("a", dispatches=(NOW,), holds=(EARLIER,))
        assert task.attempts == 1

    def test_only_the_dispatch_it_interrupted_is_forgiven(self) -> None:
        # Two failures, then a hold. The hold covers the second run only.
        task = item(
            "a",
            dispatches=(EARLIER, EARLIER + timedelta(hours=2)),
            holds=(EARLIER + timedelta(hours=3),),
        )
        assert task.attempts == 1

    def test_holding_repeatedly_never_exhausts_the_budget(self) -> None:
        # The failure this prevents: three interruptions marking a perfectly
        # healthy task `Stuck`, with the label as the only evidence.
        stamps = tuple(EARLIER + timedelta(hours=n) for n in range(0, 12, 4))
        task = item(
            "a",
            dispatches=stamps,
            holds=tuple(stamp + timedelta(hours=1) for stamp in stamps),
        )
        assert task.attempts == 0
        assert not task.stuck


class TestTheDiscountSurvivesTheRoundTrip:
    """The gap the unit tests above cannot see.

    `attempts` is right on a hand-built `TaskItem` and still wrong in
    production if `build_items` drops the field on the way past — which it did,
    until this test.
    """

    def test_a_pass_sees_the_holds_on_the_issue(self) -> None:
        stamps = tuple(EARLIER + timedelta(hours=n) for n in range(0, 12, 4))
        state = state_of(
            issue(
                "a",
                1,
                dispatches=stamps,
                holds=tuple(stamp + timedelta(hours=1) for stamp in stamps),
            )
        )
        items, _ = build_items(state)
        assert items[0].attempts == 0

    def test_and_so_the_budget_is_not_exhausted(self) -> None:
        stamps = tuple(EARLIER + timedelta(hours=n) for n in range(0, 12, 4))
        plan = plan_tick(
            state_of(
                issue(
                    "a",
                    1,
                    dispatches=stamps,
                    holds=tuple(stamp + timedelta(hours=1) for stamp in stamps),
                )
            ),
            config=CONFIG,
            now=NOW,
        )
        assert len(plan.operations) == 1
        assert plan.deferred == ()


class TestReadingHoldsFromGitHub:
    def test_a_hold_label_event_is_recorded(self) -> None:
        state = parse_state(_payload(labelled=[("dispatch:hold", "2026-09-10T09:00:00Z")]))
        assert state.issues[0].holds == (datetime(2026, 9, 10, 9, 0, tzinfo=UTC),)

    def test_other_labels_are_not(self) -> None:
        state = parse_state(_payload(labelled=[("dispatch:stuck", "2026-09-10T09:00:00Z")]))
        assert state.issues[0].holds == ()

    def test_a_payload_without_the_field_reads_as_no_holds(self) -> None:
        state = parse_state(_payload(labelled=None))
        assert state.issues[0].holds == ()


class TestTheReportSaysSo:
    def test_a_held_task_appears_as_held(self) -> None:
        plan = plan_tick(
            state_of(issue("a", 1, labels=("dispatchkit", LABEL_HOLD))),
            config=CONFIG,
            now=NOW,
        )
        assert "Held" in "\n".join(summarise(plan))

    def test_releasing_it_is_the_next_pass_and_nothing_else(self) -> None:
        # No state was written when it was held, so removing the label is the
        # entire release procedure.
        released = plan_tick(state_of(issue("a", 1)), config=CONFIG, now=NOW)
        assert len(released.operations) == 1


def _payload(*, labelled: list[tuple[str, str]] | None) -> dict[str, object]:
    node: dict[str, object] = {
        "id": "I_1",
        "number": 1,
        "title": "a",
        "body": f"<!-- dispatchkit\nv: 1\nid: a\nplan: {PLAN}\nmilestone: M\n-->",
        "state": "OPEN",
        "labels": {"nodes": [{"name": "dispatchkit"}]},
        "assignees": {"nodes": []},
        "dispatches": {"nodes": []},
        "timelineItems": {"nodes": []},
    }
    if labelled is not None:
        node["holds"] = {
            "nodes": [
                {"label": {"name": name}, "createdAt": created} for name, created in labelled
            ]
        }
    return {"data": {"repository": {"issues": {"nodes": [node]}}}}
