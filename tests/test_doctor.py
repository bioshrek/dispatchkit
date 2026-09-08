"""D5.5: `dispatchkit doctor` — the adoption on-ramp, and the live tier's assertions.

Everything the pipeline needs but does not create for itself is a way for a
first run to fail confusingly: a token without the `project` scope, a
repository with no coding agent seat, a board carrying GitHub's built-in
`Status` options, a missing `dispatchkit` label that turns every pass into a
silent no-op.

`doctor` is the pure function that turns those facts into a verdict, so the
whole check set is testable offline against a synthetic snapshot — and the
same function is what the `live` tier will assert on against a real one.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from dispatchkit.board import REQUIRED_FIELDS, REQUIRED_LABELS, BoardSnapshot, ExistingField
from dispatchkit.doctor import (
    REQUIRED_SCOPES,
    Diagnostics,
    LocalFacts,
    check,
    parse_labels,
    parse_project,
    parse_token_scopes,
    summarise,
)
from dispatchkit.resolve import FIELD_STATUS

pytestmark = pytest.mark.unit


def healthy_board() -> BoardSnapshot:
    return BoardSnapshot(
        fields=tuple(
            ExistingField(spec.name, f"PVTF_{spec.name}", spec.options)
            for spec in REQUIRED_FIELDS
        ),
        labels=REQUIRED_LABELS,
        items=0,
    )


def healthy(**overrides: object) -> Diagnostics:
    base: dict[str, object] = {
        "scopes": ("repo", "project", "read:org"),
        "agent_available": True,
        "board": healthy_board(),
    }
    return Diagnostics(**{**base, **overrides})  # type: ignore[arg-type]


def local(**overrides: object) -> LocalFacts:
    base: dict[str, object] = {
        "config_path": Path(".github/dispatchkit.toml"),
        "config_exists": True,
        "workflow_path": Path(".github/workflows/dispatchkit.yml"),
        "workflow_exists": True,
        "plans": Path("docs/plans"),
        "plans_exists": True,
    }
    return LocalFacts(**{**base, **overrides})  # type: ignore[arg-type]


def by_name(diagnostics: Diagnostics, facts: LocalFacts | None = None) -> dict[str, bool]:
    return {c.name: c.ok for c in check(diagnostics, facts if facts is not None else local())}


class TestHealthy:
    def test_a_correctly_set_up_repository_passes_every_check(self) -> None:
        checks = check(healthy(), local())
        assert checks
        assert all(c.ok for c in checks), [c.detail for c in checks if not c.ok]

    def test_a_healthy_report_still_lists_what_it_checked(self) -> None:
        # `doctor` is also documentation of the contract, so it prints the
        # passing checks rather than only the failures.
        lines = summarise(check(healthy(), local()))
        assert any(FIELD_STATUS in line for line in lines)


class TestToken:
    def test_the_project_scope_is_required_because_nothing_board_related_works_without_it(
        self,
    ) -> None:
        assert by_name(healthy(scopes=("repo",)))["token-scopes"] is False

    def test_the_missing_scope_is_named_with_the_command_that_grants_it(self) -> None:
        failure = next(
            c for c in check(healthy(scopes=("repo",)), local()) if c.name == "token-scopes"
        )
        assert "project" in failure.detail
        assert "gh auth refresh" in failure.remedy

    def test_scopes_that_cannot_be_determined_are_reported_rather_than_assumed(self) -> None:
        # A workflow token has no `gh auth status` scope line at all. Claiming
        # it passed would be a lie; claiming it failed would be a false alarm.
        unknown = next(
            c for c in check(healthy(scopes=None), local()) if c.name == "token-scopes"
        )
        assert unknown.ok is True
        assert "could not" in unknown.detail.lower()

    def test_every_required_scope_is_one_a_human_can_grant(self) -> None:
        assert set(REQUIRED_SCOPES) == {"repo", "project"}


class TestAgent:
    def test_a_repository_without_the_coding_agent_fails(self) -> None:
        assert by_name(healthy(agent_available=False))["coding-agent"] is False

    def test_the_remedy_offers_the_local_lane_as_the_alternative(self) -> None:
        failure = next(
            c for c in check(healthy(agent_available=False), local()) if c.name == "coding-agent"
        )
        assert "local" in failure.remedy


class TestBoard:
    def test_a_missing_field_fails_and_is_named(self) -> None:
        board = healthy_board()
        without = BoardSnapshot(
            fields=tuple(f for f in board.fields if f.name != FIELD_STATUS),
            labels=board.labels,
        )
        failure = next(
            c for c in check(healthy(board=without), local()) if c.name == "board-fields"
        )
        assert failure.ok is False
        assert FIELD_STATUS in failure.detail

    def test_the_built_in_status_options_fail_with_the_options_it_should_have(self) -> None:
        board = healthy_board()
        built_in = BoardSnapshot(
            fields=tuple(
                ExistingField(f.name, f.id, ("Todo", "In Progress", "Done"))
                if f.name == FIELD_STATUS
                else f
                for f in board.fields
            ),
            labels=board.labels,
        )
        failure = next(
            c for c in check(healthy(board=built_in), local()) if c.name == "board-fields"
        )
        assert failure.ok is False
        assert "Auto-merging" in failure.detail

    def test_a_missing_label_fails_and_is_named(self) -> None:
        board = healthy_board()
        without = BoardSnapshot(fields=board.fields, labels=("dispatchkit",))
        failure = next(c for c in check(healthy(board=without), local()) if c.name == "labels")
        assert failure.ok is False
        assert "lane:cloud" in failure.detail


class TestLocalFacts:
    def test_a_missing_workflow_fails(self) -> None:
        assert by_name(healthy(), local(workflow_exists=False))["workflow"] is False

    def test_a_missing_config_is_reported_but_does_not_fail(self) -> None:
        # Every setting has a default, so no config is a valid choice; the
        # check exists to say which file would be read if there were one.
        missing = next(
            c for c in check(healthy(), local(config_exists=False)) if c.name == "config"
        )
        assert missing.ok is True
        assert ".github/dispatchkit.toml" in missing.detail

    def test_a_missing_plans_directory_fails_because_no_graph_can_be_applied(self) -> None:
        assert by_name(healthy(), local(plans_exists=False))["plans"] is False


class TestVerdict:
    def test_the_summary_marks_failures_distinctly(self) -> None:
        lines = summarise(check(healthy(agent_available=False), local()))
        assert any(line.startswith("FAIL") for line in lines)
        assert any(line.startswith("ok") for line in lines)

    def test_a_failing_check_prints_its_remedy(self) -> None:
        lines = summarise(check(healthy(scopes=()), local()))
        assert any("gh auth refresh" in line for line in lines)


class TestPayloadParsing:
    """The three `gh` payloads `doctor` reads, parsed offline."""

    def test_token_scopes_are_read_from_gh_auth_status(self) -> None:
        text = (
            "github.com\n"
            "  ✓ Logged in to github.com account octocat (keyring)\n"
            "  - Active account: true\n"
            "  - Git operations protocol: https\n"
            "  - Token: gho_************\n"
            "  - Token scopes: 'gist', 'read:org', 'repo'\n"
        )
        assert parse_token_scopes(text) == ("gist", "read:org", "repo")

    def test_a_token_with_no_scope_line_is_unknown_not_empty(self) -> None:
        assert parse_token_scopes("  ✓ Logged in to github.com\n") is None

    def test_an_explicitly_empty_scope_list_is_empty_not_unknown(self) -> None:
        assert parse_token_scopes("  - Token scopes: none\n") == ()

    def test_labels_are_read_from_the_json_list(self) -> None:
        assert parse_labels([{"name": "dispatchkit"}, {"name": "bug"}]) == (
            "dispatchkit",
            "bug",
        )

    def test_the_project_view_yields_fields_options_and_the_item_count(self) -> None:
        snapshot = parse_project(
            {
                "fields": [
                    {"id": "PVTF_1", "name": "Task ID"},
                    {
                        "id": "PVTF_2",
                        "name": "Lane",
                        "options": [
                            {"id": "o1", "name": "cloud"},
                            {"id": "o2", "name": "local"},
                        ],
                    },
                ]
            },
            items=4,
            labels=("dispatchkit",),
        )
        assert snapshot.field("Lane") == ExistingField("Lane", "PVTF_2", ("cloud", "local"))
        assert snapshot.field("Task ID") == ExistingField("Task ID", "PVTF_1", ())
        assert snapshot.items == 4
