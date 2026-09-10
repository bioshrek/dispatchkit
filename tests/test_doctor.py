"""D5.5: `dispatchkit doctor` — the adoption on-ramp, and the live tier's assertions.

Everything the pipeline needs but does not create for itself is a way for a
first run to fail confusingly: no credential at all, a repository with no
coding agent seat, a missing `dispatchkit` label that turns every pass into a
silent no-op.

`doctor` is the pure function that turns those facts into a verdict, so the
whole check set is testable offline against a synthetic snapshot — and the
same function is what the `live` tier will assert on against a real one.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from dispatchkit.doctor import (
    REQUIRED_SCOPES,
    Check,
    Diagnostics,
    LocalFacts,
    _merge_gate,
    check,
    check_local,
    check_remote,
    parse_labels,
    parse_token_scopes,
    summarise,
)
from dispatchkit.github import REQUIRED_LABELS

pytestmark = pytest.mark.unit


def healthy(**overrides: object) -> Diagnostics:
    base: dict[str, object] = {
        "scopes": ("repo", "read:org"),
        "agent_available": True,
        "labels": REQUIRED_LABELS,
        "protected_branch": True,
    }
    return Diagnostics(**{**base, **overrides})  # type: ignore[arg-type]


def local(**overrides: object) -> LocalFacts:
    base: dict[str, object] = {
        "config_path": Path(".github/dispatchkit.toml"),
        "config_exists": True,
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
        assert any(line.startswith("ok   labels") for line in lines)
        assert not any(line.startswith("FAIL") for line in lines)


class TestToken:
    def test_the_project_scope_is_no_longer_asked_for(self) -> None:
        # D14: `gh auth refresh -s project` was the wall every new adopter hit
        # first, and the board it was for is gone.
        assert "project" not in REQUIRED_SCOPES
        assert by_name(healthy(scopes=("repo",)))["token-scopes"] is True

    def test_the_missing_scope_is_named_with_the_command_that_grants_it(self) -> None:
        failure = next(c for c in check(healthy(scopes=()), local()) if c.name == "token-scopes")
        assert "repo" in failure.detail
        assert "gh auth refresh" in failure.remedy

    def test_a_token_that_reports_no_scopes_is_a_failure_not_a_shrug(self) -> None:
        # This branch used to pass, because a workflow token reports no scope
        # line. It also covered "not logged in at all", which is the case that
        # actually happens to the human running `doctor` at a terminal.
        unknown = next(c for c in check(healthy(scopes=None), local()) if c.name == "token-scopes")
        assert unknown.ok is False
        assert "gh auth login" in unknown.remedy

    def test_every_required_scope_is_one_a_human_can_grant(self) -> None:
        assert set(REQUIRED_SCOPES) == {"repo"}


class TestAgent:
    def test_a_repository_without_the_coding_agent_fails(self) -> None:
        assert by_name(healthy(agent_available=False))["coding-agent"] is False

    def test_the_remedy_offers_the_local_lane_as_the_alternative(self) -> None:
        failure = next(
            c for c in check(healthy(agent_available=False), local()) if c.name == "coding-agent"
        )
        assert "local" in failure.remedy


class TestLabels:
    def test_a_missing_label_fails_and_is_named(self) -> None:
        failure = next(
            c for c in check(healthy(labels=("dispatchkit",)), local()) if c.name == "labels"
        )
        assert failure.ok is False
        assert "lane:cloud" in failure.detail

    def test_the_label_the_state_query_filters_on_is_required(self) -> None:
        # Without it the query matches nothing and a pass is a silent no-op,
        # which reads exactly like an empty backlog.
        failure = next(c for c in check(healthy(labels=()), local()) if c.name == "labels")
        assert "dispatchkit" in failure.detail

    def test_the_remedy_needs_no_project_number(self) -> None:
        failure = next(c for c in check(healthy(labels=()), local()) if c.name == "labels")
        assert "--project" not in failure.remedy

    def test_nothing_about_a_board_is_checked_any_more(self) -> None:
        assert not any(c.name == "board-fields" for c in check(healthy(), local()))


class TestLocalFacts:
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
    """The `gh` payloads `doctor` reads, parsed offline."""

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


class TestTheSchedulerIsLocalNow:
    """D13 deleted the workflow, and with it four checks.

    `doctor` answers "can this repository run a pass?", and since the pass runs
    from a terminal the answer no longer involves a file in
    `.github/workflows/`, a PYTHONPATH pointing at a source tree that may not
    exist, or a token and a plan name installed as Actions configuration. A
    check that survives its subsystem is worse than no check: it fails an
    adopter for not having something nothing needs.
    """

    def test_nothing_about_a_workflow_is_checked_any_more(self) -> None:
        names = {c.name for c in check(healthy(), local())}
        assert not any(name.startswith("workflow") for name in names)

    def test_a_tree_with_no_dot_github_at_all_is_still_healthy(self) -> None:
        # The whole point: an adopter needs a config and a plans directory,
        # and nothing else on disk.
        assert all(c.ok for c in check_local(local()))


class TestTheMergeGate:
    """D9: whether a merge dispatchkit performs has a second lock behind it.

    Neither of these can stop `merge_ops` — dispatchkit checks CI itself, and
    that gate stands on its own. What they change is how much is riding on it.
    On an unprotected branch a merge is single-gated: if dispatchkit's reading
    of CI is ever wrong, nothing else is looking. That is worth saying out loud
    rather than leaving an adopter to infer it.
    """

    @staticmethod
    def _check(*, protected: bool) -> Check:
        return _merge_gate(
            Diagnostics(
                scopes=("repo",),
                agent_available=True,
                labels=REQUIRED_LABELS,
                protected_branch=protected,
            )
        )

    def test_a_protected_branch_passes(self) -> None:
        assert self._check(protected=True).ok

    def test_an_unprotected_branch_is_reported(self) -> None:
        result = self._check(protected=False)
        assert not result.ok
        assert "single" in result.detail or "only gate" in result.detail

    def test_the_remedy_names_the_required_check(self) -> None:
        # An adopter who reads only the remedy line still ends up protected.
        assert "protection" in self._check(protected=False).remedy.lower()

    def test_it_is_part_of_the_remote_checks(self) -> None:
        names = [
            item.name
            for item in check_remote(
                Diagnostics(
                    scopes=("repo",),
                    agent_available=True,
                    labels=REQUIRED_LABELS,
                    protected_branch=False,
                )
            )
        ]
        assert "merge-gate" in names
