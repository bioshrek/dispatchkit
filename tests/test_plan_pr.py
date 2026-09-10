"""D16: the plan's own pull request, opened when its last task closes.

A plan branch accumulates every task's merge and then stops. Nothing was going
to propose it to `main`, and a plan branch nobody proposes is work that shipped
nowhere — the exact silence this tool exists to break, one level up.

Three things it must not do, each of which is a way to undo the reason plan
branches exist at all:

**It is never merged by dispatchkit.** Not even when every task said
`verify: auto`. Those declarations are about a task — a reviewed unit of a
reviewed graph — and a plan is the thing that was never individually reviewed:
it is the sum. The whole point of collecting the work on a branch is that a
human reads it once, as a whole, before it reaches `main`. So this is the one
pull request in the system that `merge_ops` may not touch.

**It is opened once.** A pass runs every few minutes; the second one must find
the pull request it opened and say nothing.

**It waits for the last task.** Opening early would be a pull request that
grows under the reviewer, and a `Ready` marker that is a lie.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from dispatchkit.config import SchedulerConfig
from dispatchkit.github import IssueState, MergePr, OpenPlanPr, RepoState
from dispatchkit.model import DEFAULT_BASE, Base, Checks, PullRequest, Verify
from dispatchkit.resolve import build_items, plan_pr_ops
from dispatchkit.tick import execute_tick, plan_tick
from tests.fake_github import FakeGitHub
from tests.items import issue, state_of

pytestmark = pytest.mark.unit

NOW = datetime(2026, 9, 11, 12, 0, tzinfo=UTC)
PLAN_BASE = Base("plan/wordfreq")


def ops(*issues: IssueState, open_plan_prs: tuple[Base, ...] = ()) -> tuple[OpenPlanPr, ...]:
    items, _ = build_items(state_of(*issues))
    return plan_pr_ops(items, open_bases=open_plan_prs)


class TestWhenTheLastTaskCloses:
    def test_a_finished_plan_is_proposed(self) -> None:
        planned = ops(
            issue("one", number=1, base=PLAN_BASE, closed=True),
            issue("two", number=2, base=PLAN_BASE, closed=True),
        )
        assert [operation.base for operation in planned] == [PLAN_BASE]

    def test_one_open_task_is_enough_to_wait(self) -> None:
        assert (
            ops(
                issue("one", number=1, base=PLAN_BASE, closed=True),
                issue("two", number=2, base=PLAN_BASE),
            )
            == ()
        )

    def test_a_cancelled_task_does_not_hold_the_plan_open(self) -> None:
        # Closed as not planned is still finished with. It satisfies no
        # dependency (D13.1), but there is nothing left to wait for.
        planned = ops(
            issue("one", number=1, base=PLAN_BASE, closed=True),
            issue("two", number=2, base=PLAN_BASE, cancelled=True),
        )
        assert [operation.base for operation in planned] == [PLAN_BASE]


class TestAPlanOnMainHasNothingToPropose:
    def test_no_branch_means_no_pull_request(self) -> None:
        assert ops(issue("one", number=1, closed=True)) == ()


class TestItIsOpenedOnce:
    def test_an_existing_plan_pr_stops_it(self) -> None:
        assert (
            ops(
                issue("one", number=1, base=PLAN_BASE, closed=True),
                open_plan_prs=(PLAN_BASE,),
            )
            == ()
        )

    def test_a_pull_request_for_another_plan_does_not_stop_it(self) -> None:
        planned = ops(
            issue("one", number=1, base=PLAN_BASE, closed=True),
            open_plan_prs=(Base("plan/other"),),
        )
        assert [operation.base for operation in planned] == [PLAN_BASE]


class TestDispatchkitNeverMergesIt:
    """`verify: auto` is a statement about a task, not about a plan."""

    def test_merge_ops_ignores_the_plan_branch(self) -> None:
        from dispatchkit.resolve import merge_ops

        # Every task auto, every task done, a green mergeable PR on the plan
        # branch: `merge_ops` still has nothing to say, because it only ever
        # looks at pull requests linked to open task issues, and the plan's
        # pull request is linked to none.
        items, _ = build_items(
            state_of(issue("one", number=1, base=PLAN_BASE, verify=Verify.AUTO, closed=True))
        )
        assert merge_ops(items, SchedulerConfig()) == ()

    def test_the_operation_carries_no_merge_authority(self) -> None:
        # Structural rather than behavioural: there is no field here that could
        # later be read as "and merge it".
        assert not hasattr(OpenPlanPr(PLAN_BASE, "wordfreq", DEFAULT_BASE), "verify")


class TestThroughAPass:
    def state(self) -> RepoState:
        return state_of(
            issue("one", number=1, base=PLAN_BASE, closed=True),
            issue("two", number=2, base=PLAN_BASE, closed=True),
        )

    def test_a_pass_opens_it(self) -> None:
        api = FakeGitHub(state=self.state())
        execute_tick(plan_tick(api.fetch_state(), config=SchedulerConfig(), now=NOW), api=api)
        assert api.opened and api.opened[-1]["head"] == str(PLAN_BASE)
        assert api.opened[-1]["base"] == "main"

    def test_the_second_pass_opens_nothing(self) -> None:
        """The convergence standard, over a pull request GitHub now reports."""
        api = FakeGitHub(state=self.state())
        execute_tick(plan_tick(api.fetch_state(), config=SchedulerConfig(), now=NOW), api=api)
        again = plan_tick(api.fetch_state(), config=SchedulerConfig(), now=NOW)
        assert not any(isinstance(operation, OpenPlanPr) for operation in again.operations)

    def test_it_is_never_a_merge(self) -> None:
        api = FakeGitHub(state=self.state())
        plan = plan_tick(api.fetch_state(), config=SchedulerConfig(), now=NOW)
        assert not any(isinstance(operation, MergePr) for operation in plan.operations)


class TestTheBodySaysWhatItIs:
    def test_it_names_the_plan_and_asks_for_a_human(self) -> None:
        from dispatchkit.resolve import plan_pr_body

        body = plan_pr_body("wordfreq", (1, 2))
        assert "wordfreq" in body
        assert "#1" in body and "#2" in body
        # The one review dispatchkit cannot do for you, said where it will be
        # read rather than only in a design document.
        assert "review" in body.lower()


class TestNotBlockedByAnUnfinishedNeighbour:
    def test_plans_are_judged_separately(self) -> None:
        planned = ops(
            issue("one", number=1, plan="alpha", base=PLAN_BASE, closed=True),
            issue("two", number=2, plan="beta", base=Base("plan/beta")),
        )
        assert [operation.plan for operation in planned] == ["alpha"]


class TestGreenCiIsNotRequired:
    """Deliberately not a gate: the reviewer sees CI on the pull request.

    Withholding the pull request until CI is green would hide a red plan
    branch, which is precisely the thing a human needs to be shown.
    """

    def test_a_red_task_pr_does_not_prevent_the_proposal(self) -> None:
        planned = ops(
            issue(
                "one",
                number=1,
                base=PLAN_BASE,
                closed=True,
                open_prs=(PullRequest(90, Checks.FAILING),),
            )
        )
        assert [operation.base for operation in planned] == [PLAN_BASE]


class TestTheAdapterReadsOpenHeads:
    """Without this the operation is planned for ever and opens a duplicate
    pull request every pass. The rule is only as idempotent as the read."""

    def test_the_query_asks_for_them(self) -> None:
        from dispatchkit.gh_cli import STATE_QUERY

        assert "pullRequests" in STATE_QUERY
        assert "headRefName" in STATE_QUERY

    def test_they_are_parsed(self) -> None:
        from dispatchkit.gh_cli import parse_state

        payload: dict[str, Any] = {
            "data": {
                "repository": {
                    "issues": {"nodes": []},
                    "pullRequests": {"nodes": [{"headRefName": "plan/wordfreq"}]},
                }
            }
        }
        assert parse_state(payload).open_pr_heads == ("plan/wordfreq",)

    def test_a_payload_without_them_still_reads(self) -> None:
        from dispatchkit.gh_cli import parse_state

        # Every recorded fixture predates D16. A missing key is an absence of
        # pull requests, not a parse error.
        payload: dict[str, Any] = {"data": {"repository": {"issues": {"nodes": []}}}}
        assert parse_state(payload).open_pr_heads == ()


class TestThePassSaysSo:
    def test_the_completion_line_names_the_review(self) -> None:
        from dispatchkit.cli import _acted, _completion
        from dispatchkit.tick import TickResult

        result = TickResult(0, proposed=1)
        assert "1 plan(s) proposed for review" in _completion(result)
        assert _acted(result)
