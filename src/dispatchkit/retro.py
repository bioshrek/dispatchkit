"""The plan retrospective (D15).

Pure functions over a resolved snapshot. `metrics.py` is this module's twin:
it reports what a graph *allows* — depth, and the maximum antichain — from the
graph alone, before anything runs. This reports what happened, from the issue
timeline, after everything has. Neither is much use without the other. A plan
of width five that never ran two tasks at once was serial in practice, which no
forecast can tell you; and an outcome printed on its own leaves the reader to
remember what was expected.

D2 deleted `estimate_minutes` on the argument that the graph is the one place
its quantity cannot be known, and promised the same number would come back as
an observation. Everything here is that observation: derived from timestamps
GitHub keeps for its own reasons, in the same way `Attempts` and `Status`
already are. No schema key, no planner input, nothing stored.

It reports and does not gate. Feeding an observed median back into `validate`
would recreate the lint that was just deleted — better numbers, same failure
mode: a plan refused for missing a threshold derived from plans that are not
this one. The audience is a human folding the figures into the planner skill.

The recurring hazard is the absent value. A hand-closed issue, a merge with no
commit GitHub will show us, a snapshot recorded before the fields existed:
each yields a missing timestamp, and each stays missing. A zero is a task
somebody finished instantly, and one of those in the set drags every median
toward work nobody did. So `None` propagates, unmeasurable tasks are counted
separately, and the count is printed — a retrospective covering four of twelve
tasks is a different claim from one covering all twelve.
"""

from __future__ import annotations

import statistics
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from dispatchkit.metrics import max_antichain
from dispatchkit.model import TaskGraph, TaskRef
from dispatchkit.resolve import TaskItem

#: How many times its own overhead a task must have worked for the split to
#: have paid. A lens rather than a verdict — the caller may change it, and
#: nothing refuses a plan for failing it.
DEFAULT_FLOOR_MULTIPLE = 2.0


@dataclass(frozen=True, slots=True)
class TaskOutcome:
    """One task, as the timeline remembers it."""

    ref: TaskRef
    attempts: int
    #: The dispatch the work actually came from: the last one at or before
    #: the first commit. A retry pays the overhead again rather than making
    #: the task bigger, so counting from the *first* dispatch would report two
    #: overheads as one enormous task; the retries are not lost, they are
    #: `attempts`, which is where a failure belongs.
    #:
    #: Taking the plain last dispatch was the first version, and it assumed
    #: every dispatch produced something. Found live on a task that was
    #: merged, re-dispatched by the bug D16's trial turned up, and then
    #: closed: its last dispatch came after its only commit, and the report
    #: said the overhead was minus two hours. The commit is the evidence for
    #: which run did the work, so the commit chooses.
    dispatched_at: datetime | None
    first_commit_at: datetime | None
    ci: timedelta | None
    #: When the work landed, if it did. Preferred to `closed_at` as the end of
    #: `work`: since D16 the close is dispatchkit's own act on a later pass,
    #: so it carries a poll interval of scheduler latency -- and on a
    #: hand-driven `watch --once`, however long the operator was away. That is
    #: not the task's duration. Before D16 the two were the same moment,
    #: because GitHub closed the issue in the instant it merged.
    merged_at: datetime | None
    closed_at: datetime | None

    @property
    def finished_at(self) -> datetime | None:
        """When the task was done. The merge, or failing that the close.

        A task finished by hand has no merge, and then the close is all there
        is -- one imperfect answer being better than no answer.
        """
        return self.merged_at or self.closed_at

    @property
    def overhead(self) -> timedelta | None:
        """Dispatch to first commit, plus CI: the price of a dispatch.

        Every split pays this again in full, which is the entire argument
        against splitting a serial chain.
        """
        if self.dispatched_at is None or self.first_commit_at is None or self.ci is None:
            return None
        return _positive((self.first_commit_at - self.dispatched_at) + self.ci)

    @property
    def work(self) -> timedelta | None:
        """Dispatch to landing: the elapsed span of the attempt that succeeded."""
        finished = self.finished_at
        if self.dispatched_at is None or finished is None:
            return None
        return _positive(finished - self.dispatched_at)

    @property
    def measured(self) -> bool:
        return self.overhead is not None and self.work is not None

    def earned_its_dispatch(self, multiple: float) -> bool | None:
        """Did this task work for enough longer than it cost to start?

        `None` when it cannot be told, so a caller cannot mistake "unknown"
        for "no" and report an unmeasured plan as a badly split one.
        """
        overhead, work = self.overhead, self.work
        if overhead is None or work is None:
            return None
        return work.total_seconds() > multiple * overhead.total_seconds()


