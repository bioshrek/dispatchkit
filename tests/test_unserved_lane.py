"""A lane with no executor must not accept work.

`lane: local` is designed but unbuilt (D6). Until it exists, the dispatcher
labelled the task `dispatch:local` anyway and moved on, and the resolver reads
that label as a claim exactly as it reads an assignee — so the task reported
`Dispatched` and stayed there. Nothing ran it, nothing timed it out (the stall
reclaim needs an assignee to time out), and no notice mentioned it.

Three harms, all silent:

- the task never runs and never says so;
- it holds the only `caps.local` slot, so every other local task defers on
  `lane-cap` forever;
- while it is admitted it also claims its `touches`, so a *cloud* task that
  overlaps it is deferred on `file-scope-conflict` — permanently, by a task
  that does not exist.

The README already said local tasks "park" until D6. This is the code saying
the same thing. It is a report, not a repair: releasing marks left by an
earlier version is startup reconciliation, which belongs to D6 along with the
worktrees it reconciles against.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from dispatchkit.config import SchedulerConfig
from dispatchkit.github import OpenPlanPr
from dispatchkit.model import Lane
from dispatchkit.resolve import Status, admit, resolve
from dispatchkit.tick import SERVED_LANES, plan_tick, summarise
from tests.items import issue, item, items_of, ref, state_of

pytestmark = pytest.mark.unit

CONFIG = SchedulerConfig()
NOW = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)


class TestWhichLanesThisBuildCanServe:
    def test_cloud_is_served(self) -> None:
        assert Lane.CLOUD in SERVED_LANES

    def test_local_is_not_until_d6(self) -> None:
        # The one place that changes when the executor lands.
        assert Lane.LOCAL not in SERVED_LANES


class TestAnUnservedLaneIsDeferred:
    def test_a_local_task_is_not_dispatched(self) -> None:
        plan = plan_tick(state_of(issue("gpu", 1, lane=Lane.LOCAL)), config=CONFIG, now=NOW)
        assert plan.operations == ()

    def test_it_is_deferred_with_a_reason_that_names_the_gap(self) -> None:
        plan = plan_tick(state_of(issue("gpu", 1, lane=Lane.LOCAL)), config=CONFIG, now=NOW)
        assert [deferral.reason for deferral in plan.deferred] == ["no-executor"]

    def test_the_report_says_so(self) -> None:
        plan = plan_tick(state_of(issue("gpu", 1, lane=Lane.LOCAL)), config=CONFIG, now=NOW)
        assert "no-executor" in "\n".join(summarise(plan))

    def test_a_cloud_task_is_unaffected(self) -> None:
        plan = plan_tick(state_of(issue("a", 1)), config=CONFIG, now=NOW)
        assert len(plan.operations) == 1

    def test_a_local_task_no_longer_claims_its_touches(self) -> None:
        # The harm that is easiest to miss: an unrunnable local task was
        # admitted, so it took its file scope with it and deferred a real
        # cloud task that overlapped -- for as long as the mark stood.
        plan = plan_tick(
            state_of(
                issue("gpu", 1, lane=Lane.LOCAL, touches=("src/*",)),
                issue("cloud", 2, touches=("src/*",)),
            ),
            config=CONFIG,
            now=NOW,
        )
        assert [
            str(operation.ref)
            for operation in plan.operations
            if not isinstance(operation, OpenPlanPr)
        ] == ["demo/cloud"]

    def test_and_no_longer_eats_the_lane_cap(self) -> None:
        plan = admit(
            items_of(item("gpu-a", lane=Lane.LOCAL), item("gpu-b", lane=Lane.LOCAL)),
            {ref("gpu-a"): Status.READY, ref("gpu-b"): Status.READY},
            CONFIG,
            served=(),
        )
        assert {deferral.reason for deferral in plan.deferred} == {"no-executor"}

    def test_serving_the_lane_restores_ordinary_admission(self) -> None:
        # The same graph, once D6 exists: one runs, the second waits on the cap
        # that is now doing its real job.
        plan = admit(
            # Numbered explicitly: admission's tie-break is issue order, and
            # the synthetic default derives a number from `hash`, which the
            # interpreter seeds differently per run.
            items_of(
                item("gpu-a", number=1, lane=Lane.LOCAL),
                item("gpu-b", number=2, lane=Lane.LOCAL),
            ),
            {ref("gpu-a"): Status.READY, ref("gpu-b"): Status.READY},
            CONFIG,
            served=(Lane.LOCAL,),
        )
        assert plan.admitted == (ref("gpu-a"),)
        assert [deferral.reason for deferral in plan.deferred] == ["lane-cap"]


class TestAMarkLeftByAnEarlierVersion:
    """Repositories already carry these, and they are the loudest form of the bug."""

    def test_it_is_reported_rather_than_left_reading_as_dispatched(self) -> None:
        plan = plan_tick(
            state_of(issue("gpu", 1, lane=Lane.LOCAL, labels=("dispatchkit", "dispatch:local"))),
            config=CONFIG,
            now=NOW,
        )
        printed = "\n".join(str(notice) for notice in plan.notices)
        assert "unserved-lane-claim" in printed
        assert "#1" in printed

    def test_the_status_is_still_read_honestly_from_the_label(self) -> None:
        # The mark is on the issue, so `Dispatched` is what the repository
        # says. The notice is what makes it legible; rewriting the status
        # would be the report disagreeing with GitHub.
        statuses = resolve(
            items_of(item("gpu", lane=Lane.LOCAL, labels=("dispatchkit", "dispatch:local")))
        )
        assert statuses[ref("gpu")] is Status.DISPATCHED

    def test_nothing_is_mutated_to_clean_it_up(self) -> None:
        # Releasing an orphaned mark is startup reconciliation, and it needs
        # the worktrees to reconcile against. That is D6's, not this guard's.
        plan = plan_tick(
            state_of(issue("gpu", 1, lane=Lane.LOCAL, labels=("dispatchkit", "dispatch:local"))),
            config=CONFIG,
            now=NOW,
        )
        assert plan.operations == ()

    def test_a_cloud_dispatch_is_not_reported_as_one(self) -> None:
        plan = plan_tick(
            state_of(issue("a", 1, assignees=("copilot-swe-agent",))), config=CONFIG, now=NOW
        )
        assert not any("unserved-lane-claim" in str(notice) for notice in plan.notices)
