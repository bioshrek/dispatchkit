"""D3: the `gh` adapter's pure halves — payload parsing and argv construction.

The subprocess shim itself is exercised for real at D5; everything that can be
checked offline is checked here, including the rule that nothing is ever handed
to a shell.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from dispatchkit.gh_cli import (
    AGENT_LOGIN,
    STATE_QUERY,
    _parse_open_prs,
    assign_command,
    auth_status_command,
    create_issue_command,
    label_command,
    label_list_command,
    merge_command,
    parse_agent_actor,
    parse_protection,
    parse_state,
    protection_command,
    ready_command,
    state_command,
    update_issue_command,
)
from dispatchkit.model import Checks, PullRequest

pytestmark = pytest.mark.replay

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "search_issues.json"


def recorded() -> dict[str, Any]:
    payload: dict[str, Any] = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return payload


class TestParseState:
    def test_reads_issues_labels_and_state(self) -> None:
        state = parse_state(recorded())
        first = state.issues[0]

        assert [issue.number for issue in state.issues] == [12, 13]
        assert first.closed is False
        assert "dispatchkit" in first.labels
        assert "verify:auto" in first.labels

    def test_closed_issues_are_recognised(self) -> None:
        payload = recorded()
        payload["data"]["repository"]["issues"]["nodes"][0]["state"] = "CLOSED"
        assert parse_state(payload).issues[0].closed is True

    def test_the_query_asks_for_nothing_about_a_project(self) -> None:
        # D14: the board is retired, and a query that still selected
        # `projectItems` would need the `project` scope to answer -- the exact
        # wall every adopter used to hit.
        assert "projectItems" not in STATE_QUERY
        assert "ProjectV2" not in STATE_QUERY

    def test_a_recording_that_still_carries_project_items_is_read_past(self) -> None:
        # The fixture predates D14 and is kept as recorded. Extra keys in a
        # response are never an error; they are simply not looked at.
        assert recorded()["data"]["repository"]["issues"]["nodes"][0]["projectItems"]
        assert parse_state(recorded()).issues[0].number == 12

    def test_reads_assignees_the_scheduler_treats_as_the_dispatch_lock(self) -> None:
        issues = parse_state(recorded()).issues
        assert issues[0].assignees == ()
        assert issues[1].assignees == ("copilot-swe-agent",)

    def test_only_open_linked_pull_requests_count_as_work_in_flight(self) -> None:
        # The fixture's timeline carries a merged/closed PR, an open one, and a
        # cross-referencing issue. Only the open PR means "work is happening".
        assert parse_state(recorded()).issues[1].open_prs == (PullRequest(42, Checks.NONE),)

    def test_an_issue_with_no_timeline_has_no_open_prs(self) -> None:
        assert parse_state(recorded()).issues[0].open_prs == ()

    def test_a_payload_without_the_new_keys_still_parses(self) -> None:
        # Older recordings predate the D4 query; they must degrade to "nothing
        # in flight" rather than blow up mid-pass.
        payload = recorded()
        node = payload["data"]["repository"]["issues"]["nodes"][1]
        del node["assignees"]
        del node["timelineItems"]
        issue = parse_state(payload).issues[1]
        assert (issue.assignees, issue.open_prs) == ((), ())


class TestParseCheckSuites:
    """CI's verdict comes from check *suites*, not the status rollup.

    This is not a stylistic preference. A workflow run held for approval
    produces a suite whose conclusion is `ACTION_REQUIRED` and which contains
    **zero check runs** — and `statusCheckRollup` is assembled from check runs,
    so it comes back `null`. Reading the rollup would make a stalled pull
    request indistinguishable from one in a repository with no CI at all.
    Recorded against the live sandbox; see docs/design.md, D5.6.
    """

    @staticmethod
    def _with_suites(*suites: dict[str, Any]) -> dict[str, Any]:
        payload = recorded()
        node = payload["data"]["repository"]["issues"]["nodes"][1]
        for event in node["timelineItems"]["nodes"]:
            source = event.get("source") or {}
            if source.get("state") == "OPEN":
                suite_nodes = {"nodes": list(suites)}
                source["commits"] = {"nodes": [{"commit": {"checkSuites": suite_nodes}}]}
        return payload

    def _checks(self, *suites: dict[str, Any]) -> Checks:
        return parse_state(self._with_suites(*suites)).issues[1].open_prs[0].checks

    def test_a_run_awaiting_approval_is_blocked(self) -> None:
        assert (
            self._checks({"status": "COMPLETED", "conclusion": "ACTION_REQUIRED"}) is Checks.BLOCKED
        )

    def test_a_green_suite_is_passing(self) -> None:
        assert self._checks({"status": "COMPLETED", "conclusion": "SUCCESS"}) is Checks.PASSING

    def test_a_skipped_or_neutral_suite_does_not_count_against_the_pr(self) -> None:
        assert (
            self._checks(
                {"status": "COMPLETED", "conclusion": "SKIPPED"},
                {"status": "COMPLETED", "conclusion": "NEUTRAL"},
            )
            is Checks.PASSING
        )

    @pytest.mark.parametrize(
        "conclusion", ["FAILURE", "TIMED_OUT", "CANCELLED", "STARTUP_FAILURE", "STALE"]
    )
    def test_every_unsuccessful_conclusion_is_a_failure(self, conclusion: str) -> None:
        assert self._checks({"status": "COMPLETED", "conclusion": conclusion}) is Checks.FAILING

    @pytest.mark.parametrize("status", ["QUEUED", "IN_PROGRESS", "REQUESTED", "WAITING"])
    def test_a_suite_that_has_not_finished_is_pending(self, status: str) -> None:
        assert self._checks({"status": status, "conclusion": None}) is Checks.PENDING

    def test_one_blocked_suite_stalls_a_pr_whose_other_suites_are_green(self) -> None:
        assert (
            self._checks(
                {"status": "COMPLETED", "conclusion": "SUCCESS"},
                {"status": "COMPLETED", "conclusion": "ACTION_REQUIRED"},
            )
            is Checks.BLOCKED
        )

    def test_an_unknown_conclusion_is_not_treated_as_success(self) -> None:
        # GitHub adds enum members; guessing green would auto-merge on a
        # verdict we do not understand.
        assert self._checks({"status": "COMPLETED", "conclusion": "SOMETHING_NEW"}) is (
            Checks.FAILING
        )

    def test_a_pr_with_no_suites_reports_none(self) -> None:
        assert self._checks() is Checks.NONE

    def test_a_payload_predating_the_check_query_degrades_to_none(self) -> None:
        assert parse_state(recorded()).issues[1].open_prs[0].checks is Checks.NONE


class TestCommands:
    def test_commands_are_argv_lists_never_shell_strings(self) -> None:
        commands = [
            state_command("owner", "repo"),
            create_issue_command("owner/repo", "T", ["dispatchkit"]),
            update_issue_command("owner/repo", 3, "T", ["a"], ["b"]),
            label_command("owner/repo", "lane:cloud"),
            ready_command(7, "owner/repo"),
            merge_command(7, "owner/repo"),
        ]
        for command in commands:
            assert command[0] == "gh"
            assert all(isinstance(part, str) for part in command)

    def test_issue_body_is_never_passed_as_an_argument(self) -> None:
        # Bodies contain newlines and backticks and would land in the process
        # table; `--body-file -` sends them over stdin instead.
        assert "--body-file" in create_issue_command("owner/repo", "T", [])
        assert "-" in create_issue_command("owner/repo", "T", [])

    def test_a_title_that_looks_like_a_flag_stays_one_argument(self) -> None:
        hostile = create_issue_command("owner/repo", "--repo evil/repo", [])
        benign = create_issue_command("owner/repo", "Ordinary title", [])

        assert len(hostile) == len(benign)  # no extra argv elements smuggled in
        assert hostile[hostile.index("--title") + 1] == "--repo evil/repo"
        assert hostile.count("--repo") == 1  # the flag itself, once

    def test_update_adds_and_removes_labels_in_one_call(self) -> None:
        command = update_issue_command("owner/repo", 3, "T", ["lane:cloud"], ["lane:local"])
        assert command[command.index("--add-label") + 1] == "lane:cloud"
        assert command[command.index("--remove-label") + 1] == "lane:local"

    def test_label_creation_is_idempotent(self) -> None:
        assert "--force" in label_command("owner/repo", "dispatchkit")


class TestDiagnosticCommands:
    """The commands `doctor` and `init` add (D5.5), argv-checked offline.

    Every one of them is now a repository command. The `gh project ...` family
    went with the board (D14), and with it the last reason to hold a token
    scope beyond `repo`.
    """

    def test_labels_are_listed_by_name_only(self) -> None:
        command = label_list_command("owner/repo")
        assert command[:3] == ["gh", "label", "list"]
        assert command[command.index("--json") + 1] == "name"

    def test_the_label_list_defeats_ghs_page_limit(self) -> None:
        """`gh label list` fetches 30 unless told otherwise.

        Truncation is silent and reads exactly like a missing label, so
        `doctor` would report a label the repository has and `init` would try
        to create it again.
        """
        command = label_list_command("owner/repo")
        assert int(command[command.index("--limit") + 1]) >= 100

    def test_every_new_command_is_an_argv_list(self) -> None:
        commands = [
            label_list_command("owner/repo"),
            auth_status_command(),
        ]
        for command in commands:
            assert command[0] == "gh"
            assert all(isinstance(part, str) for part in command)

    def test_no_command_reaches_for_a_project(self) -> None:
        commands = [
            state_command("owner", "repo"),
            label_list_command("owner/repo"),
            create_issue_command("owner/repo", "t", ()),
            auth_status_command(),
        ]
        for command in commands:
            assert "project" not in command


class TestAgentAssignment:
    """Assigning the coding agent is a GraphQL mutation, not `--add-assignee`.

    The agent is a bot actor, so it has to be looked up by capability and
    replaced through `replaceActorsForAssignable`.
    """

    def test_the_agent_actor_is_found_by_login_among_suggested_actors(self) -> None:
        payload: dict[str, Any] = {
            "data": {
                "repository": {
                    "suggestedActors": {
                        "nodes": [
                            {"login": "some-human", "__typename": "User", "id": "U_1"},
                            {"login": AGENT_LOGIN, "__typename": "Bot", "id": "BOT_9"},
                        ]
                    }
                }
            }
        }
        assert parse_agent_actor(payload) == "BOT_9"

    def test_a_repository_without_the_agent_enabled_yields_nothing(self) -> None:
        payload: dict[str, Any] = {"data": {"repository": {"suggestedActors": {"nodes": []}}}}
        assert parse_agent_actor(payload) is None

    def test_the_assignment_names_the_issue_node_and_the_actor(self) -> None:
        command = assign_command("I_kwDO123", "BOT_9")
        assert command[:3] == ["gh", "api", "graphql"]
        assert "-F" in command
        joined = " ".join(command)
        assert "assignableId=I_kwDO123" in joined
        assert "actorIds[]=BOT_9" in joined

    def test_the_mutation_replaces_rather_than_appends(self) -> None:
        # `replaceActorsForAssignable` is what makes a double-assign a no-op,
        # which is what lets two concurrent passes converge.
        assert "replaceActorsForAssignable" in " ".join(assign_command("I_1", "BOT_9"))

    def test_ids_are_never_interpolated_into_the_query_text(self) -> None:
        # They arrive as `-F` variables, so a hostile id cannot rewrite the
        # mutation body.
        query = next(part for part in assign_command("I_1", "BOT_9") if part.startswith("query="))
        assert "I_1" not in query and "BOT_9" not in query


class TestNodeIds:
    def test_issue_node_ids_are_read_from_the_state_query(self) -> None:
        assert parse_state(recorded()).issues[0].node_id == "I_kwDOports"

    def test_a_recording_without_node_ids_still_parses(self) -> None:
        payload = recorded()
        del payload["data"]["repository"]["issues"]["nodes"][0]["id"]
        assert parse_state(payload).issues[0].node_id is None


class TestParseDraft:
    """`isDraft` is read from the payload, because nothing else answers it.

    A draft pull request cannot be merged, and the field that looks like it
    should say so does not: both live sandbox PRs reported
    `mergeStateStatus: CLEAN` while `isDraft` was true. The query therefore
    asks for `isDraft` directly and the parser stores it verbatim.
    """

    @staticmethod
    def _with_draft(value: object) -> dict[str, Any]:
        payload = recorded()
        node = payload["data"]["repository"]["issues"]["nodes"][1]
        for event in node["timelineItems"]["nodes"]:
            source = event.get("source") or {}
            if source.get("state") == "OPEN":
                if value is None:
                    source.pop("isDraft", None)
                else:
                    source["isDraft"] = value
        return payload

    def _draft(self, value: object) -> bool:
        return parse_state(self._with_draft(value)).issues[1].open_prs[0].draft

    def test_a_draft_pr_is_read_as_draft(self) -> None:
        assert self._draft(True) is True

    def test_a_ready_pr_is_not_draft(self) -> None:
        assert self._draft(False) is False

    def test_a_missing_field_is_not_treated_as_draft(self) -> None:
        # An absent field must not strand a task: reading it as draft would
        # hold `verify: auto` at `In Review` forever on a payload shape we
        # simply failed to ask for.
        assert self._draft(None) is False

    def test_the_query_asks_for_it(self) -> None:
        assert "isDraft" in STATE_QUERY


class TestMarkReadyCommand:
    """Taking a PR out of draft is `gh pr ready`, as an argv list."""

    def test_it_names_the_pr_and_the_repo(self) -> None:
        assert ready_command(7, "o/r") == [
            "gh",
            "pr",
            "ready",
            "7",
            "--repo",
            "o/r",
        ]

    def test_the_number_is_never_interpolated_into_a_string(self) -> None:
        # Every argument is its own element, so nothing a PR number could
        # contain reaches a shell. There is no shell.
        assert all(isinstance(part, str) for part in ready_command(7, "o/r"))
        assert " " not in "".join(ready_command(7, "o/r")[:3])


class TestMergeCommand:
    """D9's merge, and the two flags it must never carry.

    Both were established by probing a live repository rather than by reading
    the documentation, because the documentation does not say either of them.
    """

    def test_it_is_a_direct_squash_merge(self) -> None:
        assert merge_command(7, "o/r") == [
            "gh",
            "pr",
            "merge",
            "7",
            "--squash",
            "--repo",
            "o/r",
        ]

    def test_it_never_passes_auto(self) -> None:
        # `--auto` does not mean "wait for the checks". On a repository without
        # branch protection `gh pr merge --auto` merged instantly, silently and
        # with exit 0, having consulted nothing. The GraphQL mutation underneath
        # refuses that case; the CLI flag papers over it.
        assert "--auto" not in merge_command(7, "o/r")

    def test_it_never_passes_admin(self) -> None:
        # Branch protection refuses a red pull request even for a repository
        # admin — but `--admin` overrides exactly that, and the scheduler's
        # token usually belongs to an admin. This flag is the difference
        # between a second lock and no lock.
        assert "--admin" not in merge_command(7, "o/r")


class TestParsePullRequestFiles:
    """The fence needs paths, so the state query has to ask for them."""

    def test_the_query_asks_for_the_changed_paths(self) -> None:
        assert "files(first:" in STATE_QUERY.replace(" ", "")

    @staticmethod
    def _pr(files: object) -> PullRequest:
        node = {
            "timelineItems": {"nodes": [{"source": {"number": 7, "state": "OPEN", "files": files}}]}
        }
        return _parse_open_prs(node)[0]

    def test_paths_are_read_off_the_nodes(self) -> None:
        pr = self._pr({"nodes": [{"path": "src/app.py"}, {"path": "tests/t.py"}]})
        assert pr.files == ("src/app.py", "tests/t.py")

    def test_a_missing_file_list_is_empty_not_an_error(self) -> None:
        # And `merge_ops` refuses to merge on an empty list, so a truncated or
        # absent answer fails closed rather than merging past the fence.
        assert self._pr(None).files == ()


class TestProtectionCommand:
    """Reading branch protection, and treating "not protected" as an answer."""

    def test_it_asks_the_rest_api_for_the_default_branch(self) -> None:
        assert protection_command("o/r", "main") == [
            "gh",
            "api",
            "repos/o/r/branches/main/protection",
        ]

    def test_a_required_check_is_protection(self) -> None:
        payload = {"required_status_checks": {"contexts": ["check"]}}
        assert parse_protection(payload) is True

    def test_protection_without_a_required_check_does_not_count(self) -> None:
        # Requiring a review but no check leaves `verify: auto` ungated, which
        # is the case this check exists to catch.
        assert parse_protection({"required_pull_request_reviews": {}}) is False

    def test_an_empty_context_list_does_not_count(self) -> None:
        assert parse_protection({"required_status_checks": {"contexts": []}}) is False
