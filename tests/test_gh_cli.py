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
    assign_command,
    create_issue_command,
    item_add_command,
    item_edit_command,
    label_command,
    parse_agent_actor,
    parse_field_catalog,
    parse_state,
    state_command,
    update_issue_command,
)

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

    def test_reads_project_item_id_and_field_values(self) -> None:
        first = parse_state(recorded()).issues[0]
        assert first.project_item_id == "PVTI_lADOBQfyVc0FoZzgAKLM"
        assert first.fields["Task ID"] == "ports"
        assert first.fields["Lane"] == "cloud"

    def test_reads_scheduler_owned_fields_without_apply_writing_them(self) -> None:
        first = parse_state(recorded()).issues[0]
        assert first.fields["Status"] == "Ready"
        assert first.fields["Attempts"] == "0"

    def test_field_value_nodes_without_a_field_are_ignored(self) -> None:
        # The fixture carries a `ProjectV2ItemFieldLabelValue` we do not model;
        # an unmodelled type must be skipped, never guessed at.
        first = parse_state(recorded()).issues[0]
        assert set(first.fields) == {"Task ID", "Lane", "Verify", "Status", "Attempts"}

    def test_closed_issues_are_recognised(self) -> None:
        payload = recorded()
        payload["data"]["repository"]["issues"]["nodes"][0]["state"] = "CLOSED"
        assert parse_state(payload).issues[0].closed is True

    def test_issue_without_a_project_item_parses(self) -> None:
        payload = recorded()
        payload["data"]["repository"]["issues"]["nodes"][0]["projectItems"] = {"nodes": []}
        issue = parse_state(payload).issues[0]
        assert issue.project_item_id is None
        assert issue.fields == {}

    def test_reads_assignees_the_scheduler_treats_as_the_dispatch_lock(self) -> None:
        issues = parse_state(recorded()).issues
        assert issues[0].assignees == ()
        assert issues[1].assignees == ("copilot-swe-agent",)

    def test_only_open_linked_pull_requests_count_as_work_in_flight(self) -> None:
        # The fixture's timeline carries a merged/closed PR, an open one, and a
        # cross-referencing issue. Only the open PR means "work is happening".
        assert parse_state(recorded()).issues[1].open_prs == (42,)

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


class TestFieldCatalog:
    def test_reads_field_ids_and_single_select_options(self) -> None:
        catalog = parse_field_catalog(
            {
                "fields": [
                    {"id": "PVTF_1", "name": "Task ID"},
                    {
                        "id": "PVTF_2",
                        "name": "Lane",
                        "options": [
                            {"id": "opt_cloud", "name": "cloud"},
                            {"id": "opt_local", "name": "local"},
                        ],
                    },
                ]
            }
        )
        assert catalog.ids["Task ID"] == "PVTF_1"
        assert catalog.option_id("Lane", "local") == "opt_local"
        assert catalog.option_id("Task ID", "anything") is None


class TestCommands:
    def test_commands_are_argv_lists_never_shell_strings(self) -> None:
        commands = [
            state_command("owner", "repo"),
            create_issue_command("owner/repo", "T", ["dispatchkit"]),
            update_issue_command("owner/repo", 3, "T", ["a"], ["b"]),
            label_command("owner/repo", "lane:cloud"),
            item_add_command(1, "owner", "https://github.com/owner/repo/issues/3"),
            item_edit_command(
                project_id="PVT_1", item_id="PVTI_1", field_id="PVTF_1", value="x", option_id=None
            ),
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

    def test_single_select_fields_are_set_by_option_id(self) -> None:
        command = item_edit_command(
            project_id="PVT_1",
            item_id="PVTI_1",
            field_id="PVTF_2",
            value="cloud",
            option_id="opt_cloud",
        )
        assert "--single-select-option-id" in command
        assert "--text" not in command


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
        query = next(
            part for part in assign_command("I_1", "BOT_9") if part.startswith("query=")
        )
        assert "I_1" not in query and "BOT_9" not in query


class TestNodeIds:
    def test_issue_node_ids_are_read_from_the_state_query(self) -> None:
        assert parse_state(recorded()).issues[0].node_id == "I_kwDOports"

    def test_a_recording_without_node_ids_still_parses(self) -> None:
        payload = recorded()
        del payload["data"]["repository"]["issues"]["nodes"][0]["id"]
        assert parse_state(payload).issues[0].node_id is None
