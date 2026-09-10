"""D15: the timings, read from bytes GitHub actually sent.

`tests/fixtures/live_timings.json` is the literal response to
`state_command('bioshrek', 'dispatchkit-sandbox')` after both sandbox plans
had finished — the `wordfreq` plan of six and the `mincount` plan of three that
proved D16's plan branches.

It exists because of the defect D16's live trial found. Every offline test of
the machine block passed while the base was being dropped on the way to the
issue, because the fixtures were rendered by a second copy of the renderer:
they proved the code agreed with itself. `closedAt`, `mergedAt`,
`committedDate` and the check-suite timestamps are all guesses about a
document nobody here writes, so the only test that can falsify them is one
that reads the document.
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest

from dispatchkit.gh_cli import parse_state
from dispatchkit.resolve import build_items
from dispatchkit.retro import retrospective

pytestmark = pytest.mark.replay

FIXTURE = Path(__file__).parent / "fixtures" / "live_timings.json"


@pytest.fixture(scope="module")
def state():  # type: ignore[no-untyped-def]
    return parse_state(json.loads(FIXTURE.read_text()))


class TestTheFieldsArrive:
    def test_closed_issues_carry_the_moment_they_closed(self, state) -> None:  # type: ignore[no-untyped-def]
        closed = [issue for issue in state.issues if issue.closed]

        assert closed
        assert all(issue.closed_at is not None for issue in closed)

    def test_merged_pull_requests_carry_all_three_timings(self, state) -> None:  # type: ignore[no-untyped-def]
        merged = [pr for issue in state.issues for pr in issue.merged]

        assert merged
        assert all(pr.first_commit_at is not None for pr in merged)
        assert all(pr.merged_at is not None for pr in merged)
        assert all(pr.ci is not None for pr in merged)

    def test_ci_durations_are_plausible_rather_than_merely_present(self, state) -> None:  # type: ignore[no-untyped-def]
        """A parser that returned zero everywhere would satisfy the test above.

        The sandbox's pipeline is one `check` job of a few seconds to a couple
        of minutes, so anything outside that range means the two timestamps
        being subtracted are not the ones intended.
        """
        durations = [pr.ci for issue in state.issues for pr in issue.merged if pr.ci]

        assert durations
        assert all(timedelta() < ci < timedelta(hours=1) for ci in durations)

    def test_the_work_started_before_it_landed(self, state) -> None:  # type: ignore[no-untyped-def]
        """The ordering that makes the two durations mean anything."""
        for issue in state.issues:
            for pr in issue.merged:
                if pr.first_commit_at and pr.merged_at:
                    assert pr.first_commit_at < pr.merged_at


class TestTheRetrospectiveComputes:
    def test_every_finished_task_can_be_measured(self, state) -> None:  # type: ignore[no-untyped-def]
        """Both plans ran to completion, so nothing should be unmeasurable."""
        items, _ = build_items(state)
        report = retrospective(items, plan="mincount")

        assert report.measured == 3
        assert report.unmeasured == 0

    def test_no_duration_comes_out_backwards(self, state) -> None:  # type: ignore[no-untyped-def]
        """The live defect this fixture would have caught.

        `mincount/min-count` was merged, re-dispatched by the bug D16's trial
        found, and then closed, so its last dispatch is after its only commit.
        Choosing the dispatch by that commit is what keeps this true, and the
        issue is in this fixture with its history intact.
        """
        items, _ = build_items(state)
        for plan in ("wordfreq", "mincount"):
            for outcome in retrospective(items, plan=plan).outcomes:
                assert outcome.overhead is None or outcome.overhead >= timedelta()
                assert outcome.work is None or outcome.work >= timedelta()

    def test_the_retried_task_is_seen_as_retried(self, state) -> None:  # type: ignore[no-untyped-def]
        items, _ = build_items(state)

        assert retrospective(items, plan="mincount").retried == 1