@dataclass(frozen=True, slots=True)
class Retrospective:
    plan: str
    outcomes: tuple[TaskOutcome, ...]
    floor_multiple: float
    #: The maximum antichain of the graph, when one was supplied: how wide the
    #: plan was *allowed* to run. `None` rather than 1 when there is no graph
    #: to ask, because "not asked" and "one at a time" are opposite readings.
    promised_width: int | None
    #: Tasks closed as not planned. Counted rather than averaged in: they were
    #: closed but never done, and their `work` would measure how long somebody
    #: took to give up.
    cancelled: int

    @property
    def measured(self) -> int:
        return sum(1 for outcome in self.outcomes if outcome.measured)

    @property
    def unmeasured(self) -> int:
        return sum(1 for outcome in self.outcomes if not outcome.measured)

    @property
    def retried(self) -> int:
        return sum(1 for outcome in self.outcomes if outcome.attempts > 1)

    @property
    def below_floor(self) -> int:
        """Tasks that did not work for `floor_multiple` times their overhead."""
        return sum(
            1
            for outcome in self.outcomes
            if outcome.earned_its_dispatch(self.floor_multiple) is False
        )

    @property
    def median_overhead(self) -> timedelta | None:
        """The number that replaces `overhead_minutes = 10`.

        A median rather than a mean: one task that sat in a queue overnight
        would otherwise set the constant for every plan after it.
        """
        overheads = [o.overhead for o in self.outcomes if o.overhead is not None]
        if not overheads:
            return None
        return timedelta(seconds=statistics.median(o.total_seconds() for o in overheads))

    @property
    def median_work(self) -> timedelta | None:
        works = [o.work for o in self.outcomes if o.work is not None]
        if not works:
            return None
        return timedelta(seconds=statistics.median(w.total_seconds() for w in works))

    @property
    def makespan(self) -> timedelta | None:
        """First dispatch to last landing: what the plan actually took."""
        spans = _spans(self.outcomes)
        if not spans:
            return None
        return max(end for _, end in spans) - min(start for start, _ in spans)

    @property
    def serial(self) -> timedelta | None:
        """The same work one at a time, which is what the width bought against."""
        works = [o.work for o in self.outcomes if o.work is not None]
        if not works:
            return None
        return sum(works, timedelta())

    @property
    def achieved_width(self) -> int:
        """The most tasks ever in flight at one moment.

        A sweep over the dispatch-to-close intervals. Closes are processed
        before dispatches at the same instant, because a task that closes as
        another opens is consecutive, not concurrent — the off-by-one here
        would report a perfectly serial plan as having run two wide, which is
        the exact claim this number exists to refuse.
        """
        events: list[tuple[datetime, int]] = []
        for start, end in _spans(self.outcomes):
            events.append((start, 1))
            events.append((end, -1))
        peak = live = 0
        for _, delta in sorted(events, key=lambda event: (event[0], event[1])):
            live += delta
            peak = max(peak, live)
        return peak

    @property
    def speedup(self) -> float | None:
        """How much the parallelism was worth, as a multiple of running serially."""
        serial, makespan = self.serial, self.makespan
        if serial is None or makespan is None or not makespan.total_seconds():
            return None
        return serial.total_seconds() / makespan.total_seconds()


def retrospective(
    items: Sequence[TaskItem],
    *,
    plan: str,
    graph: TaskGraph | None = None,
    floor_multiple: float = DEFAULT_FLOOR_MULTIPLE,
) -> Retrospective:
    """What one plan's finished tasks say about how it was decomposed.

    One plan, because `watch` pools every plan in the repository (D13) and
    averaging across two would blend decisions nobody made together.
    """
    mine = [item for item in items if item.ref.plan == plan]
    return Retrospective(
        plan=plan,
        outcomes=tuple(_outcome(item) for item in mine if not item.cancelled),
        floor_multiple=floor_multiple,
        promised_width=len(max_antichain(graph)) if graph is not None else None,
        cancelled=sum(1 for item in mine if item.cancelled),
    )


def _positive(span: timedelta) -> timedelta | None:
    """A duration, or `None` if the arithmetic came out backwards.

    No arrangement of real events produces a commit before the dispatch that
    caused it, so a negative span means the premise was wrong and the honest
    answer is that nothing could be measured. Printing it as a number invites
    somebody to average it.
    """
    return span if span >= timedelta() else None


