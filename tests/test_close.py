"""D16: dispatchkit closes the issues whose work has landed.

Off the default branch, GitHub does not honour a closing keyword. Its
documentation is explicit — for a pull request targeting anything but the
default branch, *"these keywords are ignored, no links are created"*. A plan
branch is by definition not the default branch, so `Closes #12` in a pull
request body becomes decoration, the issue stays open, and every task that
depends on it waits forever. The first plan on a branch would deadlock on its
first task.

So the closure that was GitHub's job becomes dispatchkit's. Three things this
has to get right, each of which is a way to be wrong quietly:

**The base is checked, not just the merge.** A merged pull request that went
somewhere else is not evidence this plan's work has landed. Anyone can open a
pull request cross-referencing an issue and merge it into a branch of their
own; if merging *anything* closed the task, an outsider could unblock the
graph.

**It is idempotent.** Closing an issue that is already closed is a second
operation for a state that has already been reached, and the convergence test
is the standard: re-read, re-plan, and the second plan is empty.

**It does not become a second way for a pass to change the graph.** A pass
still never creates or edits an issue. Closing is admitting a fact the
repository already contains — the merge happened — rather than a decision, and
on a `main`-based plan the rule emits nothing at all, because GitHub got there
first and the issue is no longer open.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from dispatchkit.config import SchedulerConfig
from dispatchkit.github import CloseIssue, IssueState, MergePr
from dispatchkit.model import DEFAULT_BASE, Base, MergedPr
from dispatchkit.resolve import build_items, close_ops
from dispatchkit.tick import execute_tick, plan_tick
from tests.fake_github import FakeGitHub
from tests.items import issue, state_of

pytestmark = pytest.mark.unit

NOW = datetime(2026, 9, 11, 12, 0, tzinfo=UTC)
PLAN_BASE = Base("plan/wordfreq")


def ops(*issues: IssueState) -> tuple[CloseIssue, ...]:
    items, _ = build_items(state_of(*issues))
    return close_ops(items)


class TestWorkThatHasLanded:
    def test_a_pr_merged_into_the_plan_base_closes_the_issue(self) -> None:
        planned = ops(
            issue("one", number=1, base=PLAN_BASE, merged=(MergedPr(90, PLAN_BASE),))
        )
        assert [operation.number for operation in planned] == [1]

    def test_an_unmerged_task_is_left_alone(self) -> None:
        assert ops(issue("one", number=1, base=PLAN_BASE)) == ()

    def test_an_already_closed_issue_is_left_alone(self) -> None:
        assert (
            ops(
                issue(
                    "one",
                    number=1,
                    base=PLAN_BASE,
                    closed=True,
                    merged=(MergedPr(90, PLAN_BASE),),
                )
            )
            == ()
        )


class TestTheBaseIsTheEvidence:
    """A merge is only this plan's work if it went where this plan integrates."""

    def test_a_merge_into_some_other_branch_proves_nothing(self) -> None:
        assert (
            ops(
                issue(
                    "one",
                    number=1,
                    base=PLAN_BASE,
                    merged=(MergedPr(90, Base("somebody-elses")),),
                )
            )
            == ()
        )

    def test_one_good_merge_among_several_is_enough(self) -> None:
        planned = ops(
            issue(
                "one",
                number=1,
                base=PLAN_BASE,
                merged=(MergedPr(90, Base("scratch")), MergedPr(91, PLAN_BASE)),
            )
        )
        assert [operation.number for operation in planned] == [1]


class TestAPlanOnMainNeedsNothing:
    """GitHub already did it, so the rule sees a closed issue and stops.

    Not a special case in the code: the same two conditions produce it.
    """

    def test_the_rule_is_silent_once_github_has_closed_it(self) -> None:
        assert ops(issue("one", number=1, closed=True, merged=(MergedPr(90, DEFAULT_BASE),))) == ()

    def test_but_an_open_issue_on_main_is_still_closed(self) -> None:
        # Belt and braces rather than a contradiction: if GitHub's own closure
        # is delayed or was undone, saying so twice is harmless and saying it
        # never is a deadlock.
        planned = ops(issue("one", number=1, merged=(MergedPr(90, DEFAULT_BASE),)))
        assert [operation.number for operation in planned] == [1]


