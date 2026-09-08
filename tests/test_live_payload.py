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

The board fields are the interesting part. `apply` writes `Task ID`, `Lane`
and `Verify` and deliberately leaves `Status` alone, because `Status` is a
derived view that a scheduler pass owns; that asymmetry is visible here.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from dispatchkit.block import BLOCK_VERSION, parse_block
from dispatchkit.gh_cli import parse_state

pytestmark = pytest.mark.replay

FIXTURE = Path(__file__).parent / "fixtures" / "live_state.json"


@pytest.fixture(scope="module")
def payload() -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return loaded


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
        ids = {issue.fields.get("Task ID") for issue in parse_state(payload).issues}
        assert ids == {"top-n", "stopwords", "encoding-fallback", "json-output", "document-flags"}

    def test_project_item_ids_survive_the_round_trip(self, payload: dict[str, Any]) -> None:
        # `SetProjectField` addresses items by this id; a `None` here would
        # make every board write a no-op.
        for issue in parse_state(payload).issues:
            assert issue.project_item_id is not None
            assert issue.project_item_id.startswith("PVTI_")

    def test_node_ids_are_present_for_assignment(self, payload: dict[str, Any]) -> None:
        for issue in parse_state(payload).issues:
            assert issue.node_id is not None

    def test_routing_labels_round_trip(self, payload: dict[str, Any]) -> None:
        issues = {i.fields["Task ID"]: i for i in parse_state(payload).issues}
        assert "lane:cloud" in issues["top-n"].labels
        assert "verify:auto" in issues["document-flags"].labels
        assert "verify:human" in issues["top-n"].labels

    def test_apply_leaves_status_to_the_scheduler(self, payload: dict[str, Any]) -> None:
        for issue in parse_state(payload).issues:
            assert "Status" not in issue.fields

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
        issues = {i.fields["Task ID"]: i for i in parse_state(payload).issues}
        block = parse_block(issues["json-output"].body)
        assert block.id == "json-output"
        assert block.plan == "wordfreq"
        assert [str(d) for d in block.depends] == ["top-n"]

    def test_the_version_key_round_tripped(self, payload: dict[str, Any]) -> None:
        issues = {i.fields["Task ID"]: i for i in parse_state(payload).issues}
        assert parse_block(issues["top-n"].body).version == BLOCK_VERSION
