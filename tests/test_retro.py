"""D15: what a finished plan reports about itself.

D2 deleted `estimate_minutes` because the graph is the one place its quantity
cannot be known, and promised the number would return as an observation. This
is the observation. Nothing here is stored, nothing is declared, and every
figure comes from timestamps the repository already keeps for its own reasons.

`metrics.py` is the forecast: what the graph *allows*, in depth and maximum
antichain. This module is the outcome, and the pairing is the point. A plan of
width five that never ran two tasks at once was serial in practice, and no
forecast can tell you that -- but nor can an outcome printed on its own, since
the reader is left to remember what was expected. Promise and outcome belong
on the same page.

Two durations carry the whole argument:

**Overhead** is dispatch to first commit, plus CI. It is the price of handing
a task to an agent at all, and every split pays it again in full.

**Work** is dispatch to close: the elapsed span of the attempt that succeeded.

A task whose work is not comfortably larger than its overhead did not earn its
own dispatch -- which is exactly what `under-economic-floor` tried to decide in
advance, from a number the planner had invented.

The recurring hazard, and most of what these tests are about, is the absent
value. A hand-closed issue, a merge with no commit shown, a payload recorded
before the fields existed: each yields a missing timestamp, and each must stay
missing. A zero is a task somebody finished instantly, and one of those in the
set drags the median toward work nobody did.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from dispatchkit.github import IssueState
from dispatchkit.model import DEFAULT_BASE, MergedPr
from dispatchkit.resolve import build_items
from dispatchkit.retro import Retrospective, retrospective
from tests.graphs import graph, task
from tests.items import issue, state_of

pytestmark = pytest.mark.unit


def at(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 9, 10, hour, minute, tzinfo=UTC)


def landed(
    name: str,
    number: int,
    *,
    dispatched: tuple[datetime, ...] = (),
    first_commit: datetime | None = None,
    ci: timedelta | None = None,
    merged_at: datetime | None = None,
    closed_at: datetime | None = None,
    depends: tuple[str, ...] = (),
) -> IssueState:
    """A closed task whose work merged, with the timings the retro reads."""
    return issue(
        name,
        number=number,
        depends=depends,
        closed=True,
        closed_at=closed_at,
        dispatches=dispatched,
        merged=(
            MergedPr(
                number + 100,
                DEFAULT_BASE,
                first_commit_at=first_commit,
                ci=ci,
                merged_at=merged_at,
            ),
        ),
    )


def retro_of(
    *issues: IssueState, tasks: tuple[str, ...] = (), plan: str = "demo"
) -> Retrospective:
    items, _ = build_items(state_of(*issues))
    shape = graph(*(task(name) for name in tasks)) if tasks else None
    return retrospective(items, plan=plan, graph=shape)


class TestTheTwoDurations:
    def test_overhead_is_dispatch_to_first_commit_plus_ci(self) -> None:
        report = retro_of(
            landed(
                "one",
                1,
                dispatched=(at(12, 0),),
                first_commit=at(12, 7),
                ci=timedelta(minutes=3),
                closed_at=at(12, 40),
            )
        )

        assert report.outcomes[0].overhead == timedelta(minutes=10)

    def test_work_is_dispatch_to_close(self) -> None:
        report = retro_of(
            landed(
                "one",
                1,
                dispatched=(at(12, 0),),
                first_commit=at(12, 7),
                ci=timedelta(minutes=3),
                closed_at=at(12, 40),
            )
        )

        assert report.outcomes[0].work == timedelta(minutes=40)

    def test_overhead_is_measured_on_the_last_dispatch(self) -> None:
        """A retry pays the overhead again; it does not make the task bigger.

        Counting from the first dispatch would report two overheads as one and
        make every retried task look enormous. The retries are not lost -- they
        are `attempts`, which is where a failure belongs.
        """
        report = retro_of(
            landed(
                "one",
                1,
                dispatched=(at(9, 0), at(12, 0)),
                first_commit=at(12, 7),
                ci=timedelta(minutes=3),
                closed_at=at(12, 40),
            )
        )

        assert report.outcomes[0].overhead == timedelta(minutes=10)
        assert report.outcomes[0].work == timedelta(minutes=40)
        assert report.outcomes[0].attempts == 2

    def test_the_dispatch_is_the_one_the_work_came_from(self) -> None:
        """Found live, on `mincount/min-count`.

        Taking the *last* dispatch assumes every dispatch produced something.
        That task was merged, then re-dispatched by the bug D16's live trial
        found, then closed -- so its last dispatch came after its only commit,
        and the retrospective reported an overhead of minus two hours.

        The right question is which run produced the work that landed, and the
        commit answers it: the last dispatch at or before the first commit.
        """
        report = retro_of(
            landed(
                "one",
                1,
                dispatched=(at(12, 0), at(14, 0)),
                first_commit=at(12, 7),
                ci=timedelta(minutes=3),
                closed_at=at(14, 5),
            )
        )

        assert report.outcomes[0].overhead == timedelta(minutes=10)
        assert report.outcomes[0].work == timedelta(hours=2, minutes=5)

    def test_a_duration_that_comes_out_negative_is_unmeasured_not_negative(self) -> None:
        """Belt and braces for the same class of surprise.

        No arrangement of real events produces a commit before the dispatch
        that caused it, so if the arithmetic says otherwise the premise is
        wrong and the honest report is that nothing could be measured. A
        negative overhead printed as a number invites somebody to average it.
        """
        report = retro_of(
            landed(
                "one",
                1,
                dispatched=(at(14, 0),),
                first_commit=at(12, 7),
                ci=timedelta(minutes=3),
                closed_at=at(14, 5),
            )
        )

        assert report.outcomes[0].overhead is None
        assert report.outcomes[0].measured is False

    def test_ci_that_was_never_recorded_is_not_counted_as_zero(self) -> None:
        report = retro_of(
            landed("one", 1, dispatched=(at(12, 0),), first_commit=at(12, 7), closed_at=at(12, 40))
        )

        assert report.outcomes[0].overhead is None
        assert report.outcomes[0].measured is False


class TestWhereWorkEnds:
    """Work ends when the work landed, not when the bookkeeping caught up.

    Before D16 the two were the same moment: GitHub closed the issue in the
    instant it merged the pull request. Now dispatchkit closes it itself, on a
    later pass, so `closedAt` carries up to one poll interval of the
    scheduler's own latency -- and on a hand-driven `watch --once`, up to
    however long the operator was away. That is not the task's duration.
    """

    def test_it_ends_at_the_merge_when_there_is_one(self) -> None:
        report = retro_of(
            landed(
                "one",
                1,
                dispatched=(at(12, 0),),
                first_commit=at(12, 5),
                ci=timedelta(minutes=5),
                merged_at=at(12, 40),
                closed_at=at(15, 0),
            )
        )

        assert report.outcomes[0].work == timedelta(minutes=40)

    def test_it_falls_back_to_the_close_when_nothing_merged(self) -> None:
        """A hand-finished task has no merge, and the close is all there is."""
        report = retro_of(
            landed(
                "one",
                1,
                dispatched=(at(12, 0),),
                first_commit=at(12, 5),
                ci=timedelta(minutes=5),
                closed_at=at(12, 40),
            )
        )

        assert report.outcomes[0].work == timedelta(minutes=40)


class TestWhatCannotBeMeasured:
    def test_a_task_closed_by_hand_has_no_overhead(self) -> None:
        """No dispatch, no merge -- somebody just closed it."""
        report = retro_of(issue("one", number=1, closed=True, closed_at=at(12, 40)))

        assert report.outcomes[0].overhead is None
        assert report.outcomes[0].work is None

    def test_an_unmeasurable_task_is_counted_and_excluded(self) -> None:
        report = retro_of(
            landed(
                "one",
                1,
                dispatched=(at(12, 0),),
                first_commit=at(12, 7),
                ci=timedelta(minutes=3),
                closed_at=at(12, 40),
            ),
            issue("two", number=2, closed=True, closed_at=at(12, 40)),
        )

        assert report.measured == 1
        assert report.unmeasured == 1

    def test_a_plan_with_nothing_measurable_reports_no_median(self) -> None:
        """Rather than reporting a zero, or refusing to run at all."""
        report = retro_of(issue("one", number=1, closed=True, closed_at=at(12, 40)))

        assert report.median_overhead is None
        assert report.measured == 0

    def test_an_unfinished_task_is_not_measured(self) -> None:
        report = retro_of(issue("one", number=1, dispatches=(at(12, 0),)))

        assert report.outcomes[0].measured is False
        assert report.unmeasured == 1


class TestTheFloor:
    def test_a_task_whose_work_barely_exceeded_its_overhead_did_not_earn_it(self) -> None:
        report = retro_of(
            landed(
                "one",
                1,
                dispatched=(at(12, 0),),
                first_commit=at(12, 5),
                ci=timedelta(minutes=5),
                closed_at=at(12, 15),
            )
        )

        assert report.below_floor == 1

    def test_a_task_that_worked_for_hours_earned_its_dispatch(self) -> None:
        report = retro_of(
            landed(
                "one",
                1,
                dispatched=(at(12, 0),),
                first_commit=at(12, 5),
                ci=timedelta(minutes=5),
                closed_at=at(16, 0),
            )
        )

        assert report.below_floor == 0

    def test_the_multiple_is_the_callers(self) -> None:
        """The floor is a lens, not a verdict, so the reader may change it."""
        early = landed(
            "one",
            1,
            dispatched=(at(12, 0),),
            first_commit=at(12, 5),
            ci=timedelta(minutes=5),
            closed_at=at(12, 25),
        )
        items, _ = build_items(state_of(early))

        assert retrospective(items, plan="demo", floor_multiple=2.0).below_floor == 0
        assert retrospective(items, plan="demo", floor_multiple=3.0).below_floor == 1

    def test_the_median_overhead_is_what_replaces_the_guess(self) -> None:
        """`overhead_minutes = 10` was a constant nobody had measured."""
        report = retro_of(
            landed(
                "one",
                1,
                dispatched=(at(12, 0),),
                first_commit=at(12, 4),
                ci=timedelta(minutes=2),
                closed_at=at(13, 0),
            ),
            landed(
                "two",
                2,
                dispatched=(at(12, 0),),
                first_commit=at(12, 10),
                ci=timedelta(minutes=2),
                closed_at=at(13, 0),
            ),
        )

        assert report.median_overhead == timedelta(minutes=9)


class TestWidthPromisedAgainstWidthAchieved:
    def test_tasks_that_never_overlapped_ran_one_at_a_time(self) -> None:
        report = retro_of(
            landed(
                "one",
                1,
                dispatched=(at(9, 0),),
                first_commit=at(9, 5),
                ci=timedelta(minutes=2),
                closed_at=at(10, 0),
            ),
            landed(
                "two",
                2,
                dispatched=(at(11, 0),),
                first_commit=at(11, 5),
                ci=timedelta(minutes=2),
                closed_at=at(12, 0),
            ),
        )

        assert report.achieved_width == 1

    def test_overlapping_tasks_are_counted_at_their_peak(self) -> None:
        report = retro_of(
            landed(
                "one",
                1,
                dispatched=(at(9, 0),),
                first_commit=at(9, 5),
                ci=timedelta(minutes=2),
                closed_at=at(12, 0),
            ),
            landed(
                "two",
                2,
                dispatched=(at(10, 0),),
                first_commit=at(10, 5),
                ci=timedelta(minutes=2),
                closed_at=at(11, 0),
            ),
            landed(
                "three",
                3,
                dispatched=(at(10, 30),),
                first_commit=at(10, 35),
                ci=timedelta(minutes=2),
                closed_at=at(13, 0),
            ),
        )

        assert report.achieved_width == 3

    def test_a_task_that_closes_as_another_opens_is_not_an_overlap(self) -> None:
        """Touching intervals are consecutive, not concurrent.

        Off by one here would report a perfectly serial plan as having run two
        wide, which is the exact claim this number exists to refuse.
        """
        report = retro_of(
            landed(
                "one",
                1,
                dispatched=(at(9, 0),),
                first_commit=at(9, 5),
                ci=timedelta(minutes=2),
                closed_at=at(10, 0),
            ),
            landed(
                "two",
                2,
                dispatched=(at(10, 0),),
                first_commit=at(10, 5),
                ci=timedelta(minutes=2),
                closed_at=at(11, 0),
            ),
        )

        assert report.achieved_width == 1

    def test_the_promised_width_comes_from_the_graph(self) -> None:
        report = retro_of(
            landed(
                "one",
                1,
                dispatched=(at(9, 0),),
                first_commit=at(9, 5),
                ci=timedelta(minutes=2),
                closed_at=at(10, 0),
            ),
            landed(
                "two",
                2,
                dispatched=(at(11, 0),),
                first_commit=at(11, 5),
                ci=timedelta(minutes=2),
                closed_at=at(12, 0),
            ),
            tasks=("one", "two"),
        )

        assert report.promised_width == 2
        assert report.achieved_width == 1

    def test_without_a_graph_the_promise_is_unknown_rather_than_one(self) -> None:
        report = retro_of(
            landed(
                "one",
                1,
                dispatched=(at(9, 0),),
                first_commit=at(9, 5),
                ci=timedelta(minutes=2),
                closed_at=at(10, 0),
            )
        )

        assert report.promised_width is None


class TestWhatTheWholePlanCost:
    def test_makespan_runs_from_the_first_dispatch_to_the_last_close(self) -> None:
        report = retro_of(
            landed(
                "one",
                1,
                dispatched=(at(9, 0),),
                first_commit=at(9, 5),
                ci=timedelta(minutes=2),
                closed_at=at(10, 0),
            ),
            landed(
                "two",
                2,
                dispatched=(at(11, 0),),
                first_commit=at(11, 5),
                ci=timedelta(minutes=2),
                closed_at=at(12, 30),
            ),
        )

        assert report.makespan == timedelta(hours=3, minutes=30)

    def test_serial_time_is_what_the_same_work_would_have_cost_in_a_queue(self) -> None:
        report = retro_of(
            landed(
                "one",
                1,
                dispatched=(at(9, 0),),
                first_commit=at(9, 5),
                ci=timedelta(minutes=2),
                closed_at=at(10, 0),
            ),
            landed(
                "two",
                2,
                dispatched=(at(9, 30),),
                first_commit=at(9, 35),
                ci=timedelta(minutes=2),
                closed_at=at(10, 30),
            ),
        )

        assert report.serial == timedelta(hours=2)
        assert report.makespan == timedelta(hours=1, minutes=30)


class TestItOnlyLooksAtOnePlan:
    def test_another_plans_tasks_are_not_in_the_report(self) -> None:
        """`watch` pools every plan in the repository (D13); a retrospective
        is about one of them, and mixing two would average across decisions
        nobody made together."""
        report = retro_of(
            landed(
                "one",
                1,
                dispatched=(at(9, 0),),
                first_commit=at(9, 5),
                ci=timedelta(minutes=2),
                closed_at=at(10, 0),
            ),
            issue("other", number=9, plan="mincount", closed=True, closed_at=at(10, 0)),
            plan="demo",
        )

        assert [str(outcome.ref.id) for outcome in report.outcomes] == ["one"]


class TestCancelledWork:
    def test_a_cancelled_task_is_excluded_rather_than_counted_as_finished(self) -> None:
        """It was closed, but it was never done (D13.1).

        Averaging it in would report the plan as having completed work that
        was abandoned, and its `work` duration would measure how long somebody
        took to give up.
        """
        report = retro_of(
            landed(
                "one",
                1,
                dispatched=(at(9, 0),),
                first_commit=at(9, 5),
                ci=timedelta(minutes=2),
                closed_at=at(10, 0),
            ),
            issue("two", number=2, closed=True, cancelled=True, closed_at=at(10, 0)),
        )

        assert [str(outcome.ref.id) for outcome in report.outcomes] == ["one"]
        assert report.cancelled == 1


class TestRetryRate:
    def test_it_counts_the_tasks_that_needed_more_than_one_go(self) -> None:
        report = retro_of(
            landed(
                "one",
                1,
                dispatched=(at(9, 0), at(9, 30)),
                first_commit=at(9, 35),
                ci=timedelta(minutes=2),
                closed_at=at(10, 0),
            ),
            landed(
                "two",
                2,
                dispatched=(at(9, 0),),
                first_commit=at(9, 5),
                ci=timedelta(minutes=2),
                closed_at=at(10, 0),
            ),
        )

        assert report.retried == 1
