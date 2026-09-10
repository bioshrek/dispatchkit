"""D15: the three timestamps a retrospective needs, carried in the snapshot.

D2 deleted `estimate_minutes` on the argument that the graph is the one place
its quantity cannot be known, and promised the number would come back as an
observation. Keeping that promise needs three facts the scheduler has never
had a reason to read: when the issue closed, when the first commit of the work
that landed was made, and how long CI took on it.

They ride in the existing state query rather than a second one. `RepoState` is
documented as the whole world so that nothing can disagree with it, and a
second snapshot type would be a second world -- its own parser, its own
fixtures, its own chance to drift. The price is three fields on connections
the query already walks.

Every one of them is optional, and absence is the point of most of these
tests. A recorded payload predates the question; a task closed by hand never
had a pull request; a merge can arrive with no commit GitHub will show us.
None of those is a zero. Zero is a task somebody finished instantly, and
substituting it would drag a median toward work nobody did.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from dispatchkit.gh_cli import STATE_QUERY, parse_state

pytestmark = pytest.mark.unit

FIXTURE = Path(__file__).parent / "fixtures" / "live_state.json"


def payload(*issues: dict[str, Any]) -> dict[str, Any]:
    return {"data": {"repository": {"issues": {"nodes": list(issues)}}}}


def issue_node(
    *,
    number: int = 1,
    state: str = "CLOSED",
    closed_at: str | None = "2026-09-10T12:30:00Z",
    prs: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    node: dict[str, Any] = {
        "number": number,
        "title": f"task {number}",
        "body": "",
        "state": state,
        "labels": {"nodes": []},
        "assignees": {"nodes": []},
        "timelineItems": {"nodes": [{"source": source} for source in (prs or [])]},
    }
    if closed_at is not None:
        node["closedAt"] = closed_at
    return node


def merged_pr(
    *,
    number: int = 90,
    base: str = "plan/wordfreq",
    first_commit: str | None = "2026-09-10T12:05:00Z",
    merged_at: str | None = None,
    suites: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    source: dict[str, Any] = {"number": number, "state": "MERGED", "baseRefName": base}
    if merged_at is not None:
        source["mergedAt"] = merged_at
    if first_commit is not None:
        source["first_commit"] = {"nodes": [{"commit": {"committedDate": first_commit}}]}
    if suites is not None:
        source["commits"] = {"nodes": [{"commit": {"checkSuites": {"nodes": suites}}}]}
    return source


def suite(created: str, updated: str) -> dict[str, Any]:
    return {
        "status": "COMPLETED",
        "conclusion": "SUCCESS",
        "createdAt": created,
        "updatedAt": updated,
    }


class TestTheQueryAsks:
    def test_it_asks_when_the_issue_closed(self) -> None:
        assert "closedAt" in STATE_QUERY

    def test_it_asks_for_the_first_commit_not_only_the_last(self) -> None:
        """`commits(last: 1)` is the head, which is what CI ran on.

        Overhead ends at the *first* commit -- the moment the agent stopped
        setting up and started working -- so this is a second connection on the
        same pull request, not a change to the existing one.
        """
        assert "first_commit: commits(first: 1)" in STATE_QUERY
        assert "commits(last: 1)" in STATE_QUERY

    def test_it_asks_when_the_pull_request_merged(self) -> None:
        assert "mergedAt" in STATE_QUERY

    def test_it_asks_when_the_check_suites_ran(self) -> None:
        assert "checkSuites" in STATE_QUERY
        assert "createdAt" in STATE_QUERY
        assert "updatedAt" in STATE_QUERY


class TestWhenTheIssueClosed:
    def test_it_is_read(self) -> None:
        state = parse_state(payload(issue_node()))

        assert state.issues[0].closed_at == datetime(2026, 9, 10, 12, 30, tzinfo=UTC)

    def test_an_open_issue_has_none(self) -> None:
        state = parse_state(payload(issue_node(state="OPEN", closed_at=None)))

        assert state.issues[0].closed_at is None

    def test_a_payload_that_predates_the_question_reads_as_absent(self) -> None:
        """Not an error. A recorded fixture is older than the field."""
        state = parse_state(payload(issue_node(closed_at=None)))

        assert state.issues[0].closed_at is None


class TestWhenTheWorkStarted:
    def test_the_first_commit_is_read_from_the_pull_request_that_landed(self) -> None:
        state = parse_state(payload(issue_node(prs=[merged_pr()])))

        assert state.issues[0].merged[0].first_commit_at == datetime(
            2026, 9, 10, 12, 5, tzinfo=UTC
        )

    def test_a_merge_with_no_commit_shown_reads_as_absent(self) -> None:
        state = parse_state(payload(issue_node(prs=[merged_pr(first_commit=None)])))

        assert state.issues[0].merged[0].first_commit_at is None


class TestWhenTheWorkLanded:
    """Since D16 the close is dispatchkit's own act on a later pass, so the
    merge is the moment the work was actually done."""

    def test_the_merge_time_is_read(self) -> None:
        state = parse_state(
            payload(issue_node(prs=[merged_pr(merged_at="2026-09-10T12:20:00Z")]))
        )

        assert state.issues[0].merged[0].merged_at == datetime(2026, 9, 10, 12, 20, tzinfo=UTC)

    def test_a_payload_that_predates_the_question_reads_as_absent(self) -> None:
        state = parse_state(payload(issue_node(prs=[merged_pr()])))

        assert state.issues[0].merged[0].merged_at is None


class TestHowLongCiTook:
    def test_it_spans_the_check_suites(self) -> None:
        """First suite created to last suite finished.

        Not the sum: suites run concurrently, and adding them up would report
        an elapsed time no clock ever measured.
        """
        state = parse_state(
            payload(
                issue_node(
                    prs=[
                        merged_pr(
                            suites=[
                                suite("2026-09-10T12:10:00Z", "2026-09-10T12:12:00Z"),
                                suite("2026-09-10T12:11:00Z", "2026-09-10T12:16:00Z"),
                            ]
                        )
                    ]
                )
            )
        )

        assert state.issues[0].merged[0].ci == timedelta(minutes=6)

    def test_a_pull_request_with_no_suites_reads_as_absent(self) -> None:
        state = parse_state(payload(issue_node(prs=[merged_pr(suites=[])])))

        assert state.issues[0].merged[0].ci is None

    def test_a_suite_missing_its_timestamps_is_skipped_not_zeroed(self) -> None:
        state = parse_state(
            payload(
                issue_node(
                    prs=[
                        merged_pr(
                            suites=[
                                {"status": "COMPLETED", "conclusion": "SUCCESS"},
                                suite("2026-09-10T12:10:00Z", "2026-09-10T12:14:00Z"),
                            ]
                        )
                    ]
                )
            )
        )

        assert state.issues[0].merged[0].ci == timedelta(minutes=4)


@pytest.mark.replay
class TestTheRecordedPayload:
    def test_it_still_parses_without_any_of_the_new_fields(self) -> None:
        """The regression this whole module could most easily cause.

        `tests/fixtures/live_state.json` is the literal bytes GitHub returned
        before any of these fields were asked for. Reading it must degrade to
        "unmeasured", never fail.
        """
        state = parse_state(json.loads(FIXTURE.read_text()))

        assert state.issues
        assert all(issue.closed_at is None for issue in state.issues)