def _outcome(item: TaskItem) -> TaskOutcome:
    landed = next(iter(item.merged), None)
    first_commit = landed.first_commit_at if landed else None
    return TaskOutcome(
        ref=item.ref,
        attempts=item.attempts,
        dispatched_at=_producing_dispatch(item.dispatches, first_commit),
        first_commit_at=first_commit,
        ci=landed.ci if landed else None,
        merged_at=landed.merged_at if landed else None,
        closed_at=item.closed_at,
    )


def _producing_dispatch(
    dispatches: Sequence[datetime], first_commit: datetime | None
) -> datetime | None:
    """The dispatch whose run produced the work that landed.

    The last one at or before the first commit. With no commit to go on there
    is nothing to choose against, so the last dispatch stands -- which is the
    only answer available and is right whenever the task simply has not
    finished yet.
    """
    if not dispatches:
        return None
    if first_commit is None:
        return max(dispatches)
    before = [stamp for stamp in dispatches if stamp <= first_commit]
    return max(before) if before else max(dispatches)


def _spans(outcomes: Sequence[TaskOutcome]) -> list[tuple[datetime, datetime]]:
    return [
        (outcome.dispatched_at, finished)
        for outcome in outcomes
        if outcome.dispatched_at is not None and (finished := outcome.finished_at) is not None
    ]


def summarise(report: Retrospective) -> tuple[str, ...]:
    """The report, as lines. Pure, like `tick.summarise`, so it is assertable.

    Promise and outcome sit on the same line wherever both exist. Anything
    that could not be measured is named rather than dropped, and no figure is
    printed at all when its inputs are missing -- a zero here would be read as
    a measurement.
    """
    lines = [
        f"retrospective for plan `{report.plan}`: "
        f"{report.measured} measured, {report.unmeasured} unmeasured"
        + (f", {report.cancelled} cancelled" if report.cancelled else ""),
        "",
    ]
    lines += _task_lines(report)
    lines.append("")
    lines += _summary_lines(report)
    return tuple(lines)


def _task_lines(report: Retrospective) -> list[str]:
    if not report.outcomes:
        return []
    width = max(len(str(outcome.ref.id)) for outcome in report.outcomes)
    lines = []
    for outcome in report.outcomes:
        earned = outcome.earned_its_dispatch(report.floor_multiple)
        lines.append(
            f"  {str(outcome.ref.id):<{width}}  "
            f"overhead {_duration(outcome.overhead):>7}  "
            f"work {_duration(outcome.work):>7}"
            + (f"  x{_ratio(outcome)}" if earned is not None else "  unmeasured")
            + ("" if earned is not False else "  <- below the floor")
            + (f"  ({outcome.attempts} attempts)" if outcome.attempts > 1 else "")
        )
    return lines


def _summary_lines(report: Retrospective) -> list[str]:
    lines = []
    if report.median_overhead is not None:
        lines.append(f"  median overhead   {_duration(report.median_overhead)}")
    if report.median_work is not None:
        lines.append(f"  median work       {_duration(report.median_work)}")
    if report.makespan is not None and report.serial is not None:
        speedup = report.speedup
        lines.append(
            f"  makespan          {_duration(report.makespan)}"
            f"  (serial {_duration(report.serial)}"
            + (f", so width bought {speedup:.1f}x)" if speedup is not None else ")")
        )
    promised = report.promised_width
    lines.append(
        "  width             "
        + (f"promised {promised}, " if promised is not None else "")
        + f"achieved {report.achieved_width}"
    )
    if report.measured:
        lines.append(
            f"  below the floor   {report.below_floor} of {report.measured} measured "
            f"worked less than {report.floor_multiple:g}x their own overhead"
        )
    if report.retried:
        lines.append(f"  retried           {report.retried} of {len(report.outcomes)}")
    return lines


def _ratio(outcome: TaskOutcome) -> str:
    overhead, work = outcome.overhead, outcome.work
    if overhead is None or work is None or not overhead.total_seconds():
        return "?"
    return f"{work.total_seconds() / overhead.total_seconds():.1f}"


def _duration(span: timedelta | None) -> str:
    """Whole units, largest two. Seconds are noise at this scale.

    `-` rather than `0m` for an absent value: the difference between "not
    measured" and "instant" is the one this module exists to preserve.
    """
    if span is None:
        return "-"
    total = int(span.total_seconds())
    hours, rest = divmod(total, 3600)
    minutes = rest // 60
    if hours and minutes:
        return f"{hours}h {minutes}m"
    if hours:
        return f"{hours}h"
    return f"{minutes}m"
