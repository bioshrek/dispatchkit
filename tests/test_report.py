"""D13.1: what a pass prints, once a person is reading every pass.

The full report was written for a cron job, whose log entries are each read in
isolation — there, printing every task on every pass is right. In a terminal
the previous pass is still on screen, so at a 60-second interval a task that
takes twenty minutes produces twenty identical blocks, and the one thing the
reader is waiting for is buried in the repetition.

So a looping pass prints the difference: the full picture once, then only what
moved. The rule this must not break is the one D13 wrote down — the process may
hold a snapshot for *rendering*, but every decision is recomputed from a fresh
read. `summarise` is display-only and `plan_tick` never sees the previous pass.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from dispatchkit.config import SchedulerConfig
from dispatchkit.github import IssueState
from dispatchkit.resolve import Status
from dispatchkit.tick import TickPlan, plan_tick, summarise
from tests.items import issue, state_of

pytestmark = pytest.mark.replay

CONFIG = SchedulerConfig()
NOW = datetime(2026, 9, 9, 12, 0, tzinfo=UTC)


def pass_over(*issues: IssueState) -> TickPlan:
    return plan_tick(state_of(*issues), config=CONFIG, now=NOW)


class TestTheFirstPass:
    """With nothing on screen yet, the reader gets the whole picture."""

    def test_it_names_every_task_not_only_the_moving_ones(self) -> None:
        plan = pass_over(issue("a", 1), issue("b", 2, depends=("a",)))
        printed = "\n".join(summarise(plan))

        assert "demo/a" in printed
        assert "demo/b" in printed

    def test_no_previous_pass_means_no_arrows(self) -> None:
        plan = pass_over(issue("a", 1))
        assert "→" not in "\n".join(summarise(plan))

    def test_the_statuses_line_up(self) -> None:
        # A ragged column is unreadable at a glance, and glancing is the whole
        # interaction: the reader is doing something else.
        plan = pass_over(
            issue("a", 1, closed=True), issue("long-task-name", 2, closed=True)
        )
        columns = {line.index("Done") for line in summarise(plan) if "Done" in line}
        assert len(columns) == 1


class TestTheDelta:
    """A second pass prints what moved, and nothing else."""

    def test_an_identical_pass_prints_nothing(self) -> None:
        # Which is what lets the caller print a one-line heartbeat instead.
        before = pass_over(issue("a", 1, assignees=("copilot-swe-agent",)))
        after = pass_over(issue("a", 1, assignees=("copilot-swe-agent",)))
        assert summarise(after, since=before) == ()

    def test_a_changed_status_is_shown_as_a_transition(self) -> None:
        before = pass_over(issue("a", 1, assignees=("copilot-swe-agent",)))
        after = pass_over(issue("a", 1, assignees=("copilot-swe-agent",), closed=True))

        assert "demo/a" in "\n".join(summarise(after, since=before))
        assert "Dispatched → Done" in "\n".join(summarise(after, since=before))

    def test_only_the_task_that_moved_is_printed(self) -> None:
        # The point of the whole exercise: five tasks and one transition is one
        # line, not six.
        stable = (issue("b", 2, closed=True), issue("c", 3, closed=True))
        before = pass_over(issue("a", 1, assignees=("copilot-swe-agent",)), *stable)
        after = pass_over(
            issue("a", 1, assignees=("copilot-swe-agent",), closed=True), *stable
        )

        moved = [line for line in summarise(after, since=before) if "demo/" in line]
        assert len(moved) == 1

    def test_a_task_that_appeared_is_reported_as_new(self) -> None:
        # `apply` ran in another terminal, or somebody opened an issue by hand.
        before = pass_over(issue("a", 1))
        after = pass_over(issue("a", 1), issue("b", 2, closed=True))

        printed = "\n".join(summarise(after, since=before))
        assert "demo/b" in printed
        assert "new" in printed

    def test_a_task_that_vanished_is_reported_as_gone(self) -> None:
        # An issue deleted outright. Rare, and silently dropping a task from
        # the report is exactly how it would go unnoticed.
        before = pass_over(issue("a", 1), issue("b", 2, closed=True))
        after = pass_over(issue("a", 1))

        printed = "\n".join(summarise(after, since=before))
        assert "demo/b" in printed
        assert "gone" in printed

    def test_a_dispatch_is_never_silent_even_with_no_status_change(self) -> None:
        # Belt and braces: the pass did something, so the pass has to say so.
        before = pass_over(issue("a", 1))
        after = pass_over(issue("a", 1))
        assert summarise(after, since=before) == ()
        assert "dispatch" in "\n".join(summarise(after, since=None))

    def test_a_repeated_deferral_is_not_a_change(self) -> None:
        # `json-output` deferred behind `encoding-fallback` persists for as
        # long as the PR is open. Reprinting it every 60 seconds would mean the
        # heartbeat never fires and the delta view buys nothing.
        both = (
            issue("a", 1, touches=("cli.py",)),
            issue("b", 2, touches=("cli.py",)),
        )
        before = pass_over(*both)
        after = pass_over(*both)

        assert before.deferred
        assert summarise(after, since=before) == ()


class TestWhatABlockedTaskIsWaitingOn:
    """The one question the status list cannot answer on its own.

    `Blocked` says a task is not going anywhere; it does not say what would
    move it. That is a one-hop question, so it gets a one-hop answer on the
    line that raised it — which is the whole reason this tool does not draw a
    dependency graph in the terminal.
    """

    def test_a_blocked_task_names_its_unmet_dependency(self) -> None:
        plan = pass_over(issue("a", 1), issue("b", 2, depends=("a",)))
        blocked = next(line for line in summarise(plan) if "demo/b" in line)

        assert Status.BLOCKED.value in blocked
        assert "a" in blocked.split(Status.BLOCKED.value)[1]

    def test_only_the_unmet_dependencies_are_named(self) -> None:
        # Naming a dependency that is already done sends the reader to look at
        # a closed issue.
        plan = pass_over(
            issue("done", 1, closed=True),
            issue("open", 2),
            issue("b", 3, depends=("done", "open")),
        )
        blocked = next(line for line in summarise(plan) if "demo/b" in line)

        assert "open" in blocked
        assert "done" not in blocked

    def test_a_ready_task_is_not_annotated(self) -> None:
        plan = pass_over(issue("a", 1))
        assert "←" not in "\n".join(summarise(plan))

    def test_it_survives_into_the_delta_view(self) -> None:
        # A task that becomes blocked mid-run is precisely when the reader
        # most wants to know what it is now waiting for.
        before = pass_over(issue("a", 1, closed=True), issue("b", 2, depends=("a",)))
        after = pass_over(issue("a", 1), issue("b", 2, depends=("a",)))

        blocked = next(line for line in summarise(after, since=before) if "demo/b" in line)
        assert "a" in blocked.split("→")[1]


class TestRenderingChangesNothing:
    """The snapshot is held for display, and display only.

    This is the rule D13 wrote down and the one a long-running process is most
    likely to break, because a delta view is the first thing that wants a
    memory. So: the same state must produce the same plan, whatever was on
    screen beforehand.
    """

    def test_the_plan_is_identical_whether_or_not_a_previous_pass_exists(self) -> None:
        before = pass_over(issue("a", 1))
        after = pass_over(issue("a", 1))

        summarise(after, since=before)
        assert after.operations == before.operations

    def test_a_no_change_pass_still_carries_its_operations(self) -> None:
        # The hazard this guards: printing nothing is a rendering decision, and
        # must never become a reason to skip the work.
        before = pass_over(issue("a", 1))
        after = pass_over(issue("a", 1))

        assert summarise(after, since=before) == ()
        assert after.operations
