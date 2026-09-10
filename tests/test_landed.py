"""D16: work that has landed is not work to hand out again.

Found live, on the first plan dispatchkit ever ran on a branch of its own. A
pass merged a task's pull request into `plan/mincount`; the next pass closed
the issue, exactly as D16 intends — and in the same breath dispatched the task
a second time, to an agent that found its own work already merged and failed
at the commit step with nothing to commit.

The window is small and entirely real. Between the merge and the close, the
issue is open, its pull request is no longer *open* so `open_prs` is empty, and
the merge released the assignment — so every readiness test passes and the task
rejoins the ready set. On `main` this window never existed: GitHub honoured the
closing keyword and closed the issue in the same instant it merged, so the
issue was never open-and-landed for a scheduler to see. Moving the closure off
GitHub opened a gap that GitHub used to close for us.

What it costs is worse than a wasted run. The re-dispatch consumes a retry from
a budget meant for failures, and it is *recorded* as an attempt — so a task
that succeeded first time carries a failure in its history, and a plan long
enough could mark a perfectly healthy task `Stuck` on the strength of its own
successes.

The fix names the state instead of leaving it between two others: a task whose
work has merged into its base is `Done`, whether or not the issue has caught up
yet. `Done` is not a lie for one pass — it is the truth arriving before the
bookkeeping.

Crucially this changes only the *status*. Dependents are released by
`_satisfied`, which reads the issue's own closure and not the status word, so
nothing downstream starts until the close has actually happened. A pass that
closes an issue and then fails would otherwise have unblocked work whose
prerequisite is still open — the one mistake this whole area exists to prevent.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from dispatchkit.config import SchedulerConfig
from dispatchkit.github import IssueState
from dispatchkit.model import DEFAULT_BASE, Base, MergedPr
from dispatchkit.resolve import Status, build_items, resolve
from dispatchkit.tick import plan_tick
from tests.items import issue, state_of

pytestmark = pytest.mark.unit

NOW = datetime(2026, 9, 11, 12, 0, tzinfo=UTC)
PLAN_BASE = Base("plan/wordfreq")


def status_of(*issues: IssueState) -> dict[str, Status]:
    items, _ = build_items(state_of(*issues))
    return {str(ref.id): value for ref, value in resolve(items).items()}


class TestTheWindowBetweenMergeAndClose:
    def test_a_landed_task_is_done_not_ready(self) -> None:
        statuses = status_of(
            issue("one", number=1, base=PLAN_BASE, merged=(MergedPr(90, PLAN_BASE),))
        )

        assert statuses["one"] is Status.DONE

    def test_it_is_not_dispatched_again(self) -> None:
        state = state_of(
            issue("one", number=1, base=PLAN_BASE, merged=(MergedPr(90, PLAN_BASE),))
        )

        plan = plan_tick(state, config=SchedulerConfig(), now=NOW)

        assert plan.admitted == ()

    def test_a_merge_somewhere_else_leaves_the_task_ready(self) -> None:
        """The base is the evidence, here as much as in `close_ops`.

        Anyone may open a pull request that cross-references an issue and merge
        it into a branch of their own. If that were enough to call a task done,
        an outsider could stop it being worked on at all -- the same attack
        `close_ops` refuses, arriving by the other door.
        """
        statuses = status_of(
            issue("one", number=1, base=PLAN_BASE, merged=(MergedPr(90, DEFAULT_BASE),))
        )

        assert statuses["one"] is Status.READY

    def test_a_task_with_nothing_merged_is_still_ready(self) -> None:
        statuses = status_of(issue("one", number=1, base=PLAN_BASE))

        assert statuses["one"] is Status.READY


class TestWhatDoneDoesNotDo:
    def test_dependents_wait_for_the_issue_to_actually_close(self) -> None:
        """`Done` is the status word; closure is what releases work.

        Read `_satisfied`, not `resolve`. If a pass could close an issue and
        fail, the dependent must still be blocked on the next pass -- so the
        edge is discharged against the fact, never against our reading of it.
        """
        statuses = status_of(
            issue("one", number=1, base=PLAN_BASE, merged=(MergedPr(90, PLAN_BASE),)),
            issue("two", number=2, base=PLAN_BASE, depends=("one",)),
        )

        assert statuses["one"] is Status.DONE
        assert statuses["two"] is Status.BLOCKED

    def test_a_cancelled_task_is_still_cancelled(self) -> None:
        """Cancelled is read first, and a merge does not overrule a human.

        A task closed as not planned whose branch happened to land is finished
        with, not done; calling it `Done` would let `_satisfied`'s sibling
        mistake back in by another route.
        """
        statuses = status_of(
            issue(
                "one",
                number=1,
                base=PLAN_BASE,
                closed=True,
                cancelled=True,
                merged=(MergedPr(90, PLAN_BASE),),
            )
        )

        assert statuses["one"] is Status.CANCELLED

    def test_a_landed_task_with_a_new_pull_request_still_reports_it(self) -> None:
        """An open pull request is a thing a human may need to act on.

        Landed-and-open outranks readiness, not review: if the re-dispatch this
        test's neighbours prevent ever happened anyway, or a human opened a
        follow-up against the same issue, the pass must keep saying so rather
        than quietly calling the task finished and abandoning the pull request.
        """
        statuses = status_of(
            issue(
                "one",
                number=1,
                base=PLAN_BASE,
                merged=(MergedPr(90, PLAN_BASE),),
                open_prs=(91,),
            )
        )

        assert statuses["one"] is Status.IN_REVIEW
