"""A real GraphQL response, recorded so it keeps being true (D5 live run).

`tests/fixtures/search_issues.json` is renderer-generated: it was written from
the same code that reads it, so agreement between them proved only internal
consistency. This fixture is the other kind — the literal bytes GitHub
returned for `state_command('bioshrek', 'dispatchkit-sandbox')` on 2026-09-08,
after `apply` had created the `wordfreq` plan's five issues.

Recording it converts the one thing offline tests could never check — that the
adapter's picture of the API matches the API — into a permanent regression
test. Compared on the day: the two payloads have **identical shape**, so the
renderer's guess was right, and every offline test built on it was testing the
right document.

The payload was recorded while the Project board still existed, and it is kept
exactly as GitHub returned it — including the `projectItems` the query no
longer asks for. That makes it a second regression test for free: the adapter
must read a response carrying fields it has stopped caring about without
noticing them (D14).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from dispatchkit.block import BLOCK_VERSION, parse_block
from dispatchkit.gh_cli import parse_state
from dispatchkit.github import IssueState

pytestmark = pytest.mark.replay

FIXTURE = Path(__file__).parent / "fixtures" / "live_state.json"


@pytest.fixture(scope="module")
def payload() -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return loaded


def by_task_id(payload: dict[str, Any]) -> dict[str, IssueState]:
    """Keyed the way the scheduler keys them: by the id in the machine block.

    Not by a board field. The block is the idempotency key and always was; the
    `Task ID` column was a copy of it that a human could read, and it went with
    the board.
    """
    return {str(parse_block(issue.body).id): issue for issue in parse_state(payload).issues}


class TestRecordedResponse:
    def test_the_adapter_parses_what_github_actually_returned(
        self, payload: dict[str, Any]
    ) -> None:
        assert len(parse_state(payload).issues) == 5

    def test_every_issue_carries_the_id_the_scheduler_keys_on(
        self, payload: dict[str, Any]
    ) -> None:
        # Without `Task ID` an issue is invisible to a pass, so a silent
        # failure here would look exactly like an empty backlog.
        assert set(by_task_id(payload)) == {
            "top-n",
            "stopwords",
            "encoding-fallback",
            "json-output",
            "document-flags",
        }

    def test_the_retired_project_selection_is_read_straight_past(
        self, payload: dict[str, Any]
    ) -> None:
        # The recording still carries `projectItems`, because GitHub returned
        # them. Nothing may come of that: an adapter that still had somewhere
        # to put them would be one that had not finished retiring the board.
        raw = payload["data"]["repository"]["issues"]["nodes"]
        assert all(node.get("projectItems") for node in raw)
        assert not any(hasattr(issue, "fields") for issue in parse_state(payload).issues)

    def test_node_ids_are_present_for_assignment(self, payload: dict[str, Any]) -> None:
        for issue in parse_state(payload).issues:
            assert issue.node_id is not None

    def test_routing_labels_round_trip(self, payload: dict[str, Any]) -> None:
        # The labels are what survives the board: they mirror the block, and a
        # saved issue-list URL filtering on them is the replacement view.
        issues = by_task_id(payload)
        assert "lane:cloud" in issues["top-n"].labels
        assert "verify:auto" in issues["document-flags"].labels
        assert "verify:human" in issues["top-n"].labels

    def test_no_issue_carries_a_recorded_status(self, payload: dict[str, Any]) -> None:
        # Status is derived on every pass, so nothing GitHub hands back should
        # ever be a status this tool wrote down earlier.
        for issue in by_task_id(payload).values():
            assert not any(label.startswith("status:") for label in issue.labels)

    def test_nothing_is_dispatched_in_this_snapshot(self, payload: dict[str, Any]) -> None:
        # Recorded before the first `tick`, which is what makes it the useful
        # starting state: assignment is the lock, and nothing holds it yet.
        for issue in parse_state(payload).issues:
            assert issue.assignees == ()
            assert issue.open_prs == ()
            assert not issue.closed


class TestTheMachineBlockSurvivedGitHub:
    """The block is written into a body GitHub stores and re-serves.

    Markdown round-tripping is the sort of thing that silently eats a
    `<!-- -->` comment or rewrites whitespace, and the block is a wire format,
    so this is worth an assertion against real returned bytes rather than
    against what we believe we sent.
    """

    def test_the_block_comes_back_parseable(self, payload: dict[str, Any]) -> None:
        issues = by_task_id(payload)
        block = parse_block(issues["json-output"].body)
        assert block.id == "json-output"
        assert block.plan == "wordfreq"
        assert [str(d) for d in block.depends] == ["top-n"]

    def test_the_version_key_round_tripped(self, payload: dict[str, Any]) -> None:
        assert parse_block(by_task_id(payload)["top-n"].body).version == BLOCK_VERSION
