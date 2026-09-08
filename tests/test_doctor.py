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
    Check,
    Diagnostics,
    LocalFacts,
    _merge_gate,
    check,
    check_local,
    check_remote,
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
            ExistingField(spec.name, f"PVTF_{spec.name}", spec.options) for spec in REQUIRED_FIELDS
        ),
        labels=REQUIRED_LABELS,
        items=0,
    )


#: A workflow that brings its own copy of dispatchkit, as `init` writes it.
FETCHING_WORKFLOW = """jobs:
  tick:
    steps:
      - uses: actions/checkout@v4
        with:
          repository: bioshrek/dispatchkit
          path: .dispatchkit
      - name: Scheduler pass
        env:
          PYTHONPATH: .dispatchkit/src
"""

#: A workflow that expects dispatchkit to already be in the tree, as this
#: repository's own does.
VENDORED_WORKFLOW = """jobs:
  tick:
    steps:
      - uses: actions/checkout@v4
      - name: Scheduler pass
        env:
          PYTHONPATH: src
"""


def healthy(**overrides: object) -> Diagnostics:
    base: dict[str, object] = {
        "scopes": ("repo", "project", "read:org"),
        "agent_available": True,
        "board": healthy_board(),
        "variables": ("DISPATCHKIT_PLAN", "DISPATCHKIT_PROJECT"),
        "secrets": ("DISPATCHKIT_TOKEN",),
        "protected_branch": True,
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
        "workflow_text": FETCHING_WORKFLOW,
        "vendored": False,
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
        unknown = next(c for c in check(healthy(scopes=None), local()) if c.name == "token-scopes")
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


class TestWorkflowCanImportDispatchkit:
    """The workflow must get dispatchkit from somewhere that exists.

    The bug this check exists for: `init` wrote a workflow that put `src` on
    PYTHONPATH, which is only correct in dispatchkit's own repository. Every
    adopter's scheduler died on `No module named dispatchkit`, on a cron, with
    nobody watching. `doctor` reported `ok workflow` throughout, because all it
    asked was whether the file existed.
    """

    def test_a_workflow_that_fetches_its_own_source_is_fine(self) -> None:
        facts = local(workflow_text=FETCHING_WORKFLOW, vendored=False)
        assert by_name(healthy(), facts)["workflow-source"]

    def test_a_repository_holding_dispatchkit_may_use_its_own_tree(self) -> None:
        facts = local(workflow_text=VENDORED_WORKFLOW, vendored=True)
        assert by_name(healthy(), facts)["workflow-source"]

    def test_pointing_at_a_tree_that_is_not_there_fails(self) -> None:
        # The exact shape of the shipped bug.
        facts = local(workflow_text=VENDORED_WORKFLOW, vendored=False)
        assert not by_name(healthy(), facts)["workflow-source"]

    def test_the_failure_names_the_path_that_is_missing(self) -> None:
        facts = local(workflow_text=VENDORED_WORKFLOW, vendored=False)
        failed = next(c for c in check_local(facts) if c.name == "workflow-source")
        assert "src" in failed.detail
        assert failed.remedy

    def test_the_remedy_admits_that_init_will_not_overwrite(self) -> None:
        # `init` writes the workflow only when there is none, so "re-run init"
        # on its own sends the reader to a command that does nothing and
        # reports success.
        facts = local(workflow_text=VENDORED_WORKFLOW, vendored=False)
        failed = next(c for c in check_local(facts) if c.name == "workflow-source")
        assert "delete" in failed.remedy
        assert "will not overwrite" in failed.remedy

    def test_a_checkout_to_a_different_path_than_pythonpath_fails(self) -> None:
        # Two halves written in separate steps; a rename of one is silent.
        text = FETCHING_WORKFLOW.replace("path: .dispatchkit", "path: .elsewhere")
        facts = local(workflow_text=text, vendored=False)
        assert not by_name(healthy(), facts)["workflow-source"]

    def test_no_workflow_at_all_is_left_to_the_workflow_check(self) -> None:
        # One failure, one message: `workflow` already says the file is
        # missing, and a second line saying its PYTHONPATH is unreadable would
        # be noise pointing at the same fix.
        facts = local(workflow_exists=False, workflow_text="", vendored=False)
        checks = {c.name: c.ok for c in check_local(facts)}
        assert checks["workflow"] is False
        assert checks["workflow-source"] is True

    def test_a_workflow_with_no_pythonpath_at_all_fails(self) -> None:
        facts = local(workflow_text="jobs: {}")
        assert not by_name(healthy(), facts)["workflow-source"]


class TestWorkflowInputs:
    """The unattended pass needs a token and two variables, or it does nothing.

    Observed live: with none of them set, the scheduler ran on its cron and
    exited 1 with empty `--plan` and `--project`. That is a configuration
    mistake a first-time adopter cannot see without opening the Actions log.
    """

    def test_all_present_passes(self) -> None:
        assert by_name(healthy())["workflow-inputs"]

    def test_a_missing_variable_is_reported(self) -> None:
        diagnostics = healthy(variables=("DISPATCHKIT_PLAN",))
        assert not by_name(diagnostics)["workflow-inputs"]

    def test_a_missing_secret_is_reported(self) -> None:
        assert not by_name(healthy(secrets=()))["workflow-inputs"]

    def test_the_failure_names_what_to_set_and_how(self) -> None:
        diagnostics = healthy(variables=(), secrets=())
        failed = next(c for c in check_remote(diagnostics) if c.name == "workflow-inputs")
        assert "DISPATCHKIT_PLAN" in failed.detail
        assert "DISPATCHKIT_PROJECT" in failed.detail
        assert "DISPATCHKIT_TOKEN" in failed.detail
        assert "gh variable set" in failed.remedy

    def test_the_token_is_never_read_only_its_presence(self) -> None:
        # A secret's value is not readable through the API, and `doctor` must
        # not invite anyone to paste one on a command line to find out.
        diagnostics = healthy(secrets=())
        failed = next(c for c in check_remote(diagnostics) if c.name == "workflow-inputs")
        assert "gh secret set DISPATCHKIT_TOKEN" in failed.remedy

    def test_the_consequence_matches_what_is_actually_missing(self) -> None:
        # Reciting all three costs when only one input is absent trains the
        # reader to skim the line.
        diagnostics = healthy(secrets=())
        failed = next(c for c in check_remote(diagnostics) if c.name == "workflow-inputs")
        assert failed.detail.endswith("runs with no token")

    def test_several_missing_inputs_read_as_a_list(self) -> None:
        diagnostics = healthy(variables=(), secrets=())
        failed = next(c for c in check_remote(diagnostics) if c.name == "workflow-inputs")
        assert failed.detail.endswith("no plan, no board and no token")


class TestTheSecretIsOnlyKnownByName:
    """`ok` here must not read as "the token works", because it cannot mean that.

    Found live. Every check passed and `doctor` exited 0, then the scheduler
    failed with `Bad credentials (HTTP 401)`: the secret existed and its value
    was invalid. GitHub never discloses a secret's value, so validity is
    unknowable from here by construction — which makes it all the more
    important that the line says what it actually verified.
    """

    @staticmethod
    def _detail() -> str:
        found = [c for c in check(healthy(), local()) if c.name == "workflow-inputs"]
        assert found and found[0].ok
        return found[0].detail

    def test_the_passing_message_says_it_only_saw_the_name(self) -> None:
        assert "name" in self._detail()

    def test_it_does_not_claim_the_token_is_valid(self) -> None:
        for overclaim in ("valid", "works", "usable"):
            assert overclaim not in self._detail()


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
                scopes=("repo", "project"),
                agent_available=True,
                board=healthy_board(),
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
                    scopes=("repo", "project"),
                    agent_available=True,
                    board=healthy_board(),
                    protected_branch=False,
                )
            )
        ]
        assert "merge-gate" in names