class TestThroughAPass:
    def test_a_pass_emits_the_closure(self) -> None:
        state = state_of(
            issue("one", number=1, base=PLAN_BASE, merged=(MergedPr(90, PLAN_BASE),))
        )
        plan = plan_tick(state, config=SchedulerConfig(), now=NOW)
        assert any(isinstance(operation, CloseIssue) for operation in plan.operations)

    def test_a_closure_is_not_a_merge(self) -> None:
        # `MergePr` changes the tree; `CloseIssue` records that it already did.
        state = state_of(
            issue("one", number=1, base=PLAN_BASE, merged=(MergedPr(90, PLAN_BASE),))
        )
        plan = plan_tick(state, config=SchedulerConfig(), now=NOW)
        assert not any(isinstance(operation, MergePr) for operation in plan.operations)

    def test_the_second_pass_is_empty(self) -> None:
        """The convergence standard: apply, re-read, re-plan, nothing left."""
        api = FakeGitHub(
            state=state_of(
                issue("one", number=1, base=PLAN_BASE, merged=(MergedPr(90, PLAN_BASE),))
            )
        )
        execute_tick(plan_tick(api.fetch_state(), config=SchedulerConfig(), now=NOW), api=api)
        again = plan_tick(api.fetch_state(), config=SchedulerConfig(), now=NOW)
        assert not any(isinstance(operation, CloseIssue) for operation in again.operations)

    def test_it_reaches_the_api(self) -> None:
        api = FakeGitHub(
            state=state_of(
                issue("one", number=1, base=PLAN_BASE, merged=(MergedPr(90, PLAN_BASE),))
            )
        )
        execute_tick(plan_tick(api.fetch_state(), config=SchedulerConfig(), now=NOW), api=api)
        assert "close_issue(1)" in api.calls


class TestTheAdapter:
    """The two halves that only exist in `gh_cli`: the command and the read."""

    def test_the_command_closes_as_completed(self) -> None:
        from dispatchkit.gh_cli import close_command

        # No `--reason`, which is "completed". `not planned` satisfies no
        # dependency (D13.1), so closing a finished task that way would block
        # everything behind it for ever.
        assert close_command(12, "o/r") == ["gh", "issue", "close", "12", "--repo", "o/r"]
        assert "--reason" not in close_command(12, "o/r")

    def test_a_merged_pr_is_read_with_its_base(self) -> None:
        from dispatchkit.gh_cli import _parse_merged_prs

        node = {
            "timelineItems": {
                "nodes": [
                    {"source": {"number": 90, "state": "MERGED", "baseRefName": "plan/wordfreq"}},
                    {"source": {"number": 91, "state": "OPEN", "baseRefName": "plan/wordfreq"}},
                ]
            }
        }
        assert _parse_merged_prs(node) == (MergedPr(90, PLAN_BASE),)

    def test_an_unreadable_base_yields_nothing(self) -> None:
        from dispatchkit.gh_cli import _parse_merged_prs

        # Failing towards leaving the issue open: an open task is visible, and
        # a task closed on evidence that could not be checked is not.
        node = {"timelineItems": {"nodes": [{"source": {"number": 90, "state": "MERGED"}}]}}
        assert _parse_merged_prs(node) == ()


class TestThePassSaysSo:
    def test_the_completion_line_counts_closures(self) -> None:
        from dispatchkit.cli import _acted, _completion
        from dispatchkit.tick import TickResult

        result = TickResult(0, closed=2)
        assert "2 issue(s) closed" in _completion(result)
        assert _acted(result)
